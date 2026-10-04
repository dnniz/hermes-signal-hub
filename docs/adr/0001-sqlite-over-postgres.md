# 0001 — SQLite over Postgres

- **Status:** accepted
- **Date:** 2026-10-04

## Context

The hub stores repository state, per-run observations, an event log and a
feedback history. It is written by a short-lived collector process and read by
one HTTP server plus a cron digest. Measured real volume from a first run:
~270 repos and ~270 events, growing slowly over weeks.

## Decision

Use SQLite via the standard library `sqlite3` module, in WAL mode.

## Why

- **The write rate is one collector at a time.** SQLite is single-writer by
  design, which is a liability for concurrent ingestion and irrelevant here: a
  second `collect` would be a bug, not a feature.
- **WAL gives concurrent readers during a write.** This is the property that
  actually matters: the HTTP server reads while a collector writes, and WAL mode
  makes that safe without a second process coordinating.
- **The data fits in memory by a wide margin.** Hundreds of repos, not millions.
  A server database would add an operational dependency — a process to run, a
  connection to secure, credentials to rotate — for no measurable gain.
- **It is in the standard library**, which is a hard constraint here (ADR 0002).

## Alternatives rejected

- **Postgres.** Correct choice at 100x the data or with several concurrent
  writers. Both conditions are false here, and it would add a service to
  supervise. If the hub is ever embedded in a platform that already runs one,
  the schema maps over almost unchanged: the tables are already normalised and
  the primary keys are already `full_name` and `id`.
- **DuckDB.** Excellent for analytics, wrong tool for a write-then-read
  workload with a mutation loop, and not in the standard library.
- **A JSON file.** No transactions, no concurrent readers, and the feedback
  learner mutates rows on every verdict.

## Consequences

- Backups are file copies. Under WAL, `signal.db`, `-wal` and `-shm` must be
  taken together, or use `sqlite3 .backup`.
- A single `collect` process is an operational invariant, not just a
  recommendation.
- If write concurrency ever becomes real, this is the component to swap, and
  ADR 0002's zero-dependency constraint would be the first thing to revisit.

## Risks

None material at current scale. Revisit if repos exceed ~100k, if a second
writer appears, or if reads start blocking writes.
