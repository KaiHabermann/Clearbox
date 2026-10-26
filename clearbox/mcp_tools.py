"""MCP server exposing plan-building and agent-coordination tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from .engine import ConflictCache, Engine
from .harness import HarnessError
from .store import PlanError, Store, public_profile, validate_working_directory

INSTRUCTIONS = """
Clearbox runs a plan: a DAG of nodes, each one piece of work done by one agent session. Edges
point from a prerequisite to the node that depends on it; independent nodes run in parallel.

Build a plan in ONE call, then start it:
  submit_plan(title, working_directory, nodes=[...], edges=[...]) -> {plan_id, nodes: {key: node_id}}
    working_directory  absolute path of the existing directory the agents work in (the repository)
    nodes  [{key, title, description, files, results_from, kind, agent}]; key is your own label
    edges  [[from_key, to_key], ...]
  It is all or nothing: every problem is reported at once and nothing is created.
  start_plan(plan_id)
Later changes: add_node / add_edge / update_node / remove_node / remove_edge.

Write each description so an agent with no other context can do the work. An edge passes
NOTHING: a node that needs an earlier node's output names it in results_from (and must depend on
it through edges). files: what the node will create or modify; parallel nodes sharing a file run
one after the other. kind="user": a step the user does (push, deploy, approve); the plan waits
for them. A node can become a subplan: submit_plan(..., parent_plan_id, parent_node_id).

agent: a profile from list_agents, per node (set_plan_agent for a plan-wide default); without
one the server default runs. get_plan shows what every node resolves to.

