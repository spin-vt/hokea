"""Exporters: convert a run's history into formats that battle-tested
external checkers consume. hokea deliberately ships no linearizability
checker of its own — converters are mechanically testable, checkers are not.

- porcupine: JSON loadable by the vendored Go example (examples/porcupine/),
  which feeds it to Porcupine, the standard Go linearizability checker.
- jepsen: an EDN history in Jepsen's conventional shape (:invoke/:ok/:fail/
  :info pairs), for tools in that ecosystem.

Both are total: every history operation appears, nothing is summarized. The
outcome mapping is the load-bearing part:
  ok       -> the operation completed (:ok / return time recorded)
  rejected -> the operation provably did not happen (:fail; a model may
              drop it entirely)
  unknown  -> outcome not known (:info; return time null = still open — the
              checker must consider it concurrent with everything after its
              invocation)
"""

import json
import re
from pathlib import Path

from .runs import load_jsonl, validate_history


def export_porcupine(run_dir: str | Path) -> Path:
    """Write `<run_dir>/porcupine.json`: {"format": ..., "ops": [...]} with
    call/return in integer nanoseconds and return null for unknown-outcome
    ops."""
    run_dir = Path(run_dir)
    history = load_jsonl(run_dir / "history.jsonl")
    validate_history(history)
    ops = [{
        "client": r["client_id"],
        "op": r["op"],
        "key": r["key"],
        "args": r["args"],
        "call_ns": int(r["invoke_ts"] * 1e9),
        # An unknown-outcome op's recorded return_ts is when the client gave
        # up, not when the op took effect — it stays open for the checker.
        "return_ns": (int(r["return_ts"] * 1e9)
                      if r["outcome"] != "unknown" else None),
        "outcome": r["outcome"],
        "response": r["response"],
    } for r in sorted(history, key=lambda r: r["invoke_ts"])]
    out = run_dir / "porcupine.json"
    out.write_text(json.dumps({"format": "hokea-porcupine-v1", "ops": ops},
                              indent=1))
    return out


OUTCOME_TO_JEPSEN = {"ok": "ok", "rejected": "fail", "unknown": "info"}


def export_jepsen(run_dir: str | Path) -> Path:
    """Write `<run_dir>/jepsen.edn`: a vector of Jepsen-style history entries,
    one :invoke and one completion entry per operation, ordered by time."""
    run_dir = Path(run_dir)
    history = load_jsonl(run_dir / "history.jsonl")
    validate_history(history)
    entries = []
    for r in history:
        value = {"key": r["key"], **r["args"]}
        entries.append({"type": "invoke", "f": Keyword(r["op"]),
                        "process": r["client_id"],
                        "time": int(r["invoke_ts"] * 1e9), "value": value})
        entries.append({"type": OUTCOME_TO_JEPSEN[r["outcome"]],
                        "f": Keyword(r["op"]), "process": r["client_id"],
                        "time": int(r["return_ts"] * 1e9),
                        "value": r["response"]})
    entries.sort(key=lambda e: e["time"])
    for i, e in enumerate(entries):
        e["index"] = i
        e["type"] = Keyword(e["type"])
    out = run_dir / "jepsen.edn"
    out.write_text(edn_dumps(entries) + "\n")
    return out


# ---------------------------------------------------------------------------
# Minimal EDN support — exactly the subset the Jepsen exporter emits.
# edn_loads exists so the round-trip is testable without Clojure.
# ---------------------------------------------------------------------------

class Keyword(str):
    """A string rendered as :keyword in EDN."""


def edn_dumps(value) -> str:
    if isinstance(value, Keyword):
        return f":{value}"
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # EDN strings escape like JSON strings
    if isinstance(value, list):
        return "[" + " ".join(edn_dumps(v) for v in value) + "]"
    if isinstance(value, dict):
        pairs = (f"{_edn_key(k)} {edn_dumps(v)}" for k, v in value.items())
        return "{" + ", ".join(pairs) + "}"
    raise TypeError(f"cannot render {type(value).__name__} as EDN: {value!r}")


def _edn_key(k) -> str:
    if isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-.]*", k):
        return f":{k}"
    return json.dumps(str(k))  # e.g. a seat id like "14C": a string key


def edn_loads(text: str):
    value, rest = _edn_parse(text.strip())
    if rest.strip():
        raise ValueError(f"trailing EDN content: {rest[:40]!r}")
    return value


def _edn_parse(text: str):
    text = text.lstrip(" ,\n")
    if text.startswith("["):
        items, text = [], text[1:]
        while not text.lstrip(" ,\n").startswith("]"):
            item, text = _edn_parse(text)
            items.append(item)
        return items, text.lstrip(" ,\n")[1:]
    if text.startswith("{"):
        result, text = {}, text[1:]
        while not text.lstrip(" ,\n").startswith("}"):
            key, text = _edn_parse(text)
            value, text = _edn_parse(text)
            result[key] = value
        return result, text.lstrip(" ,\n")[1:]
    if text.startswith('"'):
        end = 1
        while text[end] != '"' or text[end - 1] == "\\":
            end += 1
        return json.loads(text[:end + 1]), text[end + 1:]
    token = ""
    for ch in text:
        if ch in " ,\n]}":
            break
        token += ch
    rest = text[len(token):]
    if token.startswith(":"):
        return Keyword(token[1:]), rest
    if token == "nil":
        return None, rest
    if token in ("true", "false"):
        return token == "true", rest
    try:
        return int(token), rest
    except ValueError:
        return float(token), rest
