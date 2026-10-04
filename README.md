# hermes-signal-hub

Find the GitHub repos that are **moving right now**, score them, and route them
into your agent flows — with a feedback loop that learns what you actually want.

Zero third-party dependencies. Python 3.13 standard library only.

## The problem

Sorting GitHub search by stars surfaces repos that have been accruing stars for
years. What you want is what is *rising*: a 900-star MCP server created six days
ago beats a 30k-star toolchain from 2019.

`signalhub` ranks on **stars per day**, corrected for engagement, topical
relevance and project health — and it learns from your `yes`/`no`.

## Quick start

```bash
git clone https://github.com/dnniz/hermes-signal-hub
cd hermes-signal-hub

export GH_TOKEN=...          # any PAT with public read scope

./scripts/signalhub --db ./signal.db check          # preflight
./scripts/signalhub --db ./signal.db collect         # discover + score
./scripts/signalhub --db ./signal.db digest --limit 8
```

Sample output:

```
**Nuevos repos en GitHub**

_2026-10-04 15:30 UTC · 6 de 6_

1. [bojieli/ai-infra-book](https://github.com/bojieli/ai-infra-book)
   《深入理解 AI Infra：量化分析与系统设计》...
   ⭐ 5,860 · Python · Apache-2.0 · 136⭐/día ▰▰▰▰▱
   💡 steady: 136 stars/day; active use: 7% fork ratio; 5860 watchers; on-topic...
```

From the second run onward each line also carries a `+N` delta — the stars gained
since the previous run. That is what turns a static ranking into a feed.

## What it does

- **Discovers** repos created recently, across base star/recency slices plus
  topical slices for the domains you care about.
- **Scores** them on four components — velocity, engagement, relevance,
  developer.
- **Remembers** every observation, so run 2 onwards can measure real acceleration
  instead of guessing from age.
- **Publishes** an event bus other flows can consume.
- **Learns** from `decide owner/repo yes|no|noise`, nudging the ranking weights
  within a bounded range.
- **Exposes** all of it over CLI, HTTP and SSE.
- **Fans out** to as many flows as you want: each keeps its own cursor, so one
  flow reading first never starves another.

## Using it

```bash
HUB=./scripts/signalhub
DB=./signal.db

# discover (idempotent — repeat runs report 0 new)
$HUB --db $DB --days 21 --min-stars 200 --max-search-calls 25 collect

# read it
$HUB --db $DB digest --limit 10          # markdown, for a human
$HUB --db $DB rank --format briefing    # text briefing for an agent
$HUB --db $DB --json rank --limit 20    # JSON array

# train it
$HUB --db $DB decide owner/repo yes
$HUB --db $DB decide owner/repo no
$HUB --db $DB weights

# another flow consumes the same bus, with its own cursor
$HUB --db $DB --json consume morning-ai --limit 20     # read, no side effects
$HUB --db $DB consume morning-ai --limit 20 --advance  # confirm, after processing
$HUB --db $DB cursors                                  # who is behind
```

`--db` is a global flag: it goes **before** the subcommand.

### As a service

```bash
$HUB --db $DB serve --port 8787
```

`GET /health` · `/repos` · `/jsonl` · `/digest` · `/briefing` · `/search` ·
`/events` · `/consume` · `/cursors` · `/stream` (SSE; add `once=1` for a finite
stream) · `POST /collect`.

### As a daily job

```bash
./scripts/daily_digest.sh
```

Collects, prints a digest, and stays silent when nothing new arrived. Every knob
is a `SIGNALHUB_*` environment variable.

## Documentation

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data model, momentum, feedback loop |
| [SCORING.md](docs/SCORING.md) | the four components, formulas, the learner |
| [INTEGRATION.md](docs/INTEGRATION.md) | CLI reference, HTTP/SSE, event bus, multi-agent patterns |
| [OPERATIONS.md](docs/OPERATIONS.md) | runbook, quota, failure modes, lessons learned |
| [SKILL.md](SKILL.md) | the agent-facing skill definition |
| [docs/adr/](docs/adr/) | architecture decision records |

## Design notes

**Standard library only.** The target container has no `pip` and no third-party
wheels, so `urllib`, `sqlite3` and `http.server` are the whole stack. That turned
out to be a feature: a clone runs with no install step at all.

**Quota is counted per HTTP call, not per repo.** A search page of 100 items costs
one call. Getting this wrong is what made an early version exhaust a 25-call
budget after a single request.

**The Search API rejects `OR` between two qualifiers** with HTTP 422, and
parenthesising it returns zero results rather than an error. Sibling topics are
therefore separate query slices.

**Five bugs in this codebase passed the unit tests and were only caught by
running against the real API.** They are written up in
[OPERATIONS.md](docs/OPERATIONS.md#lessons-learned) — including one where a
declared-but-never-invoked interface method hid the fact that the hub never
queried the rate limit at all.

## Development

```bash
PYTHONPATH=src python -m pytest tests/ -q
ruff check src/ tests/
ruff format --check src/ tests/
```

177 tests, no network access required.
