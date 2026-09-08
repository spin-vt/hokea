"""`hokea test` — run this project's pytest suite ON the class cluster.

From the student's project directory:

    hokea test --namespace team-NN -k partition -x

What happens: the directory's top-level files ship to the cluster as a
ConfigMap in the team's CLIENT namespace (`team-NN-clients`), a Job there
runs the pinned runner image (pytest + hokea + kubectl, authenticated as
the `hokea-runner` ServiceAccount), and inside that Job pytest brings the
student's server pods up in the SERVER namespace (`team-NN`) and drives
load and chaos from in-cluster — the same tests, unchanged, that `pytest`
runs against Docker on a laptop. The launcher streams the pod's log live
(it IS pytest's output), copies the recorded `runs/` back into ./runs/
when the tests finish, and exits with pytest's own exit code.

The launcher and the runner meet over a marker protocol in the runner's
/project directory (see hokea.runner_entry): the runner touches
`.hokea-artifacts-ready` when pytest is done and holds until the launcher
touches `.hokea-copyout-done` after copying runs/ out.
"""

import argparse
import base64
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .kube import KubectlNotFound, _kubectl_sh
from .proc import CommandFailed
from .runner_entry import COPYOUT_MARKER, EXIT_CODE_FILE, READY_MARKER

# The runner image a `hokea test` Job runs. Every release is a NEW tag,
# never a mutated one: the image is side-loaded onto the cluster nodes
# (no registry) and pulled with IfNotPresent, so a node that has ever
# seen a tag keeps serving its cached copy forever — re-using a tag would
# ship different bits to different nodes. Version scheme: YYYY.MM.DD.N.
RUNNER_IMAGE = "hokea-runner:2026.08.29.3"

# The ServiceAccount the Job runs as. It exists in every team's client
# namespace, with rights over the paired server namespace.
RUNNER_SERVICE_ACCOUNT = "hokea-runner"

# Paths inside the runner pod (must match runner/Dockerfile + runner_entry).
WORK_MOUNT = "/work"
PROJECT_DIR = "/project"

# ConfigMaps hold ~1MiB; leave headroom for YAML/base64 overhead — the
# same budget the k8s backend's source ConfigMap uses.
CONFIGMAP_BUDGET = 900_000

# Client-namespace LimitRange: default 2 CPU / 2Gi, max 4 CPU / 8Gi.
DEFAULT_RUNNER_CPUS = 2.0
MAX_RUNNER_CPUS = 4.0
RUNNER_MEMORY = "2Gi"

# Tooling directories every project grows; skipped silently (anything
# ELSE skipped is named out loud, because only top-level files ship).
EXCLUDED_DIRS = {".venv", "venv", "runs", "__pycache__", ".hokea", "dist",
                 ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache",
                 ".idea", ".vscode", "node_modules", ".eggs"}

POLL_INTERVAL = 2.0        # seconds, all wait loops
POD_START_TIMEOUT = 300.0  # scheduling + container start
JOB_FINISH_TIMEOUT = 120.0 # after copy-out is signaled

IMAGE_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "ErrImageNeverPull"}


class LaunchError(RuntimeError):
    """A plain-English, student-facing failure. The CLI prints it bare."""


# -- pure pieces (unit-tested directly) --------------------------------------

def client_namespace_for(server_namespace: str) -> str:
    return f"{server_namespace}-clients"


