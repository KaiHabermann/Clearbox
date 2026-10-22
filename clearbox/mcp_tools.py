"""MCP server exposing plan-building and agent-coordination tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .engine import ConflictCache, Engine
from .harness import HarnessError
from .store import PlanError, public_profile, validate_working_directory

INSTRUCTIONS = """
Clearbox lets you lay out a piece of work as a plan: a directed acyclic graph of nodes,
where every node is one piece of work. Edges point from a prerequisite to the node that
depends on it (from_node_id must finish before to_node_id starts). Execution walks the graph
from the roots to the leaves; independent nodes run in parallel.

Building a plan:
  1. create_plan(title, description) -> plan_id
  2. add_node(plan_id, title, description) -> [plan_id, node_id]   (one call per piece of work)
  3. add_edge(plan_id, from_node_id, to_node_id)                   (dependencies)
  A node can itself be a small plan: create_plan(..., parent_plan_id, parent_node_id) attaches
  a subplan to that node; the node then completes when its whole subplan is done.
  Write node descriptions so that an agent with no other context can do the work.
  Files: independent nodes run in parallel, so give each node files=[...] listing the paths it
  will CREATE or MODIFY (reads need no declaration). Two nodes that could run at the same time
  and declare the same file then run one after the other, lowest node number first, instead of
  clobbering each other; get_plan shows each node's `conflicts`.
  4. start_plan(plan_id, executor="builtin") lets the service run an agent per node,
     or executor="external" lets outside agents pick nodes up with claim_next_node.

