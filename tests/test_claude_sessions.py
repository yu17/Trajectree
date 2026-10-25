import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from trajectree.connectors.claude_sessions import SessionSummary, parse_lines, scan_transcripts
from trajectree.connectors.paths import encode_project_dir
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.signals import list_signals

UTC = timezone.utc
CWD = "/home/u/proj"
SID = "11111111-1111-1111-1111-111111111111"

def rec_user(ts, text="hi", cwd=CWD, sid=SID, tool_result=False, meta=False):
    content = [{"type": "tool_result", "content": "x"}] if tool_result else text
    d = {"type": "user", "message": {"role": "user", "content": content}, "timestamp": ts, "cwd": cwd, "sessionId": sid, "gitBranch": "main", "version": "2.1.308"}
    if meta: d["isMeta"] = True
    return d

def rec_assistant(ts, model="claude-opus-5-5", cwd=CWD, sid=SID):
    return {"type": "assistant", "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7}},
            "timestamp": ts, "cwd": cwd, "sessionId": sid}

def write_transcript(root: Path, cwd: str, sid: str, records, newline_at_end=True) -> Path:
    d = root / "projects" / encode_project_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    body = "\n".join(json.dumps(r) for r in records)
    p.write_text(body + ("\n" if newline_at_end else ""))
    return p

def setup(ledger, human, settings, tmp_path):
    home = tmp_path / "claude"
    s = settings.model_copy(update={"claude_home": home, "backends_root": tmp_path / "backends"})
    p = create_project(ledger, human, ProjectIn(slug="proj", title="Proj", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=CWD))
    return home, s, p

def test_parse_lines_counts_prompts_tokens_and_title():
    acc = SessionSummary(transcript_path="x", backend="claude")
    lines = [json.dumps(r).encode() for r in [
        rec_user("2026-01-05T10:00:00.000Z"), rec_user("2026-01-05T10:01:00.000Z", tool_result=True),
        rec_user("2026-01-05T10:01:30.000Z", meta=True), rec_assistant("2026-01-05T10:02:00.000Z"),
        {"type": "ai-title", "aiTitle": "Fix the build", "sessionId": SID}, b"not json".decode()]]
    out = parse_lines(lines, acc)
    assert (out.prompts, out.assistant_msgs, out.tokens_in, out.tokens_out) == (1, 1, 115, 7)
    assert out.title == "Fix the build" and out.model == "claude-opus-5-5" and out.cwd == CWD and out.git_branch == "main"
    assert out.t_start == datetime(2026, 1, 5, 10, tzinfo=UTC) and out.t_end == datetime(2026, 1, 5, 10, 2, tzinfo=UTC)

def test_parse_lines_counts_usage_once_per_message_id():
    def line(msg_id, out):
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": out}
        return json.dumps({"type": "assistant", "message": {"id": msg_id, "role": "assistant", "usage": usage}}).encode()
    lines = [line("m1", 3), line("m1", 7), line("m2", 2)]
    out = parse_lines(lines, SessionSummary(transcript_path="x", backend="claude"))
    assert (out.assistant_msgs, out.tokens_in, out.tokens_out) == (2, 230, 9)
    # The same lines split across incremental reads (summary stored as JSON in between).
    first = parse_lines(lines[:1], SessionSummary(transcript_path="x", backend="claude"))
    resumed = SessionSummary.model_validate(first.model_dump(mode="json"))
    out2 = parse_lines(lines[1:], resumed)
    assert (out2.assistant_msgs, out2.tokens_in, out2.tokens_out) == (2, 230, 9)