def package_files(project_dir: Path) -> dict[str, Path]:
    """The project's top-level regular files, name -> path — what ships in
    the ConfigMap. Directories don't ship (same rule as `hokea k8s up`);
    well-known tooling dirs are skipped silently, anything else skipped
    gets named so a student with code in `lib/` finds out immediately."""
    files: dict[str, Path] = {}
    surprises: list[str] = []
    for entry in sorted(project_dir.iterdir()):
        if entry.name in EXCLUDED_DIRS:
            continue
        if not entry.is_file():
            surprises.append(entry.name)
            continue
        if not re.fullmatch(r"[-._a-zA-Z0-9]+", entry.name):
            raise LaunchError(
                f"file {entry.name!r} can't ship in a ConfigMap (names are "
                "letters, digits, '-', '_', '.') — rename it or move it "
                "out of the project's top level")
        files[entry.name] = entry
    if surprises:
        print(f"hokea: only top-level files ship to the cluster; skipping "
              f"{sorted(surprises)} — tests that import these will fail on "
              "the cluster", flush=True)
    if not files:
        raise LaunchError(
            f"nothing to ship: {project_dir} has no top-level files. Run "
            "`hokea test` from your project directory (the one with "
            "conftest.py and your test files).")
    sizes = {name: _shipped_size(path) for name, path in files.items()}
    total = sum(sizes.values())
    if total > CONFIGMAP_BUDGET:
        biggest = sorted(files, key=lambda n: sizes[n], reverse=True)[:5]
        listing = "\n".join(f"  {sizes[name]:>10,} bytes  {name}"
                            for name in biggest)
        raise LaunchError(
            f"your project's top-level files total {total:,} bytes as "
            f"shipped, and the ride to the cluster is a ConfigMap that "
            f"holds about 1 MiB (budget: {CONFIGMAP_BUDGET:,}). Biggest "
            f"offenders:\n{listing}\n"
            "Move big files into a subdirectory (only top-level files "
            "ship) or delete them and try again.")
    return files


def _shipped_size(path: Path) -> int:
    """What a file costs inside the ConfigMap: text ships verbatim, but a
    binary file lands in binaryData base64-encoded — 4 bytes on the wire
    for every 3 on disk — and the budget must count what actually ships."""
    raw = path.read_bytes()
    try:
        raw.decode()
        return len(raw)
    except UnicodeDecodeError:
        return (len(raw) + 2) // 3 * 4


def _labels(run_name: str) -> dict:
    return {"app.kubernetes.io/managed-by": "hokea",
            "hokea/test-run": run_name}


def configmap_manifest(run_name: str, files: dict[str, Path]) -> dict:
    data, binary = {}, {}
    for name, path in files.items():
        raw = path.read_bytes()
        try:
            data[name] = raw.decode()
        except UnicodeDecodeError:
            binary[name] = base64.b64encode(raw).decode()
    manifest = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": run_name, "labels": _labels(run_name)},
                "data": data}
    if binary:
        manifest["binaryData"] = binary
    return manifest


def job_manifest(run_name: str, *, server_namespace: str,
                 pytest_args: list[str], runner_cpus: float,
                 nodes: str | None = None,
                 takeover: str | None = None) -> dict:
    """The runner Job. backoffLimit 0: a failed pytest run must fail once,
    visibly, not retry into a fresh pod whose logs nobody watched.
    ttlSecondsAfterFinished: a finished Job whose launcher vanished
    (Ctrl-C, lost laptop) is garbage-collected an hour later instead of
    accumulating in the namespace."""
    cpus = f"{runner_cpus:g}"
    env = [{"name": "HOKEA_NAMESPACE", "value": server_namespace},
           {"name": "HOKEA_PYTEST_ARGS", "value": shlex.join(pytest_args)}]
    if nodes is not None:
        env.append({"name": "HOKEA_NODES", "value": nodes})
    if takeover is not None:
        env.append({"name": "HOKEA_TAKEOVER", "value": takeover})
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": run_name, "labels": _labels(run_name)},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": _labels(run_name)},
                "spec": {
                    "serviceAccountName": RUNNER_SERVICE_ACCOUNT,
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 5,
                    "containers": [{
                        "name": "runner",
                        "image": RUNNER_IMAGE,
                        # IfNotPresent is load-bearing: the image is
                        # side-loaded on the nodes, there is no registry
                        # to pull from.
                        "imagePullPolicy": "IfNotPresent",
                        "env": env,
                        "resources": {
                            "requests": {"cpu": cpus,
                                         "memory": RUNNER_MEMORY},
                            "limits": {"cpu": cpus,
                                       "memory": RUNNER_MEMORY}},
                        "volumeMounts": [{"name": "project",
                                          "mountPath": WORK_MOUNT,
                                          "readOnly": True}],
                    }],
                    "volumes": [{"name": "project",
                                 "configMap": {"name": run_name,
                                               # keep ./scripts runnable
                                               "defaultMode": 0o555}}],
                },
            },
        },
    }


