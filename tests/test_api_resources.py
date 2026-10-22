from tests.test_api_entities import mk_project

def test_resources_upsert_list_and_errors(client):
    p = mk_project(client)
    body = {"project_id": p["id"], "kind": "folder", "locator": "/mnt/x", "title": "X"}
    r1 = client.post("/api/resources", json=body).json()
    r2 = client.post("/api/resources", json={**body, "title": "X2"}).json()
    assert r1["id"] == r2["id"] and r2["title"] == "X2"
    assert [r["id"] for r in client.get("/api/resources", params={"kinds": "folder"}).json()] == [r1["id"]]
    assert client.get("/api/resources", params={"kinds": "url"}).json() == []
    assert client.post("/api/resources", json={**body, "kind": "weird"}).status_code == 422
    assert client.post("/api/resources", json={**body, "project_id": "nope"}).status_code == 404
