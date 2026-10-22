"""Connection test for an agent profile ("does this agent answer at all?").

A real (tiny) completion round trip. ``HarnessRegistry.test`` -- the shared
implementation of ``POST /api/agents/{id}/test`` and the MCP ``test_agent`` tool --
uses it (via ``openai_check``) for endpoints without a ``/models`` listing.

The probe is one tiny, tool-less round trip that asks the model to reply "OK":

- ``openai``: a single non-streamed chat completion with the profile's model,
  url, key and params, no retries. Errors carry the same actionable wording a
  failed node would show (wrong /v1 path, 401, unknown model, ...).
- ``claude-sdk``: a one-turn Claude Agent SDK session in a throwaway directory,
  without tools or the clearbox MCP server. This checks that the CLI starts
  and is logged in, and costs a few tokens.
- ``mock``: always fine, instantly.

Configuration errors (no model, missing env var, bad base_url) surface without
any network traffic. The result never contains the api key: it carries the
public agent description and, at most, the first 200 characters of the reply.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from typing import TYPE_CHECKING, Any

from .base import AgentJob, HarnessError
from .claude_sdk import ClaudeSdkHarness
from .openai_compat import OpenAiHarness

if TYPE_CHECKING:
    from .registry import HarnessRegistry

PROMPT = "This is a connection test. Reply with the single word OK and nothing else."
DEFAULT_TIMEOUT_S = 60.0
MAX_REPLY_CHARS = 200


async def probe_profile(
    registry: HarnessRegistry, profile: dict[str, Any], timeout: float = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """Test a raw profile row -> ``{ok, agent, latency_ms, reply, error}``.

    ``agent`` is ``registry.describe`` of the profile ({name, harness, model,
    profile_id}). ``ok`` is True when the endpoint answered at all; ``reply``
    is what it said (a model that ignores the instruction still counts as up).
    """
    out: dict[str, Any] = {"ok": False, "agent": registry.describe(profile), "latency_ms": None, "reply": None, "error": None}
    t0 = time.monotonic()
    try:
        reply = await asyncio.wait_for(_round_trip(registry, profile, timeout), timeout)
    except asyncio.TimeoutError:
        out["error"] = f"no answer within {timeout:.0f}s"
    except HarnessError as e:
        out["error"] = str(e)
    except Exception as e:  # noqa: BLE001 -- a test reports, it never raises
        out["error"] = f"{type(e).__name__}: {e}"[:600]
    else:
        out["ok"] = True
        out["reply"] = (reply or "").strip()[:MAX_REPLY_CHARS]
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out


async def _round_trip(registry: HarnessRegistry, profile: dict[str, Any], timeout: float) -> str:
    kind = profile["harness"]
    if kind == "mock":
        return "OK"
    if kind == "openai":
        harness = OpenAiHarness(profile=profile, mcp_url=None)  # validates model / url / key first
        return await harness.ping(PROMPT, timeout)
    if kind == "claude-sdk":
        d = registry.defaults
        harness = ClaudeSdkHarness(permission_mode="default", model=d.model, profile=profile)
        harness.mcp_url = None
        harness.allowed_tools = []
        harness.max_turns = 1
        with tempfile.TemporaryDirectory(prefix="vc-probe-") as cwd:
            job = AgentJob(plan_id=0, node_id=0, prompt=PROMPT, system_append="", cwd=cwd, node_title="connection test")
            async for kind_, data in harness.run(job):
                if kind_ == "result":
                    if data.get("is_error"):
                        errors = data.get("errors") or [data.get("subtype") or "error"]
                        raise HarnessError(f"claude session failed: {'; '.join(map(str, errors))} {data.get('text') or ''}".strip())
                    return str(data.get("text") or "")
        raise HarnessError("claude session ended without a result")
    raise HarnessError(f"agent profile {profile['name']!r}: unknown harness {kind!r}")
