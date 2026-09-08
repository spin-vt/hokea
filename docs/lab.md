# Introductory lab — run it, break it, ship it, break it in prod

This is a lab to introduce hokea, the distributed systems orchestration framework we will use,
in four acts (plus a short measurement interlude). The times are
generous — budget up to about two hours in all:

1. **Run it locally** (~15 minutes) — a 3-node toy service in containers on
   your laptop, tests passing, evidence recorded.
    - *Interlude* — **measure it** (~10 minutes): put the service under load,
      read its throughput and latency, double the fleet to six nodes, and see
      which numbers improve — and which don't.
2. **Break it locally** (~15 minutes) — kill a node and cut the network
   while clients are hammering it, and read what hokea caught.
3. **Ship it to the class cluster** (~40 minutes) — one command,
   `hokea test`, runs the same tests on real machines, in your team's own
   space on the cluster; then measure it at 3 and 6 nodes and turn the
   numbers into one graph.
4. **Break it in prod** (~15 minutes) — the same faults, now injected by
   Chaos Mesh on the class cluster.

Before any of that comes **Part 0 — Setup** (~20 minutes), the first
thing you'll do in the lab: install the tools on your own laptop and
download your cluster credentials. The Docker Desktop download is large
and can be slow on lab WiFi, so start that download first, then read on
while it comes in.

The system you'll run and break in every act is a **key-value store**: a
service other programs talk to over the network to store and fetch values
by name — put a value under a key, get it back later, like a Python
dictionary served to many clients at once. Many real systems you've heard
of are distributed key-value stores — Redis, etcd, MongoDB, DynamoDB —
exposing similar interfaces while making different tradeoffs in
consistency and in how they handle machine and network failures. The toy
server in this lab is a bare-bones one, small enough to read top
to bottom.

Each part ends at a **checkpoint** — a concrete thing you can show. Nothing
is handed in; this is an in-class exercise.
Acts 1 and 2 need only your laptop; Acts 3 and 4 need the cluster access
you set up in Part 0.

---

## Part 0 — Setup

On your own laptop: install five tools (you may already have some),
check each one works, and download your cluster credentials. Budget
about 20 minutes — more if you're installing Docker for the first time.
If you haven't already, start the Docker Desktop download (0.1 below)
right now: it is the biggest download and can be slow on lab WiFi, and
the rest of Part 0 can happen while it comes in. Part 0 ends with a
checklist; every line of it must pass before you move on to Act 1.

Every command in this document assumes a Unix shell: macOS, Linux, or —
on Windows — WSL (the Windows Subsystem for Linux, which gives you a real
Linux environment inside Windows). A plain Windows Command Prompt or
PowerShell will not work for this course.

### 0.1 Docker

Docker runs programs inside *containers* — a container is a lightweight
box with its own filesystem and network, like a very cheap virtual
machine. The lab runs a three-node distributed system in containers on
your laptop.

Install Docker Desktop for your operating system (official pages):

- **macOS**: <https://docs.docker.com/desktop/setup/install/mac-install/>
  (pick Apple silicon or Intel to match your Mac)
