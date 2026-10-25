"""Activity digest: one line per recent signal, for prompts that need to know what happened."""

from datetime import datetime
from typing import Any

import apsw

from trajectree.models import Signal
from trajectree.repo.signals import count_signals, list_signals


_FETCH = 300  # rows read per digest; more than any max_chars budget can show


def _one_line(value: Any, limit: int = 120) -> str:
    return " ".join(str(value).split())[:limit]


def _items(value: Any, limit: int = 3) -> list[str]:
    return [_one_line(v) for v in value[:limit]] if isinstance(value, list) else []


def _count(payload: dict[str, Any], key: str) -> int:
    n = payload.get(key)
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def _line(s: Signal) -> str:
    day, p = s.t_start.strftime("%Y-%m-%d"), s.payload
    if s.kind == "commits":
        return f"- {day} {_count(p, 'count')} commits: {'; '.join(_items(p.get('subjects')))}"
    if s.kind == "session":
        name = _one_line(p.get("title") or str(p.get("session_id") or "")[:8])
        model = _one_line(p.get("model") or "unknown model")
        if "segment" in p:
            first = f': "{_one_line(p["first_prompt"])}"' if p.get("first_prompt") else ""
            return (
                f'- {day} session "{name}" segment {_count(p, "segment")}/{_count(p, "segments")}'
                f' ({_count(p, "prompts")} prompts, {model}){first}'
            )
        return f'- {day} session "{name}" ({_count(p, "prompts")} prompts, {_one_line(p.get("model") or "unknown model")})'
    if s.kind == "files_changed":
        return f"- {day} {_count(p, 'count')} files changed ({', '.join(_items(p.get('paths')))})"
    if s.kind == "job_state":
        return f"- {day} job {_one_line(p.get('name', '?'))} {_one_line(p.get('state', '?'))}"
    if s.kind == "remote":
        titles = [
            _one_line(item["title"])
            for key in ("prs", "issues", "releases")
            for item in (p.get(key) if isinstance(p.get(key), list) else [])
            if isinstance(item, dict) and item.get("title")
        ][:3]
        return f"- {day} {_count(p, 'count')} remote updates: {'; '.join(titles)}"
    if s.kind == "message":
        return f"- {day} message via {_one_line(p.get('channel', '?'))}: {_one_line(p.get('summary', ''))}"
    return f"- {day} {s.kind}"


def activity_digest(
    conn: apsw.Connection,
    *,
    project_ids: list[str],
    since: datetime,
    until: datetime,
    max_chars: int = 4000,
) -> str:
    """Signals in `[since, until]`, newest first, cut at `max_chars` with an `(… n more)` line."""
    window = {"project_ids": project_ids, "t_from": since, "t_to": until}
    signals = list_signals(conn, **window, limit=_FETCH, newest_first=True)
    if not signals:
        return "_no activity recorded_"
    lines = [_line(s) for s in signals]
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    if len(kept) < len(lines):  # beyond the fetched rows, the true total says how many are left
        total = count_signals(conn, **window) if len(lines) == _FETCH else len(lines)
        kept.append(f"(… {total - len(kept)} more)")
    return "\n".join(kept)
