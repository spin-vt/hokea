# API reference

Everything here is generated from the source docstrings, so it matches
what `help(...)` prints, organized by what you are trying to do.
To **run your system** as a fleet of nodes, you need
[`Cluster`](cluster.md) (Docker, on your laptop) or
[`KubeCluster`](cluster.md#hokea.kube) (the class Kubernetes cluster) — both
expose the same nodes, the same process faults, and the same PEERS contract.
To **break the network**, use the [`Net` object](faults.md) the `net` fixture
hands you (partitions, and delay/loss via netem — Linux's
network-emulation tool — all verified before your test continues), or
[`chaos.apply`](faults.md#hokea.kube.KubeChaos) for Chaos Mesh experiments of
your own design on Kubernetes. To **put load on the system and record what
happened**, [`run_workload`](workload.md) drives your ops generator through
your adapter and writes a run directory; [`RunHandle`](workload.md#hokea.runs.RunHandle)
reads it back. To **ask questions of the run**, the [check
functions](checks.md) turn a run's files into three-way verdicts —
violated, clean, or insufficient-evidence, never a silent pass. And the
[pytest fixtures and the `hokea` CLI](fixtures-and-cli.md) (command-line
tool) connect these together: the fixtures wire all of the above into a
test function's arguments, and the CLI covers running the suite on the
class cluster (`hokea test`), exports, and deploying your system and
leaving it running so you can look at it by hand.
