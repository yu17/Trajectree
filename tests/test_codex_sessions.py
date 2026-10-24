import json
import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tests.test_catalog import MODEL, seed_catalog
from trajectree.connectors import probe as probe_mod
from trajectree.connectors.claude_sessions import SessionSummary
from trajectree.connectors.codex_sessions import (
    digest_codex,
    parse_codex_lines,
    project_codex_sessions,
    scan_codex,
    session_signal,
)
from trajectree.connectors.probe import ProbeState, probe_folder
from trajectree.context.history import session_history
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.runs import get_run
from trajectree.repo.signals import list_signals
from trajectree.repo.state import get_state
from trajectree.runner.kinds import SummarizeSessionRequest, ensure_prompts, plan_run
from trajectree.runner.service import RunService

UTC = timezone.utc
CWD = "/home/u/proj"
SID = "019aaaaa-0000-7000-8000-000000000001"
FIXTURES = Path(__file__).parent / "fixtures" / "codex"
BASIC = FIXTURES / f"rollout-2026-01-05T10-00-00-{SID}-basic.jsonl"
TRUNCATED = FIXTURES / f"rollout-2026-01-05T10-00-00-{SID}-truncated.jsonl"
UNKNOWN = FIXTURES / f"rollout-2026-01-05T10-00-00-{SID}-unknown.jsonl"


def install(settings, tmp_path, fixture: Path):
    """Copy `fixture` under a Codex sessions root; returns the settings using it and the copy."""
    root = tmp_path / "codex-sessions"
    day = root / "2026" / "01" / "05"
    day.mkdir(parents=True, exist_ok=True)
    dest = day / fixture.name
    shutil.copy(fixture, dest)
    # a long window keeps these 2026-01 fixtures inside the default cutoff whenever the suite runs
    return settings.model_copy(update={"codex_sessions_root": root, "scan_since_days": 20000}), dest


def rollout(root: Path, sid: str, cwd: str, ts: str, text: str) -> Path:
    day = root / ts[:4] / ts[5:7] / ts[8:10]
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-{ts[:19].replace(':', '-')}-{sid}.jsonl"
    meta = {"timestamp": ts, "type": "session_meta", "payload": {"id": sid, "cwd": cwd}}
    prompt = {"timestamp": ts, "type": "event_msg", "payload": {
        "type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]}}}
    path.write_text("\n".join(json.dumps(r) for r in (meta, prompt)) + "\n")
    return path


def project(ledger, human, **props):
    p = create_project(ledger, human, ProjectIn(slug="proj", title="Proj", kind="research", props=props))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=CWD))
    return p


def import_props(**sources):
    return {"import": {"sources": sources, "recorded_at": "2026-10-01T00:00:00+00:00"}}


def nothing(path):
    return ProbeState()


def test_session_from_meta():
    acc = SessionSummary(transcript_path=str(BASIC), backend="codex")
    out = parse_codex_lines(BASIC.read_bytes().splitlines(keepends=True), acc)
    assert (out.session_id, out.cwd, out.model) == (SID, CWD, "gpt-test-1")
    assert out.t_start == datetime(2026, 1, 5, 10, tzinfo=UTC) and out.t_end == datetime(2026, 1, 5, 10, 0, 10, tzinfo=UTC)
    # the injected response_item copy of a user turn is not a prompt; usage is summed per response
    assert (out.prompts, out.assistant_msgs, out.tokens_in, out.tokens_out) == (1, 1, 150, 25)


def test_session_id_falls_back_to_file_name():
    acc = SessionSummary(transcript_path=str(BASIC), backend="codex")
    out = parse_codex_lines([b'{"type": "turn_context", "payload": {"cwd": "/x"}}\n'], acc)
    assert out.session_id == SID and out.cwd == "/x"


