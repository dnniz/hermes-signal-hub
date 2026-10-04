# Integration guide

How to wire `signalhub` into other systems, flows and agents.

This document covers the *integration surfaces*: the CLI contract, the output
formats, the HTTP API, the SSE stream and the event bus. For day-to-day running,
quota, backups and troubleshooting see [OPERATIONS.md](OPERATIONS.md). For the
short usage cheat-sheet see `SKILL.md`.

- **Audience:** an engineer or an agent wiring signalhub into something else.
- **Invariant:** every surface here is read-only except the four write verbs
  (`collect`, `feedback`, `consume`, `ack`).
- **Local-first:** the server binds `127.0.0.1:8787` by default and there is no
  authentication. Do not expose it on a public interface.

---

## 1. CLI reference

Entry point: `scripts/signalhub` (a thin wrapper that puts `src/` on the path and
calls `signalhub.cli:main`).

> **`--db` is a global flag and must come BEFORE the subcommand.**
> `--db /path/signal.db rank` is correct; `rank --db /path/signal.db` is an error.

### 1.1 Global flags

Defined in `build_parser()` in `src/signalhub/cli.py`.

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--db` | path | `$SIGNALHUB_DB`, else `~/.hermes/signalhub/signalhub.db` | Path to the SQLite file |
| `--json` | flag | off | Emit JSON instead of the default rendering |
| `-v`, `--verbose` | counter | 0 | `-v` = INFO, `-vv` = DEBUG (logs go to **stderr**) |
| `--days` | int | 14 | Creation window, days |
| `--min-stars` | int | 150 | Star floor |
| `--max-search-calls` | int | 24 | Search-call budget for the run |
| `--target` | int | 400 | Target candidate count |

`--days`, `--min-stars`, `--max-search-calls` and `--target` are only consumed by
`collect` (they build a `CollectorConfig`); on read-only subcommands they are
accepted and ignored, so it is safe to keep them in a shared wrapper.

`main()` also does `os.environ.setdefault("SIGNALHUB_DB", args.db)`, so an explicit
`--db` propagates into the environment for child processes.

### 1.2 Subcommands

| Subcommand | Flags | Does |
|---|---|---|
| `collect` | `--dry-run`, `--digest`, `--briefing`, `--limit` (8), `--title` ("Nuevos repos en GitHub") | Discover → dedupe → score → store → publish events |
| `rank` | `--limit` (10), `--status {new,any,seen,acknowledged}` (new), `--format {markdown,jsonl,briefing,table}` (markdown), `--title` ("Ranking de repos") | Rehydrated ranking from the store, no network |
| `digest` | `--limit` (8), `--status {new,any,seen}` (new), `--title`, `--no-hint` | Telegram-ready markdown |
| `search` | positional `query`, `--limit` (10) | FTS5 search over what is already stored |
| `events` | `--kind`, `--after` (0), `--limit` (50), `--unconsumed`, `--run`, `--latest-run`, `--consume [CONSUMER]` | Read the event bus; optionally mark consumed |
| `ack` | positional `repos` (one or more), `--prefix` (`seen:`) | Flag repos as delivered |
| `decide` | positional `repo`, positional `verdict` ∈ `{yes,no,noise,star_per_day,relevance,quality}`, `--actor` (`user`), `--note` | Record a verdict and nudge the weights |
| `status` | — | Operational snapshot |
| `weights` | `--reset` | Show weights, or restore defaults |
| `serve` | `--host` (127.0.0.1), `--port` (`default_port()` = 8787) | Run the HTTP API |
| `prune` | `--keep-days` (60) | Delete observations older than N days |
| `reindex` | — | Rebuild the FTS5 index |
| `check` | — | Preflight: token, quota, DB. Writes nothing |

Notes that matter when scripting:

- `collect --json` returns early and ignores `--digest` / `--briefing` / `--limit`.
- `rank --json` **overrides** `--format`. This is deliberate (see
  [OPERATIONS.md § Lessons learned](OPERATIONS.md#lessons-learned)); `--json rank`
  emitted markdown before the fix and broke every JSON-parsing consumer.
- `events --consume` with no value defaults the consumer name to `cli`.
- `--latest-run` and `--run` are mutually redundant: `--latest-run` resolves to
  the id of the last run with `status = 'ok'`, or `0` if there is none.

### 1.3 Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic failure (message on stderr), or `check` not ok |
| 2 | `decide` with an unknown repo (raises `ValueError`) |
| 130 | Interrupted (`KeyboardInterrupt`) |

`BrokenPipeError` is swallowed and returns 0, so `... | head` does not produce a
spurious error.

---

## 2. Output formats: pick by consumer

Three shapes, all produced by `src/signalhub/render.py`. All examples below are
real output captured from this codebase (offline, via a stubbed client, so no
GitHub quota was spent).

### 2.1 Markdown — for humans

`digest`, `collect --digest`, `GET /digest`, and `rank --format markdown`.

Telegram-safe: no tables, descriptions clipped to 110 chars, a 5-step velocity
bar, and the body hard-capped at 4096 chars (`TELEGRAM_MESSAGE_LIMIT`) with an
explicit overflow line when entries had to be dropped.

```bash
$ signalhub --db "$DB" digest --limit 2
**Nuevos repos en GitHub**

