"""Measure it: two small performance experiments, written as tests.

No faults here — just load. Each "test" drives a fixed number of simulated
clients for a fixed time and PRINTS a summary of what the run recorded:
throughput, and the p50/p99 latency. The asserts at the bottom are
deliberately loose (the run happened, operations succeeded) — the numbers
are the point, and you read them, the asserts don't.

Run them with printed output visible (`-s` tells pytest not to swallow it):

    pytest -q test_measure.py -s
    HOKEA_NODES=6 pytest -q test_measure.py -s    # same thing, six nodes

Compare the two: read throughput climbs with more nodes; the write-heavy
mix does not. The introductory lab's Interlude (docs/lab.md) explains why.
"""

import time

import requests

from test_toy import ToyAdapter, toy_ops

from hokea import Op

KEYS = [f"shared-{i}" for i in range(20)]   # the read test's fixed keyset
DURATION = 20                               # seconds of load per experiment
CLIENTS = 30                                # concurrent simulated clients


def read_ops(client_id, rng):
    """Read-only clients: GET the shared keys over and over, round-robin."""
    k = 0
    while True:
        yield Op("get", KEYS[k % len(KEYS)])
        k += 1


def seed_keys(cluster):
    """Write the keyset once, before the clients start.

    All writes go to node 1; replication is asynchronous, so then poll
    every key on every node until it's visible — the load must start on a
    cluster that already has the data everywhere."""
    nodes = list(cluster.nodes())
    for key in KEYS:
        r = requests.put(f"{nodes[0].url}/kv/{key}", data="x", timeout=5)
        r.raise_for_status()
    deadline = time.time() + 15
    for node in nodes:
        for key in KEYS:
            while requests.get(f"{node.url}/kv/{key}",
                               timeout=5).status_code != 200:
                if time.time() > deadline:
                    raise RuntimeError(
                        f"key {key} never replicated to {node.name}")
                time.sleep(0.2)


def show(title, cluster, report):
    lat = report["ok_latency_ms"]   # empty if no op succeeded at all
    print()
    print(f"---- {title}: {cluster.n_nodes} nodes, "
          f"{CLIENTS} clients, {DURATION}s ----")
    print(f"throughput  {report['throughput_ops_per_s']:7.0f} ops/s")
    print(f"latency     p50={lat.get('p50', '?')}ms  "
          f"p99={lat.get('p99', '?')}ms")
    print(f"outcomes    {report['outcomes']}")


def test_read_throughput(cluster, run):
    seed_keys(cluster)
    h = run(read_ops, ToyAdapter(), duration=DURATION, clients=CLIENTS)
    show("reads only", cluster, h.report)
    assert h.report["total_ops"] > 0
    assert h.report["outcomes"].get("ok", 0) > 0


def test_mixed_throughput(cluster, run):
    h = run(toy_ops, ToyAdapter(), duration=DURATION, clients=CLIENTS)
    show("writes+reads", cluster, h.report)
    assert h.report["total_ops"] > 0
    assert h.report["outcomes"].get("ok", 0) > 0
