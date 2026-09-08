# hokea

Fault injection and testing for distributed-systems capstone projects.
You bring a small distributed system; hokea runs it as a fleet of
containers (isolated lightweight boxes, like the ones Docker runs), then
kills, freezes, partitions, and slows it while concurrent clients drive
load, and records everything. Your tests then compare what you claim your
system does against that record, and every verdict carries the recorded
evidence it rests on.

**If you are a student in the course, start with [the introductory
lab](docs/lab.md).** Part 0 installs the tools; Act 1 takes you from zero
to a running fault test against a toy server in about 15 minutes; the
later acts break it, ship it to the class cluster, and break it there.
You don't need anything else on this page yet.

(The name comes from the kea, a parrot known for taking apart whatever it
finds to see what happens.)

```python
def test_stays_available_during_minority_partition(cluster, net, run):
    h = run(MyOps, MyAdapter(), duration=60,
            faults=[(15, lambda: net.partition([[1, 2], [3]])),
                    (45, net.heal)])
    a = check.availability(h.history, h.faults)
    assert a.window("partition").side("majority").success_rate >= 0.99
```

## What it gives you

- **Cluster** — no Dockerfile needed: base image + your source + a start
  command becomes N containers on two networks (a client network that is
  never faulted, and a cluster network where faults happen). Process faults:
  `kill`, `stop`/`start`, `pause`/`unpause`, and `throttle`/`unthrottle` — a
  CPU-quota *compute* straggler (a machine that computes slowly but talks
  fine, where `net.slow` makes the network slow).
- **Net** — *verified* network faults: partitions and latency/loss (via
  Linux's netem) on the cluster network only. After installing a cut, hokea probes that
  traffic is actually blocked (and still alive within groups and from the
  harness) and **raises an error if not**, because a partition that
  silently didn't happen would let a test draw wrong conclusions. Faults
  survive node restarts: the intended state is re-applied and re-verified
  automatically.
- **Workload** — concurrent client worker processes running your ~30-line
  adapter and operation generator, with a strict
  ok / rejected / unknown outcome contract (transport failures are
  classified by hokea itself, so an adapter mistake can only make checks
  more cautious). Same driver does load testing: `clients=200, rate=2000`
  gives throughput, latency percentiles, and error mix.
- **Artifacts** — every run writes `history.jsonl`, `faults.jsonl`,
  `events.jsonl`, `states/`, `metadata.json` ([spec](docs/artifacts.md)).
  The files are the interface: every verdict can be reproduced from them
  later, without the cluster.
- **Checks** — five shipped, each short enough to read in full, each
  three-way (violated / clean / **insufficient evidence** — missing data
  never looks like a pass): `availability`, `converged`, `acked_visible`,
  `event_join`, `session`. What they can and cannot see:
  [docs/checks.md](docs/checks.md).
- **Exporters** — `hokea export --format porcupine|jepsen` hands histories
  to external checkers; hokea does not ship a linearizability checker of
  its own (we will cover linearizability in lecture, and why checking it
  is hard to get right). `examples/porcupine/` is a worked Go model.

## Documentation

- [docs/lab.md](docs/lab.md) — the introductory lab, in four acts:
  run it locally (the first fifteen minutes), break it, ship it to the
  class cluster, break it in prod
- [docs/adapters.md](docs/adapters.md) — the adapter contract and the three
  outcome rules (read before writing one)
- [docs/artifacts.md](docs/artifacts.md) — the recorded file formats
- [docs/checks.md](docs/checks.md) — the shipped checks, and what each
  one cannot see
- [docs/data-and-durability.md](docs/data-and-durability.md) — the fault
  model, `/data`, and datasets (`data=`)
- [docs/datasets.md](docs/datasets.md) — the course dataset images and how
  to publish your own
- [docs/testing-your-claims.md](docs/testing-your-claims.md) — turning your
  project proposal's claims into falsification experiments

The `docs/` pages above are also a browsable site (MkDocs + Material). To
serve it locally:

```bash
uv run --group docs mkdocs serve    # http://127.0.0.1:8000
```

## The contract your system meets

1. Read `NODE_ID` (1-based), `N_NODES`, and `PEERS`
   (`node-1:<port>,node-2:<port>,...`, including yourself; those names
   resolve on the cluster network only).
2. Answer `GET /health` with HTTP 200 once ready to serve.
3. For in-container network faults (the default on machines without host
   root): the image must include `iptables` (+`iproute2` for netem). Images
   hokea generates in no-Dockerfile mode already do.
4. If your system needs a dataset (a log corpus, MNIST), pass `data=` to
   `Cluster` and the directory appears read-only at `/dataset` in every node; a dict
   `{name: dir}` mounts several, each at `/dataset/<name>`. (On the k8s
   backend the same `data=` takes OCI image references instead of
   directories — same paths.) Separately,
   every node has a private writable scratch space at `/data` (a tmpfs
   locally), wiped when
   the container restarts.

## Node roles

hokea runs N identical containers; it has no notion of roles. When your
design needs distinguished nodes — a MapReduce leader and its workers, a
scheduler and its executors — derive the role from `NODE_ID` inside your
system:

```python
if int(os.environ["NODE_ID"]) == 1:
    run_leader()        # node 1: coordinator / leader
else:
    run_worker()        # everyone else: worker / replica
```

Two things follow: `cluster.node(1)` is always your leader in tests (kill
it to test failover), and the same image serves every role, so there is
nothing extra to build or configure.

## Runs without a client workload

Batch and training projects (a MapReduce job, distributed SGD) have no
clients sending requests; the record that matters is the fault timeline
and the node event stream. `record_run` produces the same run directory as
`run_workload`, minus `history.jsonl`:

```python
from hokea import record_run

with record_run(cluster, name="train-sync") as r:
    net.slow(3, delay_ms=200)      # the straggler
    time.sleep(600)                # nodes work, emitting eval events
    net.heal()

check.event_join(r.events, require="commit", for_each="ack", on="op_id")
```

In pytest, the `record` fixture wraps it the way `run` wraps `run_workload`.

## Developing hokea itself

The framework's own test suite, design notes, and course-staff tooling are
maintained in a separate private repository. You will not need them for the
course.
