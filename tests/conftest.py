"""Shared fixtures and factory helpers.

The tests never touch the network unless marked ``live``: a discovery tool
whose test suite depends on GitHub's mood is a tool nobody trusts.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from signalhub.github import RepoSnapshot
from signalhub.store import Store

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)


def make_repo(
    full_name: str = "acme/widget",
    *,
    stars: int = 1000,
    forks: int = 120,
    watchers: int = 90,
    age_days: float = 5.0,
    topics: tuple[str, ...] = ("ai", "mcp"),
    language: str | None = "Python",
    license: str | None = "MIT",
    description: str | None = "A useful tool that does something real and well documented",
    is_fork: bool = False,
    is_archived: bool = False,
    pushed_days_ago: float = 1.0,
    owner_age_days: float = 2000.0,
    open_issues: int = 3,
    size_kb: int = 5000,
    subscribers: int = 80,
    name: str | None = None,
) -> RepoSnapshot:
    """Build a RepoSnapshot with sane, explicit defaults."""

    created = NOW - timedelta(days=age_days)
    owner_login, _, short = full_name.partition("/")
    return RepoSnapshot(
        full_name=full_name,
        owner=owner_login,
        name=name or short,
        html_url=f"https://github.com/{full_name}",
        description=description,
        stars=stars,
        forks=forks,
        watchers=watchers,
        open_issues=open_issues,
        created_at=created,
        pushed_at=NOW - timedelta(days=pushed_days_ago),
        updated_at=NOW,
        language=language,
        topics=topics,
        license=license,
        is_fork=is_fork,
        is_archived=is_archived,
        has_issues=True,
        has_wiki=False,
        size_kb=size_kb,
        owner_created_at=NOW - timedelta(days=owner_age_days),
        owner_followers=500,
        subscribers=subscribers,
        default_branch="main",
        raw={"full_name": full_name},
    )


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    """A store backed by a real file.

    It has to be a file, not ``:memory:``: the CLI tests spawn ``main()``,
    which opens its *own* connection to the same path. An in-memory database
    would hand the CLI an empty store and the test would be measuring nothing.
    """

    s = Store(tmp_path / "signalhub.db")
    yield s
    s.close()


@pytest.fixture
def repo() -> RepoSnapshot:
    return make_repo()


@pytest.fixture
def scorer():
    from signalhub.scoring import Scorer

    return Scorer(now=NOW)
