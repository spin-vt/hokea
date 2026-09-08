"""Workload driver: concurrent clients executing an operation mix you
define against the cluster, with everything recorded.

You provide two pieces:

- an operation generator: `ops(client_id, rng)` returning a Python generator
  that yields Op objects. The driver sends each op's Result back into the
  generator, so reactive flows work naturally:

        def my_ops(client_id, rng):
            while True:
                res = yield Op("reserve", seat, {"client_id": f"c{client_id}"})
                if res.outcome == "ok":
                    yield Op("confirm", seat, {"rid": res.response["rid"]})

- a client adapter with `execute(op, node, timeout) -> Result`, speaking the
  system's own protocol. `node.url` is the node's base URL.

Each operation's outcome records what we KNOW about whether it took effect:

    ok       — yes: the system acknowledged it
    rejected — no: the server itself said no, and nothing changed
    unknown  — can't tell. When in doubt, use this.

hokea forces `unknown` whenever it observes a timeout or dropped connection
itself (a transport exception escaping the adapter), regardless of what the
adapter would have said — an adapter mistake can only make checks more
cautious, never wrongly confident. Any *other* exception from the adapter is
a bug in the adapter and crashes the run loudly. Clients never retry: each
operation is attempted exactly once and recorded exactly once.

Clients are worker processes (forked), so load is not capped by Python's
threading. Each worker writes its own shard; the parent merges them into
history.jsonl ordered by invocation time.
"""

import json
import multiprocessing
import os
import random
import time
import traceback
import contextlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import requests

from .runs import FaultLog, RunHandle, new_run_dir, write_jsonl

# Exceptions that mean "the harness observed a transport failure": the op's
# outcome is unknown no matter what the adapter thinks.
TRANSPORT_ERRORS = (requests.RequestException, OSError, TimeoutError)

# In load mode (an explicit `rate` above this), per-operation recording samples
# down to roughly this many lines/sec so pure load runs don't write
# multi-gigabyte histories. Correctness runs (no rate) always record everything.
RECORD_ALL_UP_TO_RATE = 500.0


@dataclass
class Op:
    op: str                       # operation kind, e.g. "reserve"
    key: str                      # logical object it touches, e.g. seat id
    args: dict = field(default_factory=dict)


@dataclass
class Result:
    outcome: str                  # "ok" | "rejected" | "unknown"
    status: int | None = None     # protocol status code, e.g. HTTP status
    response: object = None       # verbatim decoded reply — never summarized
    version: object = None        # server-supplied version token, if any
    error: str | None = None


def http_result(response, *, rejected: frozenset | set = frozenset(),
                version=None) -> Result:
    """Classify an HTTP response the safe way: 2xx -> ok, a status code you
    explicitly listed in `rejected` -> rejected, anything else -> unknown.

    The point of listing codes explicitly: "rejected" is a strong claim — the server
    said no AND nothing changed — so you must opt each status code into it
    deliberately. A code you didn't think about (a 503, a proxy's 502, an
    unexpected 500) can never be misfiled as rejected by accident; it lands
    on unknown, which is always safe.

        return http_result(r, rejected={400, 409})
    """
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if response.status_code < 300:
        outcome = "ok"
    elif response.status_code in rejected:
        outcome = "rejected"
    else:
        outcome = "unknown"
    return Result(outcome, status=response.status_code, response=body,
                  version=version)


class WorkerNode(NamedTuple):
    index: int
    url: str


def _execute_one(adapter, op: Op, node: WorkerNode, timeout: float) -> Result:
    """One attempt, exactly hokea's rules: transport errors become unknown
    no matter what; a malformed adapter return is a crash."""
    try:
        result = adapter.execute(op, node, timeout)
        if not isinstance(result, Result):
            raise TypeError(f"adapter returned {result!r}, expected a Result")
        if result.outcome not in ("ok", "rejected", "unknown"):
            raise ValueError(f"adapter returned bad outcome {result.outcome!r}")
        return result
    except TRANSPORT_ERRORS as e:
        return Result("unknown", error=f"{type(e).__name__}: {e}")


