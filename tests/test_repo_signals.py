from datetime import datetime, timedelta, timezone
from trajectree.models import ProjectIn, ProjectPatch, SignalIn
from trajectree.repo.projects import create_project, update_project
from trajectree.repo.signals import latest_signals, list_signals, strip_bins, upsert_signals
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import utcnow

UTC = timezone.utc

def sig(pid, day, kind="session", key=None, hour=10):
    t = datetime(2026, 1, day, hour, tzinfo=UTC)
    return SignalIn(project_id=pid, kind=kind, t_start=t, t_end=t + timedelta(hours=1),
                    payload={"n": day}, dedupe_key=key or f"{kind}:{pid}:{day}")

def test_upsert_is_idempotent_and_updates_payload(conn):
    assert upsert_signals(conn, [sig("p1", 5), sig("p1", 6)]) == 2
    again = sig("p1", 5).model_copy(update={"payload": {"n": 50}})
    upsert_signals(conn, [again])
    rows = list_signals(conn, project_ids=["p1"])
    assert len(rows) == 2 and rows[0].payload == {"n": 50} and rows[0].t_start < rows[1].t_start

def test_list_signals_window_kinds_and_limit(conn):
    upsert_signals(conn, [sig("p1", 5), sig("p1", 20, kind="commits")])
    rows = list_signals(conn, project_ids=["p1"], t_from=datetime(2026, 1, 10, tzinfo=UTC), t_to=datetime(2026, 1, 31, tzinfo=UTC))
    assert [r.kind for r in rows] == ["commits"]
    assert list_signals(conn, kinds=["session"])[0].dedupe_key == "session:p1:5"
    assert len(list_signals(conn, limit=1)) == 1

def test_strip_bins_group_by_day_and_kind(conn):
    upsert_signals(conn, [sig("p1", 5), sig("p1", 5, key="x", hour=15), sig("p1", 5, kind="commits"), sig("p2", 5)])
    bins = strip_bins(conn, project_ids=["p1"], t_from=datetime(2026, 1, 1, tzinfo=UTC), t_to=datetime(2026, 2, 1, tzinfo=UTC))
    assert sorted((b.kind, b.count) for b in bins) == [("commits", 1), ("session", 2)]
    assert all(b.day == "2026-01-05" and b.project_id == "p1" for b in bins)
    assert len(strip_bins(conn, project_ids=None, t_from=datetime(2026, 1, 1, tzinfo=UTC), t_to=datetime(2026, 2, 1, tzinfo=UTC))) == 3

def test_strip_bins_sum_commit_counts(conn):
    a = sig("p1", 5, kind="commits", key="git:r1:2026-01-05").model_copy(update={"payload": {"count": 5}})
    b = sig("p1", 5, kind="commits", key="git:r2:2026-01-05", hour=15).model_copy(update={"payload": {"count": 3}})
    upsert_signals(conn, [a, b])
    bins = strip_bins(conn, project_ids=["p1"], t_from=datetime(2026, 1, 1, tzinfo=UTC), t_to=datetime(2026, 2, 1, tzinfo=UTC))
    assert [(b.day, b.kind, b.count) for b in bins] == [("2026-01-05", "commits", 8)]

def test_state_round_trip(conn):
    assert get_state(conn, "k") is None
    set_state(conn, "k", {"offset": 10})
    set_state(conn, "k", {"offset": 20})
    assert get_state(conn, "k") == {"offset": 20}

def test_summary_write_stamps_summary_at(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    assert p.summary_at is None
    p2 = update_project(ledger, human, p.id, ProjectPatch(summary_md="hello"))
    assert p2.summary_at is not None
    p3 = update_project(ledger, human, p.id, ProjectPatch(title="A2"))
    assert p3.summary_at == p2.summary_at


def test_latest_signals_returns_newest_in_ascending_order(conn):
    upsert_signals(conn, [sig("p1", d) for d in range(1, 13)])
    rows = latest_signals(conn, project_id="p1", t_from=None, t_to=None, n=10)
    assert len(rows) == 10 and rows[0].t_start.day == 3 and rows[-1].t_start.day == 12

def test_strip_bins_tolerate_non_numeric_counts(conn):
    upsert_signals(conn, [SignalIn(project_id="p", kind="commits", t_start=datetime(2026, 1, 5, tzinfo=UTC), dedupe_key="a", payload={"count": "many"}),
                          SignalIn(project_id="p", kind="commits", t_start=datetime(2026, 1, 5, 1, tzinfo=UTC), dedupe_key="b", payload={"count": 2})])
    bins = strip_bins(conn, project_ids=["p"], t_from=datetime(2026, 1, 1, tzinfo=UTC), t_to=datetime(2026, 2, 1, tzinfo=UTC))
    assert bins[0].count == 3

def test_get_signal_by_key(conn):
    from trajectree.repo.signals import get_signal_by_key
    upsert_signals(conn, [SignalIn(project_id="p", kind="remote", t_start=datetime(2026, 1, 5, tzinfo=UTC), dedupe_key="k1", payload={"a": 1})])
    got = get_signal_by_key(conn, "k1")
    assert got.kind == "remote" and got.payload == {"a": 1} and get_signal_by_key(conn, "nope") is None
