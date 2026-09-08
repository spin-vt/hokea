# Fixtures & CLI

## The pytest fixtures

::: hokea.pytest_plugin
    options:
      members: false
      show_root_full_path: true
      heading_level: 3

The plugin is installed with hokea (a `pytest11` entry point) — there is
nothing to enable. You define `hokea_cluster` in your `conftest.py`; the
fixtures below are provided:

`cluster`
:   Your `hokea_cluster`, entered and `wait_healthy()`-ed before the test
    body runs, torn down after it — every test starts from a fresh, healthy
    fleet. Works with either backend
    ([`Cluster`](cluster.md#hokea.cluster.Cluster) or
    [`KubeCluster`](cluster.md#hokea.kube.KubeCluster)).

`net`
:   The [network fault injector](faults.md) matching the backend:
    [`Net`](faults.md#hokea.net.Net) (iptables/netem) on Docker,
    [`KubeNet`](faults.md#hokea.kube.KubeNet) (Chaos Mesh) on Kubernetes.
    Same API either way: `partition`, `isolate`, `slow`, `heal`.

`chaos`
:   A [`KubeChaos`](faults.md#hokea.kube.KubeChaos): apply your own Chaos
    Mesh YAML inside the fault schedule. Kubernetes backend only — on the
    Docker backend using it raises, naming the local alternatives.

`run`
:   [`run_workload`](workload.md#hokea.workload.run_workload) with the
    cluster already bound and the run directory created automatically under
    `runs/`, named after the test:
    `h = run(ops, adapter, duration=30, faults=[...])` returns a
    [`RunHandle`](workload.md#hokea.runs.RunHandle).

`record`
:   [`record_run`](workload.md#hokea.workload.record_run) bound the same
    way — a context manager for runs with no client workload (batch jobs,
    training): faults and events are recorded, no history.

The plugin also adds one pytest option:

```
pytest --repeat N     # run each selected test N times (expose flaky tests)
```

## The `hokea` command line

`hokea` has three jobs: running your pytest suite *on* the class cluster
(`hokea test`), converting a run's history for an external checker
(`hokea export`), and deploying your system on the class Kubernetes
cluster and **leaving it running** for you
to look at by hand with `kubectl` (`hokea k8s`). Local recorded runs stay plain
`pytest`: the fixtures above bring a fresh Docker cluster up and tear it
down per test.

### `hokea test`

Run this project's pytest suite on the class cluster. The directory's
top-level files ship to the cluster, a runner in your team's client
namespace runs pytest there (so the servers, faults, and load all stay
inside the cluster), the live pytest output streams to your terminal,
the recorded `runs/` are copied back into `./runs/`, and the exit code
is pytest's own. The [introductory lab](../lab.md)'s Act 3 walks through it.

```
hokea test --namespace NS [pytest args...]
```

| Flag | Meaning |
| --- | --- |
| `--namespace NS` | your team's SERVER namespace, where the system under test runs (default: `$HOKEA_NAMESPACE`) |
| `--client-namespace NS` | where the runner itself runs (default: `<namespace>-clients`) |
| `--runner-cpus N` | CPUs for the runner — the load generator, not your servers (default: 2, max 4) |
| `--keep` | leave the runner Job and ConfigMap on the cluster afterwards, for debugging |
| anything else | passed to pytest verbatim (`-q`, `-k partition`, `-x`, `-s`, file names; put `--` first if an argument collides with hokea's own flags) |

`HOKEA_NODES` and `HOKEA_TAKEOVER` set in your shell are forwarded into
the cluster-side run. Ctrl-C detaches (the run continues; hokea prints
how to re-attach and clean up) — but a detached run's `runs/` are not
copied back automatically.

### `hokea export`

Convert a run's history for an external checker, printed to stdout.

```
hokea export --format {porcupine,jepsen} RUN_DIR
```

| Argument | Meaning |
| --- | --- |
| `--format {porcupine,jepsen}` | output format (required) |
| `RUN_DIR` | a `runs/<timestamp>-<name>` directory |

hokea ships no linearizability checker; if you claim linearizability,
export to Porcupine (see [the checks guide](../checks.md)).

### `hokea k8s up`

Deploy N nodes and leave them running. The source directory ships to the
pods as a ConfigMap — no image build, no registry. State is remembered in
`.hokea/k8s-<name>.json` **relative to the current directory**, so run
`up`/`status`/`down` from the same place.

```
hokea k8s up --namespace NS --src DIR --cmd "python server.py"
```

| Flag | Meaning |
| --- | --- |
| `--namespace NS` | an existing namespace you may create pods in (required) |
| `--src DIR` | directory with your server code (ships to the pods as a ConfigMap — no image build, no registry) (required) |
| `--cmd CMD` | start command, e.g. `"python server.py"` (required) |
| `--image IMAGE` | base image to run `--cmd` on (default: `python:3.12-slim`) |
| `--nodes N` | how many nodes (default: 3) |
| `--port PORT` | the port your server listens on (default: 8000) |
| `--name NAME` | cluster name, if you want more than one (default: `default`) |
| `--env KEY=VAL` | extra environment for every node (repeatable) |
| `--data NAME=REF` | dataset image mounted read-only in every node at `/dataset/NAME` (repeatable; one bare `REF` mounts at `/dataset`) |
| `--takeover` | replace a still-running cluster with this name instead of refusing |

### `hokea k8s status`

Show a deployed cluster's pods and how to reach them (one
`kubectl port-forward` line per node).

```
hokea k8s status [--name NAME]
```

| Flag | Meaning |
| --- | --- |
| `--name NAME` | cluster name (default: `default`) |

### `hokea k8s down`

Delete a deployed cluster — everything it created (pods, services,
configmaps, chaos rules), found by label, never by sweeping the namespace.

```
hokea k8s down [--name NAME] [--namespace NS]
```

| Flag | Meaning |
| --- | --- |
| `--name NAME` | cluster name (default: `default`) |
| `--namespace NS` | only needed if the state file from `up` is gone |
