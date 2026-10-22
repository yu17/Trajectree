import json
import pytest
from trajectree.runner.parse import parse_claude_json, parse_codex

RESULT = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 345, "num_turns": 2, "result": "Answer.",
          "session_id": "abc", "total_cost_usd": 0.0123, "structured_output": {"answer": "A", "proposals": []},
          "usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7}}

def test_parse_success_picks_last_result_line():
    r = parse_claude_json('{"type":"system","subtype":"init"}\n' + json.dumps(RESULT) + "\n")
    assert r.ok and r.result_text == "Answer." and r.structured == {"answer": "A", "proposals": []}
    assert (r.usage.tokens_in, r.usage.tokens_out, r.usage.tokens_cache) == (100, 20, 12)
    assert r.est_usd == 0.0123 and r.turns == 2 and r.duration_ms == 345 and r.session_id == "abc" and r.error is None

def test_parse_budget_error():
    r = parse_claude_json(json.dumps({**RESULT, "subtype": "error_max_budget_usd", "is_error": True, "result": "Reached budget"}))
    assert not r.ok and r.subtype == "error_max_budget_usd" and r.error == "Reached budget" and r.est_usd == 0.0123

def test_parse_garbage_is_failed():
    with pytest.raises(ValueError, match="no result object"):
        parse_claude_json('not json\n{"type": "assistant"}\n')
    with pytest.raises(ValueError):
        parse_claude_json("")

def test_parse_codex():
    events = '{"type":"turn.completed","usage":{"input_tokens":50,"output_tokens":9}}\n{"type":"other"}\nnot json\n'
    r = parse_codex(events, '{"answer": "A", "proposals": []}')
    assert r.ok and r.structured == {"answer": "A", "proposals": []} and r.usage.tokens_in == 50 and r.usage.tokens_out == 9
    assert not parse_codex("", None).ok
