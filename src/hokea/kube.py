"""Kubernetes backend: the fleet as pods, faults via Chaos Mesh.

Same surface as the Docker backend — the same tests, adapters, ops
generators, and checks run unchanged; what changes is one conftest fixture:

    from hokea.kube import KubeCluster

    @pytest.fixture
    def hokea_cluster(tmp_path):
        return KubeCluster(image="python:3.12-slim", src=HERE,
                           cmd="python server.py", nodes=3, name="myproj",
                           namespace="team-NN", port=8000,
                           workdir=tmp_path / "cluster")

How the Docker backend's concepts map:

- N containers -> N bare pods named `{prefix}-node<i>`, labeled
  hokea/cluster={prefix} and `hokea/node=<i>`. restartPolicy=Never: hokea's
  kill()/start() are the only ways a node dies and returns, so a run's fault
  timeline stays the whole truth.
- no-Dockerfile mode -> no-registry mode: the source directory is shipped as
  a ConfigMap mounted read-only at /app on a public base image. There is
  nothing to build or push. (Limits: regular files in the top level of src=,
  ~1MiB total. A system that outgrows this builds an image, pushes it to any
  registry the cluster can pull from, and passes image= alone.)
- the PEERS contract is preserved exactly: each pod gets hostname `node-<i>`,
  subdomain {prefix}-peers (a headless Service), and a dnsConfig search entry
  so the bare name `node-<i>` resolves — your system connects to
  node-2:8000 identically under both backends.
- the client/cluster network split -> two mechanisms, layered. Selector
  scoping first: every Chaos Mesh CR hokea creates selects only the fleet's
  pods as source AND target, so pod-to-pod traffic is faulted while
  harness traffic never matches a rule. Namespaces second, when the
  harness itself runs inside the cluster: server pods live in the team
  namespace (annotated chaos-mesh.org/inject=enabled) and the harness
  runs as a pod in a separate CLIENT namespace that deliberately lacks
  that annotation — with Chaos Mesh's namespace filter on, faults
  structurally cannot reach the observer, which is the same guarantee the
  Docker backend gets from its never-faulted client network. up() checks
  both annotations when RBAC allows (see _preflight_internal).
- how the harness reaches nodes -> `expose` modes. expose="nodeport"
  publishes one NodePort Service per pod, for a harness OUTSIDE the
  cluster that can route to the k8s nodes. expose="internal" publishes
  nothing: node.url is the pod's per-pod DNS record behind the headless
  peers Service, written as an FQDN so it resolves cross-namespace from
  the client namespace. The default auto-detects — internal iff this
  process is itself running in a pod.
- network faults -> NetworkChaos CRs. Chaos Mesh experiments are
  level-triggered: a CR keeps its fault installed until the CR is deleted,
  and re-applying a finished object does nothing. hokea therefore creates a
  FRESH CR (generateName) for every fault action and heals by deleting its
  CRs — it never re-applies one.
- verification is still hokea's own: after Chaos Mesh reports AllInjected,
  hokea probes pod-to-pod reachability through kubectl exec and raises if
  the fault is not in the state the test believes.
- process faults: kill(i) force-deletes the pod; stop(i) deletes it with a
  grace period; start(i) re-creates it from the saved manifest (fresh pod
  IP; peers re-resolve DNS, active network faults are re-established via the
  same state-change hook the Docker backend uses). throttle(i) shrinks the
  pod's CPU limit in place via the resize subresource (k8s >= 1.33) — no
  restart, process state survives. pause is NOT implemented here — see the
  class docstring for what to use instead.
- faults hokea did NOT inject are recorded too: a watcher thread polls the
  namespace's Chaos Mesh CRs and logs foreign experiments (a kubectl- or
  dashboard-applied StressChaos, IOChaos, TimeChaos, ...) into the fault
  timeline as chaos/chaos_done/chaos_recovered events, so faults.jsonl
  stays the record of the run even when you drive Chaos Mesh by hand.
- your own chaos -> chaos.apply: the `chaos` fixture (KubeChaos)
  takes any Chaos Mesh experiment or Workflow YAML, rewrites it to survive
  level-triggering (fresh generateName per apply), creates it inside the
  fault schedule, waits for injection, and clear() deletes it — so a chaos
  experiment of your own design is one step in a pytest test, correctly
  timed, recorded, and cleaned up.

Everything hokea creates is namespaced, labeled
app.kubernetes.io/managed-by=hokea + hokea/cluster={prefix}, and removed by
down() with a label selector — teardown of a whole namespace is never
hokea's job.

Requires: kubectl on PATH configured for the target cluster, a namespace you
may create pods in, and Chaos Mesh installed cluster-wide.
"""

import base64
import copy
import json
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import requests
import yaml

from .cluster import dataset_mounts
from .events import EventCollector
from .net import PROBE_PY
from .proc import CommandFailed, sh
from .runs import FaultLog

MANAGED_BY = "hokea"

# Mounted into every pod by Kubernetes: the namespace this process's own
# ServiceAccount lives in. Its presence is also the standard "am I inside a
# pod?" tell. A module-level Path so tests can point it at a temp file.
SERVICEACCOUNT_NAMESPACE_FILE = Path(
    "/var/run/secrets/kubernetes.io/serviceaccount/namespace")

# Namespaces opt IN to Chaos Mesh with this annotation (=enabled) when the
# controller runs with ENABLE_FILTER_NAMESPACE=true — the class-cluster
# setup. The client (harness) namespace deliberately lacks it; that absence
# is the structural guarantee that faults cannot hit the observer.
CHAOS_INJECT_ANNOTATION = "chaos-mesh.org/inject"

# How long start() waits, after the pod is Ready, for the node to actually
# answer /health at its stable URL (fresh pod IP -> the DNS record can lag).
START_SERVE_TIMEOUT = 60.0


def cluster_prefix(name: str) -> str:
    """The name prefix on everything a cluster creates (pods, services,
    labels). One rule, shared with the CLI's status/label lookups."""
    return f"hokea-{name}"


def _running_in_pod() -> bool:
    """The expose= auto-detect: KUBERNETES_SERVICE_HOST is injected into
    every container Kubernetes runs, and the serviceaccount mount is there
    even when the environment was scrubbed."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) or \
        SERVICEACCOUNT_NAMESPACE_FILE.exists()


def _own_namespace() -> str | None:
    """The namespace THIS process runs in, from the serviceaccount mount —
    None when not running in a pod."""
    try:
        text = SERVICEACCOUNT_NAMESPACE_FILE.read_text().strip()
    except OSError:
        return None
    return text or None


def _selects_namespace(obj, namespace: str) -> bool:
    """Does any Chaos Mesh selector anywhere in this document — top level,
    a NetworkChaos target, a Workflow template; the walk is recursive —
    name `namespace` explicitly? Both spellings count: a `namespaces` list
    entry, and a `pods` map whose keys are namespace names (Chaos Mesh's
    per-pod selector form). Pure function, used by KubeChaos.apply's
    protected-observer guard."""
    if isinstance(obj, dict):
        selector = obj.get("selector")
        if isinstance(selector, dict):
            if namespace in (selector.get("namespaces") or []):
                return True
            pods = selector.get("pods")
            if isinstance(pods, dict) and namespace in pods:
                return True
        return any(_selects_namespace(v, namespace) for v in obj.values())
    if isinstance(obj, list):
        return any(_selects_namespace(v, namespace) for v in obj)
    return False


class ClusterExists(RuntimeError):
    """A cluster with this name already has running pods (see
    _guard_existing). Subclasses RuntimeError so existing callers that
    catch broadly keep working; the CLI catches it to print a plain hint."""


class KubectlNotFound(RuntimeError):
    """kubectl is not installed (or not on PATH)."""


def _kubectl_sh(*argv: str, check: bool = True, timeout: float = 120):
    try:
        return sh("kubectl", *argv, check=check, timeout=timeout)
    except FileNotFoundError:
        raise KubectlNotFound(
            "kubectl not found on PATH — the Kubernetes backend needs "
            "kubectl installed and configured (see the introductory lab, "
            "docs/lab.md, Part 0)"
        ) from None


class KubeNode:
    """One pod. `index` is 1-based, matching NODE_ID. `name` is the pod name;
    the pod's hostname is `node-<index>` (what PEERS entries resolve to)."""

    def __init__(self, cluster: "KubeCluster", index: int):
        self.cluster = cluster
        self.index = index
        self.name = f"{cluster.prefix}-node{index}"

    @property
    def url(self) -> str:
        """Base URL for reaching this node from the harness. Stable for the
        cluster's lifetime in either expose mode:

        - nodeport: http://<node_host>:<nodePort>. The Service selects by
          label, so a killed-and-restarted pod keeps the same URL.
        - internal: the pod's per-pod DNS record behind the headless peers
          Service, as an FQDN so it resolves from the harness's own
          (client) namespace. The NAME is stable; while the pod is down
          the record does not resolve, which the harness reads as
          "unreachable" exactly like a refused NodePort connection.
        """
        if self.cluster.expose == "internal":
            return (f"http://node-{self.index}.{self.cluster.prefix}-peers."
                    f"{self.cluster.namespace}.svc.cluster.local:"
                    f"{self.cluster.port}")
        return f"http://{self.cluster.node_host}:{self.host_port()}"

    def host_port(self) -> int:
        """The NodePort publishing this node — the k8s analog of the Docker
        backend's per-node host port. nodeport expose mode only."""
        if self.cluster.expose == "internal":
            raise NotImplementedError(
                "host_port() needs a NodePort Service and this cluster "
                "runs with expose='internal' (none are published). To "
                f"reach node {self.index} from outside the cluster, "
                f"port-forward instead: kubectl -n {self.cluster.namespace} "
                f"port-forward pod/{self.name} "
                f"{self.cluster.port}:{self.cluster.port} — the forward "
                "stays open in that terminal, and the node answers at "
                f"http://localhost:{self.cluster.port}")
        return self.cluster._node_ports[self.index]

    def cluster_ip(self) -> str:
        """The pod IP (changes when the pod is re-created)."""
        return self.cluster._kubectl(
            "get", "pod", self.name, "-o", "jsonpath={.status.podIP}").stdout

    def exec(self, *argv: str, check: bool = True):
        return self.cluster._kubectl("exec", self.name, "--", *argv, check=check)

    def logs(self, tail: int = 50) -> str:
        result = self.cluster._kubectl("logs", f"--tail={tail}", self.name,
                                       check=False)
        return result.stdout + result.stderr

    def is_running(self) -> bool:
        return self.status() == "running"

    def status(self) -> str:
        """"running", "pending", "failed", "succeeded", or "gone" — the pod
        phase, lowercased. NOT identical to the Docker backend's vocabulary:
        a killed node is "gone" here (the pod object is deleted) where
        Docker reports "exited", and "paused" cannot occur."""
        result = self.cluster._kubectl(
            "get", "pod", self.name, "-o", "jsonpath={.status.phase}",
            check=False)
        return result.stdout.strip().lower() or "gone"


