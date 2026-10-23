from __future__ import annotations

import json

from pydantic import BaseModel


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache: int = 0


class ParsedResult(BaseModel):
    ok: bool
    subtype: str
    result_text: str = ""
    structured: dict | None = None
    session_id: str | None = None
    usage: Usage = Usage()
    est_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    error: str | None = None


def _json_object(text: str) -> dict | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: object) -> int:
    """An int for a JSON number; 0 for null, bools, strings and anything else."""
    return int(value) if _number(value) else 0  # type: ignore[arg-type]


def _float(value: object) -> float:
    return float(value) if _number(value) else 0.0  # type: ignore[arg-type]


def _session_id(value: object) -> str | None:
    if isinstance(value, str):
        return value or None
    return str(value) if _number(value) else None


def parse_claude_json(stdout: str) -> ParsedResult:
    data: dict | None = None
    for line in reversed(stdout.splitlines()):
        obj = _json_object(line)
        if obj is not None and obj.get("type") == "result":
            data = obj
            break
    if data is None:
        obj = _json_object(stdout)
        if obj is not None and obj.get("type") == "result":
            data = obj
    if data is None:
        raise ValueError("no result object")
    subtype = str(data.get("subtype") or "")
    ok = subtype == "success" and not data.get("is_error")
    raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    structured = data.get("structured_output")
    text = data.get("result") if isinstance(data.get("result"), str) else ""
    return ParsedResult(
        ok=ok,
        subtype=subtype,
        result_text=text,
        structured=structured if isinstance(structured, dict) else None,
        session_id=_session_id(data.get("session_id")),
        usage=Usage(
            tokens_in=_int(raw_usage.get("input_tokens")),
            tokens_out=_int(raw_usage.get("output_tokens")),
            tokens_cache=_int(raw_usage.get("cache_creation_input_tokens"))
            + _int(raw_usage.get("cache_read_input_tokens")),
        ),
        est_usd=_float(data.get("total_cost_usd")),
        turns=_int(data.get("num_turns")),
        duration_ms=_int(data.get("duration_ms")),
        error=None if ok else (text or subtype),
    )


def parse_codex(stdout: str, last_message: str | None) -> ParsedResult:
    tokens_in = tokens_out = 0
    for line in stdout.splitlines():
        obj = _json_object(line)
        usage = obj.get("usage") if obj else None
        if isinstance(usage, dict):
            tokens_in += _int(usage.get("input_tokens"))
            tokens_out += _int(usage.get("output_tokens"))
    ok = last_message is not None
    structured = _json_object(last_message) if last_message else None
    return ParsedResult(
        ok=ok,
        subtype="success" if ok else "error",
        result_text=last_message or "",
        structured=structured,
        usage=Usage(tokens_in=tokens_in, tokens_out=tokens_out),
        error=None if ok else "no last message",
    )
