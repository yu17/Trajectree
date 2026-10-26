from tests.test_api_entities import mk_project

def seed(client):
    p = mk_project(client)
    e = client.post("/api/events", json={"project_id": p["id"], "kind": "decision", "title": "Freeze", "t_start": "2026-01-05T10:00:00Z"}).json()
    return p, e

def test_timeline_returns_window_and_pending(client, app, agent):
    p, e = seed(client)
    from trajectree.repo.events import create_event
    from trajectree.models import EventIn
    from datetime import datetime, timezone
    pending = create_event(app.state.ledger, agent, EventIn(project_id=p["id"], kind="milestone", title="draft",
                           t_start=datetime(2026, 1, 6, tzinfo=timezone.utc), source="agent"))
    tl = client.get("/api/timeline", params={"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"}).json()
    assert [x["id"] for x in tl["projects"] if x["slug"] != "inbox"] == [p["id"]]   # inbox is seeded
    assert {x["id"] for x in tl["events"]} == {e["id"], pending.id}
    assert tl["pending"]["event"] == {pending.id: client.get("/api/ledger/txns").json()[0]["id"]} and tl["live"] == []

def test_ledger_list_detail_and_revert(client):
    p, e = seed(client)
    txns = client.get("/api/ledger/txns").json()
    assert txns[0]["intent"] == "record event: Freeze" and "changeset" not in txns[0]
    detail = client.get(f"/api/ledger/txns/{txns[0]['id']}").json()
    assert detail["changes"][0]["table_name"] == "event" and detail["changes"][0]["new"]["title"] == "Freeze"
    r = client.post(f"/api/ledger/txns/{txns[0]['id']}/revert", json={})
    assert r.status_code == 200 and r.json()["reverts_txn"] == txns[0]["id"]
    assert client.get("/api/events", params={"projects": p["id"]}).json() == []
    assert client.post(f"/api/ledger/txns/{txns[0]['id']}/revert", json={}).status_code == 409

def test_revert_conflict_returns_409(client):
    p, e = seed(client)
    create_txn = client.get("/api/ledger/txns").json()[0]["id"]
    client.patch(f"/api/events/{e['id']}", json={"title": "Freeze v2"})
    r = client.post(f"/api/ledger/txns/{create_txn}/revert", json={})
    assert r.status_code == 409 and r.json()["conflicts"][0]["table_name"] == "event"
    assert client.get("/api/events", params={"projects": p["id"]}).json()[0]["title"] == "Freeze v2"

def test_review_accept(client, app, agent):
    p, _ = seed(client)
    from trajectree.repo.events import create_event
    from trajectree.models import EventIn
    from datetime import datetime, timezone
    create_event(app.state.ledger, agent, EventIn(project_id=p["id"], kind="note", title="n",
                 t_start=datetime(2026, 1, 6, tzinfo=timezone.utc), source="agent"))
    tid = client.get("/api/ledger/txns").json()[0]["id"]
    assert client.post(f"/api/ledger/txns/{tid}/review", json={"state": "accepted"}).json()["review"] == "accepted"

def test_search_route(client):
    seed(client)
    hits = client.get("/api/search", params={"q": "freeze"}).json()
    assert hits[0]["kind"] == "event" and hits[0]["title"] == "Freeze"

def test_search_limit_is_bounded(client):
    for bad in (0, 1000):
        assert client.get("/api/search", params={"q": "x", "limit": bad}).status_code == 422

def test_unknown_txn_is_404(client):
    assert client.get("/api/ledger/txns/nope").status_code == 404
    assert client.post("/api/ledger/txns/nope/revert", json={}).status_code == 404
    assert client.post("/api/ledger/txns/nope/review", json={"state": "accepted"}).status_code == 404

def test_timeline_todos_due_in_window_and_open_undated(client):
    p, _ = seed(client)
    for title, due, state in [("in", "2026-01-10T00:00:00Z", "open"), ("out", "2026-03-10T00:00:00Z", "open"),
                              ("undated", None, "open"), ("done-undated", None, "done")]:
        client.post("/api/todos", json={"project_id": p["id"], "title": title, "due": due, "state": state})
    tl = client.get("/api/timeline", params={"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"}).json()
    assert {t["title"] for t in tl["todos"]} == {"in", "undated"}

