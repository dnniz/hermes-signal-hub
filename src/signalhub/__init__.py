"""signalhub — discover high-momentum GitHub repositories and route them
into the flows that already exist in a Hermes install.

Public surface:

    from signalhub import SignalHub, CollectorConfig
    hub = SignalHub()
    report = hub.collect()
    print(render_markdown(report.scored))

Everything is stdlib-only; see docs/adr/0002-stdlib-only.md for why.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .collector import CollectionResult, Collector, CollectorConfig
from .github import (
    BudgetExceeded,
    GitHubClient,
    GitHubError,
    RateLimitState,
    RepoSnapshot,
    RequestBudget,
    SecondaryRateLimit,
)
from .hub import RunReport, SignalHub, resolve_token
from .render import (
    Digest,
    render_agent_briefing,
    render_jsonl,
    render_markdown,
    render_stats,
)
from .scoring import (
    FeedbackLearner,
    PreviousObservation,
    ScoredRepo,
    Scorer,
    ScoreWeights,
)
from .store import RepoRecord, Store

__all__ = [
    "BudgetExceeded",
    "CollectionResult",
    "Collector",
    "CollectorConfig",
    "Digest",
    "FeedbackLearner",
    "GitHubClient",
    "GitHubError",
    "PreviousObservation",
    "RateLimitState",
    "RepoRecord",
    "RepoSnapshot",
    "RequestBudget",
    "RunReport",
    "ScoreWeights",
    "ScoredRepo",
    "Scorer",
    "SecondaryRateLimit",
    "SignalHub",
    "Store",
    "__version__",
    "render_agent_briefing",
    "render_jsonl",
    "render_markdown",
    "render_stats",
    "resolve_token",
]
