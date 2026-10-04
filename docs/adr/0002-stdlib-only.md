# 0002 — Standard library only, no third-party dependencies

- **Status:** accepted
- **Date:** 2026-10-04

## Context

The target environment has no `pip` and no package index reachable. Installing
dependencies is not possible without root, which the runtime does not have. This
was discovered by spike, not assumed.

## Decision

Implement everything on the Python 3.13 standard library: `urllib` for HTTP,
`sqlite3` for storage, `http.server` for the API, `unittest`/pytest for tests,
`argparse` for the CLI. Zero third-party runtime dependencies.

## Why

- **It is the only thing that actually runs here.** Not a preference — a
  constraint discovered by trying.
- **A clone works with no install step.** `./scripts/signalhub` adds `src/` to
  `sys.path` and runs. There is no `pip install`, no lockfile, no venv to drift
  out of sync.
- **The surface needed is small.** A REST client, a database driver and an HTTP
  server are all first-class in the standard library. This problem did not need
  more.
- **It is inspectable.** Anyone can read the whole system with no transitive
  dependency to audit.

## Alternatives rejected

- **httpx + FastAPI + SQLAlchemy.** The obvious default. Each is genuinely
  better than its stdlib counterpart for its own problem — none of which are
  this problem. Unavailable here regardless.
- **requests.** Would have been the minimal deviation, if anything were
  installable. `urllib` covers the needed surface including custom headers and
  error handling.
- **Vendoring dependencies into the repo.** Avoids the install problem while
  inheriting all the maintenance cost and the licence surface, for a problem that
  did not need it.

## Consequences

- FTS5 full-text search is used because SQLite ships it, not because it is the
  best option. The `json1` extension is **not** used — it is not reliably
  present — which shaped how filters are written in `store.py`.
- The HTTP server is `http.server`, which is single-threaded by default. Good
  enough for one local consumer; it would need a real server before handling
  concurrent load.
- Hand-rolled retry and backoff logic lives in `github.py`. This is the main
  place where a dependency would have paid for itself, and it is contained to one
  module precisely so it could be swapped later.

## Risks

The custom retry/backoff layer is the most likely source of subtle bugs. It is
mitigated by bounding it and treating exhaustion as a normal outcome rather than
an exception.
