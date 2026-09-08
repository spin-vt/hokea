"""Checks: functions that read only a run's files, answering one question each.

Every verdict is three-way — violated / clean / insufficient_evidence — and
the third is reported explicitly: missing data must never look like a pass.
A violation carries the exact recorded operations that prove it, so you can
verify the conclusion by hand.

There is no linearizability checker here. Deciding linearizability requires
searching over orderings of concurrent operations, which is easy to get
subtly wrong (we will cover linearizability in lecture). If you claim
linearizability, export the history to Porcupine instead
(`hokea export --format porcupine`).
"""

from collections import Counter
from dataclasses import dataclass, field

from .runs import validate_history

VIOLATED = "violated"
CLEAN = "clean"
INSUFFICIENT = "insufficient_evidence"


class InsufficientEvidence(Exception):
    """Raised when a metric is read from data that cannot support it."""


@dataclass
class Verdict:
    """What every check returns: a three-way `status` (violated / clean /
    insufficient_evidence), a one-sentence `reason`, and the recorded
    operations that prove it in `evidence`."""

    check: str
    status: str            # violated | clean | insufficient_evidence
    reason: str            # one human sentence, always set
    evidence: list = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def __repr__(self):
        return f"<{self.check}: {self.status} — {self.reason}>"


def _unreachable_nodes(states: dict) -> list:
    return sorted(i for i, s in states.items()
                  if isinstance(s, dict) and s.get("unreachable"))


# ---------------------------------------------------------------------------

def converged(states: dict) -> Verdict:
    """Do all nodes report the same final state? states is {node_index: state}
    as captured by the run's state reader."""
    if not states:
        return Verdict("converged", INSUFFICIENT,
                       "no state snapshots were captured — nothing to compare")
    bad = _unreachable_nodes(states)
    if bad:
        return Verdict("converged", INSUFFICIENT,
                       f"nodes {bad} were unreachable at state capture; "
                       "a missing node must not silently pass convergence")
    if len(states) == 1:
        return Verdict("converged", CLEAN, "single node: trivially converged")

    indexes = sorted(states)
    reference = indexes[0]
    for other in indexes[1:]:
        a, b = states[reference], states[other]
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                return Verdict(
                    "converged", VIOLATED,
                    f"nodes {reference} and {other} disagree on {key!r}",
                    evidence=[{"key": key,
                               f"node_{reference}": a.get(key),
                               f"node_{other}": b.get(key)}])
    return Verdict("converged", CLEAN,
                   f"all {len(states)} nodes report identical state")


def acked_visible(history: list, states: dict, *, write_ops: list[str],
                  visible=None) -> Verdict:
    """Is the effect of every acknowledged write present in the final state?

    By default this checks *existence*: for each op in `write_ops` with
    outcome "ok", the op's key must appear in every node's final state. Use
    that for writes whose effect is "this thing now exists" (a confirmed
    sale, a created record). Existence is the safe default because nothing
    that happens later — except a delete — can legitimately erase it, so
    the check holds no matter how concurrent operations interleaved.

    `visible` replaces the existence test with your own: a function taking
    (one node's state, one history record) and returning True if that
    write's effect is present there. Examples:

        # the value this write set is what the node ended up with
        visible=lambda state, op: state.get(op["key"]) == op["args"]["value"]
        # an acknowledged delete stayed deleted
        visible=lambda state, op: op["key"] not in state

    WARNING — read before writing a value-level `visible`: a write's value
    only has to survive until some later write legitimately replaces it.
    Under concurrent random traffic you usually cannot know which write came
    last (two overlapping writes have no defined winner), so checking "my
    value is still there" against every acked write will flag correct
    systems. A value-level `visible` is only sound for writes you *arranged*
    to be last: issue them with a Script step after the main workload, give
    the system settle time, then check. For everything else, stick with the
    existence default — and pick `write_ops` so "acknowledged" implies
    "should still exist" (not holds that expire, not things later deleted).
    """
    validate_history(history)
    if visible is None:
        visible = lambda state, op: op["key"] in state
    if not states:
        return Verdict("acked_visible", INSUFFICIENT,
                       "no state snapshots were captured")
    bad = _unreachable_nodes(states)
    if bad:
        return Verdict("acked_visible", INSUFFICIENT,
                       f"nodes {bad} were unreachable at state capture")
    acked = [r for r in history
             if r["op"] in write_ops and r["outcome"] == "ok"]
    if not acked:
        n_unknown = sum(1 for r in history if r["op"] in write_ops
                        and r["outcome"] == "unknown")
        return Verdict("acked_visible", INSUFFICIENT,
                       f"no acknowledged {write_ops} operations in the history "
                       f"({n_unknown} with unknown outcome) — nothing to check")

    lost = []
    for record in acked:
        missing_on = [i for i in sorted(states) if not visible(states[i], record)]
        if missing_on:
            lost.append({"op": record, "missing_on_nodes": missing_on})
    if lost:
        return Verdict(
            "acked_visible", VIOLATED,
            f"{len(lost)} acknowledged write(s) whose effect is missing from "
            f"final state (first: {lost[0]['op']['op']} "
            f"{lost[0]['op']['key']!r} acked at "
            f"t={lost[0]['op']['return_ts']}, missing on nodes "
            f"{lost[0]['missing_on_nodes']})",
            evidence=lost,
            details={"acked_writes": len(acked), "lost": len(lost)})
    return Verdict("acked_visible", CLEAN,
                   f"all {len(acked)} acknowledged {write_ops} writes are "
                   "visible on every node")


