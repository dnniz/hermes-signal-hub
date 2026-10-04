"""Local HTTP API + SSE stream: how other agents and flows plug in.

Why an HTTP surface at all, when a CLI and a SQLite file already exist?

* An agent running in a different Hermes profile (or a different container)
  cannot open a database owned by another process safely while it writes.
* SSE gives *push* semantics: a long-lived research agent can be told the
  moment a repo crosses a threshold, without polling.
* The contract is ordinary JSON, so a flow written in any language (or by
  `curl`) can consume it without learning a Python API.

The server is intentionally stdlib ``http.server``: no framework to install,
no new port to supervise beyond one, and it binds to 127.0.0.1 by default.
Everything it exposes is read-only except the two feedback endpoints.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .hub import SignalHub
from .render import render_agent_briefing, render_jsonl, render_markdown

log = logging.getLogger("signalhub.server")

API_VERSION = "1"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    return str(obj)


class SignalHubHandler(BaseHTTPRequestHandler):
    """Request handler; all business logic lives in :class:`SignalHub`."""

    server_version = f"signalhub/{API_VERSION}"
    hub: SignalHub  # injected by make_server

    # ------------------------------------------------------------- plumbing

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), format % args)

    def _send(self, status: int, payload: Any, *, content_type: str = "application/json") -> None:
        body = (
            payload
            if isinstance(payload, (bytes, str))
            else json.dumps(payload, default=_json_default, ensure_ascii=False)
        )
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": {"status": status, "message": message}})

    def _query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        q = self._query()
        try:
            if path in ("/", "/health", "/healthz"):
                self._send(HTTPStatus.OK, self.hub.health())
            elif path == "/stats":
                self._send(HTTPStatus.OK, self.hub.store.stats())
            elif path == "/weights":
                self._send(HTTPStatus.OK, self.hub.load_weights().as_dict())
            elif path == "/runs":
                self._send(
                    HTTPStatus.OK,
                    {"runs": self.hub.store.recent_runs(limit=int(q.get("limit", 10)))},
                )
            elif path == "/repos":
                self._send(HTTPStatus.OK, self._repos(q))
            elif path == "/events":
                self._send(
                    HTTPStatus.OK,
                    {
                        "events": self.hub.store.read_events(
                            kind=q.get("kind"),
                            after_id=int(q.get("after", 0)),
                            limit=min(int(q.get("limit", 50)), 500),
                            unconsumed_only=q.get("unconsumed") == "1",
                        )
                    },
                )
            elif path == "/feedback":
                self._send(
                    HTTPStatus.OK,
                    {
                        "feedback": self.hub.store.list_feedback(
                            limit=min(int(q.get("limit", 50)), 500),
                            full_name=q.get("repo"),
                        )
                    },
                )
            elif path == "/digest":
                self._send(HTTPStatus.OK, self._digest(q), content_type="text/plain")
            elif path == "/briefing":
                self._send(HTTPStatus.OK, self._briefing(q), content_type="text/plain")
            elif path == "/jsonl":
                self._send(HTTPStatus.OK, self._jsonl(q), content_type="application/x-ndjson")
            elif path == "/search":
                self._send(
                    HTTPStatus.OK,
                    {
                        "results": [
                            r.as_dict()
                            for r in self.hub.search(q.get("q", ""), limit=int(q.get("limit", 10)))
                        ]
                    },
                )
            elif path == "/stream":
                self._stream(q)
            else:
                self._error(HTTPStatus.NOT_FOUND, f"unknown endpoint: {path}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except BrokenPipeError:
            log.debug("client disconnected during %s", path)
        except Exception as exc:
            log.exception("unhandled error on %s", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _repos(self, q: dict[str, str]) -> dict[str, Any]:
        status = q.get("status", "new")
        if status in ("", "any", "all"):
            status = None
        records = self.hub.store.list_repos(
            status=status,
            min_stars=int(q.get("min_stars", 0)),
            since_days=int(q["since_days"]) if q.get("since_days") else None,
            limit=min(int(q.get("limit", 20)), 200),
            offset=int(q.get("offset", 0)),
            order_by=q.get("order", "score"),
        )
        return {"repos": [r.as_dict() for r in records], "count": len(records)}

    def _top_items(self, q: dict[str, str]):
        status = q.get("status", "new")
        if status in ("", "any", "all"):
            status = None
        return self.hub.top(
            n=min(int(q.get("limit", 8)), 100),
            status=status,
            min_stars=int(q.get("min_stars", 0)),
        )

    def _digest(self, q: dict[str, str]) -> str:
        digest = render_markdown(
            self._top_items(q),
            title=q.get("title", "Nuevos repos en GitHub"),
            limit=min(int(q.get("limit", 8)), 50),
            verdict_hint=q.get("hint"),
        )
        return digest.body

    def _briefing(self, q: dict[str, str]) -> str:
        return render_agent_briefing(self._top_items(q), limit=min(int(q.get("limit", 10)), 50))

    def _jsonl(self, q: dict[str, str]) -> str:
        return render_jsonl(self._top_items(q))

    def _stream(self, q: dict[str, str]) -> None:
        """Server-sent events: new discoveries as they happen.

        The loop polls the event table (cheap, indexed) rather than holding a
        connection to GitHub, so a slow client can never hold an API budget
        hostage. ``?once=1`` returns the current backlog and closes, which is
        the mode a cron-driven flow should use.
        """

        once = q.get("once") == "1"
        interval = max(1.0, float(q.get("interval", 15)))
        timeout = float(q.get("timeout", 3600))
        kind = q.get("kind")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # ``once`` responses are finite: they must not advertise keep-alive or
        # the client waits for a second event that will never arrive. Only the
        # continuous mode is allowed to hold the socket open.
        self.send_header("Connection", "close" if once else "keep-alive")
        self.end_headers()

        cursor = int(q.get("after", 0))
        deadline = time.monotonic() + timeout
        try:
            while True:
                events = self.hub.store.read_events(
                    kind=kind,
                    after_id=cursor,
                    limit=100,
                    unconsumed_only=q.get("unconsumed") == "1",
                )
                for ev in events:
                    cursor = max(cursor, int(ev["id"]))
                    chunk = f"id: {ev['id']}\nevent: {ev['kind']}\ndata: {json.dumps(ev, default=_json_default)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                if once or time.monotonic() > deadline:
                    if once:
                        # Finite response: half-close so the client sees EOF
                        # and stops waiting. Without this, urlopen() blocks
                        # until its timeout even though we wrote everything.
                        self.close_connection = True
                    return
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("SSE client disconnected at cursor=%s", cursor)

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_json()
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        try:
            if path == "/collect":
                report = self.hub.collect(publish=bool(body.get("publish", True)))
                self._send(HTTPStatus.OK, report.summary())
            elif path == "/feedback":
                full_name = body.get("full_name") or body.get("repo")
                verdict = body.get("verdict")
                if not full_name or not verdict:
                    raise ValueError("full_name and verdict are required")
                result = self.hub.record_verdict(
                    full_name,
                    verdict,
                    actor=body.get("actor", "agent"),
                    note=body.get("note"),
                )
                self._send(HTTPStatus.OK, result)
            elif path == "/consume":
                ids = [int(i) for i in (body.get("event_ids") or [])]
                consumer = body.get("consumer", "unknown")
                self._send(HTTPStatus.OK, {"consumed": self.hub.store.mark_consumed(ids, consumer)})
            elif path == "/ack":
                names = body.get("repos") or []
                prefix = body.get("prefix", "seen:")
                self._send(
                    HTTPStatus.OK,
                    {"updated": self.hub.store.mark_delivered(names, prefix=prefix)},
                )
            else:
                self._error(HTTPStatus.NOT_FOUND, f"unknown endpoint: {path}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            log.exception("unhandled error on POST %s", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))


def make_server(
    hub: SignalHub,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> ThreadingHTTPServer:
    """Build (but do not start) the HTTP server."""

    handler = type("BoundSignalHubHandler", (SignalHubHandler,), {"hub": hub})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve_in_background(
    hub: SignalHub,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start the API in a daemon thread (used by `signalhub serve --with-api`)."""

    server = make_server(hub, host=host, port=port)
    thread = threading.Thread(target=server.serve_forever, name="signalhub-api", daemon=True)
    thread.start()
    log.info("signalhub API listening on http://%s:%d", host, port)
    return server, thread


def default_port() -> int:
    """Port for the local API.

    8787 is outside the Hermes gateway range (8642-8644) so the API can never
    collide with an already-running gateway on this host.
    """

    return 8787


__all__ = [
    "API_VERSION",
    "SignalHubHandler",
    "default_port",
    "make_server",
    "serve_in_background",
]
