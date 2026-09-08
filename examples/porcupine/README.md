# Porcupine worked example: per-key KV register

hokea deliberately ships no linearizability checker — deciding linearizability
means searching over orderings of concurrent operations, exactly where subtle
checker bugs produce silently wrong verdicts. Instead, export the history and
let [Porcupine](https://github.com/anishathalye/porcupine) (the standard Go
linearizability checker) decide:

```
hokea export --format porcupine runs/<your-run>
cd examples/porcupine
go run . ../../runs/<your-run>/porcupine.json
```

Prints `linearizable: true` (exit 0) or `linearizable: false` (exit 1).

Go is required only here; the rest of hokea never needs it. The first
`go run` downloads the Porcupine module.

## Adapting it to your system

This model checks a **key-value register** with `put`/`get` operations (the
shape of hokea's toy server). Edit three things for your system:

1. `kvInput` / `kvOutput` — what an operation and its reply carry.
2. `kvModel.Step` — your system's sequential behavior: given a state and an
   operation, is this output legal, and what is the next state?
3. `toOperations` — how exported ops map into the model. Keep the outcome
   rules exactly as written: **rejected** ops are dropped (they provably did
   not happen), **unknown-outcome reads** are dropped (they observed
   nothing), **unknown-outcome writes** stay open with no return time (they
   may take
   effect at any later moment). Weakening these rules produces confident
   wrong verdicts.

The model is validated in hokea's own test suite: a
known-linearizable history — including an unknown-outcome write later observed
by a read — passes, and a history with a stale read fails.
