"""Collector behaviour: the query funnel, budget ceiling and dedup."""

from __future__ import annotations

import pytest

from conftest import make_repo
from signalhub.collector import (
    TOPIC_QUERIES,
    Collector,
    CollectorConfig,
    default_config,
    freshness_sla,
    hours_since,
    last_run_floor,
    prune_observations,
)
from signalhub.github import GitHubClient, RateLimitState, RequestBudget


class FakeClient:
    """Stands in for GitHubClient: serves canned results and counts pages."""

    def __init__(
        self, pages: dict[str, list] | None = None, *, per_query: list | None = None
    ) -> None:
        self.pages = pages or {}
        self.per_query = per_query or []
        self.calls = 0
        self.queries: list[str] = []
        self.min_interval = 0.0
        # Declared so the double actually satisfies SearchClient. Its absence is
        # what let the hub's lazy-client bug ship: the hub silently fell back
        # to a header-only rate_limit, and no test failed.
        self.rate_limit = RateLimitState(core_remaining=4999, core_limit=5000)

    def search_repositories(
        self, query: str, *, per_page: int = 100, max_pages: int = 2, **_: object
    ):
        self.queries.append(query)
        # One HTTP request, however many items it yields -- this is what makes
        # the per-call (not per-item) quota accounting observable in tests.
        self.calls += 1
        results = self.per_query.pop(0) if self.per_query else self.pages.get(query, [])
        yield from (repo for repo in results)

    def rate_limit_snapshot(self) -> dict:
        return {}


class TestQueryBuilding:
    def test_no_query_ors_two_qualifiers(self) -> None:
        # Live-API constraint: ``topic:agent OR topic:agents`` returns HTTP 422
        # ("The search contains only logical operators ... without any search
        # terms"). OR is only valid between bare search terms, never between two
        # qualifiers, so sibling topics must be separate slices.
        for extra in TOPIC_QUERIES:
            assert " OR " not in extra, f"OR between qualifiers is rejected by the API: {extra}"
        client = FakeClient()
        collector = Collector(client, CollectorConfig(days=7, include_topics=True))
        for q in collector.build_queries(floor=200):
            assert " OR " not in q, f"OR between qualifiers is rejected by the API: {q}"

    def test_every_query_is_bounded_by_date_and_stars(self) -> None:
        client = FakeClient()
        collector = Collector(client, CollectorConfig(days=7, min_stars=200, include_topics=True))
        queries = collector.build_queries(floor=200)
        assert queries, "expected at least one query"
        for q in queries:
            assert "created:>" in q
            assert "stars:>" in q
            assert "archived:false" in q

    def test_topics_disabled_shrinks_the_plan(self) -> None:
        client = FakeClient()
        cfg = CollectorConfig(include_topics=False)
        queries = Collector(client, cfg).build_queries(floor=100)
        assert len(queries) == 2
        assert not any("topic:" in q for q in queries)

    def test_date_window_matches_days(self) -> None:
        client = FakeClient()
        queries = Collector(client, CollectorConfig(days=3)).build_queries(floor=10)
        assert "created:>2026-10-01" in queries[0]


class TestAdaptiveFloor:
    def test_cold_store_uses_the_base_floor(self) -> None:
        collector = Collector(FakeClient(), CollectorConfig(min_stars=150, target_candidates=400))
        assert collector.adaptive_floor(known_repos=0) == 150

    def test_warm_store_raises_the_floor(self) -> None:
        collector = Collector(
            FakeClient(), CollectorConfig(min_stars=150, target_candidates=400, floor_step=150)
        )
        assert collector.adaptive_floor(known_repos=400) == 150
        assert collector.adaptive_floor(known_repos=800) == 300
        assert collector.adaptive_floor(known_repos=1200) == 450

    def test_floor_is_capped(self) -> None:
        collector = Collector(
            FakeClient(), CollectorConfig(min_stars=150, max_floor=600, target_candidates=100)
        )
        assert collector.adaptive_floor(known_repos=100_000) == 600

    def test_adaptive_can_be_disabled(self) -> None:
        collector = Collector(FakeClient(), CollectorConfig(min_stars=150, adaptive_floor=False))
        assert collector.adaptive_floor(known_repos=10_000) == 150


