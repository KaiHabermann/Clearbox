"""Harness that runs a real Claude agent session through the Claude Agent SDK.

This is the original VisualClaude backend. Its settings can come from an agent
profile (when a node or plan selects one) or from the explicit constructor
arguments, which carry the CLI defaults; profile values win when present.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .base import AgentJob, _tool_result_text, _truncate


def _as_json(value: Any, default: Any) -> Any:
    """Profile columns like params/tools may arrive as JSON text or already decoded."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


class ClaudeSdkHarness:
    """Runs a real Claude agent session through the Claude Agent SDK."""

    def __init__(
        self,
        permission_mode: str = "acceptEdits",
        model: str | None = None,
        max_turns: int | None = None,
        mcp_url: str | None = None,
        allowed_tools: list[str] | None = None,
        profile: dict[str, Any] | None = None,
    ):
        self.permission_mode = permission_mode
        self.model = model
        self.max_turns = max_turns
        self.mcp_url = mcp_url
        self.allowed_tools = allowed_tools or []
        self.system_append = ""
        self.profile = profile
        if profile:
            params = _as_json(profile.get("params"), {}) or {}
            tools = _as_json(profile.get("tools"), None)
            self.permission_mode = profile.get("permission_mode") or self.permission_mode
            self.model = profile.get("model") or self.model
            if params.get("max_turns") is not None:
                self.max_turns = int(params["max_turns"])
            if tools:
                self.allowed_tools = list(tools)
            self.system_append = profile.get("system_append") or ""

    async def run(self, job: AgentJob) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
            query,
        )

        mcp_servers: dict[str, Any] = {}
        allowed = list(self.allowed_tools)
        if self.mcp_url:
            mcp_servers["visualclaude"] = {"type": "http", "url": self.mcp_url}
            allowed.append("mcp__visualclaude")

        system_append = job.system_append
        if self.system_append:
            system_append += "\n\n" + self.system_append

        options = ClaudeAgentOptions(
            cwd=job.cwd,
            permission_mode=self.permission_mode,  # type: ignore[arg-type]
            system_prompt={"type": "preset", "preset": "claude_code", "append": system_append},
            mcp_servers=mcp_servers,
            allowed_tools=allowed,
            model=self.model,
            max_turns=self.max_turns,
        )

        async for msg in query(prompt=job.prompt, options=options):
            if isinstance(msg, SystemMessage):
                if msg.subtype == "init":
                    yield "system", {
                        "text": "session started",
                        "session_id": msg.data.get("session_id"),
                        "model": msg.data.get("model"),
                        "cwd": msg.data.get("cwd"),
                    }
            elif isinstance(msg, AssistantMessage):
                sub = msg.parent_tool_use_id
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        if block.text.strip():
                            yield "text", {"text": block.text, "parent_tool_use_id": sub}
                    elif isinstance(block, ThinkingBlock):
                        if block.thinking.strip():
                            yield "thinking", {"text": block.thinking, "parent_tool_use_id": sub}
                    elif isinstance(block, ToolUseBlock):
                        yield "tool_use", {
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                            "parent_tool_use_id": sub,
                        }
            elif isinstance(msg, UserMessage):
                if isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            yield "tool_result", {
                                "tool_use_id": block.tool_use_id,
                                "content": _truncate(_tool_result_text(block.content)),
                                "is_error": bool(block.is_error),
                                "parent_tool_use_id": msg.parent_tool_use_id,
                            }
            elif isinstance(msg, ResultMessage):
                yield "result", {
                    "text": msg.result or "",
                    "is_error": bool(msg.is_error),
                    "subtype": msg.subtype,
                    "num_turns": msg.num_turns,
                    "duration_ms": msg.duration_ms,
                    "cost_usd": msg.total_cost_usd,
                    "session_id": msg.session_id,
                    "errors": msg.errors,
                }
                return  # terminal event: stop pulling, the SDK's stream may stay open afterwards
        yield "result", {"text": "", "is_error": True, "errors": ["agent ended without a result"]}
