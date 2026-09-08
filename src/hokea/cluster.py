"""Cluster: run your system as a fleet of Docker containers.

You point hokea at a base image, a source directory, and a start command
(or supply your own Dockerfile). hokea generates the compose configuration,
builds the image, and runs N nodes attached to two networks:

- the *client* network: how tests and load generators reach nodes. Never faulted.
- the *cluster* network: how nodes reach each other (PEERS points here). This is
  the only network faults touch.

The client network is attached at higher priority so it becomes the container's
primary interface: published ports and harness traffic never ride the interface
that partitions and netem act on.

The contract a system must meet: read NODE_ID (1-based), N_NODES, and PEERS
(comma-separated addresses on the cluster network), and answer GET /health with
HTTP 200 once ready to serve.

Everything Docker-visible is prefixed `hokea-<name>-`; cleanup only ever touches
this cluster's own compose project.
"""

import json
import random
import re
import shlex
import socket
import time
from pathlib import Path

import requests
import yaml

from .events import EventCollector
from .proc import CommandFailed, sh
from .runs import FaultLog


def _free_port() -> int:
    """A random port from below the OS/Windows dynamic ranges that is free on
    this side. Docker Desktop can still refuse it (the Windows host may hold it
    invisibly), so Cluster.up() retries with fresh ports on that error."""
    for _ in range(50):
        port = random.randint(20000, 32000)
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise RuntimeError("could not find a free host port in 20000-32000")

# Installed into no-Dockerfile-mode images: iptables + tc for the in-container
# fault backend, ping for partition verification probes. Tries apt then apk so
# both Debian- and Alpine-based images work; the build fails loudly if neither does.
FAULT_TOOLS_INSTALL = (
    "(apt-get update && apt-get install -y --no-install-recommends "
    "iptables iproute2 iputils-ping && rm -rf /var/lib/apt/lists/*) "
    "|| apk add --no-cache iptables iproute2"
)


def dataset_mounts(data) -> dict:
    """data= as {mount_path: source}, shared by both backends: a single
    source mounts at /dataset (the original form), a dict {name: source}
    mounts each at `/dataset/<name>`. Names become path components and (on
    k8s) volume names, so they must be DNS-label-ish; what a *source* is —
    a host directory here, an OCI image reference on Kubernetes — is the
    backend's own validation."""
    if not isinstance(data, dict):
        # "" counts as "no dataset", same as None: resolving an empty path
        # would silently mount the caller's current directory at /dataset.
        return {"/dataset": data} if data else {}
    if not data:
        raise ValueError(
            "data= got an empty dict; pass {name: source} entries, or a "
            "single source for a plain /dataset mount")
    mounts = {}
    for name, source in data.items():
        if (not isinstance(name, str) or len(name) > 54
                or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)):
            raise ValueError(
                f"data= name {name!r}: dataset names become mount paths "
                "(/dataset/<name>) and Kubernetes volume names, so use "
                "lowercase letters, digits, and '-' (max 54 chars)")
        mounts[f"/dataset/{name}"] = source
    return mounts


class Node:
    """One running container. `index` is 1-based, matching NODE_ID."""

    def __init__(self, cluster: "Cluster", index: int):
        self.cluster = cluster
        self.index = index
        self.name = f"{cluster.prefix}-node{index}"

    @property
    def url(self) -> str:
        """Base URL for reaching this node from the harness (client network).
        Stable for the cluster's lifetime, even while the node is down or
        paused — clients keep a fixed address and observe the outage."""
        return f"http://127.0.0.1:{self.host_port()}"

    def host_port(self) -> int:
        return self.cluster._host_ports[self.index]

    def cluster_ip(self) -> str:
        out = sh(
            "docker", "inspect", "-f",
            "{{json .NetworkSettings.Networks}}", self.name,
        ).stdout
        networks = json.loads(out)
        return networks[f"{self.cluster.prefix}-cluster"]["IPAddress"]

    def exec(self, *argv: str, check: bool = True):
        return sh("docker", "exec", self.name, *argv, check=check)

    def logs(self, tail: int = 50) -> str:
        result = sh("docker", "logs", "--tail", str(tail), self.name, check=False)
        return result.stdout + result.stderr

    def is_running(self) -> bool:
        result = sh("docker", "inspect", "-f", "{{.State.Running}}", self.name, check=False)
        return result.stdout.strip() == "true"

    def status(self) -> str:
        """Docker's view: "running", "paused", "exited", ... — note a paused
        container still counts as Running for is_running()."""
        result = sh("docker", "inspect", "-f", "{{.State.Status}}", self.name, check=False)
        return result.stdout.strip() or "gone"


