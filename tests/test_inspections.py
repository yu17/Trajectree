from datetime import datetime, timedelta, timezone

import apsw

from tests.test_repo_signals import sig
from trajectree.context.stretches import merge_stretches
from trajectree.db.schema import MIGRATIONS, migrate
from trajectree.models import ProjectIn, Watermark
from trajectree.repo.inspections import (
    due_projects,
    get_watermark,
    project_is_live,
    set_watermark,
    uninspected_stretches,
)
from trajectree.repo.projects import create_project, get_project
from trajectree.repo.sessions import end_session, start_session
from trajectree.repo.signals import list_signals, upsert_signals
from trajectree.repo.state import get_state

UTC = timezone.utc


def at(day, hour=0, minute=0):
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def seg(pid, day, h1, m1, h2, m2, key):
    return sig(pid, day, key=key).model_copy(update={"t_start": at(day, h1, m1), "t_end": at(day, h2, m2)})


def test_merge_stretches_joins_segments_within_the_gap():
    sigs = [
        seg("p", 1, 10, 0, 10, 30, "s1"),
        seg("p", 1, 11, 0, 11, 10, "s2"),
        sig("p", 1, kind="commits", key="c1", hour=11).model_copy(update={"t_start": at(1, 11, 30), "t_end": None}),
        seg("p", 20, 9, 0, 9, 30, "s3"),
    ]
    rows = _as_signals(sigs)
    out = merge_stretches(rows, gap_s=7200)
    assert len(out) == 2
    first = out[0]
    assert (first.segments, first.sessions, first.commits) == (3, 2, 1)
    assert first.t_end == at(1, 11, 30) and first.inspected_up_to is None
    assert out[1].sessions == 1 and out[1].signal_ids != first.signal_ids


def test_merge_stretches_marks_partial_inspection():
    rows = _as_signals([seg("p", 1, 10, 0, 10, 30, "s1"), seg("p", 1, 11, 0, 11, 10, "s2"), seg("p", 20, 9, 0, 9, 30, "s3")])
    out = merge_stretches(rows, gap_s=7200, inspected_at=at(1, 10, 45))
    assert out[0].inspected_up_to == at(1, 10, 45) and out[1].inspected_up_to is None


def _as_signals(ins):
    from trajectree.models import Signal

    return [Signal(id=f"id-{i.dedupe_key}", updated_at=at(1), **i.model_dump()) for i in ins]


def test_upsert_signals_counts_only_changes(conn):
    assert upsert_signals(conn, [sig("p1", 5), sig("p1", 6)]) == 2
    conn.execute("UPDATE signal SET updated_at = '2000-01-01T00:00:00Z'")
    assert upsert_signals(conn, [sig("p1", 5), sig("p1", 6)]) == 0
    assert {r.updated_at.year for r in list_signals(conn)} == {2000}
    changed = sig("p1", 6).model_copy(update={"payload": {"n": 60}})
    assert upsert_signals(conn, [sig("p1", 5), changed]) == 1
    by_key = {r.dedupe_key: r for r in list_signals(conn)}
    assert by_key["session:p1:5"].updated_at.year == 2000
    assert by_key["session:p1:6"].updated_at.year >= 2026


