# Data & durability

## *What survives a crash (nothing), and where data lives*

This page answers three questions every project hits sooner or later: what
actually happens to a node's data when hokea kills it, where your nodes can
write scratch files, and how to give every node a read-only dataset (a log
corpus, MNIST) to work on. Read the first section before you write any
durability claim for your project; everything else on this page follows
from it.

## The fault model: crash-stop, and what it does to your data

When hokea kills a node — `cluster.kill(2)`, or a fault schedule doing the
same — the node simply **halts**. It doesn't flush anything, doesn't say
goodbye to its peers, doesn't get a chance to run cleanup code. This style
of failure has a textbook name, **crash-stop**: a failed node does nothing
further, ever.

A test may restart the node later, and this is the part that matters for
how you design your system: **a restarted node is best understood as a
brand-new node that happens to reuse the old one's name.** It comes back
with empty memory and an empty `/data` directory. Nothing it knew before
the crash survives. On the Kubernetes backend (the class cluster) this is
literal — the old pod (the node's running container: the isolated box its
process lives in) is deleted and a fresh one is created; on the Docker
backend hokea gives you the same behavior, so that a test passing on your
laptop means the same thing on the cluster. (One Docker difference: files
written *outside* `/data`, into the container's own filesystem, can
survive a restart there. More on that later in this page.)

The consequence for your safety claims: **under this fault model,
durability can only come from replication** (or from recomputing the data
from inputs that still exist). A write is durable when enough *other* nodes
know about it, never because a disk has it. "We saved it to a file before
acknowledging" is not a durability mechanism here, because the file dies
with the node. When you write a claim like "a confirmed reservation
survives any single server dying", the mechanism backing it has to be "at
least one other node also has the reservation before we confirm".

**A caveat if you're building on a consensus protocol.** Textbook
crash-*recovery* designs assume something hokea deliberately doesn't give
you: storage that survives a crash. Raft (a consensus protocol we will
cover in lecture), for example, requires each node to persist its log and
its vote so that a restarted node rejoins remembering what it promised.
Under hokea's fault model you have two
legitimate options, and the course spec accepts either — but it must be a
choice you write down in your fault model, not a behavior you discover
during a demo:

1. **Treat a restarted node as a new member** that must be (re-)admitted to
   the group and caught up from its peers, exactly like adding a fresh
   machine.
2. **Scope restarts out of your fault model**: your claims hold under nodes
   crashing and *staying* dead (and you never call `cluster.start` on a
   killed node in your falsification tests).

One more fault to file correctly: `cluster.pause()` (Docker backend only)
freezes a node's process without killing it, and `unpause` resumes it with
all its memory intact. That is **not** crash-recovery — it models a very
slow node, or a long garbage-collection pause: the node vanishes for a
while and then continues as if nothing happened, still believing everything
it believed before.

## `/data`: the per-node scratch space

Every node has a private writable directory at `/data`. It belongs to that
one node alone, and it is **wiped whenever the node dies or restarts — on
both backends**. What backs it differs: on the cluster it is a plain
directory on the machine's own local disk (Kubernetes calls this an
`emptyDir`) — fast, and as big as the disk allows; on
the Docker backend it is kept in RAM (a tmpfs) and counts against the
node's memory limit — fine for caches and small spills, but don't test
bigger-than-memory spill files on your laptop and expect the space to be
there.

Use it for anything you can afford to lose: spill files when a computation
doesn't fit in memory, caches, a SQLite file you treat as a disposable
local index. Do **not** put anything there that a safety claim depends on.
As the section above explains, the moment the node crashes `/data` is
gone, and a check like [`acked_visible`](checks.md) will show you the
missing writes.

(The rest of the container filesystem is technically writable, but don't
use it: on the Docker backend files written there can even survive a
restart, while on the cluster a restarted pod keeps nothing — so a habit of
writing outside `/data` lets your laptop tests pass where the cluster
fails. `/data` behaves the same on both backends: gone on restart.)

## Datasets: read-only inputs with `data=`

