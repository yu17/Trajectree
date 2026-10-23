from datetime import datetime, timezone

from trajectree.connectors.recurrence import instances, materialize
from trajectree.models import EventIn, ProjectIn, ProjectPatch
from trajectree.repo.events import create_event, list_events
from trajectree.repo.projects import create_project, update_project

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # a Wednesday
RULE = "FREQ=WEEKLY;BYDAY=TU;BYHOUR=10;BYMINUTE=0"


def test_instances_bad_rule_raises_value_error():
    import pytest

    with pytest.raises(ValueError):
        instances("FREQ=NOPE", start=NOW, until=NOW)


def test_materialize_creates_planned_instances_auto_accepted(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="gm", title="Group meeting", kind="routine", recurrence=RULE))
    rep = materialize(ledger, now=NOW)
    evs = list_events(ledger.conn, project_ids=[p.id])
    assert rep.created == len(evs) == 8 and all(e.status == "planned" and e.source == "connector" for e in evs)
    assert all(e.kind == "meeting" and e.title == "Group meeting" for e in evs)
    assert ledger.list_txns(limit=1)[0].review == "accepted"
    assert materialize(ledger, now=NOW).created == 0


def test_rule_change_drops_future_instances_only(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="gm", title="Group meeting", kind="routine", recurrence=RULE))
    materialize(ledger, now=NOW)
    past = create_event(
        ledger,
        human,
        EventIn(
            project_id=p.id, kind="meeting", title="old", t_start=datetime(2026, 10, 20, 10, tzinfo=UTC),
            status="planned", source="connector", props={"instance": "2026-10-20T10:00:00Z", "rrule": RULE},
        ),
    )
    new_rule = "FREQ=WEEKLY;BYDAY=TH;BYHOUR=9;BYMINUTE=0"
    update_project(ledger, human, p.id, ProjectPatch(recurrence=new_rule))
    rep = materialize(ledger, now=NOW)
    assert rep.dropped == 8 and rep.created == 8
    evs = list_events(ledger.conn, project_ids=[p.id])
    by_id = {e.id: e for e in evs}
    assert by_id[past.id].status == "planned"
    planned = [e for e in evs if e.status == "planned" and e.id != past.id]
    dropped = [e for e in evs if e.status == "dropped"]
    assert len(planned) == 8 and len(dropped) == 8
    assert all(e.props["rrule"] == new_rule for e in planned)


def test_instance_props_customise_event(ledger, human):
    p = create_project(
        ledger, human,
        ProjectIn(slug="gm", title="Group meeting", kind="routine", recurrence=RULE,
                  props={"instance_kind": "milestone", "instance_title": "Standup", "instance_minutes": 30}),
    )
    materialize(ledger, now=NOW)
    e = list_events(ledger.conn, project_ids=[p.id])[0]
    assert e.kind == "milestone" and e.title == "Standup" and (e.t_end - e.t_start).total_seconds() == 1800
    assert e.props["instance"].startswith("2026-10-27T10:00:00")


def test_bad_rule_reported(ledger, human):
    create_project(ledger, human, ProjectIn(slug="gm", title="Group meeting", kind="routine", recurrence="FREQ=NOPE"))
    rep = materialize(ledger, now=NOW)
    assert rep.created == 0 and len(rep.errors) == 1 and rep.errors[0].startswith("gm: ")
