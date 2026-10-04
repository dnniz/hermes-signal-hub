"""Store behaviour: upserts, deltas, events, feedback, FTS."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from conftest import make_repo
from signalhub.store import Store


def upsert(store: Store, repos, scores=None) -> tuple[int, list[str]]:
    scores = scores or {r.full_name: 0.5 for r in repos}
    return store.upsert_repos((r, scores[r.full_name]) for r in repos)


class TestRepos:
    def test_insert_then_update_reports_new_only_once(self, store: Store) -> None:
        total, new = upsert(store, [make_repo("a/one"), make_repo("a/two")])
        assert (total, sorted(new)) == (2, ["a/one", "a/two"])

        total, new = upsert(store, [make_repo("a/one", stars=1500), make_repo("a/two")])
        assert (total, new) == (2, [])

        record = store.get_repo("a/one")
        assert record is not None
        assert record.stars == 1500

    def test_seen_count_increments(self, store: Store) -> None:
        upsert(store, [make_repo("a/one")])
        upsert(store, [make_repo("a/one")])
        upsert(store, [make_repo("a/one")])
        record = store.get_repo("a/one")
        assert record is not None
        assert record.seen_count == 3  # 1 insert + 2 updates
        assert record.observations == 3

    def test_topics_roundtrip(self, store: Store) -> None:
        upsert(store, [make_repo("a/t", topics=("ai", "mcp", "rag"))])
        record = store.get_repo("a/t")
        assert record is not None
        assert record.topics == ("ai", "mcp", "rag")

    def test_star_delta_across_observations(self, store: Store) -> None:
        upsert(store, [make_repo("a/d", stars=100)])
        upsert(store, [make_repo("a/d", stars=180)])
        record = store.get_repo("a/d")
        assert record is not None
        assert record.star_delta == 80

    def test_get_missing_returns_none(self, store: Store) -> None:
        assert store.get_repo("nope/nope") is None

    def test_list_respects_status_and_min_stars(self, store: Store) -> None:
        upsert(store, [make_repo("a/low", stars=50), make_repo("a/high", stars=5000)])
        store.set_status(["a/low"], "seen")
        assert [r.full_name for r in store.list_repos(status="new")] == ["a/high"]
        assert len(store.list_repos(status="any", min_stars=1000)) == 1

    def test_order_by_is_validated(self, store: Store) -> None:
        upsert(store, [make_repo("a/x", stars=10), make_repo("a/y", stars=900)])
        # An injection attempt must fall back to the default ordering.
        results = store.list_repos(order_by="stars; DROP TABLE repos")
        assert len(results) == 2
        assert results[0].full_name == "a/y"

    def test_since_days_filters_old_repos(self, store: Store) -> None:
        upsert(store, [make_repo("a/new", age_days=2), make_repo("a/old", age_days=200)])
        results = store.list_repos(since_days=30)
        assert [r.full_name for r in results] == ["a/new"]

    def test_set_status_returns_rowcount(self, store: Store) -> None:
        upsert(store, [make_repo("a/1"), make_repo("a/2")])
        assert store.set_status(["a/1", "a/2"], "seen") == 2
        assert store.set_status([], "seen") == 0

    def test_prune_observations_respects_retention(self, store: Store) -> None:
        upsert(store, [make_repo("a/x")])
        old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO observations(full_name, observed_at, stars) VALUES(?,?,?)",
                ("a/x", old, 10),
            )
        assert store.prune_observations(keep_days=60) == 1
        assert store.stats()["observations"] >= 1


class TestFullTextSearch:
    def test_finds_by_description_word(self, store: Store) -> None:
        upsert(
            store,
            [
                make_repo(
                    "a/vector", description="A vector database for embeddings", topics=("rag",)
                )
            ],
        )
        results = store.search("embeddings")
        assert [r.full_name for r in results] == ["a/vector"]

    def test_finds_by_topic(self, store: Store) -> None:
        upsert(store, [make_repo("a/mcp", topics=("mcp", "agent"))])
        assert [r.full_name for r in store.search("mcp")] == ["a/mcp"]

    def test_fts5_syntax_error_falls_back_to_like(self, store: Store) -> None:
        upsert(store, [make_repo("a/x", description="plain text here")])
        # A malformed FTS expression must not raise.
        results = store.search('bad"query(((')
        assert isinstance(results, list)

    def test_reindex_is_idempotent(self, store: Store) -> None:
        upsert(store, [make_repo("a/x", description="findme please")])
        assert store.reindex_fts() == 1
        assert store.reindex_fts() == 1
        assert [r.full_name for r in store.search("findme")] == ["a/x"]


class TestEvents:
    def test_publish_and_read(self, store: Store) -> None:
        store.publish(
            [("a/x", "https://github.com/a/x", 0.8, {"stars": 100})],
            kind="repo.discovered",
        )
        events = store.read_events()
        assert len(events) == 1
        assert events[0]["payload"]["stars"] == 100
        assert events[0]["kind"] == "repo.discovered"

    def test_after_id_is_exclusive_cursor(self, store: Store) -> None:
        for i in range(5):
            store.publish([(f"a/{i}", "u", 0.5, {})])
        assert len(store.read_events(after_id=3)) == 2
        assert store.max_event_id() == 5

    def test_unconsumed_filter_and_mark(self, store: Store) -> None:
        store.publish([("a/1", "u", 0.5, {}), ("a/2", "u", 0.5, {})])
        assert len(store.read_events(unconsumed_only=True)) == 2
        ids = [e["id"] for e in store.read_events(unconsumed_only=True)]
        assert store.mark_consumed(ids, "tester") == 2
        assert store.read_events(unconsumed_only=True) == []
        assert len(store.read_events()) == 2

    def test_kind_filter(self, store: Store) -> None:
        store.publish([("a/1", "u", 0.5, {})], kind="repo.discovered")
        store.publish([("a/2", "u", 0.5, {})], kind="repo.updated")
        assert len(store.read_events(kind="repo.updated")) == 1


class TestFeedback:
    def test_feedback_is_recorded_with_component(self, store: Store) -> None:
        upsert(store, [make_repo("a/x")])
        store.add_feedback("a/x", "no", component="relevance", actor="user", score=0.4)
        rows = store.list_feedback(full_name="a/x")
        assert len(rows) == 1
        assert rows[0]["verdict"] == "no"
        assert rows[0]["component"] == "relevance"

    def test_feedback_counts(self, store: Store) -> None:
        upsert(store, [make_repo("a/x"), make_repo("a/y")])
        store.add_feedback("a/x", "yes", component="quality")
        store.add_feedback("a/x", "yes", component="quality")
        store.add_feedback("a/y", "noise", component="noise")
        assert store.feedback_counts() == {"yes": 2, "noise": 1}

    def test_repo_record_includes_feedback_count(self, store: Store) -> None:
        upsert(store, [make_repo("a/x")])
        store.add_feedback("a/x", "yes", component="quality")
        record = store.get_repo("a/x")
        assert record is not None
        assert record.feedback_count == 1


class TestMeta:
    def test_meta_roundtrip(self, store: Store) -> None:
        store.set_meta("weights", {"velocity": 0.5})
        assert store.get_meta("weights") == {"velocity": 0.5}
        assert store.get_meta("missing", "fallback") == "fallback"

    def test_meta_overwrites(self, store: Store) -> None:
        store.set_meta("k", 1)
        store.set_meta("k", 2)
        assert store.get_meta("k") == 2


class TestRuns:
    def test_run_lifecycle(self, store: Store) -> None:
        run_id = store.start_run(["q1", "q2"])
        assert store.last_successful_run() is None  # still running
        store.finish_run(run_id, candidates=10, new_repos=3, api_calls=2)
        last = store.last_successful_run()
        assert last is not None
        assert last["candidates"] == 10
        assert last["new_repos"] == 3
        assert json.loads(last["queries"]) == ["q1", "q2"]

    def test_error_run_is_not_successful(self, store: Store) -> None:
        run_id = store.start_run([])
        store.finish_run(run_id, status="error", notes="boom")
        assert store.last_successful_run() is None

    def test_recent_runs_newest_first(self, store: Store) -> None:
        store.finish_run(store.start_run([]), status="ok")
        store.finish_run(store.start_run([]), status="ok")
        runs = store.recent_runs()
        assert len(runs) == 2
        assert runs[0]["id"] > runs[1]["id"]


class TestStats:
    def test_stats_summarise_state(self, store: Store) -> None:
        upsert(store, [make_repo("a/x", stars=1000), make_repo("a/y", stars=500)])
        store.publish([("a/x", "u", 0.5, {})])
        stats = store.stats()
        assert stats["repos"] == 2
        assert stats["total_stars"] == 1500
        assert stats["max_stars"] == 1000
        assert stats["events"] == 1
        assert stats["unconsumed_events"] == 1
        assert stats["new_repos"] == 2

    def test_stats_without_runs(self, store: Store) -> None:
        assert store.stats()["last_run"] is None


class TestDurability:
    def test_data_survives_reopen(self, tmp_path) -> None:
        path = tmp_path / "hub.db"
        s1 = Store(path)
        s1.upsert_repos([(make_repo("a/persist", stars=777), 0.42)])
        s1.close()

        s2 = Store(path)
        try:
            record = s2.get_repo("a/persist")
            assert record is not None
            assert record.stars == 777
            assert record.score == 0.42
        finally:
            s2.close()

    def test_transaction_rolls_back_on_error(self, store: Store) -> None:
        with pytest.raises(RuntimeError), store.transaction() as conn:
            conn.execute(
                "INSERT INTO repos(full_name, owner, name, html_url, stars, created_at, pushed_at, "
                "first_seen, last_seen) VALUES('a/boom','a','boom','u',1,'x','x','x','x')"
            )
            raise RuntimeError("abort")
        assert store.get_repo("a/boom") is None

    def test_parent_directory_is_created(self, tmp_path) -> None:
        nested = tmp_path / "a" / "b" / "hub.db"
        s = Store(nested)
        try:
            assert nested.exists()
        finally:
            s.close()

    def test_read_events_can_pin_to_one_run(self, store) -> None:
        # The daily digest depends on this: showing "the newest run" is what
        # makes it idempotent, where an unfiltered read would re-send the whole
        # unacknowledged backlog every day.
        store.publish([("a/one", "u1", 0.9, {})], run_id=1)
        store.publish([("a/two", "u2", 0.8, {})], run_id=2)
        store.publish([("a/three", "u3", 0.7, {})], run_id=2)

        every = store.read_events(limit=10)
        assert {e["full_name"] for e in every} == {"a/one", "a/two", "a/three"}

        latest = store.read_events(run_id=2, limit=10)
        assert {e["full_name"] for e in latest} == {"a/two", "a/three"}
        assert all(e["run_id"] == 2 for e in latest)

    def test_run_filter_combines_with_unconsumed(self, store) -> None:
        store.publish([("a/one", "u1", 0.9, {}), ("a/two", "u2", 0.8, {})], run_id=7)
        store.mark_consumed([store.read_events(run_id=7, limit=10)[0]["id"]], "someone")
        left = store.read_events(run_id=7, unconsumed_only=True, limit=10)
        assert [e["full_name"] for e in left] == ["a/two"]