def test_uninspected_stretches_use_end_and_change_time(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    conn = ledger.conn
    upsert_signals(conn, [
        seg(p.id, 5, 10, 0, 10, 30, "old"),
        seg(p.id, 12, 10, 0, 10, 30, "new"),
        seg(p.id, 20, 10, 0, 10, 30, "touched"),
    ])
    conn.execute("UPDATE signal SET updated_at = '2026-01-01T00:00:00Z'")
    conn.execute("UPDATE signal SET updated_at = '2026-02-01T00:00:00Z' WHERE dedupe_key = 'touched'")
    set_watermark(conn, p.id, Watermark(inspected_at=at(10), started_at=datetime(2026, 1, 25, tzinfo=UTC)))
    # day 5 ended before the mark and was not changed since: excluded; 12 and 20 (end > mark) included
    now = at(28)
    assert [s.t_start.day for s in uninspected_stretches(conn, settings, p, now=now)] == [12, 20]
    # a stretch ending before the mark but changed after `started_at` is included
    conn.execute("UPDATE signal SET updated_at = '2026-02-01T00:00:00Z' WHERE dedupe_key = 'old'")
    assert [s.t_start.day for s in uninspected_stretches(conn, settings, p, now=now)] == [5, 12, 20]
    assert get_watermark(conn, p.id).inspected_at == at(10)


def test_due_requires_settlement(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_signals(ledger.conn, [seg(p.id, 5, 10, 0, 10, 30, "s")])
    end = at(5, 10, 30)
    assert due_projects(ledger.conn, settings, now=end + timedelta(hours=1)) == []
    due = due_projects(ledger.conn, settings, now=end + timedelta(hours=3))
    assert len(due) == 1
    d = due[0]
    assert (d.project_id, d.slug, d.stretches, d.sessions) == (p.id, "a", 1, 1)
    assert (d.first_day, d.last_day) == ("2026-01-05", "2026-01-05")


def test_live_project_is_not_due(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_signals(ledger.conn, [seg(p.id, 5, 10, 0, 10, 30, "s")])
    now = at(6)
    start_session(ledger.conn, session_id="x", cwd="/w/a", transcript_path=None, project_id=p.id, source="hook", now=now)
    assert project_is_live(ledger.conn, settings, p.id, now=now)
    assert due_projects(ledger.conn, settings, now=now) == []
    end_session(ledger.conn, session_id="x", reason="done", now=now)
    assert not project_is_live(ledger.conn, settings, p.id, now=now)
    assert [d.project_id for d in due_projects(ledger.conn, settings, now=now)] == [p.id]


def test_watermark_does_not_touch_the_ledger(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    before = ledger.conn.execute("SELECT count(*) FROM txn").fetchone()[0]
    set_watermark(ledger.conn, p.id, Watermark(inspected_at=at(3)))
    assert ledger.conn.execute("SELECT count(*) FROM txn").fetchone()[0] == before
    assert get_state(ledger.conn, f"inspect:{p.id}")["inspected_at"].startswith("2026-01-03")
    assert get_project(ledger.conn, p.id).id == p.id


def test_schema_v5_adds_tier_and_purges_session_signals(tmp_path):
    conn = apsw.Connection(str(tmp_path / "old.db"))
    for version, statements in MIGRATIONS[:4]:
        with conn:
            for s in statements:
                conn.execute(s)
            conn.execute(f"PRAGMA user_version = {version}")
    conn.execute(
        "INSERT INTO signal (id, project_id, kind, t_start, payload, dedupe_key, updated_at)"
        " VALUES ('a', 'p', 'session', '2026-01-01T00:00:00Z', '{}', 'claude_session:x', '2026-01-01T00:00:00Z'),"
        " ('b', 'p', 'commits', '2026-01-01T00:00:00Z', '{}', 'git:r', '2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO state (key, value, updated_at) VALUES ('scan:/x', '{}', 'z'), ('keep', '1', 'z')")
    conn.execute(
        "INSERT INTO project (id, slug, title, kind, created_at, updated_at) VALUES ('p', 'p', 'P', 'research', 'z', 'z')"
    )
    for i, kind in enumerate(("idea", "note", "milestone")):
        conn.execute(
            "INSERT INTO event (id, project_id, kind, title, t_start, created_at, updated_at)"
            f" VALUES ('e{i}', 'p', '{kind}', 't', 'z', 'z', 'z')"
        )
    migrate(conn)
    assert [r[0] for r in conn.execute("SELECT kind FROM signal")] == ["commits"]
    assert [r[0] for r in conn.execute("SELECT key FROM state")] == ["keep"]
    assert "tier" in [r[1] for r in conn.execute("PRAGMA table_info(event)")]
    tiers = dict(conn.execute("SELECT kind, tier FROM event"))
    assert tiers == {"idea": "pill", "note": "pill", "milestone": "block"}
    conn.close()
