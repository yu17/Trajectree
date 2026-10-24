import json
import time

import httpx

from trajectree.runner.catalog import (
    BUILTIN_CLAUDE_MODELS,
    Catalog,
    RunnerCatalog,
    CatalogModel,
    discover,
    load_catalog,
    merge_failed,
    store_catalog,
)
from trajectree.runner.credentials import store_oauth_token
from trajectree.timeutil import utcnow

FOUND = lambda name: "/usr/bin/" + name


def refused(request):
    return httpx.Response(404)


def discover_with(settings, handler=refused, **kw):
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        return discover(settings, now=utcnow(), http=http, which=FOUND, **kw)


def runner(catalog, name):
    return next(r for r in catalog.runners if r.runner == name)


def write_cache(settings, models):
    settings.codex_home.mkdir(parents=True, exist_ok=True)
    (settings.codex_home / "models_cache.json").write_text(json.dumps({"models": models}))


def write_profile(settings, name, env):
    d = settings.backends_cfg_dir / name
    d.mkdir(parents=True)
    (d / "settings.json").write_text(json.dumps({"env": env}))


def test_codex_from_cache_hides_hidden(settings):
    write_cache(settings, [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list"},
        {"slug": "codex-auto-review", "display_name": "Auto", "visibility": "hide"},
    ])
    r = runner(discover_with(settings), "codex")
    assert [(m.model, m.display, m.source) for m in r.models] == [("gpt-6.1-sol", "GPT-6.1-Sol", "cache")]
    assert r.available and r.error is None


def test_codex_missing_cache_error(settings):
    r = runner(discover_with(settings), "codex")
    assert r.models == [] and "codex login --device-auth" in r.error


def test_missing_binary_lists_nothing(settings):
    write_cache(settings, [{"slug": "gpt-6.1-sol", "visibility": "list"}])
    with httpx.Client(transport=httpx.MockTransport(refused)) as http:
        c = discover(settings, now=utcnow(), http=http, which=lambda name: None)
    r = runner(c, "codex")
    assert not r.available and r.models == [] and "not found" in r.error


def test_ccx_profile_models(settings):
    write_profile(settings, "ds", {
        "ANTHROPIC_AUTH_TOKEN": "k", "ANTHROPIC_MODEL": "deepseek-flash[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash", "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash[1m]",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
    })
    r = runner(discover_with(settings), "ccx:ds")
    assert sorted(m.model for m in r.models) == ["deepseek-flash", "deepseek-flash[1m]"]
    assert {m.source for m in r.models} == {"profile"} and r.error is None


def test_ccx_endpoint_adds_models(settings):
    write_profile(settings, "ds", {"ANTHROPIC_API_KEY": "sk-secret", "ANTHROPIC_BASE_URL": "https://api.ds.example/anthropic", "ANTHROPIC_MODEL": "deepseek-flash"})
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "deepseek-pro"}, {"id": "deepseek-flash"}]})

    r = runner(discover_with(settings, handler), "ccx:ds")
    assert [(m.model, m.source) for m in r.models] == [("deepseek-flash", "profile"), ("deepseek-pro", "api")]
    assert len(seen) == 1 and str(seen[0].url) == "https://api.ds.example/anthropic/v1/models"
    assert seen[0].headers["x-api-key"] == "sk-secret" and seen[0].headers["anthropic-version"] == "2023-06-01"


def test_unreachable_backend_keeps_profile_models(settings):
    write_profile(settings, "slow", {"ANTHROPIC_API_KEY": "sk-secret", "ANTHROPIC_BASE_URL": "http://127.0.0.1:40090", "ANTHROPIC_MODEL": "m1"})
    write_profile(settings, "fine", {"ANTHROPIC_API_KEY": "k2", "ANTHROPIC_BASE_URL": "http://fine.example", "ANTHROPIC_MODEL": "m2"})

    def handler(request):
        if request.url.port == 40090:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json={"data": [{"id": "m3"}]})

    start = time.monotonic()
    c = discover_with(settings, handler, timeout_s=0.5)
    assert time.monotonic() - start < 2
    slow, fine = runner(c, "ccx:slow"), runner(c, "ccx:fine")
    assert [m.model for m in slow.models] == ["m1"] and slow.error == "models endpoint unavailable: ConnectTimeout (127.0.0.1)"
    assert [m.model for m in fine.models] == ["m2", "m3"] and fine.error is None


def test_claude_live_list(settings):
    store_oauth_token(settings, "oat-secret")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "claude-x-1", "display_name": "Claude X"}]})

    r = runner(discover_with(settings, handler), "claude")
    assert [(m.model, m.display, m.source) for m in r.models] == [("claude-x-1", "Claude X", "api")]
    assert seen[0].headers["authorization"] == "Bearer oat-secret"
    assert seen[0].headers["anthropic-beta"] == "oauth-2025-04-20"
    assert r.available and r.error is None


def test_claude_falls_back_to_builtin(settings):
    store_oauth_token(settings, "oat-secret")
    r = runner(discover_with(settings, lambda req: httpx.Response(401)), "claude")
    assert [m.model for m in r.models] == list(BUILTIN_CLAUDE_MODELS)
    assert {m.source for m in r.models} == {"builtin"} and r.error == "live list unavailable: 401"


def test_claude_without_token_makes_no_call(settings):
    def handler(request):
        raise AssertionError("no call without a token")

    r = runner(discover_with(settings, handler), "claude")
    assert not r.available and r.models == [] and r.error == "not logged in; run: trajectree login"


def test_unavailable_runner_shows_no_models_after_earlier_refresh():
    prev = Catalog(runners=[rc("claude", [CatalogModel(runner="claude", model="live-1", source="api")])])
    fresh = Catalog(runners=[rc("claude", [], error="not logged in; run: trajectree login", available=False)])
    claude = merge_failed(prev, fresh).runners[0]
    assert claude.models == [] and not claude.available


def test_no_secret_in_catalog(settings):
    store_oauth_token(settings, "oat-secret")
    write_profile(settings, "ds", {"ANTHROPIC_API_KEY": "sk-secret", "ANTHROPIC_BASE_URL": "https://user:pw@api.ds.example/x", "ANTHROPIC_MODEL": "m"})
    for handler in (refused, lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom https://user:pw@api.ds.example/x"))):
        text = discover_with(settings, handler).model_dump_json()
        assert "oat-secret" not in text and "sk-secret" not in text and "pw@" not in text and "https://" not in text


def test_store_load_roundtrip_and_empty(conn, settings):
    assert load_catalog(conn).runners == []
    c = discover_with(settings)
    store_catalog(conn, c)
    assert load_catalog(conn) == c


def rc(name, models, error=None, available=True, at=None):
    return RunnerCatalog(runner=name, available=available, models=models, refreshed_at=at or utcnow(), error=error)


def test_merge_failed_keeps_previous_models_and_records_error():
    old_at = utcnow()
    prev = Catalog(runners=[
        rc("claude", [CatalogModel(runner="claude", model="live-1", source="api")], at=old_at),
        rc("codex", [CatalogModel(runner="codex", model="gpt-a", source="cache")]),
    ])
    fresh = Catalog(runners=[
        rc("claude", [CatalogModel(runner="claude", model="b", source="builtin")], error="live list unavailable: 401"),
        rc("codex", [CatalogModel(runner="codex", model="gpt-b", source="cache")]),
    ])
    merged = merge_failed(prev, fresh)
    claude, codex = merged.runners
    assert [m.model for m in claude.models] == ["live-1"] and claude.error == "live list unavailable: 401" and claude.refreshed_at == old_at
    assert [m.model for m in codex.models] == ["gpt-b"]
