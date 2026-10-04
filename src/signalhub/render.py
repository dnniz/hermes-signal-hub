"""Rendering: turn scored repositories into text another agent can consume.

Three output shapes, all from the same data:

* ``markdown``  - human digest, tuned for Telegram (no tables, short lines)
* ``jsonl``     - one object per line, the agent-to-agent contract
* ``agent``     - a compact briefing block with explicit verdicts

Telegram has no table syntax and long messages get truncated silently, so the
markdown renderer hard-caps description length and total size, and reports what
it dropped instead of pretending the digest is complete.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .scoring import ScoredRepo

TELEGRAM_MESSAGE_LIMIT = 4096
MAX_DESC = 110
MAX_TOPICS = 4


@dataclass
class Digest:
    """A rendered digest plus the metadata a consumer needs to act."""

    title: str
    body: str
    items: list[ScoredRepo]
    generated_at: datetime
    truncated: bool = False
    dropped: int = 0
    notes: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "generated_at": self.generated_at.isoformat(),
            "count": len(self.items),
            "truncated": self.truncated,
            "dropped": self.dropped,
            "notes": self.notes or [],
            "items": [i.as_dict() for i in self.items],
        }


def _clip(text: str | None, limit: int = MAX_DESC) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _sparkline(velocity: float) -> str:
    """A 5-step bar so a human can scan relative momentum at a glance."""

    filled = max(0, min(5, round(velocity * 5)))
    return "▰" * filled + "▱" * (5 - filled)


def _delta_text(repo: ScoredRepo) -> str:
    if repo.star_delta is None:
        return ""
    if repo.star_delta > 0:
        return f" (+{repo.star_delta})"
    if repo.star_delta < 0:
        return f" ({repo.star_delta})"
    return ""


def render_markdown(
    items: Iterable[ScoredRepo],
    *,
    title: str = "Nuevos repos en GitHub",
    header: str | None = None,
    limit: int = 8,
    generated_at: datetime | None = None,
    verdict_hint: str | None = None,
    max_chars: int = TELEGRAM_MESSAGE_LIMIT,
) -> Digest:
    """Render a Telegram-safe digest.

    The layout is one block per repo: rank, name as link, stars with delta,
    velocity bar, and the single most useful "why". Everything else is dropped
    on purpose to keep the message scannable on a phone.

    If the result exceeds ``max_chars`` the lowest-ranked entries are dropped
    one at a time until it fits, and the digest says how many were omitted --
    a silent truncation would make a short digest look like a complete one.
    """

    from .github import utcnow

    now = generated_at or utcnow()
    candidates = list(items)
    kept = candidates[:limit]
    dropped = len(candidates) - len(kept)

    def build(entries: list[ScoredRepo], omitted: int) -> str:
        lines = [f"**{title}**", ""]
        if header:
            lines.extend([header, ""])
        lines.append(
            f"_{now.strftime('%Y-%m-%d %H:%M')} UTC · {len(entries)} de {len(candidates)}_"
        )
        lines.append("")
        lines.extend(_blocks(entries))
        if omitted:
            lines.append(f"… +{omitted} más no caben en el mensaje")
        if verdict_hint:
            lines.append("")
            lines.append(verdict_hint)
        return "\n".join(lines).rstrip()

    body = build(kept, dropped)
    truncated = False
    while len(body) > max_chars and len(kept) > 1:
        kept = kept[:-1]
        dropped = len(candidates) - len(kept)
        body = build(kept, dropped)
        truncated = True

    return Digest(
        title=title,
        body=body,
        items=kept,
        generated_at=now,
        truncated=truncated,
        dropped=dropped,
        notes=[f"{dropped} omitted to fit {max_chars} chars"] if truncated else [],
    )


def _blocks(ranked: list[ScoredRepo]) -> list[str]:
    lines: list[str] = []
    for i, item in enumerate(ranked, start=1):
        repo = item.repo
        meta_bits = [f"⭐ {repo.stars:,}{_delta_text(item)}"]
        if repo.language:
            meta_bits.append(repo.language)
        if repo.license:
            meta_bits.append(repo.license)
        desc = _clip(repo.description)
        block = [f"{i}. [{repo.full_name}]({repo.html_url})"]
        if desc:
            block.append(f"   {desc}")
        block.append(
            f"   {' · '.join(meta_bits)} · {repo.stars_per_day:.0f}⭐/día {_sparkline(item.velocity)}"
        )
        why = _clip(item.explain(), 90)
        if why:
            block.append(f"   💡 {why}")
        if repo.topics:
            block.append("   " + " ".join(f"#{t}" for t in repo.topics[:MAX_TOPICS]))
        block.append("")
        lines.extend(block)
    return lines


def render_jsonl(items: Iterable[ScoredRepo], *, rank_offset: int = 0) -> str:
    """One JSON object per line: the machine contract for other agents."""

    out = []
    for i, item in enumerate(items, start=1 + rank_offset):
        payload = item.as_dict()
        payload["rank"] = i
        out.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return "\n".join(out)


def render_agent_briefing(
    items: Iterable[ScoredRepo],
    *,
    limit: int = 10,
    context: str | None = None,
) -> str:
    """A briefing written for another agent, not for a human reader.

    Includes the scoring rationale because the consumer needs to judge whether
    the ranking itself is wrong, and the exact verdict command to report back.
    """

    ranked = list(items)[:limit]
    lines = ["# signal-hub briefing", ""]
    if context:
        lines.extend([context, ""])
    lines.append("Read-only. To report a verdict run:")
    lines.append("  signalhub decide <owner/repo> <yes|no|noise>")
    lines.append("")
    for i, item in enumerate(ranked, start=1):
        c = item.as_dict()
        lines.append(
            f"{i}. {c['full_name']} | score {c['total_score']} | "
            f"{c['stars']}⭐ | {c['stars_per_day']}/day | {c['age_days']}d old"
        )
        if c.get("description"):
            lines.append(f"   {c['description']}")
        lines.append(
            f"   components: vel={c['components']['velocity']} eng={c['components']['engagement']} "
            f"rel={c['components']['relevance']} dev={c['components']['developer']} "
            f"pen={c['components']['penalty']}"
        )
        if c.get("reasons"):
            lines.append(f"   why: {'; '.join(c['reasons'])}")
        lines.append(f"   url: {c['html_url']}")
    return "\n".join(lines)


def render_stats(stats: dict[str, Any], *, health: dict[str, Any] | None = None) -> str:
    """Compact operational summary for `signalhub status`."""

    lines = ["**signalhub status**", ""]
    lines.append(
        f"- repos conocidos: {stats.get('repos', 0):,} ({stats.get('new_repos', 0):,} nuevos)".replace(
            ",", ","
        )
    )
    lines.append(f"- estrellas totales: {stats.get('total_stars', 0):,}".replace(",", ","))
    lines.append(
        f"- eventos: {stats.get('events', 0)} (sin consumir: {stats.get('unconsumed_events', 0)})"
    )
    lines.append(f"- observaciones: {stats.get('observations', 0)}")
    last = stats.get("last_run")
    if last:
        lines.append(
            f"- última corrida: run #{last['id']} · {last['finished_at']} · "
            f"{last['candidates']} candidatos · {last['api_calls']} llamadas"
        )
    else:
        lines.append("- última corrida: nunca")
    if health:
        w = health.get("weights", {})
        lines.append(
            f"- pesos: vel={w.get('velocity')} eng={w.get('engagement')} "
            f"rel={w.get('relevance')} dev={w.get('developer')} pen={w.get('penalty_scale')}"
        )
        fb = health.get("feedback") or {}
        if fb:
            lines.append(f"- feedback: {fb}")
        rl = health.get("rate_limit") or {}
        if rl:
            lines.append(
                f"- cuota: core {rl.get('core_remaining')}/{rl.get('core_limit')} · "
                f"search {rl.get('search_remaining')}/{rl.get('search_limit')}"
            )
    return "\n".join(lines)