class Script:
    """A harness-side scripted client for choreographed steps: "at this point
    in the timeline, run this one operation against this one node."

        script = Script(MyAdapter())
        h = run_workload(cluster, ops, adapter, duration=30, script=script,
                         faults=[(10, lambda: net.partition([[1, 2], [3]])),
                                 (0,  lambda: script.do(Op("read", "k"), node=3))])

    Steps run in the fault schedule (parent process, in order — and since
    e.g. net.partition() returns only after the cut is verified, a step after
    it needs no timing guesses). Each step goes through the adapter with the
    same outcome rules as worker clients, returns its Result so later steps
    can use ids from responses, and is recorded in history.jsonl under the
    reserved client_id 0. Script ops are not counted as load: they are
    excluded from report.json."""

    def __init__(self, adapter, *, timeout: float = 5.0):
        self.adapter = adapter
        self.timeout = timeout
        self.records: list[dict] = []
        self._nodes: dict[int, WorkerNode] | None = None
        self._t0 = 0.0
        self._seq = 0

    def do(self, op: Op, *, node: int) -> Result:
        if self._nodes is None:
            raise RuntimeError("this Script is not attached to a run — pass "
                               "it to run_workload(script=...)")
        target = self._nodes[node]
        invoke_ts = time.monotonic()
        result = _execute_one(self.adapter, op, target, self.timeout)
        return_ts = time.monotonic()
        self.records.append({
            "client_id": 0, "seq": self._seq, "node": node,
            "op": op.op, "key": op.key, "args": op.args,
            "invoke_ts": round(invoke_ts - self._t0, 6),
            "return_ts": round(return_ts - self._t0, 6),
            "outcome": result.outcome, "status": result.status,
            "response": result.response, "version": result.version,
            "error": result.error,
        })
        self._seq += 1
        return result


