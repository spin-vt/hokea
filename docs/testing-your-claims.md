# Testing your claims — from proposal to falsification experiments

Your project proposal makes four kinds of claims about the system you're
going to build — safety, liveness, fault model, performance — and for each
claim it describes an experiment that would *falsify* it: show it wrong if
it is wrong. This page maps each claim type to the hokea machinery that
runs that experiment. By the end, "can be falsified by partitioning two
replicas and..." is a test file in your repo that either passes or hands
you the evidence, rather than just a sentence in a document.

The worked examples below use the example scenario from the project spec —
a replicated shopping cart where every reachable replica accepts adds,
temporary divergence is fine, but a silently lost item is not. The hokea parts of
each example are real, runnable code; the parts marked *your adapter here*
are the ~30 lines you write for your own system —
[Adapters](adapters.md) is the page for those.

## Safety claims

A safety claim names something that must **never** happen. When it is
violated, it is violated at a specific moment by a specific event, and a
recorded run should let you find that event in a log. hokea's job is to
record enough for that to be possible; these are the tools you use to find
the event:

- **`check.acked_visible`** — every effect your system *acknowledged* is
  still present later. Use it for "never silently lost" claims.
- **`check.converged`** — all replicas report the same state. For "replicas
  agree (after syncing)" claims.
- **`check.event_join`** — every event of one kind has its matching event
  of another kind (every `ack` has a `commit`, every task has a
  completion), read from your servers' own logs.