def test_title_is_first_prompt_shortened():
    long = "word " * 40
    acc = SessionSummary(transcript_path="x", backend="codex")
    line = json.dumps({"type": "event_msg", "payload": {
        "type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": long}]}}})
    out = parse_codex_lines([line.encode(), line.replace("word", "later").encode()], acc)
    assert len(out.title) == 80 and out.title.endswith("…") and out.title.startswith("word word")
    short = parse_codex_lines(BASIC.read_bytes().splitlines(keepends=True), acc)
    assert short.title == "Fix the build script"


def test_truncated_line_is_kept_for_next_pass(ledger, human, settings, tmp_path):
    s, dest = install(settings, tmp_path, TRUNCATED)
    p = project(ledger, human)
    scan_codex(ledger.conn, s, now=datetime.now(UTC))
    body = dest.read_bytes()
    state = get_state(ledger.conn, f"scan:{dest.absolute()}")
    assert state["offset"] == body.rindex(b"\n") + 1 < len(body)  # stops before the partial line
    first = list_signals(ledger.conn, project_ids=[p.id])[0]
    assert first.t_end == datetime(2026, 1, 5, 10, 0, 8, tzinfo=UTC)
    # the rest of the line arrives
    dest.write_bytes(BASIC.read_bytes())
    scan_codex(ledger.conn, s, now=datetime.now(UTC))
    (full,) = list_signals(ledger.conn, project_ids=[p.id])
    assert full.t_end == datetime(2026, 1, 5, 10, 0, 10, tzinfo=UTC)
    assert (full.payload["tokens_in"], full.payload["tokens_out"], full.payload["prompts"]) == (150, 25, 1)


def test_unknown_records_ignored():
    acc = SessionSummary(transcript_path=str(BASIC), backend="codex")
    base = parse_codex_lines(BASIC.read_bytes().splitlines(keepends=True), acc)
    assert parse_codex_lines(UNKNOWN.read_bytes().splitlines(keepends=True), acc) == base
    # the timestamp of an unknown record still counts, so t_end is unchanged but the digest is too
    assert digest_codex(UNKNOWN) == digest_codex(BASIC)


def test_signal_attaches_by_cwd_and_honours_codex_choice(ledger, human, settings, tmp_path):
    s, dest = install(settings, tmp_path, BASIC)
    other = rollout(s.codex_sessions_root, "019bbbbb-0000-7000-8000-000000000002", "/home/u/elsewhere", "2026-01-06T10:00:00.000Z", "x")
    p = project(ledger, human)
    report = scan_codex(ledger.conn, s, now=datetime.now(UTC))
    assert (report.files_seen, report.signals_written) == (2, 1) and other.exists()
    (sig,) = list_signals(ledger.conn, project_ids=[p.id])
    assert sig.kind == "session" and sig.dedupe_key == f"codex_session:{SID}"
    assert sig.payload["backend"] == "codex" and sig.payload["session_id"] == SID
    assert sig.payload["title"] == "Fix the build script" and sig.payload["transcript_path"] == str(dest.absolute())
    assert session_signal(ledger.conn, SID).id == sig.id and session_signal(ledger.conn, "nope") is None


def test_unpicked_and_early_codex_sessions_not_attached(ledger, human, settings, tmp_path):
    s, _ = install(settings, tmp_path, BASIC)
    p = project(ledger, human, **import_props(claude={"picked": True}, codex={"picked": False}))
    assert scan_codex(ledger.conn, s, now=datetime.now(UTC)).signals_written == 0
    from trajectree.models import ProjectPatch
    from trajectree.repo.projects import update_project
    update_project(ledger, human, p.id, ProjectPatch(props=import_props(codex={"picked": True, "since": "2026-05-01"})))
    assert scan_codex(ledger.conn, s, now=datetime.now(UTC)).signals_written == 0
    update_project(ledger, human, p.id, ProjectPatch(props=import_props(codex={"picked": True, "since": "all"})))
    assert scan_codex(ledger.conn, s, now=datetime.now(UTC)).signals_written == 1


