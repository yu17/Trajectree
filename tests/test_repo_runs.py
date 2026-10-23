from datetime import datetime, timezone
import pytest
from trajectree.models import Caps, Proposal
from trajectree.repo.caps import get_caps, set_caps
from trajectree.repo.runs import create_run, get_run, list_runs, oldest_queued, spent_usd, update_run, usage_by, usage_days
from trajectree.timeutil import parse_iso
UTC = timezone.utc

def test_create_update_and_list_runs(conn):
    r = create_run(conn, kind="ask", runner="claude", backend="claude", model=None, project_id=None)
    assert r.status == "queued" and r.proposals == [] and get_run(conn, r.id).id == r.id and oldest_queued(conn).id == r.id
    p = Proposal(kind="todo", project="a", title="Email Bob", reason="follow-up owed")
    r2 = update_run(conn, r.id, status="ok", est_usd=0.25, started=datetime(2026, 1, 5, 10, tzinfo=UTC), proposals=[p])
    assert r2.est_usd == 0.25 and r2.proposals[0].title == "Email Bob" and r2.proposals[0].applied_txn is None
    assert oldest_queued(conn) is None
    assert [x.id for x in list_runs(conn, with_open_proposals=True)] == [r.id]
    update_run(conn, r.id, proposals=[p.model_copy(update={"applied_txn": "t1"})])
    assert list_runs(conn, with_open_proposals=True) == []
    assert list_runs(conn, kind="summarize") == [] and list_runs(conn, status="ok")[0].id == r.id

def test_spent_and_usage(conn):
    for i, (day, usd) in enumerate([("2026-01-05", 0.5), ("2026-01-05", 0.25), ("2026-01-04", 1.0)]):
        r = create_run(conn, kind="ask" if i else "summarize", runner="claude", backend="claude", model=None, project_id=None)
        update_run(conn, r.id, status="ok", est_usd=usd, tokens_in=10, tokens_out=5, started=parse_iso(day + "T12:00:00Z"))
    assert spent_usd(conn, day="2026-01-05") == 0.75 and spent_usd(conn, day="2026-01-06") == 0.0
    days = usage_days(conn, days=3, now=datetime(2026, 1, 6, 8, tzinfo=UTC))
    assert [(d.day, d.est_usd, d.runs) for d in days] == [("2026-01-04", 1.0, 1), ("2026-01-05", 0.75, 2), ("2026-01-06", 0.0, 0)]
    by_kind = {s.key: s.est_usd for s in usage_by(conn, field="kind", since=datetime(2026, 1, 1, tzinfo=UTC))}
    assert by_kind == {"summarize": 0.5, "ask": 1.25}

def test_caps_defaults_and_roundtrip(conn):
    assert get_caps(conn) == Caps()
    set_caps(conn, Caps(usd_per_day=2.0, paused=True))
    assert get_caps(conn).usd_per_day == 2.0 and get_caps(conn).paused is True
    conn.execute("update setting set value = 'nonsense' where key = 'caps'")
    assert get_caps(conn) == Caps()

def test_parse_iso():
    assert parse_iso("2026-01-05T10:00:00Z") == datetime(2026, 1, 5, 10, tzinfo=UTC)
    for bad in ("2026-01-05T10:00:00", "yesterday", ""):
        with pytest.raises(ValueError):
            parse_iso(bad)

def test_get_caps_keeps_paused_when_invalid(conn, caplog):
    import json
    conn.execute("INSERT INTO setting (key, value) VALUES ('caps', ?)", (json.dumps({"usd_per_run": 50, "usd_per_day": 5, "paused": True}),))
    with caplog.at_level("WARNING", logger="trajectree.caps"):
        caps = get_caps(conn)
    assert caps.paused is True and caps.usd_per_run == Caps().usd_per_run
    assert any(r.name == "trajectree.caps" for r in caplog.records)


def test_oldest_queued_by_slot_and_spent_by_routine(conn):
    NOW = datetime(2026, 1, 5, 10, tzinfo=UTC)
    a = create_run(conn, kind="ask", runner="claude", backend="claude", model=None, project_id=None)
    b = create_run(conn, kind="routine", runner="claude", backend="claude", model=None, project_id=None, routine_id="r1", slot="scheduled")
    assert oldest_queued(conn, slot="scheduled").id == b.id and oldest_queued(conn, slot="interactive").id == a.id
    assert oldest_queued(conn).id == a.id and b.slot == "scheduled" and b.routine_id == "r1" and b.attempt == 1
    update_run(conn, b.id, est_usd=0.5, started=NOW)
    update_run(conn, a.id, est_usd=0.25, started=NOW)
    assert spent_usd(conn, day=NOW.strftime("%Y-%m-%d"), routine_id="r1") == 0.5
    assert spent_usd(conn, day=NOW.strftime("%Y-%m-%d")) == 0.75
    assert [r.id for r in list_runs(conn, routine_id="r1")] == [b.id]
    by = {s.key: s.est_usd for s in usage_by(conn, field="routine_id", since=datetime(2026, 1, 1, tzinfo=UTC))}
    assert by["r1"] == 0.5


def test_failed_retry_candidates(conn):
    from trajectree.repo.runs import failed_retry_candidates
    now = datetime(2026, 1, 5, 10, tzinfo=UTC)

    def mk(status="failed", kind="routine", **props):
        r = create_run(conn, kind=kind, runner="claude", backend="claude", model=None, project_id=None, routine_id="r1", slot="scheduled")
        return update_run(conn, r.id, status=status, props=props)

    ok = mk(failure="infra", retry_at="2026-01-05T09:00:00Z")
    mk(failure="infra", retry_at="2026-01-05T11:00:00Z")  # not yet
    mk(failure="infra", retry_at="2026-01-05T09:00:00Z", retried="x")  # already retried
    mk(failure="logic", retry_at="2026-01-05T09:00:00Z")  # not infra
    mk(status="ok", failure="infra", retry_at="2026-01-05T09:00:00Z")
    mk(kind="ask", failure="infra", retry_at="2026-01-05T09:00:00Z")
    mk(failure="infra")  # no retry_at
    assert [r.id for r in failed_retry_candidates(conn, now=now)] == [ok.id]
