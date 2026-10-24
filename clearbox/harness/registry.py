"""Picks the harness that runs a node.

The engine used to hold one global backend. Now every node may name a
different agent profile (directly, or through its plan chain), so the engine
asks the registry per node. Resolution order:

1. ``store.profile_for_node`` -- the node's profile, its plan's default, then
   each ancestor plan's default;
2. the server-wide default profile (``default_profile``, an id or name);
3. the implicit harness configured on the command line (``claude`` or, with
   ``--mock``, ``mock``) -- exactly what Clearbox did before profiles existed.

Harness instances are cached per profile id together with the row they were
built from; any edit (which also bumps ``updated_at``) makes the row differ, so
the next node picks up the change without a restart, while an unchanged profile
does not pay construction cost per node. The whole row is compared rather than
just ``updated_at`` because two edits can land in the same clock tick on Windows.
A misconfigured profile raises ``HarnessError`` from ``resolve``; the engine
turns that into a failed node with the message as its error.

Capability
----------
A profile's ``capability`` says what its agent may do, and means the same thing
for every harness:

    read   read and search only; no edits, no commands
    run    read, search and run commands; no edits
    edit   read, search and edit files; no commands
    full   everything

The registry maps it to each harness's own gating (``claude_access`` and
``openai_tools``). ``full`` means the agent can run arbitrary commands in the
plan's working directory; that is the operator's choice, made when they pick the
profile. When a profile sets no capability the default is ``full`` for worker
agents and ``run`` for reviewers (a reviewer must run the suite but must not
edit). A profile's raw ``permission_mode`` (claude-sdk) or ``tools`` config
(openai) is still honoured as an escape hatch when set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..store import CAPABILITIES, PlanError, Store, public_profile
from .base import Harness, HarnessError
from .claude_sdk import ClaudeSdkHarness, _as_json
from .mock import MockHarness
from .openai_compat import OpenAiHarness

DEFAULT_CAPABILITY = "full"         # worker agents
DEFAULT_REVIEW_CAPABILITY = "run"   # reviewers

_READ_TOOLS = ["Read", "Grep", "Glob"]
_OPENAI_WRITE_TOOLS = ["write_file", "edit_file"]


def _check_capability(capability: str, profile_name: str) -> str:
    if capability not in CAPABILITIES:
        raise HarnessError(
            f"agent profile {profile_name!r}: unknown capability {capability!r}; expected one of {', '.join(CAPABILITIES)}")
    return capability


def claude_access(capability: str, fallback_mode: str) -> tuple[str, list[str]]:
    """``(permission_mode, allowed_tools)`` for the Claude Agent SDK.

    Gating is done with ``allowed_tools``: anything listed is pre-approved and runs
    without a prompt. Below ``full`` the mode is ``dontAsk``, which denies every tool
    that is not pre-approved instead of prompting -- an SDK session has nobody to
    answer a prompt, so any prompting mode would silently block the tool. ``full``
    pre-approves every file and shell tool and keeps ``fallback_mode`` (the server's
    ``--permission-mode``) for whatever else the model reaches for.
    """
    tools = list(_READ_TOOLS)
    if capability in ("run", "full"):
        tools.append("Bash")
    if capability in ("edit", "full"):
        tools += ["Write", "Edit", "MultiEdit"]
    return (fallback_mode if capability == "full" else "dontAsk"), tools


def openai_tools(capability: str, tools_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``tools`` config for an OpenAI-compatible profile: the existing preset
    plus allow list. Only the ``mcp`` flag survives from ``tools_config``; the bridge
    stays governed by it, not by the capability."""
    if capability == "full":
        out: dict[str, Any] = {"preset": "all"}
    else:
        out = {"preset": "readonly"}
        allow = (["bash"] if capability == "run" else []) + (_OPENAI_WRITE_TOOLS if capability == "edit" else [])
        if allow:
            out["allow"] = allow
    mcp = (tools_config or {}).get("mcp")
    if mcp is not None:
        out["mcp"] = mcp
    return out


@dataclass
class HarnessDefaults:
    """Server-wide settings: the implicit harness and the values profiles fall back to."""

    agent: str = "claude"                # implicit harness when no profile applies: 'claude' | 'mock'
    permission_mode: str = "acceptEdits"
    model: str | None = None
    max_turns: int | None = None
    mock_speed: float = 1.0
    default_profile: int | str | None = None  # id or name of the server-wide default profile


