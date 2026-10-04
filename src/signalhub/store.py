"""Persistence: SQLite (WAL) + FTS5, stdlib only.

Why SQLite and not a server: the whole point of this hub is to be cheap to
operate inside an existing Hermes install. A single file, no daemon, no port,
no credentials, and it survives restarts. WAL mode lets the HTTP API read while
a cron run writes, which is the only concurrency we need.

Schema overview (see docs/data-model.md):

  repos        current known state, one row per repository (upsert target)
  observations every collect run appends one row per repo -> deltas
  events       append-only feed consumed by other flows (the "bus")
  feedback     verdicts from the user/agents, drives weight learning
  meta         key/value: weights, high-water marks, rate-limit state
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .github import RepoSnapshot, utcnow

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- One row per consumer flow, holding the last event id that flow has handled.
--
-- This replaces the single ``events.consumed_by`` column, which could not express
-- fan-out: whichever consumer read first claimed the events and every other
-- flow saw nothing. A cursor per consumer means every flow sees every event and
-- advances independently.
CREATE TABLE IF NOT EXISTS consumer_cursors (
    consumer   TEXT PRIMARY KEY,
    last_id    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_consumer_cursors_last ON consumer_cursors(last_id);

CREATE TABLE IF NOT EXISTS repos (
    full_name        TEXT PRIMARY KEY,
    owner            TEXT NOT NULL,
    name             TEXT NOT NULL,
    html_url         TEXT NOT NULL,
    description      TEXT,
    stars            INTEGER NOT NULL,
    forks            INTEGER NOT NULL DEFAULT 0,
    watchers         INTEGER NOT NULL DEFAULT 0,
    subscribers      INTEGER NOT NULL DEFAULT 0,
    open_issues      INTEGER NOT NULL DEFAULT 0,
    language         TEXT,
    topics           TEXT NOT NULL DEFAULT '',
    license          TEXT,
    size_kb          INTEGER NOT NULL DEFAULT 0,
    is_fork          INTEGER NOT NULL DEFAULT 0,
    is_archived      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    pushed_at        TEXT NOT NULL,
    score            REAL,
    status           TEXT NOT NULL DEFAULT 'new',
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    seen_count       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_repos_score    ON repos(score DESC);
CREATE INDEX IF NOT EXISTS idx_repos_created  ON repos(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_repos_status   ON repos(status);
CREATE INDEX IF NOT EXISTS idx_repos_stars    ON repos(stars DESC);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name   TEXT NOT NULL REFERENCES repos(full_name) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    stars       INTEGER NOT NULL,
    forks       INTEGER NOT NULL DEFAULT 0,
    watchers    INTEGER NOT NULL DEFAULT 0,
    score       REAL
);
CREATE INDEX IF NOT EXISTS idx_obs_repo ON observations(full_name, observed_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    queries       TEXT NOT NULL DEFAULT '[]',
    candidates    INTEGER NOT NULL DEFAULT 0,
    new_repos     INTEGER NOT NULL DEFAULT 0,
    api_calls     INTEGER NOT NULL DEFAULT 0,
    rate_limit    TEXT NOT NULL DEFAULT '{}',
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    run_id      INTEGER,
    full_name   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    score       REAL,
    payload     TEXT NOT NULL DEFAULT '{}',
    consumed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_open ON events(kind, id);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    full_name   TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    component   TEXT,
    actor       TEXT NOT NULL DEFAULT 'user',
    score_at_time REAL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_repo ON feedback(full_name, created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS repos_fts USING fts5(
    full_name UNINDEXED,
    body,
    tokenize='unicode61'
);
"""


@dataclass
class RepoRecord:
    """A repository row joined with the latest observation delta."""

    full_name: str
    html_url: str
    description: str | None
    stars: int
    forks: int
    watchers: int
    language: str | None
    topics: tuple[str, ...]
    license: str | None
    created_at: datetime
    pushed_at: datetime
    score: float | None
    status: str
    first_seen: datetime
    last_seen: datetime
    seen_count: int = 1
    star_delta: int | None = None
    observations: int = 0
    feedback_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "html_url": self.html_url,
            "description": self.description,
            "stars": self.stars,
            "forks": self.forks,
            "watchers": self.watchers,
            "language": self.language,
            "topics": list(self.topics),
            "license": self.license,
            "created_at": self.created_at.isoformat(),
            "pushed_at": self.pushed_at.isoformat(),
            "score": self.score,
            "status": self.status,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "seen_count": self.seen_count,
            "star_delta": self.star_delta,
            "observations": self.observations,
            "feedback_count": self.feedback_count,
        }


