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
