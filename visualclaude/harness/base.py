"""The contract every agent harness implements.

A harness turns one ``AgentJob`` (a plan node to work on) into a stream of
transcript events. The engine stores those events, fans them out over SSE and
the UI renders them, all without knowing which harness produced them. That is
why the event contract below is the integration seam: a new harness (OpenAI-
compatible, local model, ...) only has to emit the same kinds and payload keys
as the Claude Agent SDK harness, and the rest of VisualClaude just works.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol, runtime_checkable

MAX_TOOL_RESULT_CHARS = 6000


@dataclass
class AgentJob:
    plan_id: int
    node_id: int
    prompt: str
    system_append: str
    cwd: str
    # The public agent-profile row (api key redacted) that selected this harness,
    # or None when the server-wide CLI defaults apply.
    profile: dict[str, Any] | None = None
    node_title: str = ""


class HarnessError(RuntimeError):
    """A harness is misconfigured (missing api key, unknown model, bad base_url).

    Raised for problems that exist before any agent turn happens, so callers can
    tell "this profile cannot work" apart from "the agent tried and failed",
    which is reported in-band as a ``result`` with ``is_error=True``.
    """


@runtime_checkable
class Harness(Protocol):
    """Runs one agent session for one plan node.

    ``run(job)`` is an async generator yielding ``(kind, data)`` tuples. ``data``
    must be JSON-serialisable (it is stored in the events table and sent over
    SSE). It must never contain secrets (api keys, auth headers).

    Event kinds and their payload keys (exactly as ``ClaudeSdkHarness`` emits
    them; keys marked optional may be omitted):

    ``"system"``
        Session lifecycle notes. ``{"text": str, "session_id": str | None,
        "model": str | None (optional), "cwd": str | None (optional)}``.
        The first one should be ``text="session started"``; if it carries a
        ``session_id`` the engine records it on the node. The UI shows
        ``text`` and, if present, ``(model)``.

    ``"text"``
        Assistant prose. ``{"text": str, "parent_tool_use_id": str | None}``.
        Rendered as markdown. Do not emit whitespace-only text.

    ``"thinking"``
        Reasoning/thinking content. ``{"text": str, "parent_tool_use_id":
        str | None}``. Do not emit whitespace-only thinking.

    ``"tool_use"``
        The agent calls a tool. ``{"id": str, "name": str, "input": dict,
        "parent_tool_use_id": str | None}``. ``id`` must be unique within the
        run; the matching ``tool_result`` refers to it.

    ``"tool_result"``
        Outcome of a tool call. ``{"tool_use_id": str, "content": str,
        "is_error": bool, "parent_tool_use_id": str | None}``. ``content`` is
        plain text passed through ``_truncate(_tool_result_text(...))`` so
        every harness caps results at ``MAX_TOOL_RESULT_CHARS``.

    ``"error"``
        A non-fatal problem worth showing in the transcript. ``{"text": str}``.
        (The engine itself emits ``error`` when ``run`` raises.)

    ``parent_tool_use_id`` is the id of the enclosing ``tool_use`` when the
    event comes from a sub-agent, else ``None``; the UI indents such events.

    Terminal event, exactly one, always last, even on failure:

    ``"result"``
        ``{"text": str, "is_error": bool, ...}``. ``text`` is the final summary
        handed to dependent nodes as the node's result. When ``is_error`` is
        true, ``errors: list[str]`` should explain why (the engine joins it
        into the node's error, falling back to ``text``). Optional extra keys
        the UI understands: ``num_turns: int``, ``duration_ms: int``,
        ``cost_usd: float | None``; ``ClaudeSdkHarness`` also sends
        ``subtype`` and ``session_id``.

    A run that ends without its own result (stream closed early, max turns hit,
    ...) must still yield ``("result", {"text": "", "is_error": True, "errors":
    [...]})``. Raising out of ``run`` is reserved for unexpected bugs and
    ``HarnessError``; the engine then marks the node failed. Cancellation
    (``asyncio.CancelledError``) must be allowed to propagate.
    """

    def run(self, job: AgentJob) -> AsyncIterator[tuple[str, dict[str, Any]]]: ...


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