class Cluster:
    """
        with Cluster(image="python:3.12-slim", src="./myserver",
                     cmd="python server.py", nodes=5) as c:
            c.wait_healthy()
            ...

    Either image+src+cmd (no-Dockerfile mode) or dockerfile= (a build context
    directory containing a Dockerfile). `port` is the port the system listens on
    inside the container; hokea publishes it per node on an ephemeral host port.
    `peer_port` (default: `port`) is the port written into PEERS entries.

    `data=` mounts a dataset (a log corpus, MNIST, ...) read-only in every
    node: a single directory path appears at /dataset; a dict
    {name: directory} appears at `/dataset/<name>` each. It is a live bind
    mount of the host directory — edits on the host show up in the nodes.
    The k8s backend takes the same shapes at the same paths, but sources
    there are OCI image references (see KubeCluster). /data stays the
    separate per-node writable scratch tmpfs.
    """

    def __init__(self, *, image: str | None = None, src: str | None = None,
                 cmd: str | None = None, dockerfile: str | None = None,
                 data: str | Path | dict | None = None,
                 nodes: int = 3, name: str = "default", port: int = 8000,
                 peer_port: int | None = None, env: dict[str, str] | None = None,
                 cpus: float = 1.0, memory: str = "512m",
                 workdir: str | Path | None = None, up_timeout: float = 300):
        if dockerfile is None and not (image and src and cmd):
            raise ValueError("provide either image+src+cmd or dockerfile=")
        if dockerfile is not None and (image or src or cmd):
            raise ValueError("dockerfile= replaces image/src/cmd; don't mix them")
        self.image = image
        self.src = Path(src).resolve() if src else None
        self.cmd = cmd
        self.data = {}
        for mount_path, source in dataset_mounts(data).items():
            # "" would resolve to the current directory — reject it like any
            # other non-path, before Path() can turn it into a real mount.
            if not isinstance(source, (str, Path)) or not str(source):
                raise ValueError(
                    f"data= source for {mount_path} must be a directory "
                    f"path, got {source!r}")
            resolved = Path(source).resolve()
            if not resolved.exists():
                raise ValueError(f"data= path does not exist: {resolved}")
            if not resolved.is_dir():
                raise ValueError(
                    f"data= source for {mount_path} is not a directory: "
                    f"{resolved} — point it at the folder holding the "
                    "dataset files")
            self.data[mount_path] = resolved
        self.dockerfile = Path(dockerfile).resolve() if dockerfile else None
        self.n_nodes = nodes
        self.name = name
        self.prefix = f"hokea-{name}"
        self.port = port
        self.peer_port = peer_port if peer_port is not None else port
        self.env = env or {}
        self.cpus = cpus
        self.memory = memory
        # Raise this on a busy shared machine: unrelated Docker load can slow
        # container startup well past any reasonable-looking default.
        self.up_timeout = up_timeout
        self.workdir = Path(workdir) if workdir else Path(".hokea") / name
        self._nodes = [Node(self, i) for i in range(1, nodes + 1)]
        self._host_ports: dict[int, int] = {}
        # Every fault (process faults here, network faults via Net) is recorded
        # as a pair of events; run_workload writes them to faults.jsonl.
        self.fault_log = FaultLog()
        self._observers = []  # called with the node index after start/unpause
        self.events: EventCollector | None = None  # set by up()

    # -- lifecycle ------------------------------------------------------------

    def up(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._build_image()
        compose_path = self.workdir / "compose.yaml"
        # Fixed host ports, picked free at up() time: they survive node
        # restarts and pauses (an ephemeral `-p 0` port would not). Docker
        # Desktop occasionally refuses a port that looked free (the Windows
        # host can hold ports invisibly from WSL) — retry with fresh ports.
        for attempt in range(3):
            self._host_ports = {n.index: _free_port() for n in self._nodes}
            compose_path.write_text(
                yaml.safe_dump(self._compose_config(), sort_keys=False))
            try:
                sh("docker", "compose", "-p", self.prefix, "-f", str(compose_path),
                   "up", "-d", "--force-recreate", timeout=self.up_timeout)
                # Start following every node's stdout now, before any client
                # traffic can exist: runs slice this capture by byte offset.
                self.events = EventCollector(self, self.workdir / "events")
                self.events.attach_all()
                return self
            except CommandFailed as e:
                port_clash = ("ports are not available" in e.stderr
                              or "port is already allocated" in e.stderr)
                if not port_clash or attempt == 2:
                    raise
                print(f"hokea: host port refused by Docker, retrying with "
                      f"fresh ports ({e.stderr.strip().splitlines()[-1]})",
                      flush=True)
                self.down()
        raise AssertionError("unreachable")

    def down(self):
        if self.events is not None:
            self.events.stop()
            self.events = None
        compose_path = self.workdir / "compose.yaml"
        if compose_path.exists():
            sh("docker", "compose", "-p", self.prefix, "-f", str(compose_path),
               "down", "--volumes", "--remove-orphans", "-t", "5", timeout=300)

    def __enter__(self):
        return self.up()

    def __exit__(self, *exc):
        self.down()

    # -- nodes ----------------------------------------------------------------

    def node(self, i: int) -> Node:
        if not 1 <= i <= self.n_nodes:
            raise IndexError(f"node index {i} out of range 1..{self.n_nodes}")
        return self._nodes[i - 1]

    def make_net(self):
        """The fault injector matching this backend (the pytest plugin's
        `net` fixture calls this, so tests work unchanged on any backend)."""
        from .net import Net
        return Net(self)

    def make_chaos(self):
        raise NotImplementedError(
            "chaos.apply drives Chaos Mesh, which exists only on the "
            "Kubernetes backend — locally use the built-in faults "
            "(net.partition/slow, cluster.kill/throttle/...)")

    def nodes(self) -> list[Node]:
        return list(self._nodes)

    def wait_healthy(self, timeout: float = 60):
        """Poll every node's GET /health until all answer 200, or raise with
        the failing nodes' recent logs."""
        deadline = time.monotonic() + timeout
        pending = {n.index: n for n in self._nodes}
        while pending and time.monotonic() < deadline:
            for i, node in list(pending.items()):
                try:
                    r = requests.get(f"{node.url}/health", timeout=2)
                    if r.status_code == 200:
                        del pending[i]
                except requests.RequestException:
                    pass
            if pending:
                time.sleep(0.5)
        if pending:
            detail = "\n\n".join(
                f"--- {n.name} last logs ---\n{n.logs()}" for n in pending.values()
            )
            raise TimeoutError(
                f"nodes not healthy after {timeout}s: "
                f"{sorted(pending)}\n{detail}"
            )

    # -- process faults -------------------------------------------------------

    # Each is recorded as a requested/done event pair because the action takes
    # real time (docker kill can take ~1s); operations that ran in that window
    # can't be attributed to either side of the fault.

    def kill(self, i: int):
        self._fault(i, "kill", "docker", "kill", self.node(i).name)

    def stop(self, i: int):
        self._fault(i, "stop", "docker", "stop", "-t", "5", self.node(i).name)

    def start(self, i: int):
        self._fault(i, "start", "docker", "start", self.node(i).name)

    def pause(self, i: int):
        self._fault(i, "pause", "docker", "pause", self.node(i).name)

    def unpause(self, i: int):
        self._fault(i, "unpause", "docker", "unpause", self.node(i).name)

    def throttle(self, i: int, *, cpus: float):
        """Make node i a compute straggler: change its CPU quota at runtime
        via `docker update --cpus`. Where Net.slow() makes a node's *network*
        slow, this makes the *machine* slow: it computes slowly but talks
        fine, the kind of slow node a MapReduce or distributed-training
        project has to handle. Verified by reading the applied quota back
        from docker inspect rather than by timing, which is unreliable on a
        loaded machine. A fresh up() recreates containers at the configured
        cpus, so a leftover throttle never survives into the next run."""
        if cpus <= 0:
            raise ValueError(f"throttle needs cpus > 0, got {cpus} "
                             "(to halt a node entirely, use pause or stop)")
        self._set_cpus(i, "throttle", cpus)

    def unthrottle(self, i: int):
        """Restore node i to the cluster's configured cpus, verified the same
        way throttle() is."""
        self._set_cpus(i, "unthrottle", self.cpus)

    def _set_cpus(self, i: int, action: str, cpus: float):
        name = self.node(i).name
        self.fault_log.record(action, node=i, cpus=cpus)
        sh("docker", "update", f"--cpus={cpus}", name)
        applied = self._applied_cpus(name)
        if abs(applied - cpus) > 1e-6:
            raise RuntimeError(
                f"{action}({i}) did not take: asked Docker for {cpus} cpus, "
                f"inspect reports {applied}")
        self.fault_log.record(f"{action}_done", node=i, cpus=cpus)

    def _applied_cpus(self, name: str) -> float:
        # docker update --cpus lands in NanoCpus on current Docker; older
        # engines express the same quota as CpuQuota/CpuPeriod. Read both.
        out = sh("docker", "inspect", "-f",
                 "{{.HostConfig.NanoCpus}} {{.HostConfig.CpuQuota}} "
                 "{{.HostConfig.CpuPeriod}}", name).stdout.split()
        nano_cpus, quota, period = (int(x) for x in out)
        if nano_cpus:
            return nano_cpus / 1e9
        if quota > 0 and period > 0:
            return quota / period
        return 0.0  # no quota set at all: unlimited

    def _fault(self, i: int, action: str, *argv: str):
        self.fault_log.record(action, node=i)
        sh(*argv)
        self.fault_log.record(f"{action}_done", node=i)
        if action in ("start", "unpause"):
            # A returning node may need network faults re-applied (Net
            # subscribes here; the rules died with the old container netns).
            for callback in self._observers:
                callback(i)

    def on_state_change(self, callback):
        self._observers.append(callback)

    # -- internals ------------------------------------------------------------

    def _build_image(self):
        tag = f"{self.prefix}-img"
        if self.dockerfile:
            context = self.dockerfile if self.dockerfile.is_dir() else self.dockerfile.parent
            sh("docker", "build", "-t", tag, str(context), timeout=600)
        else:
            generated = self.workdir / "Dockerfile"
            generated.write_text(
                f"FROM {self.image}\nRUN {FAULT_TOOLS_INSTALL}\n"
            )
            self.workdir.mkdir(parents=True, exist_ok=True)
            sh("docker", "build", "-t", tag, "-f", str(generated), str(self.workdir),
               timeout=600)
        self._image_tag = tag

    def _peers(self) -> str:
        # Each node is resolvable as "node-<i>" on the cluster network (and only
        # there). PEERS lists every node including self, in index order.
        return ",".join(f"node-{i}:{self.peer_port}" for i in range(1, self.n_nodes + 1))

    def _compose_config(self) -> dict:
        services = {}
        for i in range(1, self.n_nodes + 1):
            service = {
                "container_name": f"{self.prefix}-node{i}",
                "image": self._image_tag,
                "environment": {
                    "NODE_ID": str(i),
                    "N_NODES": str(self.n_nodes),
                    "PEERS": self._peers(),
                    **self.env,
                },
                "ports": [f"{self._host_ports[i]}:{self.port}"],
                "cap_add": ["NET_ADMIN"],
                "cpus": self.cpus,
                "mem_limit": self.memory,
                "stop_grace_period": "5s",
                "tmpfs": ["/data"],
                "networks": {
                    # Higher priority -> primary interface -> port mappings and
                    # harness traffic use the client network, never the faulted one.
                    "client": {"priority": 100},
                    "cluster": {"priority": 0, "aliases": [f"node-{i}"]},
                },
            }
            if self.cmd:
                service["command"] = shlex.split(self.cmd)
                service["working_dir"] = "/app"
                service["volumes"] = [f"{self.src}:/app:ro"]
            # Shared read-only dataset(s) (a log corpus, MNIST, ...); /data
            # stays the per-node writable scratch tmpfs.
            for mount_path, host_dir in self.data.items():
                service.setdefault("volumes", []).append(
                    f"{host_dir}:{mount_path}:ro")
            services[f"node{i}"] = service
        return {
            "name": self.prefix,
            "services": services,
            "networks": {
                "client": {"name": f"{self.prefix}-client"},
                "cluster": {"name": f"{self.prefix}-cluster"},
            },
        }
