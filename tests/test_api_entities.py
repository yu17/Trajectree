def mk_project(client, slug="clmi"):
    return client.post("/api/projects", json={"slug": slug, "title": slug.upper(), "kind": "research"}).json()


def test_project_crud_and_duplicate(client):
    p = mk_project(client)
    assert p["slug"] == "clmi"
    assert client.post("/api/projects", json={"slug": "clmi", "title": "x", "kind": "tooling"}).status_code == 409
    r = client.patch(f"/api/projects/{p['id']}", json={"status": "paused", "intent": "pause for deadline"})
    assert r.json()["status"] == "paused"


def test_event_create_validation_and_window(client):
    p = mk_project(client)
    bad = client.post("/api/events", json={"project_id": p["id"], "kind": "meeting", "title": "m",
                                            "t_start": "2026-01-02T10:00:00Z", "t_end": "2026-01-01T10:00:00Z"})
    assert bad.status_code == 422
    naive = client.post("/api/events", json={"project_id": p["id"], "kind": "note", "title": "n", "t_start": "2026-01-01T10:00:00"})
    assert naive.status_code == 422
    ok = client.post("/api/events", json={"project_id": p["id"], "kind": "decision", "title": "Freeze", "t_start": "2026-01-05T10:00:00Z"})
    assert ok.status_code == 200 and ok.json()["status"] == "happened"
    got = client.get("/api/events", params={"projects": p["id"], "from": "2026-01-01T00:00:00Z", "to": "2026-01-31T00:00:00Z"}).json()
    assert [e["title"] for e in got] == ["Freeze"]


def test_event_unknown_project_404(client):
    r = client.post("/api/events", json={"project_id": "nope", "kind": "note", "title": "n", "t_start": "2026-01-01T10:00:00Z"})
    assert r.status_code == 404


def test_todo_and_edge_routes(client):
    p = mk_project(client)
    e1 = client.post("/api/events", json={"project_id": p["id"], "kind": "idea", "title": "a", "t_start": "2026-01-01T10:00:00Z"}).json()
    e2 = client.post("/api/events", json={"project_id": p["id"], "kind": "idea", "title": "b", "t_start": "2026-01-02T10:00:00Z"}).json()
    t = client.post("/api/todos", json={"project_id": p["id"], "title": "do it", "due": "2026-01-09T00:00:00Z"}).json()
    assert client.patch(f"/api/todos/{t['id']}", json={"state": "done"}).json()["state"] == "done"
    edge = client.post("/api/edges", json={"src_type": "event", "src_id": e1["id"], "dst_type": "event", "dst_id": e2["id"], "rel": "led_to"})
    assert edge.status_code == 200
    assert client.post("/api/edges", json={"src_type": "event", "src_id": e1["id"], "dst_type": "event", "dst_id": e2["id"], "rel": "led_to"}).status_code == 409
    assert client.delete(f"/api/edges/{edge.json()['id']}").status_code == 204
