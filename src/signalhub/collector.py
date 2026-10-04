"""Discovery: which queries to run, and how to keep the cost bounded.

Naive approach: "search everything created this week sorted by stars". That
returns thousands of repos, most of them noise, and burns the 30 requests/hour
search budget on page 1 of a sorted list you will never read.

This module instead builds a *funnel*:

1. a fixed set of high-signal queries (recent + star floor),
2. an adaptive star floor that rises while the cache is cold and relaxes once
   the store already holds enough candidates,
3. a hard request budget, so a bad run costs a bounded number of calls.

The result is a candidate set that is both fresh and small enough to rank.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .github import RepoSnapshot, RequestBudget, SearchClient, utcnow, window

log = logging.getLogger("signalhub.collector")

# Additive topical slices. These catch repos that a pure star sort buries,
# because a 900-star MCP server created today is more relevant than a
# 5k-star SEO toolchain. Always combined with Collector.COMMON_FILTER.
#
# Each entry is ONE qualifier. GitHub's search parser rejects an OR between two
# qualifiers with HTTP 422 ("contains only logical operators ... without any
# search terms") -- verified against the live API, where
# ``topic:agent OR topic:agents`` fails but ``topic:agent`` and the parenthesised
# form both parse, the latter matching nothing. Sibling topics are therefore
# separate slices and get deduplicated downstream by full_name.
TOPIC_QUERIES: tuple[str, ...] = (
    "topic:ai",
    "topic:llm",
    "topic:agent",
    "topic:agents",
    "topic:mcp",
    "topic:devops",
    "topic:self-hosted",
)


@dataclass
class CollectorConfig:
    """Tunables for one collection run."""

    days: int = 14
    min_stars: int = 150
    adaptive_floor: bool = True
    floor_step: int = 150
    max_floor: int = 1500
    target_candidates: int = 400
    per_page: int = 100
    max_pages: int = 2
    max_search_calls: int = 24
    include_topics: bool = True
    min_interval: float = 2.0

    @property
    def star_window(self) -> str:
        return window(self.days)

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "min_stars": self.min_stars,
            "adaptive_floor": self.adaptive_floor,
            "target_candidates": self.target_candidates,
            "max_search_calls": self.max_search_calls,
            "include_topics": self.include_topics,
        }


@dataclass
class CollectionResult:
    """Outcome of a collection run, ready to be stored and ranked."""

    repos: list[RepoSnapshot] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    api_calls: int = 0
    search_calls: int = 0
    duplicates_collapsed: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=utcnow)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "candidates": len(self.repos),
            "queries": len(self.queries),
            "api_calls": self.api_calls,
            "search_calls": self.search_calls,
            "duplicates_collapsed": self.duplicates_collapsed,
            "errors": self.errors,
            "started_at": self.started_at.isoformat(),
        }


class Collector:
    """Runs the discovery funnel and returns unique, fresh candidates."""

    def __init__(self, client: SearchClient, config: CollectorConfig | None = None) -> None:
        self.client = client
        self.config = config or CollectorConfig()
        self.budget = RequestBudget(max_requests=max(200, self.config.max_search_calls * 4))

    # ------------------------------------------------------------------ plans

    def adaptive_floor(self, known_repos: int) -> int:
        """Raise the star floor when we already hold enough candidates.

        Rationale: on day 1 the store is empty and we want broad coverage; once
        the store holds enough repos, a higher floor surfaces only genuine
        movers. It also shrinks each search result set, which is what keeps the
        run inside its budget.
        """

        if not self.config.adaptive_floor:
            return self.config.min_stars
        cfg = self.config
        if known_repos < cfg.target_candidates:
            return cfg.min_stars
        steps = (known_repos - cfg.target_candidates) // max(cfg.target_candidates, 1)
        floor = cfg.min_stars + steps * cfg.floor_step
        return min(floor, cfg.max_floor)

    #: Applied to every query. ``fork:false`` is applied separately to the
    #: fork-free slice below, so a query that omits it still excludes archived
    #: repos and restricts to public ones.
    COMMON_FILTER = "is:public archived:false"

    def build_queries(self, *, floor: int) -> list[str]:
        """Compose the query list, most valuable first.

        Every query is bounded on three axes -- creation window, star floor and
        repo state -- because a single unbounded slice is how a discovery tool
        burns its whole search quota on one request.
        """

        base = f"{self.config.star_window} {self.COMMON_FILTER}"
        queries = [f"{base} stars:>{floor}"]
        queries.append(f"{base} fork:false stars:>{floor}")
        if self.config.include_topics:
            # Topic slices use a lower floor: a 400-star repo tagged `mcp`
            # created this week is more interesting than a 2000-star database
            # tool with no topical signal.
            topic_floor = max(floor // 2, 50)
            queries.extend(
                f"{base} fork:false {extra} stars:>{topic_floor}" for extra in TOPIC_QUERIES
            )
        return queries

    # ------------------------------------------------------------------- run

    def collect(self, *, known_repos: int = 0) -> CollectionResult:
        """Execute the funnel. Partial results are success by design."""

        result = CollectionResult()
        seen: dict[str, RepoSnapshot] = {}
        floor = self.adaptive_floor(known_repos)
        queries = self.build_queries(floor=floor)
        result.queries = queries

        # ``client.calls`` counts every HTTP request, so the baseline is taken
        # once and the difference is the true search cost. Counting repos
        # instead would be wrong: one page yields up to ``per_page`` repos for a
        # single unit of quota, and the previous per-item counter burned the
        # whole budget on the first request and starved every later query.
        calls_at_start = self.client.calls
        for query in queries:
            used = self.client.calls - calls_at_start
            if used >= self.config.max_search_calls:
                log.info("search call budget reached after %d calls", used)
                break
            if len(seen) >= self.config.target_candidates * 2:
                log.info("candidate ceiling reached (%d)", len(seen))
                break
            try:
                for repo in self.client.search_repositories(
                    query,
                    per_page=self.config.per_page,
                    max_pages=self.config.max_pages,
                ):
                    if repo.full_name in seen:
                        result.duplicates_collapsed += 1
                        continue
                    if repo.is_archived or repo.is_fork:
                        continue
                    seen[repo.full_name] = repo
            except Exception as exc:  # noqa: BLE001 - a dead query must not kill the run
                log.warning("query failed (%s): %s", query, exc)
                result.errors.append(f"{query}: {exc}")

        search_calls = self.client.calls - calls_at_start

        result.repos = sorted(seen.values(), key=lambda r: -r.stars)
        result.api_calls = self.client.calls
        result.search_calls = search_calls
        log.info(
            "collected %d unique repos via %d search calls (%d duplicates collapsed)",
            len(result.repos),
            search_calls,
            result.duplicates_collapsed,
        )
        return result

    def iter_trending_windows(self, *, days: int = 7) -> Iterator[str]:
        """Yield overlapping creation windows for a rolling 30-day view.

        Overlap is deliberate: a repo created 10 days ago appears in both the
        14-day and the 21-day window, which is how the store recognises it as
        already known instead of emitting a duplicate "new repo" event.
        """

        for d in (days, days * 2, days * 4):
            yield window(d)


def prune_observations(store: Any, *, keep_days: int = 60) -> int:
    """Keep observation history bounded; see Store.prune_observations."""

    return store.prune_observations(keep_days=keep_days)


def default_config(**overrides: Any) -> CollectorConfig:
    return CollectorConfig(**overrides)


def last_run_floor(last_run: dict[str, Any] | None, default: int) -> int:
    """Recover the star floor used by the previous run for continuity."""

    if not last_run:
        return default
    try:
        queries = last_run.get("queries") or []
        if isinstance(queries, str):
            import json

            queries = json.loads(queries)
        for q in queries:
            if "stars:>" in q:
                return int(q.split("stars:>")[1].split()[0])
    except (ValueError, TypeError, IndexError):
        return default
    return default


def hours_since(last_run: dict[str, Any] | None) -> float:
    if not last_run or not last_run.get("finished_at"):
        return 1e9
    try:
        finished = datetime.fromisoformat(last_run["finished_at"])
    except (ValueError, TypeError):
        return 1e9
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=utcnow().tzinfo)
    return max(0.0, (utcnow() - finished).total_seconds() / 3600.0)


def freshness_sla(hours: float, *, target: float = 24.0) -> str:
    """Classify collection freshness so the digest can warn when stale."""

    if hours <= target:
        return "ok"
    if hours <= target * 2:
        return "lagging"
    return "stale"


def within(days: int, *, now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(days=days)