def run_workload(cluster, ops, adapter, *, duration: float, clients: int = 4,
                 rate: float | None = None, faults: list | None = None,
                 assign: str = "round_robin", timeout: float = 5.0,
                 seed: int = 0, sample: float | None = None,
                 state_reader=None, script: Script | None = None,
                 settle: float = 0, name: str = "run",
                 run_dir: str | Path | None = None) -> RunHandle:
    """Drive `clients` concurrent workers for `duration` seconds, executing the
    fault schedule (`faults` is a list of (offset_seconds, callable), run in
    list order) from the parent process, and write the full run record to a
    run directory.

    `rate` is the target total ops/sec across all clients (None = closed loop:
    every client issues its next op as soon as the last returns).
    `assign` is how ops map to nodes: "round_robin", "pinned", or "random".
    `state_reader(node) -> dict` (optional) fetches each node's final state.
    `script` attaches a Script whose choreographed ops land in the history.
    `settle` waits that many quiet seconds after clients finish before the
    final states are captured — systems that sync in the background need it
    to converge; the wait is recorded in the fault timeline and metadata.
    """
    if assign not in ("round_robin", "pinned", "random"):
        raise ValueError(f"unknown assign mode {assign!r}")
    run_path = Path(run_dir) if run_dir else new_run_dir(name)
    shard_dir = run_path / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    nodes = [WorkerNode(n.index, n.url) for n in cluster.nodes()]

    sample_p = _sample_probability(rate, sample)
    if sample_p < 1.0:
        print(f"hokea: load mode at rate={rate}: recording ~{sample_p:.1%} of "
              f"operations (stats still cover all of them)", flush=True)

    mono_at_start = time.monotonic()
    metadata = {
        "name": name,
        "wall_at_start": time.time(),
        "mono_at_start": mono_at_start,
        "duration": duration,
        "clients": clients,
        "rate": rate,
        "assign": assign,
        "timeout": timeout,
        "seed": seed,
        "sample_p": sample_p,
        "settle": settle,
        "nodes": [{"index": n.index, "url": n.url} for n in nodes],
        "cluster": cluster.prefix,
    }
    (run_path / "metadata.json").write_text(json.dumps(metadata, indent=2))

    fault_events_start = len(cluster.fault_log.events)
    event_offsets = cluster.events.offsets() if cluster.events else {}
    if script is not None:
        script._nodes = {n.index: n for n in nodes}
        script._t0 = mono_at_start
    ctx = multiprocessing.get_context("fork")
    workers = []
    for client_id in range(1, clients + 1):
        p = ctx.Process(
            target=_worker_main,
            args=(client_id, ops, adapter, nodes, assign, timeout, seed,
                  sample_p, rate, clients, mono_at_start, duration, shard_dir),
            name=f"hokea-client-{client_id}",
        )
        p.start()
        workers.append(p)

    try:
        _run_fault_schedule(faults or [], mono_at_start)
        _join_workers(workers, mono_at_start + duration, shard_dir)
    finally:
        for p in workers:
            if p.is_alive():
                p.terminate()

    if settle:
        # Quiet time for background sync before the final state is judged.
        cluster.fault_log.record("settle", seconds=settle)
        time.sleep(settle)
        cluster.fault_log.record("settle_done", seconds=settle)

    history = _merge_shards(shard_dir, clients)
    if script is not None:
        history.extend(script.records)
        history.sort(key=lambda r: r["invoke_ts"])
        script._nodes = None
    write_jsonl(run_path / "history.jsonl", history)
    report = _build_report(shard_dir, clients, duration)
    (run_path / "report.json").write_text(json.dumps(report, indent=2))
    _write_faults(run_path, cluster.fault_log, fault_events_start, mono_at_start)
    if cluster.events:
        cluster.events.drain()  # in-flight log-follower flushes land first
        write_jsonl(run_path / "events.jsonl",
                    cluster.events.collect_since(event_offsets))
    if state_reader is not None:
        _capture_states(run_path, cluster, state_reader)

    metadata["completed"] = True
    (run_path / "metadata.json").write_text(json.dumps(metadata, indent=2))
    return RunHandle(run_path)


@contextlib.contextmanager
def record_run(cluster, *, name: str = "record", settle: float = 0,
               state_reader=None, run_dir: str | Path | None = None):
    """A run with no client workload: server nodes + faults + events only.

    For batch and training projects (a MapReduce job, distributed SGD) where
    nothing drives request/response load, but the fault timeline and the node
    event stream are still the record that checks and analysis read. Produces
    the same run directory as run_workload minus history.jsonl and
    report.json:

        with record_run(cluster, name="train-sync") as r:
            net.slow(3, delay_ms=200)
            time.sleep(600)              # nodes work, emitting events
            net.heal()
        check.event_join(r.events, require="commit", for_each="ack",
                         on="op_id")

    The yielded RunHandle's accessors are valid once the block exits. The
    record is written even if the body raises — but then without the
    `completed` marker in metadata.json, so a partial run is detectable.
    `settle` (quiet seconds before state capture, recorded in the timeline)
    and `state_reader` behave as in run_workload.
    """
    run_path = Path(run_dir) if run_dir else new_run_dir(name)
    mono_at_start = time.monotonic()
    metadata = {
        "name": name,
        "wall_at_start": time.time(),
        "mono_at_start": mono_at_start,
        "settle": settle,
        "nodes": [{"index": n.index, "url": n.url} for n in cluster.nodes()],
        "cluster": cluster.prefix,
    }
    (run_path / "metadata.json").write_text(json.dumps(metadata, indent=2))
    fault_events_start = len(cluster.fault_log.events)
    event_offsets = cluster.events.offsets() if cluster.events else {}

    finished = False
    try:
        yield RunHandle(run_path)
        finished = True
    finally:
        if finished and settle:
            cluster.fault_log.record("settle", seconds=settle)
            time.sleep(settle)
            cluster.fault_log.record("settle_done", seconds=settle)
        _write_faults(run_path, cluster.fault_log, fault_events_start,
                      mono_at_start)
        if cluster.events:
            cluster.events.drain()  # in-flight log-follower flushes land first
            write_jsonl(run_path / "events.jsonl",
                        cluster.events.collect_since(event_offsets))
        if state_reader is not None:
            _capture_states(run_path, cluster, state_reader)
        if finished:
            metadata["completed"] = True
            (run_path / "metadata.json").write_text(
                json.dumps(metadata, indent=2))


