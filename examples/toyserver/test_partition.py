"""A network partition test for the toy server, on either backend.

Nothing here refers to Docker or Kubernetes: net.partition() is iptables
locally and a Chaos Mesh NetworkChaos CR on the class cluster; the checks
read the same recorded files either way.
"""

import time

from test_toy import ToyAdapter, read_state, toy_ops

from hokea import Op, Script, check


def test_partition_splits_replication_but_not_clients(cluster, net, run):
    """A 2|1 partition, with the claim pinned down from both directions:

    - liveness: every node keeps serving its own clients through the cut
      (the harness path is never faulted; the toy acks writes locally) —
      note these rates alone would also hold with no partition, which is
      why the test does not rest on them;
    - the discriminator: a scripted write to node 1 *during the verified
      cut* is still absent from node 3 seconds later (404) — if the
      partition silently didn't happen, replication lands in milliseconds
      and this read sees the value;
    - the damage: best-effort replication never retries, so writes acked
      across the cut stay lost after heal — acked_visible returns violated
      with the exact lost ops. settle= gives in-flight replication threads
      time to drain before state capture, so the verdict is attributable
      to the partition alone, not to a capture race.
    """
    script = Script(ToyAdapter())
    steps: dict = {}
    # 60 s with the heal at 50: on the class cluster, proving a partition
    # is in place can take tens of seconds, and the heal must not fire
    # the instant the cut is verified. Offsets are earliest-starts.
    h = run(toy_ops, ToyAdapter(), duration=60, clients=3, assign="pinned",
            script=script, settle=5,
            faults=[(8, lambda: net.partition([[1, 2], [3]])),
                    # runs right after partition() returns, i.e. cut verified
                    (0, lambda: steps.update(
                        put=script.do(Op("put", "cut-probe", {"value": "x"}),
                                      node=1))),
                    # a guaranteed 3s after the put (offsets are earliest-
                    # starts, so the sleep, not the clock, enforces the gap):
                    # without a cut this key replicates in milliseconds
                    (0, lambda: (time.sleep(3), steps.update(
                        get=script.do(Op("get", "cut-probe"), node=3)))[0]),
                    (50, net.heal)],
            state_reader=read_state)

    assert steps["put"].outcome == "ok", steps["put"]
    assert steps["get"].status == 404, (
        f"the write crossed the cut: {steps['get']}")

    a = check.availability(h.history, h.faults)
    assert a.window("baseline").side("all").success_rate > 0.9
    part = a.window("partition")
    assert part.side("majority").success_rate > 0.9
    assert part.side("minority").success_rate > 0.9  # clients still reach it

    v = check.acked_visible(h.history, h.states, write_ops=["put"])
    assert v.status == "violated", v
    assert any(3 in e["missing_on_nodes"] or 1 in e["missing_on_nodes"]
               for e in v.evidence), v.evidence
