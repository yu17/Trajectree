from datetime import datetime, timedelta, timezone

from trajectree.models import ProjectIn, SignalIn
from trajectree.repo.projects import create_project
from trajectree.repo.signals import upsert_signals

UTC = timezone.utc


def test_inspections_endpoint_lists_due_projects(client, anon, ledger, human):
    assert client.get("/api/inspections").json() == []
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    t = datetime.now(UTC) - timedelta(days=3)
    with ledger.locked() as conn:
        upsert_signals(conn, [SignalIn(project_id=p.id, kind="session", t_start=t, t_end=t + timedelta(minutes=30), dedupe_key="s")])
    got = client.get("/api/inspections").json()
    assert len(got) == 1 and got[0]["slug"] == "a" and got[0]["stretches"] == 1
    assert anon.get("/api/inspections").status_code == 401