class KubeCluster:
    """
        with KubeCluster(image="python:3.12-slim", src="./myserver",
                         cmd="python server.py", nodes=5,
                         namespace="team-NN") as c:
            c.wait_healthy()
            ...

    Either image+src+cmd (no-registry mode: src becomes a ConfigMap mounted
    at /app) or image= alone (a pre-built image from a registry the cluster
    can pull; its own entrypoint runs). `namespace` must exist and allow you
    to create pods, services, and configmaps.

    `expose` picks how the harness reaches nodes. "nodeport": one NodePort
    Service per node — for a harness OUTSIDE the cluster that can route to
    the k8s nodes. "internal": no Services are published at all; node.url
    is the pod's own DNS record (node-<i>.<prefix>-peers.<ns>.svc...) —
    for a harness running INSIDE the cluster as a pod, typically in a
    separate client namespace (`team-NN-clients` next to `team-NN`). The
    default, None, auto-detects: internal iff this process itself runs in
    a pod. The client namespace is this backend's version of the Docker
    backend's client network — it must NOT carry the
    chaos-mesh.org/inject=enabled annotation, so Chaos Mesh structurally
    cannot fault the observer; up() verifies both namespaces' annotations
    when RBAC allows (_preflight_internal), and chaos.apply refuses
    experiments whose selectors name the harness's namespace. Inside a
    pod, kubectl needs no kubeconfig: the mounted ServiceAccount
    authenticates it.

    `node_host` (nodeport mode only) is the address the harness reaches
    NodePorts at; it defaults to the first cluster node's InternalIP,
    which is right when the harness runs somewhere that can route to the
    nodes (a lab machine on the same network). Set it explicitly otherwise.

    `data=` mounts a dataset read-only in every node, at the same paths as
    the Docker backend: a single source appears at /dataset, a dict
    {name: source} at `/dataset/<name>` each. The difference is what a
    source IS: Docker takes a host directory; here it is an OCI **image
    reference**, mounted as a pod image volume (k8s >= 1.35). A dataset
    image is just a filesystem shipped as an image — a two-line Dockerfile:

        FROM scratch
        COPY corpus/ /

    built and tagged like any image. The pull policy is IfNotPresent, so a
    ref already in the node's containerd store works with NO registry at
    all (side-loaded or left over from an earlier pull), which keeps the
    default packaging free of any registry. containerd also dedupes
    by ref: one on-disk copy per cluster node no matter how many pods or
    teams mount it. Parity caveat: Docker's data= is a live bind mount of
    a host directory, so edits show up immediately; an image volume is
    immutable — to change the dataset, push a new tag and pass that.

    Not implemented in this backend (loud NotImplementedError, with the
    Chaos Mesh alternative named): pause/unpause — Kubernetes has no
    SIGSTOP analog for a pod (PodChaos pod-failure replaces the container,
    losing process state, so it models a different fault).
    """

    def __init__(self, *, image: str, src: str | None = None,
                 cmd: str | None = None, nodes: int = 3,
                 name: str = "default", namespace: str,
                 port: int = 8000, peer_port: int | None = None,
                 env: dict[str, str] | None = None,
                 cpus: float = 1.0, memory: str = "512Mi",
                 expose: str | None = None, node_host: str | None = None,
                 workdir: str | Path | None = None, up_timeout: float = 300,
                 takeover: bool = False, data: str | dict | None = None):
        if (src is None) != (cmd is None):
            raise ValueError("provide src= and cmd= together (no-registry "
                             "mode) or neither (pre-built image)")
        self.data = dataset_mounts(data)
        for mount_path, ref in self.data.items():
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError(
                    f"data= source for {mount_path} must be an OCI image "
                    f"reference on the Kubernetes backend (the Docker "
                    f"backend takes directories), got {ref!r}")
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
            raise ValueError(
                f"name={name!r}: on Kubernetes the cluster name becomes pod "
                "and service names, so it must be DNS-1123 (lowercase "
                "letters, digits, '-')")
        self.image = image
        self.src = Path(src).resolve() if src else None
        self.cmd = cmd
        self.n_nodes = nodes
        self.name = name
        self.prefix = cluster_prefix(name)
        self.namespace = namespace
        self.port = port
        self.peer_port = peer_port if peer_port is not None else port
        self.env = env or {}
        self.cpus = cpus
        self.memory = memory
        if expose not in (None, "nodeport", "internal"):
            raise ValueError(
                f"expose={expose!r}: pick 'nodeport' (per-node NodePort "
                "Services, for a harness outside the cluster), 'internal' "
                "(no published Services, pod DNS — for a harness running "
                "as a pod), or leave it None to auto-detect")
        self.expose = expose or ("internal" if _running_in_pod()
                                 else "nodeport")
        # Where the harness itself lives (None outside a pod). Read
        # automatically — students never pass it — and used by the
        # internal-mode preflight and chaos.apply's observer guard.
        self.harness_namespace = _own_namespace()
        self.node_host = node_host
        # takeover also arrives as an env var: a student driving `hokea
        # test` never touches this constructor (their conftest doesn't
        # pass takeover=), so the launcher forwards HOKEA_TAKEOVER into
        # the runner Job and it lands here.
        self.takeover = takeover or bool(os.environ.get("HOKEA_TAKEOVER"))
        self.up_timeout = up_timeout
        self.workdir = Path(workdir) if workdir else Path(".hokea") / name
        self._nodes = [KubeNode(self, i) for i in range(1, nodes + 1)]
        self._node_ports: dict[int, int] = {}
        self._pod_manifests: dict[int, dict] = {}  # for start() re-creation
        self.fault_log = FaultLog()
        self._observers = []
        self.events: EventCollector | None = None
        self._chaos_watcher: "ChaosWatcher | None" = None

    # -- lifecycle ------------------------------------------------------------

    def up(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        if self.expose == "internal":
            self._preflight_internal()
        self._guard_existing()
        self._sweep_labeled()  # leftovers from a crashed earlier run
        manifests = self._build_manifests()
        path = self.workdir / "manifests.yaml"
        path.write_text(yaml.safe_dump_all(manifests, sort_keys=False))
        try:
            # Server-side: client-side apply would stash the whole source
            # ConfigMap in a last-applied annotation (256 KiB cap).
            self._kubectl("apply", "--server-side", "-f", str(path))
            self._wait_pods_ready(self.up_timeout)
            if self.expose == "nodeport":
                if self.node_host is None:
                    self.node_host = self._detect_node_host()
                for i in range(1, self.n_nodes + 1):
                    self._node_ports[i] = int(self._kubectl(
                        "get", "svc", f"{self.prefix}-node{i}",
                        "-o", "jsonpath={.spec.ports[0].nodePort}").stdout)
        except Exception:
            # Show why, then leave nothing behind: an up() that half-happened
            # would otherwise hold pods and NodePorts until someone notices.
            self._print_pod_diagnostics()
            if self.data:
                print("hokea: this cluster may lack image-volume support "
                      "(Kubernetes >= 1.35 with the ImageVolume feature) — "
                      "datasets via data= need it", flush=True)
            self._sweep_labeled()
            raise
        self.events = KubeEventCollector(self, self.workdir / "events")
        self.events.attach_all()
        self._chaos_watcher = ChaosWatcher(self)
        self._chaos_watcher.start()
        return self

    def _build_manifests(self) -> list[dict]:
        """Every object up() creates, in apply order. expose="internal"
        publishes no Services at all: the headless peers Service is not an
        expose mechanism (it has no VIP), it only backs the per-pod DNS
        records that PEERS — and, in internal mode, node.url — resolve
        through."""
        manifests = []
        if self.src is not None:
            manifests.append(self._configmap_manifest())
        manifests.append(self._peers_service_manifest())
        for i in range(1, self.n_nodes + 1):
            if self.expose == "nodeport":
                manifests.append(self._nodeport_service_manifest(i))
            pod = self._pod_manifest(i)
            self._pod_manifests[i] = pod
            manifests.append(pod)
        return manifests

    def _preflight_internal(self):
        """The internal-mode safety invariant, checked before anything is
        created: the harness's own namespace must NOT carry
        chaos-mesh.org/inject=enabled (otherwise the observer sits inside
        the blast radius) and the server namespace MUST carry it
        (otherwise, with Chaos Mesh's namespace filter on, every
        experiment would be silently inert). Skipped when there is no
        serviceaccount namespace file — expose="internal" from outside a
        pod (the CLI's laptop deploys) has no in-cluster observer to
        protect. If RBAC forbids reading namespaces, warn once and
        continue: unverifiable is not the same as violated.

        This is also the run's first contact with the cluster API, so the
        first call gets a short timeout: if the runner can't reach the
        control plane at all, fail fast in plain English instead of letting
        every later kubectl call hang for its full 120s."""
        own = self.harness_namespace
        if own is None:
            return
        try:
            own_enabled = self._chaos_inject_enabled(own, timeout=10)
            server_enabled = self._chaos_inject_enabled(self.namespace)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "the test runner cannot reach the cluster's control plane "
                "from inside your team's client namespace — this is a "
                "cluster configuration problem, not your code; tell your "
                "instructor") from None
        except CommandFailed as e:
            blob = (e.stderr + e.stdout).lower()
            if "forbidden" in blob:
                print("hokea: could not verify chaos namespace annotations "
                      "(RBAC forbids `get` on namespaces) — trusting that "
                      f"faults cannot reach the harness namespace {own!r}",
                      flush=True)
                return
            if "notfound" in blob or "not found" in blob:
                named = re.search(r'namespaces? "([^"]+)"', e.stderr)
                which = named.group(1) if named else self.namespace
                raise RuntimeError(
                    f"namespace {which!r} doesn't exist — check "
                    "--namespace/HOKEA_NAMESPACE (is that really your "
                    "team's namespace?)") from None
            raise RuntimeError(
                "could not check the chaos annotations on namespaces "
                f"{own!r} and {self.namespace!r}: "
                f"{e.stderr.strip() or e}") from e
        if own_enabled:
            raise RuntimeError(
                f"the harness's own namespace {own!r} is annotated "
                f"{CHAOS_INJECT_ANNOTATION}=enabled — Chaos Mesh could "
                "fault the harness itself, putting the observer inside the "
                "blast radius and making every measurement suspect. Run "
                "the harness from a namespace WITHOUT that annotation "
                "(the client namespace).")
        if not server_enabled:
            raise RuntimeError(
                f"the server namespace {self.namespace!r} lacks the "
                f"{CHAOS_INJECT_ANNOTATION}=enabled annotation — with "
                "Chaos Mesh's namespace filter on, every chaos experiment "
                "there would be silently inert: tests would pass while "
                "injecting nothing. Ask whoever runs the cluster to "
                "annotate the namespace before running faults.")

    def _chaos_inject_enabled(self, namespace: str,
                              timeout: float = 120) -> bool:
        """Does this namespace carry chaos-mesh.org/inject=enabled?
        Namespaces are cluster-scoped, so no -n flag."""
        out = _kubectl_sh("get", "namespace", namespace, "-o", "json",
                          timeout=timeout).stdout
        meta = json.loads(out).get("metadata") or {}
        annotations = meta.get("annotations") or {}
        return annotations.get(CHAOS_INJECT_ANNOTATION) == "enabled"

    def _guard_existing(self):
        """A shared namespace makes name collisions possible in a way a
        personal Docker host never was: refuse to sweep pods that are
        actually running (a teammate's live run looks exactly like our own
        crashed leftovers) unless the caller said takeover=True."""
        if self.takeover:
            return
        out = self._kubectl(
            "get", "pods", "-l", f"hokea/cluster={self.prefix}", "-o",
            "jsonpath={range .items[*]}{.metadata.name}={.status.phase} {end}",
            check=False).stdout.strip()
        running = sorted(x.split("=")[0] for x in out.split()
                         if x.endswith("=Running"))
        if running:
            raise ClusterExists(
                f"a cluster named {self.name!r} already has running pods in "
                f"namespace {self.namespace!r}: {running}. If that is a "
                "teammate's live run, pick a different name=. If it is "
                "yours: running through `hokea test`, re-run with the "
                "takeover env var set — `HOKEA_TAKEOVER=1 hokea test ...` — "
                "to replace it; otherwise take it down with `hokea k8s "
                f"down --namespace {self.namespace} --name {self.name}`, "
                "or pass takeover=True (--takeover on the CLI), or sweep "
                f"by hand: kubectl -n {self.namespace} delete "
                "networkchaos,pods,services,configmaps -l "
                f"hokea/cluster={self.prefix} --ignore-not-found")

    def _print_pod_diagnostics(self):
        state = self._kubectl("get", "pods", "-l",
                              f"hokea/cluster={self.prefix}", "-o", "wide",
                              check=False)
        print(f"hokea: cluster failed to come up; pod state:\n{state.stdout}",
              flush=True)
        for node in self._nodes:
            if node.status() != "running":
                print(f"--- {node.name} last logs ---\n{node.logs()}",
                      flush=True)

    def down(self):
        """Delete everything this cluster created — and only that: deletion
        goes by the hokea/cluster label, never by namespace."""
        if self._chaos_watcher is not None:
            self._chaos_watcher.stop()
            self._chaos_watcher = None
        if self.events is not None:
            self.events.stop()
            self.events = None
        self._sweep_labeled()

    def _sweep_labeled(self):
        # Chaos CRs first (faults come off while the fleet still exists),
        # in their own kubectl: on a cluster missing some chaos CRDs the
        # combined delete errors on the unknown kinds while still deleting
        # the known ones, so check=False — the same || true semantics as
        # examples/toyserver/teardown.sh. Deleting a labeled Workflow
        # cascades to its child experiments via ownerReferences.
        self._kubectl("delete",
                      ",".join(CHAOS_KINDS + ["workflows.chaos-mesh.org"]),
                      "-l", f"hokea/cluster={self.prefix}",
                      "--ignore-not-found", "--wait=true",
                      check=False, timeout=300)
        self._kubectl("delete", "pods,services,configmaps",
                      "-l", f"hokea/cluster={self.prefix}",
                      "--ignore-not-found", "--wait=true", timeout=300)

    def __enter__(self):
        return self.up()

    def __exit__(self, *exc):
        self.down()

    # -- nodes ----------------------------------------------------------------

    def node(self, i: int) -> KubeNode:
        if not 1 <= i <= self.n_nodes:
            raise IndexError(f"node index {i} out of range 1..{self.n_nodes}")
        return self._nodes[i - 1]

    def nodes(self) -> list[KubeNode]:
        return list(self._nodes)

    def make_net(self):
        return KubeNet(self)

    def make_chaos(self):
        """For faults hokea has no wrapper for: a KubeChaos that applies
        your own Chaos Mesh YAML (any experiment kind, or a Workflow)
        inside the fault schedule. The pytest plugin's `chaos` fixture
        calls this, mirroring make_net()."""
        return KubeChaos(self)

    def wait_healthy(self, timeout: float = 90):
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
                f"--- {n.name} last logs ---\n{n.logs()}" for n in pending.values())
            hint = ""
            if self.expose == "internal" and self.harness_namespace is None:
                # The auto-detect can be fooled (KUBERNETES_SERVICE_HOST
                # set on a laptop, say): internal-mode URLs are pod DNS
                # records that only resolve from inside the cluster.
                hint = ("\nThis cluster chose expose='internal' (pod-DNS "
                        "URLs, reachable only from inside the cluster) but "
                        "the harness doesn't look like a pod — running "
                        "outside a pod? Pass expose='nodeport' or unset "
                        "KUBERNETES_SERVICE_HOST.")
            raise TimeoutError(
                f"nodes not healthy after {timeout}s: "
                f"{sorted(pending)}{hint}\n{detail}")

    # -- process faults -------------------------------------------------------

    def kill(self, i: int):
        """Force-delete the pod immediately (no grace period). start(i)
        brings it back.

        A force-delete returns when the API object is gone, which can be a
        second or two BEFORE the kubelet actually reaps the process — so
        kill_done is recorded only once the node stops answering /health:
        the availability window must not open while the node still serves."""
        self._fault_until_unreachable(i, "kill", "--grace-period=0", "--force")

    def stop(self, i: int):
        """Graceful shutdown: SIGTERM, 5s grace, then gone. Like kill(),
        stop_done is recorded only once the node stops answering."""
        self._fault_until_unreachable(i, "stop", "--grace-period=5")

    def _fault_until_unreachable(self, i: int, action: str, *delete_flags: str):
        node = self.node(i)
        self.fault_log.record(action, node=i)
        self._kubectl("delete", "pod", node.name, *delete_flags, "--wait=true",
                      timeout=120)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if requests.get(f"{node.url}/health", timeout=1).status_code != 200:
                    break
            except requests.RequestException:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(
                f"{action}({i}): pod {node.name} was deleted but the node "
                "still answers /health 15s later — the fault is not in the "
                "state the test believes.")
        self.fault_log.record(f"{action}_done", node=i)

    def start(self, i: int):
        """Re-create the pod from the manifest up() built. Fresh pod IP and
        empty /data — a returning node proves (or disproves) durability.

        The delete-first matters: if the node's process died on its own
        (crash, OOMKill), the pod object lingers in phase Failed and
        `kubectl apply` of the unchanged manifest would be a no-op — start()
        must revive a self-crashed node, not just one hokea killed.

        start_done is recorded only once the node answers /health at
        node.url again — the mirror image of kill()/stop()'s
        wait-until-unreachable. Pod Ready is not enough: the re-created pod
        has a NEW IP, and the headless Service's DNS record for its stable
        name can lag behind it, so the next dial would connect-timeout.
        This also tightens the availability-window semantics: the window a
        kill/stop opened closes here, when the node truly serves again."""
        path = self.workdir / f"restart-node{i}.yaml"
        path.write_text(yaml.safe_dump(self._pod_manifests[i], sort_keys=False))
        self.fault_log.record("start", node=i)
        self._kubectl("delete", "pod", self.node(i).name,
                      "--ignore-not-found", "--wait=true", timeout=120)
        self._kubectl("apply", "-f", str(path))
        self._wait_pods_ready(self.up_timeout, only=[i])
        self._start_until_serving(i)
        self.fault_log.record("start_done", node=i)
        for callback in self._observers:
            callback(i)

    def _start_until_serving(self, i: int):
        node = self.node(i)
        deadline = time.monotonic() + START_SERVE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if requests.get(f"{node.url}/health",
                                timeout=2).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.2)
        raise RuntimeError(
            f"start({i}): pod {node.name} is Ready but the node never "
            f"answered GET {node.url}/health within {START_SERVE_TIMEOUT}s "
            "— it is not actually serving at its stable URL (a fresh pod "
            "has a new IP; usually the DNS record behind the name is what "
            "lags, but not for this long).")

    def pause(self, i: int):
        raise NotImplementedError(
            "pause() is Docker-backend-only: Kubernetes has no SIGSTOP for a "
            "pod. Closest Chaos Mesh analog: PodChaos action=pod-failure "
            "(replaces the container for a duration — process state is LOST, "
            "unlike pause). Use kill/start if restart semantics are fine.")

    unpause = pause

    def throttle(self, i: int, *, cpus: float):
        """Make node i a compute straggler: shrink its CPU limit at runtime
        via the pod `resize` subresource (Kubernetes >= 1.33) — the pod is
        NOT restarted, process state survives, exactly like the Docker
        backend's `docker update --cpus`. Memory limits are untouched.
        Verified by reading the applied values back from the pod (spec and
        container status), not by timing, which flakes on a loaded cluster.
        A fresh up() creates pods at the configured cpus, so a leftover
        throttle never survives into the next run."""
        if cpus <= 0:
            raise ValueError(f"throttle needs cpus > 0, got {cpus} "
                             "(to halt a node entirely, use kill or stop)")
        self._resize_cpus(i, "throttle", cpus)

    def unthrottle(self, i: int):
        """Restore node i to the cluster's configured cpus, verified the
        same way throttle() is."""
        self._resize_cpus(i, "unthrottle", self.cpus)

    def _resize_cpus(self, i: int, action: str, cpus: float):
        node = self.node(i)
        self.fault_log.record(action, node=i, cpus=cpus)
        # The manifest sets resources.limits only; admission defaults
        # requests to limits, so resize both to keep them consistent (a
        # request above the limit would be rejected). Memory stays as-is.
        patch = json.dumps({"spec": {"containers": [{
            "name": "main",
            "resources": {"limits": {"cpu": str(cpus)},
                          "requests": {"cpu": str(cpus)}}}]}})
        try:
            self._kubectl("patch", "pod", node.name,
                          "--subresource", "resize", "--patch", patch)
        except CommandFailed as e:
            raise RuntimeError(
                f"{action}({i}): the cluster rejected the in-place pod "
                "resize. This needs Kubernetes >= 1.33 with the resize "
                "subresource (InPlacePodVerticalScaling) enabled and no "
                "restart-requiring resizePolicy on the container.\n"
                f"{e}") from e
        # Verify by inspection: the patch changes .spec immediately, but the
        # kubelet applies it asynchronously — poll the container STATUS
        # resources (what is actually enforced) until they match, then
        # record _done. Raise if it never takes: a throttle the test
        # believes in but the node doesn't have would poison conclusions.
        deadline = time.monotonic() + 15
        while True:
            spec_cpu, actual_cpu = self._read_cpus(node.name)
            if (_cpu_quantity(spec_cpu) is not None
                    and abs(_cpu_quantity(spec_cpu) - cpus) < 1e-6
                    and _cpu_quantity(actual_cpu) is not None
                    and abs(_cpu_quantity(actual_cpu) - cpus) < 1e-6):
                break
            if time.monotonic() > deadline:
                conditions = self._kubectl(
                    "get", "pod", node.name, "-o",
                    "jsonpath={.status.conditions}", check=False).stdout
                raise RuntimeError(
                    f"{action}({i}) did not take: asked for {cpus} cpus, pod "
                    f"reports spec.limits.cpu={spec_cpu!r} but the running "
                    f"container has {actual_cpu!r} 15s later (resize pending "
                    "or infeasible — feature gate off, restart-requiring "
                    "resizePolicy, or no node capacity?). Pod conditions:\n"
                    f"{conditions}")
            time.sleep(0.3)
        self.fault_log.record(f"{action}_done", node=i, cpus=cpus)

    def _read_cpus(self, pod_name: str) -> tuple[str | None, str | None]:
        """(spec limit, actually-applied limit) as k8s quantities, e.g.
        ("100m", "100m"). The status side is what the kubelet enforces."""
        pod = json.loads(self._kubectl("get", "pod", pod_name,
                                       "-o", "json").stdout)
        spec_cpu = ((pod["spec"]["containers"][0].get("resources") or {})
                    .get("limits", {}).get("cpu"))
        statuses = pod.get("status", {}).get("containerStatuses") or [{}]
        actual_cpu = ((statuses[0].get("resources") or {})
                      .get("limits", {}).get("cpu"))
        return spec_cpu, actual_cpu

    def on_state_change(self, callback):
        self._observers.append(callback)

    # -- internals ------------------------------------------------------------

    def _kubectl(self, *argv: str, check: bool = True, timeout: float = 120):
        return _kubectl_sh("-n", self.namespace, *argv,
                           check=check, timeout=timeout)

    def _labels(self, extra: dict | None = None) -> dict:
        return {"app.kubernetes.io/managed-by": MANAGED_BY,
                "hokea/cluster": self.prefix, **(extra or {})}

    def _peers(self) -> str:
        return ",".join(f"node-{i}:{self.peer_port}"
                        for i in range(1, self.n_nodes + 1))

    def _configmap_manifest(self) -> dict:
        from .runner import is_shippable_name
        files = sorted(p for p in self.src.iterdir()
                       if p.is_file() and is_shippable_name(p.name))
        skipped = sorted(p.name for p in self.src.iterdir() if not p.is_file())
        junk = sorted(p.name for p in self.src.iterdir()
                      if p.is_file() and not is_shippable_name(p.name))
        if skipped:
            print(f"hokea: ConfigMap source mode ships top-level regular "
                  f"files only; skipping {skipped}", flush=True)
        if junk:
            print(f"hokea: not shipping {junk} (lockfiles, images and "
                  "archives are never needed on the cluster)", flush=True)
        total = sum(p.stat().st_size for p in files)
        if total > 900_000:
            raise ValueError(
                f"src= totals {total} bytes; a ConfigMap holds ~1MiB. Build "
                "an image, push it to a registry the cluster pulls from, and "
                "pass image= alone.")
        data, binary = {}, {}
        for p in files:
            if not re.fullmatch(r"[-._a-zA-Z0-9]+", p.name):
                raise ValueError(
                    f"src file {p.name!r} is not a valid ConfigMap key "
                    "(letters, digits, '-', '_', '.') — rename it or build "
                    "an image instead")
            raw = p.read_bytes()
            try:
                data[p.name] = raw.decode()
            except UnicodeDecodeError:
                binary[p.name] = base64.b64encode(raw).decode()
        manifest = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"{self.prefix}-src",
                         "labels": self._labels()},
            "data": data,
        }
        if binary:
            manifest["binaryData"] = binary
        return manifest

    def _peers_service_manifest(self) -> dict:
        """Headless Service backing the `node-<i>` DNS names: each pod sets
        `hostname=node-<i>` subdomain={prefix}-peers, giving it the record
        `node-<i>.{prefix}-peers.{ns}.svc`; a dnsConfig search entry on every
        pod makes the bare name `node-<i>` resolve, preserving the PEERS
        contract byte-for-byte."""
        return {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": f"{self.prefix}-peers",
                         "labels": self._labels()},
            "spec": {
                "clusterIP": "None",
                "selector": {"hokea/cluster": self.prefix},
                "ports": [{"port": self.peer_port, "name": "peer"}],
            },
        }

    def _nodeport_service_manifest(self, i: int) -> dict:
        return {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": f"{self.prefix}-node{i}",
                         "labels": self._labels()},
            "spec": {
                "type": "NodePort",
                "selector": {"hokea/cluster": self.prefix,
                             "hokea/node": str(i)},
                "ports": [{"port": self.port,
                           "targetPort": self.port, "name": "client"}],
            },
        }

    def _pod_manifest(self, i: int) -> dict:
        container = {
            "name": "main",
            "image": self.image,
            "env": [{"name": "NODE_ID", "value": str(i)},
                    {"name": "N_NODES", "value": str(self.n_nodes)},
                    {"name": "PEERS", "value": self._peers()},
                    *({"name": k, "value": v} for k, v in self.env.items())],
            "ports": [{"containerPort": self.port}],
            "resources": {"limits": {"cpu": str(self.cpus),
                                     "memory": self.memory}},
            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
        }
        volumes = [{"name": "data", "emptyDir": {}}]
        # Dataset image volumes: pullPolicy IfNotPresent so a ref already
        # in the node's containerd store needs no registry (see the class
        # docstring); containerd keeps one on-disk copy per node however
        # many pods mount it. The kubelet mounts image volumes read-only
        # by design; readOnly on the mount states the intent anyway.
        for mount_path, ref in self.data.items():
            vol = ("dataset" if mount_path == "/dataset"
                   else f"dataset-{mount_path.rsplit('/', 1)[-1]}")
            container["volumeMounts"].append(
                {"name": vol, "mountPath": mount_path, "readOnly": True})
            volumes.append({"name": vol,
                            "image": {"reference": ref,
                                      "pullPolicy": "IfNotPresent"}})
        if self.src is not None:
            container["command"] = shlex.split(self.cmd)
            container["workingDir"] = "/app"
            container["volumeMounts"].append(
                {"name": "src", "mountPath": "/app", "readOnly": True})
            volumes.append({"name": "src",
                            "configMap": {"name": f"{self.prefix}-src",
                                          # scripts stay runnable: cmd may be
                                          # "./start.sh", which Docker's bind
                                          # mount allows via the host exec bit
                                          "defaultMode": 0o555}})
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": f"{self.prefix}-node{i}",
                         "labels": self._labels({"hokea/node": str(i)})},
            "spec": {
                "hostname": f"node-{i}",
                "subdomain": f"{self.prefix}-peers",
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 5,
                "dnsConfig": {"searches": [
                    f"{self.prefix}-peers.{self.namespace}.svc.cluster.local"]},
                "containers": [container],
                "volumes": volumes,
            },
        }

    def _wait_pods_ready(self, timeout: float, only: list[int] | None = None):
        names = [f"pod/{self.prefix}-node{i}"
                 for i in (only or range(1, self.n_nodes + 1))]
        self._kubectl("wait", "--for=condition=Ready", *names,
                      f"--timeout={int(timeout)}s", timeout=timeout + 30)

    def _detect_node_host(self) -> str:
        nodes = json.loads(_kubectl_sh("get", "nodes", "-o", "json").stdout)
        for item in nodes.get("items", []):
            conditions = {c["type"]: c["status"]
                          for c in item["status"].get("conditions", [])}
            if conditions.get("Ready") != "True":
                continue
            for addr in item["status"].get("addresses", []):
                if addr["type"] == "InternalIP":
                    return addr["address"]
        raise RuntimeError("no Ready cluster node with an InternalIP found; "
                           "pass node_host= explicitly")


