"""Backwards-compatible aliases for code that still imports from ``visualclaude.agents``."""

from __future__ import annotations

# The implementations moved to the visualclaude.harness package.
from .harness import AgentJob, ClaudeSdkHarness, MockHarness
from .harness.base import MAX_TOOL_RESULT_CHARS, _tool_result_text, _truncate

ClaudeBackend = ClaudeSdkHarness
MockBackend = MockHarness

__all__ = ["AgentJob", "ClaudeBackend", "MockBackend", "MAX_TOOL_RESULT_CHARS", "_tool_result_text", "_truncate"]
