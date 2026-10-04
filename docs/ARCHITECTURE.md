# Architecture

`signalhub` is a stdlib-only Python package that discovers newly created GitHub
repositories with real star velocity, scores them, stores the history, and
publishes them to a local event bus that other Hermes flows consume. Human
verdicts feed back into the scoring weights.

The design constraint that shapes everything: **Python standard library only, no
third-party runtime dependencies** (`pyproject.toml:19`, `dependencies = []`).
The HTTP layer is `urllib`, the store is `sqlite3`, the server is
`http.server`, and there is no async runtime — the whole client is blocking by
design because a collection run is a short-lived cron process, not a request
handler (`src/signalhub/github.py:5-7`).

---

## 1. Component and data flow

```
                    ┌───────────────────────────────────────────────┐
                    │  GitHub REST Search API  (api.github.com)      │
                    │  /search/repositories   30 req/min (search)    │
                    │  /rate_limit           5000 req/hour (core)    │
                    └───────────────────────▲───────────────────────┘
                            HTTPS, Bearer token, urllib
                    ┌───────┴───────────────────────┐
                    │  github.py                    │
                    │  GitHubClient                 │  budget + throttle
                    │  ├ RequestBudget (hard cap)   │  + retry/backoff
                    │  ├ RateLimitState (headers)   │
                    │  └ RepoSnapshot (frozen)      │
                    └───────┬───────────────────────┘
                            │ list[RepoSnapshot]
        ┌───────────────────▼───────────────────────┐
        │  DISCOVER                                    │
        │  collector.py  Collector.collect()         │  adaptive star floor
        │  ├ build_queries(floor)                    │  + max_search_calls
        │  ├ dedup by full_name                      │
        │  └ drop is_archived / is_fork              │
        └───────────────────┬───────────────────────┘
                            │ CollectionResult
        ┌───────────────────▼───────────────────────┐
        │  SCORE                                      │
        │  scoring.py  Scorer.score(repo, previous)  │  velocity,
        │  ├ velocity / engagement / relevance /     │  engagement,
        │  │ developer  (each 0..1)                  │  relevance,
        │  └ weighted sum × penalty_scale            │  developer
        └───────────────────┬───────────────────────┘
                            │ list[ScoredRepo]
        ┌───────────────────▼───────────────────────┐
        │  STORE                                      │
        │  store.py  Store.upsert_repos()            │  SQLite, WAL, FTS5
        │  ├ repos (upsert)                          │
        │  ├ observations (append, one per sighting) │
        │  ├ events (append-only log)                │
        │  └ runs (run lifecycle)                    │
        └───────────────────┬───────────────────────┘
                            │ RunReport{scored, new_count, ...}
        ┌───────────────────▼───────────────────────┐
        │  PUBLISH  Store.publish(..., run_id)       │
        │  events rows: repo.discovered / repo.updated
        └───────┬───────────────────────┬───────────┘
                │                       │
   ┌────────────▼──────────┐  ┌─────────▼──────────────────────────┐
   │ RENDER (render.py)     │  │ SERVE (server.py, http.server)     │
   │ render_markdown       │  │ GET /health /repos /jsonl /events  │
   │ render_jsonl          │  │ GET /digest /search                │
   │ render_agent_briefing │  │ GET /stream  (SSE, ?once=1 finite) │
   │ render_stats          │  │ POST /collect, POST /feedback      │
   └────────────┬──────────┘  └─────────┬──────────────────────────┘
                │                       │
        ┌───────▼────────┐      ┌───────▼────────┐
        │  CLI (cli.py)  │      │  HTTP + SSE    │
        │  human digest  │      │  other agents  │
        └────────────────┘      └────────────────┘

   ── FEEDBACK CLOSE-LOOP (offline, any time) ───────────────────────────────
        cli.py: record_verdict / decide      server.py: POST /feedback
                     │                                  │
                     └──────────┬───────────────────────┘
                                │  Store.add_feedback(verdict, component)
                                │  Store.record_verdict(...)
                    ┌───────────▼───────────────┐
                    │  feedback table            │  append-only audit
                    │  (full_name, verdict,      │
                    │   component, actor, score) │
                    └───────────┬───────────────┘
                    ┌───────────▼───────────────┐
                    │  scoring.FeedbackLearner  │  per-component accumulator
                    │  .update(verdict, comp)   │  applied proportionally,
                    │  .weights()               │  NO global rescale
                    └───────────┬───────────────┘
                    ┌───────────▼───────────────┐
                    │  meta['weights'] (JSON)   │  ← persisted, read by
                    │  Store.set_meta()         │    Scorer on next run
                    └───────────────────────────┘
```

