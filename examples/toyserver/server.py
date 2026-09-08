"""Toy key-value server used by hokea's own tests and the introductory lab.

Stdlib only. Each node keeps an in-memory dict and replicates writes to its
peers asynchronously, best-effort — which means it has honest, observable
weaknesses for hokea to find: a write acked while a peer is down never
reaches that peer, and a restarted node comes back empty with nobody
re-syncing it.

Meets the hokea contract: reads NODE_ID/PEERS, answers GET /health with 200.

API:
  GET  /health          -> 200 {"ok": true, "node_id": N}
  GET  /kv/<key>        -> 200 {"key": ..., "value": ...} or 404
  PUT  /kv/<key>        (body is the raw value) -> 200 {"ok": true}
  GET  /state           -> 200 {"<key>": "<value>", ...}   (whole store)

One knob, WORK (an env var, default 0): how many rounds of sha256 hashing
each /kv request burns before answering. It is a COST knob, not a bug knob —
it stands in for the real per-request work (parsing, validation, disk,
business logic) that a four-line toy handler doesn't have, so that a node
saturates at a realistic request rate instead of an absurd one. Replicated
writes pay it too, exactly like a real replica would.
"""

import hashlib
import http.client
import itertools
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE_ID = int(os.environ.get("NODE_ID", "1"))
PORT = int(os.environ.get("PORT", "8000"))
WORK = int(os.environ.get("WORK", "0"))
# PEERS is "node-1:8000,node-2:8000,..." including ourselves.
PEERS = [addr for i, addr in
         enumerate(os.environ.get("PEERS", "").split(","), start=1)
         if addr and i != NODE_ID]

store: dict[str, str] = {}
op_counter = itertools.count(1)


def work():
    # Burn WORK rounds of sha256: simulated per-request cost (see docstring).
    digest = b"hokea"
    for _ in range(WORK):
        digest = hashlib.sha256(digest).digest()


def emit(event: dict):
    # One write call per event: concurrent handler threads must never
    # interleave mid-line, or the event stream corrupts.
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def replicate(key: str, value: str):
    for peer in PEERS:
        host, port = peer.rsplit(":", 1)
        try:
            conn = http.client.HTTPConnection(host, int(port), timeout=2)
            conn.request("PUT", f"/kv/{key}", value, {"X-Replicated": "1"})
            conn.getresponse().read()
        except OSError:
            pass  # best effort: a dead peer just misses this write


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "node_id": NODE_ID})
        elif self.path == "/state":
            self._send(200, dict(store))
        elif self.path.startswith("/kv/"):
            work()
            key = self.path[len("/kv/"):]
            if key in store:
                self._send(200, {"key": key, "value": store[key]})
            else:
                self._send(404, {"error": "not_found", "key": key})
        else:
            self._send(404, {"error": "no_such_path"})

    def do_PUT(self):
        if self.path.startswith("/kv/"):
            work()
            key = self.path[len("/kv/"):]
            length = int(self.headers.get("Content-Length", 0))
            # Typed events on stdout: hokea collects any JSON line with a
            # "type" field, which lets check.event_join verify that every
            # acknowledged write was also applied.
            op_id = f"n{NODE_ID}-{next(op_counter)}"
            emit({"type": "recv", "op_id": op_id, "key": key})
            value = self.rfile.read(length).decode()
            store[key] = value
            emit({"type": "applied", "op_id": op_id})
            if "X-Replicated" not in self.headers:
                threading.Thread(target=replicate, args=(key, value),
                                 daemon=True).start()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "no_such_path"})

    def log_message(self, *args):
        pass  # keep stdout clean for hokea's event collection


class Server(ThreadingHTTPServer):
    # Python's default pending-connection queue is 5; real servers raise it
    # so a burst of clients queues briefly instead of being dropped (a
    # dropped connection attempt is retried by the OS only seconds later).
    request_queue_size = 128


if __name__ == "__main__":
    print(f"toyserver node {NODE_ID} listening on {PORT}", flush=True)
    Server(("0.0.0.0", PORT), Handler).serve_forever()