- **Windows**: <https://docs.docker.com/desktop/setup/install/windows-install/>
  — during setup, keep the **WSL 2** option enabled (it's the default);
  that's what lets your WSL shell talk to Docker. If you don't have WSL
  yet, install it first: open PowerShell and run `wsl --install`, then
  reboot.
- **Linux**: <https://docs.docker.com/engine/install/> for your
  distribution (Docker Engine is enough; Docker Desktop is optional).

**Verify** — with Docker Desktop running (start it once; it can start on
login afterwards), in your terminal:

```bash
docker ps
```

```
CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS     NAMES
```

A table — even an empty one — starting with `CONTAINER ID` means you're
done.

!!! warning "Troubleshooting"

    - **`command not found`** — Docker isn't installed (or, on Windows,
      you're not in a WSL terminal).
    - **`Cannot connect to the Docker daemon`** — Docker is installed but
      not running: start Docker Desktop (or the Docker service) and retry.
    - **`permission denied` about `docker.sock` (Linux)** — add yourself
      to the docker group: `sudo usermod -aG docker $USER`, then log out
      and back in, and retry.

### 0.2 Python

You need **Python 3.11 or newer**. **Verify:**

```bash
python3 --version
```

```
Python 3.12.3
```

(Anything from 3.11 up is fine.)

### 0.3 uv

uv is a fast, modern Python package manager. If you know pip: uv replaces
that workflow — it creates isolated per-project environments and pins
exact dependency versions, so a project behaves the same on every
teammate's machine. You're strongly encouraged to use it in this course
if you work in Python. Install it, then restart your terminal:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Verify:**

```bash
uv --version
```

```
uv 0.11.9 (x86_64-unknown-linux-gnu)
```

(Any version number is fine.) If it prints `command not found`, you
skipped the "restart your terminal" step.

### 0.4 git

You probably have it already. **Verify:**

```bash
git --version
```

```
git version 2.43.0
```

(Any version is fine.) If not: macOS offers to install it the first time
you run the command; on Linux, `sudo apt install git` (or your
distribution's equivalent); on Windows, install it *inside WSL* the Linux
way.

### 0.5 kubectl

kubectl is the Kubernetes command-line tool — Kubernetes is the system
the class cluster uses to run programs across its machines, and the
second half of the lab talks to it.
Install kubectl on your laptop by following
<https://kubernetes.io/docs/tasks/tools/> for your operating system (on
Windows, install the *Linux* version inside WSL). That page's main
install command uses `sudo`; if you can't use `sudo` on your laptop,
follow the page's note on installing without root into a folder on your
`PATH` (such as `~/.local/bin`) instead.

**Verify:**

```bash
kubectl version --client
```

```
Client Version: v1.35.8
Kustomize Version: v5.7.1
```

(Any versions are fine. kubectl doesn't need the cluster to exist yet —
`--client` only checks the tool itself.)

### 0.6 Cluster access

Two things connect you to the class cluster: your **team namespace**
name and a **kubeconfig** file you download yourself. The cluster is
reachable from any ordinary internet connection — campus WiFi, home, a
phone hotspot.

- Your **team namespace** name — a namespace is your team's own named
  compartment on the cluster, and you'll type its name in several
  commands below. We write `team-NN` for it throughout this document;
  your team's real name is zero-padded, e.g. `team-03`, and `team-3` will
  fail with `Forbidden` or `NotFound`. Your instructor announces team
  assignments; if you don't know your team number, ask.
- Your **kubeconfig** (the credentials file the cluster tools read).
  Download it yourself — about two minutes:

    1. In a browser, go to <https://launch.cs.vt.edu> and sign in with
       your **CS department account** (the same login you use for CS
       department machines — not your main VT PID).
    2. Find the cluster named **hokea** — click its name to open it, then
       click the **Download KubeConfig** button (a download icon near the
       top right; hovering over the icons shows their names). A file named
       `hokea.yaml` lands in your downloads.
    3. Put it where kubectl looks. kubectl only reads `~/.kube/config`,
       and the file must be named exactly `config` — so rename it as you
       copy:

        ```bash
        mkdir -p ~/.kube && cp ~/Downloads/hokea.yaml ~/.kube/config
        ```

        (On Windows, your browser saves into the *Windows* Downloads
        folder, which your WSL shell sees at a different path: run
        `ls /mnt/c/Users` to see the folder names, pick yours, and use
        `cp "/mnt/c/Users/YOUR-WINDOWS-NAME/Downloads/hokea.yaml" ~/.kube/config`
        — keep the quotes; they matter if the folder name has a space.
        And on any machine: if a `~/.kube/config` already exists from
        another course or job, this overwrites it — save the old one first
        with `mv ~/.kube/config ~/.kube/config.backup`.)

If you have both, **verify** (with your own team's namespace) — the
command asks the cluster to list your team's *pods* (running programs
on the cluster; you haven't started any yet, which is the point):

```bash
kubectl -n team-NN get pods
```

```
No resources found in team-NN namespace.
```

That empty answer is success.

!!! warning "Troubleshooting"

    - **Signing in to the website fails** — that's your CS department
      account, not this course. If you don't have one or forgot the
      password, tell your instructor, who will point you to CS tech staff.
    - **Signed in fine, but no cluster named `hokea` appears** — you
      haven't been added to your team on the cluster yet. Tell your
      instructor; once you're added, everything above works.
    - **`command not found`** — kubectl isn't installed (0.5 above).
    - **The verify command answers `Forbidden` or `NotFound`** — you typed
      `team-NN` literally, or a namespace that isn't yours (remember the
      zero-padding: `team-03`, not `team-3`). If the name is right, you
      may not be added to your team yet — tell your instructor.
    - **`The connection to the server localhost:8080 was refused`** —
      kubectl can't find your credentials file: step 3 above didn't
      happen, or the copy grabbed the wrong file. Redo step 3, then retry.
    - **`Unable to connect` or a long hang** — check your internet
      connection. If it persists on a connection that otherwise works,
      re-download the kubeconfig (steps 1–3) — the file may be stale or
      damaged.

### 0.7 The checklist — before moving on to Act 1

Before moving on to Act 1, confirm all of these. Run each; if any
fails, its fix is in the section above.

```bash
docker ps
python3 --version
uv --version
git --version
kubectl version --client
kubectl -n team-NN get pods
```

- [ ] `docker ps` prints a table (even an empty one)
- [ ] `python3 --version` prints 3.11 or newer
- [ ] `uv --version` prints a version
- [ ] `git --version` prints a version
- [ ] `kubectl version --client` prints a version
- [ ] team namespace known; kubeconfig at `~/.kube/config`;
  `kubectl -n team-NN get pods` answers `No resources found`

**Checkpoint — all six lines pass** (or: five pass, a TA is flagged
about the cluster one, and you start Acts 1–2 while it's sorted out —
they don't need the cluster).

---

## Act 1 — Run it locally

When this act is done you will have a 3-node replicated key-value store
running in containers on your laptop, three tests passing against it, and a
directory of recorded evidence you can open and read. The store itself is
the toy key-value server from the intro — a dictionary served over the
network, replicated across three nodes.

### 1.1 A clean project directory

Pick a folder you'll keep your course work in (say `cd ~/cs4284`), clone
the course repository there, then make a fresh directory *next to it* and
set it up as a uv project:

```bash
git clone https://github.com/spin-vt/hokea
mkdir toy-demo && cd toy-demo
uv init --bare
uv add --editable ../hokea
uv add --dev pytest
```

What each uv command did. `uv init --bare` created one file,
`pyproject.toml` — the standard file describing a Python project and what
it depends on. `uv add` records a dependency in that file and installs it
into the project's own private environment (uv creates the environment
automatically the first time; nothing you install here touches the rest of
your machine). `--editable ../hokea` means hokea is installed *from your
clone, live*: when course fixes land, a `git pull` inside `hokea/` updates
hokea for you — no reinstall. And `--dev pytest` adds pytest as a
development tool — something you test with, not part of the project
itself.

The two `uv add` lines end with the list of what was installed:

```
Installed 7 packages in 14ms
 + certifi==2026.7.22
 ...
 + hokea==0.1.0 (from file:///home/you/hokea)
 ...
```

```
Installed 5 packages in 42ms
 ...
 + pytest==9.1.1
```

(The rest of this document assumes `toy-demo` sits next to your `hokea`
clone.
If you cloned it somewhere else, use your path in the `uv add --editable`
line instead — the path is recorded in `pyproject.toml`, so if you move
things around later, re-run `uv add --editable <new-path>` to update it.)

From here on, commands that run tests start with `uv run`, which runs a
command inside this project's private environment, finding it
automatically — nothing to activate, in any terminal; just be in the
`toy-demo` directory.

### 1.2 Copy the toy project — three files

```bash
cp ../hokea/examples/toyserver/server.py .
cp ../hokea/examples/toyserver/conftest.py .
cp ../hokea/examples/toyserver/test_toy.py .
```

What each file is:

- **`server.py`** — the system under test: a ~100-line replicated key-value
  store. You will run multiple instances of the server, one per node. Each node keeps a Python dict in memory
  and forwards every write to its peers. It follows the same two-part
  contract *your* course project will follow: it reads the environment
  variables `NODE_ID` / `PEERS` (an environment variable is a named value
  the operating system hands a program as it starts — Python reads them
  with `os.environ`; hokea also provides `N_NODES`) to learn
  who it is and who its peers are, and it answers `GET /health` with HTTP
  200 (the web's "success" status code) once it's ready.
- **`conftest.py`** — one pytest *fixture* (a named piece of setup that
  pytest runs for you and hands to any test that asks for it; pytest
  automatically reads any file named `conftest.py` in the test directory)
  telling hokea how to run the system: which ready-made *base image* to
  build on (an image is a frozen snapshot of a filesystem plus a program —
  the template containers are started from), which source directory, what
  command starts a node, how many nodes, which port.
  That's the entire deployment description — no Dockerfile, no compose
  file.
- **`test_toy.py`** — a small *adapter* (about 15 lines teaching hokea to
  speak the server's HTTP protocol and classify every reply as
  ok / rejected / unknown), an operation generator (what the simulated
  clients do: write keys, read them back), and three tests. The adapter
  and the operation generator are exactly what you'll write for your own
  project — about 30 lines, total.

### 1.3 Run it

```bash
uv run pytest -q
```

The first run downloads the Python base image and builds your container
image from it, so it takes a few minutes. Every run after that takes about
a minute and a half. Expected:

```
...                                                                      [100%]
3 passed in 80.63s (0:01:20)
```

**3 passed** is the result this document promises — that's the lab's
first checkpoint. If Docker isn't running you'll see errors about failing
to connect to Docker — start it and re-run. A run much slower than the
times above — the first run's image download, or a laptop on
battery-saver; the exact times don't matter, passing does. Anything else
— re-run once, and if it repeats, flag a TA with the full output.

### 1.4 What just happened

Each test brought up three containers — `hokea-toydemo-node1` through
`node3`, one per node of the toy server — connected by two private Docker
networks: a *cluster network* the nodes use to talk to each other (this is
where faults will be injected), and a *client network* the test's own
traffic uses (this one is never faulted, so hokea can always tell the
difference between "the system is down" and "I broke my own view of it").
Then hokea started several concurrent client workers, each running your
operation generator against the nodes, exactly like real users hitting a
real service.

While all that ran, hokea wrote everything down: every client operation with
the server's verbatim reply, every fault with timestamps, every JSON line
the servers printed, and a final snapshot of each node's state. The tests'
verdicts are computed *from those files*, not from anything live — which
means you can re-read the evidence yourself, right now.

The three tests you just ran, by name:

1. `test_other_nodes_stay_available_when_one_dies`
2. `test_every_received_write_was_applied`
3. `test_replication_is_not_durable_and_hokea_catches_it`

The names hint that more went on than a smoke test — nodes were killed
mid-run, and that third test passed *because* hokea caught something. Act 2
walks through each of them slowly. For now the takeaway is simpler: it's
green, and everything that happened is written down in files you can read.

### 1.5 Look inside a run directory

Every test run left a directory under `runs/`:

```bash
ls runs/
```

```
20260815-185656-test_other_nodes_stay_available_when_one_dies
20260815-185726-test_every_received_write_was_applied
20260815-185748-test_replication_is_not_durable_and_hokea_catches_it
```

One reassurance while you're looking around: hokea removes its containers
when a run ends, so `docker ps` shows an empty table again after every
test — the runs leave nothing behind on your laptop except the `runs/`
folders themselves.

Open the last one. A word on picking "the last one": run directories
are named by their start time, but later in this lab the runs that come
back from the class cluster are stamped in UTC while local runs use your
laptop's clock — so name order isn't always time order. Sort by the time
the directory was written instead (`ls -td` = directories, newest first)
and keep the winner's name in a shell variable for the next commands
(if you open a new terminal later, run the `RUN=` line again):

```bash
RUN=$(ls -td runs/*catches_it* | head -1)
ls "$RUN"
```

```
events.jsonl  faults.jsonl  history.jsonl  metadata.json  report.json  shards  states
```

What's in each, and why you care:

- **`history.jsonl`** — one line per client operation: which node, what was
  asked, the verbatim reply, when it started and returned, and the outcome
  (`ok` / `rejected` / `unknown`). This is the ground truth every check
  reads.
- **`faults.jsonl`** — the fault timeline: every kill, restart, and
  partition, with a timestamp for when it was *requested* and another for
  when it was *actually done*. Lining this file up against `history.jsonl`
  is how a check knows which operations happened "during the fault".
- **`events.jsonl`** — every JSON line your servers printed to stdout
  (their normal console output), with
  which node printed it and when. Your system's own testimony about what it
  did internally.
- **`states/`** — one JSON file per node: a snapshot of that node's store
  at the end of the run. This is how hokea proves a write is *gone* — it's
  acknowledged in `history.jsonl` but missing from a node's state file.
- `metadata.json` and `report.json` (the run's configuration and a summary
  of how fast the run went — the Interlude after this act is all about
  reading those numbers) and `shards/` (raw per-client files that were
  merged into `history.jsonl`) — you can ignore these for now.

Look at one line of real evidence:

```bash
head -1 "$RUN/history.jsonl"
```

```
{"client_id": 1, "seq": 0, "node": 1, "op": "put", "key": "c1-0", "args": {"value": "0"}, "invoke_ts": 0.004848, "return_ts": 0.013239, "outcome": "ok", "status": 200, "response": {"ok": true}, "version": null, "error": null}
```

Every verdict in this course is reproducible from files like this one.
You never have to take a test's word for what happened: the evidence is
on disk, and you can re-read it yourself.

### 1.6 About the toy server: incomplete on purpose

The toy server is deliberately **incomplete**. It is *correct* when nothing
goes wrong: writes land, replication works, reads return what was written.
But it has no retries (a replication attempt to a dead peer is simply
dropped), no re-sync (a node that restarts comes back empty and nobody
catches it up), and everything lives in memory. Nobody planted a bug in it —
these are the gaps that every distributed system has until someone
deliberately closes them.

In your course project, your task is to build a system where the same
types of faults that break the toy server (as well as others) don't break
your own.

---

## Interlude — measure it

Act 1 asked "is it correct?". Before we start breaking things, a
ten-minute detour into the other question every distributed system exists
to answer: **how much load can it take — and what happens when you add
machines?** You'll put the toy store under load, read two numbers off the
result, then double the fleet from three nodes to six and see which number
improves — and which doesn't.

The two numbers, defined once (the rest of this document uses them too):

- **Throughput** — how many operations the system completes per second,
  counted across all clients together. Bigger is better.
- **p50 / p99 latency** — sort every response time from fastest to
  slowest; we'll track both the **p50** (median) and **p99** (slowest 1% of
  requests) latencies. Ask yourself: why would we track both, and why these
  percentiles? What do you think these represent? We'll discuss this more in a
  few weeks!

### Tiny nodes on purpose

The `conftest.py` you copied in Act 1 gives every node **half a CPU and
256 MB of memory** — deliberately puny. A production service saturates at
tens of thousands of requests per second, which you can't generate (or
afford) from a laptop. So instead of scaling the load up, we scale the
servers *down*: on half-CPU nodes, thirty simulated clients are enough to
drive the system to its limit, and you get to watch production behavior —
saturation, queueing, latency spikes, capacity that grows (or doesn't)
with the fleet — at toy scale. The behavior is the same as at production
size; only the numbers are smaller. (The
server also has a `WORK` knob — a simulated per-request cost, set in
`conftest.py` — standing in for the parsing, validation, and disk work a
real request handler does; see the docstrings in both files.)

### The fourth file

```bash
cp ../hokea/examples/toyserver/test_measure.py .
```

Inside are two "experiments" written as pytest tests. Each drives 30
concurrent simulated clients for 20 seconds — no faults — and *prints* the
throughput and latency that the run recorded. The asserts in them are
deliberately loose (the run happened, operations succeeded): with
measurements, **you read the numbers**, the asserts just keep the file
runnable as a test.

- `test_read_throughput` — writes a small set of keys once, waits for them
  to replicate everywhere, then every client hammers `GET`s across all
  nodes, round-robin (taking the nodes in turn).
- `test_mixed_throughput` — the same write+read mix Act 1's tests used:
  every client writes its own keys and reads them back.

### Experiment 1 — reads, then more machines

Run the read experiment (`-k read_throughput` selects that one test by
name; `-s` tells pytest not to swallow the printed summary):

```bash
uv run pytest -q test_measure.py -k read_throughput -s
```

```
---- reads only: 3 nodes, 30 clients, 20s ----
throughput      240 ops/s
latency     p50=11.75ms  p99=412.57ms
outcomes    {'ok': 4809}
.
1 passed, 1 deselected in 34.54s
```

Your numbers will differ — they depend on your machine — but the shape is
what matters. Now the fun part. The node count comes from an environment
variable, `HOKEA_NODES` (default 3). Writing it in front of a command sets
it for that one command only — that's deliberate, so plain
`uv run pytest -q` keeps meaning "3 nodes"; don't `export` it. Doubling
the fleet therefore needs no editing (and set it only for
`test_measure.py`: the fault tests in this lab assume 3 nodes):

```bash
HOKEA_NODES=6 uv run pytest -q test_measure.py -k read_throughput -s
```

```
---- reads only: 6 nodes, 30 clients, 20s ----
throughput      476 ops/s
latency     p50=11.23ms  p99=347.12ms
outcomes    {'ok': 9512}
.
1 passed, 1 deselected in 37.41s
```

**Read throughput roughly doubled.** This is the promise that makes
people build distributed systems in the first place — reads can be served
by any node that has a copy of the data, so six half-CPU nodes really do
have twice the read capacity of three. This is what "scaling out" means:
add machines, serve more users.

### Experiment 2 — writes don't scale the same way

Same doubling, this time with the write+read mix (half of every client's
operations are writes):

```bash
uv run pytest -q test_measure.py -k mixed -s
HOKEA_NODES=6 uv run pytest -q test_measure.py -k mixed -s
```

```
---- writes+reads: 3 nodes, 30 clients, 20s ----
throughput       80 ops/s
latency     p50=20.2ms  p99=2090.65ms
outcomes    {'ok': 1602}
.
1 passed, 1 deselected in 34.10s
```

```
---- writes+reads: 6 nodes, 30 clients, 20s ----
throughput       70 ops/s
latency     p50=8.92ms  p99=4269.11ms
outcomes    {'ok': 1386, 'unknown': 6}
.
1 passed, 1 deselected in 37.31s
```

Twice the machines — and throughput went *down*, while the p99 more than
doubled (those `unknown`s are requests that timed out entirely). The p50
actually *improved*: with six nodes each one has fewer clients queued on
it, so a typical request waits less — even as the tail got worse and
total throughput fell. On your machine the mixed numbers may dip less, or
just sit flat; what they will *not* do is double the way the reads did.
The reason is arithmetic. The toy server replicates **every write to
every other node**: with 3 nodes, one client write costs the fleet 3
requests' worth of work (the write plus 2 replica copies); with 6 nodes
it costs 6. Adding servers adds read capacity, but it adds write *work*
at exactly the same rate — so the capacity you added gets spent copying
writes around, and the extra replication traffic is also what drives the
p99 up. This is a real tradeoff, not a toy quirk: full replication buys
read scaling by making every write more expensive. Sharding, quorums,
and primary/backup schemes exist to improve exactly this tradeoff — and
you now have the instrument to measure whether your project's design
does.

---

## Act 2 — Break it locally

When this act is done you will have understood exactly what Act 1's three
green tests proved — including the one that passed by catching real data
loss — watched hokea cut the network under live load, and written your own
fault schedule.

### 2.1 The kill test, slowly

Act 1's first and third tests are two questions about the *same* fault —
node 2 dies at second 4 and comes back at second 11, while three clients
write and read the whole time:

- **Does everyone else keep working?** `check.availability` slices the
  operation history by the fault timeline: before the kill, all three
  nodes' clients succeed; during it, node 1's and node 3's clients keep
  succeeding while node 2's fail (correctly — their node is
  dead). That check comes back **clean**: the property holds.
