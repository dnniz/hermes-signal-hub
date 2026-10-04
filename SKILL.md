---
name: signal-hub
description: "Discover high-momentum GitHub repos and route them into Hermes flows. Use when asked to find trending/new repos, build a repo digest, check for new tools, feed a briefing to another agent, or record a keep/discard verdict that tunes future discovery."
version: 1.0.0
created_by: agent
metadata:
  hermes:
    tags: [github, discovery, digest, signal, ranking, routing]
    related_skills: [research, morning-ai]
---

# Signal Hub — repo discovery with a feedback loop

`signalhub` is a stdlib-only Python library + CLI that finds **newly created GitHub
repos with real star velocity**, scores them, and publishes them to a local event
bus. Other Hermes flows subscribe to that bus. Your `yes`/`no` verdicts retune the
scorer, so the hub gets more personal every day without retraining anything.

## Why it exists

Sorting GitHub search by stars surfaces repos that have been accumulating stars for
years. What you actually want is *what is moving right now*: a 900-star MCP server
created six days ago beats a 30k-star toolchain from 2019. Signal Hub ranks on
**stars per day**, corrected for engagement, topical relevance and project health.

## Location

- Project: `/opt/data/repos/hermes-signal-hub`
- Database: `/opt/data/.signalhub/signal.db`
- CLI: `/opt/data/repos/hermes-signal-hub/scripts/signalhub`
- Token: `GH_TOKEN` env, or `~/.gh-token` (never commit it)

If the project path does not exist in this session, clone from
`https://github.com/dnniz/hermes-signal-hub`.

## Core loop

```bash
HUB=/opt/data/repos/hermes-signal-hub/scripts/signalhub
DB=/opt/data/.signalhub/signal.db

# 1. Discover, score and store. Idempotent: repos already seen are not re-emitted.
"$HUB" --db "$DB" --days 21 --min-stars 200 --max-search-calls 25 --target 300 collect

# 2. Read the digest (Telegram-safe markdown, already formatted).
"$HUB" --db "$DB" digest --limit 10
```

`collect` is safe to run repeatedly. A second run over the same window reports
`0 new` — the store already knows those repos, so you never re-notify.

## Quota reality — read before adding another call

GitHub's search API allows **30 requests/minute unauthenticated**,
**30/minute authenticated** (not 30/hour — easy to misremember), and the core
REST API allows 5000/hour. One `collect` run costs **8–12 search calls**, so you can
run it many times per hour. The hub counts quota **per HTTP call, not per repo
returned** — a 100-item page costs one call, not 100.

Always bound the run with `--max-search-calls`. Run `check` first if unsure:

```bash
"$HUB" --db "$DB" check
```

## Recording a verdict (this is how the filter learns)

```bash
"$HUB" --db "$DB" decide owner/repo yes
"$HUB" --db "$DB" decide owner/repo no
"$HUB" --db "$DB" decide owner/repo noise       # spam / low effort
```

A bare `yes`/`no` nudges whichever scoring component the repo is weakest on —
`relevance` for an off-topic-looking project, `velocity` for a slow one, `quality`
for a thin one. Use the explicit keywords above when you already know which axis
failed. Watch the weights move:

```bash
"$HUB" --db "$DB" weights
```

To undo a bad run of feedback: `"$HUB" --db "$DB" weights --reset`.

## Handing work to other agents

Three output shapes, pick by consumer:

- **digest** — markdown for the human. No parsing.
- **`rank --format briefing`** — text briefing with score, component breakdown and
  the `decide` command inline. This is what you hand to a delegated subagent.
- **`--json events --unconsumed`** — the event bus. Use this to feed *other*
  Hermes flows (morning digest, a "build me something like this" router).

```bash
# What arrived since last time, machine-readable.
"$HUB" --db "$DB" --json events --unconsumed --limit 20

# Mark them handled so they are not re-delivered.
"$HUB" --db "$DB" events --consume morning-ai
```

## HTTP API for other agents

```bash
"$HUB" --db "$DB" serve --port 8787
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | status, counts, weights, rate limit |
| `GET /repos?limit=N` | scored repos as JSON |
| `GET /jsonl?limit=N` | one JSON object per line, full component breakdown |
| `GET /events?limit=N` | event bus reads |
| `GET /digest?limit=N` | markdown digest |
| `GET /search?q=...` | full-text over what was already found |
| `GET /stream?once=1&limit=N` | SSE, finite — closes cleanly with EOF |
| `POST /collect` | trigger a discovery run |
| `POST /feedback` | record a verdict over HTTP |

`/stream` with `once=1` is the one to use from a script: it terminates. Without
`once=1` it is an open stream by design.

## Setup as a scheduled job

The daily pattern is a cron job that collects, emits the digest, and stops. Keep the
prompt self-contained and let the script do the mechanical part:

```bash
cronjob:
  schedule: "0 9 * * *"
  script: /opt/data/repos/hermes-signal-hub/scripts/daily_digest.sh
  deliver: origin
```

The script is idempotent and quiet when there is nothing new, so the job does not
spam the user on a slow news day.

## Pitfalls

- **`OR` between two qualifiers is rejected by the API with HTTP 422.** Topic
  slices are separate entries on purpose. Do not "optimise" them back into
  `topic:a OR topic:b`.
- **Search quota is 30/min, not 30/hour.** Do not throttle daily runs to once an hour.
- **`--db` is global**, it goes before the subcommand.
- **There is no `/rank` HTTP route** — it is `/repos`.
- **Check quota before a run**, not after a 403 storm: `"$HUB" --db "$DB" check`.