class KubeEventCollector(EventCollector):
    """The Docker collector, reading pod logs via kubectl instead.

    Two deliberate departures from the Docker rules: the stream follows from
    the log's *beginning* (a fresh pod's log is exactly the capture we want,
    and runs slice by byte offset anyway — --tail=0 on an already-Ready pod
    would drop its first post-restart lines), and re-attachment on a node
    state change is unconditional — a `kubectl logs --follow` on a
    force-deleted pod can outlive the API object briefly, and the Docker
    rule ("re-attach only if the old stream ended") would then orphan the
    node's event stream for the rest of the cluster's life."""

    def _attach(self, node):
        raw = open(self._raw_path(node.index), "ab")
        try:
            proc = subprocess.Popen(
                ["kubectl", "-n", node.cluster.namespace, "logs",
                 "--follow", node.name],
                stdout=raw, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            print("hokea: kubectl not available; event collection disabled",
                  flush=True)
            raw.close()
            self.disabled = True
            return
        self._procs[node.index] = proc
        self._files[node.index] = raw

    def _reattach_if_ended(self, i: int):
        if self._stopped or i not in self._procs:
            return
        proc = self._procs[i]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._files[i].close()
        self._attach(self.cluster.node(i))


def _cpu_quantity(q: str | None) -> float | None:
    """A Kubernetes CPU quantity as a float: "100m" -> 0.1, "1" -> 1.0.
    The API server normalizes what we send, so read-back verification must
    compare parsed values, not strings."""
    if not q:
        return None
    if q.endswith("m"):
        return int(q[:-1]) / 1000
    return float(q)


# ---------------------------------------------------------------------------
# The chaos watcher: foreign Chaos Mesh experiments land in the fault log
# ---------------------------------------------------------------------------

# Every chaos kind Chaos Mesh 2.x can install as a namespaced CR. Clusters
# without some CRDs are tolerated: the watcher probes once at startup and
# polls only the kinds that exist.
CHAOS_KINDS = ["networkchaos", "stresschaos", "podchaos", "iochaos",
               "timechaos", "httpchaos", "dnschaos", "kernelchaos",
               "blockchaos"]


def _chaos_summary(spec: dict) -> dict:
    """A compact who/how-long summary of a foreign CR's spec — enough to
    read faults.jsonl and know what the experiment touched, without copying
    the whole spec into every run record."""
    summary = {}
    selector = (spec.get("selector") or {}).get("labelSelectors")
    if selector:
        summary["selector"] = selector
    if spec.get("duration"):
        summary["duration"] = spec["duration"]
    return summary


def _chaos_snapshot(items: list, own_cluster: str) -> dict:
    """One poll's worth of chaos CRs as {(kind, name): state}. CRs labeled
    `hokea/cluster=<own_cluster>` are skipped — those are THIS cluster's own
    faults, already recorded by KubeNet with richer semantics. Another hokea
    cluster's CRs in the same namespace are foreign to this run: recorded."""
    snap = {}
    for item in items:
        labels = (item.get("metadata") or {}).get("labels") or {}
        if labels.get("hokea/cluster") == own_cluster:
            continue
        conditions = {c.get("type"): c.get("status")
                      for c in (item.get("status") or {}).get("conditions") or []}
        spec = item.get("spec") or {}
        snap[(item["kind"].lower(), item["metadata"]["name"])] = {
            "injected": conditions.get("AllInjected") == "True",
            "recovered": conditions.get("AllRecovered") == "True",
            "action": spec.get("action"),
            "summary": _chaos_summary(spec),
        }
    return snap


def _diff_chaos_snapshots(prev: dict, curr: dict) -> list:
    """What fault events do two consecutive snapshots imply? Returns
    [(event_name, fields), ...] in recording order. Pure function — the
    watcher thread is just kubectl + clock around this.

    - a CR appears -> "chaos" (kind, chaos_name, action, spec summary);
      if it is already injected/recovered, the later events follow
      back-to-back — a fast experiment must still leave a full triple.
    - AllInjected flips True -> "chaos_done" (the fault is actually in).
    - AllRecovered flips True, or the CR disappears without having
      recovered -> "chaos_recovered" (recorded once, whichever comes first).
    """
    events = []
    for (kind, name), state in curr.items():
        base = {"kind": kind, "chaos_name": name}
        before = prev.get((kind, name))
        if before is None:
            fields = dict(base)
            if state["action"]:
                fields["action"] = state["action"]
            if state["summary"]:
                fields["spec"] = state["summary"]
            events.append(("chaos", fields))
            if state["injected"]:
                events.append(("chaos_done", dict(base)))
            if state["recovered"]:
                events.append(("chaos_recovered", dict(base)))
            continue
        if state["injected"] and not before["injected"]:
            events.append(("chaos_done", dict(base)))
        if state["recovered"] and not before["recovered"]:
            events.append(("chaos_recovered", dict(base)))
    for (kind, name), before in prev.items():
        if (kind, name) not in curr and not before["recovered"]:
            events.append(("chaos_recovered",
                           {"kind": kind, "chaos_name": name}))
    return events


class ChaosWatcher:
    """Background recorder of FOREIGN chaos: experiments a student applies
    by hand (kubectl, the dashboard) would otherwise be invisible to
    faults.jsonl, and a fault record with holes is worse than none. Every
    ~0.5s one kubectl call lists all chaos kinds in the namespace; the diff
    against the previous snapshot lands in cluster.fault_log on the same
    harness monotonic clock as every other event.

    Chaos hokea itself injects (CRs labeled `hokea/cluster=<prefix>`) is
    skipped: KubeNet already records partition/slow with verification.

    Exception-hardened: a kubectl blip logs once and polling continues; only
    >30s of consecutive failures prints a loud warning that the fault record
    may be incomplete. FaultLog.record is a plain list append (GIL-safe), so
    no locking is needed against the main thread's own record calls."""

    POLL_INTERVAL = 0.5
    FAIL_LOUD_AFTER = 30.0

    def __init__(self, cluster: KubeCluster):
        self.cluster = cluster
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._kinds: list[str] = []
        self._prev: dict = {}
        self._error_since: float | None = None
        self._warned_loud = False

    def start(self):
        self._kinds = self._probe_kinds()
        if not self._kinds:
            print("hokea: chaos watcher: no Chaos Mesh CRD kinds found; "
                  "foreign chaos will not be recorded in faults.jsonl",
                  flush=True)
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="hokea-chaos-watcher")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            # a poll's kubectl call (timeout=30) may be in flight
            self._thread.join(timeout=35)
            self._thread = None

    def _probe_kinds(self) -> list[str]:
        """The combined get is one API round-trip per poll — but it fails
        wholesale if any single CRD is missing, so probe once: try the full
        list, and on error keep only the kinds that individually exist."""
        try:
            self._get_items(CHAOS_KINDS)
            return list(CHAOS_KINDS)
        except Exception:
            kinds = []
            for kind in CHAOS_KINDS:
                try:
                    self._get_items([kind])
                    kinds.append(kind)
                except Exception:
                    pass
            return kinds

    def _get_items(self, kinds: list[str]) -> list:
        out = self.cluster._kubectl("get", ",".join(kinds), "-o", "json",
                                    timeout=30)
        return json.loads(out.stdout).get("items", [])

    def _loop(self):
        while not self._stop.wait(self.POLL_INTERVAL):
            try:
                snap = _chaos_snapshot(self._get_items(self._kinds),
                                       self.cluster.prefix)
            except Exception as e:
                self._note_error(e)
                continue
            self._error_since = None
            self._warned_loud = False
            for event, fields in _diff_chaos_snapshots(self._prev, snap):
                self.cluster.fault_log.record(event, **fields)
            self._prev = snap

    def _note_error(self, e: Exception):
        now = time.monotonic()
        if self._error_since is None:
            self._error_since = now
            print("hokea: chaos watcher: kubectl poll failed "
                  f"({type(e).__name__}); retrying", flush=True)
        elif (now - self._error_since > self.FAIL_LOUD_AFTER
              and not self._warned_loud):
            self._warned_loud = True
            print("hokea: WARNING: chaos watcher failing for over "
                  f"{self.FAIL_LOUD_AFTER:.0f}s — foreign chaos faults may "
                  "be MISSING from this run's fault record", flush=True)


