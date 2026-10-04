"""Scoring behaviour: velocity, spam, deltas and the feedback loop."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, make_repo
from signalhub.scoring import (
    FeedbackLearner,
    PreviousObservation,
    Scorer,
    ScoreWeights,
)


class TestVelocity:
    def test_fast_repo_scores_higher_than_slow_one(self, scorer: Scorer) -> None:
        fast = scorer.velocity_score(make_repo("a/fast", stars=2000, age_days=4))[0]
        slow = scorer.velocity_score(make_repo("a/slow", stars=2000, age_days=200))[0]
        assert fast > slow

    def test_stars_per_day_is_the_unit(self, scorer: Scorer) -> None:
        # Measured through the Scorer, against its injected clock -- the
        # snapshot's own property reads the wall clock, so a fixture built
        # against a fixed ``now`` would drift by however long the test took.
        assert scorer.stars_per_day(make_repo("a/x", stars=1000, age_days=10)) == pytest.approx(
            100.0
        )
        # A repo younger than 6 hours must not produce an absurd rate.
        assert scorer.stars_per_day(make_repo("a/new", stars=10, age_days=0.01)) > 0

    def test_velocity_is_bounded(self, scorer: Scorer) -> None:
        extreme = scorer.velocity_score(make_repo("a/boom", stars=500_000, age_days=1))[0]
        assert 0.0 <= extreme <= 1.0

    def test_recent_bonus_applies_within_two_weeks(self, scorer: Scorer) -> None:
        recent = scorer.velocity_score(make_repo("a/r", stars=500, age_days=5))[0]
        old = scorer.velocity_score(make_repo("a/o", stars=500, age_days=25))[0]
        assert recent > old


class TestEngagement:
    def test_forks_and_watchers_beat_pure_star_hype(self, scorer: Scorer) -> None:
        used = scorer.engagement_score(make_repo("a/used", stars=3000, forks=300, watchers=400))[0]
        hyped = scorer.engagement_score(make_repo("a/hyped", stars=3000, forks=5, watchers=10))[0]
        assert used > hyped * 1.5

    def test_zero_star_repo_does_not_divide_by_zero(self, scorer: Scorer) -> None:
        score = scorer.engagement_score(make_repo("a/zero", stars=0, forks=0, watchers=0))[0]
        assert 0.0 <= score <= 1.0


class TestRelevance:
    def test_ai_topics_beat_irrelevant(self, scorer: Scorer) -> None:
        on = scorer.relevance_score(make_repo("a/ai", topics=("ai", "mcp")))[0]
        off = scorer.relevance_score(
            make_repo("a/gardening", topics=("recipes", "cooking"), language="HTML")
        )[0]
        assert on > off

    def test_substring_topic_match(self, scorer: Scorer) -> None:
        score = scorer.relevance_score(make_repo("a/x", topics=("ai-agents",)))[0]
        assert score > 0.5

    def test_generic_topics_do_not_count(self, scorer: Scorer) -> None:
        score = scorer.relevance_score(
            make_repo("a/g", topics=("free", "list", "tutorial"), language=None, description=None)
        )[0]
        assert score == 0.0


class TestDeveloperQuality:
    def test_stale_repo_is_penalised(self, scorer: Scorer) -> None:
        fresh = scorer.developer_score(make_repo("a/f", pushed_days_ago=1))[0]
        stale = scorer.developer_score(make_repo("a/s", pushed_days_ago=300))[0]
        assert stale < fresh

    def test_new_account_with_huge_stars_is_flagged(self, scorer: Scorer) -> None:
        score, reasons = scorer.developer_score(make_repo("a/scam", stars=9000, owner_age_days=5))
        assert "brand-new account" in reasons
        assert score < 0.6

    def test_empty_repo_with_many_stars_is_flagged(self, scorer: Scorer) -> None:
        _, reasons = scorer.developer_score(
            make_repo("a/empty", stars=900, size_kb=5, pushed_days_ago=2)
        )
        assert "near-empty repository" in reasons


class TestSpamPenalty:
    @pytest.mark.parametrize(
        "description",
        [
            "Download now! Limited time offer, 100% free crypto airdrop",
            "Best SEO service with guest post and backlink packages",
            "Casino betting platform with instant crypto presale",
        ],
    )
    def test_marketing_copy_is_penalised(self, scorer: Scorer, description: str) -> None:
        penalty, reasons = scorer.spam_penalty(make_repo("a/spam", description=description))
        assert penalty > 0.0
        assert reasons

    def test_legitimate_awesome_list_is_not_flagged(self, scorer: Scorer) -> None:
        penalty, _ = scorer.spam_penalty(
            make_repo(
                "user/awesome-free-for-dev", description="A list of free resources for developers"
            )
        )
        assert penalty == 0.0

    def test_fork_is_penalised(self, scorer: Scorer) -> None:
        penalty, reasons = scorer.spam_penalty(make_repo("a/fork", is_fork=True))
        assert "fork" in reasons
        assert penalty >= 0.45

    def test_archived_is_penalised(self, scorer: Scorer) -> None:
        penalty, reasons = scorer.spam_penalty(make_repo("a/arch", is_archived=True))
        assert "archived" in reasons
        assert penalty >= 0.5

    def test_stars_without_engagement_is_penalised(self, scorer: Scorer) -> None:
        _penalty, reasons = scorer.spam_penalty(
            make_repo("a/ghost", stars=5000, forks=1, watchers=2, topics=("unique-topic",))
        )
        assert any("engagement" in r for r in reasons)


class TestScoreComposition:
    def test_total_is_bounded(self, scorer: Scorer) -> None:
        for repo in (make_repo("a/1"), make_repo("a/2", stars=99999, age_days=1)):
            scored = scorer.score(repo)
            assert 0.0 <= scored.total <= 1.0

    def test_clean_tool_beats_spam_at_equal_stars(self, scorer: Scorer) -> None:
        good = scorer.score(make_repo("a/good", stars=4000, age_days=6, topics=("ai", "agent")))
        bad = scorer.score(
            make_repo(
                "a/bad",
                stars=4000,
                age_days=6,
                topics=("seo",),
                description="Download now! 100% free crypto airdrop presale",
                forks=2,
                watchers=5,
            )
        )
        assert good.total > bad.total

    def test_delta_boosts_accelerating_repo(self, scorer: Scorer) -> None:
        repo = make_repo("a/accel", stars=2000, age_days=10)
        previous = PreviousObservation(stars=1000, observed_at=NOW - timedelta(hours=12))
        scored = scorer.score(repo, previous=previous)
        assert scored.star_delta == 1000
        assert scored.velocity > scorer.velocity_score(repo)[0] * 0.99
        assert any("accelerating" in r for r in scored.reasons)

    def test_no_previous_observation_means_no_delta(self, scorer: Scorer) -> None:
        scored = scorer.score(make_repo("a/x"), previous=None)
        assert scored.star_delta is None

    def test_as_dict_is_json_serialisable(self, scorer: Scorer) -> None:
        import json

        payload = scorer.score(make_repo("a/x")).as_dict()
        json.dumps(payload)
        # ``outburst`` is the breakout bonus; it sits outside the weighted mix.
        assert set(payload["components"]) == {
            "velocity",
            "engagement",
            "relevance",
            "developer",
            "penalty",
            "outburst",
        }


class TestRanking:
    def test_rank_is_deterministic_and_ordered(self, scorer: Scorer) -> None:
        repos = [
            make_repo("a/slow", stars=1000, age_days=100),
            make_repo("a/fast", stars=1500, age_days=3),
            make_repo("a/mid", stars=1200, age_days=10),
        ]
        first = [s.full_name for s in scorer.rank(repos)]
        second = [s.full_name for s in scorer.rank(repos)]
        assert first == second
        assert first[0] == "a/fast"
        assert [s.score for s in scorer.rank(repos)] == [1, 2, 3]

    def test_ties_break_on_stars_then_name(self, scorer: Scorer) -> None:
        # Same age, different stars: 2000 stars in 10 days is 200/day, 1000 is
        # 100/day, so m/same leads on velocity outright. The name is only the
        # last resort, so z/same and a/same both trail it and a/same precedes
        # z/same between them.
        repos = [
            make_repo("z/same", stars=1000, age_days=10),
            make_repo("a/same", stars=1000, age_days=10),
            make_repo("m/same", stars=2000, age_days=10),
        ]
        order = [s.full_name for s in scorer.rank(repos)]
        assert order == ["m/same", "a/same", "z/same"]


class TestWeights:
    def test_normalisation_makes_weights_sum_to_one(self) -> None:
        w = ScoreWeights(velocity=2.0, engagement=2.0, relevance=2.0, developer=2.0).normalised()
        assert w.velocity + w.engagement + w.relevance + w.developer == pytest.approx(1.0)
        # penalty_scale is a multiplier, not a share of the mix: it must survive
        # normalisation untouched.
        assert w.penalty_scale == 1.0

    def test_zero_weights_do_not_divide_by_zero(self) -> None:
        w = ScoreWeights(velocity=0, engagement=0, relevance=0, developer=0).normalised()
        assert w.velocity == 0.25

    def test_relevance_heavy_weights_change_the_order(self) -> None:
        relevant_low_stars = make_repo("a/relevant", stars=500, age_days=5, topics=("ai", "mcp"))
        popular_offtopic = make_repo(
            "a/popular", stars=4000, age_days=20, topics=("recipes",), language="HTML"
        )

        velocity_heavy = Scorer(
            ScoreWeights(velocity=1, relevance=0, engagement=0, developer=0), now=NOW
        )
        relevance_heavy = Scorer(
            ScoreWeights(velocity=0, relevance=1, engagement=0, developer=0), now=NOW
        )

        assert (
            velocity_heavy.rank([relevant_low_stars, popular_offtopic])[0].full_name == "a/popular"
        )
        assert (
            relevance_heavy.rank([relevant_low_stars, popular_offtopic])[0].full_name
            == "a/relevant"
        )


class TestFeedbackLearner:
    def test_noise_increases_penalty_scale(self) -> None:
        learner = FeedbackLearner(ScoreWeights())
        updated = learner.apply("noise")
        assert updated.penalty_scale > 1.0

    def test_relevance_verdict_increases_relevance_weight(self) -> None:
        updated = FeedbackLearner(ScoreWeights()).apply("relevance")
        assert updated.relevance > ScoreWeights().relevance

    def test_nudge_is_bounded(self) -> None:
        w = ScoreWeights()
        learner = FeedbackLearner(w, step=0.06)
        for _ in range(50):
            w = learner.apply("relevance")
        assert w.relevance <= ScoreWeights().relevance + FeedbackLearner.MAX_NUDGE

    def test_unknown_verdict_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown verdict"):
            FeedbackLearner(ScoreWeights()).apply("excellent")

    def test_auto_component_infers_from_profile(self, scorer: Scorer) -> None:
        learner = FeedbackLearner(ScoreWeights())
        spam = scorer.score(
            make_repo("a/s", description="Download now! crypto airdrop presale", topics=("x",))
        )
        off_topic = scorer.score(
            make_repo("a/o", topics=("cooking",), language="HTML", description=None)
        )
        assert learner.auto_component(spam) == "noise"
        assert learner.auto_component(off_topic) == "relevance"

    def test_apply_does_not_mutate_input(self) -> None:
        w = ScoreWeights()
        original = w.as_dict()
        FeedbackLearner(w).apply("quality")
        assert w.as_dict() == original