def run_name_now(clock=time.localtime) -> str:
    """Timestamp plus a random suffix: two teammates launching in the same
    second must not race to create the same Job and ConfigMap."""
    stamp = time.strftime("hokea-test-%Y%m%d-%H%M%S", clock())
    return f"{stamp}-{secrets.token_hex(2)}"


# -- kubectl plumbing (stubbed in unit tests) ---------------------------------

def _kubectl(namespace: str, *argv: str, check: bool = True,
             timeout: float = 120):
    return _kubectl_sh("-n", namespace, *argv, check=check, timeout=timeout)


def _spawn_log_follower(namespace: str, pod: str) -> subprocess.Popen:
    """`kubectl logs -f`, stdout inherited: the student watches pytest
    live. stderr silenced — the follower racing pod deletion at the end
    would otherwise print an unhelpful 'error from server'."""
    return subprocess.Popen(
        ["kubectl", "-n", namespace, "logs", "-f", pod],
        stderr=subprocess.DEVNULL)


def _sleep(seconds: float):
    time.sleep(seconds)


# -- the launcher flow --------------------------------------------------------

def _pod_snapshot(client_ns: str, run_name: str) -> tuple[str, str, set] | None:
    """(pod name, phase, waiting-reasons) for the Job's pod, or None if it
    hasn't appeared yet."""
    result = _kubectl(client_ns, "get", "pods", "-l", f"job-name={run_name}",
                      "-o", "json", check=False)
    if result.returncode != 0:
        return None
    items = json.loads(result.stdout or "{}").get("items") or []
    if not items:
        return None
    pod = items[0]
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    reasons = {(s.get("state") or {}).get("waiting", {}).get("reason")
               for s in statuses}
    return (pod["metadata"]["name"],
            (pod.get("status") or {}).get("phase", ""),
            {r for r in reasons if r})


def _wait_for_pod(client_ns: str, run_name: str) -> str:
    """The Job's pod, once its container is past Waiting. Fails loud and
    specifically on ImagePullBackOff — that is the side-load failure mode:
    nothing on this cluster pulls from the internet, so a missing image
    stays missing until a human loads it."""
    said_creating = False
    deadline = time.monotonic() + POD_START_TIMEOUT
    while time.monotonic() < deadline:
        snap = _pod_snapshot(client_ns, run_name)
        if snap is not None:
            name, phase, reasons = snap
            if reasons & IMAGE_PULL_REASONS:
                raise LaunchError(
                    f"the runner image {RUNNER_IMAGE} is not loaded on the "
                    "cluster's nodes (the pod reports "
                    f"{sorted(reasons & IMAGE_PULL_REASONS)[0]}). This "
                    "cluster pulls nothing from the internet — the runner "
                    "image is side-loaded onto every node by the course "
                    "staff. Contact the instructor and ask them to load "
                    f"{RUNNER_IMAGE}; your command will work unchanged "
                    "afterwards.")
            if phase in ("Running", "Succeeded", "Failed"):
                return name
            if "ContainerCreating" in reasons and not said_creating:
                print("hokea: runner pod is being created...", flush=True)
                said_creating = True
        _sleep(POLL_INTERVAL)
    raise LaunchError(
        f"the runner pod did not start within {POD_START_TIMEOUT:.0f}s. "
        f"Look at it yourself: kubectl -n {client_ns} describe job "
        f"{run_name} (a Pending pod usually means the namespace is out of "
        "CPU quota — a teammate's run may be holding it).")