def session(history: list) -> Verdict:
    """Within each client, server-supplied versions never go backwards — a
    plain comparison, catching a client reading older state than it already
    saw. Only for systems whose version token is global (like etcd's
    revision); meaningless for per-key versions."""
    validate_history(history)
    versioned = [r for r in history
                 if r["outcome"] == "ok" and r["version"] is not None]
    if not versioned:
        return Verdict("session", INSUFFICIENT,
                       "no operation carries a version token — this system "
                       "does not supply one, so the session check cannot apply")

    last: dict = {}  # client_id -> last (version, record)
    for record in sorted(versioned, key=lambda r: (r["client_id"], r["seq"])):
        client = record["client_id"]
        if client in last:
            prev_version, prev_record = last[client]
            try:
                went_back = record["version"] < prev_version
            except TypeError as e:
                raise ValueError(
                    f"version tokens are not comparable: "
                    f"{prev_version!r} vs {record['version']!r}") from e
            if went_back:
                return Verdict(
                    "session", VIOLATED,
                    f"client {client} saw version {record['version']!r} after "
                    f"already seeing {prev_version!r}",
                    evidence=[prev_record, record])
        last[client] = (record["version"], record)
    return Verdict("session", CLEAN,
                   f"versions monotonic within each of {len(last)} client(s) "
                   f"({len(versioned)} versioned ops)")


def event_join(events: list, *, require: str, for_each: str, on: str) -> Verdict:
    """Every event of type `for_each` has a matching event of type `require`
    with the same value of field `on` — e.g. every "ack" is eventually
    followed by a "commit" for the same op id."""
    if not events:
        return Verdict("event_join", INSUFFICIENT,
                       "no events were collected — is the system emitting "
                       "JSON lines with a \"type\" field on stdout?")
    needles = [e for e in events if e.get("type") == for_each]
    if not needles:
        return Verdict("event_join", INSUFFICIENT,
                       f"no {for_each!r} events found "
                       f"({len(events)} events total) — nothing to check")
    for e in needles:
        if on not in e:
            raise ValueError(f"{for_each!r} event missing join field {on!r}: {e}")
    satisfied = {e[on] for e in events if e.get("type") == require and on in e}
    unmatched = [e for e in needles if e[on] not in satisfied]
    if unmatched:
        return Verdict(
            "event_join", VIOLATED,
            f"{len(unmatched)} of {len(needles)} {for_each!r} event(s) have "
            f"no matching {require!r} (first: {on}={unmatched[0][on]!r})",
            evidence=unmatched,
            details={"for_each": len(needles), "unmatched": len(unmatched)})
    return Verdict("event_join", CLEAN,
                   f"every {for_each!r} ({len(needles)}) has a matching "
                   f"{require!r}")


# ---------------------------------------------------------------------------
# availability: descriptive numbers per fault window and side
# ---------------------------------------------------------------------------

@dataclass
class SideStats:
    name: str
    n: int
    outcomes: dict
    errors: dict
    _ok_latency_ms: list
    _end_latency_ms: list  # all ops: how long until the client got an answer

    @property
    def success_rate(self) -> float:
        if self.n == 0:
            raise InsufficientEvidence(
                f"no operations recorded on side {self.name!r} — a rate over "
                "zero ops would be meaningless")
        return self.outcomes.get("ok", 0) / self.n

    @property
    def latency_ms(self) -> dict:
        return _percentile_summary(self._ok_latency_ms)

    @property
    def answer_ms(self) -> dict:
        """Latency to any answer, including failures and timeouts — asserts
        like "failures are fast, not hangs" read this."""
        return _percentile_summary(self._end_latency_ms)


@dataclass
class Window:
    label: str            # "baseline", "partition", "kill:2", ...
    start: float
    end: float
    sides: dict = field(default_factory=dict)  # side name -> SideStats

    def side(self, name: str) -> SideStats:
        if name not in self.sides:
            raise KeyError(f"window {self.label!r} has no side {name!r}; "
                           f"sides: {sorted(self.sides)}")
        return self.sides[name]


class Availability:
    def __init__(self, windows: list):
        self.windows = windows

    def window(self, label: str) -> Window:
        matches = [w for w in self.windows if w.label == label
                   or w.label.startswith(f"{label}:")]
        if not matches:
            raise KeyError(f"no {label!r} window in this run's fault "
                           f"timeline; windows: {[w.label for w in self.windows]}")
        if len(matches) > 1:
            raise KeyError(f"{len(matches)} windows match {label!r}; "
                           f"pick one of {[w.label for w in matches]}")
        return matches[0]


