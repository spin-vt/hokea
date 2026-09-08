"""Net: verified network fault injection on the cluster network.

    net = Net(cluster)
    net.partition([[1, 2, 3], [4, 5]])   # groups talk within, not across
    net.isolate(4)                        # shorthand: one node vs. everyone
    net.slow(3, delay_ms=200, jitter_ms=50, loss_pct=5)
    net.heal()                            # remove every fault hokea added

Mechanism: iptables DROP rules matching cluster-network address pairs, plus
`tc netem` on the cluster-network interface for latency/loss. Two backends:

- host: rules in the host's DOCKER-USER chain. Used where the host has
  root and a real Docker daemon; less exercised than the container backend
  (Docker Desktop cannot run it), so it announces itself when active.
- container: rules installed inside the containers via docker exec. Needs
  iptables in the image and NET_ADMIN (hokea adds the capability; generated
  images get iptables installed). Checked at startup with a clear error.

Rules only ever match specific peer cluster-IP pairs — never the gateway or a
subnet — so the harness can always observe every node through the client
network. Rules go on *both* ends of each pair: a paused or crashed container
can't run iptables, but its counterparts still drop the traffic. Every rule
carries a comment tag so heal() and the startup sweep remove exactly hokea's
rules and nothing else.

Faults are verified, not assumed: after installing a partition, probes prove
traffic is blocked across the cut in both directions and alive within groups
and from the harness — and raise if not. The controller remembers intended
state; if a node restarts while a fault is active, rules are re-applied at
the node's possibly-new address and re-verified (Cluster notifies Net on
start/unpause). Every action lands in faults.jsonl as a requested/verified
event pair.

netem is always applied in-container on the cluster-network interface (there
is no "host tc" for a container's interface), so latency never touches the
path tests measure on.
"""

import shlex
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

import requests

from .proc import CommandFailed, sh

TAG = "hokea-fault"
COMMENT = ["-m", "comment", "--comment", TAG]

# docker exec stderr meaning "this container can't run commands right now" —
# tolerated with a warning (a dead node's traffic is still dropped by every
# counterpart's rules); anything else raises, because a partition that
# silently did not happen would invalidate the whole run.
CONTAINER_GONE = ("is not running", "is paused", "no such container",
                  "is restarting")

PROBE_PY = """
import socket, sys, time
s = socket.socket(); s.settimeout(2.0)
t = time.monotonic()
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
    print(f"reachable {(time.monotonic()-t)*1000:.1f}")
except ConnectionRefusedError:
    print(f"reachable {(time.monotonic()-t)*1000:.1f}")  # RST = host answered
except OSError:
    print("blocked")
"""