def _pod_phase(client_ns: str, pod: str) -> str:
    result = _kubectl(client_ns, "get", "pod", pod, "-o",
                      "jsonpath={.status.phase}", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _pod_container_exit_code(client_ns: str, pod: str) -> int | None:
    result = _kubectl(
        client_ns, "get", "pod", pod, "-o",
        "jsonpath={.status.containerStatuses[0].state.terminated.exitCode}",
        check=False)
    text = result.stdout.strip()
    return int(text) if result.returncode == 0 and text else None


def _exec(client_ns: str, pod: str, *argv: str, check: bool = True):
    return _kubectl(client_ns, "exec", pod, "--", *argv, check=check)


def _wait_for_artifacts(client_ns: str, pod: str) -> bool:
    """Until the runner touches .hokea-artifacts-ready. True when the
    marker is there (pod still holding); False if the pod terminated
    first — either the entrypoint crashed, or the hold window expired
    before we looked. No timeout of our own: pytest runs as long as it
    runs, and the student watching the live log can Ctrl-C. A quiet
    minute gets a reassurance line so a long suite doesn't look hung."""
    last_note = time.monotonic()
    phase = ""
    while True:
        probe = _exec(client_ns, pod, "test", "-f",
                      f"{PROJECT_DIR}/{READY_MARKER}", check=False)
        if probe.returncode == 0:
            return True
        phase = _pod_phase(client_ns, pod)
        if phase in ("Succeeded", "Failed"):
            return False
        if time.monotonic() - last_note >= 60:
            print(f"hokea: still waiting for pytest to finish "
                  f"(pod phase {phase or 'unknown'})", flush=True)
            last_note = time.monotonic()
        _sleep(POLL_INTERVAL)


def _read_exit_code(client_ns: str, pod: str) -> int:
    """Three tries with a short backoff: one blipped `kubectl exec` must
    not turn a finished run into a failure. By the time this runs the
    runs/ artifacts are already copied out, and the error says so."""
    text = ""
    for attempt in range(3):
        result = _exec(client_ns, pod, "cat",
                       f"{PROJECT_DIR}/{EXIT_CODE_FILE}", check=False)
        text = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"-?\d+", text or ""):
            return int(text)
        _sleep(0.5 * (attempt + 1))
    raise LaunchError(
        "the runner finished — its runs/ were already copied out — but "
        f"its exit-code file stayed unreadable (got {text!r}), so the "
        "run is treated as failed. The log above is still pytest's real "
        "output.")


def _copy_runs_out(client_ns: str, pod: str, dest: Path) -> list[str]:
    """The runner's runs/<run-dir>s into ./runs/, one directory at a time
    so unrelated local runs are never clobbered. Run-dir names embed a
    timestamp; on the freak collision, the incoming copy gets a suffix."""
    listing = _exec(client_ns, pod, "sh", "-c",
                    f"ls -1 {PROJECT_DIR}/runs 2>/dev/null", check=False)
    names = [n for n in listing.stdout.splitlines() if n.strip()]
    if not names:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    with tempfile.TemporaryDirectory(prefix="hokea-copyout-") as tmp:
        for name in names:
            staging = Path(tmp) / name
            result = _kubectl(
                client_ns, "cp",
                f"{client_ns}/{pod}:{PROJECT_DIR}/runs/{name}",
                str(staging), check=False, timeout=300)
            if result.returncode != 0:
                print(f"hokea: could not copy runs/{name} out of the pod:\n"
                      f"{result.stderr.strip()}", flush=True)
                continue
            target = dest / name
            if target.exists():
                target = dest / f"{name}-from-cluster"
            shutil.move(str(staging), str(target))
            copied.append(target.name)
    return copied


def _wait_for_job(client_ns: str, run_name: str):
    deadline = time.monotonic() + JOB_FINISH_TIMEOUT
    while time.monotonic() < deadline:
        result = _kubectl(client_ns, "get", "job", run_name, "-o", "json",
                          check=False)
        if result.returncode == 0:
            status = json.loads(result.stdout or "{}").get("status") or {}
            if status.get("succeeded") or status.get("failed"):
                return
        _sleep(POLL_INTERVAL)
    print("hokea: the Job did not report completion in time; continuing "
          "(the test result is already in hand)", flush=True)


