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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..store import PlanError, Store, public_profile
from .base import Harness, HarnessError
from .claude_sdk import ClaudeSdkHarness, _as_json
from .mock import MockHarness
from .openai_compat import OpenAiHarness


@dataclass
class HarnessDefaults:
    """Server-wide settings: the implicit harness and the values profiles fall back to."""

    agent: str = "claude"                # implicit harness when no profile applies: 'claude' | 'mock'
    permission_mode: str = "acceptEdits"
    model: str | None = None
    max_turns: int | None = None
    mcp_url: str | None = None           # None when expose_mcp_to_agents is off
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
        """Liveness check of a profile (id or name): the ONE implementation behind both
        ``POST /api/agents/{id}/test`` and the MCP ``test_agent`` tool.

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
                self._implicit = ClaudeSdkHarness(
                    permission_mode=d.permission_mode, model=d.model, max_turns=d.max_turns, mcp_url=d.mcp_url,
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
        d = self.defaults
        kind = profile["harness"]
        if kind == "claude-sdk":
            return ClaudeSdkHarness(
                permission_mode=d.permission_mode, model=d.model, max_turns=d.max_turns, mcp_url=d.mcp_url,
                profile=profile,
            )
        if kind == "openai":
            return OpenAiHarness(profile=profile, mcp_url=d.mcp_url)
        if kind == "mock":
            params = _as_json(profile.get("params"), {}) or {}
            return MockHarness(speed=float(params.get("speed") or d.mock_speed))
        raise HarnessError(f"agent profile {profile['name']!r}: unknown harness {kind!r}")

    def review_harness_for(self, profile: dict[str, Any]) -> Harness:
        """A harness for a review session: same model/endpoint as ``profile``, but forced
        read-only regardless of what the profile allows for normal node runs.

        A reviewer must never edit files, so its own ``tools``/``permission_mode`` and the
        clearbox MCP tools are ignored here: Claude Agent SDK sessions get ``permission_mode
        "plan"`` plus a fixed read-only tool list, and OpenAI-compatible ones get the
        ``"readonly"`` tools preset.
        """
        d = self.defaults
        kind = profile["harness"]
        if kind == "claude-sdk":
            return ClaudeSdkHarness(
                permission_mode="plan", model=profile.get("model") or d.model, max_turns=d.max_turns,
                mcp_url=None, allowed_tools=["Read", "Grep", "Glob"],
            )
        if kind == "openai":
            return OpenAiHarness(profile={**profile, "tools": {"preset": "readonly"}}, mcp_url=None)
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
