import json

import httpx

from trajectree.runner.catalog import load_catalog
from trajectree.runner.credentials import store_oauth_token


def test_refresh_endpoint_stores(client, app, settings, ledger):
    store_oauth_token(settings, "oat-secret")
    app.state.model_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "claude-live-1"}]})
    )
    r = client.post("/api/models/refresh")
    assert r.status_code == 200
    claude = next(x for x in r.json()["runners"] if x["runner"] == "claude")
    assert [m["model"] for m in claude["models"]] == ["claude-live-1"]
    assert client.get("/api/models").json() == r.json()
    with ledger.locked() as conn:
        assert load_catalog(conn).model_dump(mode="json") == r.json()
    assert "oat-secret" not in json.dumps(r.json())


def test_models_requires_owner(app, anon):
    assert anon.get("/api/models").status_code == 401
    assert anon.post("/api/models/refresh").status_code == 401


def test_backends_sync_refreshes(client, app, settings, tmp_path):
    root = tmp_path / "backends"
    (root / "ds").mkdir(parents=True)
    (root / "ds" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "deepseek-flash"}}))
    settings.backends_root = root
    assert client.post("/api/backends/sync").json() == {"synced": ["ds"]}
    ds = next(x for x in client.get("/api/models").json()["runners"] if x["runner"] == "ccx:ds")
    assert [m["model"] for m in ds["models"]] == ["deepseek-flash"]


def test_backends_sync_answers_even_when_the_refresh_fails(client, app, settings, tmp_path, monkeypatch, caplog):
    import trajectree.api.routes_runs as routes_runs

    root = tmp_path / "backends"
    (root / "ds").mkdir(parents=True)
    (root / "ds" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "k"}}))
    settings.backends_root = root

    def boom(app):
        raise RuntimeError("store failed")

    monkeypatch.setattr(routes_runs, "refresh_catalog", boom)
    r = client.post("/api/backends/sync")
    assert (r.status_code, r.json()) == (200, {"synced": ["ds"]})
    assert "catalog refresh after backends sync failed: RuntimeError" in caplog.text


def test_edit_endpoint_adds_and_deletes(client, app, settings):
    store_oauth_token(settings, "oat-secret")
    app.state.model_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "claude-live-1"}, {"id": "claude-live-2"}]})
    )
    client.post("/api/models/refresh")
    r = client.post("/api/models/edit", json={"runner": "claude", "add": ["my-model"], "delete": ["claude-live-1"]})
    assert r.status_code == 200
    claude = next(x for x in r.json()["runners"] if x["runner"] == "claude")
    assert [(m["model"], m["source"]) for m in claude["models"]] == [("claude-live-2", "api"), ("my-model", "manual")]
    assert client.get("/api/models").json() == r.json()


def test_edit_unknown_runner_404(client):
    assert client.post("/api/models/edit", json={"runner": "ccx:nope", "add": ["x"]}).status_code == 404


def test_edit_empty_or_blank_422(client, app, settings):
    store_oauth_token(settings, "oat-secret")
    client.post("/api/models/refresh")
    assert client.post("/api/models/edit", json={"runner": "claude"}).status_code == 422
    assert client.post("/api/models/edit", json={"runner": "claude", "add": ["  "]}).status_code == 422


def test_edit_requires_owner(anon):
    assert anon.post("/api/models/edit", json={"runner": "claude", "add": ["x"]}).status_code == 401
