"""Agent backends used by the built-in runner.

A backend's ``run`` is an async generator yielding ``(kind, data)`` tuples.
It must finish by yielding exactly one ``("result", {...})`` item whose data
contains at least ``text`` (summary handed to dependent nodes) and
``is_error``.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any, AsyncIterator

MAX_TOOL_RESULT_CHARS = 6000


@dataclass
class AgentJob:
    plan_id: int
    node_id: int
    prompt: str
    system_append: str
    cwd: str


def _truncate(s: str, n: int = MAX_TOOL_RESULT_CHARS) -> str:
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more chars]"


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(json.dumps(item, default=str)[:500])
    return "\n".join(parts)


class ClaudeBackend:
    """Runs a real Claude agent session through the Claude Agent SDK."""

    def __init__(
        self,
        permission_mode: str = "acceptEdits",
        model: str | None = None,
        max_turns: int | None = None,
        mcp_url: str | None = None,
        allowed_tools: list[str] | None = None,
    ):
        self.permission_mode = permission_mode
        self.model = model
        self.max_turns = max_turns
        self.mcp_url = mcp_url
        self.allowed_tools = allowed_tools or []

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

        options = ClaudeAgentOptions(
            cwd=job.cwd,
            permission_mode=self.permission_mode,  # type: ignore[arg-type]
            system_prompt={"type": "preset", "preset": "claude_code", "append": job.system_append},
            mcp_servers=mcp_servers,
            allowed_tools=allowed,
            model=self.model,
            max_turns=self.max_turns,
        )

        got_result = False
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
                got_result = True
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
        if not got_result:
            yield "result", {"text": "", "is_error": True, "errors": ["agent ended without a result"]}


class MockBackend:
    """Fake agent for trying out the UI without spending tokens."""

    def __init__(self, speed: float = 1.0):
        self.speed = speed

    async def _sleep(self, lo: float, hi: float) -> None:
        await asyncio.sleep(random.uniform(lo, hi) / self.speed)

    async def run(self, job: AgentJob) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        title = job.prompt.split("Your node:", 1)[-1].splitlines()[0].strip() if "Your node:" in job.prompt else "node"
        yield "system", {"text": "mock session started", "session_id": f"mock-{job.plan_id}-{job.node_id}", "cwd": job.cwd}
        await self._sleep(0.5, 1.5)
        yield "thinking", {"text": f"Let me figure out how to approach {title}."}
        await self._sleep(0.5, 1.5)
        yield "text", {"text": f"I'll start working on **{title}**. First I'll look around the workspace."}
        for i in range(random.randint(1, 3)):
            await self._sleep(0.7, 2.0)
            tid = f"toolu_{job.plan_id}_{job.node_id}_{i}"
            tool = random.choice(["Bash", "Read", "Grep", "Write"])
            inp = {"Bash": {"command": "ls -la"}, "Read": {"file_path": "README.md"},
                   "Grep": {"pattern": "TODO"}, "Write": {"file_path": f"out_{job.node_id}.txt", "content": "..."}}[tool]
            yield "tool_use", {"id": tid, "name": tool, "input": inp}
            await self._sleep(0.5, 1.5)
            yield "tool_result", {"tool_use_id": tid, "content": f"(mock output of {tool})", "is_error": False}
        await self._sleep(0.5, 1.5)
        summary = f"Finished '{title}'. (mock result)"
        yield "text", {"text": summary}
        yield "result", {"text": summary, "is_error": False, "num_turns": 3, "cost_usd": 0.0}