def test_session_id_falls_back_to_file_stem(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    rec = rec_user("2026-01-05T10:00:00.000Z")
    del rec["sessionId"]
    write_transcript(home, CWD, "4" * 36, [rec])
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    sig = list_signals(ledger.conn, project_ids=[p.id])[0]
    assert sig.dedupe_key == f"claude_session:{'4' * 36}:2026-01-05T10:00" and sig.payload["session_id"] == "4" * 36

def test_scan_maps_sessions_and_skips_unmapped(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z"), rec_assistant("2026-01-05T10:02:00.000Z"), rec_user("2026-01-05T10:03:00.000Z")])
    write_transcript(home, "/elsewhere", "2" * 36, [rec_user("2026-01-05T10:00:00.000Z", cwd="/elsewhere", sid="2" * 36)])
    now = datetime.now(UTC)
    report = scan_transcripts(ledger.conn, s, now=now)
    assert report.files_seen == 2 and report.files_read == 2 and report.signals_written == 1
    sig = list_signals(ledger.conn, project_ids=[p.id])[0]
    assert sig.kind == "session" and sig.dedupe_key == f"claude_session:{SID}:2026-01-05T10:00"
    assert sig.payload["prompts"] == 2 and sig.payload["backend"] == "claude" and sig.payload["running"] is True
    assert report.live[0].session_id == SID and report.live[0].project_id == p.id

def test_scan_is_incremental(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])
    now = datetime.now(UTC)
    scan_transcripts(ledger.conn, s, now=now)
    assert scan_transcripts(ledger.conn, s, now=now).files_read == 0
    with path.open("a") as f:
        f.write(json.dumps(rec_user("2026-01-05T10:05:00.000Z")) + "\n")
    os.utime(path, None)
    r = scan_transcripts(ledger.conn, s, now=now)
    assert r.files_read == 1 and list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 2

def test_scanner_resumes_after_partial_line(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])
    partial = json.dumps(rec_user("2026-01-05T10:05:00.000Z"))
    with path.open("a") as f:
        f.write(partial[:40])
    now = datetime.now(UTC)
    scan_transcripts(ledger.conn, s, now=now)
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 1
    with path.open("a") as f:
        f.write(partial[40:] + "\n")
    os.utime(path, None)
    scan_transcripts(ledger.conn, s, now=now)
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 2

def test_malformed_lines_are_skipped(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])
    with path.open("a") as f:
        f.write("{broken\n[1,2]\n" + json.dumps(rec_user("2026-01-05T10:06:00.000Z")) + "\n")
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 2

def test_live_depends_on_mtime_and_shrunk_file_rescans(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z"), rec_user("2026-01-05T10:01:00.000Z")])
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(path, (old, old))
    r = scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert r.live == [] and list_signals(ledger.conn, project_ids=[p.id])[0].payload["running"] is False
    write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])   # rewritten shorter
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 1

def test_backend_profiles_are_scanned(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    write_transcript(tmp_path / "backends" / "deepseek", CWD, "3" * 36, [rec_user("2026-01-05T10:00:00.000Z", sid="3" * 36)])
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["backend"] == "deepseek"

def test_scan_without_roots_is_noop(ledger, settings):
    r = scan_transcripts(ledger.conn, settings, now=datetime.now(UTC))
    assert r.files_seen == 0 and r.live == []

def test_large_transcript_streams_and_resumes(ledger, human, settings, tmp_path):
    from trajectree.repo.state import get_state
    home, s, p = setup(ledger, human, settings, tmp_path)
    records = [rec_user("2026-01-05T10:00:00.000Z", text="x" * 300) for _ in range(20000)]
    path = write_transcript(home, CWD, SID, records)
    complete_len = path.stat().st_size
    assert complete_len > 9_000_000
    partial = json.dumps(rec_user("2026-01-05T11:00:00.000Z"))
    with path.open("a") as f:
        f.write(partial[:40])
    now = datetime.now(UTC)
    scan_transcripts(ledger.conn, s, now=now)
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 20000
    assert get_state(ledger.conn, f"scan:{path.absolute()}")["offset"] == complete_len
    with path.open("a") as f:
        f.write(partial[40:] + "\n" + json.dumps(rec_user("2026-01-05T11:01:00.000Z")) + "\n")
    os.utime(path, None)
    r = scan_transcripts(ledger.conn, s, now=now)
    assert r.files_read == 1
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 20002
    assert get_state(ledger.conn, f"scan:{path.absolute()}")["offset"] == path.stat().st_size


def rec_assistant_text(ts, text, cwd=CWD, sid=SID):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            "timestamp": ts, "cwd": cwd, "sessionId": sid}

def rec_assistant_tool(ts, name, cwd=CWD, sid=SID):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": {}}]},
            "timestamp": ts, "cwd": cwd, "sessionId": sid}

