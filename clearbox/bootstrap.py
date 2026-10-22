"""Startup bootstrap: built-in agent profiles, the server default, the banner line.

A fresh install has an empty ``agent_profiles`` table, yet the operator should
be able to pick a harness straight from the command line without opening the UI
or calling an MCP tool first. So on every start the server *ensures* a few
profiles exist -- creating them only when absent, never overwriting what an
operator edited since -- and then resolves the server-wide default profile.

- ``claude`` (claude-sdk) and ``mock`` always exist. The ``claude`` profile
  stores no model / permission mode / max turns of its own: empty fields fall
  back to ``--model`` / ``--permission-mode`` / ``--max-turns`` at run time, so
  those flags keep working on every restart exactly as before profiles existed.
  An operator who sets a value on the profile overrides the flag.
- ``openai`` is created only when an endpoint is configured (``--openai-base-url``,
  ``OPENAI_BASE_URL`` or ``OPENAI_API_KEY``). Its key is referenced by env var
  name only; the value is never stored, logged or printed.

The default is ``--agent-profile`` when given (an unknown name is a fatal
startup error: silently running a different model is worse than not starting),
else ``mock`` with ``--mock``, else ``claude``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .harness.base import HarnessError
from .harness.openai_compat import DEFAULT_BASE_URL, is_local_url
from .harness.registry import HarnessRegistry
from .store import PlanError, Store, public_profile

log = logging.getLogger("clearbox")

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"  # only for the real OpenAI endpoint; other servers name their own models


class StartupError(RuntimeError):
    """Configuration problem that should stop the server with a clear message (no traceback)."""


@dataclass
class OpenAiBootstrap:
    """``--openai-*`` flags: used only to create the built-in ``openai`` profile."""

    base_url: str | None = None
    model: str | None = None
    api_key_env: str = "OPENAI_API_KEY"

    def wanted(self) -> bool:
        return bool(self.base_url or os.environ.get(self.api_key_env or "OPENAI_API_KEY"))


def ensure_profiles(store: Store, openai: OpenAiBootstrap) -> None:
    """Create the built-in profiles that are missing. Idempotent; never touches existing rows."""
    if store.get_profile_by_name("claude") is None:
        store.create_profile(
            name="claude", harness="claude-sdk",
            description="Claude Agent SDK; empty model/permission mode/max turns follow the server's CLI flags",
        )
    if store.get_profile_by_name("mock") is None:
        store.create_profile(name="mock", harness="mock", description="fake agent for trying out the UI (no cost)")
    existing = store.get_profile_by_name("openai")
    if existing is None and openai.wanted():
        model = openai.model or (None if openai.base_url else DEFAULT_OPENAI_MODEL)
        store.create_profile(
            name="openai", harness="openai", model=model, base_url=openai.base_url,
            api_key_env=openai.api_key_env or None,
            description="OpenAI-compatible endpoint bootstrapped from --openai-* flags",
        )
        if not model:
            log.warning("agent profile 'openai' has no model; pass --openai-model (e.g. qwen2.5-coder) "
                        "or set one in the Agents panel")
        return
    if existing is None:
        return
    differs = [
        flag for flag, given, stored in (
            ("--openai-base-url", openai.base_url, existing["base_url"]),
            ("--openai-model", openai.model, existing["model"]),
        )
        if given and given != stored
    ]
    if differs:
        log.warning("agent profile 'openai' already exists; %s ignored (edit the profile instead)", ", ".join(differs))


def resolve_default(store: Store, agent_profile: str | None, mock: bool) -> dict[str, Any]:
    """The raw row of the server-wide default profile."""
    name = agent_profile or ("mock" if mock else "claude")
    try:
        return store.resolve_profile_ref(name)
    except PlanError:
        names = ", ".join(p["name"] for p in store.list_profiles()) or "none"
        raise StartupError(f"--agent-profile {name!r}: no such agent profile (available: {names})") from None


def credential_missing(profile: dict[str, Any]) -> str | None:
    """For an openai profile without a usable key: the env var to export, else None."""
    if profile["harness"] != "openai":
        return None
    if public_profile(profile)["has_api_key"]:
        return None
    base_url = profile.get("base_url")
    if base_url and is_local_url(base_url):
        return None  # local servers rarely check keys; the harness sends a placeholder
    if not base_url and os.environ.get("OPENAI_API_KEY"):
        return None
    return profile.get("api_key_env") or "OPENAI_API_KEY"


def validate(registry: HarnessRegistry, store: Store, default: dict[str, Any]) -> None:
    """Fail fast if the default profile cannot build; only warn about missing credentials."""
    for p in store.list_profiles():
        env_name = credential_missing(p)
        if env_name:
            log.warning("agent profile %r: no api key (environment variable %s is not set); "
                        "its nodes will fail until it is exported", p["name"], env_name)
    try:
        registry.harness_for(default)
    except HarnessError as e:
        if not credential_missing(default):
            raise StartupError(f"default agent profile {default['name']!r} is misconfigured: {e}") from None
        # Already warned: the key may be exported later. Check everything else with a
        # placeholder key (built uncached, never used for a request).
        try:
            registry._build({**default, "api_key": "placeholder"})
        except HarnessError as e2:
            raise StartupError(f"default agent profile {default['name']!r} is misconfigured: {e2}") from None


def _endpoint(base_url: str | None) -> str:
    """host[:port] only -- never userinfo, path or query."""
    u = urlsplit(base_url or DEFAULT_BASE_URL)
    return (u.hostname or "?") + (f":{u.port}" if u.port else "")


def agents_line(registry: HarnessRegistry, store: Store, default_id: int | None) -> str:
    """e.g. ``claude* (claude-sdk), mock, openai (gpt-4o-mini @ localhost:11434)``; * marks the default."""
    parts = []
    for p in store.list_profiles():
        star = "*" if p["id"] == default_id else ""
        model = registry.describe(p)["model"]  # claude-sdk profiles fall back to --model
        if p["harness"] == "mock":
            detail = ""
        elif p["harness"] == "openai":
            detail = f" ({model or 'no model'} @ {_endpoint(p['base_url'])})"
        else:
            detail = f" ({p['harness']}" + (f" / {model}" if model else "") + ")"
        parts.append(f"{p['name']}{star}{detail}")
    return ", ".join(parts)