class Net:
    def __init__(self, cluster, backend: str = "auto"):
        self.cluster = cluster
        self.log = cluster.fault_log
        self._groups: list[list[int]] | None = None
        self._slow: dict[int, tuple] = {}
        self._dirty: set[int] = set()  # nodes we couldn't sweep yet

        host_prefix = _detect_host_prefix() if backend in ("auto", "host") else None
        if backend == "host" and host_prefix is None:
            raise RuntimeError("host backend requested but the DOCKER-USER "
                               "chain is not accessible (need root or "
                               "passwordless sudo on the Docker host)")
        if backend in ("auto", "host") and host_prefix is not None:
            self.backend = _HostBackend(host_prefix)
            print("hokea: network fault backend: host (DOCKER-USER). This "
                  "backend is less exercised than the in-container one; if "
                  "a fault does not take effect, report it.",
                  flush=True)
        else:
            self.backend = _ContainerBackend(cluster)
            print("hokea: network fault backend: in-container iptables", flush=True)

        self._probe_via = self._detect_probe_tool()
        self._sweep_everything()  # remove leftovers from any crashed earlier run
        cluster.on_state_change(self._on_node_change)

    # -- public API -----------------------------------------------------------

    def partition(self, groups: list[list[int]]):
        """Cut the cluster network between groups. Groups must cover every
        node exactly once. Raises unless probes prove the cut is real."""
        flat = sorted(i for g in groups for i in g)
        if flat != list(range(1, self.cluster.n_nodes + 1)):
            raise ValueError(f"groups {groups} must cover nodes "
                             f"1..{self.cluster.n_nodes} exactly once")
        if self._groups is not None:
            raise RuntimeError("a partition is already active; heal() first")
        self.log.record("partition", groups=groups)
        self._groups = groups
        self._install_partition()
        skipped = self._verify_partition()
        self.log.record("partition_verified", groups=groups,
                        skipped_nodes=sorted(skipped))

    def isolate(self, i: int):
        rest = [j for j in range(1, self.cluster.n_nodes + 1) if j != i]
        self.partition([[i], rest])

    def slow(self, i: int, *, delay_ms: int, jitter_ms: int = 0,
             loss_pct: float = 0):
        """Shape node i's egress on the cluster network with netem."""
        self.log.record("slow", node=i, delay_ms=delay_ms,
                        jitter_ms=jitter_ms, loss_pct=loss_pct)
        self._slow[i] = (delay_ms, jitter_ms, loss_pct)
        self._apply_netem(i)
        rtt = self._verify_netem(i)
        self.log.record("slow_verified", node=i, probe_rtt_ms=rtt)

    def heal(self):
        """Remove every fault this framework added, and prove recovery."""
        self.log.record("heal")
        was_cross = self._cross_pairs() if self._groups else []
        self._groups = None
        self._slow = {}
        self._sweep_everything()
        if self._dirty:
            print(f"hokea: WARNING: nodes {sorted(self._dirty)} could not be "
                  "cleaned (paused or stopped); their rules will be removed "
                  "when they come back", flush=True)
        for a, b in was_cross:
            if self._runnable(a) and self._runnable(b) and b not in self._dirty:
                if self._probe(a, b) != "reachable":
                    raise RuntimeError(
                        f"heal failed: node {a} still cannot reach node {b}")
        self.log.record("heal_done", dirty_nodes=sorted(self._dirty))

    # -- reconciliation -------------------------------------------------------

    def _on_node_change(self, i: int):
        """Called by Cluster after start/unpause. Re-establish desired state:
        a restarted node lost its rules and may have a new address; an
        unpaused node may hold rules heal() couldn't remove."""
        if self._groups is None and not self._slow and not self._dirty:
            return
        print(f"hokea: node {i} state changed; re-applying network faults",
              flush=True)
        self._sweep_everything()
        if self._groups is not None:
            self._install_partition()
            skipped = self._verify_partition()
            self.log.record("partition_verified", groups=self._groups,
                            skipped_nodes=sorted(skipped), reapplied_after=i)
        for j in self._slow:
            self._apply_netem(j)

    # -- install / sweep ------------------------------------------------------

    def _cross_pairs(self) -> list[tuple[int, int]]:
        pairs = []
        for ga, gb in combinations(self._groups, 2):
            pairs.extend((a, b) for a in ga for b in gb)
        return pairs

    def _install_partition(self):
        ips = {}
        for node in self.cluster.nodes():
            if self._runnable(node.index):
                ips[node.index] = node.cluster_ip()
        for a, b in self._cross_pairs():
            if a in ips and b in ips:
                self.backend.block_pair(a, ips[a], b, ips[b])
            else:
                print(f"hokea: cannot install rules for pair ({a},{b}) — a "
                      "node is down; will re-apply when it returns", flush=True)

    def _sweep_everything(self):
        self.backend.sweep()
        for node in self.cluster.nodes():
            if node.status() != "running":
                self._dirty.add(node.index)  # cleaned when it comes back
                continue
            try:
                self.backend.sweep_node(node)
                self._clear_netem(node.index)
                self._dirty.discard(node.index)
            except CommandFailed as e:
                if any(m in e.stderr for m in CONTAINER_GONE):
                    self._dirty.add(node.index)
                else:
                    raise

    # -- verification ---------------------------------------------------------

    def _verify_partition(self) -> set[int]:
        """Prove the cut: blocked across groups (both directions), reachable
        within groups, every running node still visible to the harness."""
        skipped = {i for i in range(1, self.cluster.n_nodes + 1)
                   if not self._runnable(i)}
        checks = []  # (from, to, expected)
        for a, b in self._cross_pairs():
            checks.append((a, b, "blocked"))
            checks.append((b, a, "blocked"))
        for group in self._groups:
            for a, b in combinations(group, 2):
                checks.append((a, b, "reachable"))
        checks = [c for c in checks if c[0] not in skipped and c[1] not in skipped]

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda c: self._probe(c[0], c[1]), checks))
        for (a, b, expected), actual in zip(checks, results):
            if actual != expected:
                raise RuntimeError(
                    f"partition verification failed: node {a} -> node {b} is "
                    f"{actual}, expected {expected}. The fault is not in the "
                    "state the test believes; results would be meaningless.")

        for node in self.cluster.nodes():
            if node.index in skipped:
                continue
            try:
                requests.get(f"{node.url}/health", timeout=3)
            except requests.RequestException as e:
                raise RuntimeError(
                    f"harness lost contact with node {node.index} during a "
                    f"partition ({type(e).__name__}). The client network is "
                    "never faulted; something else is wrong.") from e
        return skipped

    def _detect_probe_tool(self) -> str:
        node = self.cluster.node(1)
        if node.exec("ping", "-c", "1", "-W", "1", "127.0.0.1", check=False).returncode == 0:
            return "ping"
        if node.exec("python3", "-c", "pass", check=False).returncode == 0:
            return "python3"
        raise RuntimeError(
            "fault verification needs `ping` or `python3` inside the "
            "containers; add one to the image (hokea-generated images "
            "include ping)")

    def _probe(self, i: int, j: int) -> str:
        """Can node i reach node j on the cluster network? -> reachable|blocked"""
        ip = self.cluster.node(j).cluster_ip()
        if self._probe_via == "ping":
            r = self.cluster.node(i).exec("ping", "-c", "1", "-W", "2", ip,
                                          check=False)
            return "reachable" if r.returncode == 0 else "blocked"
        r = self.cluster.node(i).exec("python3", "-c", PROBE_PY, ip,
                                      str(self.cluster.peer_port), check=False)
        return r.stdout.split()[0] if r.stdout else "blocked"

    def _probe_rtt_ms(self, i: int, j: int) -> float | None:
        ip = self.cluster.node(j).cluster_ip()
        if self._probe_via == "ping":
            r = self.cluster.node(i).exec("ping", "-c", "1", "-W", "4", ip,
                                          check=False)
            for token in r.stdout.split():
                if token.startswith("time="):
                    return float(token[5:])
            return None
        r = self.cluster.node(i).exec("python3", "-c", PROBE_PY, ip,
                                      str(self.cluster.peer_port), check=False)
        parts = r.stdout.split()
        return float(parts[1]) if len(parts) == 2 else None

    # -- netem ----------------------------------------------------------------

    def _cluster_iface(self, i: int) -> str:
        node = self.cluster.node(i)
        ip = node.cluster_ip()
        out = node.exec("ip", "-o", "-4", "addr", "show").stdout
        for line in out.splitlines():
            fields = line.split()
            if fields[3].split("/")[0] == ip:
                return fields[1]
        raise RuntimeError(f"could not find the interface holding {ip} on "
                           f"node {i}:\n{out}")

    def _apply_netem(self, i: int):
        delay_ms, jitter_ms, loss_pct = self._slow[i]
        iface = self._cluster_iface(i)
        args = ["delay", f"{delay_ms}ms"]
        if jitter_ms:
            args.append(f"{jitter_ms}ms")
        if loss_pct:
            args += ["loss", f"{loss_pct:g}%"]
        self.cluster.node(i).exec("tc", "qdisc", "replace", "dev", iface,
                                  "root", "netem", *args)

    def _verify_netem(self, i: int) -> float | None:
        iface = self._cluster_iface(i)
        out = self.cluster.node(i).exec("tc", "qdisc", "show", "dev", iface).stdout
        if "netem" not in out:
            raise RuntimeError(f"netem did not take effect on node {i} "
                               f"({iface}): {out.strip()}")
        # Measure the effect from a peer that can still talk to node i, and
        # record it (loaded machines make exact timing assertions unreliable).
        for j in self._same_side_peers(i):
            if self._runnable(j):
                return self._probe_rtt_ms(j, i)
        return None

    def _same_side_peers(self, i: int) -> list[int]:
        if self._groups is None:
            return [j for j in range(1, self.cluster.n_nodes + 1) if j != i]
        group = next(g for g in self._groups if i in g)
        return [j for j in group if j != i]

    def _clear_netem(self, i: int):
        iface = self._cluster_iface(i)
        node = self.cluster.node(i)
        out = node.exec("tc", "qdisc", "show", "dev", iface).stdout
        if "netem" in out:  # never yank a default qdisc that isn't ours
            node.exec("tc", "qdisc", "del", "dev", iface, "root")

    def _runnable(self, i: int) -> bool:
        # "running" exactly: a paused container counts as Running to Docker
        # but cannot exec probes or answer them.
        return self.cluster.node(i).status() == "running"


