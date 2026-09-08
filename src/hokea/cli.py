"""The hokea command line.

    hokea export --format porcupine runs/<run>   # history -> external checker
    hokea test --namespace NS [pytest args...]   # run the suite ON the cluster
    hokea k8s up --namespace NS --src DIR --cmd "python server.py"
    hokea k8s status                             # where is my deployment?
    hokea k8s down                               # delete it

`hokea k8s` deploys your system to the class Kubernetes cluster and LEAVES
IT RUNNING for you to poke at with curl and kubectl — it is the "look, you
deployed something" tool. Recorded fault-injection runs are a different
thing: those go through pytest and the fixtures (see the toyserver
example's conftest), which bring a fresh cluster up and tear it down per
test — locally with `pytest` (Docker), or on the class cluster with
`hokea test` (a runner Job drives pytest from inside the cluster, streams
its output here, and copies the recorded runs/ back).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .export import export_jepsen, export_porcupine
from .kube import ClusterExists, KubeCluster, KubectlNotFound, cluster_prefix
from .proc import CommandFailed
from .runner import add_test_parser, cmd_test


def _state_path(name: str) -> Path:
    """Where `k8s up` remembers a deployment, so `down`/`status` can find it
    later without re-typing every flag. Relative to the current directory:
    run all three commands from your project."""
    return Path(".hokea") / f"k8s-{name}.json"


def _fail(message: str):
    """Exit with a plain-English message (no traceback)."""
    raise SystemExit(f"hokea: {message}")


def _parse_env(pairs: list[str]) -> dict[str, str]:
    env = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            _fail(f"--env expects KEY=VAL, got {pair!r}")
        env[key] = value
    return env


def _parse_data(values: list[str]):
    """--data flags into the data= kwarg KubeCluster takes: one bare REF is
    a plain /dataset mount, NAME=REF entries mount at `/dataset/<NAME>`."""
    if not values:
        return None
    named, bare = {}, []
    for value in values:
        name, sep, ref = value.partition("=")
        if not sep:
            bare.append(value)
        elif not name or not ref:
            _fail(f"--data expects NAME=REF (or a single bare REF), "
                  f"got {value!r}")
        else:
            named[name] = ref
    if not all(bare):
        _fail("--data got an empty value; pass an image reference")
    if bare and named:
        _fail("--data: mixing a bare REF with NAME=REF entries would leave "
              "the bare one unnamed — name them all")
    if len(bare) > 1:
        _fail("--data: only one bare REF can sit at /dataset — use NAME=REF "
              "to mount several")
    return bare[0] if bare else named


def _print_access(namespace: str, pods: list[str], port: int):
    """Per node, a copy-paste way to reach it from this machine. The class
    cluster publishes no NodePorts (quota zero), so a laptop reaches a pod
    through the API server with a port-forward. Local ports increment so
    forwards to several nodes can run at once."""
    print("\nreach a node from here (each forward keeps running in its "
          "own terminal until Ctrl-C):")
    for k, pod in enumerate(pods):
        local = port + k
        print(f"  node {k + 1}: kubectl -n {namespace} port-forward "
              f"pod/{pod} {local}:{port}")
        print(f"          then, in another terminal: "
              f"curl http://localhost:{local}/health")


def _k8s_up(args):
    src = Path(args.src)
    if not src.is_dir():
        _fail(f"--src {args.src!r} is not a directory. Point it at the "
              "folder containing your server code.")
    if args.nodes < 1:
        _fail(f"--nodes must be at least 1, got {args.nodes}")
    # expose="internal": the class cluster forbids NodePorts (quota zero),
    # so only the headless peers Service exists — from a laptop you reach
    # nodes with a port-forward (printed below), not a URL.
    kwargs = dict(image=args.image, src=str(src.resolve()), cmd=args.cmd,
                  nodes=args.nodes, name=args.name, namespace=args.namespace,
                  port=args.port, env=_parse_env(args.env),
                  data=_parse_data(args.data), expose="internal")
    try:
        cluster = KubeCluster(**kwargs, takeover=args.takeover)
    except ValueError as e:
        # KubeCluster validates its inputs (dataset names, cluster name)
        # with plain-English messages; pass them on without a traceback.
        _fail(str(e))
    try:
        # up() waits for every pod to reach condition=Ready (kubectl wait);
        # on failure it prints pod state and logs, then sweeps itself.
        cluster.up()
    except ClusterExists:
        _fail(f"cluster {args.name!r} is already running in "
              f"{args.namespace!r} — run `hokea k8s down` first, or pass "
              "--takeover to replace it.")
    except KubectlNotFound as e:
        _fail(str(e))
    except CommandFailed as e:
        print(e)
        _fail("the cluster did not come up — the pod state and logs above "
              "say why (a common cause: your server exits, or never starts "
              f"listening on --port {args.port}). Everything created was "
              "cleaned up; fix it and deploy again.")

    state = {"kwargs": kwargs,
             "pods": [node.name for node in cluster.nodes()]}
    path = _state_path(args.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))

    # The event collector tails pod logs with `kubectl logs --follow`; that
    # is for recorded test runs. This CLI exits and leaves the pods running,
    # so it must not leave follower processes behind.
    if cluster.events is not None:
        cluster.events.stop()
        cluster.events = None

    print(f"\nYour cluster is up: {args.nodes} node(s) in namespace "
          f"{args.namespace!r}, and it stays up until you take it down.")
    # No NodePorts means nothing to curl from here — as a health peek,
    # show what the server printed (a "listening on ..." line is the
    # usual sign of life).
    peek = cluster.node(1).logs(tail=5).strip()
    if peek:
        print("\nnode 1's last log lines:")
        for line in peek.splitlines():
            print(f"  {line}")
    _print_access(args.namespace, state["pods"], args.port)
    print(f"\nsee your pods: kubectl -n {args.namespace} get pods")
    print(f"take it down:  hokea k8s down --name {args.name}")


def _k8s_down(args):
    path = _state_path(args.name)
    if path.exists():
        try:
            state = json.loads(path.read_text())
            cluster = KubeCluster(**state["kwargs"])
        except (json.JSONDecodeError, KeyError, TypeError):
            _fail(f"state file {path} is unreadable (from an older hokea?) "
                  "— delete it and use: hokea k8s down --namespace <ns> "
                  f"--name {args.name}")
        if args.namespace and args.namespace != cluster.namespace:
            print(f"note: the state file says namespace "
                  f"{cluster.namespace!r}; it wins over --namespace "
                  f"{args.namespace!r}")
    elif args.namespace:
        # No record of an `up`, but the caller told us where to look: sweep
        # anything labeled with this cluster name out of the namespace.
        cluster = KubeCluster(image="unused", name=args.name,
                              namespace=args.namespace)
    else:
        _fail(f"no state file at {path} — I don't know where cluster "
              f"{args.name!r} lives. If its pods are still running, re-run "
              "with --namespace <ns> (and --name if you used one) to sweep "
              "them out by label.")
    try:
        cluster.down()
    except KubectlNotFound as e:
        _fail(str(e))
    if path.exists():
        path.unlink()
        print(f"removed {path}")
    print(f"deleted cluster {args.name!r} (pods, services, configmaps, "
          f"chaos rules) from namespace {cluster.namespace!r}")


def _k8s_status(args):
    path = _state_path(args.name)
    if not path.exists():
        _fail(f"no state file at {path} — nothing recorded as up here. "
              "Deploy with `hokea k8s up`, from this directory.")
    state = json.loads(path.read_text())
    kwargs = state["kwargs"]
    namespace = kwargs["namespace"]
    # Older state files recorded NodePort URLs instead of pod names; those
    # URLs are dead now (the cluster forbids NodePorts), so derive the pod
    # names from what up() recorded either way.
    pods = state.get("pods") or [
        f"{cluster_prefix(args.name)}-node{i}"
        for i in range(1, kwargs.get("nodes", 3) + 1)]
    print(f"cluster {args.name!r} in namespace {namespace!r}")
    _print_access(namespace, pods, kwargs.get("port", 8000))
    argv = ["kubectl", "-n", namespace, "get", "pods",
            "-l", f"hokea/cluster={cluster_prefix(args.name)}"]
    print(f"\n$ {' '.join(argv)}")
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=30)
        print((result.stdout + result.stderr).rstrip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("(could not run kubectl — install/configure it to see live "
              "pod state)")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hokea",
        description="fault injection and testing for distributed systems")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser(
        "export", help="convert a run's history for an external checker")
    export.add_argument("--format", required=True,
                        choices=["porcupine", "jepsen"])
    export.add_argument("run_dir", help="a runs/<timestamp>-<name> directory")

    add_test_parser(sub)

    k8s = sub.add_parser(
        "k8s", help="deploy your system to a Kubernetes cluster and leave "
                    "it running (tests use the pytest fixtures instead)")
    k8s_sub = k8s.add_subparsers(dest="k8s_command", required=True)

    up = k8s_sub.add_parser(
        "up", help="deploy N nodes and leave them running")
    up.add_argument("--namespace", required=True,
                    help="an existing namespace you may create pods in")
    up.add_argument("--src", required=True,
                    help="directory with your server code (ships to the "
                         "pods as a ConfigMap — no image build, no registry)")
    up.add_argument("--cmd", required=True,
                    help='start command, e.g. "python server.py"')
    up.add_argument("--image", default="python:3.12-slim",
                    help="base image to run --cmd on (default: %(default)s)")
    up.add_argument("--nodes", type=int, default=3,
                    help="how many nodes (default: %(default)s)")
    up.add_argument("--port", type=int, default=8000,
                    help="the port your server listens on "
                         "(default: %(default)s)")
    up.add_argument("--name", default="default",
                    help="cluster name, if you want more than one "
                         "(default: %(default)s)")
    up.add_argument("--env", action="append", default=[], metavar="KEY=VAL",
                    help="extra environment for every node (repeatable)")
    up.add_argument("--data", action="append", default=[], metavar="NAME=REF",
                    help="dataset image mounted read-only in every node at "
                         "/dataset/NAME (repeatable; one bare REF mounts at "
                         "/dataset)")
    up.add_argument("--takeover", action="store_true",
                    help="replace a still-running cluster with this name "
                         "instead of refusing")

    down = k8s_sub.add_parser(
        "down", help="delete a deployed cluster (everything it created)")
    down.add_argument("--name", default="default")
    down.add_argument("--namespace", default=None,
                      help="only needed if the state file from `up` is gone")

    status = k8s_sub.add_parser(
        "status", help="show a deployed cluster's pods and how to reach "
                       "them (port-forward lines)")
    status.add_argument("--name", default="default")

    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["test"]:
        # `hokea test -q -k partition`: the unrecognized flags belong to
        # pytest. argparse's REMAINDER only starts at a non-option token,
        # so collect strays with parse_known_args and hand them (in their
        # original order, strays first) to pytest with the remainder. A
        # bare `--` separator lands at the head of the remainder; strip it
        # here, BEFORE prepending strays — `hokea test -q -- -k x` must
        # reach pytest as `-q -k x`, not with a stranded `--` in the
        # middle that pytest reads as a file-arguments separator.
        args, strays = parser.parse_known_args(argv)
        remainder = args.pytest_args
        if remainder[:1] == ["--"]:
            remainder = remainder[1:]
        args.pytest_args = strays + remainder
    else:
        args = parser.parse_args(argv)

    if args.command == "export":
        exporter = {"porcupine": export_porcupine, "jepsen": export_jepsen}
        print(exporter[args.format](args.run_dir))
    elif args.command == "test":
        code = cmd_test(args)
        if code != 0:
            raise SystemExit(code)
    elif args.command == "k8s":
        {"up": _k8s_up, "down": _k8s_down, "status": _k8s_status}[
            args.k8s_command](args)


if __name__ == "__main__":
    main()