def test_codex_cutoff_uses_earliest_codex_start(ledger, human, settings, tmp_path):
    s, dest = install(settings, tmp_path, BASIC)
    s = s.model_copy(update={"scan_since_days": 365})
    old = datetime(2024, 8, 1, tzinfo=UTC).timestamp()
    os.utime(dest, (old, old))
    project(ledger, human)
    assert scan_codex(ledger.conn, s, now=datetime.now(UTC)).files_seen == 0
    deep = create_project(ledger, human, ProjectIn(slug="deep", title="Deep", kind="research", props=import_props(codex={"picked": True, "since": "all"})))
    upsert_resource(ledger, human, ResourceIn(project_id=deep.id, kind="folder", locator="/home/u/deep"))
    assert scan_codex(ledger.conn, s, now=datetime.now(UTC)).files_seen == 1


def test_live_by_mtime(ledger, human, settings, tmp_path):
    s, _ = install(settings, tmp_path, BASIC)
    project(ledger, human)
    assert [x.session_id for x in scan_codex(ledger.conn, s, now=datetime.now(UTC)).live] == [SID]
    later = datetime.now(UTC).replace(year=datetime.now(UTC).year + 1)
    assert scan_codex(ledger.conn, s, now=later).live == []


def test_digest_codex_caps_length():
    digest = digest_codex(BASIC, max_chars=12000)
    assert "## Prompts\n1. Fix the build script" in digest and "shell × 1" in digest
    assert "I fixed the build." in digest and "## Final message\nBuild fixed" in digest
    assert "injected" not in digest
    small = digest_codex(BASIC, max_chars=60)
    assert len(small) <= 60 and small.endswith("(digest truncated)")
    with pytest.raises(OSError):
        digest_codex(BASIC.parent / "nope.jsonl")


def test_summarize_codex_session(ledger, human, settings, logged_in, tmp_path):
    seed_catalog(ledger)
    s, _ = install(settings, tmp_path, BASIC)
    p = project(ledger, human)
    scan_codex(ledger.conn, s, now=datetime.now(UTC))
    ensure_prompts(s)
    run = RunService(ledger, s, base_env={}).enqueue(SummarizeSessionRequest(model=MODEL, session_id=SID))
    assert get_run(ledger.conn, run.id).project_id == p.id
    plan = plan_run(ledger.conn, s, run=run, request=SummarizeSessionRequest(model=MODEL, session_id=SID), now=datetime.now(UTC))
    assert "Fix the build script" in plan.prompt and "I fixed the build." in plan.prompt and "shell × 1" in plan.prompt


def test_probe_counts_codex_sessions(settings, tmp_path, monkeypatch):
    s, _ = install(settings, tmp_path, BASIC)
    rollout(s.codex_sessions_root, "019bbbbb-0000-7000-8000-000000000002", CWD + "/sub", "2026-01-03T09:00:00.000Z", "x")
    elsewhere = rollout(s.codex_sessions_root, "019ccccc-0000-7000-8000-000000000003", "/elsewhere", "2026-01-02T09:00:00.000Z", "x")
    os.utime(elsewhere, (1000, 1000))  # oldest file: the cap of one leaves it out
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    codex = {x.source: x for x in probe_folder(nothing, s, CWD, now=now).sources}["codex"]
    assert codex.count == 2 and codex.oldest == datetime(2026, 1, 3, 9, tzinfo=UTC) and codex.detail == ""
    real = probe_mod.project_codex_sessions
    monkeypatch.setattr(probe_mod, "project_codex_sessions", lambda *a, **k: real(*a, **k, max_files=1))
    capped = {x.source: x for x in probe_folder(nothing, s, CWD, now=now).sources}["codex"]
    assert capped.count == 1 and capped.detail == "at least 1 sessions"
    off = {x.source: x for x in probe_folder(nothing, settings, CWD, now=now).sources}["codex"]
    assert (off.count, off.detail) == (0, "not connected")