# ---------------------------------------------------------------------------
# Network faults via Chaos Mesh
# ---------------------------------------------------------------------------

class KubeNet:
    """Verified network faults on the pod fleet, implemented as Chaos Mesh
    NetworkChaos CRs. Same API and same discipline as Net:

        net.partition([[1, 2, 3], [4, 5]])
        net.slow(3, delay_ms=200, jitter_ms=50, loss_pct=5)
        net.heal()

    Chaos Mesh experiments are level-triggered — a CR holds its fault until
    deleted, and re-applying a completed CR is a no-op — so every action
    creates a FRESH CR via generateName and heal() deletes this cluster's
    CRs (by label). After Chaos Mesh reports AllInjected, hokea still probes
    reachability itself and raises on any mismatch: a partition that
    silently didn't happen would produce confident wrong conclusions.

    Scoping: every CR's source AND target select only this cluster's pods
    (hokea/cluster label), so harness traffic — the "client network",
    arriving via NodePorts in nodeport expose mode or straight from the
    client namespace's pods in internal mode — is never faulted. The cut covers the direct pod-to-pod path,
    i.e. exactly what the PEERS names resolve to — which is also the path
    the probes verify. Systems must reach peers via PEERS only: traffic
    routed through a Kubernetes Service VIP is not reliably cut (the
    OUTPUT-side rule fires before kube-proxy's DNAT) and such a leak is
    invisible to verification.

    If a node is killed and re-started while faults are active, the pod
    returns under a new IP; Chaos Mesh pins a CR's targets at creation, so
    hokea deletes and re-creates its CRs (fresh objects, per the
    level-triggered rule) and re-verifies — the same reconciliation the
    Docker backend does with iptables rules.
    """

    def __init__(self, cluster: KubeCluster):
        self.cluster = cluster
        self.log = cluster.fault_log
        self._groups: list[list[int]] | None = None
        self._slow: dict[int, tuple] = {}
        self._partition_crs: list[str] = []  # live CR names, for surgical delete
        self._slow_crs: dict[int, str] = {}
        self._cr_seq = 0  # distinct manifest filenames: keep all for forensics
        self._probe_via = self._detect_probe_tool()
        self._sweep()  # leftover CRs from a crashed earlier run
        cluster.on_state_change(self._on_node_change)
        print("hokea: network fault backend: Chaos Mesh NetworkChaos "
              f"(namespace {cluster.namespace})", flush=True)

    # -- public API -----------------------------------------------------------

    def partition(self, groups: list[list[int]]):
        flat = sorted(i for g in groups for i in g)
        if flat != list(range(1, self.cluster.n_nodes + 1)):
            raise ValueError(f"groups {groups} must cover nodes "
                             f"1..{self.cluster.n_nodes} exactly once")
        if self._groups is not None:
            raise RuntimeError("a partition is already active; heal() first")
        self.log.record("partition", groups=groups)
        self._groups = groups
        self._partition_crs = self._install_partition()
        skipped = self._verify_partition()
        self.log.record("partition_verified", groups=groups,
                        skipped_nodes=sorted(skipped),
                        chaos_objects=list(self._partition_crs))

    def isolate(self, i: int):
        rest = [j for j in range(1, self.cluster.n_nodes + 1) if j != i]
        self.partition([[i], rest])

    def slow(self, i: int, *, delay_ms: int, jitter_ms: int = 0,
             loss_pct: float = 0):
        """Shape node i's egress toward its peers with a netem NetworkChaos.
        Verified as: AllInjected on the CR, plus a recorded probe RTT from a
        same-side peer (recorded, not asserted — loaded clusters make exact
        timing assertions flaky)."""
        self.log.record("slow", node=i, delay_ms=delay_ms,
                        jitter_ms=jitter_ms, loss_pct=loss_pct)
        self._slow[i] = (delay_ms, jitter_ms, loss_pct)
        # Replace, like Docker's `tc qdisc replace`: install the new CR
        # first (no un-slowed hole), then delete the superseded one —
        # leaving both would stack netems into a delay nobody asked for.
        superseded = self._slow_crs.pop(i, None)
        name = self._install_slow(i)
        self._slow_crs[i] = name
        if superseded:
            self._delete_crs([superseded])
        rtt = None
        for j in self._same_side_peers(i):
            if self._runnable(j):
                rtt = self._probe_rtt_ms(j, i)
                break
        self.log.record("slow_verified", node=i, probe_rtt_ms=rtt,
                        chaos_objects=[name])

    def heal(self):
        """Delete every Chaos Mesh CR this cluster created, and prove
        recovery: probes across the former cut, and a recorded RTT to each
        formerly-slowed node (a chaos-daemon hiccup leaving netem behind
        after the CR is gone would otherwise poison every later baseline)."""
        self.log.record("heal")
        was_cross = self._cross_pairs() if self._groups else []
        was_slow = list(self._slow)
        self._groups = None
        self._slow = {}
        self._sweep()
        for a, b in was_cross:
            if self._runnable(a) and self._runnable(b):
                if self._probe(a, b) != "reachable":
                    raise RuntimeError(
                        f"heal failed: node {a} still cannot reach node {b}")
        post_heal_rtt = {}
        for i in was_slow:
            for j in range(1, self.cluster.n_nodes + 1):
                if j != i and self._runnable(j) and self._runnable(i):
                    post_heal_rtt[i] = self._probe_rtt_ms(j, i)
                    break
        self.log.record("heal_done", dirty_nodes=[],
                        post_heal_rtt_ms=post_heal_rtt)

    # -- reconciliation -------------------------------------------------------

    def _on_node_change(self, i: int):
        """A returned pod has a new IP and chaos CRs pin targets at
        creation, so the intended faults are re-established as fresh CRs.
        Create-new-THEN-delete-stale: chaos rules are additive, so the cut
        never has a hole during reconciliation — the reverse order would
        leave the cluster un-partitioned for seconds of finalizer wait."""
        if self._groups is None and not self._slow:
            return
        print(f"hokea: node {i} state changed; re-creating Chaos Mesh faults",
              flush=True)
        stale = self._partition_crs + list(self._slow_crs.values())
        self._partition_crs = []
        self._slow_crs = {}
        if self._groups is not None:
            self._partition_crs = self._install_partition()
        for j in list(self._slow):
            self._slow_crs[j] = self._install_slow(j)
        self._delete_crs(stale)
        if self._groups is not None:
            skipped = self._verify_partition()
            self.log.record("partition_verified", groups=self._groups,
                            skipped_nodes=sorted(skipped), reapplied_after=i)

    # -- chaos CR plumbing ----------------------------------------------------

    def _selector(self, indexes: list[int]) -> dict:
        return {
            "namespaces": [self.cluster.namespace],
            "labelSelectors": {"hokea/cluster": self.cluster.prefix},
            "expressionSelectors": [{"key": "hokea/node", "operator": "In",
                                     "values": [str(i) for i in indexes]}],
        }

    def _create_cr(self, kind_hint: str, spec: dict) -> str:
        """Create a fresh NetworkChaos (generateName — never re-apply a used
        object) and wait until Chaos Mesh reports AllInjected."""
        manifest = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {
                "generateName": f"{self.cluster.prefix}-{kind_hint}-",
                "namespace": self.cluster.namespace,
                "labels": {"app.kubernetes.io/managed-by": MANAGED_BY,
                           "hokea/cluster": self.cluster.prefix},
            },
            "spec": spec,
        }
        self._cr_seq += 1
        path = self.cluster.workdir / f"chaos-{self._cr_seq:03d}-{kind_hint}.yaml"
        path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        name = _kubectl_sh("create", "-f", str(path),
                           "-o", "name").stdout.strip()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            out = self.cluster._kubectl(
                "get", name, "-o",
                'jsonpath={.status.conditions[?(@.type=="AllInjected")].status}',
                check=False).stdout.strip()
            if out == "True":
                return name
            time.sleep(0.5)
        detail = self.cluster._kubectl("get", name, "-o", "yaml",
                                       check=False).stdout[-2000:]
        raise RuntimeError(
            f"Chaos Mesh did not reach AllInjected within 30s for {name}; "
            f"the fault is not in the state the test believes.\n{detail}")

    def _sweep(self):
        self.cluster._kubectl(
            "delete", "networkchaos",
            "-l", f"hokea/cluster={self.cluster.prefix}",
            "--ignore-not-found", "--wait=true", timeout=120)
        self._partition_crs = []
        self._slow_crs = {}

    def _delete_crs(self, names: list[str]):
        if names:
            self.cluster._kubectl("delete", *names, "--ignore-not-found",
                                  "--wait=true", timeout=120)

    def _install_partition(self) -> list[str]:
        created = []
        for k, (ga, gb) in enumerate(self._group_pairs()):
            ga_up = [i for i in ga if self._runnable(i)]
            gb_up = [i for i in gb if self._runnable(i)]
            if not ga_up or not gb_up:
                print(f"hokea: no running pods on one side of {ga}|{gb}; "
                      "the cut re-establishes when nodes return", flush=True)
                continue
            created.append(self._create_cr(f"part{k}", {
                "action": "partition",
                "mode": "all",
                "direction": "both",
                "selector": self._selector(ga_up),
                "target": {"mode": "all", "selector": self._selector(gb_up)},
            }))
        return created

    def _install_slow(self, i: int) -> str:
        delay_ms, jitter_ms, loss_pct = self._slow[i]
        others = [j for j in range(1, self.cluster.n_nodes + 1) if j != i]
        spec = {
            "action": "netem",
            "mode": "all",
            "direction": "to",
            "selector": self._selector([i]),
            "target": {"mode": "all", "selector": self._selector(others)},
            "delay": {"latency": f"{delay_ms}ms",
                      "jitter": f"{jitter_ms}ms"},
        }
        if loss_pct:
            spec["loss"] = {"loss": f"{loss_pct:g}"}
        return self._create_cr(f"slow{i}", spec)

    # -- verification (same discipline as Net) --------------------------------

    def _group_pairs(self):
        from itertools import combinations
        return list(combinations(self._groups, 2))

    def _cross_pairs(self) -> list[tuple[int, int]]:
        return [(a, b) for ga, gb in self._group_pairs()
                for a in ga for b in gb]

    def _verify_partition(self) -> set[int]:
        from concurrent.futures import ThreadPoolExecutor
        skipped = {i for i in range(1, self.cluster.n_nodes + 1)
                   if not self._runnable(i)}
        checks = []
        for a, b in self._cross_pairs():
            checks.append((a, b, "blocked"))
            checks.append((b, a, "blocked"))
        from itertools import combinations
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
                    f"partition ({type(e).__name__}). Harness traffic is "
                    "never faulted; something else is wrong.") from e
        return skipped

    def _detect_probe_tool(self) -> str:
        node = self.cluster.node(1)
        if node.exec("ping", "-c", "1", "-W", "1", "127.0.0.1",
                     check=False).returncode == 0:
            return "ping"
        if node.exec("python3", "-c", "pass", check=False).returncode == 0:
            return "python3"
        raise RuntimeError(
            "fault verification needs `ping` or `python3` inside the pods; "
            "add one to the image")

    def _probe(self, i: int, j: int) -> str:
        """Can node i reach node j? -> reachable|blocked. Every probe rides
        kubectl exec through the API server, and an exec that failed to run
        must never be read as "blocked" — that would let an API blip pass a
        partition check. No remote stdout = the probe didn't run: retry,
        then refuse to guess."""
        for attempt in range(3):
            result = self._probe_once(i, j)
            if result is not None:
                return result
            time.sleep(0.5)
        raise RuntimeError(
            f"could not execute a reachability probe on node {i} (kubectl "
            "exec produced no output three times); refusing to guess "
            "whether the network is cut")

    def _probe_once(self, i: int, j: int) -> str | None:
        ip = self.cluster.node(j).cluster_ip()
        if not ip:
            return None  # pod object mid-recreation; retry
        if self._probe_via == "ping":
            r = self.cluster.node(i).exec("ping", "-c", "1", "-W", "2", ip,
                                          check=False)
            if r.returncode == 0:
                return "reachable"
            # ping that RAN prints its header/stats even on total loss;
            # a failed exec prints nothing at all.
            return "blocked" if r.stdout else None
        r = self.cluster.node(i).exec("python3", "-c", PROBE_PY, ip,
                                      str(self.cluster.peer_port), check=False)
        return r.stdout.split()[0] if r.stdout else None

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

    def _same_side_peers(self, i: int) -> list[int]:
        if self._groups is None:
            return [j for j in range(1, self.cluster.n_nodes + 1) if j != i]
        group = next(g for g in self._groups if i in g)
        return [j for j in group if j != i]

    def _runnable(self, i: int) -> bool:
        return self.cluster.node(i).status() == "running"


