"""hokea's pytest integration.

Your conftest.py defines ONE fixture describing how to build your cluster:

    import pytest
    from hokea import Cluster

    @pytest.fixture
    def hokea_cluster(tmp_path):
        return Cluster(image="python:3.12-slim", src="./server",
                       cmd="python server.py", nodes=3, name="myproj",
                       port=8000, workdir=tmp_path / "cluster")

and your tests then use:

    cluster — the cluster, up and healthy (torn down after the test)
    net     — a Net bound to it (network faults)
    chaos   — apply your own Chaos Mesh YAML in the fault schedule
              (Kubernetes backend only)
    run     — run_workload bound to the cluster, with each run's directory
              created automatically under runs/, named after the test

`pytest --repeat 3` runs every selected test 3 times, to expose flakiness.
"""

import pytest

from .workload import record_run, run_workload


def pytest_addoption(parser):
    try:
        parser.addoption("--repeat", type=int, default=1, metavar="N",
                         help="run each test N times (expose flaky tests)")
    except ValueError:
        pass  # another plugin (e.g. pytest-repeat) already provides it


def pytest_generate_tests(metafunc):
    n = metafunc.config.getoption("--repeat", default=1)
    if n > 1:
        metafunc.fixturenames.append("hokea_repeat")
        metafunc.parametrize("hokea_repeat", range(1, n + 1),
                             ids=[f"rep{i}" for i in range(1, n + 1)])


@pytest.fixture
def cluster(hokea_cluster):
    """The student-described cluster, running and healthy."""
    with hokea_cluster as c:
        c.wait_healthy()
        yield c


@pytest.fixture
def net(cluster):
    # Dispatch per backend: Cluster -> Net (iptables/netem), KubeCluster ->
    # KubeNet (Chaos Mesh). Same fault API either way.
    return cluster.make_net()


@pytest.fixture
def chaos(cluster):
    # KubeCluster -> KubeChaos: apply your own Chaos Mesh YAML (any
    # experiment kind, or a Workflow) inside the fault schedule. On the
    # Docker backend this raises with the local alternatives named —
    # Chaos Mesh only exists on Kubernetes.
    return cluster.make_chaos()


@pytest.fixture
def run(cluster, request):
    """run(ops, adapter, duration=..., ...) -> RunHandle, recording into an
    automatically created `runs/<timestamp>-<testname>/` directory."""
    def _run(ops, adapter, **kwargs):
        kwargs.setdefault("name", request.node.name)
        return run_workload(cluster, ops, adapter, **kwargs)
    return _run


@pytest.fixture
def record(cluster, request):
    """record(...) -> context manager for runs with no client workload
    (batch jobs, training): faults + events are recorded, no history."""
    def _record(**kwargs):
        kwargs.setdefault("name", request.node.name)
        return record_run(cluster, **kwargs)
    return _record
