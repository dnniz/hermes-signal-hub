#!/usr/bin/env bash
# Daily Signal Hub digest.
#
# Discovers new high-momentum repos and prints a Telegram-ready markdown digest on
# stdout. Stays silent when this run found nothing new, so a scheduled job never
# pings you on a slow news day.
#
# Three "seen" notions live in the store and mixing them up is the easiest way to
# build a digest that repeats itself forever:
#
#   events.consumed_by   the event bus, for machine consumers (other flows)
#   repos.status         'new' -> 'seen', for the human digest
#   run_id               which discovery run produced an event
#
# The digest is driven by the BUS filtered to the latest run, then those exact
# repos are acked. Reading `rank --status new` instead would show the entire
# backlog -- after a week of silent runs that is hundreds of repos, and the top
# slice of it is re-sent every single day.
#
# Safe to run repeatedly: collect is idempotent and the store remembers every repo.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${SIGNALHUB_DB:-/opt/data/.signalhub/signal.db}"
WINDOW_DAYS="${SIGNALHUB_DAYS:-21}"
MIN_STARS="${SIGNALHUB_MIN_STARS:-200}"
MAX_CALLS="${SIGNALHUB_MAX_SEARCH_CALLS:-25}"
TARGET="${SIGNALHUB_TARGET:-300}"
LIMIT="${SIGNALHUB_LIMIT:-8}"
PREFIX="${SIGNALHUB_ACK_PREFIX:-daily-digest}"
HUB="$PROJECT_DIR/scripts/signalhub"

mkdir -p "$(dirname "$DB")"

# Collect. stdout is the run summary, which we discard; stderr carries warnings
# (quota, a malformed topic slice) and is passed through so they stay visible.
if ! "$HUB" --db "$DB" --days "$WINDOW_DAYS" --min-stars "$MIN_STARS" \
     --max-search-calls "$MAX_CALLS" --target "$TARGET" collect >/dev/null; then
  echo "signalhub: discovery run failed; skipping digest" >&2
  exit 1
fi

# What THIS run discovered, top-ranked, and nothing else. Reading the bus rather
# than `rank` is what keeps the daily message to one slice instead of the whole
# unacknowledged backlog.
DELIVERED="$("$HUB" --db "$DB" --json events --latest-run --limit "$LIMIT" \
  | python3 -c 'import json,sys; print("\n".join(dict.fromkeys(e["full_name"] for e in json.load(sys.stdin))))')"

if [ -z "$DELIVERED" ]; then
  exit 0
fi

# Mark them delivered on both sides of the bus, so neither the human digest nor a
# machine consumer re-sends them.
REPOS=$(printf '%s\n' "$DELIVERED" | tr '\n' ' ')
# shellcheck disable=SC2086 # deliberate word splitting: ack takes a repo list
"$HUB" --db "$DB" ack $REPOS --prefix "$PREFIX" >/dev/null
"$HUB" --db "$DB" events --latest-run --consume "$PREFIX" --limit "$LIMIT" >/dev/null

"$HUB" --db "$DB" digest --limit "$LIMIT"