A **dataset** — a log corpus, MNIST, anything your nodes read but never
write — is passed to your cluster as `data=`. One source appears
read-only at `/dataset` in every node (hokea *mounts* it — attaches it
into each node's filesystem); a dict `{name: source}` mounts each source
at `/dataset/<name>`.

What a *source* is depends on the backend:

- **Docker backend**: a host directory, mounted live — edit a file on your
  laptop and the nodes see the change immediately.
- **Kubernetes backend**: an **OCI image reference** — the name of a
  container image (a frozen filesystem snapshot, the kind Docker builds),
  like `ghcr.io/you/my-corpus:v1` — the same kind of name you'd
  `docker pull`. A
  dataset image is just `FROM scratch` + `COPY corpus/ /`: a filesystem
  shipped as an image, no code, nothing to run — built and tagged like any
  image. The cluster attaches it read-only, keeping **one on-disk
  copy per cluster machine** no matter how many teams use it.

The paths are identical on both backends, so your node code reads
`/dataset/...` the same everywhere. The difference is mutability: the
Docker mount is live, the image is immutable — to change a dataset on the
cluster, push a new tag (`v2`) and pass that.

Here is the pattern in a `conftest.py`, with the same `HOKEA_NAMESPACE`
switch as the [introductory lab](lab.md)'s toy project. You never set that
variable yourself: `hokea test` ships your files to the cluster and sets
it for the cluster-side run — it always names your *server* namespace
(`team-NN`: a namespace is your team's named compartment on the cluster,
and the server one is where your nodes run); plain `pytest` on your laptop
leaves it unset and gets the Docker branch:

```python
import os
from pathlib import Path

import pytest

HERE = Path(__file__).parent

@pytest.fixture
def hokea_cluster(tmp_path):
    # HOKEA_NODES=6 pytest ... reruns the same tests at a different fleet size
    nodes = int(os.environ.get("HOKEA_NODES", "3"))
    namespace = os.environ.get("HOKEA_NAMESPACE")
    if namespace:
        from hokea.kube import KubeCluster
        return KubeCluster(image="python:3.12-slim", src=HERE,
                           cmd="python server.py", nodes=nodes,
                           # your team shares its namespace: make the name
                           # yours alone, e.g. "myproj-alice" (see the
                           # introductory lab, Act 3)
                           name="myproj", namespace=namespace, port=8000,
                           workdir=tmp_path / "cluster",
                           # k8s: an image reference. This one is a
                           # placeholder — I will give you the real ref
                           # (see the note below).
                           data="ghcr.io/spin-vt/datasets/wikitext-2:v1")
    from hokea import Cluster
    return Cluster(image="python:3.12-slim", src=HERE,
                   cmd="python server.py", nodes=nodes, name="myproj",
                   port=8000, workdir=tmp_path / "cluster",
                   # Docker: a plain directory on your machine
                   data=HERE / "corpus")
```

Node code then just opens `/dataset/wiki.train.tokens` — same line on your
laptop and on the cluster. Need more than one dataset? The dict form mounts
each under its name:

```python
data={"corpus": "ghcr.io/spin-vt/datasets/wikitext-2:v1",
      "logs":   "ghcr.io/spin-vt/datasets/weblogs-synth:v1"}
# node code: /dataset/corpus/wiki.train.tokens
#            /dataset/logs/events-000.jsonl
```

(On the Docker backend the dict values are directories, one per name.)

**Sharding convention.** Every node sees the *same* `/dataset` — hokea does
not split it for you, because how to split is a design decision your
project owns. One convention you might use: node *i* picks its own slice using `NODE_ID`
(the 1-based id every node reads from its environment — see the contract in
the [introductory lab](lab.md#12-copy-the-toy-project-three-files)):

```python
shards = sorted(Path("/dataset").glob("*.jsonl"))  # same sorted list on every node
# every N-th shard, starting from this node's own offset
mine = shards[int(os.environ["NODE_ID"]) - 1::int(os.environ["N_NODES"])]
```

Every node runs the same code, each takes every N-th file starting at its
own offset, and together they cover the dataset exactly once.

Tying this back to the fault model above: `/dataset` is read-only and
comes back intact after a restart. It is the one kind of "storage that
survives a crash" you do have, precisely because nobody can write to it.
Recomputing lost state from `/dataset` (re-reading your shard after a
restart) is a legitimate durability mechanism; a MapReduce worker that
re-runs its task from the input files is the classic example.

## Course datasets

The course ships four ready-made dataset images — `fashion-mnist`,
`mnist`, `wikitext-2`, and `weblogs-synth` — and any team can publish its
own the same way. The [Datasets](datasets.md) page is the catalog:
contents, sizes, licenses, and the two-line Dockerfile recipe for
publishing yours. I will announce the current image references; get them
from me before wiring them into your conftest.
