import pytest
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo import NotFound
from trajectree.repo.projects import create_project
from trajectree.repo.resources import list_resources, project_for_path, upsert_resource

def test_upsert_creates_then_updates_without_duplicate(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r1 = upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator="/mnt/x", title="X"))
    r2 = upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator="/mnt/x", title="X2"))
    assert r1.id == r2.id and r2.title == "X2"
    assert len(list_resources(ledger.conn, project_ids=[p.id])) == 1

def test_upsert_unchanged_writes_no_txn(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    data = ResourceIn(project_id=p.id, kind="folder", locator="/mnt/x", title="X")
    upsert_resource(ledger, human, data)
    n = len(ledger.list_txns())
    upsert_resource(ledger, human, data)
    assert len(ledger.list_txns()) == n

def test_upsert_unknown_project_raises(ledger, human):
    with pytest.raises(NotFound):
        upsert_resource(ledger, human, ResourceIn(project_id="nope", kind="folder", locator="/x"))

def test_project_for_path_prefers_longest_component_prefix(ledger, human):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    b = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=a.id, kind="folder", locator="/mnt/apps"))
    upsert_resource(ledger, human, ResourceIn(project_id=b.id, kind="git_local", locator="/mnt/apps/trajectree"))
    conn = ledger.conn
    assert project_for_path(conn, "/mnt/apps/trajectree/src").id == b.id
    assert project_for_path(conn, "/mnt/apps/trajectree").id == b.id
    assert project_for_path(conn, "/mnt/apps/other").id == a.id
    assert project_for_path(conn, "/mnt/applications") is None
    assert project_for_path(conn, "/elsewhere") is None
