# Writing an adapter (and an operation generator)

The *adapter* is a piece of code you write to allow hokea to drive your
project; it's critical that this code is correct as a mistake here can poison
your results. Thankfully, adapters should only be about 30-40 lines of code.
Read this whole page before writing your own!

## The contract

```python
class MyAdapter:
    def execute(self, op: Op, node, timeout: float) -> Result:
        ...
```

Execute exactly one operation against exactly one node (`node.url` is its
base URL, `node.index` its 1-based id), using your system's own protocol —
HTTP, gRPC, your client library, anything. Return a `Result`.

## The three outcomes — what they mean and how to choose

The outcome is not a status code. It records **what you know about whether
the operation took effect on the system**: yes, no, or can't tell.

- **`ok`** — yes: the server acknowledged it ("done"). Not merely "the
  request got a response": a 500 ("internal server error") is a response
  but is not an acknowledgment.
- **`rejected`** -- no, provably: the server itself said no, *and nothing
  changed, and nothing ever will* because of this request: e.g.,  HTTP 409 "seat
  taken", 400 bad request, or a validation error.
- **`unknown`** — can't tell. Timeouts, dropped connections, any 5xx
  server error, "no quorum", anything ambiguous. **When in doubt, use this.** Lots of
  unknowns during faults is a run behaving normally, not dirty data.

To classify a reply, try asking yourself three questions, in order:

1. **Did you get an answer at all?** If not -- timeout, dropped connection --
   don't classify anything, just let your `execute()` raise an unhandled
   underlying exception. hokea will catch it and record `unknown` itself.
2. **Did the server say "done"?** Then `ok`.
3. **Did the server say "no" — and are you certain nothing changed?** Both
   parts, then `rejected`. Any hesitation about the second part: `unknown`.

Why the caution concentrates on `rejected`: checks and exporters treat a
rejected operation as one that *never happened* — e.g., the Porcupine exporter
literally deletes them from the history it hands the checker. A wrong
`unknown` just makes conclusions more cautious; a wrong `rejected`
fabricates evidence, and every later conclusion built on it is confidently
wrong.

These mistakes can be subtle. During testing of hokea, I accidentally let hokea
classify a server's 503 Service Unavailable response (which reflects a
temporary server error) as a rejection: the server said it wasn't able to
handle the request, after all. But then the recorded runs said otherwise: a
write that had been rejected with the 503 showed up on all replicas because the
server that sent the 503 had applied it locally before noticing it did not have
the required quorum, and then a later resync applied the write elsewhere.  **If
your server can say "no" after doing something, its no is an `unknown`.**

For HTTP-based protocols, `http_result` makes the safe choice the default:

```python
from hokea import http_result
...
return http_result(r, rejected={400, 409})
```

2xx becomes `ok`; only the status codes you deliberately listed become
`rejected`; **everything else — including the 503 you didn't think about —
becomes `unknown`**. Build a `Result` by hand for non-HTTP protocols or
special cases.

Two more rules:

1. **Never catch transport errors** (question 1 above). If your client
   library wraps transport failures in its own exception type, let that
   escape too. Any non-transport exception crashes the run with an error,
   which is better than a silent misclassification.
2. **A read that observes "not found" is `ok`.** The read *happened* and
   observed an absence. `rejected` would erase a legitimate observation.

Extras on `Result`:

- `response` — the verbatim decoded reply. Never summarize it; checks and
  exporters need the whole thing.
