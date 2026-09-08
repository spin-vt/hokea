"""Run artifacts: what a test run writes to disk, and how it is read back.

These file formats are the stable contract of the framework — checks,
exporters, and any tools of your own all read them. A run directory holds:

    history.jsonl   one line per client operation
    faults.jsonl    the fault timeline (paired requested/done|verified events)
    events.jsonl    server-side events collected from node stdout
    states/         optional end-of-run state snapshot per node
    metadata.json   run configuration, clock anchor, completion marker
    report.json     throughput / latency / error-mix summary

All timestamps in history and faults are seconds since run start, measured on
the harness host's monotonic clock — one clock, which is what makes
"operation A finished before operation B started" a legitimate claim.
metadata.json carries {wall_at_start, mono_at_start} to recover wall time.
"""

import json
import time
from pathlib import Path

HISTORY_FIELDS = [
    "client_id", "seq", "node", "op", "key", "args",
    "invoke_ts", "return_ts", "outcome", "status", "response", "version", "error",
]
OUTCOMES = ("ok", "rejected", "unknown")


class FaultLog:
    """In-memory fault event list, timestamped on the harness monotonic clock.
    Cluster and Net record into it; run_workload writes the slice covering its
    run to faults.jsonl."""

    def __init__(self):
        self.events: list[dict] = []

    def record(self, event: str, **fields):
        self.events.append({"ts_mono": time.monotonic(), "event": event, **fields})


def new_run_dir(name: str, root: str | Path = "runs") -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(root) / f"{stamp}-{name}"
    n = 2
    while run_dir.exists():
        run_dir = Path(root) / f"{stamp}-{name}-{n}"
        n += 1
    run_dir.mkdir(parents=True)
    return run_dir


def write_jsonl(path: Path, records: list[dict]):
    with open(path, "w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f]


def validate_history(records: list[dict]):
    """Raise ValueError on the first malformed history record. Checks call this
    before rendering any verdict: malformed input must fail loudly, never skip."""
    seq_seen: dict = {}
    for i, r in enumerate(records):
        missing = [f for f in HISTORY_FIELDS if f not in r]
        if missing:
            raise ValueError(f"history line {i}: missing fields {missing}: {r}")
        if r["outcome"] not in OUTCOMES:
            raise ValueError(f"history line {i}: bad outcome {r['outcome']!r}")
        if r["return_ts"] is not None and r["return_ts"] < r["invoke_ts"]:
            raise ValueError(f"history line {i}: return_ts before invoke_ts: {r}")
        last = seq_seen.get(r["client_id"])
        if last is not None and r["seq"] <= last:
            raise ValueError(
                f"history line {i}: seq {r['seq']} not increasing for "
                f"client {r['client_id']} (last {last})"
            )
        seq_seen[r["client_id"]] = r["seq"]


class RunHandle:
    """Access to a finished run's artifacts. Everything is read from the files
    — any run can be re-analyzed later without re-running the cluster."""

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)

    @property
    def history(self) -> list[dict]:
        return load_jsonl(self.run_dir / "history.jsonl")

    @property
    def faults(self) -> list[dict]:
        return load_jsonl(self.run_dir / "faults.jsonl")

    @property
    def events(self) -> list[dict]:
        path = self.run_dir / "events.jsonl"
        return load_jsonl(path) if path.exists() else []

    @property
    def states(self) -> dict[int, dict]:
        """Per-node final state, keyed by node index. A node that could not be
        read appears with {"unreachable": true, ...} — checks must treat that
        as missing evidence, never as an empty state."""
        states_dir = self.run_dir / "states"
        if not states_dir.is_dir():
            return {}
        result = {}
        for path in sorted(states_dir.glob("node-*.json")):
            index = int(path.stem.split("-")[1])
            result[index] = json.loads(path.read_text())
        return result

    @property
    def metadata(self) -> dict:
        return json.loads((self.run_dir / "metadata.json").read_text())

    @property
    def report(self) -> dict:
        return json.loads((self.run_dir / "report.json").read_text())
