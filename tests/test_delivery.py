from datetime import datetime, timezone

from trajectree.delivery import DeliveryMessage, deliver
from trajectree.models import ProjectIn
from trajectree.repo.events import get_event, list_events
from trajectree.repo.projects import create_project

NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


def _msg(project_id=None):
    return DeliveryMessage(title="t", body_md="b", project_id=project_id, run_id="r", routine="x")


def test_deliver_inbox_falls_back_to_log_without_project(ledger, settings, agent):
    res = deliver(ledger, settings, "inbox", DeliveryMessage(title="t", body_md="b", project_id=None, run_id="r", routine="x"), actor=agent, now=NOW)
    assert res.channel == "log" and res.ok and "no project" in res.detail
    assert ledger.list_txns() == []


def test_deliver_inbox_creates_pending_note(ledger, settings, agent, human):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    res = deliver(ledger, settings, "inbox", _msg(a.id), actor=agent, now=NOW)
    assert res.channel == "inbox" and res.ok and res.event_id
    ev = get_event(ledger.conn, res.event_id)
    assert (ev.project_id, ev.kind, ev.title, ev.body_md, ev.source, ev.status) == (a.id, "note", "t", "b", "agent", "happened")
    assert ev.t_start == NOW
    txn = ledger.get_txn(ev.created_txn)
    assert txn.review == "pending" and txn.intent == "deliver x result"


def test_deliver_inbox_uses_inbox_project_when_message_has_none(ledger, settings, agent, human):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    inbox = create_project(ledger, human, ProjectIn(slug="inbox", title="Inbox", kind="inbox"))
    res = deliver(ledger, settings, "inbox", _msg(), actor=agent, now=NOW)
    assert res.channel == "inbox" and get_event(ledger.conn, res.event_id).project_id == inbox.id


def test_deliver_chatmaid_unconfigured_falls_back(ledger, settings, agent, human):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    posted = []
    res = deliver(ledger, settings, "chatmaid", _msg(a.id), actor=agent, now=NOW, post=lambda *a, **k: posted.append(a))
    assert res.channel == "inbox" and res.ok and "chatmaid" in res.detail and res.event_id
    assert posted == [] and len(list_events(ledger.conn, project_ids=[a.id])) == 1


def test_deliver_none_and_log_write_nothing(ledger, settings, agent, human, caplog):
    import logging
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    n = len(ledger.list_txns())
    assert deliver(ledger, settings, "none", _msg(a.id), actor=agent, now=NOW).model_dump() == {
        "channel": "none", "ok": True, "detail": None, "event_id": None}
    with caplog.at_level(logging.INFO, logger="trajectree.delivery"):
        res = deliver(ledger, settings, "log", _msg(a.id), actor=agent, now=NOW)
    assert res.channel == "log" and res.ok and "x" in caplog.text
    assert len(ledger.list_txns()) == n
