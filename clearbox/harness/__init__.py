"""Agent harnesses: the interchangeable runtimes that execute plan nodes.

Every harness implements the ``Harness`` protocol from ``base`` and emits the
same transcript events, so the engine, store and UI stay harness-agnostic.
``HARNESSES`` maps the slug stored in an agent profile's ``harness`` column to
the class implementing it.
"""

from __future__ import annotations

from .base import MAX_TOOL_RESULT_CHARS, AgentJob, Harness, HarnessError
from .claude_sdk import ClaudeSdkHarness
from .mock import MockHarness
from .openai_compat import OpenAiHarness

HARNESSES: dict[str, type] = {
    "claude-sdk": ClaudeSdkHarness,
    "mock": MockHarness,
    "openai": OpenAiHarness,
}

__all__ = [
    "AgentJob",
    "ClaudeSdkHarness",
    "HARNESSES",
    "Harness",
    "HarnessError",
    "MAX_TOOL_RESULT_CHARS",
    "MockHarness",
    "OpenAiHarness",
]
