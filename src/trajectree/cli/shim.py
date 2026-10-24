from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Mapping

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)

from trajectree.cli.main import _token, base_url


def _root_cause(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def build_proxy(connect: Callable[[], Any]) -> Server:
    async def handle_list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        try:
            async with connect() as remote:
                return await remote.list_tools()
        except Exception as exc:
            print(f"trajectree shim: cannot list tools: {_root_cause(exc)}", file=sys.stderr)
            return ListToolsResult(tools=[])

    async def handle_call_tool(
        ctx: ServerRequestContext, params: CallToolRequestParams
    ) -> CallToolResult:
        try:
            async with connect() as remote:
                return await remote.call_tool(params.name, params.arguments or {})
        except Exception as exc:
            return CallToolResult(
                is_error=True,
                content=[TextContent(type="text", text=f"trajectree daemon unreachable: {_root_cause(exc)}")],
            )

    return Server(
        "trajectree-shim", on_list_tools=handle_list_tools, on_call_tool=handle_call_tool
    )


def remote_connector(
    url: str, token: str, cwd: str | None, session_id: str | None = None, transport: Any = None
) -> Callable[[], Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Trajectree-Client": "shim",
        **({"X-Trajectree-Cwd": cwd} if cwd else {}),
        **({"X-Trajectree-Session": session_id} if session_id else {}),
    }

    @asynccontextmanager
    async def connect() -> AsyncIterator[Client]:
        async with httpx2.AsyncClient(
            headers=headers, timeout=httpx2.Timeout(30, read=120), transport=transport
        ) as http:
            async with Client(streamable_http_client(url, http_client=http)) as remote:
                yield remote

    return connect


def run_shim(env: Mapping[str, str]) -> int:
    token = _token(env)
    if token is None:
        print(
            "no token: set TRAJECTREE_TOKEN or write it to ~/.config/trajectree/token",
            file=sys.stderr,
        )
        return 2
    url = base_url(env) + "/mcp"
    cwd = env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    server = build_proxy(remote_connector(url, token, cwd, env.get("CLAUDE_CODE_SESSION_ID")))

    async def serve() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(serve)
    return 0