### Run pipeline, step by step

`SignalHub.collect()` (`src/signalhub/hub.py`) is the single entry point that
chains the pipeline:

1. `Collector.adaptive_floor(known_repos)` — pick the star floor.
2. `Collector.build_queries(floor)` — compose bounded search slices.
3. `Collector.collect()` — run the slices, dedup, filter archived/forked.
4. For each snapshot, read the **previous** observation for that repo and call
   `Scorer.score(repo, previous=...)`.
5. `Store.upsert_repos(...)` — upsert `repos`, append `observations`, emit
   `events` for the new ones.
6. `store.finish_run(run_id, ...)` — close the run lifecycle row.

Everything after step 6 (render, serve, publish to other flows) reads from the
store; nothing is recomputed from the network.

---

## 2. Module map

| Module | Responsibility | Key public API | Depends on |
|---|---|---|---|
| `github.py` | REST client, rate-limit accounting, normalisation | `GitHubClient`, `RepoSnapshot`, `RateLimitState`, `RequestBudget`, `GitHubError`, `SecondaryRateLimit`, `BudgetExceeded`, `SearchClient` (Protocol) | stdlib only |
| `collector.py` | Query funnel: which slices to run, at what star floor, with what ceiling | `Collector`, `CollectorConfig`, `CollectionResult`, `TOPIC_QUERIES`, `default_config`, `last_run_floor`, `hours_since`, `freshness_sla`, `prune_observations` | `github` |
| `scoring.py` | Score components, weights, feedback learning | `Scorer`, `ScoreWeights`, `ScoredRepo`, `PreviousObservation`, `FeedbackLearner`, `Verdict`, `Component` | `github` (types only) |
| `store.py` | SQLite persistence, FTS5 search, event bus, run lifecycle | `Store`, `RepoRecord`, `SearchEvent` | stdlib `sqlite3` |
| `hub.py` | Orchestrator: token resolution, run lifecycle, feedback application | `SignalHub`, `RunReport`, `resolve_token` | `github`, `collector`, `scoring`, `store` |
| `render.py` | Output shapes: markdown, JSONL, agent briefing, stats | `render_markdown`, `render_jsonl`, `render_agent_briefing`, `render_stats`, `Digest`, `TELEGRAM_MESSAGE_LIMIT` | `scoring` |
| `cli.py` | argparse front end, global `--db`, subcommands | `main` | all of the above |
| `server.py` | `http.server` HTTP + SSE read API and write endpoints | `build_server`, `SignalHubHandler` | `hub`, `store`, `render` |

Dependency direction is strictly one-way: `cli`/`server` → `hub` → {`collector`,
`scoring`, `store`} → `github`. Nothing in `github.py` knows the store exists,
and nothing in `store.py` knows the network exists.

### Public surface

`src/signalhub/__init__.py` re-exports the stable surface:
`SignalHub`, `Collector`, `CollectorConfig`, `Scorer`, `ScoreWeights`,
`ScoredRepo`, `PreviousObservation`, `FeedbackLearner`, `Store`, `RepoRecord`,
`Digest`, `GitHubClient`, `RepoSnapshot`, `RateLimitState`, `RequestBudget`,
`GitHubError`, `SecondaryRateLimit`, `BudgetExceeded`, `RunReport`,
`render_markdown`, `render_jsonl`, `render_agent_briefing`, `render_stats`,
`resolve_token`, `__version__`.

---

## 3. Data model

`Store.__init__` creates the schema on first open and enables the pragmas
described below. Everything is one file on disk, default
`/opt/data/.signalhub/signal.db`.

### Tables

**`repos`** — the current state of one repository (one row per `full_name`).

