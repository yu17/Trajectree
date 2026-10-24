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
