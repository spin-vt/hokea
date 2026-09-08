"""What runs inside the runner image — the `hokea test` Job's process.

The launcher (`hokea test`, src/hokea/runner.py) ships the student's
project as a ConfigMap and creates a Job from the runner image; this
module is that Job's entrypoint. The two sides meet over a tiny marker
protocol in the runner's working directory:

1. The project's files are mounted read-only at /work. Copy them into a
   writable /project and cd there (pytest and hokea both want to write:
   caches, runs/).
2. Run `pytest <HOKEA_PYTEST_ARGS>` (shlex-split), output unbuffered so
   `kubectl logs -f` streams it live.
3. Write pytest's exit code to `.hokea-exit-code`, then touch
   `.hokea-artifacts-ready` — the signal the launcher's exec loop waits
   for. Order matters: the exit code must be readable the moment the
   ready marker exists.
4. Hold — poll for `.hokea-copyout-done` for up to HOKEA_HOLD_SECONDS
   (default 600) — so the launcher can `kubectl cp` the recorded runs/
   out of the pod before it disappears. The launcher touches the marker
   when it is done; a vanished launcher just costs the hold window.
5. Exit with pytest's code (0 stays 0), so the Job's Complete/Failed
   status agrees with the test outcome.

Everything here is plain files and subprocess — no Kubernetes API — so
the whole protocol is unit-testable with a tmp directory.
"""

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

WORK = Path("/work")          # read-only ConfigMap mount (launcher's side)
PROJECT = Path("/project")    # writable copy we actually run in

READY_MARKER = ".hokea-artifacts-ready"
EXIT_CODE_FILE = ".hokea-exit-code"
COPYOUT_MARKER = ".hokea-copyout-done"
DEFAULT_HOLD_SECONDS = 600.0


def copy_project(work: Path, project: Path):
    """The mounted files into a writable directory. A ConfigMap volume
    surfaces each file as a symlink next to `..data`/`..2026_...`
    bookkeeping entries — follow the links, skip the bookkeeping. The
    mount is read-only (defaultMode 0o555) and shutil preserves modes, so
    the copies are re-chmodded writable: pytest rewrites files it owns
    (cache entries, and some plugins touch the tests themselves)."""
    project.mkdir(parents=True, exist_ok=True)
    for entry in sorted(work.iterdir()):
        if entry.name.startswith(".."):
            continue
        if entry.is_dir():
            shutil.copytree(entry, project / entry.name, dirs_exist_ok=True)
        elif entry.is_file():
            shutil.copy(entry, project / entry.name)
    for path in project.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            executable = path.stat().st_mode & 0o111
            path.chmod(0o755 if executable else 0o644)


def run_pytest(args: list[str], cwd: Path) -> int:
    """pytest under the same interpreter, output inherited (so the pod
    log IS the live pytest output). -u keeps it unbuffered even if the
    image's PYTHONUNBUFFERED were lost."""
    return subprocess.call(
        [sys.executable, "-u", "-m", "pytest", *args], cwd=cwd)


def hold_for_copyout(project: Path, hold_seconds: float,
                     poll: float = 1.0) -> bool:
    """Wait for the launcher's copy-out signal, up to hold_seconds.
    True if it came, False if the hold expired."""
    deadline = time.monotonic() + hold_seconds
    marker = project / COPYOUT_MARKER
    while True:
        if marker.exists():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(poll, hold_seconds))


def main() -> int:
    work = Path(os.environ.get("HOKEA_WORK", WORK))
    project = Path(os.environ.get("HOKEA_PROJECT", PROJECT))
    copy_project(work, project)
    os.chdir(project)

    args = shlex.split(os.environ.get("HOKEA_PYTEST_ARGS", ""))
    print(f"hokea runner: running: pytest {' '.join(args) or '(no args)'}",
          flush=True)
    code = run_pytest(args, project)

    # Exit code first, THEN the ready marker: the launcher reads the code
    # as soon as it sees the marker, and must never catch a half-state.
    (project / EXIT_CODE_FILE).write_text(f"{code}\n")
    (project / READY_MARKER).touch()

    hold = float(os.environ.get("HOKEA_HOLD_SECONDS", DEFAULT_HOLD_SECONDS))
    print(f"hokea runner: pytest exited {code}; artifacts ready — holding "
          f"up to {hold:.0f}s for the launcher to copy runs/ out",
          flush=True)
    if hold_for_copyout(project, hold):
        print("hokea runner: copy-out confirmed; exiting", flush=True)
    else:
        print(f"hokea runner: no copy-out signal after {hold:.0f}s; "
              "exiting anyway (runs/ in this pod is lost)", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
