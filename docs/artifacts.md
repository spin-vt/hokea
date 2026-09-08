# Artifact formats — the stable contract

Every run writes `runs/<timestamp>-<name>/`. These files are the interface:
checks read only these files, exporters translate them, and any run can be
re-analyzed later without re-running the cluster. (Nothing deletes old run
directories; for long or frequent runs they add up, so clear out the ones
you no longer need.) Load them with
`hokea.RunHandle(run_dir)` or read the files directly — they are JSONL
("JSON Lines": one JSON object per line).

## Clock

All `*_ts` / `ts` values are **seconds since run start**, measured on the
monotonic clock (one that only ever moves forward, immune to wall-clock
adjustments) of the machine running hokea. Because history and faults
share one clock, a timestamp in one file can be compared against a
timestamp in the other: "operation A finished before operation B started"
is a valid claim across files. `metadata.json` carries `wall_at_start`
(Unix epoch seconds — seconds since 1970) and `mono_at_start` to recover
wall time. Never compare server-side
timestamps across nodes; different machines' clocks drift.

## history.jsonl — one line per client operation

| field | meaning |
|---|---|
| `client_id` | 1-based worker id, stable for the whole run. `0` is reserved for the harness-side `Script` client (choreographed steps) |
| `seq` | per-client counter, strictly increasing (gaps appear when load-mode sampling is active) |
| `node` | 1-based index of the node the op targeted |
| `op` | operation kind, e.g. `"reserve"` |
| `key` | the logical object touched — nearly every check groups by it |
| `args` | the generator's arguments for this op |
| `invoke_ts` / `return_ts` | when the call started / returned. For an `unknown` op, `return_ts` is when the client *gave up*, not when anything took effect |
| `outcome` | `ok` \| `rejected` \| `unknown` — what is known about whether the op took effect; see docs/adapters.md |
| `status` | protocol status code, or null |
| `response` | the verbatim decoded reply, never summarized |
| `version` | server-supplied version token, or null |
| `error` | transport/classification error string, or null |

Ordered by `invoke_ts`. Every check first runs
`hokea.runs.validate_history`, which raises an error if the file does not
match this format.

In load mode (an explicit `rate` above 500 ops/s) per-op recording samples
down (`metadata.sample_p` < 1.0, announced at run time); `report.json`
statistics always cover every operation regardless.

## faults.jsonl — the fault timeline

One JSON object per event, `{"ts": ..., "event": ..., ...}`. Every fault is
**a pair of events**, because installing a fault takes measurable time and
operations inside that gap cannot be attributed to either side:

- process faults: `kill` / `kill_done` (also `stop`, `start`, `pause`,
  `unpause`), with `node`.
- partitions: `partition` (requested) then `partition_verified` — written
  only after probes proved traffic blocked across the cut and alive within
  groups. Carries `groups` and any `skipped_nodes` (not running, so not
  probed). A `partition_verified` with `reapplied_after: <node>` records
  re-verification after a node restarted mid-partition.
- netem (the Linux network-emulation tool behind `net.slow`): `slow` /
  `slow_verified` (with the measured probe round-trip time), and
  `heal` / `heal_done` (with `dirty_nodes` that couldn't be cleaned yet —
  paused or stopped; they are swept when they return).
- `settle` / `settle_done`: the quiet period `run_workload(settle=N)` waits
  between the last client finishing and the state snapshots — so a
  `converged` verdict can be read as "after N quiet seconds".

`check.availability` opens each fault's window at the `*_done`/`*_verified`
event and closes it at the matching recovery request.

## events.jsonl — server-side events

Any line a node prints to **stdout** (its normal console output) that
parses as a JSON object with a
`"type"` field, plus `"node"` (hokea's tag; it overwrites a same-named field
from the event). Grouped by node, in log order. **Within one node, order is
log order; across nodes, correlate by ids in the events — never by
timestamps.** Collection follows `docker logs` (`kubectl logs` on the
Kubernetes backend) from the moment the cluster
comes up (before any operations can exist); each run takes the slice
appended during it. When a node restarts, its stream is re-attached: events
printed in the instants around a restart can be missed — a capture boundary
may drop an event but never reorders any.

## states/ — end-of-run snapshots

`node-<i>.json`, one per node, produced by your state reader after the
workload ends, under whatever fault state the run left in place (capturing
*while partitioned* is often what you want). A node that could not be read is
recorded as `{"unreachable": true, "error": ...}`; checks treat that as
missing evidence, never as an empty state.

## metadata.json / report.json

`metadata.json`: the run configuration (clients, rate, assign, timeout,
seed, node URLs, cluster prefix), the clock anchor, `sample_p`, and
`"completed": true` written only when the run finished — its absence marks
a partial run. `report.json`: total ops, throughput, ok-latency
percentiles (p50 = median, p99 = the time only the slowest 1% exceed),
outcome/status/error mixes, aggregated over all operations.

## Runs without history.jsonl

Run directories produced by `record_run` (no client workload — batch jobs,
training) contain everything above except `history.jsonl` and `report.json`.
A directory whose `metadata.json` lacks the `completed` marker is a partial
run: the recording block raised or the harness died; its artifacts are still
readable but should not be trusted as a full account.