| Column | Notes |
|---|---|
| `full_name` | `owner/name`, primary identity, used as the upsert conflict key |
| `owner`, `name` | split from `full_name` for display and filtering |
| `html_url`, `description` | as returned by Search |
| `stars`, `forks`, `open_issues` | current counters |
| `created_at`, `pushed_at` | GitHub timestamps, ISO-8601 |
| `topics` | JSON array, round-tripped to a tuple on read |
| `license` | SPDX id or `None` |
| `subscribers` | watcher count, an engagement input |
| `is_archived`, `is_fork` | filtered at collection time, stored for auditing |
| `first_seen`, `last_seen` | set by the store, not by GitHub |
| `seen_count` | number of sightings; incremented on every upsert |
| `observations` | number of rows this repo has in `observations` |
| `star_delta` | `stars` minus the previous observation's stars |
| `status` | `new` → `seen`, the human-digest dedup flag |
| `score` | the last computed score |
| `components` | JSON of the per-component breakdown, for the JSONL/briefing views |

**`observations`** — append-only time series, one row per sighting of a repo.
This table *is* the momentum signal: paired with the immediately preceding row it
yields a star delta over a known time gap.

| Column | Notes |
|---|---|
| `full_name` | the repo |
| `observed_at` | run timestamp |
| `stars`, `forks`, `open_issues` | counters as seen at that moment |
| `score` | score at that moment |

**`runs`** — run lifecycle. `start_run(queries)` opens a row, `finish_run(...)`
closes it with `status` (`ok` / `error`), `candidates`, `new_repos`, `api_calls`,
`notes`, `finished_at`. `last_successful_run()` only returns `status='ok'` rows,
so an errored run never becomes the reference state for the next run's floor
recovery.

**`events`** — the event bus. Append-only, `(id, run_id, full_name, url, kind,
score, payload, created_at, consumed_by)`. `kind` is `repo.discovered` or
`repo.updated`. `read_events(after_id=...)` is an **exclusive** cursor
(`store.py`), `read_events(unconsumed_only=True)` returns what no consumer has
acked, and `read_events(run_id=N)` pins to one discovery run — the property the
daily digest depends on (`tests/test_store.py:261-280`).

**`feedback`** — the verdict audit trail: `full_name`, `verdict` (`yes` / `no` /
`noise`), `component`, `actor`, `score`, `created_at`. Append-only; the learner
reads it to rebuild weights, and `feedback_counts()` gives the per-verdict
totals.

**`meta`** — key/value with a JSON payload. This is where the learned weights
live, under the key `weights`, alongside the persisted `rate_limit` snapshot
and other small state.

**`repos_fts`** — the FTS5 virtual table over name, description and topics,
kept in sync by triggers on insert/update/delete of `repos`.

### Indices

Indices exist for the access patterns that actually run in production:

- `repos(status)`, `repos(score)`, `repos(stars)`, `repos(last_seen)` — the
  digest filter and the default orderings used by `list_repos` / `rank`.
- `repos(created_at)` — the `since_days` filter.
- `observations(full_name, observed_at)` — the previous-observation lookup and
  the prune scan.
- `events(run_id)`, `events(created_at)`, and a partial index on
  `consumed_by IS NULL` — the unconsumed read that every consumer hits.
- `feedback(full_name)` and `feedback(verdict)` — verdict lookups and counts.

`list_repos` validates its `order_by` against a whitelist and falls back to the
default on anything unknown, so an injection attempt degrades instead of
executing (`tests/test_store.py:62-67`).

### Why WAL

`PRAGMA journal_mode=WAL` is set on open. The workload is a short writer (one
collection run) plus concurrent readers: the cron job writes while a digest
renders, or an HTTP `/health` is polled. In WAL mode readers do not block the
writer and the writer does not block readers, and a commit is a sequential
append to the WAL rather than a whole-file rewrite. This is what allows a
read-heavy consumer to run while a collection is in flight. The trade-off is
that WAL mode does not work over a network filesystem — the DB file must be on
local disk, which is the deployment reality here.

### Why FTS5

Search is *over what the hub already found*, not over GitHub. FTS5 gives
tokenised matching on name + description + topics, which a `LIKE '%x%'` scan
cannot do at index level, and it is compiled into CPython's `sqlite3`. The
write side is kept honest by triggers, so a plain `upsert_repos` keeps the index
current without an extra step. There is a deliberate fallback: a malformed FTS
expression must not raise, so `search()` catches the syntax error and falls back
to a `LIKE` scan (`tests/test_store.py:108-112`).

---

## 4. Why SQLite and not Postgres

The honest reason: **this workload does not need a server**.

