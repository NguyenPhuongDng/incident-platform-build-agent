"""MCP client: talks to the mock department servers over streamable HTTP.

The room loop is synchronous, so all async MCP work is funnelled through one
dedicated background event loop and exposed as blocking calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from backend.app.config import settings

logger = logging.getLogger("mcp")

# server key -> url ; adding a department = adding a line here + entries in catalog.yaml
MCP_SERVERS: dict[str, str] = {
    "ky_thuat": settings.mcp_ky_thuat_url,
    "an_ninh": settings.mcp_an_ninh_url,
    "ve_sinh": settings.mcp_ve_sinh_url,
}


class _LoopThread:
    """A private asyncio loop so sync code can await MCP coroutines."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="mcp-loop")
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: float = 30.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)


_loop_thread: _LoopThread | None = None
_loop_init_lock = threading.Lock()


def _runner() -> _LoopThread:
    """Same double-checked-lock fix as knowledge/store.py: the Evaluator calls this
    from several concurrent case threads, and racing through this lazy singleton
    could start (and leak) more than one background loop thread."""
    global _loop_thread
    if _loop_thread is None:
        with _loop_init_lock:
            if _loop_thread is None:
                _loop_thread = _LoopThread()
    return _loop_thread


async def _with_session(url: str, fn):
    from mcp import Client

    async with Client(url) as client:
        return await fn(client)


class MCPClient:
    """Caches tool schemas per server; degrades gracefully when a server is down."""

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}   # tool name -> input schema
        self._owner: dict[str, str] = {}                # tool name -> server key
        self._available: dict[str, bool] = {k: False for k in MCP_SERVERS}
        self._errors: dict[str, str] = {}

    # ---------------------------------------------------------------- discovery
    async def _list(self, server: str) -> list[Any]:
        url = MCP_SERVERS[server]
        result = await _with_session(url, lambda s: s.list_tools())
        return list(result.tools)

    async def refresh(self) -> None:
        for server in MCP_SERVERS:
            try:
                tools = await self._list(server)
                for t in tools:
                    schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                    self._schemas[t.name] = _flatten(schema or {"type": "object", "properties": {}})
                    self._owner[t.name] = server
                self._available[server] = True
                self._errors.pop(server, None)
                logger.info("MCP '%s' sẵn sàng: %s", server, [t.name for t in tools])
            except Exception as exc:  # noqa: BLE001 - optional dependency
                self._available[server] = False
                self._errors[server] = str(exc)[:200]
                logger.warning("MCP '%s' không khả dụng: %s", server, str(exc)[:200])

    def refresh_sync(self) -> None:
        try:
            _runner().run(self.refresh(), timeout=25)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Làm mới MCP thất bại: %s", exc)

    # ---------------------------------------------------------------- accessors
    def is_available(self, server: str) -> bool:
        return self._available.get(server, False)

    def error(self, server: str) -> str:
        return self._errors.get(server, "")

    def schema_for(self, tool_name: str) -> dict[str, Any] | None:
        return self._schemas.get(tool_name)

    # ---------------------------------------------------------------- execution
    def call(self, server: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if server not in MCP_SERVERS:
            return {"loi": f"Không biết MCP server '{server}'"}

        async def _do(session):
            return await session.call_tool(tool_name, args)

        try:
            result = _runner().run(_with_session(MCP_SERVERS[server], _do), timeout=45)
        except Exception as exc:  # noqa: BLE001
            self._available[server] = False
            self._errors[server] = str(exc)[:200]
            return {"loi": f"Không gọi được tool qua MCP server '{server}': {str(exc)[:200]}"}

        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            return {"loi": _text_of(result)}
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            # FastMCP wraps plain return values under "result"
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        text = _text_of(result)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"ket_qua": text}


def _flatten(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $defs and collapse anyOf so tool schemas stay simple for the LLM API."""
    from backend.tools.catalog import _flatten_refs

    return _flatten_refs(dict(schema))


def _text_of(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


mcp_client = MCPClient()
