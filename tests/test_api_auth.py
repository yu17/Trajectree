def test_health_is_public(anon):
    assert anon.get("/api/health").json()["ok"] is True


def test_api_requires_token(anon):
    assert anon.get("/api/projects").status_code == 401


def test_bearer_token_accepted(client):
    assert client.get("/api/projects").status_code == 200


def test_login_url_sets_cookie_and_redirects(anon, app):
    r = anon.get(f"/?token={app.state.token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "trajectree_token" in r.cookies
    assert anon.get("/api/projects").status_code == 200


def test_bad_token_401(anon):
    r = anon.get("/?token=wrong", follow_redirects=False)
    assert r.status_code == 401 and "trajectree_token" not in r.cookies


def test_token_file_created_with_mode_600(settings):
    from trajectree.config import ensure_token

    t = ensure_token(settings)
    assert len(t) >= 43 and oct(settings.token_path.stat().st_mode)[-3:] == "600"


def test_two_apps_do_not_duplicate_routes(settings, ledger):
    from trajectree.api.app import create_app

    first = create_app(settings, ledger=ledger)
    second = create_app(settings, ledger=ledger)
    assert len(second.routes) == len(first.routes)
    assert list(second.openapi()["paths"]).count("/api/projects") == 1


def test_empty_static_dir_is_not_mounted(settings, ledger, tmp_path, monkeypatch):
    from trajectree.api import app as app_module

    empty = tmp_path / "static"
    empty.mkdir()
    monkeypatch.setattr(app_module, "STATIC_DIR", empty)
    built = app_module.create_app(settings, ledger=ledger)
    assert not any(getattr(r, "name", "") == "static" for r in built.routes)
