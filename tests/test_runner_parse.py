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

@pytest.mark.parametrize("bad", [None, "12", "x", True, [1], {"a": 1}])
def test_parse_tolerates_non_numeric_fields(bad):
    usage = {k: bad for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    data = {**RESULT, "usage": usage, "total_cost_usd": bad, "num_turns": bad, "duration_ms": bad, "session_id": bad, "result": bad}
    r = parse_claude_json(json.dumps(data))
    assert (r.usage.tokens_in, r.usage.tokens_out, r.usage.tokens_cache) == (0, 0, 0)
    assert r.est_usd == 0.0 and r.turns == 0 and r.duration_ms == 0
    if isinstance(bad, str):   # a string result and session id are kept as they are
        assert r.result_text == bad and r.session_id == bad
    else:
        assert r.result_text == "" and r.session_id is None

def test_parse_null_usage_and_numeric_session_id():
    r = parse_claude_json(json.dumps({**RESULT, "usage": None, "session_id": 42, "num_turns": 3.0, "total_cost_usd": 1}))
    assert r.usage.tokens_in == 0 and r.session_id == "42" and r.turns == 3 and r.est_usd == 1.0
    assert parse_claude_json(json.dumps({**RESULT, "session_id": ""})).session_id is None

def test_parse_codex_tolerates_non_numeric_usage():
    r = parse_codex('{"usage":{"input_tokens":null,"output_tokens":"9"}}\n', "x")
    assert r.usage.tokens_in == 0 and r.usage.tokens_out == 0

def test_parse_codex():
    events = '{"type":"turn.completed","usage":{"input_tokens":50,"output_tokens":9}}\n{"type":"other"}\nnot json\n'
    r = parse_codex(events, '{"answer": "A", "proposals": []}')
    assert r.ok and r.structured == {"answer": "A", "proposals": []} and r.usage.tokens_in == 50 and r.usage.tokens_out == 9
    assert not parse_codex("", None).ok

def test_non_finite_usage_is_zero():
    out = parse_claude_json(json.dumps({"type": "result", "subtype": "success", "is_error": False,
        "result": "x", "total_cost_usd": float("nan"), "usage": {"input_tokens": float("inf")}, "num_turns": 1}))
    assert out.est_usd == 0 and out.usage.tokens_in == 0


def test_parse_codex_reports_turn_failure():
    msg = ("{\"type\":\"error\",\"error\":{\"message\":\"Invalid schema for response_format 'codex_output_schema': "
           "'schema.properties.proposals.items.required' is required to include every key in properties. Missing "
           "'event_kind'.\",\"type\":\"invalid_request_error\",\"param\":\"text.format.schema\","
           "\"code\":\"invalid_json_schema\"},\"status\":400}")
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"01a16fc5-982d-7ad3-9adc-3f975ce7247e"}',
        '{"type":"turn.started"}',
        json.dumps({"type": "error", "message": msg}),
        json.dumps({"type": "turn.failed", "error": {"message": msg}}),
    ]) + "\n"
    r = parse_codex(stdout, None)
    assert not r.ok and "Invalid schema for response_format" in r.error and len(r.error) <= 500


def test_parse_codex_error_event_without_turn_failed_and_default():
    assert parse_codex(json.dumps({"type": "error", "message": "boom"}) + "\n", None).error == "boom"
    assert parse_codex('{"type":"turn.started"}\n', None).error == "no last message"


def test_claude_model_is_the_one_with_most_output():
    usage = {"a": {"outputTokens": 1}, "b": {"outputTokens": 9}}
    assert parse_claude_json(json.dumps({**RESULT, "modelUsage": usage})).model == "b"
    for bad in (None, {}, [], "x", {"a": 1}, {"": {"outputTokens": 3}}):
        assert parse_claude_json(json.dumps({**RESULT, "modelUsage": bad})).model is None
    assert parse_claude_json(json.dumps(RESULT)).model is None