_2026-10-04 15:36 UTC · 2 de 2_

1. [acme/tinyvector](https://github.com/acme/tinyvector)
   An embedded vector index with zero-copy scans and a 3 MB binary
   ⭐ 980 · Rust · Apache-2.0 · 190⭐/día ▰▰▰▰▰
   💡 steady: 190 stars/day; active use: 14% fork ratio; 95 watchers; on-topic: mcp, rag, Rust
   #mcp #rag

2. [someone/oldtool](https://github.com/someone/oldtool)
   DevOps toolbox
   ⭐ 700 · Shell · 17⭐/día ▰▰▱▱▱
   💡 on-topic: devops, Shell
   #devops


Responde `signalhub decide <owner/repo> yes|no|noise` para entrenar el filtro.
```

The trailing hint is the `verdict_hint`; `--no-hint` removes it (over HTTP, the
`hint` query param supplies it instead).

### 2.2 JSONL — the agent-to-agent contract

`rank --format jsonl`, `GET /jsonl` (`Content-Type: application/x-ndjson`).

One JSON object per line, keys sorted, each with a `rank` field, the full
component breakdown and the human-readable `reasons`. This is the shape to use
when a machine, not a person, consumes the ranking.

```bash
$ signalhub --db "$DB" rank --format jsonl --limit 1
{"age_days": 5.15, "components": {"developer": 0.5, "engagement": 0.9908, "outburst": 0.1397, "penalty": 0.0, "relevance": 0.9, "velocity": 0.9719}, "created_at": "2026-09-29T12:00:00+00:00", "description": "An embedded vector index with zero-copy scans and a 3 MB binary", "forks": 140, "full_name": "acme/tinyvector", "html_url": "https://github.com/acme/tinyvector", "language": "Rust", "license": "Apache-2.0", "pushed_at": "2026-10-03T12:00:00+00:00", "rank": 1, "reasons": ["steady: 190 stars/day", "active use: 14% fork ratio", "95 watchers", "on-topic: mcp, rag, Rust"], "star_delta": 0, "stars": 980, "stars_per_day": 190.28, "stars_per_day_delta": 190.283, "topics": ["mcp", "rag"], "total_score": 0.9156}
```

One line in full, wrapped here for readability — on the wire it is a single line.

### 2.3 JSON (`--json`) — for tooling that prefers an array

`--json rank` / `--json search` return an **array** of the same per-repo objects
plus `status`, `first_seen`, `last_seen`, `seen_count`, `observations` and
`feedback_count` from the store row. `--json events` returns an array of event
rows. `--json digest` returns a `Digest.as_dict()` (`title`, `body`,
`generated_at`, `count`, `truncated`, `dropped`, `notes`, `items`).

```bash
$ signalhub --db "$DB" --json rank --limit 1
[
  {
    "full_name": "acme/tinyvector",
    "html_url": "https://github.com/acme/tinyvector",
    "description": "An embedded vector index with zero-copy scans and a 3 MB binary",
    "stars": 980,
    "forks": 140,
    "language": "Rust",
    "topics": ["mcp", "rag"],
    "license": "Apache-2.0",
    "age_days": 5.15,
    "created_at": "2026-09-29T12:00:00+00:00",
    "pushed_at": "2026-10-03T12:00:00+00:00",
    "total_score": 0.9156,
    "components": {
      "velocity": 0.9719,
      "engagement": 0.9908,
      "relevance": 0.9,
      "developer": 0.5,
      "penalty": 0.0,
      "outburst": 0.1397
    },
    "reasons": ["steady: 190 stars/day", "active use: 14% fork ratio", "95 watchers", "on-topic: mcp, rag, Rust"],
    "star_delta": 0,
    "stars_per_day_delta": 190.283,
    "stars_per_day": 190.28
  }
]
```

### 2.4 Briefing — delegation to a subagent

`rank --format briefing`, `collect --briefing`, `GET /briefing`.

Plain text written *for another agent*: it states that the block is read-only,
gives the exact verdict command to run, and includes the component breakdown so
the delegate can judge whether the **ranking** is wrong, not just the repo.

```bash
$ signalhub --db "$DB" rank --format briefing --limit 2
# signal-hub briefing

Read-only. To report a verdict run:
  signalhub decide <owner/repo> <yes|no|noise>

1. acme/tinyvector | score 0.9189 | 980⭐ | 190.28/day | 5.15d old
   An embedded vector index with zero-copy scans and a 3 MB binary
   components: vel=0.9719 eng=0.9908 rel=0.9 dev=0.5 pen=0.0
   why: steady: 190 stars/day; active use: 14% fork ratio; 95 watchers; on-topic: mcp, rag, Rust
   url: https://github.com/acme/tinyvector
2. someone/oldtool | score 0.483 | 700⭐ | 17.43/day | 40.15d old
   DevOps toolbox
   components: vel=0.4688 eng=0.2618 rel=0.7 dev=0.45 pen=0.0
   why: on-topic: devops, Shell
   url: https://github.com/someone/oldtool
```

### 2.5 Compact table — for eyeballing, not parsing

`rank --format table` (CLI only, no HTTP route):

```bash
$ signalhub --db "$DB" rank --format table --limit 3
 1. acme/tinyvector                                        980⭐ 0.916   190.3/d
 2. someone/oldtool                                        700⭐ 0.483    17.4/d
```

### 2.6 Choosing

| Consumer | Use | Command |
|---|---|---|
| A human on Telegram | Markdown digest | `digest` |
| A delegated subagent | Briefing | `rank --format briefing` |
| Another program | JSONL stream | `rank --format jsonl` |
| A script that needs the store row (status, seen counts) | `--json` | `--json rank` |
| Another Hermes flow | Event bus | `--json events --unconsumed` |

---

## 3. HTTP API

```bash
$ signalhub --db "$DB" serve --host 127.0.0.1 --port 8787
```

Stdlib `http.server` (`ThreadingHTTPServer`), no framework, no auth, default bind
`127.0.0.1:8787` (chosen to stay clear of the Hermes gateway range 8642–8644).
`Server` header is `signalhub/1`.

### 3.1 GET endpoints

| Method | Path | Query params | Response |
|---|---|---|---|
| GET | `/`, `/health`, `/healthz` | — | `hub.health()` object (see §3.3) |
| GET | `/stats` | — | `store.stats()` object |
| GET | `/weights` | — | `{velocity, engagement, relevance, developer, penalty_scale}` |
| GET | `/runs` | `limit` (10) | `{"runs": [run rows]}` |
| GET | `/repos` | `status` (`new`), `min_stars` (0), `since_days`, `limit` (20, max 200), `offset` (0), `order` (`score`) | `{"repos": [...], "count": N}` |
| GET | `/events` | `kind`, `after` (0), `limit` (50, max 500), `unconsumed` (`1` to filter) | `{"events": [...]}` |
| GET | `/feedback` | `limit` (50, max 500), `repo` | `{"feedback": [...]}` |
| GET | `/digest` | `status`, `min_stars`, `limit` (8, max 100), `title`, `hint` | `text/plain` markdown |
| GET | `/briefing` | `status`, `min_stars`, `limit` (10, max 100) | `text/plain` briefing |
| GET | `/jsonl` | `status`, `min_stars`, `limit` (8, max 100) | `application/x-ndjson` |
| GET | `/search` | `q`, `limit` (10) | `{"results": [...]}` |
| GET | `/stream` | `once`, `interval` (15), `timeout` (3600), `kind`, `after` (0), `unconsumed` | `text/event-stream` |

`order` is allow-listed to `{score, stars, created_at, pushed_at, first_seen}`;
anything else silently falls back to `score`. `status` accepts `""`, `any` or
`all` to mean "no filter" — otherwise an unknown literal would match nothing and
return an empty list.

`/digest`, `/briefing` and `/jsonl` all share the same `status` / `min_stars` /
`limit` semantics via `_top_items()`.

### 3.2 POST endpoints

| Method | Path | JSON body | Response |
|---|---|---|---|
| POST | `/collect` | `{"publish": true}` (optional) | `RunReport.summary()` |
| POST | `/feedback` | `{"full_name" \| "repo", "verdict", "actor": "agent", "note": null}` | `{"full_name", "verdict", "component", "weights"}` |
| POST | `/consume` | `{"event_ids": [...], "consumer": "unknown"}` | `{"consumed": N}` |
| POST | `/ack` | `{"repos": [...], "prefix": "seen:"}` | `{"updated": N}` |

`POST /feedback` returns 400 if `full_name`/`repo` or `verdict` is missing, and
400 if the repo is unknown (`ValueError`). An unknown path returns 404.
**There is no `/rank` route** — use `/repos`.

### 3.3 Real response samples

`GET /healthz`:

```json
{
  "status": "ok",
  "db": "/opt/data/.signalhub/signal.db",
  "stats": {
    "repos": 3,
    "new_repos": 2,
    "total_stars": 5880,
    "max_stars": 4200,
    "observations": 3,
    "events": 3,
    "unconsumed_events": 0,
    "last_run": {
      "id": 1,
      "finished_at": "2026-10-04T15:36:15.947266+00:00",
      "candidates": 3,
      "new_repos": 3,
      "api_calls": 9
    }
  },
  "weights": {"velocity": 0.4071, "engagement": 0.2035, "relevance": 0.2545, "developer": 0.1349, "penalty_scale": 1.0},
  "hours_since_run": 0.0,
  "rate_limit": {},
  "feedback": {"no": 2}
}
```

> **Note on `rate_limit` in `/healthz` and `status --json`.** `health()` reads
> `store.get_meta("rate_limit", {})` and nothing in the current code ever writes
> that key — quota is persisted per run in the `runs.rate_limit` column instead.
> So this field is `{}` in practice. Read the live quota from
> `GET /runs` (`runs[0].rate_limit`) or run `check`. See
> [OPERATIONS.md § Metrics](OPERATIONS.md#metrics-what-status-actually-tells-you).

`GET /repos?limit=1&status=any`:

```json
{
  "repos": [
    {
      "full_name": "acme/tinyvector",
      "html_url": "https://github.com/acme/tinyvector",
      "description": "An embedded vector index with zero-copy scans and a 3 MB binary",
      "stars": 980,
      "forks": 140,
      "watchers": 95,
      "language": "Rust",
      "topics": ["mcp", "rag"],
      "license": "Apache-2.0",
      "created_at": "2026-09-29T12:00:00+00:00",
      "pushed_at": "2026-10-03T12:00:00+00:00",
      "score": 0.9870635325587047,
      "status": "new",
      "first_seen": "2026-10-04T15:36:15.946518+00:00",
      "last_seen": "2026-10-04T15:36:15.946518+00:00",
      "seen_count": 1,
      "star_delta": 0,
      "observations": 1,
      "feedback_count": 0
    }
  ],
  "count": 1
}
```

Note `/repos` returns the **stored** `score` column (the value from the last
`collect`), while `/jsonl` and `/briefing` recompute components with the live
weights. They can therefore disagree after a verdict — that is expected, see
`_to_scored()` in `hub.py`.

---

## 4. SSE stream

```bash
signalhub --db "$DB" serve &
curl -N 'http://127.0.0.1:8787/stream?once=1&limit=20'
```

Wire format — one SSE frame per event, with the event id so a client can resume:

```
id: 1
event: repo.discovered
data: {"id": 1, "created_at": "...", "run_id": 1, "full_name": "dnniz/agentmesh", "kind": "repo.discovered", "score": 1.0, "payload": {...}, "consumed_by": null}

: keepalive

```

### 4.1 `once=1` vs an open stream

| | `?once=1` | no `once` |
|---|---|---|
| Behaviour | Emits the current backlog (up to 100 events per poll), then returns | Polls forever until `timeout` elapses |
| Headers | `Connection: close` | `Connection: keep-alive` |
| Batching | One batch, one `keepalive` comment, EOF | A `keepalive` comment every `interval` seconds |
| Use from | A script / cron / a flow that wants a finite read | A long-lived listener that wants push |

**`once=1` half-closes the socket and the client sees EOF.** The handler sets
`self.close_connection = True` after writing the backlog. This matters: without
it, `urllib.request.urlopen()` blocks until its own timeout even though the server
already wrote everything, and the caller looks like it hung. If you script
against `/stream`, either use `once=1` or set an explicit read timeout.

The `Connection: close` header is equally deliberate — a `once` response must not
advertise keep-alive or the client waits for a second event that will never
arrive.

### 4.2 Other stream parameters

| Param | Default | Notes |
|---|---|---|
| `after` | 0 | Resume cursor: only events with `id > after` |
| `interval` | 15 s | Poll period, floored at 1.0 s (`max(1.0, ...)`) |
| `timeout` | 3600 s | Wall-clock cap; the continuous stream returns when exceeded |
| `kind` | — | Event kind filter |
| `unconsumed` | — | `1` to send only `consumed_by IS NULL` |

The loop polls the SQLite event table (indexed) rather than holding a connection
to GitHub, so a slow client can never hold API budget hostage. A client
disconnect surfaces as `BrokenPipeError` / `ConnectionResetError` and is logged at
debug level.

---

## 5. Event bus

The `events` table is an append-only feed. `collect` publishes one
`repo.discovered` event per newly discovered repo, stamped with the `run_id`
that found it.

### 5.1 Event shape

```bash
$ signalhub --db "$DB" --json events --limit 1
[
  {
    "id": 1,
    "created_at": "2026-10-04T15:36:15.947135+00:00",
    "run_id": 1,
    "full_name": "dnniz/agentmesh",
    "kind": "repo.discovered",
    "score": 1.0,
    "payload": {
      "html_url": "https://github.com/dnniz/agentmesh",
      "stars": 4200,
      "stars_per_day": 1866.51,
      "language": "Python",
      "topics": ["ai", "agents", "mcp"],
      "license": "MIT",
      "reasons": ["fast: 1867 stars/day", "active use: 7% fork ratio", "on-topic: ai, agents, mcp", "established maintainer"],
      "components": {"velocity": 1.0, "engagement": 0.7899, "relevance": 1.0, "developer": 1.0, "penalty": 0.0},
      "description": "Lightweight mesh runtime that routes tool calls between local LLM agents over stdio and HTTP"
    },
    "consumed_by": "morning-ai"
  }
]
```

The `payload` is self-contained: a consumer needs neither the store nor the API
to act on it.

Text mode is for eyeballing only:

```bash
$ signalhub --db "$DB" events --limit 3
#    1 repo.discovered      dnniz/agentmesh                               score=1.000 ⭐4200
#    2 repo.discovered      acme/tinyvector                               score=0.987 ⭐980
#    3 repo.discovered      someone/oldtool                               score=0.550 ⭐700
```

### 5.2 `read_events` filters

`store.read_events(kind, after_id, limit, unconsumed_only, run_id)` — keyword-only.
CLI: `--kind`, `--after`, `--limit`, `--unconsumed`, `--run`, `--latest-run`.
HTTP `GET /events`: `kind`, `after`, `limit`, `unconsumed=1` (no `run_id` over HTTP).

| Filter | SQL effect | Use |
|---|---|---|
| `after_id` | `id > ?` | Cursor resume — the idempotent-consumer primitive |
| `kind` | `kind = ?` | Future event types; today only `repo.discovered` is published |
| `run_id` | `run_id = ?` | "What did *this* run find?" |
| `unconsumed_only` | `consumed_by IS NULL` | Queue draining |

Ordering is always `ORDER BY id ASC`, so paging by `after_id` is stable.

### 5.3 The cursor pattern (idempotent consumers)

Because `id` is `INTEGER PRIMARY KEY AUTOINCREMENT` and reads are ordered by it, a
consumer can make progress durable by remembering the last id it *processed*:

```python
import json, subprocess, pathlib

HUB = "/opt/data/repos/hermes-signal-hub/scripts/signalhub"
DB = "/opt/data/.signalhub/signal.db"
CURSOR = pathlib.Path("/opt/data/.signalhub/morning-ai.cursor")   # one per consumer
CONSUMER = "morning-ai"

cursor = int(CURSOR.read_text()) if CONSUMER_OK and CURSOR.exists() else 0
raw = subprocess.run(
    [HUB, "--db", DB, "--json", "events", "--after", str(cursor), "--limit", "50"],
    capture_output=True, text=True, check=True,
).stdout
events = json.loads(raw)
if not events:
    raise SystemExit(0)

# ... process each event; do the side effect first, THEN advance the cursor ...
for ev in events:
    handle(ev)

CURSOR.write_text(str(events[-1]["id"]))
subprocess.run([HUB, "--db", DB, "events", "--after", str(0), "--limit", "0",
                "--consume", CONSUMER], check=False)   # optional: also flag consumed
```

Rules that make this safe:

1. **Advance the cursor only after the side effect succeeded.** A crash between
   "sent to Telegram" and "cursor written" replays one event; the reverse silently
   loses one. Replay is recoverable, loss is not.
2. **`--consume` is optional and lossy.** It is one `consumed_by` string per
   event, so the *first* consumer to claim an event hides it from
   `--unconsumed` reads for everyone else. Use a cursor for your own flow and
   treat `consumed_by` as a shared "someone handled this" marker, not a
   per-consumer ack.
3. **Batch with `--limit` and loop** until it comes back empty; a single call
   only returns one page.
4. Keep a **separate cursor file per consumer name.** Two flows sharing a cursor
   file is exactly the double-delivery bug described in §6.

### 5.4 Publishing and consuming (Python API)

| Operation | Call |
|---|---|
| Publish | `store.publish(items, kind="repo.discovered", run_id=N)` where `items` is an iterable of `(full_name, html_url, score, payload)` |
| Read | `store.read_events(kind=..., after_id=..., limit=..., unconsumed_only=..., run_id=...)` |
| Mark consumed | `store.mark_consumed([ids], consumer)` → returns row count |
| Highest id | `store.max_event_id()` — useful to seed a cursor |

Over HTTP the equivalents are `POST /consume` with
`{"event_ids": [...], "consumer": "..."}` → `{"consumed": N}`.

---

## 6. Routing to other flows

### 6.1 The pattern

Every consumer is a **named** reader of the bus:

1. Pick a stable consumer name (`morning-ai`, `build-router`, …). It is the value
   written to `consumed_by` and the name of your cursor file.
2. Read with a **cursor**, not with `--unconsumed`, if you must not miss events
   that another flow already claimed.
3. Do the side effect (message, ticket, PR).
4. Advance the cursor, then optionally `POST /consume`.

### 6.2 Avoiding double delivery

Double delivery has two distinct causes with two distinct fixes:

| Cause | Symptom | Fix |
|---|---|---|
| Two flows share one `--unconsumed` queue | Flow B never sees an event Flow A already read | Per-consumer cursor (`after_id`), not `--unconsumed` |
| The same flow re-runs over the same window | The same repo appears in two digests | Consume/ack what you delivered, and pin reads to a `run_id` |

`consumed_by` is a **single** column, so it can only express "handled by someone".
It cannot express per-consumer delivery state. That is the structural reason the
cursor pattern exists.

### 6.3 Concrete example: a second flow subscribing

Given a `research` flow that should pick up freshly discovered repos and open an
investigation, without competing with the daily digest:

```bash
#!/usr/bin/env bash
# /opt/data/repos/hermes-signal-hub/scripts/research_pickup.sh
set -euo pipefail
HUB=/opt/data/repos/hermes-signal-hub/scripts/signalhub
DB=/opt/data/.signalhub/signal.db
CURSOR_FILE=/opt/data/.signalhub/research.cursor
CONSUMER=research
mkdir -p "$(dirname "$CURSOR_FILE")"

CURSOR=0
[ -f "$CURSOR_FILE" ] && CURSOR="$(cat "$CURSOR_FILE")"

# Read strictly what this flow has not seen, regardless of who else consumed.
NEW="$("$HUB" --db "$DB" --json events --after "$CURSOR" --kind repo.discovered --limit 50)"

IDS=$(printf '%s' "$NEW" | python3 -c 'import json,sys; print(" ".join(str(e["id"]) for e in json.load(sys.stdin)))')
[ -z "$IDS" ] && exit 0

REPOS=$(printf '%s' "$NEW" | python3 -c 'import json,sys; print(" ".join(dict.fromkeys(e["full_name"] for e in json.load(sys.stdin))))')

# 1. side effect first
# shellcheck disable=SC2086
for repo in $REPOS; do
  "$HUB" --db "$DB" rank --format briefing --limit 1 >/dev/null   # or: dispatch to your agent
  echo "queued $repo" >> /opt/data/.signalhub/research.log
done

# 2. then advance the cursor
printf '%s' "$NEW" | python3 -c 'import json,sys; e=json.load(sys.stdin); print(e[-1]["id"] if e else 0)' > "$CURSOR_FILE"

# 3. and flag them for anyone polling --unconsumed
# shellcheck disable=SC2086
"$HUB" --db "$DB" events --after 0 --limit 0 --consume "$CONSUMER" >/dev/null 2>&1 || true
```

The daily digest (`scripts/daily_digest.sh`) uses the *other* strategy — it pins
to `--latest-run`, which is idempotent because it reports what that one run
found rather than the whole backlog. Copy whichever of the two matches your flow:

| Flow shape | Strategy | Command |
|---|---|---|
| "Tell me what is new since I last looked" | Cursor | `--json events --after $CURSOR` |
| "Tell me what today's run found" | Run pin | `--json events --latest-run` |
| "Give me whatever nobody has handled" | Shared queue | `--json events --unconsumed` (single consumer only) |

---

## 7. Error handling patterns

### 7.1 What the client already does for you

`GitHubClient.request()` (`src/signalhub/github.py`) applies this policy per call,
so callers normally never see a transient failure:

| Condition | Behaviour |
|---|---|
| 403/429 with `X-RateLimit-Remaining: 0` or a `Retry-After` header | Classified as secondary rate limit; sleeps `Retry-After` (capped at 60 s) with exponential backoff, up to `max_retries = 4` |
| 403/429 without those | `SecondaryRateLimit` raised immediately (not retried) |
| 5xx | Retried with exponential backoff + jitter, up to 4 attempts |
| `TimeoutError` / `URLError` | Retried with exponential backoff + jitter, up to 4 attempts, then `GitHubError(0, ...)` |
| All retries exhausted on a rate limit | `SecondaryRateLimit` raised |

A per-run `RequestBudget` ceiling of `max(200, max_search_calls * 4)` raises
`BudgetExceeded` before the quota is actually gone.

### 7.2 What the collector does with a failure

Each query is wrapped: a dead slice is appended to `result.errors` and the run
continues. **Partial results are success by design** — a run that loses three of
ten topic slices still returns the other seven. `collect` exits 0 and reports
`errors` in the summary.

```bash
$ signalhub --db "$DB" --json collect | python3 -c 'import json,sys; s=json.load(sys.stdin); print(s["errors"])'
[]
```

Always inspect `errors` and `new_repos` after a scheduled run rather than
assuming a zero exit code means full coverage.

### 7.3 What you should do

| Situation | Action |
|---|---|
| `check` reports `token.found == false` | Export `SIGNALHUB_GITHUB_TOKEN`; nothing network-related will work |
| `check` reports quota exhausted (`remaining: 0`) | Do **not** retry in a loop. Let the window reset, then re-run — `collect` is idempotent and the second run resumes cleanly |
| `SecondaryRateLimit` in a scheduled run | Back off, do not escalate. The client already retried 4 times |
| `BudgetExceeded` | You set `--max-search-calls` too low for the query plan. Raise it, or reduce `--days` |
| HTTP 422 from a query | A malformed slice — see [OPERATIONS.md § Search API limits](OPERATIONS.md#limits-of-the-search-api) |
| Empty `events` | Not an error. Either the run found nothing new or the window is fully drained |
| `decide` exits 2 | Unknown repo. Confirm the exact `owner/name` with `search` |
| `POST /feedback` 400 | Missing `full_name`/`repo` or `verdict` |
| Unknown HTTP path 404 | Note there is no `/rank`; use `/repos` |

### 7.4 Idempotency summary

| Operation | Idempotent? | Notes |
|---|---|---|
| `collect` | Yes | Known repos are updated, not re-published; a repeat run over the same window reports `0 new` |
| `rank`, `digest`, `search`, `status` | Yes | Read-only |
| `events --after N` | Yes | Same cursor, same window of ids |
| `events --consume` | Yes | `UPDATE` to the same `consumed_by` value |
| `ack` | Yes | `set_status` to the same prefixed value |
| `decide` | **No** | Each call nudges the weights again. Record a verdict once |
| `POST /collect` | Yes | Same as `collect` |

---

## 8. Embedding the library

`SignalHub` is the facade; the CLI, the HTTP server and cron all go through it.

```python
from signalhub.hub import SignalHub, resolve_token
from signalhub.store import Store
from signalhub.collector import CollectorConfig

hub = SignalHub(
    store=Store("/opt/data/.signalhub/signal.db"),
    collector_config=CollectorConfig(days=21, min_stars=200, max_search_calls=25),
    token=resolve_token(),        # optional; resolved lazily otherwise
)

report = hub.collect()
print(report.summary()["new_repos"])

for item in hub.top(5, status="new"):
    print(item.repo.full_name, round(item.total, 3))
```

Useful seams for tests: `SignalHub(client=...)` accepts any object satisfying the
`SearchClient` protocol (`calls: int`, `rate_limit: RateLimitState`,
`search_repositories(...)`), and `hub.py` has a `RateLimitReader` protocol for the
optional `rate_limit_snapshot(charge=True)` capability. A three-line stub is
enough — that is exactly how `tests/test_collector.py` and
`tests/test_hub_cli.py` run without network.

Token resolution order (`resolve_token`): `SIGNALHUB_GITHUB_TOKEN` →
`GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_PERSONAL_ACCESS_TOKEN` → the
`GITHUB_PERSONAL_ACCESS_TOKEN` entry in `$HERMES_HOME/config.yaml` (default
`~/.hermes/profiles/backend-dev`). The token is never logged.