# ---------------------------------------------------------------------------
# Student-authored chaos: apply your own Chaos Mesh YAML in the schedule
# ---------------------------------------------------------------------------

def _prepare_chaos_manifest(doc: dict, *, namespace: str, labels: dict) -> dict:
    """One YAML document, made safe to apply through hokea. Pure function.

    - only Chaos Mesh objects pass: anything whose apiVersion is not
      chaos-mesh.org/* is rejected in plain English (a stray Deployment in
      the file must not be silently created with hokea's labels on it).
    - metadata.namespace is forced to the cluster's namespace — the CR must
      land where the fleet (and the sweep) live, whatever the file says.
    - metadata.name becomes metadata.generateName (name + "-"): Chaos Mesh
      experiments are level-triggered, so re-applying the same file must
      create a FRESH object every time or the second run silently injects
      nothing. The original name is kept in the hokea/source-name
      annotation so faults.jsonl entries can be traced back to the file.
      A document that already uses generateName keeps it.
    - hokea's labels are stamped on (same _labels() as everything else):
      the sweep can delete leftovers, and the ChaosWatcher skips these CRs
      so KubeChaos's own events are the only record of them.
    """
    if not isinstance(doc, dict) or not doc:
        raise ValueError(
            f"chaos.apply: each YAML document must be a mapping describing "
            f"one Chaos Mesh object, got {doc!r}")
    api = doc.get("apiVersion") or ""
    kind = doc.get("kind") or ""
    if not api.startswith("chaos-mesh.org/"):
        raise ValueError(
            "chaos.apply takes Chaos Mesh experiment or Workflow YAML — "
            f"this document is {kind or '<no kind>'}/{api or '<no apiVersion>'}")
    out = copy.deepcopy(doc)
    meta = out.setdefault("metadata", {})
    meta["namespace"] = namespace
    name = meta.pop("name", None)
    if name:
        meta["generateName"] = f"{name}-"
        meta.setdefault("annotations", {})["hokea/source-name"] = name
    elif not meta.get("generateName"):
        meta["generateName"] = f"{kind.lower()}-"
    meta["labels"] = {**(meta.get("labels") or {}), **labels}
    return out


