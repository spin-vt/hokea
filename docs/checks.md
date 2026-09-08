# The five checks — what they see, and what they don't

A run leaves behind a directory of files: the **history** (the ordered
log of every operation your clients sent, with the server's exact reply),
one **state snapshot** per node (everything that node stored, copied at
the end of the run), the fault timeline (when each fault started and
ended), and any events your servers printed. A check is a function that
reads only those files and answers one question about the run. It does
not need the cluster, so you can run it on an old run any time and get
the same answer.

Each check returns a **verdict**: a one-word answer plus a one-sentence
reason. The answer is three-way. `violated` means the run proves the
property was broken; `clean` means the run shows no violation;
`insufficient_evidence` means the run did not contain enough data to
decide. The third answer is there on purpose: a check with nothing to
look at says so instead of quietly saying `clean`, so missing data never
looks like a pass. A `violated` verdict also carries the exact recorded
operations that prove it, so you can confirm the conclusion by hand.
Parameters and return values are on [the API page](api/checks.md); this
page is about what the checks mean.

## What each check answers

| check | question | needs |
|---|---|---|
| `check.availability(history, faults)` | during each fault window, per side: success rate, latencies, error mix | history + fault timeline |
| `check.converged(states)` | do all nodes report the same final state? | state snapshots |
| `check.acked_visible(history, states, write_ops=[...])` | is every acknowledged write's key present in every node's final state? | history + state snapshots |
| `check.event_join(events, require=, for_each=, on=)` | does every event of one type have a matching event of another? | server-emitted events |
| `check.session(history)` | within each client, do server-supplied versions ever go backwards? | version tokens in responses |

Some words from the table. An **acknowledged write** is a write the
server answered "ok" to, so the client was told it succeeded; its **key**
is the name the write stored data under. A node's **final state** is its
state snapshot; nodes have **converged** when their final states are all
the same. **Success rate** is the fraction of operations answered "ok",
**latency** is how long an operation took to get an answer, and the
**error mix** is which kinds of error came back and how often. An
**event** is a line a server prints to report something that happened
inside it (one JSON object per line, with a `"type"` field), collected
during the run. A **version token** is a number the server includes in
each reply saying how up to date the data it answered from was. Windows
and sides are explained in item 5 below.

When you would use each one in your project:

- `availability` — you claim the system keeps serving clients while
  something is broken, and you want the numbers that show it.
- `converged` — you claim that once the faults are over and the system
  has caught up, every copy of the data agrees.
- `acked_visible` — you claim that once a client is told "ok", a crash or
  a network cut will not lose that data.
- `event_join` — you claim something a client cannot see from outside
  (say, "every acknowledged write was also copied to more than half the
  nodes"), and your servers can print an event each time it happens.
- `session` — your server returns a version token with every reply, and
  you claim a client never sees an older version than one it already
  saw. Only for a token that is one counter for the whole store, not one
  per key.

## What they can catch: one worked example

Take a leader-based replicated store (every node keeps a copy of the
data): one node is the leader, every write goes to it, and it copies the
write to the other nodes (Raft is one such design; we will cover it in
lecture). Suppose it has two common bugs. First, every node answers
reads from its own local copy, without checking whether that copy is up
to date. Second, the leader tells clients "ok" as soon as it has written
locally, without waiting for a **majority** of nodes (more than half) to
have the write too.

Now **partition** the cluster in the middle of a run: cut the network so
the leader can reach only a **minority** of nodes (fewer than half), and
the rest, the majority, can reach only each other. The majority notices
the leader has gone quiet, elects a new one, and keeps going. The old
leader does not know it was replaced and keeps acknowledging writes.

Both bugs show up in the verdicts. `converged` is `violated`, because the
old leader's final state is stale (it stopped receiving the majority's
writes) while the majority moved on. `acked_visible` is `violated`,
because the old leader acknowledged writes the majority never received.
Those writes were provably lost: a client was told "ok", and the data is
gone. The verdict carries the exact recorded operations that prove it.

## What they cannot see — read this before trusting a clean verdict

1. **No linearizability.** Linearizability is the guarantee that every
   operation appears to take effect at one instant between when it was
   sent and when it returned, as if operations happened one at a time (we
   will cover it in lecture). None of the five checks reasons about the
   order in which overlapping operations happened. So a system can pass
   all five and still break linearizability: a read returns stale data a
   few milliseconds after a write that overlapped it in time, the nodes
   catch up, and nothing at the end of the run looks wrong. The first bug
   in the worked example produces stale reads that no check flags on its
   own; it was caught only because the partition held the stale copy in
   place until the snapshots were taken. If you claim linearizability,
   export the run with `hokea export --format porcupine` and check it with
   Porcupine, an external tool that checks whether a recorded history
   could have happened one operation at a time (`examples/porcupine/` is a
   worked model).

2. **Existence by default — and why.** The rule: `acked_visible` checks
   that each acknowledged write's key exists in every node's final state;
   it does not check that the value is right.

    Why: a write that *creates* something makes a claim nothing later can
    legitimately erase, except a delete, so "does it exist?" has an answer
    no matter what order things happened in. A write that *sets a value*
    only has to survive until a later write legitimately replaces it, so
    judging the final value means knowing which write came last. With
    several clients writing at once, "last" often has no defined answer:
    two overlapping writes may land in either order, and both outcomes are
    correct. hokea ships no check that tries to decide that ordering; for
    claims that depend on it, export the history to Porcupine.

    In your own test:

    - Choose `write_ops` (the operation names the check treats as writes)
      so that "acknowledged" implies "should still exist": operations that
      create things, not holds that expire, not things a test later
      deletes.
    - Many bugs live one level below the key: the shopping cart exists,
      but an item's quantity is wrong, or a deleted item came back. To
      test those, remove the ordering question: after the faults and the
      main workload (the stream of client operations the test runs), issue
      one write (or delete) whose result you know, using a `Script` step (a
      single operation you place at a chosen point in the timeline; see
      [Adapters](adapters.md)); give the system `settle` time (quiet
      seconds with no client traffic); only then let the snapshots be
      taken. Now "which write was last" is something you arranged, not
      something the check has to work out.
    - For that final comparison, pass `visible=` to `acked_visible`: your
      own one-line test for "is this write's effect present in this node's
      state", such as value equality, or `key not in state` for a delete
      that must stick. Use a value-level `visible` only for writes you
      arranged to be last; on ordinary concurrent traffic it will flag
      correct systems.
    - Later traffic can mask loss. If a node loses data and clients keep
      writing, they may recreate the key, and the check will find it there
      and say `clean`. The verdict cannot tell you this happened, so
      prevent it by test design: when you test that data survives a crash,
      stop writes to those keys before the crash.

3. **Convergence is not correctness.** All nodes agreeing on a wrong state
   is `clean` for `converged`, because the check only compares nodes to
   each other. Pair it with `acked_visible` (did the agreed state keep the
   acknowledged writes?) and with checks of your own for rules specific to
   your project.

4. **Narrow windows need events.** Some bugs are visible for only a few
   milliseconds: a server acknowledges a write and crashes before the
   write reaches any other node, so the bug exists only in the gap between
   the "ok" and the crash. Even a workload designed to hit that gap
   catches it once in several runs. Two remedies. First, keep the node
   with the stale view alive but cut off, so the gap stays open for
   seconds; that is what the lab's partition test does. Second, have your
   servers emit events such as `ack` (we told the client "ok") and
   `commit` (the write is safely on a majority), and use
   `check.event_join` to confirm every `ack` has a matching `commit`. That
   turns a bug that depends on timing into a check that gives the same
   answer every time you run it on the recorded files.

5. **Availability numbers are descriptive.** `availability` never returns
   a verdict; it returns numbers, and your test asserts against them. It
   cuts the history into **windows**: one per fault, from when hokea
   confirmed the fault had taken hold until the fault was removed, plus a
   `baseline` window before the first fault. Inside each window it sorts
   operations into **sides** by the node each was sent to: `majority` and
   `minority` for a two-way partition, `faulted-node` and `others` for a
   fault on one node (killed, stopped, paused, or slowed). Each side
   reports its operation count, success rate, and latencies. Be careful
   what you assert: a system that chooses consistency over availability
   under a partition (it would rather refuse requests than let the two
   sides give different answers; we will cover this tradeoff in lecture)
   *must* refuse writes on the minority side, so asserting a high success
   rate there would fail a correct system. Also, reading `success_rate`
   (or latencies) off a side with zero recorded operations stops your test
   with an `InsufficientEvidence` error instead of returning a number: a
   rate over zero operations means nothing, and in a run where the fault
   hit the whole system a side may well be empty. Check the side's `.n`
   (its operation count) first if a side can legitimately be idle.

6. **The state reader is part of what you have to trust.** The rule:
   `converged` and `acked_visible` believe whatever the state reader (the
   part of your adapter, the code that tells hokea how to talk to your
   server, that fetches one node's state for the snapshot) returns.

    Why: these checks exist to find differences between nodes. If the
    state reader reports something other than what one node actually
    holds, the differences are hidden before the check sees them, and a
    `clean` verdict says nothing.

    In your own test:

    - If each server answers reads from its own local copy, read state
      through the same client API your users use; a separate debug
      endpoint can report something different from what clients see.
    - If answering a client read involves several servers (asking a
      majority, or merging their replies), the client API shows the merged
      answer, which hides exactly the differences these checks look for.
      Add a `/debug` endpoint (a URL path your server answers) that mirrors
      the client API but answers only from that node's own state, never by
      contacting other nodes.
    - Hold that endpoint to the three requirements in
      [Adapters](adapters.md): it answers while partitioned, it covers the
      same keys your operations use, and it agrees with client reads once
      the network cut is removed and the system has had `settle` time.

Some kinds of bug no generic check will catch: a client that retries after
a timeout and ends up with two of something it asked for once (a check
would need to know which requests were retries); an internal log that
grows without bound (a number visible only from inside the server); a
decision based on a node's local clock (from outside, all you see is
stale reads, covered in item 1); and any single stale read while the
cluster is healthy. Write project-specific checks, or emit events, for the
properties you actually claim.