- **Did we lose anything?** While node 2 was dead, clients of nodes 1 and 3
  kept writing, and the server *acknowledged* those writes — it said "ok,
  saved". Node 2 never got them: replication to a dead peer is dropped, and
  nothing re-syncs a returning node. `check.acked_visible` compares every
  acknowledged write against the final state snapshot of every node, and
  returns **violated** — along with the exact operations that are missing
  and which node they're missing from. The test then *asserts* the
  violation: it passes because hokea caught the loss and produced the
  evidence.

Act 1's second test, `test_every_received_write_was_applied`, runs with
no faults at all. The
server prints a `recv` event and an `applied` event for every write it
handles (that's the `events.jsonl` file from Act 1), and `check.event_join`
confirms every `recv` has its matching `applied` — the system's own
testimony about its internals, audited line by line.

For client-server projects like this one, that is the pattern: the
checks compare what the system acknowledged with what actually happened
to the data. Batch-style projects (a MapReduce, distributed training) audit
the same kind of promises through the events their nodes emit instead of
client histories — hokea records both.

### 2.2 Add the partition test

A *network partition* is when machines are all still running but some can
no longer talk to each other — think a cut network cable, though a variety
of faults can cause this situation. The fifth and last file you'll copy
tests how the toy server handles one. Copy it and run everything again:

```bash
cp ../hokea/examples/toyserver/test_partition.py .
uv run pytest -q
```

```
......                                                                   [100%]
6 passed in 200.11s (0:03:20)
```

**Checkpoint — `6 passed`.**

(Six now: Act 1's three tests, the Interlude's two measurements, and the
new partition test. The measurement tests print nothing without `-s`, but
they still run — that's the extra minute.)

### 2.3 What the partition test proves

This test splits the cluster network into `{node 1, node 2}` on one side
and `{node 3}` on the other, from second 8 to second 22 of a 30-second
run. Three things, in order:

- **The cut is real.** hokea injects the fault and proves the cluster is
  actually partitioned before the test proceeds — there are subtle ways a
  cut can silently fail to happen, and hokea manages those for you,
  erroring out rather than letting a test draw conclusions from a fake
  fault. The test adds its own demonstration: a key written to node 1
  during the cut still isn't readable from node 3 seconds later (a 404 —
  without a cut, replication lands in milliseconds).
- **Everyone still serves their own clients.** Client traffic rides the
  never-faulted client network, and the toy server acknowledges writes
  locally, so during the cut *every* node — both sides — keeps answering
  its own clients. `check.availability` shows success rates above 90% on
  the majority side *and* the minority side.
- **The damage is permanent.** Writes acknowledged on one side during the
  cut never crossed it, and the toy never retries or re-syncs — so even
  after the network heals, they're still missing from the other side.
  `check.acked_visible` returns **violated** with the lost operations,
  exactly like the kill test.

Don't take the test's word for it; read the evidence yourself. Inside
the run's directory, `history.jsonl` is the ordered log of every client
operation with the server's verbatim reply, and `states/node-<i>.json`
is each node's entire store at the end of the run. The key the test
writes during the cut is named `cut-probe`:

```bash
RUN=$(ls -td runs/*partition* | head -1)
grep cut-probe "$RUN/history.jsonl"
```

```
{"client_id": 0, "seq": 0, "node": 1, "op": "put", "key": "cut-probe", "args": {"value": "x"}, "invoke_ts": 13.706348, "return_ts": 13.775434, "outcome": "ok", "status": 200, "response": {"ok": true}, "version": null, "error": null}
{"client_id": 0, "seq": 1, "node": 3, "op": "get", "key": "cut-probe", "args": {}, "invoke_ts": 16.775656, "return_ts": 16.783771, "outcome": "ok", "status": 404, "response": {"error": "not_found", "key": "cut-probe"}, "version": null, "error": null}
```

Line 1: node 1 said **ok — saved** (`"outcome": "ok"`, HTTP 200). Line
2: three seconds later, node 3 has never heard of the key (404) —
without a cut, replication lands in milliseconds. Now check the *final*
states, captured after the network healed:

```bash
grep -c cut-probe "$RUN/states/node-1.json" "$RUN/states/node-3.json"
```

```
runs/20260829-102147-test_partition_splits_replication_but_not_clients/states/node-1.json:1
runs/20260829-102147-test_partition_splits_replication_but_not_clients/states/node-3.json:0
```

`1` and `0`: node 1 still has the write it acknowledged; node 3 never
got it — and never will, because nothing in the toy re-syncs. An
acknowledged write, permanently missing from a replica: that's a
**violation**, caught and pinned to evidence.

**Checkpoint — paste the `"outcome": "ok"` put line and the `1`/`0`
state counts.** Together they are the violation: the server's promise,
and the node that never received it.

!!! warning "Troubleshooting"

    - **`ls: cannot access 'runs/*partition*': No such file or
      directory`** (and then a `grep` complaint) — there is no partition
      run yet: the test didn't pass (scroll up for pytest's error) or
      you're not in `toy-demo`.
    - **You ran the test more than once** — that's fine; the `RUN=` line
      picks the most recently written run.