def _sample_probability(rate, sample) -> float:
    if sample is not None:
        if not 0 < sample <= 1:
            raise ValueError(f"sample must be in (0, 1], got {sample}")
        return sample
    if rate is not None and rate > RECORD_ALL_UP_TO_RATE:
        return RECORD_ALL_UP_TO_RATE / rate
    return 1.0


def _run_fault_schedule(faults, t0):
    """Actions run in list order, each no earlier than t0 + its offset. An
    action starts only after the previous one returned — and fault primitives
    like net.partition() return only once verified — so an offset of 0 means
    "immediately after the previous step", with no timing guesswork."""
    for offset, action in faults:
        delay = t0 + offset - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        action()


def _join_workers(workers, deadline_mono, shard_dir):
    grace = 30  # workers get the run duration plus time for in-flight ops
    for p in workers:
        p.join(timeout=max(0, deadline_mono - time.monotonic()) + grace)
        if p.is_alive():
            raise RuntimeError(
                f"worker {p.name} still running {grace}s past the deadline — "
                "an adapter is probably making calls without a timeout"
            )
    crashed = sorted(shard_dir.glob("error-*.txt"))
    if crashed:
        details = "\n\n".join(p.read_text() for p in crashed)
        raise RuntimeError(f"{len(crashed)} worker(s) crashed:\n{details}")
    for p in workers:
        if p.exitcode != 0:
            raise RuntimeError(f"worker {p.name} exited with {p.exitcode}")


def _merge_shards(shard_dir, clients) -> list[dict]:
    records = []
    for client_id in range(1, clients + 1):
        path = shard_dir / f"client-{client_id}.jsonl"
        with open(path) as f:
            records.extend(json.loads(line) for line in f)
    records.sort(key=lambda r: r["invoke_ts"])
    return records


def _build_report(shard_dir, clients, duration) -> dict:
    outcomes, statuses, errors = Counter(), Counter(), Counter()
    ok_latencies_ms = []
    for client_id in range(1, clients + 1):
        summary = json.loads((shard_dir / f"client-{client_id}-summary.json").read_text())
        outcomes.update(summary["outcomes"])
        statuses.update(summary["statuses"])
        errors.update(summary["errors"])
        ok_latencies_ms.extend(summary["ok_latencies_ms"])
    ok_latencies_ms.sort()
    total = sum(outcomes.values())
    return {
        "duration": duration,
        "total_ops": total,
        "throughput_ops_per_s": total / duration,
        "ok_per_s": outcomes.get("ok", 0) / duration,
        "outcomes": dict(outcomes),
        "statuses": dict(statuses),
        "errors": dict(errors),
        "ok_latency_ms": _percentiles(ok_latencies_ms),
    }


def _percentiles(sorted_ms: list[float]) -> dict:
    if not sorted_ms:
        return {}
    def at(p):
        return round(sorted_ms[min(len(sorted_ms) - 1, int(p * len(sorted_ms)))], 2)
    return {"p50": at(0.50), "p90": at(0.90), "p99": at(0.99),
            "max": round(sorted_ms[-1], 2), "count": len(sorted_ms)}


