"""Orchestration: one call to run the whole pipeline, or a slice of it.

    collect ──▶ dedupe/store ──▶ momentum score ──▶ publish events
                    │                                      │
                    └── observations (deltas) ◀────── feedback loop (weights)

Everything the CLI, the HTTP API and the cron job do goes through
:class:`SignalHub`, so there is exactly one place where the pipeline order and
its invariants live.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from .collector import (
    CollectionResult,
    Collector,
    CollectorConfig,
    hours_since,
    last_run_floor,
)
from .github import GitHubClient, RepoSnapshot, RequestBudget, SearchClient
from .scoring import FeedbackLearner, PreviousObservation, ScoredRepo, Scorer, ScoreWeights
from .store import Store

log = logging.getLogger("signalhub.hub")


class RateLimitReader(Protocol):
    """Optional capability: a client that can re-read authoritative quota."""

    def rate_limit_snapshot(self, *, charge: bool = True) -> dict[str, Any]: ...


DEFAULT_DB = Path(
    os.environ.get("SIGNALHUB_DB", Path.home() / ".hermes" / "signalhub" / "signalhub.db")
)
TOKEN_ENV_CANDIDATES = (
    "SIGNALHUB_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)


def resolve_token() -> str:
    """Find a GitHub token without ever logging it.

    Order matters: the dedicated variable wins so a user can scope access, and
    the Hermes MCP config is read as a last resort because that is where this
    install already keeps a working fine-grained PAT.
    """

    for var in TOKEN_ENV_CANDIDATES:
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    config = (
        Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes" / "profiles" / "backend-dev"))
        / "config.yaml"
    )
    try:
        import re

        text = config.read_text(encoding="utf-8")
        match = re.search(r"GITHUB_PERSONAL_ACCESS_TOKEN:\s*['\"]?([A-Za-z0-9_]+)", text)
        if match:
            return match.group(1)
    except OSError:
        pass
    raise RuntimeError(
        "No GitHub token found. Set SIGNALHUB_GITHUB_TOKEN (recommended) or GH_TOKEN."
    )


@dataclass
class RunReport:
    """Everything a caller (CLI, HTTP, cron) needs to know about one run."""

    run_id: int
    collection: CollectionResult
    scored: list[ScoredRepo] = field(default_factory=list)
    new_repos: list[str] = field(default_factory=list)
    events_published: int = 0
    weights: dict[str, float] = field(default_factory=dict)
    rate_limit: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "candidates": len(self.collection.repos),
            "ranked": len(self.scored),
            "new_repos": len(self.new_repos),
            "events_published": self.events_published,
            "api_calls": self.collection.api_calls,
            "search_calls": self.collection.search_calls,
            "errors": self.collection.errors,
            "weights": self.weights,
            "rate_limit": self.rate_limit,
            "duration_s": round(self.duration_s, 2),
        }

    def top(self, n: int = 5) -> list[ScoredRepo]:
        return self.scored[:n]


class SignalHub:
    """Facade over collector, scorer and store."""

    def __init__(
        self,
        store: Store | None = None,
        *,
        db_path: str | Path | None = None,
        client: SearchClient | None = None,
        collector_config: CollectorConfig | None = None,
        token: str | None = None,
    ) -> None:
        self.store = store or Store(db_path or DEFAULT_DB)
        self.config = collector_config or CollectorConfig()
        self._token = token
        self._client = client
        self._scorer = Scorer(self.load_weights())

    # -------------------------------------------------------------- weights

    def load_weights(self) -> ScoreWeights:
        return ScoreWeights.from_dict(self.store.get_meta("weights"))

    def save_weights(self, weights: ScoreWeights) -> None:
        self.store.set_meta("weights", weights.as_dict())

    def refresh_scorer(self) -> None:
        self._scorer = Scorer(self.load_weights())

    # ---------------------------------------------------------------- client

    @property
    def client(self) -> SearchClient:
        if self._client is None:
            token = self._token or resolve_token()
            self._client = GitHubClient(
                token,
                budget=RequestBudget(max_requests=max(200, self.config.max_search_calls * 4)),
                min_interval=self.config.min_interval,
            )
        return self._client

    def rate_limit_snapshot(self, *, charge: bool = True) -> dict[str, Any]:
        """Quota state, read from the API when the client can answer it.

        The Protocol deliberately does not require this method, so a stub client
        is free to omit it; we fall back to the headers already observed during
        the run instead of forcing every test double to fake a network call.
        """

        reader = getattr(self.client, "rate_limit_snapshot", None)
        if callable(reader):
            snapshot = cast("RateLimitReader", self.client).rate_limit_snapshot
            try:
                return dict(snapshot(charge=charge))
            except TypeError:
                return dict(snapshot())
            except Exception as exc:  # noqa: BLE001 - diagnostics must not fail a run
                log.warning("rate limit snapshot failed: %s", exc)
        return self.client.rate_limit.as_dict()

    # ------------------------------------------------------------------- run

    def collect(self, *, publish: bool = True, dry_run: bool = False) -> RunReport:
        """Run the full pipeline once.

        ``dry_run`` performs the network calls and scoring but writes nothing,
        which is what you want before enabling a cron job.
        """

        import time

        started = time.monotonic()
        known = int(self.store.get_meta("repo_count", 0) or 0)
        floor = last_run_floor(self.store.last_successful_run(), self.config.min_stars)
        cfg = CollectorConfig(
            **{
                **self.config.as_dict(),
                "min_stars": max(self.config.min_stars, floor // 2),
            }
        )
        run_id = 0 if dry_run else self.store.start_run([])
        collector = Collector(self.client, cfg)

        try:
            # Two-phase: the collector needs to know the store size to pick the
            # floor, and the floor decision must be recorded before we start.
            collection = collector.collect(known_repos=known)
        except Exception as exc:
            if not dry_run:
                self.store.finish_run(run_id, status="error", notes=str(exc))
            raise

        previous = self.store.previous_observations()
        self.refresh_scorer()
        scored = self._scorer.rank(collection.repos, previous=_as_previous(previous))

        new_repos: list[str] = []
        events = 0
        if not dry_run:
            # upsert_repos is the single source of truth for "is this new":
            # it reports the rows it inserted rather than guessing from meta.
            _, new_repos = self.store.upsert_repos((s.repo, s.total) for s in scored)
            self.store.set_scores({s.full_name: s.total for s in scored})
            self.store.set_meta("repo_count", self.store.stats()["repos"])
            events = self._publish_new(scored, new_names=new_repos, run_id=run_id)
            self.store.finish_run(
                run_id,
                status="ok",
                candidates=len(collection.repos),
                new_repos=len(new_repos),
                api_calls=collection.api_calls,
                rate_limit=self.client.rate_limit.as_dict(),
            )
            self.store.set_meta("last_collection", collection.summary)

        duration = time.monotonic() - started
        return RunReport(
            run_id=run_id,
            collection=collection,
            scored=scored,
            new_repos=new_repos,
            events_published=events,
            weights=self.load_weights().as_dict(),
            rate_limit=self.client.rate_limit.as_dict(),
            duration_s=duration,
        )

    def _publish_new(self, scored: list[ScoredRepo], *, new_names: list[str], run_id: int) -> int:
        fresh = set(new_names)
        items = [
            (
                s.full_name,
                s.repo.html_url,
                s.total,
                {
                    "stars": s.repo.stars,
                    "stars_per_day": round(s.repo.stars_per_day, 2),
                    "language": s.repo.language,
                    "topics": list(s.repo.topics),
                    "license": s.repo.license,
                    "reasons": s.reasons,
                    "components": {
                        "velocity": round(s.velocity, 4),
                        "engagement": round(s.engagement, 4),
                        "relevance": round(s.relevance, 4),
                        "developer": round(s.developer, 4),
                        "penalty": round(s.penalty, 4),
                    },
                    "description": s.repo.description,
                },
            )
            for s in scored
            if s.full_name in fresh
        ]
        return self.store.publish(items, kind="repo.discovered", run_id=run_id)

    # -------------------------------------------------------------- querying

    def top(
        self, n: int = 10, *, status: str | None = "new", min_stars: int = 0
    ) -> list[ScoredRepo]:
        """Rehydrate the top N from the store, sorted by stored score."""

        records = self.store.list_repos(status=status, min_stars=min_stars, limit=n)
        if not records:
            records = self.store.list_repos(min_stars=min_stars, limit=n)
        return [self._to_scored(r) for r in records]

    def _to_scored(self, record: Any) -> ScoredRepo:
        """Rebuild a ScoredRepo from a stored row.

        The store persists the final score but not the per-component
        breakdown, so the components are recomputed here with the live scorer.
        Hardcoding them to 0.0 (as this used to) is not a cosmetic shortcut:
        ``auto_component`` reads them to decide what a bare ``yes``/``no``
        refers to, and with every component at zero it always guessed
        ``relevance``, so every verdict trained the wrong thing.
        """

        repo = RepoSnapshot(
            full_name=record.full_name,
            owner=record.full_name.split("/")[0],
            name=record.full_name.split("/")[-1],
            html_url=record.html_url,
            description=record.description,
            stars=record.stars,
            forks=record.forks,
            watchers=record.watchers,
            open_issues=0,
            created_at=record.created_at,
            pushed_at=record.pushed_at,
            updated_at=record.last_seen,
            language=record.language,
            topics=record.topics,
            license=record.license,
            is_fork=False,
            is_archived=False,
            has_issues=True,
            has_wiki=False,
            size_kb=0,
            owner_created_at=None,
            owner_followers=None,
            subscribers=record.watchers,
            default_branch="main",
        )
        # Recompute the breakdown against the current weights rather than
        # trusting the stored total: the weights move after every verdict, so
        # a cached breakdown would describe a ranking that no longer exists.
        previous = None
        if record.star_delta is not None:
            previous = PreviousObservation(
                stars=record.stars - record.star_delta,
                observed_at=record.last_seen,
            )
        sc = self._scorer.score(repo, previous=previous)
        # Keep the star delta the store already knows about: it survives across
        # collections, whereas ``previous`` is only a best-effort hint.
        if record.star_delta is not None:
            sc.star_delta = record.star_delta
            sc.stars_per_day_delta = sc.stars_per_day
        return sc

    def search(self, query: str, *, limit: int = 10) -> list[Any]:
        return self.store.search(query, limit=limit)

    def unconsumed(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.read_events(after_id=0, limit=limit, unconsumed_only=True)

    # -------------------------------------------------------------- feedback

    def record_verdict(
        self,
        full_name: str,
        verdict: str,
        *,
        actor: str = "user",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Store a verdict and re-tune the weights.

        This is the optimisation loop: every ``yes``/``no``/``noise`` from the
        user or an agent moves the ranking toward their demonstrated taste.
        """

        weights = self.load_weights()
        learner = FeedbackLearner(weights)
        record = self.store.get_repo(full_name)
        if record is None:
            raise ValueError(f"unknown repository: {full_name}")

        verdict = verdict.lower().strip()
        component: str | None
        # ``yes``/``no`` are bare user verdicts with no component named, so the
        # component has to be *inferred* from the repo's profile. Only an
        # explicit component (``quality``, ``relevance``, ``star_per_day``)
        # names itself. Checking ``yes`` against VERDICT_COMPONENT here would
        # be wrong: it is a polarity, not a component, and routing it there
        # trains a component the user never named.
        if verdict in FeedbackLearner.VERDICT_COMPONENT and verdict not in ("yes", "no"):
            scored = self._to_scored(record)
            component = verdict
        elif verdict in ("yes", "no", "noise"):
            scored = self._to_scored(record)
            component = learner.auto_component(scored) or ("noise" if verdict == "noise" else None)
        else:
            raise ValueError(f"unknown verdict: {verdict}")

        self.store.add_feedback(
            full_name,
            verdict,
            component=component,
            actor=actor,
            score=record.score,
            note=note,
        )
        # ``yes``/``no`` are user-facing shorthands; the learner only speaks in
        # component names, so we hand it the component we just inferred. The
        # polarity has to travel with it -- a "no" on a relevance-failing repo
        # must *lower* the relevance weight, not raise it like a "yes" would.
        effective = component or verdict
        polarity = -1 if verdict == "no" else 1
        updated = learner.apply(
            effective,
            confidence=0.8 if verdict in ("yes", "noise") else 1.0,
            sign=polarity,
        )
        self.save_weights(updated)
        self.refresh_scorer()
        return {
            "full_name": full_name,
            "verdict": verdict,
            "component": component,
            "weights": updated.as_dict(),
        }

    # ------------------------------------------------------------ diagnostics

    def health(self) -> dict[str, Any]:
        last = self.store.last_successful_run()
        return {
            "status": "ok",
            "db": str(self.store.path),
            "stats": self.store.stats(),
            "weights": self.load_weights().as_dict(),
            "hours_since_run": round(hours_since(last), 2),
            "rate_limit": self.store.get_meta("rate_limit", {}),
            "feedback": self.store.feedback_counts(),
        }


def _as_previous(raw: dict[str, tuple[int, Any]]) -> dict[str, Any]:
    """Adapt store observations to the scorer's PreviousObservation type."""

    from .scoring import PreviousObservation

    return {k: PreviousObservation(stars=v[0], observed_at=v[1]) for k, v in raw.items()}