def _cleanup(client_ns: str, run_name: str):
    _kubectl(client_ns, "delete", "job", run_name, "--ignore-not-found",
             "--wait=false", check=False)
    _kubectl(client_ns, "delete", "configmap", run_name, "--ignore-not-found",
             check=False)


def cmd_test(args) -> int:
    """The `hokea test` flow. Returns pytest's exit code (raises
    SystemExit with a plain-English message on launcher-side failures)."""
    server_ns = args.namespace or os.environ.get("HOKEA_NAMESPACE")
    if not server_ns:
        raise SystemExit(
            "hokea: which team? Pass --namespace <your team's namespace> "
            "or set HOKEA_NAMESPACE — the same value your conftest uses.")
    client_ns = args.client_namespace or client_namespace_for(server_ns)
    if not 0 < args.runner_cpus <= MAX_RUNNER_CPUS:
        raise SystemExit(
            f"hokea: --runner-cpus {args.runner_cpus:g} is outside what the "
            f"client namespace allows (more than 0, at most "
            f"{MAX_RUNNER_CPUS:g} — the namespace's LimitRange caps "
            "containers at 4 CPU / 8Gi).")
    pytest_args = list(args.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]

    try:
        files = package_files(Path.cwd())
    except LaunchError as e:
        raise SystemExit(f"hokea: {e}") from None

    run_name = run_name_now()
    manifests = [
        configmap_manifest(run_name, files),
        job_manifest(run_name, server_namespace=server_ns,
                     pytest_args=pytest_args, runner_cpus=args.runner_cpus,
                     nodes=os.environ.get("HOKEA_NODES"),
                     takeover=os.environ.get("HOKEA_TAKEOVER")),
    ]
    workdir = Path(".hokea") / "test"
    workdir.mkdir(parents=True, exist_ok=True)
    manifest_path = workdir / f"{run_name}.yaml"
    manifest_path.write_text(yaml.safe_dump_all(manifests, sort_keys=False))

    print(f"hokea: shipping {len(files)} file(s) and starting the runner "
          f"Job {run_name} in {client_ns} (servers will run in "
          f"{server_ns})", flush=True)
    try:
        _kubectl(client_ns, "apply", "-f", str(manifest_path))
    except KubectlNotFound as e:
        raise SystemExit(f"hokea: {e}") from None
    except CommandFailed as e:
        # The apply may have created the ConfigMap before the Job failed;
        # sweep both so a refused launch leaves nothing behind.
        _cleanup(client_ns, run_name)
        stderr = e.stderr.lower()
        if "not found" in stderr or "forbidden" in stderr:
            raise SystemExit(
                f"hokea: could not create the test Job in {client_ns!r}:\n"
                f"{e.stderr.strip()}\n"
                f"Does that namespace exist, and are you a member of the "
                f"team? Runs launch in your team's client namespace "
                f"(`{server_ns}-clients` next to `{server_ns}`); "
                "--client-namespace overrides it if yours is named "
                "differently.") from None
        raise SystemExit(f"hokea: creating the test Job failed:\n{e}") \
            from None

    follower = None
    exit_code: int | None = None
    keep = args.keep
    try:
        pod = _wait_for_pod(client_ns, run_name)
        print(f"hokea: streaming pytest output from pod {pod} "
              "(Ctrl-C detaches; the Job keeps running)", flush=True)
        follower = _spawn_log_follower(client_ns, pod)

        if _wait_for_artifacts(client_ns, pod):
            # runs/ first: a flaky exit-code read must never cost the
            # student their recorded artifacts.
            copied = _copy_runs_out(client_ns, pod, Path("runs"))
            if copied:
                print(f"hokea: copied {len(copied)} run(s) into ./runs/: "
                      f"{', '.join(copied)}", flush=True)
            else:
                print("hokea: the run recorded no runs/ artifacts to copy",
                      flush=True)
            try:
                exit_code = _read_exit_code(client_ns, pod)
            finally:
                # Release the runner's hold even when the read failed.
                _exec(client_ns, pod, "touch",
                      f"{PROJECT_DIR}/{COPYOUT_MARKER}", check=False)
                _wait_for_job(client_ns, run_name)
        else:
            # The pod ended before the ready marker appeared: entrypoint
            # crash, eviction, or the hold expired before we got here.
            exit_code = _pod_container_exit_code(client_ns, pod)
            if exit_code is None:
                raise LaunchError(
                    "the runner pod ended before pytest finished, and left "
                    "no exit code. Its log above is the best evidence; "
                    f"kubectl -n {client_ns} describe pod {pod} has the "
                    "rest (OOMKilled? evicted?).")
            message = ("the runner had already exited (its runs/ could "
                       "not be copied out)")
            if exit_code == 137:
                message += (" — exit 137 means the container was killed, "
                            "usually by the out-of-memory killer (the "
                            f"runner is capped at {RUNNER_MEMORY})")
            print(f"hokea: {message}", flush=True)
    except LaunchError as e:
        raise SystemExit(f"hokea: {e}") from None
    except KeyboardInterrupt:
        keep = True  # never delete a Job out from under a watching student
        print(f"\nhokea: detached. The Job is still running:\n"
              f"  watch it:  kubectl -n {client_ns} logs -f job/{run_name}\n"
              f"  clean up:  kubectl -n {client_ns} delete job,configmap "
              f"-l hokea/test-run={run_name}\n"
              "Detached means runs/ will NOT be copied back here: the "
              "finished runner holds them for ~10 minutes, then exits and "
              "they are gone. Within that window, either re-run `hokea "
              "test` or copy them yourself:\n"
              f"  kubectl -n {client_ns} cp "
              f"<pod-name>:{PROJECT_DIR}/runs ./runs\n"
              f"  (the pod: kubectl -n {client_ns} get pods "
              f"-l job-name={run_name})",
              flush=True)
        raise SystemExit(130) from None
    finally:
        if follower is not None:
            follower.terminate()
            try:
                follower.wait(timeout=10)
            except subprocess.TimeoutExpired:
                follower.kill()
        if not keep:
            _cleanup(client_ns, run_name)
        elif args.keep:
            print(f"hokea: --keep: leaving the Job and ConfigMap in place. "
                  f"Clean up later with:\n"
                  f"  kubectl -n {client_ns} delete job,configmap "
                  f"-l hokea/test-run={run_name}", flush=True)

    if exit_code == 0:
        print("hokea: tests passed", flush=True)
        return 0
    if exit_code == 5:
        print("hokea: pytest exited 5 — no tests collected. Did you run "
              "from the right directory? Only top-level files ship; test "
              "files in any skipped directories named above stayed home.",
              flush=True)
        return exit_code
    print(f"hokea: pytest exited {exit_code} — the output above is "
          "pytest's own", flush=True)
    return exit_code


def add_test_parser(sub: "argparse._SubParsersAction"):
    """Wire `hokea test` into the CLI's subparsers (called by cli.main)."""
    test = sub.add_parser(
        "test",
        help="run this project's pytest suite on the class cluster (a "
             "runner Job drives everything from inside)")
    test.add_argument("--namespace", default=None,
                      help="your team's SERVER namespace, where the system "
                           "under test runs (default: $HOKEA_NAMESPACE)")
    test.add_argument("--client-namespace", default=None,
                      help="where the runner Job itself runs "
                           "(default: <namespace>-clients)")
    test.add_argument("--runner-cpus", type=float,
                      default=DEFAULT_RUNNER_CPUS,
                      help="CPUs for the runner Job — the load generator, "
                           "not your servers (default: %(default)s, "
                           f"max {MAX_RUNNER_CPUS:g})")
    test.add_argument("--keep", action="store_true",
                      help="leave the Job and ConfigMap on the cluster "
                           "afterwards, for debugging")
    test.add_argument("pytest_args", nargs=argparse.REMAINDER,
                      metavar="pytest-args",
                      help="everything else goes to pytest verbatim "
                           "(put -- first if an argument collides with "
                           "hokea's own flags)")
    return test
