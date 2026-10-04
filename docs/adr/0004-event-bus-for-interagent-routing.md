# 0004 — An event bus in SQLite as the inter-agent interface

- **Status:** accepted
- **Date:** 2026-10-04

## Context

The stated goal was to integrate this into a wider agent setup where several flows
consume the same discoveries. Those consumers run on their own schedules, and
more than one may want the same repo.

## Decision

Persist discoveries to an `events` table and let consumers read them with a
cursor, rather than wiring the flows together directly or pushing to a broker.

## Why

- **Decoupling.** A consumer polls when it wants. A new flow needs no change to
  the collector and no coordination with existing ones.
- **Replay after failure.** A consumer that dies mid-batch resumes from its last
  event id instead of losing work or re-reading everything.
- **The transactional boundary already exists.** A discovery run already writes
  within a transaction, so an event and the repo it describes commit together. An
  external broker would need an outbox pattern to avoid a window where the write
  succeeded and the message did not.
- **A broker is a service to run, secure and monitor.** The same reasoning as
  ADR 0001, applied to messaging.

## Alternatives rejected

- **Direct calls between flows.** Couples the collector to every consumer and
  fails the moment one of them is down.
- **Redis or NATS.** Better for high fan-out and pub/sub semantics, and
  unjustified for a handful of local consumers. Not in the standard library
  either.
- **Webhooks.** Inverts the dependency problem: the hub would need to know
  reachable endpoints, handle retries and dead-lettering, and every consumer
  would need public ingress. Polling a local database avoids all of it.

## Consequences

- Delivery is **at-least-once**. Consumers must be idempotent; the cursor makes
  that a caller responsibility, which is documented rather than hidden.
- A single `consumed_by` column means the bus is a *shared* consumption record.
  Consumers that must each see every event should keep their own `after_id`
  cursor rather than relying on `consumed_by`. This distinction is documented
  because it is easy to get wrong — it is the third of the three "seen" notions
  in OPERATIONS.md.
- Latency is bounded by poll interval, not by push. Acceptable for daily digests
  and periodic briefings; it would not suit a real-time dashboard.

## Risks

Silent lag if a consumer stops polling. Mitigated by watching
`stats.unconsumed_events` in `status`, which is expected to grow if a consumer
stops acking.