def test_digest_transcript_sections_and_cap(tmp_path):
    from trajectree.connectors.claude_sessions import digest_transcript
    path = write_transcript(tmp_path, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z", text="fix the encoder"), rec_assistant_tool("2026-01-05T10:01:00.000Z", "Read"), rec_assistant_text("2026-01-05T10:02:00.000Z", "Froze the encoder.")])
    d = digest_transcript(path)
    assert "## Prompts\n1. fix the encoder" in d and "Read × 1" in d and d.rstrip().endswith("Froze the encoder.")
    assert digest_transcript(path, max_chars=60).endswith("(digest truncated)")

def test_digest_transcript_skips_noise_and_keeps_first_and_last_assistant(tmp_path):
    from trajectree.connectors.claude_sessions import digest_transcript
    recs = [rec_user("2026-01-05T10:00:00.000Z", text="go"), rec_user("2026-01-05T10:00:01.000Z", tool_result=True), rec_user("2026-01-05T10:00:02.000Z", text="meta", meta=True)]
    recs += [rec_assistant_text(f"2026-01-05T10:{i:02d}:00.000Z", f"step {i} " + "x" * 400) for i in range(1, 26)]
    recs += [rec_assistant_tool("2026-01-05T11:00:00.000Z", "Bash"), rec_assistant_tool("2026-01-05T11:01:00.000Z", "Bash"), rec_assistant_tool("2026-01-05T11:02:00.000Z", "Read")]
    path = write_transcript(tmp_path, CWD, SID, recs)
    with path.open("ab") as f:
        f.write(b"not json\n[1,2]\n")
    d = digest_transcript(path, max_chars=50000)
    assert "1. go" in d and "meta" not in d and "tool_result" not in d
    assert d.index("Bash × 2") < d.index("Read × 1")
    assert "step 1 " in d and "step 10 " in d and "step 11 " not in d and "step 15 " not in d and "step 16 " in d and "step 25 " in d
    assert "x" * 301 not in d.split("## Final message")[0]
    assert "## Final message\nstep 25 " in d

def import_props(**sources):
    return {"import": {"sources": sources, "recorded_at": "2026-10-01T00:00:00+00:00"}}

def setup_with_import(ledger, human, settings, tmp_path, **sources):
    home = tmp_path / "claude"
    s = settings.model_copy(update={"claude_home": home, "backends_root": tmp_path / "backends"})
    p = create_project(ledger, human, ProjectIn(slug="proj", title="Proj", kind="research", props=import_props(**sources)))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=CWD))
    return home, s, p

def test_unpicked_sessions_not_attached(ledger, human, settings, tmp_path):
    home, s, p = setup_with_import(ledger, human, settings, tmp_path, claude={"picked": False})
    write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])
    report = scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert report.signals_written == 0 and report.live == []
    assert list_signals(ledger.conn, project_ids=[p.id]) == []

def test_session_before_start_not_attached(ledger, human, settings, tmp_path):
    home, s, p = setup_with_import(ledger, human, settings, tmp_path, claude={"picked": True, "since": "2026-05-01"})
    write_transcript(home, CWD, SID, [rec_user("2026-03-01T10:00:00.000Z"), rec_assistant("2026-03-01T10:02:00.000Z")])
    other = "2" * 36
    write_transcript(home, CWD, other, [rec_user("2026-06-01T10:00:00.000Z", sid=other)])
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert [x.payload["session_id"] for x in list_signals(ledger.conn, project_ids=[p.id])] == [other]