# ---------------------------------------------------------------------------
# Backends: where the DROP rules live
# ---------------------------------------------------------------------------

class _ContainerBackend:
    """Rules inside each container via docker exec. The iptables binary is
    probed per node (images may ship nft-backed iptables that cannot operate
    in a container while iptables-legacy can)."""

    def __init__(self, cluster):
        self.cluster = cluster
        self._bin: dict[int, str] = {}
        for node in cluster.nodes():
            for binary in ("iptables", "iptables-legacy"):
                if node.exec(binary, "-S", "INPUT", check=False).returncode == 0:
                    self._bin[node.index] = binary
                    break
            else:
                raise RuntimeError(
                    f"node {node.index}: cannot run iptables inside the "
                    "container. The in-container fault backend needs the "
                    "image to include iptables and the container to run with "
                    "NET_ADMIN (hokea adds the capability; your image must "
                    "provide the tool).")

    def block_pair(self, a: int, ip_a: str, b: int, ip_b: str):
        # Symmetric on purpose: if one container is unreachable, the other
        # side's rules still enforce the cut.
        self._add_rules(a, ip_b)
        self._add_rules(b, ip_a)

    def _add_rules(self, i: int, peer_ip: str):
        node = self.cluster.node(i)
        binary = self._bin[i]
        try:
            node.exec(binary, "-I", "INPUT", "1", "-s", peer_ip, *COMMENT, "-j", "DROP")
            node.exec(binary, "-I", "OUTPUT", "1", "-d", peer_ip, *COMMENT, "-j", "DROP")
        except CommandFailed as e:
            if any(m in e.stderr for m in CONTAINER_GONE):
                print(f"hokea: node {i} is down; relying on peer-side rules",
                      flush=True)
            else:
                raise

    def sweep(self):
        pass  # nothing global; per-node sweeps do the work

    def sweep_node(self, node):
        binary = self._bin[node.index]
        for chain in ("INPUT", "OUTPUT"):
            listing = node.exec(binary, "-S", chain).stdout
            for line in listing.splitlines():
                if TAG not in line:
                    continue
                # -S quotes the comment; shlex handles it. Flip -A to -D.
                args = shlex.split(line)
                args[0] = "-D"
                node.exec(binary, *args)


