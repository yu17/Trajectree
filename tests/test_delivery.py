from datetime import datetime, timezone

import httpx

from trajectree.runner.credentials import store_connector_token
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


def test_deliver_chatmaid_posts_and_falls_back(ledger, settings, agent, human, tmp_path):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = settings.model_copy(update={"chatmaid_url": "http://chatmaid.local/"})
    store_connector_token(s, "chatmaid", "cm_x")
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        return httpx.Response(202)

    res = deliver(ledger, s, "chatmaid", _msg(), actor=agent, now=NOW, post=post)
    assert res.ok and res.channel == "chatmaid"
    assert calls[0][0] == "http://chatmaid.local/deliver" and calls[0][1]["headers"]["Authorization"] == "Bearer cm_x"
    assert calls[0][1]["json"] == {"title": "t", "body_md": "b", "project": None, "run_id": "r", "routine": "x"}
    assert calls[0][1]["timeout"] == 20

    deliver(ledger, s, "chatmaid", _msg(a.id), actor=agent, now=NOW, post=post)
    assert calls[1][1]["json"]["project"] == "a"

    def bad(url, **kw):
        raise httpx.ConnectError("down")

    res2 = deliver(ledger, s, "chatmaid", _msg(a.id), actor=agent, now=NOW, post=bad)
    assert res2.channel == "inbox" and "chatmaid: " in res2.detail and res2.event_id
    assert "cm_x" not in res2.detail

    res3 = deliver(ledger, s, "chatmaid", _msg(a.id), actor=agent, now=NOW, post=lambda url, **kw: httpx.Response(500))
    assert res3.channel == "inbox" and "chatmaid: 500" in res3.detail and res3.event_id


def test_deliver_chatmaid_without_token_falls_back(ledger, settings, agent, human):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = settings.model_copy(update={"chatmaid_url": "http://chatmaid.local"})
    res = deliver(ledger, s, "chatmaid", _msg(a.id), actor=agent, now=NOW, post=lambda *a, **k: 1 / 0)
    assert res.channel == "inbox" and "not configured" in res.detail


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
