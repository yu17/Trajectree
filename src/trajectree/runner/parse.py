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
        session_id=data.get("session_id"),
        usage=Usage(
            tokens_in=raw_usage.get("input_tokens", 0),
            tokens_out=raw_usage.get("output_tokens", 0),
            tokens_cache=raw_usage.get("cache_creation_input_tokens", 0) + raw_usage.get("cache_read_input_tokens", 0),
        ),
        est_usd=data.get("total_cost_usd", 0.0),
        turns=data.get("num_turns", 0),
        duration_ms=data.get("duration_ms", 0),
        error=None if ok else (text or subtype),
    )


def parse_codex(stdout: str, last_message: str | None) -> ParsedResult:
    tokens_in = tokens_out = 0
    for line in stdout.splitlines():
        obj = _json_object(line)
        usage = obj.get("usage") if obj else None
        if isinstance(usage, dict):
            tokens_in += usage.get("input_tokens", 0)
            tokens_out += usage.get("output_tokens", 0)
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