def test_all_lifts_transcript_cutoff(ledger, human, settings, tmp_path):
    old = (datetime.now(UTC) - timedelta(days=800)).timestamp()
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2024-08-01T10:00:00.000Z")])
    os.utime(path, (old, old))
    assert scan_transcripts(ledger.conn, s, now=datetime.now(UTC)).files_seen == 0
    # another project asking for everything lifts the file cutoff; this project still starts at the default
    other = create_project(ledger, human, ProjectIn(slug="deep", title="Deep", kind="research", props=import_props(claude={"picked": True, "since": "all"})))
    upsert_resource(ledger, human, ResourceIn(project_id=other.id, kind="folder", locator="/home/u/deep"))
    write_transcript(home, "/home/u/deep", "4" * 36, [rec_user("2024-08-01T10:00:00.000Z", cwd="/home/u/deep", sid="4" * 36)])
    os.utime(home / "projects" / encode_project_dir("/home/u/deep") / f"{'4' * 36}.jsonl", (old, old))
    report = scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert report.files_seen == 2 and report.signals_written == 1
    assert [x.payload["session_id"] for x in list_signals(ledger.conn, project_ids=[other.id])] == ["4" * 36]
    assert list_signals(ledger.conn, project_ids=[p.id]) == []

def test_default_cutoff_is_local_midnight_of_the_default_day(ledger, settings):
    from trajectree.connectors.claude_sessions import transcript_cutoff
    s = settings.model_copy(update={"tz": "Europe/Berlin", "scan_since_days": 30})
    now = datetime(2026, 10, 24, 15, 30, tzinfo=UTC)
    midnight = datetime(2026, 9, 23, 22, tzinfo=UTC).timestamp()   # 2026-09-24 00:00 in Berlin (CEST)
    assert transcript_cutoff(ledger.conn, s, now, "claude") == midnight
    assert transcript_cutoff(ledger.conn, s, now, "codex") == midnight

def test_digest_transcript_missing_file_raises(tmp_path):
    from trajectree.connectors.claude_sessions import digest_transcript
    import pytest
    with pytest.raises(OSError):
        digest_transcript(tmp_path / "nope.jsonl")


def _two_bursts():
    return [
        rec_user("2026-08-30T19:20:00.000Z"), rec_assistant("2026-08-30T19:30:00.000Z"),
        rec_user("2026-09-22T02:57:00.000Z"), rec_assistant("2026-09-22T03:00:00.000Z"),
    ]


def _lines(records):
    return [json.dumps(r).encode() + b"\n" for r in records]


def test_segments_split_on_idle_gap_with_stable_keys():
    from trajectree.connectors.claude_sessions import segment_key
    acc = parse_lines(_lines(_two_bursts()), SessionSummary(transcript_path="/x/a.jsonl", backend="claude"))
    assert len(acc.segments) == 2
    a, b = acc.segments
    assert a.t_start == datetime(2026, 8, 30, 19, 20, tzinfo=UTC) and a.t_end == datetime(2026, 8, 30, 19, 30, tzinfo=UTC)
    assert (a.prompts, a.records, a.first_prompt) == (1, 2, "hi")
    assert b.t_start == datetime(2026, 9, 22, 2, 57, tzinfo=UTC)
    assert segment_key("claude", SID, a.t_start) == f"claude_session:{SID}:2026-08-30T19:20"


def test_segment_extends_as_the_file_grows(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, _two_bursts()[:2])
    now = datetime.now(UTC)
    scan_transcripts(ledger.conn, s, now=now)
    (first,) = list_signals(ledger.conn, kinds=["session"])
    with path.open("a") as f:
        f.write(json.dumps(rec_user("2026-08-30T19:45:00.000Z")) + "\n")
        f.write(json.dumps(rec_user("2026-09-22T02:57:00.000Z")) + "\n")
    os.utime(path, None)
    scan_transcripts(ledger.conn, s, now=now)
    rows = list_signals(ledger.conn, kinds=["session"])
    assert len(rows) == 2
    assert rows[0].dedupe_key == first.dedupe_key and rows[0].t_end == datetime(2026, 8, 30, 19, 45, tzinfo=UTC)
    assert (rows[0].payload["segment"], rows[0].payload["segments"]) == (1, 2)
    assert rows[1].payload["segment"] == 2


def test_segments_tolerate_unordered_and_missing_timestamps():
    no_ts = rec_user("x")
    del no_ts["timestamp"]
    records = [
        rec_assistant("2026-08-30T19:30:00.000Z", cwd=None),
        rec_user("2026-08-30T19:20:00.000Z", cwd=None),
        no_ts,
        rec_user("2026-08-30T19:25:00.000Z", cwd=None),
    ]
    acc = parse_lines(_lines(records), SessionSummary(transcript_path="/x/a.jsonl", backend="claude"))
    (seg,) = acc.segments
    assert seg.t_start == datetime(2026, 8, 30, 19, 20, tzinfo=UTC) and seg.t_end == datetime(2026, 8, 30, 19, 30, tzinfo=UTC)
    assert seg.records == 3
    assert acc.cwd == CWD