class _HostBackend:
    """Rules in the Docker host's DOCKER-USER chain (survive container
    restarts and pauses). Docker Desktop cannot run it, so it is the
    less-exercised backend."""

    def __init__(self, prefix: list[str]):
        self.prefix = prefix  # [] or ["sudo", "-n"]

    def block_pair(self, a: int, ip_a: str, b: int, ip_b: str):
        for src, dst in ((ip_a, ip_b), (ip_b, ip_a)):
            sh(*self.prefix, "iptables", "-I", "DOCKER-USER", "1",
               "-s", src, "-d", dst, *COMMENT, "-j", "DROP")

    def sweep(self):
        listing = sh(*self.prefix, "iptables", "-S", "DOCKER-USER").stdout
        for line in listing.splitlines():
            if TAG not in line:
                continue
            args = shlex.split(line)
            args[0] = "-D"
            sh(*self.prefix, "iptables", *args)

    def sweep_node(self, node):
        pass  # rules live on the host, not in containers


def _detect_host_prefix() -> list[str] | None:
    for prefix in ([], ["sudo", "-n"]):
        try:
            result = sh(*prefix, "iptables", "-L", "DOCKER-USER", "-n", check=False)
        except FileNotFoundError:
            return None  # no iptables binary on the host at all
        if result.returncode == 0:
            return prefix
    return None
