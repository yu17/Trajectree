from datetime import datetime, timedelta, timezone
from trajectree.repo.sessions import end_session, ended_session_ids, running_sessions, session_for_cwd, start_session, touch_session
UTC = timezone.utc
T0 = datetime(2026, 1, 5, 10, tzinfo=UTC)

def test_session_lifecycle(conn):
    s = start_session(conn, session_id="s1", cwd="/p", transcript_path="/t.jsonl", project_id="p1", source="startup", now=T0)
    assert s.started_at == T0 and s.ended_at is None
    touch_session(conn, session_id="s1", cwd="/p", project_id="p1", now=T0 + timedelta(minutes=5))
    assert running_sessions(conn, since=T0)[0].last_seen == T0 + timedelta(minutes=5)
    assert session_for_cwd(conn, cwd="/p", since=T0).id == "s1" and session_for_cwd(conn, cwd="/q", since=T0) is None
    assert end_session(conn, session_id="s1", reason="other", now=T0 + timedelta(minutes=6)).end_reason == "other"
    assert running_sessions(conn, since=T0) == [] and ended_session_ids(conn, ["s1", "zz"]) == {"s1"}
    start_session(conn, session_id="s1", cwd="/p", transcript_path="/t.jsonl", project_id="p1", source="resume", now=T0 + timedelta(hours=1))
    assert running_sessions(conn, since=T0)[0].started_at == T0  # reopened, original start kept
    assert end_session(conn, session_id="ghost", reason="other", now=T0) is None

def test_ended_sessions_since_oldest_first(conn):
    from trajectree.repo.sessions import ended_sessions_since
    for sid, minute in (("late", 30), ("early", 10), ("old", -60)):
        start_session(conn, session_id=sid, cwd="/p", transcript_path=None, project_id="p1", source="startup", now=T0 - timedelta(hours=2))
        end_session(conn, session_id=sid, reason="other", now=T0 + timedelta(minutes=minute))
    start_session(conn, session_id="open", cwd="/p", transcript_path=None, project_id="p1", source="startup", now=T0)
    assert [s.id for s in ended_sessions_since(conn, since=T0)] == ["early", "late"]
    assert [s.id for s in ended_sessions_since(conn, since=T0 + timedelta(minutes=10))] == ["early", "late"]
    assert ended_sessions_since(conn, since=T0 + timedelta(hours=1)) == []

def test_touch_unknown_session_creates_it(conn):
    s = touch_session(conn, session_id="s9", cwd="/x", project_id=None, now=T0)
    assert s.started_at == T0 and s.source == "stop" and s.project_id is None


def test_idle_sessions_lists_only_quiet_open_sessions(conn):
    from trajectree.repo.sessions import idle_sessions
    for sid, minute in (("quiet", 0), ("fresh", 50), ("done", 0)):
        start_session(conn, session_id=sid, cwd="/p", transcript_path=None, project_id="p1", source="startup", now=T0 + timedelta(minutes=minute))
    end_session(conn, session_id="done", reason="other", now=T0 + timedelta(minutes=1))
    assert [s.id for s in idle_sessions(conn, before=T0 + timedelta(minutes=30))] == ["quiet"]