| Property | Postgres | SQLite (chosen) |
|---|---|---|
| Writers | Many concurrent, row-level locking | **One writer at a time**; readers are concurrent |
| Deployment | A server process, credentials, a network hop, backups | One file |
| Dependencies | Client driver (forbidden by the stdlib-only constraint) | `sqlite3` ships with CPython |
| FTS | `tsvector` + GIN, server-side | FTS5, in-process |
| Operational cost | Daemon, upgrades, connection pool | None |

The single-writer limitation is real and is the strongest argument *against*
SQLite. It is acceptable here for concrete reasons:

- **The writer is a cron job.** There is exactly one collection process, run
  once a day (`scripts/daily_digest.sh`). The hub never runs two writes
  concurrently: `Store.transaction()` opens a write transaction and the run
  completes before the process exits. So the write lock is uncontended.
- **Readers never block the writer** (WAL), so the HTTP server and the digest
  can read while the cron run is mid-flight.
- **Write volume is tiny.** A run stores hundreds of repos and a few hundred
  observation rows. Postgres is sized for orders of magnitude more.
- **Durability is a file copy.** `cp signal.db signal.db.bak` is the backup
  procedure. There is no separate backup system to operate.
- **stdlib-only forbids the client driver anyway.** `psycopg` is a third-party
  dependency; `pyproject.toml:19` is explicitly empty.

Where SQLite would stop being enough: multiple concurrent collector processes
writing at once, a dataset in the millions of rows, or a need for concurrent
writers with row-level conflict resolution. At that point the seam is
`Store` — the only module that knows SQLite exists.

---

## 5. The momentum model

The insight the project is built on (`SKILL.md`): sorting GitHub search by stars
surfaces repos that accumulated stars for years. What is wanted is *what is
moving right now* — a 900-star MCP server created six days ago beats a 30k-star
toolchain from 2019.

Momentum is not stored as a field. It is **derived at scoring time** from two
observations of the same repo:

```
                 run N-1                                run N
  observations   ┌──────────────┐                    ┌──────────────┐
  rows for       │ stars = 1000 │   ── gap ──▶      │ stars = 1180 │
  a/one          │ t = t0       │                    │ t = t1       │
                 └──────────────┘                    └──────────────┘
                          │                                   │
                          └────────────┬──────────────────────┘
                                       ▼
                        Scorer.score(repo, previous=PreviousObservation(...))
                                       │
                                       ▼
                             velocity = stars/day  (+ the previous star count
                             feeds the "already popular" discount)
```

`Store` provides the previous observation for each repo; the hub passes it to
`Scorer.score()` as a `PreviousObservation`. The scorer then computes a
**stars-per-day** velocity, where the day count is derived from the repo's
age (or from the gap to the previous observation when one exists).

**Why the first sighting has no delta.** A delta needs two points. On the first
run a repo is inserted with no prior row, so `previous` is `None` and there is
nothing to subtract. That is not a degraded mode — it is the correct answer:

- Star *count* is known on day 0, and the age-based stars/day still works, so
  the repo is scored and ranked normally.
- The *delta* is what the human digest shows as growth. Showing `+0` or a
  fabricated delta for a repo seen for the first time would be a lie. The
  renderer prints the delta only when there is one.
- The store records the first observation, so the very next run has a real
  baseline and a real delta. Momentum becomes available one run after
  discovery, by construction.

So: first sighting = ranked on velocity from age, no delta line. Second
sighting onward = a genuine measured delta. The test suite pins the
cross-observation behaviour: `tests/test_store.py:46-51`
(`test_star_delta_across_observations`, `100 → 180` yields `star_delta == 80`).

---

## 6. Feedback flow

```
 user/agent  ──▶  decide owner/repo yes|no|noise      (cli.py)
                    │
                    ├─▶ record_verdict()  → repos.status 'new' → 'seen'
                    │                      → publish repo.updated event
                    │
                    └─▶ add_feedback(verdict, component)
                                │
                                ▼
                          feedback table (append-only)
                                │
                                ▼
                   FeedbackLearner.update(verdict, component)
                                │   per-component accumulator, applied
                                │   proportionally — never rescaled
                                ▼
                       Store.set_meta("weights", {...})
                                │
                                ▼
                  Scorer(weights=...) on the NEXT run
```

Three separate notions of "seen" live in the store and mixing them up is the
classic bug (`scripts/daily_digest.sh:8-18`):

