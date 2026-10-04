"""GitHub REST client built on the standard library only.

Design constraints discovered by spike (docs/adr/0002-stdlib-only.md):

* The container has no ``pip`` and no third-party wheels, so the HTTP layer is
  ``urllib`` only. Everything here is async-free and blocking by design: the
  collector is a short-lived cron process, not a request handler.
* The Search API does **not** emit an ``ETag``, so conditional revalidation is
  impossible. Quota is instead protected by (a) coarse star thresholds that
  shrink the result set as the cache warms, and (b) an explicit
  ``RequestBudget`` that refuses to exceed a configured number of calls.
* Search responses already embed license, topics and subscriber counts, so the
  collector never fans out into ``/repos/{owner}/{repo}`` per item. That N+1
  elimination is what keeps a daily run at ~30 requests instead of ~130.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

log = logging.getLogger("signalhub.github")

API_ROOT = "https://api.github.com"
#: The only absolute URLs this client may call. Anything else is refused so a
#: crafted path cannot exfiltrate the token to a third-party host.
_ALLOWED_ABSOLUTE_URLS: frozenset[str] = frozenset({API_ROOT})
USER_AGENT = "hermes-signal-hub/1.0 (+https://github.com/dnniz/hermes-signal-hub)"


class GitHubError(RuntimeError):
    """A GitHub API call failed in a way the caller must handle."""

    def __init__(self, status: int, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.retry_after = retry_after


class BudgetExceeded(RuntimeError):
    """The configured request budget for this run is spent."""


class SecondaryRateLimit(GitHubError):
    """GitHub asked us to slow down (403/429 with Retry-After)."""


@dataclass
class RateLimitState:
    """Latest observed rate-limit headers, persisted between runs."""

    core_remaining: int = 0
    core_limit: int = 0
    search_remaining: int = 0
    search_limit: int = 0
    reset_at: datetime | None = None

    @property
    def core_fraction(self) -> float:
        return self.core_remaining / self.core_limit if self.core_limit else 0.0

    @property
    def search_fraction(self) -> float:
        return self.search_remaining / self.search_limit if self.search_limit else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "core_remaining": self.core_remaining,
            "core_limit": self.core_limit,
            "search_remaining": self.search_remaining,
            "search_limit": self.search_limit,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> RateLimitState:
        raw = raw or {}
        reset = raw.get("reset_at")
        return cls(
            core_remaining=int(raw.get("core_remaining", 0)),
            core_limit=int(raw.get("core_limit", 0)),
            search_remaining=int(raw.get("search_remaining", 0)),
            search_limit=int(raw.get("search_limit", 0)),
            reset_at=datetime.fromisoformat(reset) if reset else None,
        )


@dataclass
class RequestBudget:
    """Hard ceiling on API calls for a single run.

    A daily cron that silently burns 400 search requests will be rate limited
    within a week. The budget makes the cost of a run explicit and bounded.
    """

    max_requests: int
    spent: int = 0
    _by_resource: dict[str, int] = field(default_factory=dict)

    def charge(self, resource: str = "core", n: int = 1) -> None:
        self.spent += n
        self._by_resource[resource] = self._by_resource.get(resource, 0) + n
        if self.spent > self.max_requests:
            raise BudgetExceeded(
                f"request budget of {self.max_requests} exhausted after {self.spent - 1} calls"
            )


@dataclass(frozen=True)
class RepoSnapshot:
    """Normalised view of one repository as returned by the Search API.

    Only fields that come free in a single search request are represented, so
    enriching a repo never costs an extra call.
    """

    full_name: str
    owner: str
    name: str
    html_url: str
    description: str | None
    stars: int
    forks: int
    watchers: int
    open_issues: int
    created_at: datetime
    pushed_at: datetime
    updated_at: datetime
    language: str | None
    topics: tuple[str, ...]
    license: str | None
    is_fork: bool
    is_archived: bool
    has_issues: bool
    has_wiki: bool
    size_kb: int
    owner_created_at: datetime | None
    owner_followers: int | None
    subscribers: int
    default_branch: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def age_days(self) -> float:
        return max(0.0, (utcnow() - self.created_at).total_seconds() / 86400)

    @property
    def stars_per_day(self) -> float:
        """Absolute star velocity since creation."""
        return self.stars / max(self.age_days, 0.25)

    @property
    def is_new(self) -> bool:
        return self.age_days <= 30

    @classmethod
    def from_search_item(cls, item: dict[str, Any]) -> RepoSnapshot:
        owner = item.get("owner") or {}
        return cls(
            full_name=item["full_name"],
            owner=item.get("owner", {}).get("login", item["full_name"].split("/")[0]),
            name=item.get("name") or item["full_name"].split("/")[-1],
            html_url=item.get("html_url", f"https://github.com/{item['full_name']}"),
            description=item.get("description"),
            stars=int(item.get("stargazers_count") or 0),
            forks=int(item.get("forks_count") or 0),
            watchers=int(item.get("watchers_count") or 0),
            open_issues=int(item.get("open_issues_count") or 0),
            created_at=parse_ts(item.get("created_at")),
            pushed_at=parse_ts(item.get("pushed_at")),
            updated_at=parse_ts(item.get("updated_at")),
            language=item.get("language"),
            topics=tuple(item.get("topics") or ()),
            license=(item.get("license") or {}).get("spdx_id"),
            is_fork=bool(item.get("fork")),
            is_archived=bool(item.get("archived")),
            has_issues=bool(item.get("has_issues")),
            has_wiki=bool(item.get("has_wiki")),
            size_kb=int(item.get("size") or 0),
            owner_created_at=parse_ts(owner.get("created_at")) if owner.get("created_at") else None,
            owner_followers=int(owner["followers"]) if owner.get("followers") is not None else None,
            subscribers=int(item.get("subscribers_count") or 0),
            default_branch=item.get("default_branch") or "main",
            raw=item,
        )


class SearchClient(Protocol):
    """The single contract every consumer depends on.

    The collector and the hub both take a ``SearchClient``. :class:`GitHubClient`
    satisfies it structurally, and so does a three-line stub in a test, so
    discovery logic never imports or subclasses the HTTP layer.
    """

    calls: int
    rate_limit: RateLimitState

    def search_repositories(
        self,
        query: str,
        *,
        per_page: int = ...,
        max_pages: int = ...,
        sort: str = ...,
        order: str = ...,
    ) -> Iterator[RepoSnapshot]: ...


class GitHubClient:
    """Minimal, polite GitHub REST client.

    Responsibilities are deliberately narrow: build the request, apply retry
    policy, track rate limits and hand back parsed JSON. All discovery logic
    lives in :mod:`signalhub.collector`.
    """

    def __init__(
        self,
        token: str,
        *,
        budget: RequestBudget | None = None,
        timeout: float = 20.0,
        max_retries: int = 4,
        min_interval: float = 0.0,
    ) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self._token = token
        self.budget = budget or RequestBudget(max_requests=500)
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.rate_limit = RateLimitState()
        self.calls = 0
        self._last_call = 0.0

    # ------------------------------------------------------------------ core

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if extra:
            headers.update(extra)
        return headers

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _absorb_headers(self, headers: Any) -> None:
        """Persist rate-limit headers so the next run starts informed."""

        def _int(name: str) -> int:
            try:
                return int(headers.get(name, 0))
            except (TypeError, ValueError):
                return 0

        resource = headers.get("X-RateLimit-Resource", "core")
        state = self.rate_limit
        if resource == "search":
            state.search_remaining = _int("X-RateLimit-Remaining")
            state.search_limit = _int("X-RateLimit-Limit")
        else:
            state.core_remaining = _int("X-RateLimit-Remaining")
            state.core_limit = _int("X-RateLimit-Limit")
        reset = _int("X-RateLimit-Reset")
        if reset:
            state.reset_at = datetime.fromtimestamp(reset, tz=UTC)

    def request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        resource: str = "core",
        method: str = "GET",
    ) -> dict[str, Any]:
        """Perform one API call with retry/backoff and budget accounting."""

        # The path is either a bare API path or one of the absolute constants
        # defined in this module. Anything that merely *looks* like a URL is
        # rejected: allowing an arbitrary "http..." string here would let a
        # caller redirect the request (and the token) to a foreign host.
        if path.startswith("http"):
            if path not in _ALLOWED_ABSOLUTE_URLS:
                raise ValueError(f"refusing to call a non-API host: {path!r}")
            url = path
        else:
            url = f"{API_ROOT}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        delay = 1.0
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            self.budget.charge(resource)
            self._throttle()
            req = urllib.request.Request(url, headers=self._headers(), method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.calls += 1
                    self._last_call = time.monotonic()
                    self._absorb_headers(resp.headers)
                    payload = resp.read()
                    if not payload:
                        return {}
                    return json.loads(payload)
            except urllib.error.HTTPError as exc:
                last_error = exc
                self._absorb_headers(exc.headers)
                retry_after = _retry_after(exc.headers)
                if exc.code in (403, 429):
                    remaining = exc.headers.get("X-RateLimit-Remaining")
                    if remaining == "0" or retry_after:
                        if attempt == self.max_retries:
                            raise SecondaryRateLimit(exc.code, "secondary rate limit") from exc
                        wait = retry_after or delay
                        log.warning(
                            "secondary rate limit; sleeping %ss (attempt %d)", wait, attempt
                        )
                        time.sleep(min(wait, 60))
                        delay = min(delay * 2, 60)
                        continue
                    raise SecondaryRateLimit(exc.code, exc.reason or "forbidden") from exc
                if exc.code >= 500 and attempt < self.max_retries:
                    time.sleep(delay + random.uniform(0, delay / 2))  # noqa: S311 - jitter, not crypto
                    delay *= 2
                    continue
                raise GitHubError(exc.code, exc.reason or "error") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                time.sleep(delay + random.uniform(0, delay / 2))  # noqa: S311
                delay *= 2

        raise GitHubError(0, f"request failed after {self.max_retries} attempts: {last_error}")

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self.request(path, **kwargs)

    # --------------------------------------------------------------- helpers

    def rate_limit_snapshot(self, *, charge: bool = True) -> dict[str, Any]:
        """Read the authoritative rate-limit block (costs one core call)."""

        data = self.request("rate_limit", resource="core") if charge else {}
        resources = data.get("resources", {})
        for key, target in (("core", "core"), ("search", "search")):
            block = resources.get(key) or {}
            if block:
                setattr(self.rate_limit, f"{target}_limit", int(block.get("limit", 0)))
                setattr(self.rate_limit, f"{target}_remaining", int(block.get("remaining", 0)))
        return self.rate_limit.as_dict()

    def search_repositories(
        self,
        query: str,
        *,
        per_page: int = 100,
        max_pages: int = 3,
        sort: str = "stars",
        order: str = "desc",
    ) -> Iterator[RepoSnapshot]:
        """Yield repositories for a Search query, one page at a time.

        Each page costs one *search* request (30/hour authenticated), so
        ``max_pages`` is the real cost knob. Partial results are normal: a
        stopped run still yields everything collected so far.
        """

        page = 1
        while page <= max_pages:
            payload = self.request(
                "search/repositories",
                params={
                    "q": query,
                    "sort": sort,
                    "order": order,
                    "per_page": min(per_page, 100),
                    "page": page,
                },
                resource="search",
            )
            items = payload.get("items") or []
            total = payload.get("total_count", 0)
            log.info("search page %d: %d/%d items for %r", page, len(items), total, query)
            if not items:
                return
            for item in items:
                yield RepoSnapshot.from_search_item(item)
            if len(items) < min(per_page, 100) or page * per_page >= min(total, 1000):
                return
            page += 1


def _retry_after(headers: Any) -> int | None:
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_ts(value: str | None) -> datetime:
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime."""

    if not value:
        return utcnow()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return utcnow()


def utcnow() -> datetime:
    return datetime.now(UTC)


def window(days: int, *, now: datetime | None = None) -> str:
    """Build the ``created:>YYYY-MM-DD`` fragment used by search queries."""

    ref = now or utcnow()
    return f"created:>{(ref - timedelta(days=days)).date().isoformat()}"
