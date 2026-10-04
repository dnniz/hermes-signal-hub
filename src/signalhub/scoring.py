"""Ranking engine: turn raw repositories into a defensible ordering.

The product requirement is "new repositories with the most stars", but raw
star count is a poor filter for a two-week-old repo in a 200k-repo week. This
module implements a *momentum score* instead:

    score = w_vel * velocity + w_eng * engagement + w_rel * relevance
            + w_dev * developer_quality - spam_penalty

Every component is a bounded, explainable number in [0, 1] and every component
is returned alongside the total so the agent (or the human) can see *why* a
repo ranked where it did. Weights live in the store and are updated from
feedback, so the ranking drifts toward what this user actually cares about.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from .github import RepoSnapshot, utcnow

# Topics that mark a repo as adjacent to this user's stack. Weights are
# deliberately modest: relevance is a tiebreaker, not the ranking signal.
RELEVANCE_TOPICS: dict[str, float] = {
    "ai": 1.0,
    "llm": 1.0,
    "agent": 0.95,
    "agents": 0.95,
    "agentic": 0.95,
    "mcp": 0.9,
    "rag": 0.85,
    "inference": 0.8,
    "fine-tuning": 0.8,
    "machine-learning": 0.75,
    "deep-learning": 0.7,
    "automation": 0.7,
    "workflow": 0.6,
    "devops": 0.7,
    "infrastructure": 0.65,
    "kubernetes": 0.6,
    "docker": 0.6,
    "observability": 0.6,
    "postgresql": 0.6,
    "database": 0.55,
    "api": 0.5,
    "cli": 0.5,
    "developer-tools": 0.6,
    "programming": 0.4,
    "python": 0.5,
    "typescript": 0.45,
    "rust": 0.4,
    "go": 0.35,
    "latam": 0.8,
    "español": 0.7,
    "spanish": 0.6,
    "open-source": 0.4,
    "self-hosted": 0.65,
    "privacy": 0.6,
    "local-first": 0.7,
}

RELEVANCE_LANGUAGES: dict[str, float] = {
    "Python": 0.6,
    "TypeScript": 0.5,
    "Go": 0.45,
    "Rust": 0.4,
    "JavaScript": 0.35,
    "Shell": 0.4,
}

# Marketing-speak patterns that inflate stars without producing usable code.
SPAM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(100%\s*free|no\s*cost|limited\s*time)\b",
        r"\b(download\s+now|buy\s+now|order\s+now)\b",
        r"\b(guest\s*post|seo\s*service|backlink)\b",
        r"\b(crypto|airdrop|presale|ico\b|web3\b)\b",
        r"\b(casino|betting|viagra|porn)\b",
        r"\b(weight\s*loss|supplement|nutrition\s*system)\b",
        r"\b(hack|cheat|crack|keygen)\b",
    )
)

# Legitimate repo names/titles that would otherwise trip the spam regexes.
SPAM_ALLOWLIST = ("awesome-", "awesome/", "free-for-dev", "public-apis")

GENERIC_TOPIC_STOPWORDS = {"free", "list", "collection", "tutorial", "beginner", "guide"}


@dataclass
class ScoreWeights:
    """Tunable ranking weights, persisted between runs."""

    velocity: float = 0.40
    engagement: float = 0.20
    relevance: float = 0.25
    developer: float = 0.15
    penalty_scale: float = 1.0

    def normalised(self) -> ScoreWeights:
        """Return a copy whose positive weights sum to 1.0.

        The penalty scale is intentionally excluded: it multiplies a penalty
        rather than competing with the positive terms.

        When every weight is zero we fall back to a flat 0.25 each. Returning
        the class defaults instead would silently swap in a *different* profile
        (0.40/0.20/0.25/0.15) that nobody asked for -- an explicit "I don't care"
        must not become an implicit "velocity matters most".
        """

        total = self.velocity + self.engagement + self.relevance + self.developer
        if total <= 0:
            return ScoreWeights(
                velocity=0.25,
                engagement=0.25,
                relevance=0.25,
                developer=0.25,
                penalty_scale=self.penalty_scale,
            )
        return ScoreWeights(
            velocity=self.velocity / total,
            engagement=self.engagement / total,
            relevance=self.relevance / total,
            developer=self.developer / total,
            penalty_scale=self.penalty_scale,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "velocity": round(self.velocity, 4),
            "engagement": round(self.engagement, 4),
            "relevance": round(self.relevance, 4),
            "developer": round(self.developer, 4),
            "penalty_scale": round(self.penalty_scale, 4),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ScoreWeights:
        raw = raw or {}
        base = cls()
        return cls(
            velocity=float(raw.get("velocity", base.velocity)),
            engagement=float(raw.get("engagement", base.engagement)),
            relevance=float(raw.get("relevance", base.relevance)),
            developer=float(raw.get("developer", base.developer)),
            penalty_scale=float(raw.get("penalty_scale", base.penalty_scale)),
        )


@dataclass
class ScoredRepo:
    """A repository plus its score breakdown and the raw signals behind it."""

    repo: RepoSnapshot
    total: float
    velocity: float
    engagement: float
    relevance: float
    developer: float
    penalty: float
    outburst: float = 0.0
    reasons: list[str] = field(default_factory=list)
    star_delta: int | None = None
    stars_per_day_delta: float | None = None
    score: float | None = None
    #: Age/velocity as measured by the Scorer's injected clock. The snapshot's
    #: own properties read the wall clock, which would make the JSON payload
    #: disagree with the score computed right next to it.
    age_days: float = 0.0
    stars_per_day: float = 0.0

    @property
    def full_name(self) -> str:
        return self.repo.full_name

    def explain(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "baseline fit"

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.repo.full_name,
            "html_url": self.repo.html_url,
            "description": self.repo.description,
            "stars": self.repo.stars,
            "forks": self.repo.forks,
            "language": self.repo.language,
            "topics": list(self.repo.topics),
            "license": self.repo.license,
            "age_days": round(self.age_days, 2),
            "created_at": self.repo.created_at.isoformat(),
            "pushed_at": self.repo.pushed_at.isoformat(),
            "total_score": round(self.total, 4),
            "components": {
                "velocity": round(self.velocity, 4),
                "engagement": round(self.engagement, 4),
                "relevance": round(self.relevance, 4),
                "developer": round(self.developer, 4),
                "penalty": round(self.penalty, 4),
                "outburst": round(self.outburst, 4),
            },
            "reasons": list(self.reasons),
            "star_delta": self.star_delta,
            "stars_per_day_delta": round(self.stars_per_day_delta, 3)
            if self.stars_per_day_delta is not None
            else None,
            "stars_per_day": round(self.stars_per_day, 2),
        }


class Scorer:
    """Compute momentum scores with explainable components."""

    #: How much a genuine breakout can add on top of the weighted mix. Small
    #: enough to stay a tie-breaker in normal ranges, large enough to put a
    #: 3000+/day repo above a merely steady one.
    OUTBURST_WEIGHT = 0.18

    def __init__(
        self,
        weights: ScoreWeights | None = None,
        *,
        now: datetime | None = None,
        relevance_topics: dict[str, float] | None = None,
    ) -> None:
        self.weights = (weights or ScoreWeights()).normalised()
        self.now = now or utcnow()
        self.relevance_topics = relevance_topics or RELEVANCE_TOPICS

    # ------------------------------------------------------------ components

    def age_days(self, repo: RepoSnapshot) -> float:
        """Repo age measured against the *injected* clock.

        ``RepoSnapshot.age_days`` reads the wall clock, which makes every score
        non-deterministic and untestable. Everything in this scorer goes through
        here so ``Scorer(now=...)`` actually means something.
        """

        return max(0.0, (self.now - repo.created_at).total_seconds() / 86400.0)

    def stars_per_day(self, repo: RepoSnapshot) -> float:
        return repo.stars / max(self.age_days(repo), 0.25)

    def velocity_score(self, repo: RepoSnapshot) -> tuple[float, list[str]]:
        """Reward how fast stars arrive, with a recency bonus.

        Absolute stars/day decays naturally as a repo ages, which is what we
        want: a repo at 500 stars in 3 days matters more than 500 stars in 200
        days.

        The pivot is set at 500 stars/day on purpose. It was 150 before, which
        saturated the component at 1.0 for anything above ~100 stars/day: 40% of
        the total score became a constant and the ranking quietly fell back to
        engagement. 500/day still separates 100/day from 200/day (0.742 vs 0.853)
        while leaving headroom to rank a 2000/day breakout above them.
        """

        spd = self.stars_per_day(repo)
        normalised = _log_norm(spd, pivot=500.0)
        age_bonus = 1.0 + (0.15 if self.age_days(repo) <= 14 else 0.0)
        score = min(1.0, normalised * age_bonus)
        reasons: list[str] = []
        if spd >= 2000:
            reasons.append(f"viral: {spd:.0f} stars/day")
        elif spd >= 500:
            reasons.append(f"fast: {spd:.0f} stars/day")
        elif spd >= 100:
            reasons.append(f"steady: {spd:.0f} stars/day")
        return score, reasons

    def outburst_score(self, repo: RepoSnapshot) -> tuple[float, list[str]]:
        """How far past "successful" this repo actually is.

        ``velocity_score`` is capped at 1.0 by design, so every runaway repo
        (9000 stars in 2 days, 4000 in 3) ties at the ceiling and the ranking
        silently hands the decision to engagement. That is the wrong signal for
        the product goal: a 4500/day breakout is a categorically different find
        from a 250/day one, and the digest should lead with it.

        This is a separate, unbounded-in-spirit but strictly bounded to [0, 1]
        measure using log10 decades, applied as a *bonus* on top of the base
        score rather than as another weighted term, so it cannot be tuned away
        by the feedback loop and does not dilute the other components.
        """

        spd = self.stars_per_day(repo)
        # 100/day -> 0.0, 1k/day -> 0.5, 10k/day -> 1.0
        decades = (math.log10(max(spd, 1.0)) - 2.0) / 2.0
        score = max(0.0, min(1.0, decades))
        reasons: list[str] = []
        if spd >= 3000:
            reasons.append(f"outburst: {spd:.0f} stars/day")
        return score, reasons

    def engagement_score(self, repo: RepoSnapshot) -> tuple[float, list[str]]:
        """Stars per fork and watcher ratio proxy for "people actually use this".

        A repo with 10k stars, 12 forks and 40 subscribers is a readme hit;
        3k stars with 500 forks and 900 watchers is a working tool.
        """

        stars = max(repo.stars, 1)
        fork_ratio = repo.forks / stars
        # Forks saturate: 8% of stars is already a very healthy ratio.
        fork_component = min(1.0, fork_ratio / 0.08)
        watch_component = min(1.0, (repo.watchers / stars) / 0.10)
        issues_open = repo.open_issues
        issue_component = 1.0 if issues_open == 0 else min(1.0, 8 / max(issues_open, 1))
        score = 0.5 * fork_component + 0.3 * watch_component + 0.2 * issue_component

        reasons: list[str] = []
        if fork_ratio >= 0.05:
            reasons.append(f"active use: {fork_ratio * 100:.0f}% fork ratio")
        if repo.watchers / stars >= 0.08:
            reasons.append(f"{repo.watchers} watchers")
        return score, reasons

    def relevance_score(self, repo: RepoSnapshot) -> tuple[float, list[str]]:
        """Topic/language affinity with the user's stack, saturating at 1.0."""

        best = 0.0
        hits: list[str] = []
        for topic in repo.topics:
            t = topic.lower().strip()
            if t in GENERIC_TOPIC_STOPWORDS:
                continue
            weight = self.relevance_topics.get(t)
            if weight is None:
                # Substring match catches "ai-agent", "llm-inference", ...
                for key, value in self.relevance_topics.items():
                    if len(key) >= 3 and (key in t or t in key):
                        weight = max(weight or 0.0, value * 0.85)
                        if weight:
                            break
            if weight:
                best = max(best, weight)
                hits.append(topic)
        if repo.language in RELEVANCE_LANGUAGES:
            lang_w = RELEVANCE_LANGUAGES[repo.language]
            best = max(best, lang_w)
            hits.append(repo.language)
        # A real description is itself a signal of substance.
        if repo.description and len(repo.description) > 40:
            best = max(best, 0.3)
        reasons = [f"on-topic: {', '.join(hits[:3])}"] if hits else []
        return min(1.0, best), reasons

    def developer_score(self, repo: RepoSnapshot) -> tuple[float, list[str]]:
        """Account maturity and maintenance cadence.

        A brand-new account shipping a 10k-star project is sometimes a scam
        and sometimes a launch; the age check keeps it visible but ranks it
        below an established maintainer, and a stale push date is a hard veto.
        """

        score = 0.5
        reasons: list[str] = []
        owner_created = repo.owner_created_at
        if owner_created:
            owner_age_days = (self.now - owner_created).days
            score = min(1.0, 0.35 + owner_age_days / 365.0)
            if owner_age_days < 30 and repo.stars > 2000:
                score *= 0.75
                reasons.append("brand-new account")
            elif owner_age_days > 730:
                reasons.append("established maintainer")
        idle_days = (self.now - repo.pushed_at).days
        if idle_days > 180:
            score *= 0.4
            reasons.append(f"stale: no push in {idle_days}d")
        elif idle_days > 60:
            score *= 0.8
            reasons.append(f"quiet: {idle_days}d since push")
        if repo.size_kb > 0 and repo.stars > 500 and repo.size_kb < 50:
            score *= 0.7
            reasons.append("near-empty repository")
        if not repo.license:
            score *= 0.9
        return max(0.0, min(1.0, score)), reasons

    def spam_penalty(self, repo: RepoSnapshot) -> tuple[float, list[str]]:
        """Detect star-bait: marketing copy, forks, mislabelled topics."""

        reasons: list[str] = []
        penalty = 0.0
        haystack = f"{repo.name} {repo.description or ''} {' '.join(repo.topics)}"
        if not any(tag in repo.full_name.lower() for tag in SPAM_ALLOWLIST):
            for pattern in SPAM_PATTERNS:
                if pattern.search(haystack):
                    penalty += 0.35
                    reasons.append("marketing/spam wording")
                    break
        if repo.is_fork:
            penalty += 0.45
            reasons.append("fork")
        if repo.is_archived:
            penalty += 0.5
            reasons.append("archived")
        topic_stars = sum(1 for t in repo.topics if t.lower() in self.relevance_topics)
        if repo.stars > 1000 and topic_stars == 0 and not repo.description:
            penalty += 0.2
            reasons.append("no substance to justify stars")
        if (
            repo.stars > 1500
            and repo.forks < 0.01 * repo.stars
            and repo.watchers < 0.05 * repo.stars
        ):
            penalty += 0.15
            reasons.append("stars without engagement")
        return min(1.0, penalty), reasons

    # ----------------------------------------------------------------- score

    def score(
        self,
        repo: RepoSnapshot,
        *,
        previous: PreviousObservation | None = None,
    ) -> ScoredRepo:
        """Combine components into the final momentum score."""

        w = self.weights
        vel, vel_reasons = self.velocity_score(repo)
        eng, eng_reasons = self.engagement_score(repo)
        rel, rel_reasons = self.relevance_score(repo)
        dev, dev_reasons = self.developer_score(repo)
        pen, pen_reasons = self.spam_penalty(repo)
        burst, burst_reasons = self.outburst_score(repo)

        delta = None
        spd_delta = None
        if previous is not None:
            delta = repo.stars - previous.stars
            hours = max((self.now - previous.observed_at).total_seconds() / 3600.0, 1.0)
            spd_delta = delta / (hours / 24.0)
            # A repo gaining stars fast *right now* gets a genuine boost that
            # raw star count cannot express.
            if spd_delta >= 50:
                vel = min(1.0, vel * 1.25)
                vel_reasons.append(f"accelerating: +{delta} stars in {hours:.0f}h")

        base = (
            w.velocity * vel + w.engagement * eng + w.relevance * rel + w.developer * dev
        ) - w.penalty_scale * pen
        # The outburst bonus is added after the weighted mix and scaled by the
        # penalty: a starred spam repo must never out-rank a clean breakout.
        # Keeping it outside the mix is deliberate -- it is the signal the whole
        # product exists to surface, so feedback should not be able to dilute it
        # to nothing.
        total = base + self.OUTBURST_WEIGHT * burst * (1.0 - min(1.0, pen))
        total = max(0.0, min(1.0, total))

        reasons = [
            *vel_reasons,
            *burst_reasons,
            *eng_reasons,
            *rel_reasons,
            *dev_reasons,
            *pen_reasons,
        ]
        return ScoredRepo(
            repo=repo,
            total=total,
            velocity=vel,
            engagement=eng,
            relevance=rel,
            developer=dev,
            penalty=pen,
            outburst=burst,
            reasons=reasons,
            star_delta=delta,
            stars_per_day_delta=spd_delta,
            age_days=self.age_days(repo),
            stars_per_day=self.stars_per_day(repo),
        )

    def rank(
        self, repos: list[RepoSnapshot], previous: dict[str, PreviousObservation] | None = None
    ) -> list[ScoredRepo]:
        scored = [self.score(r, previous=(previous or {}).get(r.full_name)) for r in repos]
        # Deterministic ordering: score desc, then stars desc, then name asc.
        scored.sort(key=lambda s: (-s.total, -s.repo.stars, s.repo.full_name))
        for i, item in enumerate(scored, start=1):
            item.score = i
        return scored