| Notion | Field | Purpose |
|---|---|---|
| Machine consumption | `events.consumed_by` | The event bus, for other flows |
| Human digest dedup | `repos.status` (`new` → `seen`) | The daily digest |
| Run attribution | `events.run_id` | Which discovery run produced an event |

A bare `yes`/`no` does not say *which axis* failed, so `auto_component` picks
the weakest component — `relevance` before `velocity`, with a 0.7 threshold. The
full algorithm, the weight maths and the exact convergence behaviour are in
[SCORING.md](SCORING.md).

Persisted weights live in `meta` under the key `weights` as JSON, so the
learned state survives restarts and the whole thing is inspectable with the
`weights` subcommand. `--reset` restores the defaults.

---

## 7. Consumption interfaces

Three shapes, one data source. All of them read the store; none of them call
GitHub.

| Interface | Command / route | Output | Use when |
|---|---|---|---|
| **CLI — human digest** | `signalhub --db DB digest --limit 10` | Telegram-safe markdown, pre-formatted | A human reads it. No parsing, no pipes. |
| **CLI — agent briefing** | `signalhub --db DB rank --format briefing` | Plain text with `components:` breakdown and the literal `decide` command | Handing work to a delegated subagent that must then call back with a verdict |
| **CLI — machine** | `signalhub --db DB --json events --unconsumed` | JSON array of events | Feeding another Hermes flow; ack with `events --consume <actor>` |
| **CLI — JSONL** | `signalhub --db DB rank --format jsonl` | One JSON object per line, full components, `rank` field | Bulk processing, `jq`, spreadsheets |
| **HTTP** | `GET /repos?limit=N` | JSON | Another agent that wants a live query surface |
| **HTTP** | `GET /digest?limit=N` | The same markdown as the CLI | Remote rendering without a local install |
| **HTTP** | `GET /jsonl?limit=N` | Same JSONL | Remote bulk processing |
| **HTTP** | `GET /events?limit=N` | Event bus reads | Remote machine consumer |
| **HTTP** | `GET /search?q=...` | FTS5 results over what was already found | "Have we seen something like this?" without spending quota |
| **HTTP — SSE** | `GET /stream?once=1&limit=N` | `text/event-stream` | Scripted reads. **With `once=1` the stream is finite and closes with EOF**; without it, it is an open stream by design |
| **HTTP** | `POST /collect` | Run report | Trigger a run remotely |
| **HTTP** | `POST /feedback` | Recorded verdict | Verdict over HTTP instead of the CLI |

Practical guidance: **use the CLI for anything scripted or cron-driven** (no
daemon to run, no port to expose). Use **HTTP when another agent needs a live
query surface** or when the consumer is not on the same host. Use **SSE with
`once=1` from scripts** — a plain open stream will hang them forever. There is
no `/rank` HTTP route; it is `/repos`.

---

## 8. Operation

### Preflight

```bash
HUB=/opt/data/repos/hermes-signal-hub/scripts/signalhub
DB=/opt/data/.signalhub/signal.db

"$HUB" --db "$DB" check     # quota snapshot: core and search remaining/limit
"$HUB" --db "$DB" status    # store health: counts, weights, feedback, last run
```

`check` reports the rate-limit state persisted from the last run's response
headers, so it answers "do I have room?" without spending a request.
`status` reports repos, events, observations, learned weights and feedback
totals, and reports the last run. If the last run is older than the freshness
target, `freshness_sla()` classifies it `ok` / `lagging` / `stale` so the digest
can warn.

Note that `--db` is a **global** option: it goes before the subcommand.

### Quota management

| Limit | Value | Notes |
|---|---|---|
| Search API | 30 requests / **minute** | Authenticated and unauthenticated alike. It is not 30/hour. |
| Core REST API | 5000 requests / **hour** | `GET /rate_limit` costs one core call |
| Cost accounting | **per HTTP call, not per repo** | A 100-item page is one call |

One `collect` run costs roughly 8–12 search calls. The budget is enforced in
three independent places, which is deliberate defence in depth:

1. `RequestBudget(max_requests=...)` raises `BudgetExceeded` — the hard stop
   inside the client.
2. `CollectorConfig.max_search_calls` (default 24) stops the query loop; the
   collector measures the cost of a slice as the *difference in HTTP call
   count*, not the number of repos returned.
