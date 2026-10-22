"""Fake harness for trying out the UI without spending tokens."""

from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator

from .base import AgentJob


class MockHarness:
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
