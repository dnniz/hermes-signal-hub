# Operations runbook

Everything needed to run, debug and recover `signalhub`.

Source of truth: [`src/signalhub/cli.py`](../src/signalhub/cli.py) and
[`scripts/daily_digest.sh`](../scripts/daily_digest.sh). Quota numbers below were
verified live against `GET /rate_limit` during development, not copied from docs.

## Preflight

Run this before anything else. It resolves the token, opens the database and
reports live quota without spending a search call.

```bash
signalhub --db /opt/data/.signalhub/signal.db check
```

Never start a discovery run on an empty quota. A 403 storm mid-run wastes the
work already done.

## GitHub quota — the number people get wrong

Verified live in this session against `GET /rate_limit`:

| Resource | Limit | Reset |
|---|---|---|
| `core` | 5000 / hour | hourly |
| `search` | **30 / minute** | per minute |

**Search is 30 per minute, not 30 per hour.** The per-hour figure is the
unauthenticated limit people carry in their heads, and treating the authenticated
budget as 30/hour makes you throttle daily jobs for no reason.

One `collect` run costs **8–12 search calls**, so many runs per hour are fine.
Check the budget with `--max-search-calls` rather than a cron schedule.

## How quota is counted

**Per HTTP call, not per repo returned.** A single search page of 100 items costs
one call.

This was a real bug. The collector incremented its budget counter once per repo
*yielded*, so a first page of 100 items consumed 100 units of a 25-call budget and
stopped the run after a single request. The fix derives the count from
`client.calls` (a true request counter) against a baseline taken at the start of
the run:

```python
calls_at_start = self.client.calls
...
spent = self.client.calls - calls_at_start
```

Pinned by `tests/test_collector.py::test_search_call_budget_is_respected` and
`::test_a_wide_first_page_does_not_starve_later_queries`.

## Subcommands at a glance

| Subcommand | Purpose |
|---|---|
| `check` | preflight: token, quota, database. Writes nothing |
| `collect` | run discovery, score, store, publish events |
| `rank` | top-ranked repos (`--format`, `--status`, `--min-stars`) |
| `digest` | Telegram-ready markdown |
| `search` | full-text over what was already found |
| `events` | read the event bus (`--latest-run`, `--run`, `--unconsumed`, `--consume`) |
| `ack` | mark repos as shown to a human |
| `decide` | record a verdict and retune the weights |
| `weights` | inspect or `--reset` the learned weights |
| `status` | counts, weights, feedback, last run |
| `serve` | HTTP + SSE API |
| `prune` | drop old observations |
| `reindex` | rebuild the FTS5 index |

HTTP routes exposed by `serve`, for the same surface in scriptable form:
`/health`, `/healthz`, `/repos`, `/jsonl`, `/digest`, `/briefing`, `/search`,
`/events`, `/runs`, `/stats`, `/weights`, `/stream`, plus `POST /collect`,
`/feedback` and `/consume`, and `POST /ack`.

## Configuration

Flags win over environment variables. `--db` is a **global** flag and must come
*before* the subcommand.

| Variable | Default | Meaning |
|---|---|---|
| `SIGNALHUB_DB` | `~/.hermes/signalhub/signalhub.db` (`hub.py::DEFAULT_DB`) | database path |
| `SIGNALHUB_DAYS` | `21` | discovery window in days |
| `SIGNALHUB_MIN_STARS` | `200` | minimum stars to admit a repo |
| `SIGNALHUB_MAX_SEARCH_CALLS` | `25` | hard ceiling on search calls per run |
| `SIGNALHUB_TARGET` | `300` | stop once this many candidates are collected |
| `SIGNALHUB_LIMIT` | `8` | repos in the digest |
| `SIGNALHUB_ACK_PREFIX` | `daily-digest` | tag recorded on delivered repos |