Optional: test_instructions (how the project's tests run; agents get it verbatim);
start_plan(executor="external") + claim_next_node for outside agents; reviews
(set_plan_review_agent, review_node, review_plan, get_reviews, rework_node) are advisory only.
""".strip()


AGENT_INSTRUCTIONS = """
Clearbox tools for an agent working on one node of a plan. They only reach the subplan of your own
node and plans nested inside it: submit_plan(title, parent_plan_id, parent_node_id, nodes, edges)
expands your node in one call (pass your own plan and node id); add_node / add_edge / update_node /
... adjust it and get_plan reads it back. Edges point from a prerequisite to the node that depends
on it and pass no results. Every sub-node runs on your own agent, after your session ends.
""".strip()


OBSERVE_INSTRUCTIONS = """
Clearbox shows the user your work live as a plan: a graph of steps, each with its own diff. Use it
for every task of three or more steps; skip it for questions and one-step edits. You do the work
yourself: Clearbox runs no agents here.

Start: ONE submit_plan(title, working_directory, nodes, edges) per task; it also starts the plan.
  title: the task in a few words; description: the goal and what "done" means.
  nodes: one per step, title a short verb phrase ("Parse config file"), description for the user
    (what and why), files = the paths it will create or modify (its diff in the UI).
  edges: [from_key, to_key] for every real dependency ("must finish first"); none between
    independent steps. kind="user" for a step only the user can do (push, deploy, approve).
  Then give the user one line and the link http://127.0.0.1:{port}/#/plan/<plan_id> instead of
  writing the plan out in chat.

Work: claim_next_node(agent_name, plan_id) gives the lowest-numbered ready step, so number steps
in the order you mean to do them. Claim one at a time and work only on what you claimed.
complete_node(plan_id, node_id, result) the moment it is done, with 1-3 sentences: what was done
and any deviation. Never in a batch afterwards. fail_node with the reason if it is abandoned.
report_progress only at real milestones (a finding, a decision, a dead end).

Keep the graph true, at the moment you learn something:
- new work: add_node, then add_edge from what it depends on and to each pending step needing it.
- a running step needs something first: complete it with what it did, add the missing step and a
  follow-up for the rest, wire current -> missing -> follow-up -> former dependants.
- unnecessary step: remove_node, reconnect its neighbours. Wrong dependency: remove_edge/add_edge.
- too big: submit_plan(parent_plan_id, parent_node_id, ...) expands a step into a subplan that
  runs with its plan. For the step you hold, complete it first: then its subplan's steps are
  claimable, and the step is done when they all are.
- changed direction: remove or fail what no longer applies; leave no stale pending steps.
Blocked on the user (a token, an install, a decision): complete the current step saying what is
missing, add_node(kind="user") with an exact instruction for a person, wire a follow-up after it,
and tell the user in one line with the link. Do not work around the blocker.
""".strip()


class NodeSpec(BaseModel):
    """One node of a submit_plan call."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="your own label for this node, unique in the call; edges and results_from use it")
    title: str
    description: str = Field("", description="a self-contained instruction for whoever does the work")
    files: list[str] | None = Field(None, description="paths, relative to the working directory, it will "
                                                      "create or modify; a directory claims everything under it")
    results_from: list[str] | None = Field(None, description="keys of nodes (it depends on via edges) whose "
                                                             "result text it receives")
    kind: str | None = Field(None, description='"agent" (default) or "user" (a step the user does)')
    agent: str | int | None = Field(None, description="agent profile (name or id) that runs it; omit to inherit")


def _in_subtree(store: Store, plan_id: int, node: tuple[int, int]) -> bool:
    """Whether ``plan_id`` is the subplan of ``node`` or nested anywhere below it: the one rule
    deciding which plans a worker agent's scoped tools may touch. Unknown plans are outside."""
    try:
        p = store.get_plan(plan_id)
        while p["parent_plan_id"] is not None:
            if (p["parent_plan_id"], p["parent_node_id"]) == node:
                return True
            p = store.get_plan(p["parent_plan_id"])
    except PlanError:
        pass
    return False


def build_mcp(engine: Engine) -> tuple[MCPServer, MCPServer, MCPServer]:
    """The planning server the user's own session reaches at /mcp, the scoped server built-in
    worker agents reach at /mcp-agent/<token> (a checked subset of the same tools), and the slim
    server at /mcp/observe for a session that tracks its own work (another subset)."""
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
                    "kind": n.get("kind") or "agent",
                    "waiting_for_user": engine.waiting_for_user(n),
                    "agent": _resolved_agent(plan_id, n["node_id"]),
                    "agent_override": _profile_name(n["agent_profile_id"]),
                    "subplan_id": store.subplan_of(plan_id, n["node_id"]),
                    "files": [f["path"] for f in tree.get((plan_id, n["node_id"]), [])],
                    "conflicts": _conflicts(plan_id, n["node_id"], tree, cache),
                    "results_from": store.node_results_from(plan_id, n["node_id"]),
                    "result": n["result"],
                    "error": n["error"],
                    "undeclared_files": n["undeclared_files"],
                    "undeclared_more": n["undeclared_more"],
                    "undeclared_shared": n["undeclared_shared"],
                }
            )
        return {
            "plan_id": p["id"],
            "title": p["title"],
            "description": p["description"],
            "working_directory": engine.plan_workdir(plan_id),
            "test_instructions": engine.plan_test_instructions(plan_id),
            "parent": [p["parent_plan_id"], p["parent_node_id"]] if p["parent_plan_id"] else None,
            "max_parallel": p["max_parallel"],
            "default_agent": _profile_name(p["default_agent_profile_id"]),
            "started": bool(store.root_of(plan_id)["started"]),
            "status": engine.plan_status(plan_id),
            "nodes": nodes,
            "edges": [[a, b] for a, b in store.edges(plan_id)],
        }

    # ------------------------------------------------------------ building
    def _check_plan_args(parent_plan_id: int | None, working_directory: str | None,
                         max_parallel: int | None, test_instructions: str | None) -> None:
        """The rules for a new plan's own settings, shared by create_plan and submit_plan."""
        if parent_plan_id is not None and max_parallel is not None:
            raise ToolError("max_parallel can only be set on top-level plans")
        if parent_plan_id is not None and test_instructions:
            raise ToolError("test_instructions can only be set on top-level plans; a subplan "
                            "uses its top-level plan's")
        if parent_plan_id is None and working_directory is None:
            raise ToolError(
                "working_directory is required for a top-level plan: pass the absolute path of "
                "the existing directory its agents should work in (e.g. the repository)")
        if working_directory is not None:
            if parent_plan_id is not None:
                raise ToolError("working_directory can only be set on top-level plans; a subplan "
                                "uses its top-level plan's directory")
            try:
                validate_working_directory(working_directory)
            except PlanError as e:
                raise ToolError(str(e)) from e

    def _submit(title: str, working_directory: str | None, description: str, test_instructions: str | None,
                nodes: list[NodeSpec], edges: list[tuple[str, str]], parent_plan_id: int | None,
                parent_node_id: int | None, max_parallel: int | None,
                default_profile_id: int | None = None) -> dict[str, Any]:
        """submit_plan for both endpoints. The checks that need nothing written run first; the rest
        (subplan rules, files, kind, cycles, results_from) are the store's and engine's own, applied
        by writing the plan inside one transaction that any error rolls back. Every error is
        collected, so one call reports them all and leaves nothing behind."""
        errors: list[str] = []

        def check(label: str, fn: Any, *args: Any) -> Any:
            try:
                return fn(*args)
            except (PlanError, ToolError) as e:
                errors.append(f"{label}{e}")
                return None

        check("", _check_plan_args, parent_plan_id, working_directory, max_parallel, test_instructions)
        keys = [n.key for n in nodes]
        for k in sorted({k for k in keys if keys.count(k) > 1}):
            errors.append(f"duplicate node key {k!r}")
        known = set(keys)
        for a, b in edges:
            for k in (a, b):
                if k not in known:
                    errors.append(f"edge [{a!r}, {b!r}]: unknown node key {k!r}")
        for n in nodes:
            for k in n.results_from or ():
                if k not in known:
                    errors.append(f"node {n.key!r}: results_from names unknown node key {k!r}")
        profiles = {n.key: check(f"node {n.key!r}: ", _profile_id, n.agent) for n in nodes}

        ids: dict[str, int] = {}
        try:
            with store.transaction():
                pid = check("", store.create_plan, title, description, parent_plan_id, parent_node_id,
                            working_directory, max_parallel, (test_instructions or "").strip() or None)
                if pid is not None:
                    if default_profile_id is not None:
                        store.set_plan_profile(pid, default_profile_id)
                    for n in dict(zip(keys, nodes)).values():  # a duplicate key is already reported
                        ref = check(f"node {n.key!r}: ", store.add_node, pid, n.title, n.description,
                                    profiles[n.key], n.files, None, n.kind)
                        if ref is not None:
                            ids[n.key] = ref[1]
                    for a, b in edges:
                        if a in ids and b in ids:
                            check(f"edge [{a!r}, {b!r}]: ", store.add_edge, pid, ids[a], ids[b])
                    # results_from last: whether each is a dependency depends on every edge
                    for n in nodes:
                        if n.key not in ids or not n.results_from:
                            continue
                        wanted = [k for k in n.results_from if k in ids]
                        bad = []
                        for k in wanted:
                            try:
                                engine.check_results_from(pid, ids[n.key], [ids[k]])
                            except PlanError:
                                bad.append(k)
                        if bad:
                            errors.append(f"node {n.key!r}: results_from names {bad}, which it does not "
                                          "depend on; add edges from them to it (directly or via other nodes)")
                        store.set_node_results_from(pid, ids[n.key], [ids[k] for k in wanted])
                if errors:
                    raise ToolError("nothing was created:\n" + "\n".join(f"- {e}" for e in errors))
        except PlanError as e:  # not one of the checks above: still nothing written
            raise ToolError(str(e)) from e
        if parent_plan_id is not None:
            engine.notify_plan(parent_plan_id)
        if any(n.files for n in nodes):
            _notify_tree(pid)
        else:
            engine.notify_plan(pid)
        engine.kick()
        return {"plan_id": pid, "nodes": ids}

    @mcp.tool()
    def submit_plan(
        title: str,
        working_directory: str | None = None,
        description: str = "",
        test_instructions: str | None = None,
        nodes: list[NodeSpec] | None = None,
        edges: list[tuple[str, str]] | None = None,
        parent_plan_id: int | None = None,
        parent_node_id: int | None = None,
        max_parallel: int | None = None,
    ) -> dict[str, Any]:
        """Create a whole plan -- its nodes and edges -- in one call; the way to build a plan.
        Returns {"plan_id": ..., "nodes": {key: node_id}}.

        All or nothing: everything is checked first and every problem is reported in one error,
        and then nothing at all has been created. start_plan runs it afterwards.

        nodes: [{key, title, description, files, results_from, kind, agent}, ...]. key is your own
        label, unique in this call. description: a self-contained instruction for whoever does
        the work. files (optional): paths, relative to the working directory, that the node will
        CREATE or MODIFY (reads need no declaration; a directory claims everything under it); nodes
        that could run in parallel and share a file run one after the other, lowest number first.
        results_from (optional): keys of earlier nodes whose result text it receives -- an edge
        passes NOTHING -- each one a node it depends on through edges. kind (optional): "agent" or
        "user", a step the user does themselves (push, deploy, approve); the plan waits there until
        they mark it done. agent (optional): a profile from list_agents; omit to inherit.
        edges: [[from_key, to_key], ...] -- from must finish before to starts; no cycles.

        working_directory, test_instructions, max_parallel: as in create_plan (top-level plans
        only; working_directory is required there). parent_plan_id + parent_node_id make this the
        subplan of that node, which then completes when the whole subplan is done.
        """
        return _submit(title, working_directory, description, test_instructions, nodes or [],
                       edges or [], parent_plan_id, parent_node_id, max_parallel)

    @mcp.tool()
    def create_plan(
        title: str,
        description: str = "",
        parent_plan_id: int | None = None,
        parent_node_id: int | None = None,
        working_directory: str | None = None,
        max_parallel: int | None = None,
        test_instructions: str | None = None,
    ) -> int:
        """Create an empty plan and return its integer id; submit_plan creates one with its nodes
        and edges in a single call, and is the usual way.

        Pass parent_plan_id and parent_node_id to make this plan the subplan of that node
        (the node then represents "do this whole small plan" instead of a single task).

        working_directory: REQUIRED for a top-level plan -- the absolute path of an existing
        directory the plan's agents work in, e.g. a code repository. Say where the work happens
        rather than letting agents edit files somewhere you did not choose. Subplans inherit it
        from their top-level plan and must not pass it.

        max_parallel (top-level plans only): cap how many nodes of this plan's whole tree
        (including its subplans) run at once, regardless of the server's global concurrency
        cap; null = no plan-specific limit. Useful to keep one plan from taking every free
        slot, or to throttle a plan whose agents share a rate-limited resource.

        test_instructions (top-level plans only): free text saying how this project's tests are
        run (commands, servers they need, what to avoid). Worker and reviewer agents get it
        verbatim; without it they work it out from the repository (README, CI config, ...).
        Subplans inherit it.
        """
        _check_plan_args(parent_plan_id, working_directory, max_parallel, test_instructions)
        try:
            pid = store.create_plan(title, description, parent_plan_id, parent_node_id, cwd=working_directory,
                                    max_parallel=max_parallel,
                                    test_instructions=(test_instructions or "").strip() or None)
        except PlanError as e:
            raise ToolError(str(e)) from e
        if parent_plan_id is not None:
            engine.notify_plan(parent_plan_id)
            engine.kick()
        engine.notify_plan(pid)
        return pid

    @mcp.tool()
    def update_plan(plan_id: int, max_parallel: int | None = None, test_instructions: str | None = None) -> str:
        """Change a top-level plan's own settings; only the fields you pass change.

        max_parallel: cap how many nodes of this plan's whole tree run at once (0 or null
        removes the limit). Only meaningful on top-level plans; subplans share their root
        plan's cap. See create_plan.

        test_instructions: how this project's tests are run, as in create_plan; "" removes
        them (agents then work it out from the repository). Top-level plans only; nodes that
        start afterwards get the new text.
        """
        fields: dict[str, Any] = {}
        if max_parallel is not None:
            fields["max_parallel"] = max_parallel or None
        if test_instructions is not None:
            fields["test_instructions"] = test_instructions.strip() or None
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
        results_from: list[int] | None = None,
        kind: str | None = None,
    ) -> tuple[int, int]:
        """Add one node to an existing plan, e.g. to extend or correct it. Returns (plan_id, node_id).

        The fields are those of a submit_plan node, except that results_from takes node ids of this
        plan; add the edges that make each one a dependency too (start_plan checks it). A new plan
        is built in one submit_plan call instead.
        """
        profile_id = _profile_id(agent)
        try:
            ref = store.add_node(plan_id, title, description, agent_profile_id=profile_id, files=files,
                                 results_from=results_from, kind=kind)
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
        results_from: list[int] | None = None,
        kind: str | None = None,
        agent: str | int | None = None,
    ) -> str:
        """Change a node; only what you pass changes, and nothing does if any of it is rejected.

        files, results_from: omit (or pass null) to KEEP the current value; a list REPLACES it; []
        clears it. results_from takes node ids of this plan, each one a node it depends on through
        edges. kind: "agent" or "user". These three can only change before the node has run.
        agent: a profile name or id from list_agents; "" clears the node's own agent so it inherits
        again. Applies the next time the node starts. See submit_plan / add_node for the fields.
        """
        try:
            with store.transaction():
                store.get_node(plan_id, node_id)
                if results_from is not None:
                    engine.check_results_from(plan_id, node_id, results_from)
                    store.set_node_results_from(plan_id, node_id, results_from)
                if files is not None:
                    store.set_node_files(plan_id, node_id, files)
                if kind is not None:
                    store.set_node_kind(plan_id, node_id, kind)
                if agent is not None:
                    store.set_node_profile(plan_id, node_id, _profile_id(agent))
                fields = {k: v for k, v in (("title", title), ("description", description)) if v is not None}
                store.update_node(plan_id, node_id, **fields)
        except PlanError as e:
            raise _files_error(plan_id, e) from e
        if files is not None:
            _notify_tree(plan_id)
        else:
            engine.notify_plan(plan_id)
        engine.kick()  # a narrowed file set or a node that became an agent's job may start now
        return "updated"

    @mcp.tool()
    def get_plan(plan_id: int) -> dict[str, Any]:
        """Return a plan with its nodes (status, results, subplan ids) and edges.

        Each node shows `agent` (name, harness and model of the profile that will run it, after
        inheritance) and `agent_override` (the profile set on the node itself, or null); the plan
        shows `default_agent` (its plan-wide default, or null). `files` lists the files the node
        declared; `conflicts` the [plan_id, node_id] of other nodes in the plan tree that share one
        of them and could run at the same time -- those run one at a time, lowest number first.
        `results_from` lists the earlier nodes whose result text the node receives. `kind` is
        "agent" or "user"; a user node with `waiting_for_user: true` is parked for the user to
        mark it done (nothing runs it), not waiting for an agent. `undeclared_files` names the
        files its last run changed outside `files` (not diffed; `undeclared_more` counts those
        left off the list, `undeclared_shared` means other steps ran meanwhile and some may be theirs).
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

        Each entry has name, harness, model, base_url, max_parallel, capability, test_level, ... and
        `default: true` on the server-wide default profile. capability is what the agent may do: read
        (read and search only), run (also run commands), edit (also edit files, no commands) or full;
        test_level how much a worker tests: none, light, standard or thorough (null = the default).
        Profiles are set up in the web UI or over REST (/api/agents), not here. Secrets are never
        shown: `has_api_key` says whether a key is available (from the api_key_env variable, or one
        stored through the web UI).
        """
        return [_agent_view(p) for p in store.list_profiles()]

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

    # ------------------------------------------------------------ reviews
    @mcp.tool()
    def set_plan_review_agent(plan_id: int, agent: str | int | None) -> str:
        """Set (or clear) the agent profile that AUTOMATICALLY reviews this plan's steps and,
        once it finishes, the plan as a whole; null disables automatic reviewing (the default).

        Reviews are advisory only: a reviewer records a verdict ('ok', 'concerns' or 'error') and
        findings, but never fails a node, blocks a dependant, or edits a file -- the user decides
        what to do with what it finds. Clearing this does not touch reviews already recorded, and
        review_node/review_plan can still review this plan on demand by passing their own agent.
        """
        profile_id = _profile_id(agent)
        try:
            store.set_plan_review_agent(plan_id, profile_id)
        except PlanError as e:
            raise ToolError(str(e)) from e
        engine.notify_plan(plan_id)
        if profile_id is None:
            return f"plan {plan_id} no longer reviews automatically"
        return f"plan {plan_id} is automatically reviewed by agent {_profile_name(profile_id)!r}"

    @mcp.tool()
    async def review_node(plan_id: int, node_id: int, agent: str | int | None = None) -> dict[str, Any]:
        """Review one node against its own diff right now (re-reviews if it already has one).

        agent (optional): name or id of the agent profile to review with, used for this run only
        -- it does not change the plan's automatic review agent. Falls back to the plan's review
        agent when omitted; errors if neither is set. Works on any node with a captured diff,
        including one from a plan that finished with no review agent set at all -- the whole
        point is that a finished run can be reviewed afterwards just by pointing an agent at it.
        """
        try:
            return await engine.review_now(plan_id, node_id, agent)
        except PlanError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def review_plan(plan_id: int, agent: str | int | None = None) -> dict[str, Any]:
        """Review a finished plan as a whole for oversights right now (re-reviews if it already
        has one). Reads titles, descriptions, results and each step's own review verdict --
        never diffs or source, that is review_node's job.

        agent (optional): as in review_node, used for this run only. Falls back to the plan's
        review agent when omitted; errors if neither is set.
        """
        try:
            return await engine.review_now(plan_id, None, agent)
        except PlanError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def get_reviews(plan_id: int) -> dict[str, Any]:
        """The plan's review (if any) plus every node's review verdict and findings, across the
        whole plan tree (including subplans).

        Meant to be turned into follow-up work: read the plan-level review's findings for what
        it says was missed or contradicted, and add_node/add_edge new nodes for it.
        """
        try:
            store.get_plan(plan_id)
            plan_review = store.review(plan_id, None)
            node_reviews = [
                r for pid in store.descendant_plans(plan_id)
                for r in store.reviews_of_plan(pid) if r["node_id"] is not None
            ]
        except PlanError as e:
            raise ToolError(str(e)) from e
        return {"plan_review": plan_review, "node_reviews": node_reviews}

    @mcp.tool()
    def rework_node(plan_id: int, node_id: int, instructions: str | None = None) -> str:
        """Re-run one finished (done or failed) step with its review findings in the prompt, so
        its agent fixes what the reviewer raised on top of the work already in the tree.

        instructions (optional): what to address instead of the review findings, e.g. "fix X";
        needed when the node has no review. Only this one step re-runs: nodes that depend on it
        are left untouched, even if they built on the old output. If the plan has a review agent,
        the step is reviewed again automatically when the rework finishes -- read the new
        verdict with get_reviews.
        """
        try:
            engine.rework_node(plan_id, node_id, instructions)
        except PlanError as e:
            raise ToolError(str(e)) from e
        return f"node ({plan_id}, {node_id}) queued for rework"

    # ----------------------------------------------------------- execution
    @mcp.tool()
    def start_plan(plan_id: int, executor: str = "builtin") -> str:
        """Start executing a top-level plan, root nodes first.

        executor="builtin": the service runs one Claude agent session per node.
        executor="external": nodes wait to be picked up via claim_next_node.
        """
        try:
            # add_node takes results_from before the edges exist, so the dependency check waits until here.
            for pid in store.descendant_plans(plan_id):
                for n in store.nodes(pid):
                    if n["status"] in ("pending", "ready"):
                        engine.check_results_from(pid, n["node_id"], store.node_results_from(pid, n["node_id"]))
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

        Returns the node with full context (overall goal, requested earlier results, instructions)
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

    # ------------------------------------------------------- worker agents
    # The scoped server a built-in worker reaches at /mcp-agent/<token> instead of /mcp. The
    # token names the node it runs (engine.agent_run); each tool below checks its plan against
    # that node's subtree (_in_subtree) and then calls the planning tool of the same name.
    agent_mcp = MCPServer("clearbox", instructions=AGENT_INSTRUCTIONS)

    def _caller(ctx: Context) -> tuple[int, int, int | None]:
        run = engine.agent_run(ctx.request_context.request.path_params["token"])
        if run is None:
            raise ToolError("this agent token is unknown or its run has ended")
        return run

    def _scoped(ctx: Context, plan_id: int) -> None:
        own_plan, own_node, _ = _caller(ctx)
        if not _in_subtree(store, plan_id, (own_plan, own_node)):
            raise ToolError(f"plan {plan_id} is not inside the subplan of your node ({own_plan}, {own_node}); "
                            "you can only build below your own node")

    def _no_agent(agent: str | int | None) -> None:
        if agent is not None:
            raise ToolError("agent is not accepted here: every node you add runs on your own agent, and "
                            "a worker cannot choose another one. Leave agent out.")

    def _expanding(ctx: Context, parent_plan_id: int, parent_node_id: int) -> int | None:
        """Check that a new subplan goes below the caller's own node; the caller's profile id."""
        plan_id, node_id, profile_id = _caller(ctx)
        if (parent_plan_id, parent_node_id) != (plan_id, node_id):
            _scoped(ctx, parent_plan_id)
        return profile_id

    @agent_mcp.tool(name="submit_plan")
    def agent_submit_plan(ctx: Context, title: str, parent_plan_id: int, parent_node_id: int,
                          description: str = "", nodes: list[NodeSpec] | None = None,
                          edges: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        """Expand a node into a whole subplan -- its nodes and edges -- in one call. Returns
        {"plan_id": ..., "nodes": {key: node_id}}. All or nothing: every problem is reported in one
        error, and then nothing has been created.

        parent_plan_id / parent_node_id: your own node, or a node of a subplan you created. The
        subplan runs on your own agent, inherits the working directory and test instructions, and
        starts by itself once your session ends.
        nodes: [{key, title, description, files, results_from, kind}, ...]; key is your own label.
        description: a self-contained instruction for the agent doing the work. files (optional):
        paths, relative to the working directory, it will CREATE or MODIFY; nodes that could run in
        parallel and share a file run one after the other. results_from (optional): keys of nodes it
        depends on whose result text it receives -- an edge passes nothing. kind="user" makes it a
        person's job. agent is not accepted: every node runs on your own agent.
        edges: [[from_key, to_key], ...] -- from must finish before to starts.
        """
        profile_id = _expanding(ctx, parent_plan_id, parent_node_id)
        for n in nodes or ():
            _no_agent(n.agent)
        # never let expanding a node run the sub-work on a more capable agent than the node had
        return _submit(title, None, description, None, nodes or [], edges or [], parent_plan_id,
                       parent_node_id, None, default_profile_id=profile_id)

    @agent_mcp.tool(name="create_plan")
    def agent_create_plan(ctx: Context, title: str, parent_plan_id: int, parent_node_id: int,
                          description: str = "") -> int:
        """Expand a node into an empty subplan and return its id; submit_plan does it with the
        nodes and edges in one call. As for submit_plan, the subplan runs on your own agent."""
        profile_id = _expanding(ctx, parent_plan_id, parent_node_id)
        pid = create_plan(title, description, parent_plan_id, parent_node_id)
        if profile_id is not None:
            # never let expanding a node run the sub-work on a more capable agent than the node had
            try:
                store.set_plan_profile(pid, profile_id)
            except PlanError as e:
                raise ToolError(str(e)) from e
            engine.notify_plan(pid)
        return pid

    @agent_mcp.tool(name="add_node")
    def agent_add_node(ctx: Context, plan_id: int, title: str, description: str = "",
                       files: list[str] | None = None, results_from: list[int] | None = None,
                       kind: str | None = None, agent: str | int | None = None) -> tuple[int, int]:
        """Add a node (one piece of work) to a subplan of yours. Returns (plan_id, node_id).

        The fields are those of a submit_plan node, except that results_from takes node ids of this
        plan. agent is not accepted: the node runs on your own agent.
        """
        _scoped(ctx, plan_id)
        _no_agent(agent)
        return add_node(plan_id, title, description, None, files, results_from, kind)

    @agent_mcp.tool(name="add_edge", description=add_edge.__doc__)
    def agent_add_edge(ctx: Context, plan_id: int, from_node_id: int, to_node_id: int) -> str:
        _scoped(ctx, plan_id)
        return add_edge(plan_id, from_node_id, to_node_id)

    @agent_mcp.tool(name="remove_edge", description=remove_edge.__doc__)
    def agent_remove_edge(ctx: Context, plan_id: int, from_node_id: int, to_node_id: int) -> str:
        _scoped(ctx, plan_id)
        return remove_edge(plan_id, from_node_id, to_node_id)

    @agent_mcp.tool(name="remove_node", description=remove_node.__doc__)
    def agent_remove_node(ctx: Context, plan_id: int, node_id: int) -> str:
        _scoped(ctx, plan_id)
        return remove_node(plan_id, node_id)

    @agent_mcp.tool(name="update_node")
    def agent_update_node(ctx: Context, plan_id: int, node_id: int, title: str | None = None,
                          description: str | None = None, files: list[str] | None = None,
                          results_from: list[int] | None = None, kind: str | None = None,
                          agent: str | int | None = None) -> str:
        """Change a node of a subplan of yours; only what you pass changes, and nothing does if any
        of it is rejected.

        files, results_from: omit (or pass null) to KEEP the current value; a list REPLACES it; []
        clears it. results_from takes node ids of this plan, each one a node it depends on through
        edges. kind: "agent" or "user". These three can only change before the node has run.
        agent is not accepted: the node runs on your own agent.
        """
        _scoped(ctx, plan_id)
        _no_agent(agent)
        return update_node(plan_id, node_id, title, description, files, results_from, kind)

    @agent_mcp.tool(name="get_plan", description=get_plan.__doc__)
    def agent_get_plan(ctx: Context, plan_id: int) -> dict[str, Any]:
        _scoped(ctx, plan_id)
        return get_plan(plan_id)

    agent_mcp.add_tool(list_agents)

    @agent_mcp.tool(name="report_progress")
    def agent_report_progress(ctx: Context, message: str, kind: str = "text") -> str:
        """Append a progress message to your own node's live transcript (shown in the UI). Your
        messages and tool calls are already recorded there; use this only to narrate long-running
        work. kind is one of: text, thinking, tool_use, tool_result, log."""
        plan_id, node_id, _ = _caller(ctx)
        return report_progress(plan_id, node_id, message, kind)

    # ------------------------------------------------------ observing itself
    # The slim server at /mcp/observe: a session that tracks its own work as an external executor
    # needs only these tools, and submit_plan starts a top-level plan right away.
    observe_mcp = MCPServer("clearbox", instructions=OBSERVE_INSTRUCTIONS.format(port=engine.config.server_port))

    @observe_mcp.tool(name="submit_plan")
    def observe_submit_plan(title: str, working_directory: str | None = None, description: str = "",
                            nodes: list[NodeSpec] | None = None, edges: list[tuple[str, str]] | None = None,
                            parent_plan_id: int | None = None,
                            parent_node_id: int | None = None) -> dict[str, Any]:
        """Create a whole plan -- its nodes and edges -- in one call and, if it is a top-level
        plan, start it with you as the executor. Returns {"plan_id", "nodes": {key: node_id},
        "started"}. All or nothing: every problem is reported in one error, and then nothing has
        been created.

        working_directory: absolute path of the project you work in (top-level plans only, required
        there). nodes: [{key, title, description, files, results_from, kind}, ...]; key is your own
        label. files: paths, relative to the working directory, the step will CREATE or MODIFY (its
        diff in the UI). results_from: keys of nodes it depends on whose result text it receives.
        kind="user": a step only the user can do; the plan waits there for them.
        edges: [[from_key, to_key], ...] -- from must finish before to starts; no cycles.
        parent_plan_id + parent_node_id: expand that node into this subplan instead; it runs with
        its parent plan and completes when all its nodes are done.
        """
        out = submit_plan(title, working_directory, description, None, nodes, edges, parent_plan_id,
                          parent_node_id)
        if parent_plan_id is None:
            try:
                start_plan(out["plan_id"], "external")
            except ToolError as e:  # e.g. another running plan in this folder: keep all or nothing
                store.delete_plan(out["plan_id"])
                engine.bus.publish({"type": "plan", "plan_id": out["plan_id"], "root_id": out["plan_id"],
                                    "deleted": True})
                raise ToolError(f"nothing was created: {e}") from e
            out["started"] = 'yes, executor "external": claim its steps with claim_next_node'
        else:
            out["started"] = "runs with its parent plan"
        return out

    for fn in (claim_next_node, report_progress, complete_node, fail_node, add_node, add_edge,
               remove_node, remove_edge, get_plan):
        observe_mcp.add_tool(fn)

    return mcp, agent_mcp, observe_mcp
