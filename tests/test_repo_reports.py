import pytest

from trajectree.db import fts
from trajectree.models import ReportIn, ReportPatch
from trajectree.repo import NotFound
from trajectree.repo.reports import create_report, get_report, list_reports, update_report


def test_report_save_is_a_transaction(ledger, human):
    r = create_report(ledger, human, ReportIn(title="Deck", kind="deck", source_md="# Hi"), intent="new report deck")
    r2 = update_report(ledger, human, r.id, ReportPatch(source_md="# Hi\n\n---\n\n# Two"))
    txns = ledger.list_txns(limit=5)
    assert txns[0].intent.startswith("update report") and len([t for t in txns if "report" in t.intent]) == 2
    assert ledger.changes_for(txns[0].id)[0].table_name == "report"
    assert any(h.kind == "report" for h in fts.search(ledger.conn, "Two"))
    assert list_reports(ledger.conn)[0].id == r2.id
    assert get_report(ledger.conn, r.id).source_md.endswith("# Two")


def test_report_scope_roundtrip_and_ordering(ledger, human):
    a = create_report(ledger, human, ReportIn(title="A", kind="memo", scope={"projects": ["p"]}))
    b = create_report(ledger, human, ReportIn(title="B", kind="deck"))
    assert get_report(ledger.conn, a.id).scope.projects == ["p"]
    update_report(ledger, human, a.id, ReportPatch(title="A2"))
    # timestamps have second precision, so pin B to an older time to make the order deterministic
    ledger.conn.execute("UPDATE report SET updated_at = '2020-01-01T00:00:00Z' WHERE id = ?", (b.id,))
    assert [r.title for r in list_reports(ledger.conn)] == ["A2", "B"]
    assert len(list_reports(ledger.conn, limit=1)) == 1
    assert [h.id for h in fts.search(ledger.conn, "A2")] == [a.id]
    fts.rebuild(ledger.conn)
    assert {h.id for h in fts.search(ledger.conn, "B")} == {b.id}


def test_event_auto_accept_flag(ledger, agent):
    from datetime import datetime, timezone
    from trajectree.models import EventIn, EventPatch, ProjectIn
    from trajectree.repo.events import create_event, update_event
    from trajectree.repo.projects import create_project
    p = create_project(ledger, agent, ProjectIn(slug="p", title="P", kind="research"))
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    e = create_event(ledger, agent, EventIn(project_id=p.id, kind="note", title="x", t_start=t))
    assert ledger.list_txns(limit=1)[0].review == "pending"
    e2 = create_event(ledger, agent, EventIn(project_id=p.id, kind="note", title="y", t_start=t), auto_accept=True)
    assert ledger.list_txns(limit=1)[0].review == "accepted"
    update_event(ledger, agent, e.id, EventPatch(title="z"), auto_accept=True)
    assert ledger.list_txns(limit=1)[0].review == "accepted" and e2.title == "y"


def test_update_missing_report(ledger, human):
    with pytest.raises(NotFound):
        update_report(ledger, human, "nope", ReportPatch(title="x"))
