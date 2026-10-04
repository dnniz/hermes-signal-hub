# 0005 — A cursor per consumer, not a shared `consumed_by` column

- **Status:** accepted
- **Date:** 2026-10-05
- **Supersedes:** part of [0004](0004-event-bus-for-interagent-routing.md), which
  specified a shared `consumed_by` ack

## Context

ADR 0004 chose a SQLite event bus so several agent flows could consume the same
discoveries independently. That decision was right, but the original ack
mechanism could not deliver it.

`events.consumed_by` is a single nullable text column: one consumer per event.
With two flows, whichever read first set the value and the other got nothing:

```
daily-digest reads events 1-10  → consumed_by = 'daily-digest'
morning-ai  reads events 1-10  → empty
```

The failure is silent and misreads as "MorningAI is behind" rather than "the bus
cannot fan out". It only becomes visible once a second consumer exists, which is
exactly when it is most expensive to discover.

## Decision

Add a `consumer_cursors` table — one row per flow, holding the last event id that
flow has handled. Reading and confirming are separate operations:

- `read_events_for(consumer)` is a pure read and never moves the cursor
- `advance_cursor(consumer, ids)` moves it, and only forward

Exposed as `signalhub consume <name> [--advance]`, `signalhub cursors`,
`GET /consume?consumer=NAME&advance=1` and `GET /cursors`.

`mark_consumed` is kept, documented as a single-consumer tool, so existing
callers keep working.

## Why

- **A cursor is the natural shape of this problem.** Consumers are independent
  readers of a log; that is a cursor, not a claim.
- **At-least-once delivery with a replayable failure mode.** Because reading
  does not advance, a consumer that crashes mid-batch sees the same events again
  instead of losing them. `rewind_cursor` makes deliberate replay one call.
- **Lag becomes attributable.** `cursors` reports per-consumer pending counts, so
  a stalled flow is distinguishable from a busy one. With a single shared column
  you cannot tell which flow stopped.
- **It is the standard pattern.** Kafka, Postgres `LISTEN/NOTIFY` consumers and
  every queue cursor do this. Inventing a different mechanism would be a cost
  with no benefit.

## Alternatives rejected

- **A real broker (Redis, NATS, SQS).** Would also solve fan-out, and adds a
  service to run, secure, monitor and back up. The whole point of ADR 0004 was to
  avoid that. The cursor table gets the correctness property without the
  operational cost.
- **Broadcast events, e.g. a `consumed_by` JSON list per event.** Reads and
  rewrites grow with the number of consumers, every read touches every row, and
  it still conflates "read" with "finished".
- **Per-consumer copies of the event table.** Duplicates the log; the events are
  the expensive part.
- **Let each flow keep its own offset file.** Works, but puts the state outside
  the database, where `cursors` and the HTTP API cannot see it, and it is one
  more file to lose or forget to back up.

## Consequences

- Schema version moves 1 → 2. The new table is created by the existing
  `CREATE TABLE IF NOT EXISTS` path, so the migration is automatic and needs no
  separate step. Verified on a populated database.
- A brand-new consumer starts at event 0 and therefore replays the entire
  history on its first run. That is correct for a new flow but noisy against a
  large backlog, so a consumer should be seeded with
  `advance_cursor(name, [max_event_id])` when it should start from now.
- `events.consumed_by` still exists and is now partly redundant. It is kept
  because it answers a different question ("did anyone handle this?") that the
  cursor table does not.
- Two mechanisms now track "seen", alongside `repos.status`. All three are
  documented in [../OPERATIONS.md](../OPERATIONS.md#idempotency-three-different-meanings-of-seen),
  because confusing them is the original bug in a new form.

## Risks

A consumer that never advances will read an ever-growing backlog, in batches,
forever. `GET /cursors` surfaces it as a growing `pending` count. Accepted: the
failure is visible and the fix is one command.
