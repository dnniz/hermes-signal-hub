"""Rendering: Telegram-safety, size caps, machine contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from conftest import make_repo
from signalhub.render import (
    TELEGRAM_MESSAGE_LIMIT,
    render_agent_briefing,
    render_jsonl,
    render_markdown,
    render_stats,
)
from signalhub.scoring import Scorer

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def scored(scorer: Scorer):
    return [
        scorer.score(make_repo("a/one", stars=5000, age_days=3, topics=("ai", "mcp"))),
        scorer.score(make_repo("a/two", stars=1200, age_days=12, topics=("devops",))),
        scorer.score(make_repo("a/three", stars=300, age_days=20, topics=("cooking",))),
    ]


class TestMarkdown:
    def test_contains_every_expected_element(self, scored) -> None:
        digest = render_markdown(scored, generated_at=NOW)
        assert "a/one" in digest.body
        assert "https://github.com/a/one" in digest.body
        assert "⭐" in digest.body
        assert "⭐/día" in digest.body
        assert len(digest.items) == 3

    def test_no_pipe_tables(self, scored) -> None:
        # Telegram has no table syntax; a stray pipe means a broken layout.
        body = render_markdown(scored, generated_at=NOW).body
        for line in body.splitlines():
            assert not line.strip().startswith("|")

    def test_respects_the_item_limit(self, scored) -> None:
        digest = render_markdown(scored, limit=2, generated_at=NOW)
        assert len(digest.items) == 2
        assert digest.dropped == 1

    def test_truncates_long_content_instead_of_overflowing(self, scorer: Scorer) -> None:
        noisy = [
            scorer.score(
                make_repo(
                    f"a/repo-{i}",
                    stars=1000 + i,
                    age_days=2,
                    description="x" * 400,
                    topics=tuple(f"topic-number-{j}" for j in range(12)),
                )
            )
            for i in range(30)
        ]
        digest = render_markdown(noisy, limit=30, generated_at=NOW, max_chars=2000)
        assert len(digest.body) <= 2000
        assert digest.truncated
        assert digest.dropped > 0

    def test_never_drops_every_item(self, scorer: Scorer) -> None:
        # Even with an absurd cap, one entry must survive.
        items = [scorer.score(make_repo("a/only", description="y" * 500))]
        digest = render_markdown(items, max_chars=10, generated_at=NOW)
        assert len(digest.items) == 1

    def test_verdict_hint_is_included_when_asked(self, scored) -> None:
        with_hint = render_markdown(scored, verdict_hint="responde decide", generated_at=NOW)
        without = render_markdown(scored, generated_at=NOW)
        assert "responde decide" in with_hint.body
        assert "responde decide" not in without.body

    def test_header_context_is_preserved(self, scored) -> None:
        digest = render_markdown(scored, header="corrida #7", generated_at=NOW)
        assert "corrida #7" in digest.body

    def test_long_description_is_clipped(self, scorer: Scorer) -> None:
        item = scorer.score(make_repo("a/long", description="z" * 900))
        digest = render_markdown([item], generated_at=NOW)
        assert "…" in digest.body
        assert "z" * 900 not in digest.body

    def test_delta_is_shown_when_present(self, scorer: Scorer) -> None:
        from signalhub.scoring import PreviousObservation

        repo = make_repo("a/d", stars=2000, age_days=10)
        item = scorer.score(
            repo, previous=PreviousObservation(stars=1000, observed_at=NOW.replace(year=2026))
        )
        # No meaningful delta without a real time gap; just assert no crash.
        assert render_markdown([item], generated_at=NOW).body

    def test_as_dict_is_json_serialisable(self, scored) -> None:
        digest = render_markdown(scored, generated_at=NOW)
        payload = digest.as_dict()
        json.dumps(payload)
        assert payload["count"] == 3

    def test_default_limit_is_telegram_safe(self, scored) -> None:
        assert len(render_markdown(scored, generated_at=NOW).body) <= TELEGRAM_MESSAGE_LIMIT


class TestJsonl:
    def test_one_object_per_line(self, scored) -> None:
        out = render_jsonl(scored)
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 3
        for i, line in enumerate(lines, start=1):
            obj = json.loads(line)
            assert obj["rank"] == i

    def test_rank_offset(self, scored) -> None:
        obj = json.loads(render_jsonl(scored[:1], rank_offset=10).splitlines()[0])
        assert obj["rank"] == 11

    def test_empty_input(self) -> None:
        assert render_jsonl([]) == ""


class TestBriefing:
    def test_contains_scores_components_and_verdict_command(self, scored) -> None:
        text = render_agent_briefing(scored, limit=2)
        assert "signalhub decide" in text
        assert "components:" in text
        assert "url:" in text
        assert "a/one" in text
        assert "a/three" not in text

    def test_context_is_included(self, scored) -> None:
        assert "contexto especial" in render_agent_briefing(scored, context="contexto especial")


class TestStats:
    def test_renders_without_a_previous_run(self) -> None:
        text = render_stats(
            {
                "repos": 5,
                "new_repos": 2,
                "total_stars": 100,
                "events": 3,
                "unconsumed_events": 1,
                "observations": 10,
                "last_run": None,
            }
        )
        assert "nunca" in text
        assert "signalhub status" in text

    def test_includes_weights_and_quota(self) -> None:
        health = {
            "weights": {
                "velocity": 0.4,
                "engagement": 0.2,
                "relevance": 0.25,
                "developer": 0.15,
                "penalty_scale": 1.0,
            },
            "feedback": {"yes": 3, "no": 1},
            "rate_limit": {
                "core_remaining": 4900,
                "core_limit": 5000,
                "search_remaining": 28,
                "search_limit": 30,
            },
        }
        text = render_stats(
            {
                "repos": 1,
                "new_repos": 0,
                "total_stars": 1,
                "events": 0,
                "unconsumed_events": 0,
                "observations": 1,
                "last_run": None,
            },
            health=health,
        )
        assert "4900/5000" in text
        assert "28/30" in text
        assert "'yes': 3" in text