- **[`Script` steps](adapters.md#choreographed-steps-the-script-client)** —
  for claims about a *specific point in time*
  ("during the partition, a read on the other side must not see this"),
  choreograph exact operations at exact moments in the fault schedule
  instead of hoping random load hits the window.

[Checks](checks.md) documents what each one can and — just as important —
cannot see.

**Worked example.** The cart proposal's safety claim: *concurrent adds from
different sessions all survive being merged*. Its falsification experiment,
as a real hokea test: partition the replicas into two groups, add a
*different* item to the same cart on each side, heal, give the system quiet
time to merge, then check that the replicas agree and that both items
survived:

```python
from hokea import Op, Script, check
from my_cart import CartAdapter, cart_ops, read_cart_state   # your adapter here — see docs/adapters.md


def test_concurrent_adds_survive_the_merge(cluster, net, run):
    script = Script(CartAdapter())               # your adapter here
    h = run(cart_ops, CartAdapter(), duration=30, script=script, settle=5,
            state_reader=read_cart_state,        # your adapter here
            faults=[(5,  lambda: net.partition([[1, 2], [3]])),
                    # offset 0: runs right after partition() returns, i.e. cut verified
                    (0,  lambda: script.do(Op("add", "cart-9", {"item": "boots"}), node=1)),
                    (0,  lambda: script.do(Op("add", "cart-9", {"item": "socks"}), node=3)),
                    (20, net.heal)])
    v = check.converged(h.states)
    assert v.status == "clean", v
    in_cart = lambda state, op: op["args"]["item"] in state.get(op["key"], [])
    v = check.acked_visible(h.history, h.states, write_ops=["add"], visible=in_cart)
    assert v.status == "clean", v
```

Reading it: the schedule runs in order — partition (verified real before
anything proceeds), one scripted add on each side of the cut, heal at
t=20; `settle=5` gives the merge five quiet seconds before states are
captured. If the merge drops an item, `acked_visible` comes back
`violated` naming the exact add and the node it's missing from — the
recorded evidence the falsification experiment promised. (`in_cart`
assumes the state reader
returns each cart as a list of items; adapt it to your state shape.) The
`state_reader=` is the third ~5-line piece of your adapter (see
[Adapters](adapters.md#the-state-reader-optional-recommended)): it fills
`h.states` with each node's final
state, and without it both state checks return `insufficient_evidence` —
even on a correct system, because there is nothing to judge.

!!! note "Where this file lives, and how to run it"
    Put this in a `test_claims.py` next to your `conftest.py` — the fixture
    from [Data & durability](data-and-durability.md) or the
    [introductory lab](lab.md), pointed at *your* server. Then run
    `uv run pytest -q test_claims.py` on your laptop, or
    `uv run hokea test --namespace team-NN -q test_claims.py` to ship the
    same file to the class cluster and run it there (the lab's
    Act 3 covers `hokea test`).

One design point from the [Checks](checks.md) page: `cart_ops` in
*this* test should only add items, never remove them. An acknowledged add
is then an effect nothing can legitimately erase, which is exactly what
makes the `visible=` comparison sound — with no removes in play, "is the
item in the cart?" is itself an existence test, so `acked_visible`'s
warning about value-level `visible` (which is about writes you must
*arrange* to be last) doesn't apply here. Mix removes into the same
workload and it does: the check can flag a correct system for an item a
client legitimately removed.

## Liveness claims

A liveness claim names something that must **eventually** happen — requests
get answered, replicas converge, jobs finish — including a statement of
when your system is *allowed* to stop (refusing writes during
a partition can be a legitimate design choice, but it must be a choice you
wrote down, not a behavior you discovered).

In hokea, liveness claims become **availability windows**:
`check.availability` slices the recorded history by the fault timeline, so
"any reachable replica accepts writes" turns into success-rate asserts on
each side of a partition window, and "replicas converge after healing"
turns into `settle=` plus `check.converged`. The cart proposal's liveness
falsifier, same shape:

```python
def test_reachable_replicas_accept_writes(cluster, net, run):
    h = run(cart_ops, CartAdapter(), duration=30, settle=5,   # your adapter here
            state_reader=read_cart_state,                     # your adapter here
            faults=[(8, lambda: net.partition([[1, 2], [3]])),
                    (22, net.heal)])
    part = check.availability(h.history, h.faults).window("partition")
    assert part.side("majority").success_rate > 0.95   # both sides keep
    assert part.side("minority").success_rate > 0.95   # accepting writes
    v = check.converged(h.states)
    assert v.status == "clean", v   # and re-merge after heal
```

If your design deliberately *refuses* minority-side writes (choosing
consistency over availability, as a consensus-based design like Raft or
Paxos must, since a side without a majority cannot accept writes; we will
cover why in lecture), your falsifier asserts the opposite there: clean
rejections, not successes. Your proposal should say so. Asserting high
success on a side your design promises to refuse would fail a correct
system; see the [Checks](checks.md) page's notes on availability.

## Fault-model claims

Your proposal must say which failures your claims hold under, and, just as
importantly, which are out of scope. Pick from the faults hokea can
actually inject, so that every in-scope fault has a falsifier that uses it:

- **`cluster.kill(i)` / `stop(i)` / `start(i)`** — crash-stop: the node
  halts with no warning and no flush. A restart brings back a *new* node
  with the old name — empty memory, empty `/data`. If your design assumes
  storage that survives a crash, read
  [Data & durability](data-and-durability.md) before writing this section.
- **`net.partition([[...], [...]])` / `net.isolate(i)`** — cut the cluster
  network between groups, or cut one node off from all its peers. Verified
  real before your test proceeds.
- **`net.slow(i, delay_ms=..., jitter_ms=..., loss_pct=...)`** — a slow or
  lossy network path: delay, jitter (random variation in that delay), and
  packet loss on one node's traffic.
- **`cluster.throttle(i, cpus=...)`** — a *compute* straggler: the node
  talks fine but computes slowly (where `net.slow` makes it slow to talk).
- **`cluster.pause(i)` / `unpause(i)`** — Docker backend only: freeze the
  process and resume it later with memory intact — a very long
  garbage-collection pause, not a crash.
- **`chaos.apply("your.yaml")`** — on the class cluster, anything Chaos
  Mesh can inject: CPU/memory stress, disk I/O delays, clock skew, HTTP
  tampering, multi-step Workflows. The lab's
  [Act 4](lab.md#act-4-break-it-in-prod) shows the pattern.

Whichever you use, the run's record (`faults.jsonl`) captures all of it
with timestamps — including chaos experiments injected outside your test —
so the claim "holds under X" is always checked against evidence that X
actually happened. One scope note to copy into your proposal if it applies:
a *malicious* replica, one that lies rather than fails (called a Byzantine
fault; we will cover this in lecture), is out of scope for the course tooling. hokea's
built-in faults inject crashes, cuts, and slowness, never corruption;
Chaos Mesh's HTTP tampering (reachable through `chaos.apply`) *can*
corrupt traffic in flight, but even that is a hostile network, not a
replica that lies about its own state.

## Performance claims

The proposal doesn't ask for numbers yet — it asks you to *name the
metrics*, *define the benchmarks*, and *say how you'll generate the load*.
hokea maps onto those directly:

- **Metrics**: every run writes `report.json` — throughput, latency
  percentiles (p50/p99), outcome and error mixes
  ([Artifacts](artifacts.md) has the format; the lab's
  [measurement interlude](lab.md#interlude-measure-it) defines the terms). Naming your
  metrics mostly
  means picking which of these you'll report, plus any system-specific
  ones your nodes emit as events.
- **Benchmarks**: the lab's
  [measurement interlude](lab.md#interlude-measure-it) is the template — separate
  read-heavy and write-heavy workloads as their own test files, and
  `HOKEA_NODES=6` to re-run the same benchmark at a different fleet size
  and see which numbers scale (a conftest convention, not built into
  hokea — your conftest must read the variable; the sample in
  [Data & durability](data-and-durability.md) does).
- **Load**: the same `run(...)` that drives fault tests is the load
  generator — `clients=200, rate=2000` runs two hundred concurrent
  simulated clients at a target operation rate.
- **Under-fault vs. baseline**: performance claims like "convergence time
  after a partition heals" or "write latency during a partition versus
  normal operation" fall out of availability windows — compare
  `window("partition")` against `window("baseline")` in one recorded run,
  as the lab's CPU-stress test does with p99 latency.

## Your claims will get sharper — keep the falsifiers

Your proposal will change over the semester, and these tests should
change with it: each falsifier is the precise version of one claim. Keep
them in your repo next to your code, and run them, in CI (automated tests
on every push) if your team has it set up, or at least by hand before
every milestone. When a claim changes ("we now refuse minority writes"),
the falsifier changes in the same commit, and the history of your test
files over the semester is the record of your claims getting sharper,
which is what I will be evaluating.