def availability(history: list, faults: list) -> Availability:
    """Slice the history by the fault timeline: descriptive numbers per fault
    window and per side, for tests to assert against.

    Each fault produces a window starting when it was VERIFIED/done —
    operations during the multi-second install gap belong to neither the
    baseline nor the fault — and ending when the matching recovery was
    requested. Sides split operations by the node they targeted:
    majority/minority for a 2-group partition (group-1/group-2 when sizes
    tie), faulted-node/others for process faults.

    Foreign chaos (experiments applied outside hokea, recorded by the k8s
    backend's watcher as chaos/chaos_done/chaos_recovered) windows the same
    way: open at chaos_done, closed by the first chaos_recovered for the
    SAME CR (matched on chaos_name), labeled `chaos:<kind>:<name>`, e.g.
    "chaos:stresschaos:my-stress". No node attribution — all operations
    fall on the single side "all".
    """
    validate_history(history)
    if not history:
        raise InsufficientEvidence("history is empty")
    end_of_run = max(r["return_ts"] for r in history) + 1e-6
    first_fault = faults[0]["ts"] if faults else end_of_run
    windows = [_make_window("baseline", 0.0, first_fault, {}, history)]

    for i, event in enumerate(faults):
        kind, sep, suffix = event["event"].rpartition("_")
        if suffix not in ("verified", "done"):
            continue  # only the *_verified/*_done event opens a window
        if kind not in _RECOVERY:
            continue  # start_done, unpause_done, heal_done: recoveries
        if "reapplied_after" in event:
            continue  # re-verification of a window that is already open
        if kind == "partition":
            node_to_side = _partition_sides(event["groups"])
            label = "partition"
        elif kind == "chaos":
            node_to_side = {}  # no node attribution: one side, "all"
            label = f"chaos:{event['kind']}:{event['chaos_name']}"
        else:
            node_to_side = _node_fault_sides(event["node"])
            label = f"{kind}:{event['node']}"
        end = next((later["ts"] for later in faults[i:]
                    if later["event"] == _RECOVERY[kind]
                    and _recovery_matches(event, later)),
                   end_of_run)
        windows.append(_make_window(label, event["ts"], end, node_to_side, history))
    return Availability(windows)


_RECOVERY = {"partition": "heal", "kill": "start", "stop": "start",
             "pause": "unpause", "slow": "heal", "chaos": "chaos_recovered"}


def _recovery_matches(opening: dict, recovery: dict) -> bool:
    """Does this recovery event close the window `opening` started? Node
    faults match on node; chaos events additionally carry a chaos_name and
    must match on it too (two overlapping foreign experiments each end at
    their OWN recovery, not the first one seen). Events without chaos_name
    behave exactly as before."""
    if recovery.get("node") not in (None, opening.get("node")):
        return False
    if "chaos_name" in opening or "chaos_name" in recovery:
        return opening.get("chaos_name") == recovery.get("chaos_name")
    return True


def _partition_sides(groups: list) -> dict:
    """node index -> side name."""
    if len(groups) == 2 and len(groups[0]) != len(groups[1]):
        big, small = sorted(groups, key=len, reverse=True)
        return {**{n: "majority" for n in big}, **{n: "minority" for n in small}}
    return {n: f"group-{k + 1}" for k, g in enumerate(groups) for n in g}


def _node_fault_sides(node: int) -> dict:
    return {node: "faulted-node"}  # every other node falls to the default side


def _make_window(label, start, end, node_to_side, history) -> Window:
    default = ("all" if not node_to_side
               else "others" if "faulted-node" in node_to_side.values()
               else "unassigned")
    buckets: dict = {name: [] for name in {*node_to_side.values(), default}}
    for record in history:
        if start <= record["invoke_ts"] < end:
            buckets[node_to_side.get(record["node"], default)].append(record)
    sides = {name: _side_stats(name, records)
             for name, records in buckets.items()}
    return Window(label, start, end, sides)


def _side_stats(name: str, records: list) -> SideStats:
    outcomes = Counter(r["outcome"] for r in records)
    errors = Counter(r["error"].split(":")[0] for r in records if r["error"])
    ok_ms = [(r["return_ts"] - r["invoke_ts"]) * 1000
             for r in records if r["outcome"] == "ok"]
    end_ms = [(r["return_ts"] - r["invoke_ts"]) * 1000 for r in records]
    return SideStats(name, len(records), dict(outcomes), dict(errors),
                     sorted(ok_ms), sorted(end_ms))


def _percentile_summary(sorted_ms: list) -> dict:
    if not sorted_ms:
        raise InsufficientEvidence("no operations to take latencies from")
    def at(p):
        return round(sorted_ms[min(len(sorted_ms) - 1, int(p * len(sorted_ms)))], 2)
    return {"p50": at(0.50), "p90": at(0.90), "p99": at(0.99),
            "max": round(sorted_ms[-1], 2), "count": len(sorted_ms)}