def _write_faults(run_path, fault_log: FaultLog, start_index: int, t0: float):
    lines = []
    for event in fault_log.events[start_index:]:
        line = dict(event)
        line["ts"] = round(line.pop("ts_mono") - t0, 6)
        lines.append(line)
    write_jsonl(run_path / "faults.jsonl", lines)


def _capture_states(run_path, cluster, state_reader):
    states_dir = run_path / "states"
    states_dir.mkdir(exist_ok=True)
    for node in cluster.nodes():
        try:
            state = state_reader(node)
        except TRANSPORT_ERRORS as e:
            print(f"hokea: could not read final state of node {node.index}: "
                  f"{type(e).__name__}: {e}", flush=True)
            state = {"unreachable": True, "error": f"{type(e).__name__}: {e}"}
        (states_dir / f"node-{node.index}.json").write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Worker process
# ---------------------------------------------------------------------------

def _worker_main(client_id, ops, adapter, nodes, assign, timeout, seed,
                 sample_p, rate, clients, t0, duration, shard_dir):
    try:
        _worker_loop(client_id, ops, adapter, nodes, assign, timeout, seed,
                     sample_p, rate, clients, t0, duration, shard_dir)
    except Exception:
        (shard_dir / f"error-{client_id}.txt").write_text(
            f"client {client_id}:\n{traceback.format_exc()}"
        )
        os._exit(1)


def _worker_loop(client_id, ops, adapter, nodes, assign, timeout, seed,
                 sample_p, rate, clients, t0, duration, shard_dir):
    rng = random.Random(f"{seed}/{client_id}/ops")
    sample_rng = random.Random(f"{seed}/{client_id}/sample")
    generator = ops(client_id, rng)
    deadline = t0 + duration
    rate_per_client = rate / clients if rate else None

    outcomes, statuses, errors = Counter(), Counter(), Counter()
    ok_latencies_ms = []
    records = []
    result = None
    k = 0

    while time.monotonic() < deadline:
        if rate_per_client:
            # Fixed schedule (catch up after delays, don't drift).
            slot = t0 + k / rate_per_client
            now = time.monotonic()
            if slot >= deadline:
                break
            if now < slot:
                time.sleep(slot - now)
        try:
            op = generator.send(result)
        except StopIteration:
            break
        if not isinstance(op, Op):
            raise TypeError(f"ops generator yielded {op!r}, expected an Op")

        node = _pick_node(nodes, assign, client_id, k, rng)
        invoke_ts = time.monotonic()
        result = _execute_one(adapter, op, node, timeout)
        return_ts = time.monotonic()

        outcomes[result.outcome] += 1
        if result.status is not None:
            statuses[str(result.status)] += 1
        if result.error is not None:
            errors[result.error.split(":")[0]] += 1
        if result.outcome == "ok":
            ok_latencies_ms.append((return_ts - invoke_ts) * 1000)
        if sample_p >= 1.0 or sample_rng.random() < sample_p:
            records.append({
                "client_id": client_id, "seq": k, "node": node.index,
                "op": op.op, "key": op.key, "args": op.args,
                "invoke_ts": round(invoke_ts - t0, 6),
                "return_ts": round(return_ts - t0, 6),
                "outcome": result.outcome, "status": result.status,
                "response": result.response, "version": result.version,
                "error": result.error,
            })
        k += 1

    with open(shard_dir / f"client-{client_id}.jsonl", "w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    (shard_dir / f"client-{client_id}-summary.json").write_text(json.dumps({
        "outcomes": dict(outcomes), "statuses": dict(statuses),
        "errors": dict(errors), "ok_latencies_ms": ok_latencies_ms,
    }))


def _pick_node(nodes, assign, client_id, k, rng):
    if assign == "pinned":
        return nodes[(client_id - 1) % len(nodes)]
    if assign == "random":
        return rng.choice(nodes)
    return nodes[k % len(nodes)]  # round_robin