- `version` — if your system returns a global version/revision token (like
  etcd's revision) pass it through; it enables `check.session`. Omit it
  otherwise.
- `status` — the protocol status code, for availability error mixes.

There are **no retries**: hokea attempts each operation exactly once and
records it exactly once. If a client retried after a timeout, the history
could no longer say whether an operation happened once or twice, and no
check could reason about it. If your protocol needs retries, model each
retry as its own operation in the generator.

## The operation generator

```python
def my_ops(client_id: int, rng: random.Random):
    while True:
        result = yield Op("reserve", seat, {"client_id": f"c{client_id}"})
        if result.outcome == "ok":
            yield Op("confirm", seat,
                     {"rid": result.response["reservation_id"]})
```

- It's a plain Python generator, one per client, run in that client's worker
  process. hokea sends each op's `Result` back in, so flows that depend on
  a previous response (ids, tokens) read naturally. If you don't need the
  result, just `yield Op(...)`.
- Use only the passed-in `rng` for randomness — that's what makes
  `seed=` reproduce a workload shape.
- `Op(op, key, args)`: `key` is the logical object the operation touches
  (seat id, cart id, document key). Checks group by it; choose it carefully.
- `time.sleep()` inside the generator is fine and useful (think time —
  it also means operations are in flight when your faults hit).

## Choreographed steps: the Script client

Some bugs need a specific sequence rather than random load: "cut the
network, then delete on the majority side, then write on the minority
side, then look." A random workload only produces such a sequence by
luck. For these, use a `Script`, a scripted client that hokea itself runs,
whose steps run inside the fault schedule:

```python
script = Script(MyAdapter())
h = run_workload(cluster, my_ops, MyAdapter(), duration=30, script=script,
                 faults=[(10, lambda: net.partition([[1, 2], [3]])),
                         (0,  lambda: script.do(Op("delete", "cart-7"), node=1)),
                         (0,  lambda: script.do(Op("read", "cart-7"), node=3))])
```

Schedule entries run **in list order**, each no earlier than its offset, and
each only after the previous returned — `net.partition()` returns only once
the cut is verified, so an offset of `0` means "immediately after that",
with no guessing how long installation takes. `script.do` returns the live
`Result` (use ids from `result.response` in later steps), applies the same
outcome rules as worker clients, and is recorded in `history.jsonl` under
the reserved `client_id` 0, so checks and exporters see scripted ops like
any others. Script operations are not counted as load: `report.json`
excludes them.

## Settling before judgment

Systems that sync in the background need some quiet time before their
nodes agree; that is normal, not a bug. `run_workload(..., settle=5)` waits five
quiet seconds after the clients finish before final states are captured,
and records the wait in the fault timeline. Without it, a perfectly healthy
eventually-consistent system can fail `check.converged` purely because the
snapshot came too early.

## The state reader (optional, recommended)

```python
def read_state(node) -> dict:
    ...  # this node's view of the data, WITHOUT contacting its peers
```

Used at the end of a run to snapshot every node for `check.converged` and
`check.acked_visible`. What those checks consume is **what each individual
server believes** — divergence between servers is exactly what they detect.
Keys of the returned dict are the same `key` space your ops use.

**If your servers answer client reads from their own local state** (many
designs do), point the state reader at the ordinary client API and you're
done. That is the most trustworthy option, because it is the same
interface real clients use, and there is no separate endpoint that could
report something different from what clients see.

**If a server interacts with its peers to return a result to a client**
(quorum reads, a coordinator merging replica views), the client API will
not reflect the state held by the server you asked — which is important
for the convergence and durability checks, because the merged answer hides
exactly the per-server differences they exist to find, and during a
partition the coordinator may refuse to answer at all. In these cases,
your implementation should expose endpoints with identical semantics to
the client read API (e.g. `GET /debug/cart/<id>` mirroring
`GET /cart/<id>`) that answer only from state held on that node at the
moment of the call — never by contacting peers. Mirror the client API
one-for-one: same paths under a `/debug` prefix, same response shapes,
ideally the same serialization code with the peer coordination skipped —
that way the state reader is just your client reader with a different URL
prefix, and there is no second schema to invent or to drift. This may not
be necessary depending on the type of system you are building, but for any
system where answering a client request requires responses from multiple
server nodes, it is strongly recommended.

A debug state endpoint must meet three requirements, each of which you
can test:

1. **It never contacts peers.** It must answer, quickly, while fully
   partitioned. A test can call it during a verified partition, and if it
   stalls or errors there, it is contacting peers after all.
2. **It covers the same key space your operations use.**
3. **It agrees with the client API when the system is healthy.** After a
   heal and a settle, what clients read must match what the per-node
   states predict. Put that comparison in one test, so the endpoint cannot
   drift away from what clients see without you noticing.

## Worked example

`examples/toyserver/test_toy.py` contains a complete adapter and operation
generator for the introductory lab's toy key-value server, which
[the lab](lab.md) walks through, outcome classification included.
Start from it: swap in your system's endpoints and status codes, then go
through the three outcome rules above for every reply your server can
produce.
