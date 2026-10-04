"""``signalhub`` command line: the surface every other flow calls.

Design rule: every subcommand is non-interactive, writes machine-readable
output by default, and has a human-readable mode behind an explicit flag. That
is what makes it safe to call from cron, from a shell script, and from an
agent that wants to read the result rather than be shown it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from .collector import CollectorConfig
from .hub import DEFAULT_DB, SignalHub, resolve_token
from .render import render_agent_briefing, render_jsonl, render_markdown, render_stats
from .scoring import ScoreWeights
from .server import default_port

log = logging.getLogger("signalhub.cli")


def _setup_logging(verbosity: int) -> None:
    level = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}.get(verbosity, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _hub(args: argparse.Namespace) -> SignalHub:
    return SignalHub(
        db_path=args.db,
        collector_config=CollectorConfig(
            days=args.days,
            min_stars=args.min_stars,
            max_search_calls=args.max_search_calls,
            target_candidates=args.target,
        ),
    )


def _out(data: Any, args: argparse.Namespace) -> None:
    """Emit machine-readable JSON unless the caller asked for markdown."""

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str))


# --------------------------------------------------------------------- verbs


def cmd_collect(args: argparse.Namespace) -> int:
    hub = _hub(args)
    # The run report carries its own duration_s measured inside the hub, so
    # there is nothing to time out here.
    report = hub.collect(publish=not args.dry_run, dry_run=args.dry_run)
    if args.json:
        _out(report.summary(), args)
        return 0
    if args.briefing or args.digest:
        items = report.scored
        if args.digest:
            digest = render_markdown(
                items,
                title=args.title,
                limit=args.limit,
                header=f"corrida #{report.run_id} · {report.collection.api_calls} llamadas API",
            )
            print(digest.body)
        else:
            print(render_agent_briefing(items, limit=args.limit))
        return 0
    s = report.summary()
    print(
        f"run #{s['run_id']} · {s['candidates']} candidatos · {s['ranked']} rankeados · "
        f"{s['new_repos']} nuevos · {s['api_calls']} llamadas ({s['search_calls']} search) · "
        f"{s['duration_s']}s"
    )
    if s["errors"]:
        print(f"errores: {len(s['errors'])}", file=sys.stderr)
        for e in s["errors"][:3]:
            print(f"  - {e}", file=sys.stderr)
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    hub = _hub(args)
    items = hub.top(n=args.limit, status=args.status, min_stars=args.min_stars)
    # ``--json`` is a global flag and must win over ``--format``. Without this
    # check ``--json rank`` silently emitted markdown, so any caller piping stdout
    # into a JSON parser (the daily-digest script, another agent) got a
    # hard-to-trace failure instead of data.
    if args.json:
        _out([s.as_dict() for s in items], args)
        return 0
    if args.format == "jsonl":
        print(render_jsonl(items))
    elif args.format == "briefing":
        print(render_agent_briefing(items, limit=args.limit))
    elif args.format == "table":
        for i, s in enumerate(items, start=1):
            r = s.repo
            print(f"{i:2}. {r.full_name:50} {r.stars:>7}⭐ {s.total:.3f} {r.stars_per_day:>7.1f}/d")
    else:
        digest = render_markdown(items, title=args.title, limit=args.limit)
        print(digest.body)
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    hub = _hub(args)
    items = hub.top(n=args.limit, status=args.status, min_stars=args.min_stars)
    if args.json:
        digest = render_markdown(items, title=args.title, limit=args.limit)
        _out(digest.as_dict(), args)
        return 0
    print(
        render_markdown(
            items,
            title=args.title,
            limit=args.limit,
            verdict_hint=(
                "Responde `signalhub decide <owner/repo> yes|no|noise` para entrenar el filtro."
                if not args.no_hint
                else None
            ),
        ).body
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    hub = _hub(args)
    results = hub.search(args.query, limit=args.limit)
    if args.json:
        _out([r.as_dict() for r in results], args)
        return 0
    for i, r in enumerate(results, start=1):
        print(f"{i:2}. {r.full_name:50} {r.stars:>7}⭐  {r.description or ''}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    hub = _hub(args)
    run_id = args.run
    if run_id is None and args.latest_run:
        last = hub.store.last_successful_run()
        run_id = int(last["id"]) if last else 0
    events = hub.store.read_events(
        kind=args.kind,
        after_id=args.after,
        limit=args.limit,
        unconsumed_only=args.unconsumed,
        run_id=run_id,
    )
    if args.consume:
        ids = [int(e["id"]) for e in events]
        hub.store.mark_consumed(ids, args.consume)
    if args.json:
        _out(events, args)
        return 0
    for e in events:
        p = e["payload"]
        print(
            f"#{e['id']:>5} {e['kind']:20} {e['full_name']:45} "
            f"score={e['score']:.3f} ⭐{p.get('stars', 0)}"
        )
    if not events:
        print("(sin eventos nuevos)")
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    hub = _hub(args)
    n = hub.store.mark_delivered(args.repos, prefix=args.prefix)
    print(f"{n} repos marcados como vistos ({args.prefix})")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    hub = _hub(args)
    try:
        result = hub.record_verdict(args.repo, args.verdict, actor=args.actor, note=args.note)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _out(result, args)
        return 0
    w = result["weights"]
    print(f"✓ {result['full_name']} → {result['verdict']} (componente: {result['component']})")
    print(
        f"  pesos: vel={w['velocity']} eng={w['engagement']} rel={w['relevance']} "
        f"dev={w['developer']} pen={w['penalty_scale']}"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    hub = _hub(args)
    health = hub.health()
    if args.json:
        _out(health, args)
        return 0
    print(render_stats(hub.store.stats(), health=health))
    return 0


def cmd_weights(args: argparse.Namespace) -> int:
    hub = _hub(args)
    if args.reset:
        hub.save_weights(ScoreWeights())
        print("pesos restaurados a los valores por defecto")
        return 0
    w = hub.load_weights()
    if args.json:
        _out(w.as_dict(), args)
        return 0
    print(
        f"vel={w.velocity} eng={w.engagement} rel={w.relevance} dev={w.developer} pen={w.penalty_scale}"
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    hub = _hub(args)
    from .server import make_server

    server = make_server(hub, host=args.host, port=args.port)
    print(f"signalhub API en http://{args.host}:{args.port}  (Ctrl-C para salir)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nparando…", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    hub = _hub(args)
    removed = hub.store.prune_observations(keep_days=args.keep_days)
    print(f"{removed} observaciones eliminadas (>{args.keep_days}d)")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    hub = _hub(args)
    print(f"{hub.store.reindex_fts()} repos reindexados")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Preflight: verify token, quota and database without writing anything."""

    hub = _hub(args)
    report: dict[str, Any] = {"db": str(hub.store.path)}
    try:
        token = resolve_token()
        report["token"] = {"found": True, "prefix_ok": token.startswith(("ghp_", "github_pat_"))}
    except RuntimeError as exc:
        report["token"] = {"found": False, "error": str(exc)}
        print(json.dumps(report, indent=2))
        return 1
    try:
        report["rate_limit"] = hub.rate_limit_snapshot()
    except Exception as exc:  # noqa: BLE001
        report["rate_limit"] = {"error": str(exc)}
    report["stats"] = hub.store.stats()
    report["ok"] = "error" not in report.get("rate_limit", {})
    _out(report, args)
    return 0 if report["ok"] else 1


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="signalhub",
        description="Descubre repos nuevos de GitHub por velocidad de estrellas y los enruta a tus flujos.",
    )
    p.add_argument("--db", default=str(DEFAULT_DB), help="ruta de la base SQLite")
    p.add_argument("--json", action="store_true", help="salida JSON")
    p.add_argument("-v", "--verbose", action="count", default=0, help="verbose (-vv para debug)")
    p.add_argument("--days", type=int, default=14, help="ventana de creación en días")
    p.add_argument("--min-stars", type=int, default=150, help="piso de estrellas")
    p.add_argument(
        "--max-search-calls", type=int, default=24, help="presupuesto de llamadas search"
    )
    p.add_argument("--target", type=int, default=400, help="candidatos objetivo")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="descubrir, rankear y guardar")
    c.add_argument("--dry-run", action="store_true", help="no escribir nada")
    c.add_argument("--digest", action="store_true", help="imprimir digest markdown")
    c.add_argument("--briefing", action="store_true", help="imprimir briefing para agentes")
    c.add_argument("--limit", type=int, default=8)
    c.add_argument("--title", default="Nuevos repos en GitHub")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("rank", help="ver el ranking actual")
    r.add_argument("--limit", type=int, default=10)
    r.add_argument("--status", default="new", choices=["new", "any", "seen", "acknowledged"])
    r.add_argument(
        "--format", default="markdown", choices=["markdown", "jsonl", "briefing", "table"]
    )
    r.add_argument("--title", default="Ranking de repos")
    r.set_defaults(func=cmd_rank)

    d = sub.add_parser("digest", help="digest listo para Telegram")
    d.add_argument("--limit", type=int, default=8)
    d.add_argument("--status", default="new", choices=["new", "any", "seen"])
    d.add_argument("--title", default="Nuevos repos en GitHub")
    d.add_argument("--no-hint", action="store_true", help="sin instrucción de feedback")
    d.set_defaults(func=cmd_digest)

    s = sub.add_parser("search", help="búsqueda full-text en lo ya descubierto")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    e = sub.add_parser("events", help="leer el bus de eventos")
    e.add_argument("--kind", default=None)
    e.add_argument("--after", type=int, default=0, help="id de evento desde el cual leer")
    e.add_argument("--limit", type=int, default=50)
    e.add_argument("--unconsumed", action="store_true", help="sólo eventos sin consumir")
    e.add_argument("--run", type=int, default=None, help="sólo eventos de esta corrida")
    e.add_argument(
        "--latest-run",
        action="store_true",
        help="ata al último collect exitoso; es lo que hace idempotente al digest diario",
    )
    e.add_argument(
        "--consume", nargs="?", const="cli", default=None, metavar="CONSUMER", help="marcar leídos"
    )
    e.set_defaults(func=cmd_events)

    a = sub.add_parser("ack", help="marcar repos como vistos")
    a.add_argument("repos", nargs="+")
    a.add_argument("--prefix", default="seen:")
    a.set_defaults(func=cmd_ack)

    v = sub.add_parser("decide", help="dar veredicto (entrena el filtro)")
    v.add_argument("repo")
    v.add_argument(
        "verdict", choices=["yes", "no", "noise", "star_per_day", "relevance", "quality"]
    )
    v.add_argument("--actor", default="user")
    v.add_argument("--note", default=None)
    v.set_defaults(func=cmd_decide)

    st = sub.add_parser("status", help="estado y cuota")
    st.set_defaults(func=cmd_status)

    w = sub.add_parser("weights", help="ver o resetear pesos")
    w.add_argument("--reset", action="store_true")
    w.set_defaults(func=cmd_weights)

    sv = sub.add_parser("serve", help="API HTTP local para otros agentes")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=default_port())
    sv.set_defaults(func=cmd_serve)

    pr = sub.add_parser("prune", help="limpiar observaciones antiguas")
    pr.add_argument("--keep-days", type=int, default=60)
    pr.set_defaults(func=cmd_prune)

    ri = sub.add_parser("reindex", help="reconstruir índice FTS")
    ri.set_defaults(func=cmd_reindex)

    ck = sub.add_parser("check", help="preflight: token, cuota y BD")
    ck.set_defaults(func=cmd_check)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    # The dedicated token wins; the MCP config is a fallback inside resolve_token.
    os.environ.setdefault("SIGNALHUB_DB", args.db)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:
        log.debug("command failed", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