class _LockedConnection:
    """Thread-safe proxy in front of a :class:`sqlite3.Connection`.

    Every statement is serialised through the store's re-entrant lock. This is
    deliberately a proxy rather than a property returning the raw connection:
    a property would release the lock before the caller ran its statement.

    ``__getattr__`` forwards everything else (``row_factory``, cursors, ...),
    and the explicit methods below take precedence over it.
    """

    __slots__ = ("_conn", "_lock")

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class Store:
    """All persistence in one place. Cheap to construct, easy to fake in tests.

    Thread safety: the HTTP API serves every request on its own thread while a
    cron run may be writing, so the connection is opened with
    ``check_same_thread=False`` and guarded by a re-entrant lock. SQLite in WAL
    mode already serialises at the file level; the lock is what stops two
    threads from interleaving *transactions* on the same connection object,
    which is the one thing WAL does not protect against.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        raw = sqlite3.connect(
            self.path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        raw.row_factory = sqlite3.Row
        self.conn = _LockedConnection(raw, self._lock)
        self.conn.executescript(SCHEMA)
        self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[_LockedConnection]:
        """Explicit transaction; the connection runs in autocommit mode."""

        # The RLock is re-entrant, so the proxy statements inside the block stay
        # covered by the same lock this context manager holds.
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # ------------------------------------------------------------------ runs

    def start_run(self, queries: list[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, queries) VALUES(?, ?)",
            (utcnow().isoformat(), json.dumps(queries)),
        )
        return int(cur.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str = "ok",
        candidates: int = 0,
        new_repos: int = 0,
        api_calls: int = 0,
        rate_limit: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, candidates = ?, new_repos = ?, "
            "api_calls = ?, rate_limit = ?, notes = ? WHERE id = ?",
            (
                utcnow().isoformat(),
                status,
                candidates,
                new_repos,
                api_calls,
                json.dumps(rate_limit or {}),
                notes,
                run_id,
            ),
        )

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_successful_run(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # ----------------------------------------------------------------- repos

    def previous_observations(self, *, within_hours: int = 168) -> dict[str, tuple[int, datetime]]:
        """Latest observation per repo inside the lookback window.

        This is what makes momentum real: a repo's *first* sighting has no
        delta, the second one does, and the ranking can reward acceleration.
        """

        cutoff = (utcnow() - timedelta(hours=within_hours)).isoformat()
        rows = self.conn.execute(
            "SELECT full_name, stars, observed_at FROM observations o "
            "WHERE observed_at >= ? AND id = ("
            "  SELECT id FROM observations x WHERE x.full_name = o.full_name"
            "  ORDER BY x.observed_at DESC, x.id DESC LIMIT 1)",
            (cutoff,),
        ).fetchall()
        return {
            r["full_name"]: (int(r["stars"]), datetime.fromisoformat(r["observed_at"]))
            for r in rows
        }

    def upsert_repos(
        self,
        items: Iterable[tuple[RepoSnapshot, float | None]],
        *,
        run_id: int | None = None,
    ) -> tuple[int, list[str]]:
        """Insert or refresh repositories, appending an observation per row.

        Returns ``(total_seen, new_full_names)``.
        """

        now = utcnow().isoformat()
        new_names: list[str] = []
        total = 0
        with self.transaction() as conn:
            for repo, score in items:
                total += 1
                existing = conn.execute(
                    "SELECT full_name, first_seen, seen_count FROM repos WHERE full_name = ?",
                    (repo.full_name,),
                ).fetchone()
                topics = " ".join(repo.topics)
                if existing:
                    conn.execute(
                        "UPDATE repos SET stars = ?, forks = ?, watchers = ?, subscribers = ?, "
                        "open_issues = ?, language = ?, topics = ?, license = ?, size_kb = ?, "
                        "is_fork = ?, is_archived = ?, pushed_at = ?, description = ?, "
                        "score = ?, last_seen = ?, seen_count = seen_count + 1 WHERE full_name = ?",
                        (
                            repo.stars,
                            repo.forks,
                            repo.watchers,
                            repo.subscribers,
                            repo.open_issues,
                            repo.language,
                            topics,
                            repo.license,
                            repo.size_kb,
                            int(repo.is_fork),
                            int(repo.is_archived),
                            repo.pushed_at.isoformat(),
                            repo.description,
                            score,
                            now,
                            repo.full_name,
                        ),
                    )
                else:
                    new_names.append(repo.full_name)
                    conn.execute(
                        "INSERT INTO repos(full_name, owner, name, html_url, description, stars, "
                        "forks, watchers, subscribers, open_issues, language, topics, license, "
                        "size_kb, is_fork, is_archived, created_at, pushed_at, score, status, "
                        "first_seen, last_seen, seen_count) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            repo.full_name,
                            repo.owner,
                            repo.name,
                            repo.html_url,
                            repo.description,
                            repo.stars,
                            repo.forks,
                            repo.watchers,
                            repo.subscribers,
                            repo.open_issues,
                            repo.language,
                            topics,
                            repo.license,
                            repo.size_kb,
                            int(repo.is_fork),
                            int(repo.is_archived),
                            repo.created_at.isoformat(),
                            repo.pushed_at.isoformat(),
                            score,
                            "new",
                            now,
                            now,
                            1,
                        ),
                    )
                conn.execute(
                    "INSERT INTO observations(full_name, observed_at, stars, forks, watchers, score) "
                    "VALUES(?,?,?,?,?,?)",
                    (repo.full_name, now, repo.stars, repo.forks, repo.watchers, score),
                )
                if existing is None:
                    self._index_fts(conn, repo)
        return total, new_names

    def _index_fts(self, conn: _LockedConnection, repo: RepoSnapshot) -> None:
        body = " ".join(
            filter(
                None,
                [
                    repo.full_name,
                    repo.description,
                    repo.language,
                    " ".join(repo.topics),
                ],
            )
        )
        conn.execute(
            "INSERT INTO repos_fts(full_name, body) VALUES(?, ?)",
            (repo.full_name, body),
        )

    def reindex_fts(self) -> int:
        """Rebuild the FTS index from `repos` (repair/consistency helper)."""

        count = 0
        with self.transaction() as conn:
            conn.execute("DELETE FROM repos_fts")
            rows = conn.execute(
                "SELECT full_name, description, language, topics FROM repos"
            ).fetchall()
            for r in rows:
                body = " ".join(
                    filter(None, [r["full_name"], r["description"], r["language"], r["topics"]])
                )
                conn.execute(
                    "INSERT INTO repos_fts(full_name, body) VALUES(?,?)", (r["full_name"], body)
                )
                count += 1
        return count

    def set_scores(self, scores: dict[str, float]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE repos SET score = ? WHERE full_name = ?",
                [(v, k) for k, v in scores.items()],
            )

    def get_repo(self, full_name: str) -> RepoRecord | None:
        row = self.conn.execute("SELECT * FROM repos WHERE full_name = ?", (full_name,)).fetchone()
        if not row:
            return None
        return self._to_record(row)

    def _to_record(self, row: sqlite3.Row) -> RepoRecord:
        obs = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(stars) AS first_stars FROM observations WHERE full_name = ?",
            (row["full_name"],),
        ).fetchone()
        fb = self.conn.execute(
            "SELECT COUNT(*) AS n FROM feedback WHERE full_name = ?", (row["full_name"],)
        ).fetchone()
        latest = self.conn.execute(
            "SELECT stars FROM observations WHERE full_name = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
            (row["full_name"],),
        ).fetchone()
        delta = (
            int(latest["stars"] - obs["first_stars"])
            if latest and obs["first_stars"] is not None
            else None
        )
        return RepoRecord(
            full_name=row["full_name"],
            html_url=row["html_url"],
            description=row["description"],
            stars=int(row["stars"]),
            forks=int(row["forks"]),
            watchers=int(row["watchers"]),
            language=row["language"],
            topics=tuple((row["topics"] or "").split()),
            license=row["license"],
            created_at=datetime.fromisoformat(row["created_at"]),
            pushed_at=datetime.fromisoformat(row["pushed_at"]),
            score=row["score"],
            status=row["status"],
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
            seen_count=int(row["seen_count"]),
            star_delta=delta,
            observations=int(obs["n"]),
            feedback_count=int(fb["n"]),
        )

    def list_repos(
        self,
        *,
        status: str | None = None,
        min_stars: int = 0,
        since_days: int | None = None,
        limit: int = 20,
        offset: int = 0,
        order_by: str = "score",
    ) -> list[RepoRecord]:
        allowed = {"score", "stars", "created_at", "pushed_at", "first_seen"}
        if order_by not in allowed:
            order_by = "score"
        clauses = ["stars >= ?"]
        params: list[Any] = [min_stars]
        # ``any`` (or no status at all) means "do not filter". Treating it as a
        # literal would match nothing and silently return an empty list.
        if status and status.lower() not in ("any", "all", "*"):
            clauses.append("status = ?")
            params.append(status)
        if since_days is not None:
            clauses.append("created_at >= ?")
            params.append((utcnow() - timedelta(days=since_days)).isoformat())
        where = " AND ".join(clauses)
        # S608: safe by construction -- ``order_by`` is checked against the
        # allow-list above and ``where`` is built only from literal fragments
        # with their values bound as parameters.
        # S608: safe by construction -- ``order_by`` is checked against the
        # allow-list above and ``where`` is built only from literal fragments
        # with every value bound as a parameter.
        sql = (
            f"SELECT * FROM repos WHERE {where} ORDER BY {order_by} DESC NULLS LAST, stars DESC "  # noqa: S608
            "LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        rows = self.conn.execute(sql, params).fetchall()
        return [self._to_record(r) for r in rows]

    def search(self, query: str, *, limit: int = 20) -> list[RepoRecord]:
        """Full-text search over name, description, language and topics."""

        try:
            rows = self.conn.execute(
                "SELECT r.* FROM repos_fts f JOIN repos r ON r.full_name = f.full_name "
                "WHERE repos_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Fall back to LIKE when the user types a non-FTS expression.
            like = f"%{query}%"
            rows = self.conn.execute(
                "SELECT * FROM repos WHERE full_name LIKE ? OR description LIKE ? OR topics LIKE ? "
                "ORDER BY stars DESC LIMIT ?",
                (like, like, like, limit),
            ).fetchall()
        return [self._to_record(r) for r in rows]

    def set_status(self, full_names: list[str], status: str) -> int:
        if not full_names:
            return 0
        with self.transaction() as conn:
            cur = conn.executemany(
                "UPDATE repos SET status = ? WHERE full_name = ?",
                [(status, n) for n in full_names],
            )
            return cur.rowcount

    def prune_observations(self, *, keep_days: int = 60) -> int:
        cutoff = (utcnow() - timedelta(days=keep_days)).isoformat()
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))
            return cur.rowcount

    # ---------------------------------------------------------------- events

    def publish(
        self,
        items: Iterable[tuple[str, str, float | None, dict[str, Any]]],
        *,
        kind: str = "repo.discovered",
        run_id: int | None = None,
    ) -> int:
        """Append events to the bus. ``items`` is (full_name, html_url, score, payload)."""

        now = utcnow().isoformat()
        count = 0
        with self.transaction() as conn:
            for full_name, html_url, score, payload in items:
                body = {"html_url": html_url, **payload}
                conn.execute(
                    "INSERT INTO events(created_at, run_id, full_name, kind, score, payload) "
                    "VALUES(?,?,?,?,?,?)",
                    (now, run_id, full_name, kind, score, json.dumps(body)),
                )
                count += 1
        return count

    def read_events(
        self,
        *,
        kind: str | None = None,
        after_id: int = 0,
        limit: int = 50,
        unconsumed_only: bool = False,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["id > ?"]
        params: list[Any] = [after_id]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if run_id is not None:
            # Pin to one discovery run. This is how the daily digest stays
            # idempotent: it shows what THIS run found, not the whole 'new'
            # backlog, which may hold hundreds of repos from earlier days.
            clauses.append("run_id = ?")
            params.append(run_id)
        if unconsumed_only:
            clauses.append("consumed_by IS NULL")
        params.append(limit)
        rows = self.conn.execute(
            # S608: safe by construction -- every clause is a literal and the
            # only interpolated values (after_id, kind, limit) stay bound.
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ?",  # noqa: S608
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def mark_consumed(self, event_ids: list[int], consumer: str) -> int:
        """Legacy shared-column ack. Prefer :meth:`advance_cursor` for fan-out.

        Kept because it is still the right tool for a single-consumer setup and
        for marking an event as handled by *someone*, but it cannot serve several
        independent flows: the second one to read gets nothing.
        """

        if not event_ids:
            return 0
        with self.transaction() as conn:
            cur = conn.executemany(
                "UPDATE events SET consumed_by = ? WHERE id = ?",
                [(consumer, i) for i in event_ids],
            )
            return cur.rowcount

    # ------------------------------------------------------ consumer cursors

    def cursor_position(self, consumer: str) -> int:
        """Last event id this consumer has handled. Unknown consumer is 0."""

        row = self.conn.execute(
            "SELECT last_id FROM consumer_cursors WHERE consumer = ?",
            (consumer,),
        ).fetchone()
        return int(row["last_id"]) if row else 0

    def read_events_for(
        self,
        consumer: str,
        *,
        kind: str | None = None,
        run_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Events this consumer has not handled yet, oldest first.

        Pure read: it never moves the cursor, so a consumer that crashes
        mid-batch replays the same events on its next run instead of losing
        them. Call :meth:`advance_cursor` only after the work is done.
        """

        if limit <= 0:
            return []
        params: list[Any] = [self.cursor_position(consumer)]
        sql = "SELECT * FROM events WHERE id > ?"
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def advance_cursor(self, consumer: str, event_ids: list[int]) -> int:
        """Mark these events handled by ``consumer`` and move the cursor forward.

        Monotonic by design: a late ack from a straggler batch must not rewind
        the cursor and cause events to be replayed.

        Returns the number of events the cursor has now covered, not the number
        of rows written, so callers can log what they actually finished.
        """

        if not event_ids:
            return 0
        target = max(event_ids)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO consumer_cursors (consumer, last_id, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(consumer) DO UPDATE SET
                    last_id = MAX(consumer_cursors.last_id, excluded.last_id),
                    updated_at = datetime('now')
                """,
                (consumer, target),
            )
        return len(set(event_ids))

    def rewind_cursor(self, consumer: str, event_id: int = 0) -> None:
        """Reset a consumer to an earlier point, for replay after a failure."""

        with self.transaction() as conn:
            if event_id <= 0:
                conn.execute("DELETE FROM consumer_cursors WHERE consumer = ?", (consumer,))
            else:
                conn.execute(
                    """
                    INSERT INTO consumer_cursors (consumer, last_id, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(consumer) DO UPDATE SET
                        last_id = excluded.last_id, updated_at = datetime('now')
                    """,
                    (consumer, event_id),
                )

    def list_consumers(self) -> list[dict[str, Any]]:
        """Consumers that have actually moved a cursor, with their lag."""

        head = self.max_event_id()
        rows = self.conn.execute(
            """
            SELECT c.consumer, c.last_id, c.updated_at,
                   ? - c.last_id AS pending
            FROM consumer_cursors c
            ORDER BY c.last_id ASC
            """,
            (head,),
        ).fetchall()
        return [dict(r) for r in rows if int(r["last_id"]) > 0]

    def max_event_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
        return int(row["m"])

    def mark_delivered(self, full_names: list[str], *, prefix: str = "seen:") -> int:
        """Flag repos as already shown, so a digest never repeats itself."""

        return self.set_status(full_names, prefix)

    # -------------------------------------------------------------- feedback

    def add_feedback(
        self,
        full_name: str,
        verdict: str,
        *,
        component: str | None = None,
        actor: str = "user",
        score: float | None = None,
        note: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO feedback(created_at, full_name, verdict, component, actor, score_at_time, note) "
            "VALUES(?,?,?,?,?,?,?)",
            (utcnow().isoformat(), full_name, verdict, component, actor, score, note),
        )
        return int(cur.lastrowid or 0)

    def list_feedback(
        self, *, limit: int = 50, full_name: str | None = None
    ) -> list[dict[str, Any]]:
        if full_name:
            rows = self.conn.execute(
                "SELECT * FROM feedback WHERE full_name = ? ORDER BY id DESC LIMIT ?",
                (full_name, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def feedback_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM feedback GROUP BY verdict"
        ).fetchall()
        return {r["verdict"]: int(r["n"]) for r in rows}

    def stats(self) -> dict[str, Any]:
        """One-call health summary used by `/healthz` and the CLI."""

        repo_row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS stars, "
            "COALESCE(MAX(stars),0) AS max_stars FROM repos"
        ).fetchone()
        new_row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM repos WHERE status = 'new'"
        ).fetchone()
        ev_row = self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        unconsumed = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE consumed_by IS NULL"
        ).fetchone()
        obs = self.conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()
        last = self.last_successful_run()
        return {
            "repos": int(repo_row["n"]),
            "new_repos": int(new_row["n"]),
            "total_stars": int(repo_row["stars"]),
            "max_stars": int(repo_row["max_stars"]),
            "observations": int(obs["n"]),
            "events": int(ev_row["n"]),
            "unconsumed_events": int(unconsumed["n"]),
            "last_run": {
                "id": last["id"],
                "finished_at": last["finished_at"],
                "candidates": last["candidates"],
                "new_repos": last["new_repos"],
                "api_calls": last["api_calls"],
            }
            if last
            else None,
        }
