"""MCP tool bridge: hand the clearbox MCP tools to harnesses without an MCP client.

A built-in Claude agent reaches the service's own MCP endpoint through the
Claude Agent SDK; that is how a node "expands" itself into a subplan (see
EXPAND_HINT in engine.py). An OpenAI-compatible model has no MCP client, so this
module is one: it lists the server's tools, turns each into a ``ToolSpec`` (the
same provider-neutral shape ``harness/tools.py`` uses for the local toolbox) and
forwards calls over streamable HTTP.

Names are prefixed ``vc_`` (``vc_create_plan``) so they can never collide with
the local toolbox's ``read_file``/``bash``/...; the prefix is stripped again when
dispatching. The server runs with ``stateless_http=True``, so every call opens a
short-lived client connection -- no session to keep alive or reconnect. Only the
tool list is cached, per bridge instance.

Like ``Toolbox.call``, nothing here raises into the agent loop: a dead endpoint
or a failing tool becomes ``(error text, True)`` that the model can read. A
connection failure is logged once per bridge, not once per call.

Whether a profile gets the bridge at all is decided by ``mcp_enabled``: the
profile's ``tools.mcp`` flag, falling back to the server-wide
``expose_mcp_to_agents`` setting (the same switch that hands Claude agents the
MCP url today).
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

# The tools a worker agent may call. A worker builds and inspects structure
# below its own node (create_plan with parent_plan_id/parent_node_id, add_node,
# add_edge, update_node, get_plan) and narrates (report_progress).
# Deliberately NOT exposed:
# - start_plan / stop_plan: the engine schedules plans; a subplan attached to a
#   running node is started by the engine, and an agent stopping plans could
#   kill its own run or its siblings'.
# - claim_next_node / complete_node / fail_node: the external-agent protocol.
#   The engine owns a built-in node's lifecycle and finishes it from the
#   harness's "result" event; an agent completing (or claiming) nodes itself
#   would race that and corrupt the state machine.
# - list_plans / remove_node / remove_edge: not needed to expand a node, and
#   removal could delete work outside the agent's own subtree.
ALLOWED_TOOLS = ("create_plan", "add_node", "add_edge", "get_plan", "update_node", "report_progress")

REPORT_PROGRESS_NOTE = (
    " Note: your messages and tool calls are already recorded in the node's transcript;"
    " use this only to narrate long-running work."
)

DEFAULT_TIMEOUT_S = 60.0


def mcp_enabled(tools: dict[str, Any] | None, default: bool) -> bool:
    """Whether a profile's agents get the bridge: ``tools.mcp`` if set, else ``default``.

    ``default`` is the server's ``expose_mcp_to_agents`` setting.
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
    """The allowlisted clearbox MCP tools of one server, as ``ToolSpec``s.

    ``vc_report_progress`` duplicates what the harness already emits: every
    text/tool_use/tool_result event of the agent is recorded on the node anyway,
    so a progress message shows up next to them. It is exposed regardless,
    because it is how an agent narrates long tool work; the tool description
    tells the model to use it sparingly.
    """

    def __init__(self, url: str, timeout_s: float = DEFAULT_TIMEOUT_S, allowed: tuple[str, ...] = ALLOWED_TOOLS):
        self.url = url
        self.timeout_s = timeout_s
        self.allowed = allowed
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
        """The allowlisted tools, fetched once and cached. Empty if the server is unreachable.

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
            by_name = {t.name: t for t in tools}
            self._specs = [self._spec(by_name[n]) for n in self.allowed if n in by_name]
            return self._specs

    def _spec(self, tool: Any) -> ToolSpec:
        name = tool.name
        description = inspect.cleandoc(tool.description or tool.title or name)
        if name == "report_progress":
            description += REPORT_PROGRESS_NOTE
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
        if bare not in self.allowed:
            avail = ", ".join(PREFIX + n for n in self.allowed)
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
