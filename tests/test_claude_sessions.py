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
    assert sig.dedupe_key == f"claude_session:{'4' * 36}" and sig.payload["session_id"] == "4" * 36

def test_scan_maps_sessions_and_skips_unmapped(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z"), rec_assistant("2026-01-05T10:02:00.000Z"), rec_user("2026-01-05T10:03:00.000Z")])
    write_transcript(home, "/elsewhere", "2" * 36, [rec_user("2026-01-05T10:00:00.000Z", cwd="/elsewhere", sid="2" * 36)])
    now = datetime.now(UTC)
    report = scan_transcripts(ledger.conn, s, now=now)
    assert report.files_seen == 2 and report.files_read == 2 and report.signals_written == 1
    sig = list_signals(ledger.conn, project_ids=[p.id])[0]
    assert sig.kind == "session" and sig.dedupe_key == f"claude_session:{SID}"
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
