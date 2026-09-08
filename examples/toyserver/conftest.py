"""How to run this project under hokea: one fixture describing the cluster.
hokea's pytest plugin turns this into the `cluster`, `net`, and `run`
fixtures your tests use.

One command moves the same tests from your laptop to the class cluster:

    pytest -q                             # Docker, on your own machine
    hokea test --namespace team-NN -q      # Kubernetes, on the class cluster

Nothing in the tests changes — `hokea test` ships this directory to the
cluster and runs pytest there, with HOKEA_NAMESPACE set; this fixture sees
it and builds a Kubernetes cluster instead of a Docker one. kill()
force-deletes a pod there instead of `docker kill`, net.partition()
becomes a Chaos Mesh rule instead of iptables, and the recorded artifacts
and checks are identical. No image is built and no registry is involved:
this directory ships to the pods as a ConfigMap on a python base image.

One env var scales the fleet without editing anything (it rides along
through `hokea test` too):

    HOKEA_NODES=6 pytest -q test_measure.py -s   # six nodes instead of three

The nodes are deliberately TINY — half a CPU and 256 MB of memory each —
so that a couple dozen simulated clients are enough to saturate one. That
is the whole trick of the measurement tests: production systems hit their
capacity limits at tens of thousands of requests per second, and we
under-provision on purpose so you can watch the very same behavior at toy
scale. WORK (below) is the matching cost knob on the server itself.
"""

import os
from pathlib import Path

import pytest

HERE = Path(__file__).parent

# Your cluster's name — it goes into the names of the pods hokea creates.
# On the class cluster your whole TEAM shares one namespace, so before you
# run there, make this name yours alone (convention: put your own name on
# the end, lowercase letters/digits/hyphens only). Two teammates running
# under the same name at the same time collide.
NAME = "toydemo"                # e.g. "toydemo-alice" — see the lab, Act 3

# Simulated per-request cost inside server.py (rounds of sha256 hashing —
# see its docstring). Combined with the half-CPU nodes, this puts one
# node's capacity around a hundred requests per second, so the measurement
# tests can saturate the whole fleet with ~30 clients.
ENV = {"WORK": "2000"}


@pytest.fixture
def hokea_cluster(tmp_path):
    nodes = int(os.environ.get("HOKEA_NODES", "3"))
    namespace = os.environ.get("HOKEA_NAMESPACE")
    if namespace:
        from hokea.kube import KubeCluster
        return KubeCluster(image="python:3.12-slim", src=HERE,
                           cmd="python server.py", nodes=nodes, name=NAME,
                           namespace=namespace, port=8000, env=ENV,
                           cpus=0.5, memory="256Mi",   # tiny on purpose
                           workdir=tmp_path / "cluster")
    from hokea import Cluster
    return Cluster(image="python:3.12-slim", src=HERE,
                   cmd="python server.py", nodes=nodes, name=NAME,
                   port=8000, env=ENV,
                   cpus=0.5, memory="256m",            # tiny on purpose
                   workdir=tmp_path / "cluster")
