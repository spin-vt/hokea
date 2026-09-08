"""Ambient server-side event collection.

Any line a node prints to stdout that parses as a JSON object with a "type"
field is captured into a run's events.jsonl, tagged with the node index.
Nothing is required of the system and there is no schema beyond that — but a
team that emits e.g. {"type":"ack","op_id":...} and {"type":"commit",...}
turns "nothing acked is ever lost" into a deterministic offline check
(check.event_join).

Within one node, event order is the log order (events.jsonl is grouped by
node). Across nodes, correlate by IDs in the events — never by comparing
timestamps.

Mechanics: one `docker logs --follow` stream per node is attached when the
cluster comes up — before any operations can exist, so a run never races the
attachment — and appends to a raw capture file for the cluster's lifetime.
A run records each file's byte offset at its start and takes what was
appended by its end. If a container restarts, the stream ends with it and
the collector re-attaches (via the cluster's state-change hook); lines
printed in the instants around a restart can be missed, which is why a
capture boundary may drop an event but never reorders any.
"""

import json
import subprocess
import time


class EventCollector:
    def __init__(self, cluster, capture_dir):
        self.cluster = cluster
        self.capture_dir = capture_dir
        self._procs: dict[int, subprocess.Popen] = {}
        self._files: dict[int, object] = {}
        self._stopped = False
        self.disabled = False

    def attach_all(self):
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        for node in self.cluster.nodes():
            self._attach(node)
            if self.disabled:
                return
        self.cluster.on_state_change(self._reattach_if_ended)

    def _attach(self, node):
        raw = open(self._raw_path(node.index), "ab")
        try:
            proc = subprocess.Popen(
                ["docker", "logs", "--tail", "0", "--follow", node.name],
                stdout=raw, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            print("hokea: docker not available; event collection disabled",
                  flush=True)
            raw.close()
            self.disabled = True
            return
        self._procs[node.index] = proc
        self._files[node.index] = raw

    def _reattach_if_ended(self, i: int):
        # A restarted container ended its old log stream; follow the new one.
        proc = self._procs.get(i)
        if self._stopped or proc is None or proc.poll() is None:
            return
        self._files[i].close()
        self._attach(self.cluster.node(i))

    def _raw_path(self, index: int):
        return self.capture_dir / f"node-{index}.raw"

    def offsets(self) -> dict[int, int]:
        """Current size of each node's capture — a run's starting boundary."""
        return {n.index: (self._raw_path(n.index).stat().st_size
                          if self._raw_path(n.index).exists() else 0)
                for n in self.cluster.nodes()}

    def drain(self, stable_for: float = 0.5, timeout: float = 5.0):
        """Wait for the capture files to stop growing before a read: the
        follower processes flush a beat behind the nodes, so the moment a
        workload ends there can still be lines in flight. Returns once
        every file's size has held still for `stable_for` seconds, or
        after `timeout` seconds regardless."""
        if self.disabled:
            return
        deadline = time.monotonic() + timeout
        sizes = self.offsets()
        unchanged_since = time.monotonic()
        while time.monotonic() < deadline:
            if time.monotonic() - unchanged_since >= stable_for:
                return
            time.sleep(0.05)
            current = self.offsets()
            if current != sizes:
                sizes = current
                unchanged_since = time.monotonic()

    def collect_since(self, offsets: dict[int, int]) -> list[dict]:
        """Events appended after `offsets`, grouped by node in log order.
        (A boundary can fall mid-line; that partial old line fails to parse
        and is dropped, which is correct — it predates the run.)"""
        events = []
        for node in self.cluster.nodes():
            path = self._raw_path(node.index)
            if not path.exists():
                continue
            tail = path.read_bytes()[offsets.get(node.index, 0):]
            for line in tail.splitlines():
                event = _parse_event(line)
                if event is not None:
                    events.append({**event, "node": node.index})  # our tag wins
        return events

    def stop(self):
        self._stopped = True
        for proc in self._procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in self._procs.values():
            proc.wait(timeout=10)
        for raw in self._files.values():
            raw.close()


def _parse_event(line: bytes) -> dict | None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(obj, dict) and "type" in obj:
        return obj
    return None
