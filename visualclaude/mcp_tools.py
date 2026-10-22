"""MCP server exposing plan-building and agent-coordination tools."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .engine import Engine
from .store import PlanError

INSTRUCTIONS = """
VisualClaude lets you lay out a piece of work as a plan: a directed acyclic graph of nodes,
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
  4. start_plan(plan_id, executor="builtin") lets the service run a Claude agent per node,
     or executor="external" lets outside agents pick nodes up with claim_next_node.

Working on nodes as an external agent:
  claim_next_node -> do the work -> report_progress (optional, repeatedly) -> complete_node / fail_node
""".strip()


def build_mcp(engine: Engine) -> MCPServer:
    mcp = MCPServer("visualclaude", instructions=INSTRUCTIONS)
    store = engine.store

    def _plan_view(plan_id: int) -> dict[str, Any]:
        p = store.get_plan(plan_id)
        nodes = []
        for n in store.nodes(plan_id):
            nodes.append(
                {
                    "node_id": n["node_id"],
                    "title": n["title"],
                    "description": n["description"],
                    "status": n["status"],
                    "subplan_id": store.subplan_of(plan_id, n["node_id"]),
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
    ) -> int:
        """Create a new plan and return its integer id.

        Pass parent_plan_id and parent_node_id to make this plan the subplan of that node
        (the node then represents "do this whole small plan" instead of a single task).

        working_directory (top-level plans only): absolute path of an existing directory the
        plan's agents work in, e.g. a code repository. Defaults to <workdir>/plan-<id>.
        Subplans always use the working directory of their top-level plan.
        """
        if working_directory is not None:
            if parent_plan_id is not None:
                raise ToolError("working_directory can only be set on top-level plans")
            if not os.path.isabs(working_directory) or not os.path.isdir(working_directory):
                raise ToolError(f"working_directory must be an existing absolute path: {working_directory}")
        try:
            pid = store.create_plan(title, description, parent_plan_id, parent_node_id, cwd=working_directory)
        except PlanError as e:
            raise ToolError(str(e)) from e
        if parent_plan_id is not None:
            engine.notify_plan(parent_plan_id)
            engine.kick()
        engine.notify_plan(pid)
        return pid

    @mcp.tool()
    def add_node(plan_id: int, title: str, description: str = "") -> tuple[int, int]:
        """Add a node (one piece of work) to a plan. Returns (plan_id, node_id).

        The description should be a self-contained instruction for the agent doing the work.
        """
        try:
            ref = store.add_node(plan_id, title, description)
        except PlanError as e:
            raise ToolError(str(e)) from e
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
    def update_node(plan_id: int, node_id: int, title: str | None = None, description: str | None = None) -> str:
        """Change the title and/or description of a node."""
        try:
            store.get_node(plan_id, node_id)
        except PlanError as e:
            raise ToolError(str(e)) from e
        fields = {k: v for k, v in (("title", title), ("description", description)) if v is not None}
        store.update_node(plan_id, node_id, **fields)
        engine.notify_plan(plan_id)
        return "updated"

    @mcp.tool()
    def get_plan(plan_id: int) -> dict[str, Any]:
        """Return a plan with its nodes (status, results, subplan ids) and edges."""
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
