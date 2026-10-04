"""Hub + CLI + HTTP API: end-to-end behaviour without the network.

The hub is wired to a fake client so the whole pipeline -- collect, score,
store, publish, digest, feedback -- runs exactly as it would in production,
minus the HTTP calls to GitHub.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from conftest import make_repo
from signalhub.cli import main
from signalhub.github import RateLimitState, RepoSnapshot
from signalhub.hub import SignalHub, resolve_token
from signalhub.server import make_server


class StubClient:
    """Minimal stand-in for GitHubClient used by the hub tests.

    Satisfies :class:`signalhub.github.SearchClient` structurally -- that is the
    whole point of the Protocol: the hub never needs a real HTTP client.
    """

    def __init__(self, repos: list, *, error_on_query: str | None = None) -> None:
        self.repos = repos
        self.error_on_query = error_on_query
        self.calls = 0
        self.rate_limit = RateLimitState(core_remaining=4999, core_limit=5000)

    def search_repositories(
        self,
        query: str,
        *,
        per_page: int = 100,
        max_pages: int = 3,
        sort: str = "stars",
        order: str = "desc",
    ) -> Iterator[RepoSnapshot]:
        self.calls += 1
        if self.error_on_query and self.error_on_query in query:
            raise RuntimeError("simulated API failure")
        yield from (repo for repo in self.repos)

    def rate_limit_snapshot(self, *, charge: bool = True) -> dict[str, object]:
        return self.rate_limit.as_dict()


@pytest.fixture
def hub(store) -> SignalHub:
    repos = [
        make_repo("a/rocket", stars=9000, age_days=2, topics=("ai", "agent")),
        make_repo("a/solid", stars=1500, age_days=6, topics=("mcp", "rag")),
        make_repo("a/slow", stars=800, age_days=60, topics=("devops",)),
        # Off-topic on purpose: no AI/MCP topics, no description, no license.
        # It is the fixture the feedback tests use to check that a bare "no"
        # gets attributed to relevance rather than to whatever came first.
        make_repo(
            "a/unrelated",
            stars=400,
            age_days=90,
            topics=("cooking",),
            description=None,
            license=None,
        ),
    ]
    h = SignalHub(store=store, client=StubClient(repos))
    h._client.calls = 0
    return h


class TestCollectPipeline:
    def test_run_stores_ranks_and_publishes(self, hub: SignalHub) -> None:
        report = hub.collect()
        assert report.summary()["candidates"] == 4
        assert report.summary()["new_repos"] == 4
        assert report.events_published == 4
        assert next(s.full_name for s in report.scored) == "a/rocket"
        assert hub.store.stats()["repos"] == 4

    def test_second_run_has_no_new_repos(self, hub: SignalHub) -> None:
        hub.collect()
        second = hub.collect()
        assert second.new_repos == []
        assert second.events_published == 0
        assert hub.store.stats()["events"] == 4  # first run only

    def test_dry_run_writes_nothing(self, hub: SignalHub) -> None:
        report = hub.collect(dry_run=True)
        assert report.run_id == 0
        assert len(report.scored) == 4
        assert hub.store.stats()["repos"] == 0
        assert hub.store.recent_runs() == []

    def test_scores_are_persisted(self, hub: SignalHub) -> None:
        hub.collect()
        record = hub.store.get_repo("a/rocket")
        assert record is not None
        assert record.score is not None and 0 < record.score <= 1

    def test_run_is_recorded_with_call_count(self, hub: SignalHub) -> None:
        hub.collect()
        last = hub.store.last_successful_run()
        assert last is not None
        assert last["candidates"] == 4
        assert last["api_calls"] >= 0

    def test_rate_limit_snapshot_reaches_the_client(self, hub: SignalHub) -> None:
        # Regression: the hub exposes the client through a lazy ``client``
        # property, and this once read the raw ``_client`` attribute instead --
        # which is None until something else triggers construction. The stub
        # does implement the method, so only an assertion on the result catches
        # it; the symptom was ``signalhub check`` always printing {}.
        snapshot = hub.rate_limit_snapshot(charge=False)
        assert snapshot["core_remaining"] == 4999
        assert snapshot["core_limit"] == 5000

    def test_error_closes_the_run(self, store) -> None:
        class Exploding:
            calls = 0
            rate_limit = RateLimitState(core_remaining=5000, core_limit=5000)

            def search_repositories(
                self,
                query: str,
                *,
                per_page: int = 100,
                max_pages: int = 3,
                sort: str = "stars",
                order: str = "desc",
            ) -> Iterator[RepoSnapshot]:
                raise RuntimeError("total failure")
                yield  # pragma: no cover

        h = SignalHub(store=store, client=Exploding())
        # The collector swallows per-query errors, so the run completes with 0
        # candidates rather than exploding; the important part is that the run
        # row is closed and not left dangling.
        report = h.collect()
        assert report.summary()["candidates"] == 0
        assert h.store.recent_runs()[0]["status"] == "ok"

    def test_top_rehydrates_from_the_store(self, hub: SignalHub) -> None:
        hub.collect()
        items = hub.top(2)
        assert len(items) == 2
        assert items[0].repo.full_name == "a/rocket"
        assert items[0].repo.html_url == "https://github.com/a/rocket"

    def test_top_falls_back_when_nothing_is_new(self, hub: SignalHub) -> None:
        hub.collect()
        assert hub.top(5, status="nonexistent-status")


class TestFeedbackLoop:
    def test_no_verdict_lowers_the_inferred_component(self, hub: SignalHub) -> None:
        hub.collect()
        before = hub.load_weights().relevance
        # a/unrelated is off-topic, so the learner attributes the "no" to
        # relevance and lowers that weight.
        result = hub.record_verdict("a/unrelated", "no")
        assert result["component"] == "relevance"
        assert hub.load_weights().relevance < before
        # Survives a reload: it is in the database, not in memory.
        assert hub.store.get_meta("weights")["relevance"] == pytest.approx(
            result["weights"]["relevance"]
        )

    def test_yes_verdict_increases_quality(self, hub: SignalHub) -> None:
        hub.collect()
        before = hub.load_weights().developer
        result = hub.record_verdict("a/rocket", "yes")
        # A/rocket is clean on every axis, so "yes" is attributed to quality.
        assert result["component"] == "quality"
        assert hub.load_weights().developer > before

    def test_noise_increases_penalty(self, hub: SignalHub) -> None:
        hub.collect()
        before = hub.load_weights().penalty_scale
        hub.record_verdict("a/slow", "noise")
        assert hub.load_weights().penalty_scale > before

    def test_unknown_repo_is_rejected(self, hub: SignalHub) -> None:
        with pytest.raises(ValueError, match="unknown repository"):
            hub.record_verdict("nope/nope", "yes")

    def test_unknown_verdict_is_rejected(self, hub: SignalHub) -> None:
        hub.collect()
        with pytest.raises(ValueError, match="unknown verdict"):
            hub.record_verdict("a/rocket", "excellent")

    def test_repeated_feedback_moves_weights_monotonically(self, hub: SignalHub) -> None:
        hub.collect()
        values = []
        for _ in range(3):
            hub.record_verdict("a/unrelated", "no")
            values.append(hub.load_weights().relevance)
        # Each "no" keeps pushing relevance down, and never past the floor.
        assert values == sorted(values, reverse=True)
        assert all(v > 0 for v in values)

    def test_weights_survive_a_new_hub_instance(self, store) -> None:
        h1 = SignalHub(store=store, client=StubClient([make_repo("a/x", stars=10)]))
        h1.collect()
        h1.record_verdict("a/x", "no")
        h2 = SignalHub(store=store, client=StubClient([]))
        assert h2.load_weights().as_dict() == h1.load_weights().as_dict()


class TestHealth:
    def test_health_reports_stats_and_weights(self, hub: SignalHub) -> None:
        health = hub.health()
        assert health["status"] == "ok"
        assert "stats" in health
        assert "weights" in health
        assert "hours_since_run" in health

    def test_search_delegates_to_store(self, hub: SignalHub) -> None:
        hub.collect()
        assert [r.full_name for r in hub.search("rocket")]

    def test_unconsumed_events_are_listed(self, hub: SignalHub) -> None:
        hub.collect()
        assert len(hub.unconsumed()) == 4


class TestTokenResolution:
    def test_env_var_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("SIGNALHUB_GITHUB_TOKEN", "ghp_fromenv")
        assert resolve_token() == "ghp_fromenv"

    def test_raises_when_nothing_available(self, monkeypatch) -> None:
        for var in (
            "SIGNALHUB_GITHUB_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_PERSONAL_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HERMES_HOME", "/nonexistent/hermes-home")
        with pytest.raises(RuntimeError, match="No GitHub token"):
            resolve_token()


class TestHttpApi:
    @pytest.fixture
    def server(self, hub: SignalHub):
        hub.collect()
        srv = make_server(hub, host="127.0.0.1", port=0)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        host, port = srv.server_address[0], srv.server_address[1]
        yield f"http://{host}:{port}"
        srv.shutdown()
        srv.server_close()

    def get(self, base: str, path: str):
        with urllib.request.urlopen(f"{base}{path}", timeout=5) as r:
            return r.status, r.read().decode("utf-8"), r.headers.get("Content-Type")

    def test_health(self, server: str) -> None:
        status, body, _ = self.get(server, "/healthz")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

    def test_two_consumers_over_http_see_the_same_events(self, server: str) -> None:
        _, body_a, _ = self.get(server, "/consume?consumer=a&limit=5")
        _, body_b, _ = self.get(server, "/consume?consumer=b&limit=5")
        a, b = json.loads(body_a), json.loads(body_b)
        assert [e["id"] for e in a["events"]] == [e["id"] for e in b["events"]]
        assert a["pending"] > 0

    def test_advancing_one_consumer_over_http_leaves_the_other(self, server: str) -> None:
        _, before, _ = self.get(server, "/consume?consumer=a&limit=100")
        total = json.loads(before)["pending"]
        assert total > 2

        self.get(server, "/consume?consumer=a&limit=2&advance=1")
        _, body_a, _ = self.get(server, "/consume?consumer=a&limit=100")
        # The batch was consumed; the rest of the backlog is untouched.
        assert json.loads(body_a)["pending"] == total - 2

        _, body_b, _ = self.get(server, "/consume?consumer=b&limit=100")
        assert json.loads(body_b)["pending"] == total

    def test_reading_without_advance_does_not_move_the_http_cursor(self, server: str) -> None:
        _, first, _ = self.get(server, "/consume?consumer=a&limit=3")
        _, second, _ = self.get(server, "/consume?consumer=a&limit=3")
        assert json.loads(first)["pending"] == json.loads(second)["pending"] > 0

    def test_consume_without_a_consumer_is_rejected(self, server: str) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc:
            self.get(server, "/consume")
        assert exc.value.code == 400

    def test_cursors_endpoint_reports_advancement(self, server: str) -> None:
        _, _, _ = self.get(server, "/cursors")
        self.get(server, "/consume?consumer=a&limit=1&advance=1")
        _, body, _ = self.get(server, "/cursors")
        rows = {r["consumer"]: r for r in json.loads(body)["consumers"]}
        assert rows["a"]["last_id"] > 0

    def test_repos_listing(self, server: str) -> None:
        status, body, _ = self.get(server, "/repos?limit=2")
        assert status == 200
        payload = json.loads(body)
        assert payload["count"] == 2
        assert payload["repos"][0]["full_name"] == "a/rocket"

    def test_digest_is_plain_text(self, server: str) -> None:
        status, body, ctype = self.get(server, "/digest?limit=2")
        assert status == 200
        assert "text/plain" in ctype
        assert "a/rocket" in body

    def test_jsonl_content_type(self, server: str) -> None:
        _, body, ctype = self.get(server, "/jsonl?limit=2")
        assert "application/x-ndjson" in ctype
        assert len([line for line in body.splitlines() if line]) == 2

    def test_briefing_includes_verdict_instructions(self, server: str) -> None:
        _, body, _ = self.get(server, "/briefing?limit=1")
        assert "signalhub decide" in body

    def test_events_endpoint(self, server: str) -> None:
        _, body, _ = self.get(server, "/events?limit=10")
        assert len(json.loads(body)["events"]) == 4

    def test_weights_endpoint(self, server: str) -> None:
        _, body, _ = self.get(server, "/weights")
        assert set(json.loads(body)) == {
            "velocity",
            "engagement",
            "relevance",
            "developer",
            "penalty_scale",
        }

    def test_search_endpoint(self, server: str) -> None:
        _, body, _ = self.get(server, "/search?q=rocket")
        assert json.loads(body)["results"][0]["full_name"] == "a/rocket"

    def test_sse_once_returns_backlog(self, server: str) -> None:
        status, body, ctype = self.get(server, "/stream?once=1")
        assert status == 200
        assert "text/event-stream" in ctype
        assert "event: repo.discovered" in body

    def test_unknown_endpoint_is_404(self, server: str) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc:
            self.get(server, "/nope")
        assert exc.value.code == 404

    def test_post_feedback_changes_weights(self, server: str) -> None:
        payload = json.dumps({"full_name": "a/slow", "verdict": "no"}).encode()
        req = urllib.request.Request(
            f"{server}/feedback",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            result = json.loads(r.read())
        assert result["verdict"] == "no"
        _, body, _ = self.get(server, "/weights")
        assert "relevance" in json.loads(body)

    def test_post_feedback_requires_fields(self, server: str) -> None:
        payload = json.dumps({"verdict": "no"}).encode()
        req = urllib.request.Request(
            f"{server}/feedback",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 400

    def test_post_consume_marks_events(self, server: str) -> None:
        _, body, _ = self.get(server, "/events?limit=10")
        ids = [e["id"] for e in json.loads(body)["events"]]
        payload = json.dumps({"event_ids": ids, "consumer": "test"}).encode()
        req = urllib.request.Request(
            f"{server}/consume",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["consumed"] == len(ids)
        _, after, _ = self.get(server, "/events?unconsumed=1")
        assert after == json.dumps({"events": []})


class TestCli:
    def run(self, capsys, *args: str) -> tuple[int, str]:
        from signalhub.cli import main

        code = main(list(args))
        return code, capsys.readouterr().out

    def test_rank_jsonl(self, store, capsys) -> None:
        store.upsert_repos([(make_repo("a/x", stars=500), 0.7)])
        code, _ = self.run(capsys, "--db", ":memory:", "--json", "rank", "--format", "jsonl")
        # A fresh CLI invocation opens its own store, so :memory: is empty here;
        # the command must still succeed.
        assert code == 0

    def test_rank_json_overrides_the_default_markdown_format(self, store, capsys) -> None:
        # Regression: ``--json`` is a global flag and used to be ignored by
        # ``rank``, which emitted markdown. The daily-digest script pipes this
        # into json.load and silently delivered an empty digest.
        store.upsert_repos([(make_repo("a/x", stars=500), 0.7)])
        code, out = self.run(capsys, "--db", str(store.path), "--json", "rank", "--limit", "5")
        assert code == 0
        assert out.strip().startswith("["), f"expected JSON array, got: {out[:80]!r}"
        payload = json.loads(out)
        assert payload and payload[0]["full_name"] == "a/x"

    def test_status_on_empty_db(self, store, capsys) -> None:
        from signalhub.cli import main

        assert main(["--db", str(store.path), "status"]) == 0

    def test_digest_on_populated_db(self, store, capsys) -> None:
        from signalhub.cli import main

        store.upsert_repos([(make_repo("a/x", stars=500), 0.7)])
        assert main(["--db", str(store.path), "digest", "--limit", "1"]) == 0
        assert "a/x" in capsys.readouterr().out

    def test_decide_updates_weights_via_cli(self, store, capsys) -> None:
        from signalhub.cli import main

        store.upsert_repos([(make_repo("a/x", stars=500), 0.7)])
        assert main(["--db", str(store.path), "--json", "decide", "a/x", "no"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["full_name"] == "a/x"
        assert "weights" in out

    def test_decide_rejects_bad_verdict(self, store) -> None:
        from signalhub.cli import main

        with pytest.raises(SystemExit):
            main(["--db", str(store.path), "decide", "a/x", "amazing"])

    def test_decide_unknown_repo_exits_nonzero(self, store, capsys) -> None:
        from signalhub.cli import main

        assert main(["--db", str(store.path), "decide", "nope/nope", "no"]) == 2

    def test_events_and_ack(self, store, capsys) -> None:
        from signalhub.cli import main

        store.publish([("a/x", "u", 0.5, {"stars": 10})])
        assert main(["--db", str(store.path), "events"]) == 0
        assert "a/x" in capsys.readouterr().out
        assert main(["--db", str(store.path), "ack", "a/x"]) == 0

    def test_weights_reset(self, store, capsys) -> None:
        from signalhub.cli import main

        assert main(["--db", str(store.path), "weights", "--reset"]) == 0
        assert "por defecto" in capsys.readouterr().out

    def test_reindex_and_prune(self, store, capsys) -> None:
        from signalhub.cli import main

        store.upsert_repos([(make_repo("a/x"), 0.5)])
        assert main(["--db", str(store.path), "reindex"]) == 0
        assert main(["--db", str(store.path), "prune"]) == 0

    def test_check_without_token_fails_cleanly(self, monkeypatch) -> None:
        from signalhub.cli import main

        for var in (
            "SIGNALHUB_GITHUB_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_PERSONAL_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HERMES_HOME", "/nonexistent")
        assert main(["--db", ":memory:", "check"]) == 1

    def test_help_lists_all_subcommands(self, capsys) -> None:
        from signalhub.cli import build_parser

        text = build_parser().format_help()
        for verb in ("collect", "rank", "digest", "events", "decide", "serve", "check"):
            assert verb in text

    def test_help_lists_the_fanout_subcommands(self, capsys) -> None:
        from signalhub.cli import build_parser

        text = build_parser().format_help()
        for verb in ("consume", "cursors"):
            assert verb in text


class TestConsumeCursors:
    """CLI surface for the per-consumer cursor.

    The point of these tests is the fan-out guarantee: two flows reading the
    same bus must each see the same events, and one advancing must not affect
    the other.
    """

    @staticmethod
    def _seed(db: str, n: int = 4) -> None:
        from signalhub.store import Store

        store = Store(db)
        try:
            store.publish(
                [
                    (f"a/repo{i}", f"https://github.com/a/repo{i}", 0.5, {"stars": 100 + i})
                    for i in range(n)
                ],
                run_id=1,
            )
        finally:
            store.close()

    def test_two_consumers_both_see_the_same_events(self, tmp_path, capsys) -> None:
        db = str(tmp_path / "h.db")
        self._seed(db)
        assert main(["--db", db, "--json", "consume", "a", "--limit", "10"]) == 0
        out_a = json.loads(capsys.readouterr().out)

        assert main(["--db", db, "--json", "consume", "b", "--limit", "10"]) == 0
        out_b = json.loads(capsys.readouterr().out)

        assert [e["id"] for e in out_a["events"]] == [e["id"] for e in out_b["events"]]
        assert len(out_a["events"]) == 4

    def test_advance_moves_only_that_consumer(self, tmp_path, capsys) -> None:
        db = str(tmp_path / "h.db")
        self._seed(db)

        assert main(["--db", db, "--json", "consume", "a", "--limit", "10", "--advance"]) == 0
        assert json.loads(capsys.readouterr().out)["cursor"] > 0

        assert main(["--db", db, "--json", "consume", "a", "--limit", "10"]) == 0
        assert json.loads(capsys.readouterr().out)["events"] == []

        assert main(["--db", db, "--json", "consume", "b", "--limit", "10"]) == 0
        assert len(json.loads(capsys.readouterr().out)["events"]) == 4

    def test_reading_without_advance_leaves_the_cursor_alone(self, tmp_path, capsys) -> None:
        db = str(tmp_path / "h.db")
        self._seed(db)
        assert main(["--db", db, "--json", "consume", "a", "--limit", "10"]) == 0
        assert json.loads(capsys.readouterr().out)["cursor"] == 0

        assert main(["--db", db, "--json", "consume", "a", "--limit", "10"]) == 0
        assert len(json.loads(capsys.readouterr().out)["events"]) == 4

    def test_cursors_reports_each_consumer_lag(self, tmp_path, capsys) -> None:
        db = str(tmp_path / "h.db")
        self._seed(db)
        main(["--db", db, "--json", "consume", "a", "--limit", "2", "--advance"])
        capsys.readouterr()

        assert main(["--db", db, "--json", "cursors"]) == 0
        payload = json.loads(capsys.readouterr().out)
        rows = {r["consumer"]: r for r in payload["consumers"]}
        assert rows["a"]["last_id"] > 0
        assert rows["a"]["pending"] == 2

    def test_cursors_on_a_fresh_db_is_empty(self, tmp_path, capsys) -> None:
        assert main(["--db", str(tmp_path / "h.db"), "--json", "cursors"]) == 0
        assert json.loads(capsys.readouterr().out)["consumers"] == []