### 2.4 Write your own fault schedule

Every test you've run so far asked for three helpers by naming them as
arguments (that's the fixture mechanism from `conftest.py` again). One
sentence each: `cluster` controls the nodes as processes — `cluster.kill(2)`,
`cluster.start(2)`; `net` controls the network between them —
`net.partition(...)`, `net.heal`; and `run(...)` drives a recorded client
workload against the nodes and hands back the recorded files.

A fault schedule is just a list of `(seconds_after_start, action)` pairs
passed to `run(...)`, and you can mix process and network faults freely.
Here is a complete test — try writing one like it in a new file (say
`test_mine.py`) next to the others:

```python
from test_toy import ToyAdapter, toy_ops

from hokea import check

def test_my_first_fault_schedule(cluster, net, run):
    h = run(toy_ops, ToyAdapter(), duration=30, clients=3,
            faults=[(5, lambda: cluster.kill(2)),                # t=5s:  node 2 dies
                    (10, lambda: cluster.start(2)),              # t=10s: it returns (empty!)
                    (15, lambda: net.partition([[1], [2, 3]])),  # t=15s: cut node 1 off
                    (25, net.heal)])                             # t=25s: reconnect everyone
    a = check.availability(h.history, h.faults)
    assert a.window("baseline").side("all").success_rate > 0.9
```

Change the schedule and the assertions to the question *you* want
answered, and run `uv run pytest -q` again. The times are "earliest
start": each
action fires once its offset has passed and the previous action finished.
Everything still lands in `faults.jsonl` with requested/done timestamps.
From here on, your counts will be one higher than the ones I show —
`7 passed` where I show `6 passed`, one more file shipped — because of
`test_mine.py`.

### 2.5 A check answers one question

Look at what happened inside the *same* partition run:
`check.availability` came back **clean** (every node kept serving) while
`check.acked_visible` came back **violated** (acked writes were lost).
A check isn't a thumbs up or down on your system, it's an
answer to one specific question about one recorded run. The toy server
really is available under partition, and really does lose data — both are
true, and the checks say exactly that, each carrying its evidence. (There's
a third answer a check can give — *insufficient evidence* — when the run
didn't record enough to decide; missing data never silently counts as a
pass.)

---

## Act 3 — Ship it to the class cluster

When this act is done, the same six tests will have run on the class
Kubernetes cluster — your servers on real machines, driven and checked
from inside the cluster itself — with one command; and, optionally,
you'll have poked at a live deployment with `curl` and `kubectl`.

### What "deploying to Kubernetes" means

So far the three nodes were containers on your laptop. The class cluster is
a set of real machines running **Kubernetes** — the standard system for
running containers across many machines: you describe what you want (three
copies of this program, listening on this port) and Kubernetes finds
machines for them, starts them, and wires up the networking. A running
container on Kubernetes is called a **pod** (for us: one pod = one node of
your system).

Everything your team creates lives in your team's **namespaces** — a
namespace is a named compartment of the cluster that keeps your team's
stuff separate from every other team's. Your team gets *two* of them, and
the split matters:

- **`team-NN`** (use your team's; we write `team-NN` here and everywhere
  below) — where your *servers* run. This is the room where faults
  happen: kills, partitions, CPU stress.
- **`team-NN-clients`** — where your *tests* run. Faults can never happen
  in here, by construction.

This split is Act 1's two networks, rebuilt out of namespaces. On your laptop, hokea kept the test's own
traffic on a client network it never faulted, so it could always tell
"the system is down" from "I broke my own view of it". On the cluster,
the same guarantee comes from running the observer in a separate
namespace that the fault machinery is not allowed to touch.

Which leads to the biggest change from Act 1: **your test run itself now
happens inside the cluster.** When you run `hokea test`, your laptop
packs up your project's files, ships them to the cluster, and pytest runs
in `team-NN-clients` — right next to your servers in `team-NN`, on the
cluster's fast internal network. Your laptop doesn't drive the load; it
just launches the run, streams the live pytest output to your screen, and
collects the recorded `runs/` directories when the tests finish. (This
mirrors real practice: production load tests run near the servers, not
from a laptop on the campus WiFi.)

No packaging or uploading is involved for a project like the toy server:
your handful of Python files rides along inside the request itself, and
the cluster runs them on the same stock Python image Act 1 used. Call that
**Approach A** — it's the course default, and the rest of this act uses
it. Alternatively, you could package and publish your own container images 
(**Approach B**, in Appendix A at the end of this document), which may be useful for your actual projects as they get more complex. For this introductory lab, you will only need to learn Approach A.

Two rules Approach A imposes, both worth knowing before you write your
own project's server. First, **the server must be plain Python that
imports only the standard library**: the cluster runs your files on the
stock `python:3.12-slim` image exactly as they are — there is no
`pip install` step anywhere (Act 1's local runs build on that same image
and don't install anything either). A server that needs Flask, FastAPI,
gRPC, or any other package — or that isn't Python at all — needs
Approach B. Second, **your test and adapter files can import only the
standard library plus `requests`** (and `hokea` and `pytest` themselves):
the runner that executes pytest on the cluster has exactly those
installed, nothing else, so an adapter that imports anything more works
on your laptop and fails on `hokea test`. And one rule about layout:
**only the files at the top level of your project directory ship to the
cluster** — subdirectories don't. This differs from your laptop, where
Act 1's runs mount your whole directory, subdirectories included: code
laid out as a package (`myserver/raft.py`) passes every local test and
then fails on the cluster with an import error. Keep everything at the
top level, or use Approach B.

### Are you still connected?

You installed `kubectl` and put your kubeconfig in place back in
Part 0. Re-run Part 0's proof command before going on — it takes a
second and saves a confusing failure later:

```bash
kubectl -n team-NN get pods
```

```
No resources found in team-NN namespace.
```

Anything else: Part 0's Troubleshooting under [Cluster access](#06-cluster-access).

### Before your first run: put your name on your cluster

One small edit. Open the `conftest.py` you copied in Act 1 in your
editor and search for `NAME =` (about 40 lines down):

```python
NAME = "toydemo"                # e.g. "toydemo-alice" — see the lab, Act 3
```

Change it to your own name (lowercase letters, digits, and hyphens
only, starting and ending with a letter or digit — the name ends up
inside pod names, which Kubernetes is picky about):

```python
NAME = "toydemo-alice"
```

Why: on your laptop, `toydemo` named a cluster on a machine only you use.
On the class cluster, your whole *team* shares `team-NN`, and hokea names
everything it creates after the cluster name — so if you and a teammate
both run tests as `toydemo` at the same time, the second run refuses to
start rather than trample the first (you'd see an error naming the
already-running pods; more on it below). With `toydemo-alice` and
`toydemo-bob`, you never collide. The rule worth internalizing, for this
course and afterwards: **in a shared space, everything you create carries
your name.**

### Ship it: `hokea test`

From your `toy-demo` directory (any terminal — `uv run` finds the project
environment on its own):

```bash
uv run hokea test --namespace team-NN -q
```

Everything after the hokea options (`-q` here) is passed to pytest
untouched, so all your pytest habits work: `-k partition` selects tests
by name, `-x` stops at the first failure, `-s` shows printed output.
(If you'd rather not type the namespace every time, writing
`HOKEA_NAMESPACE=team-NN` in front of the command works too — it's the
same variable your conftest reads. In front of the one command only:
don't `export` it, or your plain local `uv run pytest -q` would start
trying to talk to the cluster too.)

What you'll see, in order — the whole run is streamed to your terminal
live:

```
hokea: shipping 7 file(s) and starting the runner Job hokea-test-20260829-141530-3f2a in team-NN-clients (servers will run in team-NN)
hokea: runner pod is being created...
hokea: streaming pytest output from pod hokea-test-20260829-141530-3f2a-abc12 (Ctrl-C detaches; the Job keeps running)
hokea runner: running: pytest -q
......                                                                   [100%]
6 passed in 335.65s (0:05:35)
hokea runner: pytest exited 0; artifacts ready — holding up to 600s for the launcher to copy runs/ out
hokea: copied 6 run(s) into ./runs/: 20260829-141612-test_other_nodes_stay_available_when_one_dies, ...
hokea runner: copy-out confirmed; exiting
hokea: tests passed
```

(A *Job* is the Kubernetes object for a task that runs to completion —
here, your pytest run. Your Job name and timestamps will differ, and the
`runner pod is being created` line only shows up when the start takes a
moment. Expect the full suite to take about six minutes end to end — in
the same ballpark as the local run, with some extra startup per test
because pods on real machines take longer to come up than containers on
your laptop. During long stretches with no pytest output, hokea prints a
reassurance line about once a minute —
`hokea: still waiting for pytest to finish (pod phase Running)` — so a
long suite never just looks hung. Above the `6 passed` line you'll see
a block headed `warnings summary`, and the line itself ends with
something like `, 75 warnings` — the count varies from run to run.
Warnings are advisory notices from Python (one mentions `fork()` and
deadlocks — a Python deprecation notice, not a report that anything
went wrong); they are not failures, and only `failed` or `error` mean
trouble. And you may
see a line about `skipping ['__pycache__']` — that's hokea leaving out a
cache folder Python creates, which your tests don't need.)

Same six tests, same files, unchanged. The `conftest.py` you copied in
Act 1 checks an environment variable, `HOKEA_NAMESPACE`: `hokea test`
sets it inside the cluster-side run, so the fixture builds a
Kubernetes-backed cluster instead of a Docker one. Kills become pod
deletions, partitions become Chaos Mesh rules (Act 4 introduces Chaos
Mesh), and the recorded files and checks are byte-for-byte the same kind
of evidence.

Notice the second to last line: **your `runs/` came back.** The tests ran
on the cluster, but every recorded run directory was copied into the same
local `runs/` folder Act 1 used. You can open them exactly like before, with
`ls` and `head`. The results from the run will live on your laptop, even though
the run itself was on the cluster. Note that for large runs these could become
large.

**Checkpoint — `6 passed` and `hokea: tests passed`, and six new run
directories in your local `runs/`.**

A few things worth knowing about this command:

- **Only the files at the top level of your directory ship.** The ride to
  the cluster is a **ConfigMap** — a Kubernetes object that holds a small
  bundle of files. Some files are skipped automatically, and hokea names
  them in one line: the obvious machinery (`.venv`, `runs/`, caches),
  lockfiles such as `uv.lock`, and image and archive files (`.png`,
  `.pdf`, `.zip`, and the like). Whatever is left must total no more
  than about 1 MiB — source code fits easily; if you go over, hokea
  refuses to start the run and names your biggest files. hokea also
  *names anything else it skips out loud* — if you see
  `hokea: only top-level files ship to the cluster; skipping ['lib'] —
  tests that import these will fail on the cluster`, it means exactly
  that: code you keep in that subdirectory won't be there.
- **Ctrl-C detaches, it doesn't kill.** The run keeps going on the
  cluster, and hokea prints the exact commands to re-attach to the log
  or clean the run up. One catch, printed right there too: a detached
  run's `runs/` are *not* copied back automatically — the finished
  runner holds them for about 10 minutes for you to collect, then they
  are gone. Stay attached for the whole run unless you have a reason
  not to.
- **The exit code is pytest's own.** `hokea: tests passed` means the
  suite passed on the cluster; a red run prints pytest's real failure
  output (it was streaming to you all along) and exits nonzero, exactly
  like a local run.

!!! warning "Troubleshooting"

    - **`hokea: which team? Pass --namespace <your team's namespace>
      ...`** — you left off `--namespace`.
    - **`hokea: could not create the test Job in 'team-77-clients': ...
      Does that namespace exist, and are you a member of the team?`** —
      the namespace name is wrong (typo, or another team's). This one
      fails within seconds, before any test runs, and cleans up after
      itself.
    - **An error inside the streamed pytest output saying `a cluster
      named 'toydemo' already has running pods in namespace 'team-NN'
      ...`** — the name collision from the previous section. If those
      pods are a teammate's live run, this is the system working: pick
      your own `NAME` and re-run. If they're yours (a crashed or Ctrl-C'd
      earlier run left them behind), the fix — the message points at it
      too — is `HOKEA_TAKEOVER=1 uv run hokea test --namespace team-NN
      -q`, which replaces them. (And if things are ever so tangled you
      just want your namespace empty, `teardown.sh` at the end of this
      act sweeps everything hokea ever created out of it.)
    - **The run starts, then sits for several minutes with no test output
      at all** (stuck before the first test finishes setting up) — tell
      your instructor, and include the Job name (the `hokea-test-...`
      token) from the `hokea: shipping ...` line. This failure is a
      course-infrastructure problem, not a mistake in your setup.

### Same knobs, real machines

Two of the six tests were the Interlude's measurement experiments — which
means you just load-tested a deployment on real machines. Everything from
the Interlude works here, including the fleet-doubling variable, which
`hokea test` passes through to the cluster-side run. The numbers you get
here differ from the Interlude's, because these are real machines with the
cluster's internal network between them, not containers sharing one
laptop's CPU — the *shape* (reads scale, writes don't) is what should
match.

While a measurement runs, `kubectl -n team-NN top pods` (in a second
terminal) shows each server pod's live CPU use pinned at its half-CPU
limit while the p99 climbs — no setup, the cluster's metrics service is
already installed. If your team wants real dashboards with graphs over
time, you're free to run a monitoring stack like Prometheus+Grafana
inside your own namespace.

### Scale it on the cluster: 3 nodes vs 6

You copied `test_measure.py` in the Interlude and ran its two experiments
on your laptop. Now the same two workloads, at both fleet sizes, on the
cluster — four runs. Start them back-to-back, since most of the time is
waiting (`-k read` / `-k mixed` selects one experiment by name, `-s` shows
its printed summary, and `HOKEA_NODES=6` in front of a command runs six
server nodes for that one command only):

```bash
uv run hokea test --namespace team-NN -q test_measure.py -k read -s
HOKEA_NODES=6 uv run hokea test --namespace team-NN -q test_measure.py -k read -s
uv run hokea test --namespace team-NN -q test_measure.py -k mixed -s
HOKEA_NODES=6 uv run hokea test --namespace team-NN -q test_measure.py -k mixed -s
```

Each one looks like the `hokea test` launch above, with the experiment's summary in
the middle. The first, for example:

```
---- reads only: 3 nodes, 30 clients, 20s ----
throughput      222 ops/s
latency     p50=15.33ms  p99=439.24ms
outcomes    {'ok': 4450}
.
1 passed, 1 deselected in 35.51s
hokea runner: pytest exited 0; artifacts ready — holding up to 600s for the launcher to copy runs/ out
hokea: copied 1 run(s) into ./runs/: 20260829-144210-test_read_throughput
hokea runner: copy-out confirmed; exiting
hokea: tests passed
```

(`1 deselected` is the other experiment `-k` filtered out — expected.)

One number worth understanding if you see it: `outcomes` may include an
`'unknown'` count, like `{'ok': 1453, 'unknown': 9}`. An `unknown` is an
operation whose answer never came back before the client gave up — hokea
can't tell whether it landed or not, so it refuses to guess and records
exactly that. Under heavy write load a few unknowns are normal (they're
the timeouts hiding inside that huge p99), and they're worth a close
look: timeouts under load are classic distributed-systems behavior, and
you'll meet them again in your own project.
Budget a minute or two per run. Watch the numbers as the four summaries go
by: read throughput should roughly **double** at 6 nodes; the mixed
workload's should **not** — the toy replicates every write to every
node, so extra machines bring exactly as much new replication work as
new capacity. The next section turns this into the graph.

Confirm all four runs came back — `ls -t` lists newest first, so the
Interlude's local `throughput` runs sit further down and `head -4` cuts
them off:

```bash
ls -t runs/ | grep throughput | head -4
```

```
20260829-144210-test_read_throughput
20260829-144655-test_read_throughput
20260829-145120-test_mixed_throughput
20260829-145601-test_mixed_throughput
```

**Checkpoint — the four newest `throughput` run directories are your
cluster runs** (two `read`, two `mixed`; timestamps yours — remember
these are stamped in UTC).

!!! warning "Troubleshooting"

    - **No summary printed but the test passed** — you dropped `-s`; the
      run still recorded everything (the graph below reads the files, not
      the screen), but re-run with `-s` if you want to see it live.
    - **`HOKEA_NODES=6` had no effect, or every later run silently
      became six-node** — `HOKEA_NODES=6` must be *in front of* the
      command, as shown; it sets the variable for that one command only.
      Don't `export` it.
    - **A run collides with a teammate's** — the `HOKEA_TAKEOVER` /
      `NAME` Troubleshooting above applies.

### One graph

Copy `plot_scale.py` into your toy-demo directory, `uv add matplotlib`
(one-time), `uv run python plot_scale.py`, open `scale.png` — reads
should roughly double, reads+writes should not. This picture is the
evidence you'd show to back up a claim about how your system scales.

```bash
cp ../hokea/examples/toyserver/plot_scale.py .
uv add matplotlib
uv run python plot_scale.py
```

```
(8 runs from other tests left off the chart — it only plots the measurement experiments)
reads + writes at 3 nodes: p99 latency 1076.18 ms
reads + writes at 6 nodes: p99 latency 5194.0 ms
reads only at 3 nodes: p99 latency 439.24 ms
reads only at 6 nodes: p99 latency 326.67 ms
wrote scale.png — open it to see how throughput scaled
```

(Your numbers and skipped-run count will differ; if you repeated an
experiment, a `using your most recent ... run` line tells you the newest
one won.) Open the picture — macOS: `open scale.png`; Linux:
`xdg-open scale.png`; WSL: `explorer.exe scale.png`. Two side effects
to know about: `uv add matplotlib` makes `uv.lock` grow, and
`plot_scale.py` leaves `scale.png` in your project folder — both are
skipped automatically when hokea ships your directory to the cluster,
so the cluster runs later in this lab are unaffected.

What you should see: two bars per fleet size. The **reads only** bar
roughly doubles from 3 nodes to 6 — reads can be served by any node
that has a copy, so more machines really do mean more capacity. The
**reads + writes** bar stays roughly flat (or drops) — every write must be
copied to every node, so the machines you added spend their new capacity
copying writes around. That is the whole lesson in one picture: adding
servers buys capacity only when the work per operation stays fixed, and
full replication makes write work grow with the fleet. Making that
tradeoff come out better — sharding, quorums, primary/backup — is what
your course project is for.

**Checkpoint — `scale.png`, showing read throughput roughly doubling
and mixed throughput staying put.**

!!! warning "Troubleshooting"

    - **An error asking you to run `uv add matplotlib`** — do exactly
      that, then re-run.
    - **`run the scale experiments first — see the introductory lab, Act
      3`** — the four runs above aren't in `runs/` (are you in
      `toy-demo`?).
    - **A bar pair missing at one fleet size** — one of the four runs
      didn't finish; re-run that command from the previous section.

### The optional poke-at-it path: `hokea k8s up`

Test runs tear everything down when they finish. To get a deployment that
*stays up* so you can poke at it by hand, use the `hokea` command-line
tool directly (run it from your
`toy-demo` directory — and note the `--name`: same shared-space rule,
name it after yourself):

```bash
uv run hokea k8s up --namespace team-NN --name alice --src . --cmd "python server.py"
```

After half a minute or so:

```
hokea: ConfigMap source mode ships top-level regular files only; skipping ['.hokea', '.venv', 'runs']

Your cluster is up: 3 node(s) in namespace 'team-NN', and it stays up until you take it down.

node 1's last log lines:
  toyserver node 1 listening on 8000

reach a node from here (each forward keeps running in its own terminal until Ctrl-C):
  node 1: kubectl -n team-NN port-forward pod/hokea-alice-node1 8000:8000
          then, in another terminal: curl http://localhost:8000/health
  node 2: kubectl -n team-NN port-forward pod/hokea-alice-node2 8001:8000
          then, in another terminal: curl http://localhost:8001/health
  node 3: kubectl -n team-NN port-forward pod/hokea-alice-node3 8002:8000
          then, in another terminal: curl http://localhost:8002/health

see your pods: kubectl -n team-NN get pods
take it down:  hokea k8s down --name alice
```

(The `skipping` line is normal, and the exact list varies: anything in
your directory that isn't a top-level source file — the project
environment, `runs/`, caches, and by now `uv.lock` and `scale.png` —
is left out of the shipment, which is exactly what you want. One quirk of the printed hints: they omit the
`uv run` prefix, so type the last one as
`uv run hokea k8s down --name alice`.)

Your deployed nodes have no public address — pods on the class cluster
are reachable only from inside it (your tests, running in
`team-NN-clients`, reach them directly; that's the point of `hokea test`).
To reach one *from your laptop*, you open a temporary private tunnel with
`kubectl port-forward` — exactly the lines the output above printed: the
command connects a port on your laptop (`localhost:8000`) to the pod's
port, and keeps the tunnel open until you press Ctrl-C. So, in a
**second terminal**:

```bash
kubectl -n team-NN port-forward pod/hokea-alice-node1 8000:8000
```

```
Forwarding from 127.0.0.1:8000 -> 8000
```

Leave that running, and in a **third terminal** talk to your deployed
node through the tunnel:

```bash
curl http://localhost:8000/health
```

```
{"ok": true, "node_id": 1}
```

Then look at your three nodes as Kubernetes sees them:

```bash
kubectl -n team-NN get pods
```

```
NAME                READY   STATUS    RESTARTS   AGE
hokea-alice-node1   1/1     Running   0          61s
hokea-alice-node2   1/1     Running   0          61s
hokea-alice-node3   1/1     Running   0          61s
```

(Test runs create and destroy their own fresh pods, named after your
conftest's `NAME` — `hokea-toydemo-alice-...` — and never touch this
standing deployment's `hokea-alice-...` pods; if you run tests while
this is up and see six pods at once, that's normal.)

That's your code, running on the class cluster. Write a key to node 1 and
read it back from node 2 — replication, across real machines. Open node
2's tunnel too, in a **fourth terminal** (its line is in the `up`
output — note it uses local port 8001, so both tunnels can stay open at
once), then, back in the third terminal:

```bash
curl -X PUT http://localhost:8000/kv/hello -d world
curl http://localhost:8001/kv/hello
```

```
{"ok": true}
{"key": "hello", "value": "world"}
```

`uv run hokea k8s status --name alice` re-prints the port-forward lines
and pod table any time. When you're done (cluster machines are shared —
always take your deployment down):

```bash
uv run hokea k8s down --name alice
```

```
removed .hokea/k8s-alice.json
deleted cluster 'alice' (pods, services, configmaps, chaos rules) from namespace 'team-NN'
```

If a run ever crashes half-way and leaves pods behind, sweep everything
hokea created out of your namespace with:

```bash
../hokea/examples/toyserver/teardown.sh team-NN   # team-NN → your team's namespace
```

```
No resources found
No resources found
```

Two lines of `No resources found` is the answer you want: the script
asks the cluster to delete everything hokea labeled in your namespace
(pods, services, and files in one sweep, chaos experiments in a second),
and each sweep reports that there was nothing left to remove — nothing
was left behind.

**Limits of the no-image Approach A.** The shipment (for `hokea test`
and `hokea k8s up` both) carries only the files at the *top level* of
your project directory (no subdirectories — unlike local runs, which
mount everything). Lockfiles (`uv.lock`) and image or archive files are
skipped automatically, and the launcher names what it skipped; whatever
remains must total no more than about 1 MiB, or the launcher refuses to
run and names your biggest files. Source code fits easily; a compiled
binary, a big dependency, or a bundled dataset does not. When your
project outgrows this, that's what Approach B is for.

**Differences you'll notice on the cluster** These matter when you write *your*
project's server — the toy already follows them, so there's nothing to do right
now:

- A restarted node comes back *empty* and with a new address — keep
  node-local scratch in `/data` (wiped on restart on both backends) and
  treat only replicated state as durable. You cannot assume there is a
  persistent "disk" available; it dies when the node dies.
- Always reach peers by their `PEERS` names (`node-2:8000`), never by a
  cached IP address — fault injection targets those name-to-name paths.
- Pod restarts take 5–20 seconds, not milliseconds.
- One local-only fault you haven't met yet — `cluster.pause()` (freeze a
  node's process without killing it) — exists only on the Docker backend;
  on the cluster it stops with an error naming the Chaos Mesh alternative.
  `cluster.throttle()` (slow a node's CPU down) works on both backends: on
  the cluster it shrinks the pod's CPU limit in place (no restart) and
  verifies the applied value by reading it back.

### Approach B — build and publish your own image

The class project doesn't require this, and nothing in the rest of this
lab uses it. When your project outgrows Approach A — a server that needs
packages beyond the standard library, isn't Python, keeps its code in
subdirectories, or ships more than about 1 MiB — the answer is to package
it as a container image and publish it where the cluster can download it.
The full recipe, using the toy server, is
[Appendix A](#appendix-a-approach-b-build-and-publish-your-own-image) at
the end of this document; skip it for now.

---

## Act 4 — Break it in prod

When this act is done you will have watched real fault-injection machinery
cut the network between pods on the class cluster, seen the experiment
objects appear and vanish, and injected a fault of your own design from
your own test — plus seen that hokea records even faults it didn't inject.

### The same partition, injected by Chaos Mesh

On your laptop, hokea created partitions with firewall rules inside the
containers. On the class cluster it delegates to **Chaos Mesh**, an
industry-standard open-source fault-injection system for Kubernetes, used to
test real production systems. Chaos Mesh injects a range of realistic faults by
manipulating pods from the inside: network rules, CPU load, clock skew. On the
class cluster (where I have installed it cluster-wide), hokea drives Chaos Mesh
for you — it creates the fault experiments and then independently verifies each
fault took hold, erroring out if not. In addition, you're not limited to what
hokea itself can inject as faults: anything else you inject through Chaos Mesh
yourself is still captured in the run's record — more on that at the end of
this act.

Run just the partition test against the cluster — same launcher as Act 3,
with a pytest filter on the end:

```bash
uv run hokea test --namespace team-NN -q test_partition.py
```

While it runs, watch the experiment object come and go in a **second
terminal** (`-w` means *watch*: the command keeps running and prints a new
line whenever an experiment appears or changes; `Ctrl+C` stops it):

```bash
kubectl -n team-NN get networkchaos -w
```

Be patient: nothing appears for the first minute or two — the runner and
then the test's server pods have to start before the workload's clock
begins. Then, around the workload's 8-second mark, hokea's experiment
shows up — a `NetworkChaos` object Chaos Mesh enforces for as long as it
exists (the name carries your conftest's `NAME`; yours will match your
own):

```
NAME                              ACTION      DURATION
hokea-toydemo-alice-part0-x7k2m   partition
```

(More lines for the same object may follow as its status updates, and the
blank `DURATION` is on purpose: hokea heals by *deleting* the object at
the 22-second mark rather than giving it a lifespan.) After the test
ends, press `Ctrl+C` and confirm nothing is left:

```bash
kubectl -n team-NN get networkchaos
```

```
No resources found in team-NN namespace.
```

That's the whole lifecycle: hokea creates a fresh experiment object to
install a fault, verifies it took effect, and deletes it to heal. Back in
your first terminal:

```
.                                                                        [100%]
1 passed in 54.91s
hokea runner: pytest exited 0; artifacts ready — holding up to 600s for the launcher to copy runs/ out
hokea: copied 1 run(s) into ./runs/: 20260829-150312-test_partition_splits_replication_but_not_clients
hokea runner: copy-out confirmed; exiting
hokea: tests passed
```

Same test file as Act 2, and the run's evidence is back in your local
`runs/` as always. Same verdicts, now proven against real machines with
more realistic fault machinery.

You can re-run Act 2's two `grep`s on the new run directory and find the
same acked-then-lost write, this time across real machines. Several
`*partition*` runs match by now, and the cluster one is stamped in UTC,
so pick it by time written, as in Act 2:

```bash
RUN=$(ls -td runs/*partition* | head -1)
grep cut-probe "$RUN/history.jsonl"
grep -c cut-probe "$RUN/states/node-1.json" "$RUN/states/node-3.json"
```

**Checkpoint — `hokea: tests passed` for the partition test on the
cluster** (and, if you watched, you saw the `networkchaos` object appear
and vanish).

!!! warning "Troubleshooting"

    - **The watch shows nothing *ever*** — most likely the test failed
      before the fault fired (read the first terminal), or you watched
      the wrong namespace.
    - **The watch command itself errors** (e.g. `the server doesn't have
      a resource type "networkchaos"`) — tell your instructor; the watch
      is a bonus, the test itself is unaffected.

Everything from Act 3's `hokea test` Troubleshooting applies here too.

### Injecting a fault of your own design: CPU stress

Here's how you inject a fault hokea has no wrapper for: making one node's
CPU busy, so it responds slowly — a *straggler*. You describe the
experiment in a small file, and hokea runs it as a step in a normal
recorded test run.

Create a file named `stress.yaml` — a *manifest*, a small text file
describing the experiment you want (change `team-NN` to your namespace and
`toydemo-alice` to your conftest's `NAME`; the format is YAML, which is
indentation-sensitive, so copy it exactly, spaces not tabs):

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: cpu-stress-node1
spec:
  mode: all
  selector:
    namespaces: [team-NN]
    labelSelectors:
      hokea/cluster: hokea-toydemo-alice
      hokea/node: "1"
  stressors:
    cpu:
      workers: 2
      load: 100
```

Reading it: find every pod labeled as node 1 of the hokea cluster named
`toydemo-alice` in your namespace (hokea labels every pod it creates with
`hokea/cluster` and `hokea/node`, exactly so you can target them like
this), and run 2 CPU-burning workers at full load inside it. Note the
label's value is `hokea-` + your conftest's `NAME` — hokea adds that
prefix, so keep it when you substitute your own name. Notice also what
you did *not* write: no duration — your test decides when the stress
ends.

Now hand the file to hokea inside a test. The `chaos` fixture takes your
YAML as a fault-schedule step, exactly like the built-in faults. Put the
test in a new file, `test_stress.py`, next to the others, and start it
with the two-line skip marker shown below — this test is cluster-only:
the `chaos` fixture doesn't exist on the Docker backend, and without the
marker pytest would still collect the test locally, build and start the
whole Docker cluster, and then error out asking for `chaos`. The marker
tells pytest to skip the test unless `HOKEA_NAMESPACE` is set — which
`hokea test` does inside the cluster-side run, and your plain local
`uv run pytest -q` never does — so locally you'll see `6 passed, 1
skipped` and the suite stays green. Run it with
`uv run hokea test --namespace team-NN -q test_stress.py` — both new files
sit at the top level of your directory, so they ship with everything
else, and `chaos.apply("stress.yaml")` finds the YAML right there on the
cluster side:

```python
import os

import pytest
from test_toy import ToyAdapter, toy_ops

from hokea import check

pytestmark = pytest.mark.skipif(not os.environ.get("HOKEA_NAMESPACE"),
                                reason="Chaos Mesh exists only on the class cluster")

def test_cpu_stress_makes_node1_a_straggler(cluster, chaos, run):
    # clients=2 keeps the load light on purpose: this experiment measures
    # how slow the stressed node's responses get, and we don't want the
    # queueing delays of a saturated cluster (which the Interlude created
    # deliberately) drowning that signal out.
    h = run(toy_ops, ToyAdapter(), duration=60, clients=2,
            faults=[(10, lambda: chaos.apply("stress.yaml")),
                    (40, chaos.clear)])
    a = check.availability(h.history, h.faults)
    stressed = a.window("chaos:stresschaos")
    # The stressed p99 typically lands several times above the baseline's;
    # assert the direction, not a specific factor.
    assert stressed.side("all").latency_ms["p99"] > \
        a.window("baseline").side("all").latency_ms["p99"]
```

The test itself runs for a bit over a minute (`duration=60` plus the
usual startup), ending in the familiar
`1 passed ... hokea: tests passed` tail. At the 10-second mark
`chaos.apply` creates your experiment and waits until Chaos Mesh
confirms the stress is actually running inside the pod before the
schedule moves on; at 40, `chaos.clear` deletes it and waits for the
recovery to finish. The run's `faults.jsonl` shows the whole lifecycle:

```
{"event": "chaos", "kind": "stresschaos", "chaos_name": "cpu-stress-node1-wr6gq", "source": "applied", ..., "ts": 10.32}
{"event": "chaos_done", "kind": "stresschaos", "chaos_name": "cpu-stress-node1-wr6gq", "ts": 11.36}
{"event": "chaos_recovered", "kind": "stresschaos", "chaos_name": "cpu-stress-node1-wr6gq", "ts": 40.09}
```

Two details worth noticing. The experiment's name grew a random suffix:
hokea creates a *fresh* experiment object on every apply, because Chaos
Mesh ignores a re-applied used one — so your test can re-run forever
without you renaming anything. And `check.availability` turns the
chaos_done→chaos_recovered span into a window (here labeled
`chaos:stresschaos:cpu-stress-node1-wr6gq`; `a.window(...)` matches by
prefix, which is why the test can ask for just `chaos:stresschaos`), so
the assert reads the latency of exactly the operations that ran under
stress.

!!! warning "Troubleshooting"

    - **`chaos.apply` errors out saying Chaos Mesh never confirmed the
      fault was injected** — the usual cause is a selector matching zero
      pods: your `stress.yaml`'s label doesn't match your `NAME` (check
      with `kubectl -n team-NN get pods --show-labels` while a test
      runs).
    - **The p99 assert itself fails** — re-run once before digging: with
      only two light clients the signal is real but small, and one quiet
      run can happen. Two failures in a row means look at `faults.jsonl`
      and check the stress actually landed on a node your clients were
      talking to.

Don't expect every response in that window to be slow — the effect is
spikier than that: most responses stay near the run's normal p50, but again
and again the stressed pod uses up its CPU allowance for the current
scheduling slice and everything inside it — your server included — waits
for the next one. That's why the assert reads the p99 rather than the p50
(both defined back in the Interlude): the spikes move the p99 hard while
the p50 barely notices — exactly the straggler behavior this experiment
set out to create.

You can still `kubectl apply -f stress.yaml` by hand (or build an
experiment in the Chaos Mesh dashboard — Appendix B) while a test runs —
hokea watches the
namespace and records experiments it didn't create the same way, so it
lands in `faults.jsonl` too. `chaos.apply` just spares you the fiddly
parts: the timing (the pods must exist when the experiment is created,
and inside the schedule they always do), the namespace (hokea fills in
yours), the re-run renaming, and the cleanup — anything you forget to
`clear()` is deleted when the cluster comes down.

---

## Where this leaves you

Count what you watched hokea catch in these four acts: writes acknowledged
and then lost when a node died; a node that came back empty with nobody
re-syncing it; writes that never crossed a partition and were never
retried; a node that a little CPU pressure turned into a straggler. Every
one of those is a *gap in the toy server* — no retries, no re-sync, no
durability, no defenses — and every one has a known remedy that real
systems implement.

Your course project is to build a system where these same types of tests -- the
faults, the correctness checks, etc -- come up clean on both your laptop and
the cluster because your design actually accounts for their possibility, unlike
the toy example we've used here. Remember, hokea isn't a grading script: it
records what your system did, and your role is to use it to write tests to show
that your claims hold given the evidence hokea collects.

---

## Appendix A — Approach B: build and publish your own image

The class project doesn't require this. But "package my program as an
image anyone can run" is a skill worth having, and it's the industry
answer to what Approach A does with a file shipment. It works on the
class cluster, too: the cluster downloads publicly published images
from the internet exactly like your laptop does, so once your image is
published — and marked public — the cluster can run it. Here's the
whole path, using the toy server.

Why is there an "upload" step at all? A machine that will run your image
must be able to *download* it, and your laptop isn't reachable from other
machines — so you push the image to a **registry**, a public server that
hosts images (like a package index, but for container images). We'll use
GitHub's free one, `ghcr.io`.

**1. Write a Dockerfile.** In your `toy-demo` directory, create a file
named `Dockerfile` (a recipe for building an image) with exactly this:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY server.py .
EXPOSE 8000
CMD ["python", "server.py"]
```

Line by line: start from the official slim Python image; work in `/app`;
copy the server code in; document that it listens on port 8000; run it.

**2. Build and test it locally.**

```bash
docker build -t toyserver:v1 .
```

```
...
 => => naming to docker.io/library/toyserver:v1
```

Run one container from it and check it answers (this will sit in the
foreground; that's expected):

```bash
docker run --rm -p 8000:8000 toyserver:v1
```

```
toyserver node 1 listening on 8000
```

In a **second terminal**:

```bash
curl http://localhost:8000/health
```

```
{"ok": true, "node_id": 1}
```

Press `Ctrl+C` in the first terminal to stop it.

**3. Create a registry account and log in.** On GitHub: *Settings →
Developer settings → Personal access tokens → Tokens (classic) → Generate
new token (classic)*, check the **`write:packages`** scope, and copy the
token it shows you. Then (replace `YOURNAME` with your GitHub username,
here and everywhere below):

```bash
docker login ghcr.io -u YOURNAME
```

Paste the token when it asks for a password.

```
Login Succeeded
```

**4. Tag and push.** A registry image name includes where it lives:

```bash
docker tag toyserver:v1 ghcr.io/YOURNAME/toyserver:v1
docker push ghcr.io/YOURNAME/toyserver:v1
```

```
...
v1: digest: sha256:… size: …
```

One last click: on GitHub, go to your profile → *Packages* → `toyserver` →
*Package settings* → *Change visibility* → **Public** — so machines can
download it without logging in.

**5. Point hokea at it.** In `conftest.py`, the Kubernetes branch changes
from `image=` + `src=` + `cmd=` to `image=` alone — your code is *inside*
the image now, and the image's own start command runs:

```python
return KubeCluster(image="ghcr.io/YOURNAME/toyserver:v1",
                   nodes=nodes, name=NAME,
                   namespace=namespace, port=8000, env=ENV,
                   cpus=0.5, memory="256Mi",
                   workdir=tmp_path / "cluster")
```

On any cluster that can pull public images — the class cluster
included — `hokea test` then works exactly as before, with the pods
running your published image. Two caveats. First, the *Public* click in
step 4 is required: the cluster has no login for your registry
account, so a private image fails to download — pods sit in
`ImagePullBackOff`, Kubernetes-speak for "couldn't fetch the image".
If your image genuinely can't be made public (say it contains something
you may not publish), ask your instructor: the course staff can load an
unpublished image directly onto the cluster's machines by hand, and
your conftest change above works unchanged once they have. Second, the
`hokea k8s up` command always ships a `--src` directory, so the
image-alone mode is used from `conftest.py` rather than the CLI.

That's the entire industry workflow — Dockerfile, build, push, deploy —
and now you've done it once. For the class, Approach A remains the
default: with Approach B, every code change means rebuilding and
re-publishing the image before you can re-run, which is safer but slower.
(Real teams automate exactly that step — a GitHub Action that rebuilds
the image on every push is easy to set up, and left as an exercise.)

## Appendix B — The Chaos Mesh dashboard and Workflows (optional; needs a per-team token)

Nothing in the lab needs this appendix. The dashboard needs a per-team
login token that I hand out separately, later in the course. It's here so
you know what's coming.

### The Chaos Mesh dashboard

Chaos Mesh also has a web dashboard — your instructor gives you the URL and
a login token. It shows every experiment in your namespace on a timeline
(including the ones hokea creates during your test runs, appearing and
disappearing live), and it can inject fault types hokea doesn't wrap:
CPU/memory stress, disk I/O delays, clock skew, and more. And they land in
your run's record too: hokea watches your namespace for chaos experiments
it didn't create, so a fault you inject by dashboard or by hand shows up
in `faults.jsonl` as `chaos` / `chaos_done` / `chaos_recovered` events
named after your experiment, and `check.availability()` gives it a window
labeled `chaos:<kind>:<name>`. (hokea doesn't attribute hand-made faults
to specific nodes — the window has a single side, `all`.)

### Chaos Mesh Workflows

So far, one file meant one fault. Chaos Mesh also has *Workflows*: a
single YAML that chains several experiments — run this CPU stress for 30
seconds, then that network delay, or both at once — like a fault schedule
that lives on the cluster. One thing to understand before using one: a
workflow times its steps relative to *its own start*, cluster-side,
knowing nothing about your client load — by itself it can't line up with
your run. Applying it through hokea closes that gap: hokea anchors the
workflow's start to a known moment in your schedule, and as each step's
experiment fires, it lands in `faults.jsonl` individually, with real
timestamps — the record shows what actually happened and when, not what
the workflow intended.

```python
# a sketch — workflow.yaml is a file you'd write yourself (link below)
faults=[(10, lambda: chaos.apply("workflow.yaml")),
        (50, chaos.clear)]
```

How to write one: <https://chaos-mesh.org/docs/create-chaos-mesh-workflow/>
