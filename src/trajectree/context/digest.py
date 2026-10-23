"""Activity digest: one line per recent signal, for prompts that need to know what happened."""

from datetime import datetime
from typing import Any

import apsw

from trajectree.models import Signal
from trajectree.repo.signals import list_signals


def _items(value: Any, limit: int = 3) -> list[str]:
    return [str(v) for v in value[:limit]] if isinstance(value, list) else []


def _count(payload: dict[str, Any], key: str) -> int:
    n = payload.get(key)
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def _line(s: Signal) -> str:
    day, p = s.t_start.strftime("%Y-%m-%d"), s.payload
    if s.kind == "commits":
        return f"- {day} {_count(p, 'count')} commits: {'; '.join(_items(p.get('subjects')))}"
    if s.kind == "session":
        name = p.get("title") or str(p.get("session_id") or "")[:8]
        return f'- {day} session "{name}" ({_count(p, "prompts")} prompts, {p.get("model") or "unknown model"})'
    if s.kind == "files_changed":
        return f"- {day} {_count(p, 'count')} files changed ({', '.join(_items(p.get('paths')))})"
    if s.kind == "job_state":
        return f"- {day} job {p.get('name', '?')} {p.get('state', '?')}"
    if s.kind == "remote":
        return f"- {day} {_count(p, 'count')} remote updates: {'; '.join(_items(p.get('titles')))}"
    if s.kind == "message":
        return f"- {day} message via {p.get('channel', '?')}: {p.get('summary', '')}"
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
    signals = list_signals(conn, project_ids=project_ids, t_from=since, t_to=until)
    if not signals:
        return "_no activity recorded_"
    lines = [_line(s) for s in sorted(signals, key=lambda s: (s.t_start, s.id), reverse=True)]
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    if len(kept) < len(lines):
        kept.append(f"(… {len(lines) - len(kept)} more)")
    return "\n".join(kept)