Choosing agents (optional; without it every node runs on the server's default agent):
  An agent profile is a named harness + model: 'claude-sdk' (Claude Agent SDK), 'openai' (any
  OpenAI-compatible endpoint: OpenAI, OpenRouter, vLLM, Ollama, LM Studio, ...) or 'mock'.
  list_agents shows them, create_agent adds one, test_agent checks it answers. A plan may mix
  them per node: cheap/local models for mechanical nodes, strong models for the hard ones.
  add_node(..., agent="name") or set_node_agent picks a node's agent; set_plan_agent sets a
  plan-wide default. Resolution: node's agent -> its plan's default -> each ancestor plan's
  default -> the server default. get_plan shows the resolved agent of every node.

Working on nodes as an external agent:
  claim_next_node -> do the work -> report_progress (optional, repeatedly) -> complete_node / fail_node
""".strip()


def build_mcp(engine: Engine) -> MCPServer:
    mcp = MCPServer("clearbox", instructions=INSTRUCTIONS)
    store = engine.store

    registry = engine.harnesses

    def _profile_ref(agent: int | str) -> dict[str, Any]:
        """Raw profile row for an id or name (ToolError if unknown)."""
        try:
            return store.resolve_profile_ref(agent)
        except PlanError as e:
            raise ToolError(f"{e}; list_agents shows the configured agent profiles") from e

    def _profile_id(agent: int | str | None) -> int | None:
        return None if agent is None or agent == "" else _profile_ref(agent)["id"]

    def _default_profile_id() -> int | None:
        ref = registry.defaults.default_profile
        if ref is None:
            return None
        try:
            return store.resolve_profile_ref(ref)["id"]
        except PlanError:
            return None

    def _agent_view(profile: dict[str, Any]) -> dict[str, Any]:
        out = public_profile(profile)
        out["default"] = out["id"] == _default_profile_id()
        return out

    def _profile_name(profile_id: int | None) -> str | None:
        if profile_id is None:
            return None
        try:
            return store.get_profile(profile_id)["name"]
        except PlanError:
            return None

    def _resolved_agent(plan_id: int, node_id: int) -> dict[str, Any]:
        """Name, harness and model of the agent that would run this node right now."""
        try:
            profile = registry.profile_for(plan_id, node_id)
        except HarnessError as e:
            return {"name": None, "harness": None, "model": None, "profile_id": None, "error": str(e)}
        return registry.describe(profile)

    def _conflicts(plan_id: int, node_id: int, tree: dict[tuple[int, int], list[dict[str, Any]]],
                   cache: ConflictCache) -> list[list[int]]:
        """Other nodes of the plan tree that share a file with this one and could run concurrently."""
        return [[p, n] for p, n in engine.file_conflicts(plan_id, node_id, tree=tree, cache=cache)]

    def _files_error(plan_id: int, e: PlanError) -> ToolError:
        """ToolError for a rejected file path, restating the rule with the actual directory."""
        msg = str(e)
        if msg.startswith("file"):  # a store message about a path or the files list
            try:
                msg += (f" (files lists the paths, relative to {engine.plan_workdir(plan_id)}, that the "
                        "node will create or modify; a directory claims everything under it)")
            except PlanError:
                pass
        return ToolError(msg)

    def _notify_tree(plan_id: int) -> None:
        """A change to one node's files can change `conflicts` anywhere in its plan tree."""
        for pid in store.descendant_plans(store.root_of(plan_id)["id"]):
            engine.notify_plan(pid)

    def _plan_view(plan_id: int) -> dict[str, Any]:
        p = store.get_plan(plan_id)
        tree = store.files_of_plan_tree(store.root_of(plan_id)["id"])
        cache = ConflictCache()
        nodes = []
        for n in store.nodes(plan_id):
            nodes.append(
                {
                    "node_id": n["node_id"],
                    "title": n["title"],
                    "description": n["description"],
                    "status": n["status"],
                    "agent": _resolved_agent(plan_id, n["node_id"]),
                    "agent_override": _profile_name(n["agent_profile_id"]),
                    "subplan_id": store.subplan_of(plan_id, n["node_id"]),
                    "files": [f["path"] for f in tree.get((plan_id, n["node_id"]), [])],
                    "conflicts": _conflicts(plan_id, n["node_id"], tree, cache),
                    "result": n["result"],
                    "error": n["error"],
                }
            )
        return {
            "plan_id": p["id"],
            "title": p["title"],
            "description": p["description"],
            "working_directory": engine.plan_workdir(plan_id),
            "parent": [p["parent_plan_id"], p["parent_node_id"]] if p["parent_plan_id"] else None,
            "max_parallel": p["max_parallel"],
            "default_agent": _profile_name(p["default_agent_profile_id"]),
            "started": bool(store.root_of(plan_id)["started"]),
            "status": engine.plan_status(plan_id),
            "nodes": nodes,
            "edges": [[a, b] for a, b in store.edges(plan_id)],
        }

    # ------------------------------------------------------------ building
    @mcp.tool()
    def create_plan(
        title: str,
        description: str = "",
        parent_plan_id: int | None = None,
        parent_node_id: int | None = None,
        working_directory: str | None = None,
        max_parallel: int | None = None,
    ) -> int:
        """Create a new plan and return its integer id.

        Pass parent_plan_id and parent_node_id to make this plan the subplan of that node
        (the node then represents "do this whole small plan" instead of a single task).

        working_directory (top-level plans only): absolute path of an existing directory the
        plan's agents work in, e.g. a code repository. Defaults to <workdir>/plan-<id>.
        Subplans always use the working directory of their top-level plan.

        max_parallel (top-level plans only): cap how many nodes of this plan's whole tree
        (including its subplans) run at once, regardless of the server's global concurrency
        cap; null = no plan-specific limit. Useful to keep one plan from taking every free
        slot, or to throttle a plan whose agents share a rate-limited resource.
        """
        if parent_plan_id is not None and max_parallel is not None:
            raise ToolError("max_parallel can only be set on top-level plans")
        if working_directory is not None:
            if parent_plan_id is not None:
                raise ToolError("working_directory can only be set on top-level plans")
            try:
                validate_working_directory(working_directory)
            except PlanError as e:
                raise ToolError(str(e)) from e
        try:
            pid = store.create_plan(title, description, parent_plan_id, parent_node_id, cwd=working_directory,
                                    max_parallel=max_parallel)
        except PlanError as e:
            raise ToolError(str(e)) from e
        if parent_plan_id is not None:
            engine.notify_plan(parent_plan_id)
            engine.kick()
        engine.notify_plan(pid)
        return pid

    @mcp.tool()
    def update_plan(plan_id: int, max_parallel: int | None = None) -> str:
        """Change a top-level plan's own settings; only the fields you pass change.

        max_parallel: cap how many nodes of this plan's whole tree run at once (0 or null
        removes the limit). Only meaningful on top-level plans; subplans share their root
        plan's cap. See create_plan.
        """
        fields: dict[str, Any] = {}
        if max_parallel is not None:
            fields["max_parallel"] = max_parallel or None
        if fields:
            try:
                store.update_plan(plan_id, **fields)
            except PlanError as e:
                raise ToolError(str(e)) from e
            engine.kick()  # a raised (or cleared) limit may let waiting nodes start
        engine.notify_plan(plan_id)
        return "updated"

    @mcp.tool()
    def set_plan_workdir(plan_id: int, working_directory: str | None) -> str:
        """Change a top-level plan's working directory; only before it has been started.

        working_directory must be an existing absolute path, as in create_plan; null or ""
        resets it to the default <workdir>/plan-<id>.
        """
        try:
            engine.set_plan_workdir(plan_id, working_directory or None)
        except PlanError as e:
            raise ToolError(str(e)) from e
        engine.notify_plan(plan_id)
        return f"plan {plan_id} working directory set to {engine.plan_workdir(plan_id)}"

    @mcp.tool()
    def add_node(
        plan_id: int,
        title: str,
        description: str = "",
        agent: str | int | None = None,
        files: list[str] | None = None,
    ) -> tuple[int, int]:
        """Add a node (one piece of work) to a plan. Returns (plan_id, node_id).

        The description should be a self-contained instruction for the agent doing the work.
        agent (optional): name (or id) of the agent profile that runs this node, e.g. a cheap local
        model for a mechanical task and a strong one for a hard task; omit it to inherit the plan's.
        files (optional): list the files this node will CREATE or MODIFY; reading is always allowed
        and needs no declaration. Paths are relative to the plan's working directory, may name
        files that do not exist yet, and a directory claims everything under it.
        Example: files=["clearbox/store.py", "tests/"]. Two nodes that could otherwise run in
        parallel and declare the same file run one after the other (lowest node number first)
        instead of editing it at the same time. Nodes without files never wait for each other.
        """
        profile_id = _profile_id(agent)
        try:
            ref = store.add_node(plan_id, title, description, agent_profile_id=profile_id, files=files)
        except PlanError as e:
            raise _files_error(plan_id, e) from e
        if files:
            _notify_tree(plan_id)
        else:
            engine.notify_plan(plan_id)
        engine.kick()
        return ref

    @mcp.tool()
    def add_edge(plan_id: int, from_node_id: int, to_node_id: int) -> str:
        """Add a dependency: from_node_id must be finished before to_node_id can start.

        Both nodes must belong to plan_id. Edges that would create a cycle are rejected.
        """
        try:
            store.add_edge(plan_id, from_node_id, to_node_id)
        except PlanError as e:
            raise ToolError(str(e)) from e
        engine.notify_plan(plan_id)
        engine.kick()
        return f"edge {from_node_id} -> {to_node_id} added to plan {plan_id}"

    @mcp.tool()
    def remove_edge(plan_id: int, from_node_id: int, to_node_id: int) -> str:
        """Remove a dependency edge."""
        store.remove_edge(plan_id, from_node_id, to_node_id)
        engine.notify_plan(plan_id)
        engine.kick()
        return "removed"

    @mcp.tool()
    def remove_node(plan_id: int, node_id: int) -> str:
        """Remove a node that has not started yet (and its edges and subplan)."""
        try:
            store.remove_node(plan_id, node_id)
        except PlanError as e:
            raise ToolError(str(e)) from e
        engine.notify_plan(plan_id)
        engine.kick()
        return "removed"

    @mcp.tool()
    def update_node(
        plan_id: int,
        node_id: int,
        title: str | None = None,
        description: str | None = None,
        files: list[str] | None = None,
    ) -> str:
        """Change the title, description and/or declared files of a node; only what you pass changes.

        files: omit it (or pass null) to KEEP the node's current files; a list REPLACES the whole
        set; [] clears it. Files can only change before the node has run. See add_node.
        """
        try:
            store.get_node(plan_id, node_id)
            if files is not None:
                store.set_node_files(plan_id, node_id, files)
        except PlanError as e:
            raise _files_error(plan_id, e) from e
        fields = {k: v for k, v in (("title", title), ("description", description)) if v is not None}
        store.update_node(plan_id, node_id, **fields)
        if files is not None:
            _notify_tree(plan_id)
            engine.kick()  # a cleared or narrowed set may let a held-back node start
        else:
            engine.notify_plan(plan_id)
        return "updated"

    @mcp.tool()
    def set_node_files(plan_id: int, node_id: int, files: list[str] | None) -> list[str]:
        """Replace the files a node will CREATE or MODIFY; returns the stored list. [] or null clears it.

        Paths as for add_node: relative to the plan's working directory, a directory claims
        everything under it, reads need no declaration. Only for nodes that have not run yet.
        """
        try:
            stored = store.set_node_files(plan_id, node_id, files)
        except PlanError as e:
            raise _files_error(plan_id, e) from e
        _notify_tree(plan_id)
        engine.kick()  # a cleared or narrowed set may let a held-back node start
        return [f["path"] for f in stored]

    @mcp.tool()
    def get_plan(plan_id: int) -> dict[str, Any]:
        """Return a plan with its nodes (status, results, subplan ids) and edges.

        Each node shows `agent` (name, harness and model of the profile that will run it, after
        inheritance) and `agent_override` (the profile set on the node itself, or null); the plan
        shows `default_agent` (its plan-wide default, or null). `files` lists the files the node
        declared; `conflicts` the [plan_id, node_id] of other nodes in the plan tree that share one
        of them and could run at the same time -- those run one at a time, lowest number first.
        """
        try:
            return _plan_view(plan_id)
        except PlanError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def list_plans() -> list[dict[str, Any]]:
        """List all top-level plans with their status and progress."""
        out = []
        for p in store.list_root_plans():
            out.append(
                {
                    "plan_id": p["id"],
                    "title": p["title"],
                    "started": bool(p["started"]),
                    "executor": p["executor"],
                    "status": engine.plan_status(p["id"]),
                    "progress": engine.rollup(p["id"]),
                }
            )
        return out

    # -------------------------------------------------------------- agents
    @mcp.tool()
    def list_agents() -> list[dict[str, Any]]:
        """List the agent profiles nodes can run on.

        Each entry has name, harness, model, base_url, max_parallel, ... and `default: true` on the
        server-wide default profile. Secrets are never shown: `has_api_key` says whether a key is
        available (from the api_key_env variable, or one stored through the web UI).
        """
        return [_agent_view(p) for p in store.list_profiles()]

    @mcp.tool()
    def create_agent(
        name: str,
        harness: str,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        params: dict[str, Any] | None = None,
        tools: dict[str, Any] | None = None,
        permission_mode: str | None = None,
        system_append: str | None = None,
        max_parallel: int | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Create an agent profile (a named harness + model) and return it.

        name: unique and not purely numeric; nodes and plans refer to the profile by it.
        harness: 'claude-sdk' (Claude Agent SDK; model e.g. "claude-opus-5-5", empty = server default),
          'openai' (any OpenAI-compatible chat-completions endpoint) or 'mock' (fake agent for tests).
        For 'openai': model is required; base_url is the endpoint root ending in /v1 (empty = the real
          OpenAI API); api_key_env is the NAME of an environment variable of the Clearbox server
          holding the key (local endpoints usually need none). Examples:
            create_agent(name="gpt-mini", harness="openai", model="gpt-4o-mini",
                         api_key_env="OPENAI_API_KEY")
            create_agent(name="local-qwen", harness="openai", model="qwen2.5-coder:14b",
                         base_url="http://localhost:11434/v1", max_parallel=1)   # Ollama
            (LM Studio's default is base_url="http://localhost:1234/v1".)
        A literal api key is deliberately NOT accepted here: secrets should not pass through a model's
        context. Set the environment variable on the server, or enter the key in the web UI.
        params: e.g. {"temperature": 0.2, "max_turns": 40, "timeout": 300}.
        tools: {"preset": ..., "allow": [...], "deny": [...], "mcp": true|false}.
        permission_mode (claude-sdk only); system_append: extra system prompt text;
        max_parallel: max nodes running on this profile at once, across all plans (null = no limit).
        Run test_agent afterwards to check that the endpoint answers.
        """
        try:
            pid = store.create_profile(
                name=name, harness=harness, model=model, base_url=base_url, api_key_env=api_key_env,
                params=params, tools=tools, permission_mode=permission_mode, system_append=system_append,
                max_parallel=max_parallel, description=description,
            )
            return _agent_view(store.get_profile(pid))
        except PlanError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def update_agent(
        agent: str | int,
        name: str | None = None,
        harness: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        params: dict[str, Any] | None = None,
        tools: dict[str, Any] | None = None,
        permission_mode: str | None = None,
        system_append: str | None = None,
        max_parallel: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Change an agent profile (by name or id); only the fields you pass change. Returns it.

        Pass "" to clear a text field (model, base_url, api_key_env, permission_mode, system_append)
        and 0 to remove the max_parallel limit; params/tools replace the whole object. Applies to
        nodes that start afterwards. See create_agent for the fields; api keys are not accepted here.
        """
        profile = _profile_ref(agent)
        fields: dict[str, Any] = {
            k: v
            for k, v in (("name", name), ("harness", harness), ("params", params), ("tools", tools),
                         ("description", description))
            if v is not None
        }
        for k, v in (("model", model), ("base_url", base_url), ("api_key_env", api_key_env),
                     ("permission_mode", permission_mode), ("system_append", system_append)):
            if v is not None:
                fields[k] = v or None
        if max_parallel is not None:
            fields["max_parallel"] = max_parallel or None
        try:
            store.update_profile(profile["id"], **fields)
            updated = _agent_view(store.get_profile(profile["id"]))
        except PlanError as e:
            raise ToolError(str(e)) from e
        engine.kick()  # a raised max_parallel may let waiting nodes start
        return updated

    @mcp.tool()
    def delete_agent(agent: str | int) -> str:
        """Delete an agent profile (by name or id).

        Nodes and plans that used it lose that setting and inherit again (plan default -> server default).
        """
        profile = _profile_ref(agent)
        # plans whose view changes: those with a node or a plan default on this profile
        affected = {r["plan_id"] for r in store._q(
            "SELECT plan_id FROM nodes WHERE agent_profile_id=? "
            "UNION SELECT id FROM plans WHERE default_agent_profile_id=?", (profile["id"], profile["id"]))}
        try:
            store.delete_profile(profile["id"])
        except PlanError as e:
            raise ToolError(str(e)) from e
        for pid in affected:
            engine.notify_plan(pid)
        engine.kick()
        return f"agent profile {profile['name']!r} deleted"

    @mcp.tool()
    def set_node_agent(plan_id: int, node_id: int, agent: str | int | None) -> str:
        """Choose the agent profile (name or id) that runs one node; null clears the override so the
        node inherits its plan's default again. Applies the next time the node starts."""
        profile_id = _profile_id(agent)
        try:
            store.set_node_profile(plan_id, node_id, profile_id)
        except PlanError as e:
            raise ToolError(str(e)) from e
        engine.notify_plan(plan_id)
        if profile_id is None:
            return f"node {node_id} of plan {plan_id} now inherits its agent ({_resolved_agent(plan_id, node_id)['name']})"
        return f"node {node_id} of plan {plan_id} runs on agent {_profile_name(profile_id)!r}"

    @mcp.tool()
    def set_plan_agent(plan_id: int, agent: str | int | None) -> str:
        """Set a plan-wide default agent profile (name or id); null clears it.

        The default applies to every node of the plan that has no agent of its own and is inherited
        by its subplans. Resolution order for a node, first hit wins: the node's own agent -> its
        plan's default -> the nearest ancestor plan's default -> ... -> the root plan's default ->
        the server-wide default profile -> the server's built-in agent.
        """
        profile_id = _profile_id(agent)
        try:
            store.set_plan_profile(plan_id, profile_id)
        except PlanError as e:
            raise ToolError(str(e)) from e
        for pid in [plan_id, *store.descendant_plans(plan_id)]:
            engine.notify_plan(pid)
        if profile_id is None:
            return f"plan {plan_id} no longer has a default agent"
        return f"plan {plan_id} (and its subplans) default to agent {_profile_name(profile_id)!r}"

    @mcp.tool()
    async def test_agent(agent: str | int) -> dict[str, Any]:
        """Quick liveness check of an agent profile (by name or id). Never raises.

        For 'openai' profiles it lists the endpoint's models (~10s timeout; endpoints without
        /models get a tiny completion instead) and returns {ok, detail, reachable, model_listed,
        models_sample, credential}; credential says where the key comes from ("env:OPENAI_API_KEY",
        "literal", "none"), never the key itself. 'claude-sdk' and 'mock' need no check.
        """
        try:
            return await registry.test(agent)  # shared with POST /api/agents/{id}/test
        except PlanError as e:
            return {"ok": False, "detail": f"{e}; list_agents shows the configured agent profiles", "models_sample": []}

    # ----------------------------------------------------------- execution
    @mcp.tool()
    def start_plan(plan_id: int, executor: str = "builtin") -> str:
        """Start executing a top-level plan, root nodes first.

        executor="builtin": the service runs one Claude agent session per node.
        executor="external": nodes wait to be picked up via claim_next_node.
        """
        try:
            engine.start_plan(plan_id, executor)
        except PlanError as e:
            raise ToolError(str(e)) from e
        return f"plan {plan_id} started with executor '{executor}'"

    @mcp.tool()
    async def stop_plan(plan_id: int) -> str:
        """Pause a plan: running built-in agents are cancelled and their nodes re-queued."""
        await engine.stop_plan(plan_id)
        return f"plan {plan_id} stopped"

    # ----------------------------------------------------- external agents
    @mcp.tool()
    def claim_next_node(agent_name: str, plan_id: int | None = None) -> dict[str, Any] | None:
        """Claim the next ready node of a plan started with executor="external".

        Returns the node with full context (overall goal, prerequisite results, instructions)
        or null if nothing is ready right now. Optionally restrict to one plan tree.
        """
        try:
            return engine.claim_next(agent_name, plan_id)
        except PlanError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def report_progress(plan_id: int, node_id: int, message: str, kind: str = "text") -> str:
        """Append a progress message to a node's live transcript (shown in the UI).

        kind is one of: text, thinking, tool_use, tool_result, log.
        """
        try:
            engine.report(plan_id, node_id, message, kind)
        except PlanError as e:
            raise ToolError(str(e)) from e
        return "ok"

    @mcp.tool()
    def complete_node(plan_id: int, node_id: int, result: str) -> str:
        """Mark a claimed node as done. result is a concise summary passed on to dependent nodes."""
        try:
            engine.complete_external(plan_id, node_id, result)
        except PlanError as e:
            raise ToolError(str(e)) from e
        return "ok"

    @mcp.tool()
    def fail_node(plan_id: int, node_id: int, error: str) -> str:
        """Mark a claimed node as failed (dependent nodes will not start until it is retried)."""
        try:
            engine.fail_external(plan_id, node_id, error)
        except PlanError as e:
            raise ToolError(str(e)) from e
        return "ok"

    return mcp