3. `CollectorConfig.max_pages` (default 2) bounds each slice individually.

A secondary rate limit (HTTP 403/429 with `X-RateLimit-Remaining: 0` or a
`Retry-After` header) is retried with exponential backoff and jitter, capped at
60s, up to `max_retries`; exhausting the retries raises `SecondaryRateLimit`.
5xx and timeouts follow the same backoff path.

The conditional-request path is not available: **the Search API does not emit an
`ETag`**, so revalidation is impossible and quota is protected by the star-floor
heuristics and the budget instead (`github.py:8-11`).

### Failure behaviour and recovery

The system is built so that partial failure is still success:

| Failure | Behaviour | Recovery |
|---|---|---|
| One search slice fails (500) | Caught per-query; the error is appended to `result.errors` and the run continues with the other slices | None needed — the next run retries |
| Budget exhausted mid-run | The loop breaks; already-collected repos are kept and stored | Raise `--max-search-calls`, or wait for the window to reset |
| `BudgetExceeded` raised in the client | Propagates; the run is abandoned before it starts writing | Raise the budget or fix the query plan |
| Secondary rate limit | Retried with backoff + jitter; then `SecondaryRateLimit` | Wait for `reset_at` from `check` |
| Token missing | `resolve_token` raises; the client refuses an empty token | Export `GH_TOKEN` or write a token file |
| Auth failure (401) | `GitHubError(401, ...)` | Rotate the token |
| Malformed FTS query | Caught; falls back to a `LIKE` scan | None needed |
| Sort injection attempt | `order_by` is whitelisted; falls back to the default | None needed |
| Process killed mid-run | The `runs` row is left unclosed; `last_successful_run()` ignores it | The next run opens a fresh row; stale rows are harmless |
| Digest repeats itself | Using `rank --status new` re-sends the whole backlog | Drive the digest from `events --latest-run` instead (`scripts/daily_digest.sh:44-59`) |
| Bad run of verdicts | Weights drifted | `weights --reset` restores the defaults |
| Digest too long | Truncated to `max_chars`, items dropped, `truncated=True` reported | Raise `--limit` or `--max-chars` deliberately |

The design rule throughout: **a dead slice must never kill the run, and an
incomplete run must still store what it found** (`collector.py:197-199`).
Deleting the database and re-running `collect` is a valid, cheap recovery — the
only thing lost is the observation history, and therefore the deltas.

### Scheduling

The daily pattern is a cron job that collects, emits the digest and stops.
`scripts/daily_digest.sh` is idempotent and silent when nothing is new, so the
job does not ping the user on a slow news day. It is driven by
`events --latest-run` (not `rank --status new`) so the daily message is one
slice rather than the entire unacknowledged backlog, and it acks on both sides
of the bus — `repos.status` for the human digest and `events.consumed_by` for
machine consumers.

---

## 9. Known limits

- **The feedback learner is heuristic, not ML.** There is no model, no
  gradient, no held-out evaluation. It is a per-component accumulator with
  proportional application. See [SCORING.md](SCORING.md) for the exact
  algorithm and its failure modes.
- **The scorer is hand-tuned, not validated against ground truth.** The weight
  defaults encode an opinion about what matters, refined by verdict, not
  measured against an outcome the system can score.
- **Auto-component attribution is a guess.** A bare `yes`/`no` nudges the
  component the repo scores weakest on, which is a heuristic proxy for "what
  was actually wrong". Explicit `--component` always beats it.
- **No historical backfill.** Momentum requires two sightings, so a repo
  discovered today has no delta until the next run. There is no way to
  reconstruct star history from the Search API.
- **The search window is forward-looking only.** Repos are found by
  `created:>...`; a repo that becomes interesting after its creation window
  closes may never be rediscovered.
- **Deliberate N+1 elimination means less data.** Search responses already
  embed license, topics and subscriber counts, so the collector never fans out
  into `/repos/{owner}/{repo}` per item. That is what keeps a run at ~30
  requests instead of ~130, but it also means fields absent from the search
  payload (release cadence, contributor count) are not available to the scorer.
- **SQLite is single-writer.** Fine for one cron job; not fine for several
  collectors writing at once. See §4.
- **WAL requires a local filesystem.** A DB on NFS will not work correctly.
- **The store is not shared-process safe across hosts.** `Store` assumes one
  process writes at a time.