@dataclass(frozen=True)
class PreviousObservation:
    """What we knew about a repo in a previous run (for deltas)."""

    stars: int
    observed_at: datetime


def _log_norm(value: float, *, pivot: float) -> float:
    """Logarithmic squash into [0, 1): fast movers separate without outliers.

    ``log1p(value)/log1p(pivot)`` reaches 1.0 at 150 stars/day and is clamped
    above, so a 5k/day repo and a 200/day repo stay distinguishable in the
    explanation but not in the ranking.
    """

    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(pivot))


class FeedbackLearner:
    """Nudge ranking weights from explicit user/agent verdicts.

    The loop is intentionally conservative: a multiplicative nudge bounded by
    ±25% per verdict, on the component the verdict is about. A dozen verdicts
    can re-shape the ranking; a single accidental click cannot destroy it.
    """

    #: component -> the weight it affects, and the sign a verdict pushes it.
    #: A positive verdict means "more of this component means more like this",
    #: so it *raises* that component's weight; a negative one lowers it. Getting
    #: the sign backwards is invisible in the output but silently trains the
    #: ranking away from the user.
    VERDICT_COMPONENT: ClassVar[dict[str, tuple[str | None, int]]] = {
        "star_per_day": ("velocity", +1),
        "relevance": ("relevance", +1),
        "quality": ("developer", +1),
        "noise": (None, +1),  # explicit noise report: increase the penalty scale
    }

    MAX_NUDGE = 0.25
    #: How many raw points of budget the learner may hold above the initial
    #: profile per component. Expressed as a *relative* budget so it does not
    #: depend on which component is being nudged.
    MAX_RELATIVE_GROWTH = 0.60

    def __init__(self, weights: ScoreWeights, *, step: float = 0.06) -> None:
        self.weights = weights
        self.step = step
        # The baseline is the profile we started from. Feedback is allowed to
        # move weights *relative to it*, never without bound.
        self._baseline = {
            "velocity": weights.velocity,
            "engagement": weights.engagement,
            "relevance": weights.relevance,
            "developer": weights.developer,
        }

    def apply(
        self,
        verdict: str,
        *,
        confidence: float = 1.0,
        sign: int = 1,
    ) -> ScoreWeights:
        """Return updated weights for a verdict, without mutating in place.

        The nudge is applied to the *normalised* weights, and the result is
        re-normalised. That sounds redundant but it is the whole point: bumping
        a raw weight and then normalising is a no-op once the weights already
        sum to 1.0 -- the bump gets divided straight back out and the learner
        converges to a single fixed point after the first verdict.

        ``sign`` carries the polarity when the caller resolved it from a
        yes/no pair; it is folded with the component's own direction.
        """

        verdict = verdict.lower().strip()
        if verdict not in self.VERDICT_COMPONENT:
            raise ValueError(f"unknown verdict {verdict!r}")

        weights = ScoreWeights(**self.weights.as_dict())
        strength = self.step * max(0.0, min(1.0, confidence))
        component, base_sign = self.VERDICT_COMPONENT[verdict]
        direction = base_sign * (1 if sign >= 0 else -1)

        if component is None:
            weights.penalty_scale += strength
        else:
            current = getattr(weights, component)
            baseline = self._baseline[component]
            if baseline > 0:
                # Multiplicative nudge on a normalised weight, clamped so a long
                # run of identical verdicts approaches the cap asymptotically
                # instead of saturating on the first one. Negative verdicts get
                # the mirror-image floor: 1 - MAX_RELATIVE_GROWTH of baseline.
                ceiling = baseline * (1.0 + self.MAX_RELATIVE_GROWTH)
                floor = baseline * (1.0 - self.MAX_RELATIVE_GROWTH)
                target = current * (1.0 + direction * strength)
                setattr(weights, component, max(min(target, ceiling), floor))
            else:
                setattr(weights, component, current + direction * strength)
        updated = weights.normalised()
        # Keep our own copy in step so the next call continues from here.
        self.weights = updated
        return updated

    def auto_component(self, scored: ScoredRepo) -> str | None:
        """Infer which component a verdict is about from the repo's profile.

        Lets the CLI accept a bare ``yes``/``no`` and still learn something
        useful: a "no" on a repo that failed on spam wording trains the penalty,
        while a "no" on a relevant-but-slow repo trains relevance.
        """

        if scored.penalty > 0.2:
            return "noise"
        # Relevance is checked before velocity on purpose. When a user says
        # "no", the useful question is "was this even about my topic?" -- a
        # slow-but-relevant repo is a tuning problem, an off-topic one is a
        # relevance problem. Reversing the order sends every off-topic verdict
        # to the velocity weight, where it has no effect on what gets surfaced.
        #
        # The 0.7 threshold sits above the *baseline* relevance an unremarkable
        # repo scores on generic signals alone (licence, activity, owner age).
        # Anything scoring below that is off-topic in a way a user would notice,
        # and only then is "no" really about relevance.
        if scored.relevance < 0.7:
            return "relevance"
        if scored.velocity < 0.35:
            return "star_per_day"
        return "quality"