def test_metadata_records_do_not_open_or_count_toward_segments():
    meta = {"type": "custom-title", "customTitle": "t", "timestamp": "2026-07-01T00:00:00.000Z"}
    acc = parse_lines(_lines([meta, rec_user("2026-08-30T19:20:00.000Z")]), SessionSummary(transcript_path="/x/a.jsonl", backend="claude"))
    (seg,) = acc.segments
    assert seg.records == 1 and seg.t_start == datetime(2026, 8, 30, 19, 20, tzinfo=UTC)
    assert acc.t_start == datetime(2026, 7, 1, tzinfo=UTC)


def test_first_uuid_and_skip_until():
    a = rec_user("2026-08-30T19:20:00.000Z"); a["uuid"] = "u1"
    b = rec_user("2026-09-22T02:57:00.000Z"); b["uuid"] = "u2"
    base = SessionSummary(transcript_path="/x/a.jsonl", backend="claude", skip_until=datetime(2026, 9, 1, tzinfo=UTC))
    acc = parse_lines(_lines([a, b]), base)
    assert acc.first_uuid == "u1" and acc.prompts == 1 and len(acc.segments) == 1


def test_session_payload_carries_segment_fields(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    write_transcript(home, CWD, SID, _two_bursts())
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    rows = list_signals(ledger.conn, kinds=["session"])
    for key in ("segment", "segments", "records", "prompts", "first_prompt", "resumed_from"):
        assert key in rows[0].payload
    assert rows[0].payload["resumed_from"] is None
    assert [r.payload["running"] for r in rows] == [False, True]


def test_settings_read_segment_gap_and_idle(monkeypatch, tmp_path):
    from trajectree.config import load_settings
    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRAJECTREE_SESSION_IDLE_S", raising=False)
    monkeypatch.setenv("TRAJECTREE_SEGMENT_GAP_S", "600")
    s = load_settings()
    assert s.segment_gap_s == 600 and s.idle_s == 600 and s.inspect_budget_chars == 40000
    monkeypatch.setenv("TRAJECTREE_SESSION_IDLE_S", "90")
    assert load_settings().idle_s == 90


FIXTURES = Path(__file__).parent / "fixtures" / "claude"
ORIG = "3f2b8c1e-5d4a-4e7b-9a10-2c6d8e4f1a01"
CONT = "7a9d0e52-1b3c-4f68-8d2e-5c0b7a4e9f02"
THIRD = "c4e1f7a3-9b2d-4a56-8e0f-1d3b5c7e9a03"


def _install(home, sid, source):
    import shutil
    d = home / "projects" / encode_project_dir(CWD)
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, d / f"{sid}.jsonl")
    return d / f"{sid}.jsonl"


def _continuation_setup(ledger, human, settings, tmp_path, *, marker=True):
    home, s, p = setup_with_import(ledger, human, settings, tmp_path, claude={"picked": True, "since": "all"})
    orig = _install(home, ORIG, FIXTURES / "continued_original.jsonl")
    _install(home, CONT, FIXTURES / "continued_continuation.jsonl")
    if not marker:
        kept = [l for l in orig.read_text().splitlines() if '"continued-in"' not in l]
        orig.write_text("\n".join(kept) + "\n")
    return home, s, p


def _by_session(ledger):
    out = {}
    for row in list_signals(ledger.conn, kinds=["session"]):
        out.setdefault(row.payload["session_id"], []).append(row)
    return out


def _assert_split(ledger):
    by = _by_session(ledger)
    assert sum(len(v) for v in by.values()) == 3
    (cont,) = by[CONT]
    assert cont.t_start == datetime(2026, 9, 29, 16, 9, 30, tzinfo=UTC) and cont.t_end == datetime(2026, 9, 29, 16, 13, tzinfo=UTC)
    assert (cont.payload["prompts"], cont.payload["records"], cont.payload["resumed_from"]) == (2, 4, ORIG)
    assert [(r.t_start.day, r.payload["resumed_from"]) for r in by[ORIG]] == [(30, None), (22, None)]


