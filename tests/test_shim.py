import pytest
from mcp import Client

from trajectree.auth.scopes import owner_principal
from trajectree.cli.shim import build_proxy
from trajectree.mcpserver.server import build_mcp
from trajectree.models import ProjectIn
from trajectree.repo.projects import create_project

pytestmark = pytest.mark.anyio


async def test_proxy_forwards_list_and_call(ledger, settings, human):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    local = build_mcp(ledger, settings, resolve=lambda ctx: owner_principal(None))
    proxy = build_proxy(lambda: Client(local))
    async with Client(proxy) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "record_event" in names and "get_context_pack" in names
        r = await c.call_tool("list_projects", {})
        assert not r.is_error and r.structured_content["result"][0]["slug"] == "a"
        bad = await c.call_tool("get_project", {"project": "zzz"})
        assert bad.is_error and "known projects" in bad.content[0].text


async def test_proxy_reports_unreachable_daemon():
    proxy = build_proxy(lambda: Client("http://127.0.0.1:9/mcp"))
    async with Client(proxy) as c:
        assert (await c.list_tools()).tools == []
        r = await c.call_tool("list_projects", {})
        assert r.is_error and "unreachable" in r.content[0].text


async def test_remote_connector_sends_headers():
    import httpx2

    from trajectree.cli.shim import remote_connector

    seen: list[dict[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.headers))
        return httpx2.Response(500)

    connect = remote_connector(
        "http://daemon.test/mcp", "tok", "/proj", session_id="sess-1", transport=httpx2.MockTransport(handler)
    )
    try:
        async with connect() as remote:
            await remote.list_tools()
    except Exception:
        pass
    assert seen
    h = seen[0]
    assert h["authorization"] == "Bearer tok"
    assert h["x-trajectree-client"] == "shim"
    assert h["x-trajectree-cwd"] == "/proj"
    assert h["x-trajectree-session"] == "sess-1"


def test_shim_uses_base_url_file(monkeypatch, tmp_path):
    from trajectree.cli import shim

    (tmp_path / ".config" / "trajectree").mkdir(parents=True)
    (tmp_path / ".config" / "trajectree" / "url").write_text("http://127.0.0.1:40210/\n")
    seen: list[str] = []

    def fake_connector(url, *args, **kwargs):
        seen.append(url)
        return lambda: None

    monkeypatch.setattr(shim, "remote_connector", fake_connector)
    monkeypatch.setattr(shim, "build_proxy", lambda connector: None)
    monkeypatch.setattr(shim.anyio, "run", lambda fn: None)
    assert shim.run_shim({"HOME": str(tmp_path), "TRAJECTREE_TOKEN": "tok"}) == 0
    assert seen == ["http://127.0.0.1:40210/mcp"]
