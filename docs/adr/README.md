# Architecture Decision Records

Short, dated records of the decisions that shaped `hermes-signal-hub`. Each one
states the context, the choice, the alternatives actually rejected, and what it
costs.

| # | Decision | Status |
|---|---|---|
| [0001](0001-sqlite-over-postgres.md) | SQLite over Postgres | accepted |
| [0002](0002-stdlib-only.md) | Standard library only, zero dependencies | accepted |
| [0003](0003-velocity-over-stars.md) | Rank on velocity, not absolute stars | accepted |
| [0004](0004-event-bus-for-interagent-routing.md) | An event bus in SQLite as the inter-agent interface | accepted |

## Why these four

Three of them are constraints discovered by spike rather than preferences chosen
up front:

- **0002** — the environment has no `pip`. This was found by trying, not assumed,
  and it determines the entire stack.
- **0001** — the measured volume (hundreds of repos, one writer) makes a server
  database pure operational cost.
- **0003** — the literal request would have produced a list of famous old
  projects. Ranking had to change to answer the actual question.
- **0004** — decided by the requirement that other agent flows consume the same
  discoveries independently.

## Operational decisions that are *not* here

Defects found and fixed during development are not architectural decisions. They
are written up as **Lessons learned** in [../OPERATIONS.md](../OPERATIONS.md),
because a regression test is a better record than an ADR: each of the five
passed the unit tests and was only caught by running against the real API.
