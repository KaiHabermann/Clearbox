"""MCP tool bridge: hand the clearbox MCP tools to harnesses without an MCP client.

A built-in Claude agent reaches the service's scoped agent endpoint
(``/mcp-agent/<token>``, see ``mcp_tools.build_mcp``) through the Claude Agent
SDK; that is how a node "expands" itself into a subplan (see EXPAND_HINT in
prompts.py). An OpenAI-compatible model has no MCP client, so this module is one:
it lists the endpoint's tools, turns each into a ``ToolSpec`` (the same
provider-neutral shape ``harness/tools.py`` uses for the local toolbox) and
forwards calls over streamable HTTP. The endpoint decides what an agent may
call; the bridge exposes whatever it lists.

Names are prefixed ``vc_`` (``vc_create_plan``) so they can never collide with
the local toolbox's ``read_file``/``bash``/...; the prefix is stripped again when
dispatching. The server runs with ``stateless_http=True``, so every call opens a
short-lived client connection -- no session to keep alive or reconnect. Only the
tool list is cached, per bridge instance.

Like ``Toolbox.call``, nothing here raises into the agent loop: a dead endpoint
or a failing tool becomes ``(error text, True)`` that the model can read. A
connection failure is logged once per bridge, not once per call.

A run gets the bridge only when the engine hands it an endpoint (``job.mcp_url``;
none with ``--no-agent-mcp``) and ``mcp_enabled`` says yes: the profile's
``tools.mcp`` flag, on by default.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any

from mcp.client.client import Client

from .harness.base import _truncate
from .harness.tools import ToolError, ToolSpec

log = logging.getLogger("clearbox")

PREFIX = "vc_"

DEFAULT_TIMEOUT_S = 60.0


def mcp_enabled(tools: dict[str, Any] | None, default: bool) -> bool:
    """Whether a profile's agents get the bridge: ``tools.mcp`` if set, else ``default``.

    ``default`` applies when the profile does not say (the harness passes True).
    """
    flag = (tools or {}).get("mcp")
    return default if flag is None else bool(flag)


def _flatten(result: Any) -> str:
    """Render a CallToolResult as text, the way tests/test_flow.py's ``call()`` reads it."""
    texts = [getattr(c, "text", None) for c in (result.content or [])]
    joined = "\n".join(t for t in texts if t)
    if result.is_error:
        return joined or "tool reported an error"
    sc = result.structured_content
    if sc is None:
        return joined
    value = sc.get("result", sc) if isinstance(sc, dict) else sc
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class McpToolBridge:
    """The clearbox MCP tools of one endpoint, as ``ToolSpec``s."""

    def __init__(self, url: str, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.url = url
        self.timeout_s = timeout_s
        self._specs: list[ToolSpec] | None = None
        self._lock = asyncio.Lock()
        self._warned = False

    def _warn(self, what: str, e: BaseException) -> str:
        # anyio wraps transport errors in (nested) ExceptionGroups; show the real cause.
        while getattr(e, "exceptions", None):  # BaseExceptionGroup is 3.11+
            e = e.exceptions[0]
        msg = f"clearbox MCP endpoint {self.url} unreachable ({what}): {type(e).__name__}: {e}"
        if not self._warned:
            self._warned = True
            log.warning("%s", msg)
        return msg

    async def specs(self) -> list[ToolSpec]:
        """The endpoint's tools, fetched once and cached. Empty if the server is unreachable.

        A failed fetch is not cached, so a later call can still succeed.
        """
        async with self._lock:
            if self._specs is not None:
                return self._specs
            try:
                tools = []
                async with Client(self.url, read_timeout_seconds=self.timeout_s) as c:
                    cursor = None
                    while True:
                        page = await c.list_tools(cursor=cursor)
                        tools.extend(page.tools)
                        cursor = page.next_cursor
                        if not cursor:
                            break
            except Exception as e:
                self._warn("list_tools", e)
                return []
            self._specs = [self._spec(t) for t in tools]
            return self._specs

    def _spec(self, tool: Any) -> ToolSpec:
        name = tool.name
        description = inspect.cleandoc(tool.description or tool.title or name)
        schema = dict(tool.input_schema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})

        async def handler(**args: Any) -> str:
            text, is_error = await self._call(name, args)
            if is_error:
                raise ToolError(text)
            return text

        return ToolSpec(name=PREFIX + name, description=description, schema=schema, handler=handler)

    def owns(self, name: str) -> bool:
        """Whether a model-supplied tool name belongs to this bridge (``vc_`` prefix)."""
        return name.startswith(PREFIX)

    async def call(self, name: str, args: dict[str, Any] | None) -> tuple[str, bool]:
        """Run ``vc_<tool>``; same contract as ``Toolbox.call``: never raises."""
        bare = name[len(PREFIX):] if name.startswith(PREFIX) else name
        known = [s.name for s in self._specs or []]
        if PREFIX + bare not in known:
            avail = ", ".join(known)
            return _truncate(f"Error: tool {name!r} is not available. Available tools: {avail}"), True
        if args is not None and not isinstance(args, dict):
            return _truncate(f"Error: arguments for {name} must be a JSON object"), True
        return await self._call(bare, args or {})

    async def _call(self, bare: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            async with Client(self.url, read_timeout_seconds=self.timeout_s) as c:
                result = await c.call_tool(bare, args)
        except Exception as e:
            return _truncate("Error: " + self._warn(bare, e)), True
        try:
            text = _flatten(result)
        except Exception as e:
            return _truncate(f"Error: {PREFIX}{bare}: unreadable result: {type(e).__name__}: {e}"), True
        text = text.replace("\x00", "")
        if result.is_error:
            return _truncate(f"Error: {text}"), True
        return _truncate(text), False