class TestCollect:
    def test_deduplicates_across_queries(self) -> None:
        dup = make_repo("a/dup", stars=100)
        other = make_repo("a/other", stars=200)
        client = FakeClient(per_query=[[dup, other], [dup]])
        collector = Collector(client, CollectorConfig(include_topics=True, max_search_calls=10))

        result = collector.collect(known_repos=0)
        names = [r.full_name for r in result.repos]
        assert names.count("a/dup") == 1
        assert set(names) == {"a/dup", "a/other"}
        assert result.duplicates_collapsed == 1

    def test_archives_and_forks_are_filtered_out(self) -> None:
        client = FakeClient(
            per_query=[
                [
                    make_repo("a/fork", is_fork=True),
                    make_repo("a/dead", is_archived=True),
                    make_repo("a/ok"),
                ]
            ]
        )
        result = Collector(client, CollectorConfig(include_topics=False)).collect()
        assert [r.full_name for r in result.repos] == ["a/ok"]

    def test_one_failing_query_does_not_kill_the_run(self) -> None:
        class FlakyClient(FakeClient):
            def search_repositories(self, query: str, **kwargs):
                if not self.queries:
                    self.queries.append(query)
                    raise RuntimeError("500 boom")
                self.queries.append(query)
                yield from (make_repo("a/survivor"),)

        client = FlakyClient()
        result = Collector(
            client, CollectorConfig(include_topics=False, max_search_calls=10)
        ).collect()
        assert [r.full_name for r in result.repos] == ["a/survivor"]
        assert result.errors

    def test_search_call_budget_is_respected(self) -> None:
        client = FakeClient(per_query=[[make_repo(f"a/{i}") for i in range(50)] for _ in range(50)])
        collector = Collector(client, CollectorConfig(include_topics=True, max_search_calls=3))
        result = collector.collect(known_repos=0)
        # One query = one unit of quota, no matter how many repos it returns.
        # The old per-item counter exhausted 3 "calls" on the first page alone
        # (3 repos in) and never reached the second query.
        assert result.search_calls == 3
        assert client.calls == 3
        assert len(client.queries) == 3

    def test_a_wide_first_page_does_not_starve_later_queries(self) -> None:
        # The regression this pins down: a first page returning 100 repos used
        # to consume 100 units of a 10-call budget, so the run stopped after one
        # query and the topic slices never ran. Now one page is one call, and
        # the remaining queries still get their turn.
        client = FakeClient(per_query=[[make_repo(f"a/wide{i}") for i in range(100)]])
        collector = Collector(client, CollectorConfig(include_topics=True, max_search_calls=10))
        result = collector.collect(known_repos=0)
        assert len(result.repos) == 100
        assert result.search_calls == len(client.queries)
        assert result.search_calls < 10  # the budget was never starved
        assert len(client.queries) > 1  # more than the first query ran

    def test_results_are_sorted_by_stars(self) -> None:
        client = FakeClient(
            per_query=[[make_repo("a/low", stars=10), make_repo("a/high", stars=9000)]]
        )
        result = Collector(client, CollectorConfig(include_topics=False)).collect()
        assert [r.stars for r in result.repos] == [9000, 10]

    def test_summary_is_serialisable(self) -> None:
        result = Collector(
            FakeClient(per_query=[[make_repo("a/x")]]), CollectorConfig(include_topics=False)
        ).collect()
        import json

        json.dumps(result.summary)
        assert result.summary["candidates"] == 1


class TestHelpers:
    def test_default_config_applies_overrides(self) -> None:
        cfg = default_config(days=3, min_stars=99)
        assert (cfg.days, cfg.min_stars) == (3, 99)

    def test_prune_delegates_to_store(self, store) -> None:
        from signalhub.store import Store  # noqa: F401

        store.upsert_repos([(make_repo("a/x"), 0.5)])
        assert prune_observations(store, keep_days=60) == 0

    def test_last_run_floor_recovers_previous_floor(self) -> None:
        assert last_run_floor({"queries": '["created:>x stars:>450"]'}, 100) == 450

    def test_last_run_floor_handles_missing_or_broken_data(self) -> None:
        assert last_run_floor(None, 100) == 100
        assert last_run_floor({"queries": "[]"}, 100) == 100
        assert last_run_floor({"queries": '["no stars here"]'}, 100) == 100
        assert last_run_floor({"queries": "not json"}, 100) == 100

    def test_hours_since_last_run(self) -> None:
        assert hours_since(None) == 1e9
        assert hours_since({"finished_at": "2000-01-01T00:00:00+00:00"}) > 0

    def test_freshness_sla_classification(self) -> None:
        assert freshness_sla(1) == "ok"
        assert freshness_sla(30) == "lagging"
        assert freshness_sla(100) == "stale"


class TestBudget:
    def test_budget_raises_when_exhausted(self) -> None:
        from signalhub.github import BudgetExceeded

        budget = RequestBudget(max_requests=2)
        budget.charge("search")
        budget.charge("search")
        with pytest.raises(BudgetExceeded):
            budget.charge("search")

    def test_real_client_requires_a_token(self) -> None:
        with pytest.raises(ValueError, match="token"):
            GitHubClient("")