Token resolution order (`hub.py::TOKEN_ENV_CANDIDATES`): `SIGNALHUB_GITHUB_TOKEN`,
`GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, then — as a last
resort — the PAT stored in this install's Hermes MCP config, which is where a
working fine-grained token usually already lives. The token is never printed and
never committed.

## Idempotency: three different meanings of "seen"

This is the single most confusing area of the store. Three independent mechanisms
exist, and picking the wrong one is how you build a digest that re-sends itself
forever.

| Mechanism | Lives in | Controls | Touch it when |
|---|---|---|---|
| `events.consumed_by` | event bus | what a **machine consumer** has taken | another flow should not re-process |
| `repos.status` | repo row | `'new'` → `'seen'` for the **human digest** | a repo was shown to a human |
| `events.run_id` | event row | which **discovery run** produced it | you need "only what this run found" |

**Why `collect` repeated is a no-op:** a repo already in `repos` is updated, not
re-inserted, and emits no new event. A second run over the same window reports
`0 new`. This is correct and expected.

**Why the daily digest needs `run_id`:** filtering on `repos.status = 'new'` shows
the *entire* backlog. After a week of silent runs that is hundreds of repos, and
the top slice of it gets re-sent every single day. The digest therefore reads the
bus pinned to the latest run:

```bash
signalhub --db DB --json events --latest-run --limit 8
```

then acks exactly those repos on both channels. Verified: a first run delivered
its digest, the second and third stayed silent.

## Search API limits

**`OR` between two qualifiers is rejected.** `topic:agent OR topic:agents` returns
HTTP 422 with a message about the search containing only logical operators
without a qualifier. Wrapping it in parentheses does not help — the query is
accepted but matches nothing, which is worse because it fails silently.

This is why sibling topics are **separate slices** in `collector.py`. They each
cost one call, which is the correct trade. Do not "optimise" them back into one
query with `OR`.

## Failure states

| Symptom | Cause | Verify |
|---|---|---|
| `check` reports no token | `GH_TOKEN` unset and no `~/.gh-token` | `check` |
| `collect` fails immediately with 403 | search quota exhausted (30/min) | `check` |
| `collect` reports `errores: 2` | a malformed topic slice; see 422 above | `collect` stderr |
| `digest` empty but `status` shows `new_repos > 0` | everything was already acked | `--json events --unconsumed` |
| `daily_digest.sh` never speaks | the last run found 0 new repos | `--json status`, check `last_run.new_repos` |
| ranking unchanged after many verdicts | weights were reset, or `relevance` forced on bare verdicts (fixed) | `weights` |
| HTTP client hangs on `/stream` | stream opened without `once=1` | use `once=1` |
| `--json rank` output is markdown | fixed; `--json` is now global and wins over `--format` | `pytest tests/test_hub_cli.py` |

## Backup and restore

SQLite in WAL mode has three files. Back up the database while no writer is
running, or use the online backup API.

```bash
# consistent copy
sqlite3 /opt/data/.signalhub/signal.db ".backup '/opt/data/.signalhub/backup.db'"
```

If you copy by hand, take `signal.db`, `signal.db-wal` and `signal.db-shm`
together, or the copy will be missing recent commits.

## Resetting state

```bash
# learning only
signalhub --db DB weights --reset

# mark everything as already shown (digest goes quiet)
signalhub --db DB --json rank --limit 1000 \
  | python3 -c 'import json,sys; print("\n".join(r["full_name"] for r in json.load(sys.stdin)))' \
  | xargs signalhub --db DB ack --prefix manual

# start completely fresh
rm -f /opt/data/.signalhub/signal.db*
```

The last one is irreversible and drops all feedback history.

## Metrics worth watching

From `signalhub --db DB --json status`:

| Field | Watch for |
|---|---|
| `stats.repos` | total known repos; should grow slowly |
| `stats.new_repos` | what the last run discovered; `0` on a repeat run is correct |
| `stats.unconsumed_events` | machine backlog; if it grows unbounded, a consumer is not acking |
| `stats.observations` | should be ≈ `repos × runs`; flat means collection stopped |
| `stats.last_run.api_calls` | quota per run; a jump means the query set grew |
| `stats.last_run.errors` | any non-zero value means a query was rejected |

## Lessons learned

The five defects below were found by running against the live API, not by
reading the code. Each now has a regression test.

1. **The hub never queried the rate limit.** `rate_limit_snapshot` read the
   private `_client` attribute, which is `None` until a lazy property builds it,
   so the call always no-opped and returned `{}`. Fixed to use the public
   `client` property. The test double had declared the method but no test ever
   invoked it — a declared-but-unexercised interface is not coverage.

2. **Quota was counted per repo, not per call** (detailed above). One page of
   100 items exhausted a 25-call budget.

3. **`OR` between qualifiers returns 422** (detailed above). Caught only by
   running the real query; it fails silently when parenthesised.

4. **SSE with `once=1` hung.** The handler looped without terminating the
   connection, so any script client blocked forever. It now half-closes
   (`wfile.flush()` + `wfile.close()`) and sends EOF after `[DONE]`.

5. **`--json` was ignored by `rank`.** It branched on `--format` and never read
   the global `args.json`, so `--json rank` emitted markdown. Any caller piping
   stdout into a JSON parser failed with an unhelpful error. The daily-digest
   script hit exactly this and silently produced an empty digest.

The pattern across all five: **each one passed unit tests and failed against the
real system.** Mocks that do not fail the way production fails are worse than no
mocks, because they certify behaviour that does not exist.
