"""Your first fault tests, against the toy replicated KV server.

Three tests, three lessons:
1. a property that HOLDS: losing one node doesn't take down the others,
2. a property that the server EMITS EVIDENCE for: every write it received
   was applied (event_join over its stdout events),
3. a property that FAILS, caught with evidence: the toy's replication is
   not durable — a killed-and-restarted node comes back empty and acked
   writes are simply gone from it.
"""

import requests

from hokea import Op, Result, check, http_result


class ToyAdapter:
    """~15 lines: speak the server's protocol, classify every reply.

    The outcome records what we KNOW about whether the operation took
    effect: `ok` (the server said done), `rejected` (the server said no and
    nothing changed), `unknown` (can't tell — when in doubt, this one).
    Two habits keep that honest (see docs/adapters.md):

    - http_result only files a status code under "rejected" if we listed it
      deliberately; any reply we didn't think about lands on unknown.
    - timeouts and dropped connections are never caught here. They escape,
      and hokea records unknown itself.
    """

    def execute(self, op: Op, node, timeout: float) -> Result:
        if op.op == "put":
            r = requests.put(f"{node.url}/kv/{op.key}",
                             data=op.args["value"], timeout=timeout)
            return http_result(r)
        r = requests.get(f"{node.url}/kv/{op.key}", timeout=timeout)
        if r.status_code == 404:
            # A read observing "no such key" DID happen: ok, not rejected.
            return Result("ok", status=404, response=r.json())
        return http_result(r)


def toy_ops(client_id, rng):
    """Each client writes its own keys and reads them back."""
    k = 0
    while True:
        key = f"c{client_id}-{k}"
        yield Op("put", key, {"value": str(k)})
        yield Op("get", key)
        k += 1


def read_state(node):
    """Used for state snapshots: this node's whole store."""
    return requests.get(f"{node.url}/state", timeout=5).json()


def test_other_nodes_stay_available_when_one_dies(cluster, run):
    h = run(toy_ops, ToyAdapter(), duration=15, clients=3, assign="pinned",
            faults=[(4, lambda: cluster.kill(2)),
                    (11, lambda: cluster.start(2))])
    a = check.availability(h.history, h.faults)
    assert a.window("baseline").side("all").success_rate > 0.95
    killed = a.window("kill")
    assert killed.side("others").success_rate > 0.95      # unaffected
    assert killed.side("faulted-node").success_rate < 0.05  # honest outage


def test_every_received_write_was_applied(cluster, run):
    # settle: a request can still be in flight when the run's deadline
    # hits (the nodes are deliberately small and slow) — give it a couple
    # of quiet seconds to finish before the event stream is audited.
    h = run(toy_ops, ToyAdapter(), duration=8, clients=3, settle=2)
    v = check.event_join(h.events, require="applied", for_each="recv",
                         on="op_id")
    assert v.status == "clean", v


def test_replication_is_not_durable_and_hokea_catches_it(cluster, run):
    """This test PASSES by catching a deliberate gap in the toy server:
    replication is best-effort and nothing re-syncs a restarted node, so
    writes acked while node 2 was dead never reach it, and its restart wipes
    what it had. The violation comes with the exact lost operations."""
    h = run(toy_ops, ToyAdapter(), duration=15, clients=3, assign="pinned",
            faults=[(4, lambda: cluster.kill(2)),
                    (11, lambda: cluster.start(2))],
            state_reader=read_state)
    v = check.acked_visible(h.history, h.states, write_ops=["put"])
    assert v.status == "violated", v
    lost_on_node2 = [e for e in v.evidence if 2 in e["missing_on_nodes"]]
    assert lost_on_node2, v.evidence