class HarnessRegistry:
    """Resolves (plan_id, node_id) to the agent profile and harness that run it."""

    def __init__(self, store: Store, defaults: HarnessDefaults | None = None):
        self.store = store
        self.defaults = defaults or HarnessDefaults()
        self._cache: dict[int, tuple[dict[str, Any], Harness]] = {}  # profile id -> (row, harness)
        self._implicit: Harness | None = None

    # --------------------------------------------------------------- resolving
    def profile_for(self, plan_id: int, node_id: int) -> dict[str, Any] | None:
        """The raw profile row (api_key included) for a node, or None for the implicit harness."""
        profile = self.store.profile_for_node(plan_id, node_id)
        if profile is None and self.defaults.default_profile is not None:
            try:
                profile = self.store.resolve_profile_ref(self.defaults.default_profile)
            except PlanError as e:
                raise HarnessError(f"server default agent profile {self.defaults.default_profile!r} does not exist") from e
        return profile

    def resolve(self, plan_id: int, node_id: int) -> tuple[dict[str, Any] | None, Harness]:
        """The public profile row (no secrets; None = implicit harness) and the harness to run."""
        profile = self.profile_for(plan_id, node_id)
        if profile is None:
            return None, self.implicit()
        return public_profile(profile), self.harness_for(profile)

    def source_for(self, plan_id: int, node_id: int) -> dict[str, Any]:
        """Which agent would run a node and why, for the UI (never raises, no secrets).

        ``{name, harness, model, profile_id, source}`` where ``source`` is
        ``"node"`` (the node's own override), ``"plan"`` (its plan's default),
        ``"inherited"`` (an ancestor plan's default) or ``"server-default"`` (the
        server-wide default profile, or the CLI harness when there is none).
        ``from_plan`` is the plan whose default applied ("plan"/"inherited" only),
        so the UI can say "inherited from plan N".
        A dangling server default adds ``error`` instead of failing the request.
        """
        store = self.store
        node = store.get_node(plan_id, node_id)
        plan = store.get_plan(plan_id)
        ref, source = node.get("agent_profile_id"), "node"
        if ref is None:
            ref, source = plan.get("default_agent_profile_id"), "plan"
        while ref is None and plan["parent_plan_id"] is not None:
            plan = store.get_plan(plan["parent_plan_id"])
            ref, source = plan.get("default_agent_profile_id"), "inherited"
        profile = None
        if ref is not None:
            try:
                profile = store.get_profile(ref)
            except PlanError:
                profile = None  # dangling reference in a hand-edited db: fall through like profile_for_node
        error = None
        if profile is None:
            source = "server-default"
            try:
                profile = self.profile_for(plan_id, node_id)
            except HarnessError as e:
                error = str(e)
        out = {**self.describe(profile), "source": source}
        if source in ("plan", "inherited"):
            out["from_plan"] = plan["id"]
        if error:
            out["error"] = error
        return out

    async def test(self, ref: int | str) -> dict[str, Any]:
        """Liveness check of a profile (id or name): the implementation behind
        ``POST /api/agents/{id}/test``.

        Raises ``PlanError`` for an unknown profile; otherwise never raises and returns
        ``{ok, detail, agent, models_sample, ...}`` (see ``openai_check``). 'openai'
        profiles are checked against their endpoint; 'claude-sdk' and 'mock' need no check.
        """
        from .openai_check import check_openai_profile

        profile = self.store.resolve_profile_ref(ref)
        if profile["harness"] != "openai":
            return {
                "ok": True,
                "detail": f"no check needed for harness {profile['harness']!r}",
                "agent": self.describe(profile),
                "models_sample": [],
            }
        return await check_openai_profile(self, profile)

    def implicit(self) -> Harness:
        if self._implicit is None:
            d = self.defaults
            if d.agent == "mock":
                self._implicit = MockHarness(speed=d.mock_speed)
            else:
                mode, tools = claude_access(DEFAULT_CAPABILITY, d.permission_mode)
                self._implicit = ClaudeSdkHarness(
                    permission_mode=mode, model=d.model, max_turns=d.max_turns, allowed_tools=tools,
                )
        return self._implicit

    def harness_for(self, profile: dict[str, Any]) -> Harness:
        """A harness for a raw profile row, built once per profile version."""
        hit = self._cache.get(profile["id"])
        if hit is not None and hit[0] == profile:
            return hit[1]
        harness = self._build(profile)  # HarnessError propagates and is not cached
        self._cache[profile["id"]] = (dict(profile), harness)
        return harness

    def _build(self, profile: dict[str, Any]) -> Harness:
        """A harness for a normal node run. The profile's ``capability`` (default
        ``full``: the agent can run arbitrary commands in the plan's working directory,
        the operator's choice) sets the tool access; an explicit ``permission_mode`` or
        ``tools`` on the profile still overrides it as an escape hatch."""
        d = self.defaults
        kind = profile["harness"]
        capability = _check_capability(profile.get("capability") or DEFAULT_CAPABILITY, profile["name"])
        if kind == "claude-sdk":
            mode, tools = claude_access(capability, d.permission_mode)
            return ClaudeSdkHarness(  # the profile's own permission_mode / tools win inside, when set
                permission_mode=mode, model=d.model, max_turns=d.max_turns,
                allowed_tools=tools, profile=profile,
            )
        if kind == "openai":
            if profile.get("capability"):
                profile = {**profile, "tools": openai_tools(capability, _as_json(profile.get("tools"), {}))}
            return OpenAiHarness(profile=profile)
        if kind == "mock":
            params = _as_json(profile.get("params"), {}) or {}
            return MockHarness(speed=float(params.get("speed") or d.mock_speed))
        raise HarnessError(f"agent profile {profile['name']!r}: unknown harness {kind!r}")

    def review_harness_for(self, profile: dict[str, Any]) -> Harness:
        """A harness for a review session: same model/endpoint as ``profile``, but with
        capability ``run`` regardless of what the profile allows for normal node runs.

        A reviewer has to run the test suite but must never edit files, so its own
        ``capability``/``tools``/``permission_mode`` and the clearbox MCP tools are ignored
        here. A profile can set ``params.review_capability`` to choose otherwise. The prompt
        forbids using the shell to change anything, and the node's diff would show it if one
        tried.
        """
        d = self.defaults
        kind = profile["harness"]
        params = _as_json(profile.get("params"), {}) or {}
        capability = _check_capability(params.get("review_capability") or DEFAULT_REVIEW_CAPABILITY, profile["name"])
        if kind == "claude-sdk":
            mode, tools = claude_access(capability, d.permission_mode)
            return ClaudeSdkHarness(
                permission_mode=mode, model=profile.get("model") or d.model, max_turns=d.max_turns,
                allowed_tools=tools,
            )
        if kind == "openai":
            return OpenAiHarness(
                profile={**profile, "tools": openai_tools(capability)})
        if kind == "mock":
            params = _as_json(profile.get("params"), {}) or {}
            return MockHarness(speed=float(params.get("speed") or d.mock_speed))
        raise HarnessError(f"agent profile {profile['name']!r}: unknown harness {kind!r}")

    # ------------------------------------------------------------- describing
    def agent_name(self, profile: dict[str, Any] | None) -> str:
        """What the UI shows as the node's agent: the profile name, or 'claude'/'mock'."""
        if profile is not None:
            return profile["name"]
        return "mock" if self.defaults.agent == "mock" else "claude"

    def describe(self, profile: dict[str, Any] | None) -> dict[str, Any]:
        """Name, harness and model of the agent, for the "agent: ..." transcript line."""
        if profile is None:
            harness = "mock" if self.defaults.agent == "mock" else "claude-sdk"
            model = None if harness == "mock" else self.defaults.model
            return {"name": self.agent_name(None), "harness": harness, "model": model, "profile_id": None}
        model = profile.get("model")
        if not model and profile["harness"] == "claude-sdk":
            model = self.defaults.model
        return {"name": profile["name"], "harness": profile["harness"], "model": model, "profile_id": profile["id"]}

    def label(self, profile: dict[str, Any] | None) -> str:
        """e.g. ``agent: gpt-cheap (openai / gpt-4o-mini)`` or ``agent: claude (claude-sdk / default model)``."""
        a = self.describe(profile)
        if a["harness"] == "mock":
            return f"agent: {a['name']} (mock)"
        return f"agent: {a['name']} ({a['harness']} / {a['model'] or 'default model'})"