def test_project_codex_sessions_deepest_folder_and_source_filter(settings, tmp_path):
    s, _ = install(settings, tmp_path, BASIC)
    rollout(s.codex_sessions_root, "019bbbbb-0000-7000-8000-000000000002", CWD + "-android", "2026-01-03T09:00:00.000Z", "x")
    rollout(s.codex_sessions_root, "019ccccc-0000-7000-8000-000000000003", CWD + "/deep", "2026-01-02T09:00:00.000Z", "x")
    got = project_codex_sessions(s, CWD, registered=[CWD, CWD + "/deep"])
    assert [x.session_id for x in got.sessions] == [SID]
    cutoff = datetime(2026, 1, 5, 10, tzinfo=UTC)
    both = project_codex_sessions(s, CWD, registered=[CWD])
    assert [x.session_id for x in both.sessions] == [SID, "019ccccc-0000-7000-8000-000000000003"]
    before = project_codex_sessions(s, CWD, registered=[CWD], before=cutoff)
    assert [x.session_id for x in before.sessions] == ["019ccccc-0000-7000-8000-000000000003"]
    assert project_codex_sessions(s, CWD, registered=[CWD], before_by_source={"claude": cutoff}).sessions == []


def test_history_includes_codex_sessions(settings, tmp_path):
    s, _ = install(settings, tmp_path, BASIC)
    rollout(s.codex_sessions_root, "019bbbbb-0000-7000-8000-000000000002", CWD, "2026-06-01T09:00:00.000Z", "a later one")
    text = session_history(s, [CWD], [CWD], starts={"codex": date(2026, 3, 1)}, budget_chars=5000)
    assert "Fix the build script" in text and "I fixed the build." in text and "a later one" not in text
    assert "Fix the build script (1 prompts)" in text
    assert session_history(s, [CWD], [CWD], starts={"claude": date(2026, 3, 1)}, budget_chars=5000) == ""


def test_no_secret_fields_leak(ledger, human, settings, tmp_path):
    s, dest = install(settings, tmp_path, BASIC)
    p = project(ledger, human)
    scan_codex(ledger.conn, s, now=datetime.now(UTC))
    (sig,) = list_signals(ledger.conn, project_ids=[p.id])
    state = get_state(ledger.conn, f"scan:{dest.absolute()}")
    seen = json.dumps(sig.payload) + json.dumps(state) + digest_codex(dest)
    for secret in ("SECRET-BASE-INSTRUCTIONS", "SECRET-ACCOUNT-ID", "SECRET-USER-ID", "SECRET-TOKEN"):
        assert secret in BASIC.read_text() and secret not in seen


def test_cap_counts_only_this_folders_rollouts(settings, tmp_path, monkeypatch):
    s, _ = install(settings, tmp_path, BASIC)  # the project's older session, mtime now
    old = tmp_path / "codex-sessions" / "2026" / "01" / "05" / f"rollout-2026-01-05T10-00-00-{SID}-basic.jsonl"
    os.utime(old, (1000, 1000))
    for i in range(3):  # newer rollouts elsewhere, more than the cap
        rollout(s.codex_sessions_root, f"019ddddd-0000-7000-8000-00000000000{i}", "/elsewhere", f"2026-02-0{i + 1}T09:00:00.000Z", "x")
    got = project_codex_sessions(s, CWD, registered=[CWD], max_files=2)
    assert [x.session_id for x in got.sessions] == [SID] and got.capped is False
    real = probe_mod.project_codex_sessions
    monkeypatch.setattr(probe_mod, "project_codex_sessions", lambda *a, **k: real(*a, **k, max_files=2))
    probed = {x.source: x for x in probe_folder(nothing, s, CWD, now=datetime(2026, 10, 24, tzinfo=UTC)).sources}["codex"]
    assert probed.count == 1 and probed.detail == ""
    # a file whose first record is not a session_meta is skipped
    bad = s.codex_sessions_root / "2026" / "01" / "05" / "rollout-bad.jsonl"
    bad.write_text('{"type": "turn_context", "payload": {"cwd": "/home/u/proj"}}\n')
    assert [x.session_id for x in project_codex_sessions(s, CWD, registered=[CWD]).sessions] == [SID]