def test_ledger_filters(client, app, agent):
    p, _ = seed(client)
    from trajectree.repo.events import create_event
    from trajectree.models import EventIn
    from datetime import datetime, timezone
    create_event(app.state.ledger, agent, EventIn(project_id=p["id"], kind="note", title="n",
                 t_start=datetime(2026, 1, 6, tzinfo=timezone.utc), source="agent"))
    assert [t["actor_kind"] for t in client.get("/api/ledger/txns", params={"review": "pending"}).json()] == ["agent"]
    human = client.get("/api/ledger/txns", params={"actor_kind": "human"}).json()
    assert len([t for t in human if t["tool"] != "seed"]) == 2   # the seeded inbox project is a human txn too
    assert client.get("/api/ledger/txns", params={"review": "bogus"}).status_code == 422


def test_context_route(client):
    p, e = seed(client)
    r = client.get("/api/context", params={"projects": p["slug"], "from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"})
    assert r.status_code == 200 and "Freeze" in r.json()["markdown"]
    assert client.get("/api/context", params={"projects": "ghost"}).status_code == 404
    assert client.get("/api/context", params={"projects": p["slug"], "budget": 5}).status_code == 422


def _agent_create_then_update(client, app, run_id="run_a"):
    from datetime import datetime, timezone

    from trajectree.models import Actor, EventIn, EventPatch
    from trajectree.repo.events import create_event, update_event

    p, _ = seed(client)
    actor = Actor(kind="agent", ref=run_id, run_id=run_id)
    ev = create_event(app.state.ledger, actor, EventIn(project_id=p["id"], kind="note", title="n",
                      t_start=datetime(2026, 1, 6, tzinfo=timezone.utc), source="agent"))
    update_event(app.state.ledger, actor, ev.id, EventPatch(title="n2"))
    rows = client.get("/api/ledger/txns", params={"run_id": run_id}).json()
    return ev, rows   # newest first


def test_review_many_rejects_newest_first_so_dependents_go_before_their_target(client, app):
    ev, rows = _agent_create_then_update(client, app)
    b, a = rows[0]["id"], rows[1]["id"]
    r = client.post("/api/ledger/review-many", json={"ids": [a, b], "state": "rejected"})
    assert r.status_code == 200
    assert r.json() == {"done": [b, a], "failed": None, "conflicts": []}
    assert all(client.get(f"/api/ledger/txns/{i}").json()["txn"]["review"] == "rejected" for i in (a, b))


def test_review_many_stops_at_a_conflict_and_reports_it(client, app):
    ev, rows = _agent_create_then_update(client, app)
    b, a = rows[0]["id"], rows[1]["id"]
    assert client.post(f"/api/ledger/txns/{a}/revert").status_code == 409
    r = client.post("/api/ledger/review-many", json={"ids": [a], "state": "rejected"})
    assert r.status_code == 200
    body = r.json()
    assert body["done"] == [] and body["failed"] == a and body["conflicts"]


def test_review_many_accepts_in_given_order_and_unknown_id_stops(client, app):
    ev, rows = _agent_create_then_update(client, app)
    b, a = rows[0]["id"], rows[1]["id"]
    r = client.post("/api/ledger/review-many", json={"ids": [a, "nope", b], "state": "accepted"})
    assert r.json() == {"done": [a], "failed": "nope", "conflicts": []}
    assert client.post("/api/ledger/review-many", json={"ids": [], "state": "accepted"}).status_code == 422


def test_review_many_treats_already_rejected_as_done(client, app):
    ev, rows = _agent_create_then_update(client, app)
    b, a = rows[0]["id"], rows[1]["id"]
    client.post("/api/ledger/review-many", json={"ids": [a, b], "state": "rejected"})
    r = client.post("/api/ledger/review-many", json={"ids": [a, b], "state": "rejected"})
    assert r.json() == {"done": [b, a], "failed": None, "conflicts": []}


def test_txns_filter_by_run_id(client, app):
    ev, rows = _agent_create_then_update(client, app, "run_a")
    assert len(rows) == 2 and {r["run_id"] for r in rows} == {"run_a"}
    assert client.get("/api/ledger/txns", params={"run_id": "run_zzz"}).json() == []