class KubeChaos:
    """Your own chaos, timed by the fault schedule: apply a Chaos Mesh
    experiment (or Workflow) you wrote, at a known moment in a recorded run,
    and clean it up — one step in a pytest test instead of a sequence of
    kubectl commands run by hand.

        def test_my_stress(cluster, chaos, run):
            h = run(ops, adapter, duration=60,
                    faults=[(10, lambda: chaos.apply("stress.yaml")),
                            (40, chaos.clear)])

    apply() rewrites each document to survive Chaos Mesh's level-triggered
    lifecycle (fresh generateName per call — re-running the same file always
    injects), forces it into the cluster's namespace, stamps hokea's labels,
    creates it, and by default waits until Chaos Mesh reports the fault
    injected. In internal expose mode it first refuses any document whose
    selector names the harness's own namespace — the client namespace is
    the protected observer, never a fault target. Because the labels make the ChaosWatcher skip these CRs (they
    are this cluster's own), apply()/clear() record the events themselves:
    "chaos" at creation, "chaos_done" once injected, "chaos_recovered" at
    clear() — the same shapes the watcher writes, so check.availability()
    windows them identically (label `chaos:<kind>:<generated-name>`).

    Workflows: a Workflow has no AllInjected condition — apply() waits until
    the workflow's entry node exists (status.entryNode) and records
    chaos_done with workflow=True; that window means "the workflow is
    running", not "a fault is in". The workflow's child experiments are
    created by Chaos Mesh WITHOUT hokea's label, so the ChaosWatcher records
    each child individually as it fires — faults.jsonl shows the workflow's
    actual steps with their real timestamps. Note the timing model: a
    workflow times its steps relative to its own start, cluster-side;
    anchoring that start is exactly what the fault schedule offset does.

    clear() deletes every CR this KubeChaos applied (--wait=true: Chaos Mesh
    finalizers make deletion mean recovery complete). Deleting a Workflow
    cascades to its children via ownerReferences; the children's recoveries
    are recorded by the watcher. Anything not cleared is deleted by the
    cluster's labeled sweep at down() — nothing outlives the run.

    Docker backend: Cluster.make_chaos() raises — Chaos Mesh only exists on
    Kubernetes; locally the built-in faults cover the same ground.
    """

    def __init__(self, cluster: KubeCluster):
        self.cluster = cluster
        self.log = cluster.fault_log
        self._applied: list[dict] = []  # handles, in creation order
        self._seq = 0

    def apply(self, source, *, wait: bool = True,
              timeout: float = 60) -> list[str]:
        """Create the chaos object(s) in `source` — a YAML file path, a
        dict, or a list of dicts — and return the created object refs
        (e.g. ["stresschaos.chaos-mesh.org/cpu-stress-x7k2m"]).

        wait=True (default) blocks until every created experiment reports
        AllInjected (a workflow: until its entry node starts) or raises
        after `timeout` seconds — so the next schedule step runs with the
        fault actually in, no timing guesses. wait=False returns as soon as
        the API accepted the objects."""
        docs = self._load(source)
        protected = (self.cluster.harness_namespace
                     if self.cluster.expose == "internal" else None)
        if protected:
            for doc in docs:
                if isinstance(doc, dict) and _selects_namespace(doc, protected):
                    raise ValueError(
                        "chaos.apply: a selector in this "
                        f"{doc.get('kind') or 'document'} names namespace "
                        f"{protected!r} — the harness's own namespace. The "
                        "client namespace is the protected observer: if "
                        "faults could reach the harness, its measurements "
                        "would mean nothing. Point the selector at the "
                        f"server namespace ({self.cluster.namespace!r}) "
                        "instead — or just omit `namespaces`, which "
                        "defaults to it.")
        prepared = [_prepare_chaos_manifest(
                        d, namespace=self.cluster.namespace,
                        labels=self.cluster._labels())
                    for d in docs]
        self._seq += 1
        path = (self.cluster.workdir /
                f"chaos-{self._seq:03d}-applied.yaml")
        self.cluster.workdir.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump_all(prepared, sort_keys=False))
        refs = _kubectl_sh("create", "-f", str(path),
                           "-o", "name").stdout.split()
        if len(refs) != len(prepared):  # partial create: sweep will catch it
            raise RuntimeError(
                f"chaos.apply: created {len(refs)} of {len(prepared)} "
                f"objects from {source!r} ({refs}); leftovers are labeled "
                "and will be removed by the cluster sweep")
        created = []
        for ref, doc in zip(refs, prepared):
            handle = {"ref": ref, "kind": doc["kind"].lower(),
                      "name": ref.rsplit("/", 1)[-1]}
            fields = {"kind": handle["kind"], "chaos_name": handle["name"],
                      "source": "applied"}
            spec = doc.get("spec") or {}
            if spec.get("action"):
                fields["action"] = spec["action"]
            summary = _chaos_summary(spec)
            if summary:
                fields["spec"] = summary
            self.log.record("chaos", **fields)
            self._applied.append(handle)
            created.append(handle)
        if wait:
            for handle in created:
                if handle["kind"] == "workflow":
                    self._wait_workflow(handle, timeout)
                else:
                    self._wait_injected(handle, timeout)
        return [h["ref"] for h in created]

    def clear(self):
        """Delete every CR this KubeChaos applied, waiting for Chaos Mesh's
        finalizers (deletion returns once recovery is complete), and record
        chaos_recovered for each. Safe to call with nothing applied."""
        applied, self._applied = self._applied, []
        for handle in applied:
            self.cluster._kubectl("delete", handle["ref"],
                                  "--ignore-not-found", "--wait=true",
                                  timeout=120)
            self.log.record("chaos_recovered", kind=handle["kind"],
                            chaos_name=handle["name"])

    # -- internals ------------------------------------------------------------

    def _load(self, source) -> list[dict]:
        if isinstance(source, dict):
            return [source]
        if isinstance(source, (list, tuple)):
            return list(source)
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(
                f"chaos.apply: no such file: {path} (pass a Chaos Mesh YAML "
                "file path, a dict, or a list of dicts)")
        docs = [d for d in yaml.safe_load_all(path.read_text())
                if d is not None]
        if not docs:
            raise ValueError(f"chaos.apply: {path} contains no YAML documents")
        return docs

    def _wait_injected(self, handle: dict, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self.cluster._kubectl(
                "get", handle["ref"], "-o",
                'jsonpath={.status.conditions[?(@.type=="AllInjected")].status}',
                check=False).stdout.strip()
            if out == "True":
                self.log.record("chaos_done", kind=handle["kind"],
                                chaos_name=handle["name"])
                return
            time.sleep(0.5)
        detail = self.cluster._kubectl("get", handle["ref"], "-o", "yaml",
                                       check=False).stdout[-2000:]
        raise RuntimeError(
            f"chaos.apply: Chaos Mesh did not reach AllInjected within "
            f"{timeout:.0f}s for {handle['ref']} — the fault is not in the "
            "state the test believes. Common cause: a selector matching "
            f"zero pods (this fleet's pods are labeled hokea/cluster="
            f"{self.cluster.prefix}, hokea/node=<i>).\n{detail}")

    def _wait_workflow(self, handle: dict, timeout: float):
        """A Workflow never reports AllInjected; the honest 'it has begun'
        signal 2.8.3 exposes is status.entryNode — set once the controller
        creates the entry WorkflowNode and the steps start executing."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self.cluster._kubectl(
                "get", handle["ref"], "-o", "jsonpath={.status.entryNode}",
                check=False).stdout.strip()
            if out:
                self.log.record("chaos_done", kind=handle["kind"],
                                chaos_name=handle["name"], workflow=True,
                                entry_node=out)
                return
            time.sleep(0.5)
        detail = self.cluster._kubectl("get", handle["ref"], "-o", "yaml",
                                       check=False).stdout[-2000:]
        raise RuntimeError(
            f"chaos.apply: workflow {handle['ref']} did not start (no "
            f"status.entryNode) within {timeout:.0f}s.\n{detail}")