def test_continuation_skips_copied_records_via_marker(ledger, human, settings, tmp_path):
    from trajectree.ingest.job import run_ingest
    home, s, p = _continuation_setup(ledger, human, settings, tmp_path)
    run_ingest(ledger, s, now=datetime(2026, 10, 25, tzinfo=UTC))
    _assert_split(ledger)


def test_continuation_detected_without_marker_by_first_uuid(ledger, human, settings, tmp_path):
    from trajectree.ingest.job import run_ingest
    home, s, p = _continuation_setup(ledger, human, settings, tmp_path, marker=False)
    run_ingest(ledger, s, now=datetime(2026, 10, 25, tzinfo=UTC))
    _assert_split(ledger)


def test_chain_of_continuations_counts_each_record_once(ledger, human, settings, tmp_path):
    from trajectree.ingest.job import run_ingest
    home, s, p = _continuation_setup(ledger, human, settings, tmp_path)
    cont_path = home / "projects" / encode_project_dir(CWD) / f"{CONT}.jsonl"
    lines = cont_path.read_text().splitlines()
    marker = {"type": "continued-in", "timestamp": "2026-09-29T16:13:30.000Z", "sessionId": CONT, "continuedInSessionId": THIRD}
    cont_path.write_text("\n".join(lines + [json.dumps(marker)]) + "\n")
    copied = [l.replace(CONT, THIRD) for l in lines if '"uuid"' in l]
    own = [dict(rec_user("2026-10-01T09:00:00.000Z", sid=THIRD), uuid="u15"), dict(rec_assistant("2026-10-01T09:01:00.000Z", sid=THIRD), uuid="u16")]
    (cont_path.parent / f"{THIRD}.jsonl").write_text("\n".join(copied + [json.dumps(r) for r in own]) + "\n")
    run_ingest(ledger, s, now=datetime(2026, 10, 25, tzinfo=UTC))
    by = _by_session(ledger)
    (third,) = by[THIRD]
    assert (third.payload["records"], third.payload["resumed_from"]) == (2, CONT)
    assert [r.payload["resumed_from"] for r in by[CONT]] == [ORIG]
    # distinct prompt records: u1 u3 u6 u8 (original), u11 u13 (continuation), u15 (third)
    assert sum(r.payload["prompts"] for rows in by.values() for r in rows) == 7


def test_continuation_state_survives_a_restart(ledger, human, settings, tmp_path):
    from trajectree.ingest.job import run_ingest
    home, s, p = _continuation_setup(ledger, human, settings, tmp_path)
    now = datetime(2026, 10, 25, tzinfo=UTC)
    run_ingest(ledger, s, now=now)
    before = [(r.dedupe_key, r.payload) for r in list_signals(ledger.conn, kinds=["session"])]
    report = run_ingest(ledger, s, now=now)
    assert report.scan.files_read == 0
    assert [(r.dedupe_key, r.payload) for r in list_signals(ledger.conn, kinds=["session"])] == before


def test_resolve_continuations_is_pure_and_maps_by_marker_or_first_uuid():
    from trajectree.connectors.claude_sessions import resolve_continuations
    def sm(sid, end, **kw):
        return SessionSummary(transcript_path=f"/x/{sid}.jsonl", backend="claude", session_id=sid, t_end=datetime(2026, 9, end, tzinfo=UTC), **kw)
    a, b, c = sm("a", 1, continued_in="b", first_uuid="u1"), sm("b", 2, first_uuid="u1"), sm("c", 3, first_uuid="u1")
    got = resolve_continuations([a, b, c])
    assert got == {"/x/b.jsonl": ("a", a.t_end), "/x/c.jsonl": ("b", b.t_end)}
    other_dir = SessionSummary(transcript_path="/y/d.jsonl", backend="claude", session_id="d", t_end=a.t_end, first_uuid="u1")
    assert resolve_continuations([a, other_dir]) == {}
