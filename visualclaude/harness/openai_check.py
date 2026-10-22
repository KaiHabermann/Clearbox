"""Cheap, model-free check of an 'openai' agent profile: list the endpoint's models.

``probe.py`` answers "does this agent reply?" with a real (tiny) completion.
A planning model mostly wants something cheaper and more specific: is the
endpoint up, does it know the configured model name (a misspelt Ollama tag is
the most common mistake), and where does the key come from. ``GET /models``
answers all three without spending tokens; only endpoints without a model
listing fall back to the completion round trip of ``probe_profile``.

Nothing here raises, and the report names the credential *source*
(``env:OPENAI_API_KEY`` / ``literal`` / ``none``), never the key; error texts are
scrubbed of it by the harness.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from .base import HarnessError
from .openai_compat import DEFAULT_BASE_URL, OpenAiHarness, _host

if TYPE_CHECKING:
    from .registry import HarnessRegistry

CHECK_TIMEOUT_S = 10.0
SAMPLE_SIZE = 10


def credential_source(profile: dict[str, Any]) -> str:
    """Where the openai harness takes its key from; mirrors ``OpenAiHarness._resolve_key``."""
    env_name = (profile.get("api_key_env") or "").strip()
    if env_name and os.environ.get(env_name):
        return f"env:{env_name}"
    if profile.get("api_key"):
        return "literal"
    if not profile.get("base_url") and os.environ.get("OPENAI_API_KEY"):
        return "env:OPENAI_API_KEY"
    return "none"


def _model_listed(model: str, ids: list[str]) -> bool:
    # Ollama lists "llama3.1:latest" but accepts "llama3.1"
    return model in ids or f"{model}:latest" in ids


async def check_openai_profile(
    registry: HarnessRegistry, profile: dict[str, Any], timeout: float = CHECK_TIMEOUT_S
) -> dict[str, Any]:
    """-> ``{ok, detail, reachable, model_listed, models_sample, credential, ...}`` for a raw profile row."""
    base_url = (profile.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    out: dict[str, Any] = {
        "ok": False,
        "detail": "",
        "agent": registry.describe(profile),
        "base_url": _host(base_url),
        "credential": credential_source(profile),
        "reachable": False,
        "model_listed": None,
        "models_sample": [],
    }
    try:
        h = OpenAiHarness(profile=profile)  # validates model, base_url, tools preset and the key
    except HarnessError as e:
        out["detail"] = f"misconfigured: {e}"
        return out

    import openai

    client = openai.AsyncOpenAI(base_url=h.base_url, api_key=h._api_key, timeout=timeout, max_retries=0)
    try:
        page = await asyncio.wait_for(client.models.list(), timeout)
        ids = sorted(str(m.id) for m in page.data)
    except openai.NotFoundError:
        ids = None  # no /models on this endpoint
    except (asyncio.TimeoutError, openai.APITimeoutError):
        out["detail"] = f"no answer from {_host(h.base_url)} within {timeout:g}s"
        return out
    except openai.APIStatusError as e:
        out["reachable"] = True
        out["detail"] = h._explain(e)[1]
        return out
    except Exception as e:  # noqa: BLE001 -- connection errors and the unexpected: report, don't raise
        out["detail"] = h._explain(e)[1] if isinstance(e, openai.APIError) else h._scrub(f"{type(e).__name__}: {e}")[:400]
        return out
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass

    out["reachable"] = True
    where = _host(h.base_url)
    if ids is None:
        from .probe import probe_profile

        ping = await probe_profile(registry, profile, timeout)
        out["ok"] = ping["ok"]
        out["detail"] = (
            f"{where} has no /models listing, but a tiny completion with {h.model!r} succeeded"
            if ping["ok"] else f"{where} has no /models listing and a tiny completion failed: {ping['error']}"
        )
        return out
    listed = _model_listed(h.model, ids)
    sample = ids[:SAMPLE_SIZE]
    if h.model in ids and h.model not in sample:
        sample = [h.model, *sample[: SAMPLE_SIZE - 1]]
    out.update(model_listed=listed, models_sample=sample, ok=listed)
    if listed:
        out["detail"] = f"{where} is reachable and offers model {h.model!r} ({len(ids)} models listed)"
    else:
        out["detail"] = (
            f"{where} is reachable but does not list model {h.model!r} ({len(ids)} models listed, see "
            "models_sample); check the profile's model name"
        )
    return out
