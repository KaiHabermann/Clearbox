"""FastAPI app: REST + Server-Sent Events for the UI, MCP at /mcp (/mcp/observe for a session
tracking itself, /mcp-agent/<token> for workers)."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import ipaddress
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.routing import Route

from . import bootstrap
from .harness.registry import HarnessDefaults, HarnessRegistry
from .engine import Bus, ConflictCache, Engine, EngineConfig
from .export import plan_markdown
from .mcp_tools import build_mcp
from .store import CAPABILITIES, PlanError, Store, keys_conflict, public_profile

STATIC = Path(__file__).parent / "static"


def _version() -> str:
    try:
        return importlib.metadata.version("clearbox")
    except importlib.metadata.PackageNotFoundError:  # running from a plain checkout
        return "unknown"
WILDCARD_HOSTS = ("0.0.0.0", "::", "")


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    db: str = "clearbox.db"
    workdir: str = "workspace"
    agent: str = "claude"          # 'claude' | 'mock': the harness used when no agent profile applies
    default_agent: str | None = None  # --agent-profile: name of the server-wide default profile
    max_concurrency: int = 3
    permission_mode: str = "acceptEdits"
    default_capability: str = "full"  # --default-capability: what an unset capability means for workers
    model: str | None = None
    max_turns: int | None = None
    expose_mcp_to_agents: bool = True
    mock_speed: float = 1.0
    # --openai-*: only used to bootstrap the built-in "openai" profile when it does not exist yet
    openai_base_url: str | None = None
    openai_model: str | None = None
    openai_api_key_env: str = "OPENAI_API_KEY"
    allowed_hosts: list[str] = field(default_factory=list)  # --allowed-host: extra names for the Host check

    @property
    def mcp_url(self) -> str:
        host = "127.0.0.1" if self.host in WILDCARD_HOSTS else self.host
        return f"http://{host}:{self.port}/mcp"

    @property
    def observe_mcp_url(self) -> str:
        """The slim endpoint for a session that tracks its own work (executor "external")."""
        return self.mcp_url + "/observe"

    @property
    def agent_mcp_url(self) -> str:
        """Base of the scoped endpoint built-in worker agents get; the engine appends a run's token."""
        return self.mcp_url + "-agent"


class StartBody(BaseModel):
    executor: str = "builtin"


class PlanPatch(BaseModel):
    """Partial update of a (top-level) plan's own settings; fields left out are unchanged.

    max_parallel: cap how many nodes of this plan's whole tree run at once; null removes it.
    test_instructions: how the project's tests are run, given verbatim to workers and reviewers;
    null or "" removes it. Top-level plans only; subplans inherit it.
    isolate_steps: run each built-in step in its own git worktree and apply its changes back when
    it finishes. Top-level plans in a git repository only, and not while a step is running.
    """

    max_parallel: int | None = None
    test_instructions: str | None = None
    isolate_steps: bool | None = None


class WorkdirBody(BaseModel):
    """Change a top-level plan's working directory; null/"" resets it to the default."""

    dir: str | None = None


class CompleteBody(BaseModel):
    """Mark a node done by hand; null result = reuse the agent's own closing summary."""

    result: str | None = None


class KindBody(BaseModel):
    """Who does a node: "agent" (default) or "user" -- the user's own job, no agent runs it and
    the plan waits there until the user marks it done (store.NODE_KINDS)."""

    kind: str = "agent"


class AgentPatch(BaseModel):
    """Partial update of an agent profile; fields left out of the body are unchanged.

    ``api_key``: missing (or null) = keep the stored key, ``""`` = clear it, any
    other string = replace it. The UI's edit form relies on this so it never has
    to know, or re-send, the secret.
    """

    name: str | None = None
    harness: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    api_key: str | None = None
    params: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    permission_mode: str | None = None
    system_append: str | None = None
    max_parallel: int | None = None
    description: str | None = None
    capability: str | None = None   # store.CAPABILITIES: read | run | edit | full; null = the role default
    test_level: str | None = None   # store.TEST_LEVELS: none | light | standard | thorough; null = standard


class AgentBody(AgentPatch):
    """A new agent profile. Unlike MCP, REST may take a literal ``api_key``: it is
    typed into a localhost form by the operator, not into a model's context."""

    name: str
    harness: str


class AgentRef(BaseModel):
    """Assign an agent profile by id or name; null clears the assignment (= inherit)."""

    agent: int | str | None = None


NodeAgentBody = PlanAgentBody = AgentRef


class FilesBody(BaseModel):
    """Replace the files a node may create or modify (paths relative to the plan's working dir); [] clears."""

    files: list[str] = []


class ResultsFromBody(BaseModel):
    """Replace the earlier nodes (same plan, must be dependencies) whose result text a node receives; [] clears."""

    node_ids: list[int] = []


class ReviewBody(BaseModel):
    """Optional one-off agent for a manual review trigger; null/omitted falls back to the plan's
    review agent (set with PUT .../review-agent). Used for this run only, it does not change that
    setting -- this is what makes reviewing a finished run practical even if it never had one."""

    agent: int | str | None = None


class ReviewAgentBody(BaseModel):
    """Set (or null to clear) the agent profile that AUTOMATICALLY reviews a plan's steps and,
    once it finishes, the plan as a whole. Manual review triggers keep working while this is unset."""

    agent: int | str | None = None


class ReworkBody(BaseModel):
    """What a rework should address; null/omitted uses the node's review summary and findings."""

    instructions: str | None = None


@dataclass
class FileScope:
    """One request's view of a plan tree's declared files and reviews, shared by every node_view call.

    `/api/plans/{id}` renders all nodes at once, so the tree's file map and reviews are each
    fetched once (one query apiece) and the dependency closures are memoized across nodes.
    """

    tree: dict[tuple[int, int], list[dict[str, Any]]]
    cache: ConflictCache
    titles: dict[tuple[int, int], str]
    reviews: dict[tuple[int, int], dict[str, Any]]


class AgentTokenGate:
    """ASGI wrapper for /mcp-agent/{token}: a token that names no running built-in node (never
    minted, or its run has ended) gets a 404 before any MCP traffic. The tools check it again,
    since a run can end between two calls of one session."""

    def __init__(self, engine: Engine, app: Any):
        self.engine, self.app = engine, app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self.engine.agent_run(scope["path_params"]["token"]) is None:
            await JSONResponse({"detail": "unknown or expired agent token"}, status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _host_entry(name: str) -> str:
    """A bare IPv6 address as it appears in a Host header (``::1`` -> ``[::1]``); others unchanged."""
    return f"[{name}]" if name.count(":") > 1 and not name.startswith("[") else name


def _has_port(entry: str) -> bool:
    return "]:" in entry if entry.startswith("[") else ":" in entry


def allowed_hosts(settings: Settings) -> set[str]:
    """Host header values the server answers to: loopback, the --host it binds (unless a
    wildcard) and every --allowed-host. A name without a port allows it with the server's port and
    without one (a reverse proxy or a default port drops it); ``name:port`` allows just that."""
    names = ["127.0.0.1", "localhost", "::1", *settings.allowed_hosts]
    if settings.host not in WILDCARD_HOSTS:
        names.append(settings.host)
    out: set[str] = set()
    for n in names:
        n = _host_entry(n.strip().lower())
        if not n:
            continue
        out.add(n)
        if not _has_port(n):
            out.add(f"{n}:{settings.port}")
    return out


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class HostCheck:
    """ASGI middleware in front of everything (REST, /api/stream, the MCP endpoints, the UI).

    A request whose Host is not one of ours is a DNS-rebinding page -- its own name resolving to
    127.0.0.1, so the browser calls it same-origin -- and gets a 421. A request whose Origin is
    not one of ours is another web page calling across origins and gets a 403. Browsers send
    Origin on every cross-origin and every non-GET fetch, so the UI's own requests carry ours;
    curl, MCP clients and the tests send none and pass. Headers only, so streams are untouched.
    """

    def __init__(self, app: Any, allowed: set[str]):
        self.app, self.allowed = app, allowed

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = {k: v.decode("latin-1") for k, v in scope["headers"]}
            host = headers.get(b"host", "").lower()
            origin = headers.get(b"origin")
            reason = None
            if host not in self.allowed:
                reason, code = f"Host {host!r} is not allowed (see --allowed-host)\n", 421
            elif origin is not None:
                o = urlsplit(origin)
                if o.scheme not in ("http", "https") or o.netloc.lower() not in self.allowed:
                    reason, code = f"cross-origin request from {origin!r} refused\n", 403
            if reason:
                await PlainTextResponse(reason, status_code=code)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def review_view(r: dict[str, Any] | None) -> dict[str, Any] | None:
    """A review row as the UI/REST sees it: verdict, summary, findings, agent, when. None if unreviewed."""
    if r is None:
        return None
    return {
        "verdict": r["verdict"],
        "summary": r["summary"],
        "findings": r["findings"],
        "agent": r["agent_name"],
        "when": r["created_at"],
    }


def create_app(settings: Settings) -> FastAPI:
    if settings.default_capability not in CAPABILITIES:
        raise bootstrap.StartupError(f"--default-capability {settings.default_capability!r}: "
                                     f"expected one of {', '.join(CAPABILITIES)}")
    store = Store(settings.db)
    bus = Bus()
    bootstrap.ensure_profiles(store, bootstrap.OpenAiBootstrap(
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        api_key_env=settings.openai_api_key_env,
    ))
    default = bootstrap.resolve_default(store, settings.default_agent, mock=settings.agent == "mock")
    harnesses = HarnessRegistry(store, HarnessDefaults(
        agent=settings.agent,
        permission_mode=settings.permission_mode,
        model=settings.model,
        max_turns=settings.max_turns,
        mock_speed=settings.mock_speed,
        default_profile=default["id"],
        default_capability=settings.default_capability,
    ))
    bootstrap.validate(harnesses, store, default)
    engine = Engine(
        store,
        bus,
        harnesses,
        EngineConfig(
            workdir=settings.workdir,
            max_concurrency=settings.max_concurrency,
            agent_mcp_url=settings.agent_mcp_url if settings.expose_mcp_to_agents else None,
            server_port=settings.port,
            snapshot_dir=str(Path(settings.db).resolve().parent / "snapshots"),
        ),
    )
    mcp, agent_mcp, observe_mcp = build_mcp(engine)
    # The MCP SDK has a Host/Origin check of its own, on by default only for a loopback --host
    # (and then for any port). HostCheck already covers every MCP endpoint; giving the SDK the
    # same allowlist keeps the two from disagreeing, e.g. on an --allowed-host name.
    hosts = allowed_hosts(settings)
    security = TransportSecuritySettings(
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(f"{s}://{h}" for s in ("http", "https") for h in hosts),
    )
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    observe_mcp_app = observe_mcp.streamable_http_app(
        streamable_http_path="/mcp/observe",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    agent_mcp_app = agent_mcp.streamable_http_app(
        streamable_http_path="/mcp-agent/{token}",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp.session_manager.run(), observe_mcp.session_manager.run(),                 agent_mcp.session_manager.run():
            await engine.start()
            try:
                yield
            finally:
                # Wake every open /api/stream generator so it returns. Without this an open
                # browser tab holds its SSE response forever and Ctrl+C never completes.
                bus.publish({"type": "shutdown"})
                await engine.shutdown()

    app = FastAPI(title="Clearbox", lifespan=lifespan)
    app.add_middleware(HostCheck, allowed=hosts)
    app.state.engine = engine
    app.state.agents_line = bootstrap.agents_line(harnesses, store, default["id"])
    app.state.capability_line = bootstrap.capability_line(harnesses, default)
    app.router.routes.extend(mcp_app.routes)  # POST/GET /mcp
    app.router.routes.extend(observe_mcp_app.routes)  # /mcp/observe: exact routes, no mount, so no clash
    (agent_route,) = agent_mcp_app.routes
    app.router.routes.append(Route(agent_route.path, endpoint=AgentTokenGate(engine, agent_route.endpoint)))

    def _err(e: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(e))

    # ------------------------------------------------------------- views
    def file_scope(plan_id: int) -> FileScope:
        root_id = store.root_of(plan_id)["id"]
        tree = store.files_of_plan_tree(root_id)
        reviews = {
            (pid, r["node_id"]): r
            for pid in store.descendant_plans(root_id)
            for r in store.reviews_of_plan(pid) if r["node_id"] is not None
        }
        return FileScope(tree=tree, cache=ConflictCache(), titles={}, reviews=reviews)

    def node_ref(scope: FileScope, ref: tuple[int, int], files: list[str]) -> dict[str, Any]:
        if ref not in scope.titles:
            scope.titles[ref] = store.get_node(*ref)["title"]
        return {"plan_id": ref[0], "node_id": ref[1], "title": scope.titles[ref], "files": files}

    def shared_files(scope: FileScope, mine: tuple[int, int], other: tuple[int, int]) -> list[str]:
        """The files two nodes compete for: per conflicting pair the narrower path (a file
        inside a declared directory), in this node's own spelling when both name the same one."""
        out: list[str] = []
        for a in scope.tree.get(mine, []):
            for b in scope.tree.get(other, []):
                if keys_conflict(a["key"], b["key"]):
                    path = b["path"] if len(b["key"]) > len(a["key"]) else a["path"]
                    if path not in out:
                        out.append(path)
        return out

    def file_fields(plan_id: int, n: dict[str, Any], scope: FileScope) -> dict[str, Any]:
        """files / conflicts / blocked_by: everything the UI needs to stagger and explain a node.

        The conflict rule and the hold-back decision both come from the engine
        (file_conflicts, files_blocked_by); nothing here re-derives them.
        """
        me = (plan_id, n["node_id"])
        if me not in scope.tree:
            return {"files": [], "conflicts": [], "blocked_by": None}
        conflicts = [
            node_ref(scope, ref, shared_files(scope, me, ref))
            for ref in engine.file_conflicts(plan_id, n["node_id"], tree=scope.tree, cache=scope.cache)
        ]
        blocked_by = None
        if n["status"] == "ready":
            hit = engine.files_blocked_by(plan_id, n["node_id"])
            if hit is not None:
                ref, reason = hit
                blocked_by = {**node_ref(scope, ref, shared_files(scope, me, ref)), "reason": reason}
        return {
            "files": [f["path"] for f in scope.tree[me]],
            "conflicts": conflicts,
            "blocked_by": blocked_by,
        }

    def node_view(plan_id: int, n: dict[str, Any], scope: FileScope | None = None) -> dict[str, Any]:
        scope = scope or file_scope(plan_id)
        sub = store.subplan_of(plan_id, n["node_id"])
        return {
            **n,
            "kind": n.get("kind") or "agent",
            "waiting_for_user": engine.waiting_for_user(n),
            "subplan_id": sub,
            "progress": engine.rollup(sub) if sub is not None else None,
            "agent": harnesses.source_for(plan_id, n["node_id"]),
            "agent_override": n.get("agent_profile_id"),
            "review": review_view(scope.reviews.get((plan_id, n["node_id"]))),
            "results_from": store.node_results_from(plan_id, n["node_id"]),
            "started_dependants": started_dependants(plan_id, n["node_id"]),
            **file_fields(plan_id, n, scope),
        }

    def started_dependants(plan_id: int, node_id: int) -> list[dict[str, Any]]:
        """Direct dependants that already ran (or are running) -- what a rework of this node
        would leave built on its old output."""
        return store._q(
            "SELECT n.node_id, n.title, n.status FROM edges e JOIN nodes n "
            "ON n.plan_id=e.plan_id AND n.node_id=e.dst "
            "WHERE e.plan_id=? AND e.src=? AND n.status NOT IN ('pending', 'ready') ORDER BY n.node_id",
            (plan_id, node_id),
        )

    def agent_view(p: dict[str, Any], default_id: int | None, usage: dict[int, int]) -> dict[str, Any]:
        """The only shape an agent profile leaves this API in (public_profile drops api_key)."""
        out = public_profile(p)
        assert "api_key" not in out
        out["default"] = p["id"] == default_id
        out["in_use"] = usage.get(p["id"], 0)
        return {**out, **capability_fields(p)}

    def capability_fields(p: dict[str, Any]) -> dict[str, Any]:
        """What a worker (and a reviewer) on this profile may do, as resolved by the registry."""
        cap = harnesses.capability_for(p)
        return {"effective_capability": cap["capability"], "capability_source": cap["source"],
                "review_capability": harnesses.review_capability_for(p)}

    def agent_usage() -> dict[int, int]:
        """profile id -> number of nodes and plans that reference it directly."""
        rows = store._q(
            "SELECT agent_profile_id AS id, COUNT(*) AS n FROM nodes WHERE agent_profile_id IS NOT NULL "
            "GROUP BY agent_profile_id UNION ALL "
            "SELECT default_agent_profile_id, COUNT(*) FROM plans WHERE default_agent_profile_id IS NOT NULL "
            "GROUP BY default_agent_profile_id"
        )
        usage: dict[int, int] = {}
        for r in rows:
            usage[r["id"]] = usage.get(r["id"], 0) + r["n"]
        return usage

    def default_agent_id() -> int | None:
        ref = harnesses.defaults.default_profile
        if ref is None:
            return None
        try:
            return store.resolve_profile_ref(ref)["id"]
        except PlanError:
            return None

    def one_agent(agent_id: int) -> dict[str, Any]:
        try:
            p = store.get_profile(agent_id)
        except PlanError as e:
            raise HTTPException(404, str(e)) from e
        return agent_view(p, default_agent_id(), agent_usage())

    def profile_id_of(ref: int | str | None) -> int | None:
        if ref is None or ref == "":
            return None
        try:
            return store.resolve_profile_ref(ref)["id"]
        except PlanError as e:
            raise _err(e) from e

    def agents_changed() -> None:
        bus.publish({"type": "agents"})

    def active_nodes(root_id: int) -> list[dict[str, Any]]:
        """Nodes in the whole plan tree that currently have an agent working on them."""
        out = []
        for pid in store.descendant_plans(root_id):
            for n in store.nodes(pid):
                if n["status"] == "running" and n["agent_kind"] and not n["agent_done"]:
                    out.append({"plan_id": pid, "node_id": n["node_id"], "title": n["title"],
                                "agent_kind": n["agent_kind"], "agent_name": n["agent_name"]})
        return sorted(out, key=lambda x: (x["plan_id"], x["node_id"]))

    def waiting_nodes(root_id: int) -> list[dict[str, Any]]:
        """User nodes in the whole plan tree that are waiting for the user to mark them done."""
        return [
            {"plan_id": pid, "node_id": n["node_id"], "title": n["title"]}
            for pid in store.descendant_plans(root_id)
            for n in store.nodes(pid) if engine.waiting_for_user(n)
        ]

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        # `clearbox ensure` asks this to tell a running Clearbox from something else on the port
        return {"app": "clearbox", "version": _version()}

    @app.get("/api/plans")
    def list_plans() -> list[dict[str, Any]]:
        return [
            {
                "id": p["id"],
                "title": p["title"],
                "started": bool(p["started"]),
                "executor": p["executor"],
                "status": engine.plan_status(p["id"]),
                "progress": engine.rollup(p["id"]),
                "created_at": p["created_at"],
                "working_directory": engine.plan_workdir(p["id"]),
            }
            for p in store.list_root_plans()
        ]

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: int) -> dict[str, Any]:
        try:
            p = store.get_plan(plan_id)
            root = store.root_of(plan_id)
        except PlanError as e:
            raise HTTPException(404, str(e)) from e
        scope = file_scope(plan_id)
        default_agent = None
        if p.get("default_agent_profile_id") is not None:
            with contextlib.suppress(PlanError):
                d = store.get_profile(p["default_agent_profile_id"])
                default_agent = {"id": d["id"], "name": d["name"], "harness": d["harness"], "model": d["model"]}
        return {
            "plan": p,
            "default_agent": default_agent,
            "working_directory": engine.plan_workdir(plan_id),
            "test_instructions": engine.plan_test_instructions(plan_id),
            "root": {"id": root["id"], "started": bool(root["started"]), "executor": root["executor"],
                     "isolate_steps": bool(root["isolate_steps"])},
            "status": engine.plan_status(plan_id),
            "progress": engine.rollup(plan_id),
            "ancestors": store.ancestors(plan_id),
            "active": active_nodes(root["id"]),
            "waiting": waiting_nodes(root["id"]),
            "review": review_view(store.review(plan_id)),
            "review_agent_profile_id": p.get("review_agent_profile_id"),
            "nodes": [node_view(plan_id, n, scope) for n in store.nodes(plan_id)],
            "edges": store.edges(plan_id),
        }

    @app.get("/api/plans/{plan_id}/export.md")
    def export_plan(plan_id: int, diffs: bool = True) -> PlainTextResponse:
        """The plan (a subplan: just that subplan) as Markdown for a PR description."""
        try:
            text = plan_markdown(store, plan_id, diffs)
        except PlanError as e:
            raise HTTPException(404, str(e)) from e
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/api/plans/{plan_id}/nodes/{node_id}")
    def get_node(plan_id: int, node_id: int, after: int = 0) -> dict[str, Any]:
        try:
            n = store.get_node(plan_id, node_id)
        except PlanError as e:
            raise HTTPException(404, str(e)) from e
        return {
            "node": node_view(plan_id, n),
            "predecessors": store.predecessors(plan_id, node_id),
            "events": store.events(plan_id, node_id, after),
        }

    @app.get("/api/plans/{plan_id}/nodes/{node_id}/context")
    def get_node_context(plan_id: int, node_id: int) -> dict[str, Any]:
        """The prompt build_context makes for this node from the plan as it is now."""
        try:
            return {"context": engine.build_context(plan_id, node_id)}
        except PlanError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/plans/{plan_id}/nodes/{node_id}/diffs")
    def get_node_diffs(plan_id: int, node_id: int) -> dict[str, Any]:
        try:
            store.get_node(plan_id, node_id)
        except PlanError as e:
            raise HTTPException(404, str(e)) from e
        return {"diffs": store.node_diffs(plan_id, node_id)}

    @app.put("/api/plans/{plan_id}/review-agent")
    async def set_plan_review_agent(plan_id: int, body: ReviewAgentBody) -> dict[str, Any]:
        """Set (or null to clear) the agent that automatically reviews this plan; manual triggers
        (POST .../review, POST .../nodes/{node_id}/review) keep working either way."""
        pid = profile_id_of(body.agent)
        try:
            store.set_plan_review_agent(plan_id, pid)
            root_id = store.root_of(plan_id)["id"]
        except PlanError as e:
            raise _err(e) from e
        bus.publish({"type": "plan", "plan_id": plan_id, "root_id": root_id})
        return {"ok": "set", "plan_id": plan_id, "review_agent_profile_id": pid}

    @app.post("/api/plans/{plan_id}/nodes/{node_id}/review")
    async def review_node_now(plan_id: int, node_id: int, body: ReviewBody | None = None) -> dict[str, Any]:
        """Review this node against its own diff now; re-reviews if it already has one.

        body.agent, if given, is used for this run only (see ReviewBody). Falls back to the
        plan's review agent; 400s with a clear message if neither is set.
        """
        try:
            r = await engine.review_now(plan_id, node_id, body.agent if body else None)
        except PlanError as e:
            raise _err(e) from e
        return {"review": review_view(r)}

    @app.post("/api/plans/{plan_id}/review")
    async def review_plan_now(plan_id: int, body: ReviewBody | None = None) -> dict[str, Any]:
        """Review this plan as a whole for oversights now; re-reviews if it already has one.

        Reads titles, descriptions, results and each step's own review verdict -- never diffs or
        source (that is the per-node review's job). body.agent as in review_node_now.
        """
        try:
            r = await engine.review_now(plan_id, None, body.agent if body else None)
        except PlanError as e:
            raise _err(e) from e
        return {"review": review_view(r)}

    @app.patch("/api/plans/{plan_id}")
    def update_plan(plan_id: int, body: PlanPatch) -> dict[str, Any]:
        """Change a plan's own settings (max_parallel, test_instructions, isolate_steps); only given
        fields change."""
        fields = body.model_dump(exclude_unset=True)
        if "test_instructions" in fields:
            fields["test_instructions"] = (fields["test_instructions"] or "").strip() or None
        isolate = fields.pop("isolate_steps", None)
        try:
            if isolate is not None:
                engine.set_isolate_steps(plan_id, isolate)
            store.update_plan(plan_id, **fields)
            p = store.get_plan(plan_id)
            root_id = store.root_of(plan_id)["id"]
        except PlanError as e:
            raise _err(e) from e
        bus.publish({"type": "plan", "plan_id": plan_id, "root_id": root_id})
        engine.kick()  # a raised (or cleared) limit may let waiting nodes start
        return {"plan": p}

    @app.put("/api/plans/{plan_id}/workdir")
    def set_plan_workdir(plan_id: int, body: WorkdirBody) -> dict[str, Any]:
        """Change a top-level plan's working directory; only while it has not been started."""
        try:
            engine.set_plan_workdir(plan_id, body.dir or None)
            p = store.get_plan(plan_id)
        except PlanError as e:
            raise _err(e) from e
        bus.publish({"type": "plan", "plan_id": plan_id, "root_id": plan_id})
        return {"plan": p, "working_directory": engine.plan_workdir(plan_id)}

    # ---------------------------------------------------------- controls
    @app.post("/api/plans/{plan_id}/start")
    def start(plan_id: int, body: StartBody) -> dict[str, str]:
        try:
            engine.start_plan(store.root_of(plan_id)["id"], body.executor)
        except PlanError as e:
            raise _err(e) from e
        return {"ok": "started"}

    @app.post("/api/plans/{plan_id}/stop")
    async def stop(plan_id: int) -> dict[str, str]:
        await engine.stop_plan(plan_id)
        return {"ok": "stopped"}

    @app.post("/api/plans/{plan_id}/reset")
    async def reset(plan_id: int) -> dict[str, str]:
        await engine.reset_plan(plan_id)
        return {"ok": "reset"}

    @app.post("/api/plans/{plan_id}/retry-failed")
    def retry_failed(plan_id: int) -> dict[str, Any]:
        try:
            count = engine.retry_failed(plan_id)
        except PlanError as e:
            raise _err(e) from e
        return {"ok": "retrying", "count": count}

    @app.delete("/api/plans/{plan_id}")
    async def delete(plan_id: int) -> dict[str, str]:
        try:
            p = store.get_plan(plan_id)
        except PlanError as e:
            raise HTTPException(404, str(e)) from e
        if p["parent_plan_id"] is not None:
            raise HTTPException(400, "delete the root plan instead")
        await engine.stop_plan(plan_id)
        store.delete_plan(plan_id)
        bus.publish({"type": "plan", "plan_id": plan_id, "root_id": plan_id, "deleted": True})
        return {"ok": "deleted"}

    @app.post("/api/plans/{plan_id}/nodes/{node_id}/retry")
    def retry(plan_id: int, node_id: int) -> dict[str, str]:
        try:
            engine.retry_node(plan_id, node_id)
        except PlanError as e:
            raise _err(e) from e
        return {"ok": "retrying"}

    @app.post("/api/plans/{plan_id}/nodes/{node_id}/rework")
    def rework(plan_id: int, node_id: int, body: ReworkBody | None = None) -> dict[str, Any]:
        """Re-run this one finished node to address its review findings (or body.instructions).

        Dependants are left as they are; a step review runs again when it finishes if the plan
        has a review agent. 400 when the node is running or there is nothing to address.
        """
        try:
            engine.rework_node(plan_id, node_id, body.instructions if body else None)
            n = store.get_node(plan_id, node_id)
        except PlanError as e:
            raise _err(e) from e
        return {"node": node_view(plan_id, n)}

    @app.post("/api/plans/{plan_id}/nodes/{node_id}/complete")
    async def complete_node(plan_id: int, node_id: int, body: CompleteBody | None = None) -> dict[str, str]:
        """Stop the node's agent and mark it done, so its dependants start.

        `result` is what dependants read; left out, the agent's own closing
        summary is used when the transcript has one.
        """
        try:
            await engine.complete_node_manually(plan_id, node_id, (body.result if body else None))
        except PlanError as e:
            raise _err(e) from e
        return {"ok": "completed"}

    @app.post("/api/plans/{plan_id}/nodes/{node_id}/stop")
    async def stop_node(plan_id: int, node_id: int) -> dict[str, str]:
        await engine.stop_node(plan_id, node_id)
        return {"ok": "stopped"}

    if settings.agent == "mock":
        # --mock only, for tests/test_agent_scope.py: the scoped endpoint a running node's agent
        # was handed, so a test can call it as that agent would. A real server never hands it out.
        @app.get("/api/debug/plans/{plan_id}/nodes/{node_id}/agent-mcp-url")
        async def agent_mcp_url(plan_id: int, node_id: int) -> dict[str, str | None]:
            token = engine.agent_token(plan_id, node_id)
            return {"url": f"{settings.agent_mcp_url}/{token}" if token else None}

    # ------------------------------------------------------------ agents
    @app.get("/api/agents")
    async def list_agents() -> list[dict[str, Any]]:
        default_id, usage = default_agent_id(), agent_usage()
        return [agent_view(p, default_id, usage) for p in store.list_profiles()]

    @app.post("/api/agents")
    async def create_agent(body: AgentBody) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        if not fields.get("api_key"):
            fields.pop("api_key", None)
        try:
            agent_id = store.create_profile(**fields)
        except PlanError as e:
            raise _err(e) from e
        agents_changed()
        return one_agent(agent_id)

    @app.patch("/api/agents/{agent_id}")
    async def update_agent(agent_id: int, body: AgentPatch) -> dict[str, Any]:
        """Partial update: only fields present in the body change.

        api_key: absent or null = leave the stored key unchanged; "" = clear it;
        any other string = replace it. Other fields sent as null are reset.
        """
        fields = body.model_dump(exclude_unset=True)
        if fields.get("api_key") is None:
            fields.pop("api_key", None)
        one_agent(agent_id)  # 404 for an unknown profile, 400 below for invalid fields
        try:
            store.update_profile(agent_id, **fields)
        except PlanError as e:
            raise _err(e) from e
        agents_changed()
        return one_agent(agent_id)

    @app.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: int) -> dict[str, str]:
        """Delete a profile; nodes and plans that used it go back to inheriting."""
        try:
            store.delete_profile(agent_id)
        except PlanError as e:
            raise HTTPException(404, str(e)) from e
        agents_changed()
        return {"ok": "deleted"}

    @app.post("/api/agents/{agent_id}/test")
    async def test_agent(agent_id: int) -> dict[str, Any]:
        """Connection test (HarnessRegistry.test)."""
        try:
            return await harnesses.test(agent_id)
        except PlanError as e:
            raise HTTPException(404, str(e)) from e

    @app.put("/api/plans/{plan_id}/nodes/{node_id}/agent")
    async def set_node_agent(plan_id: int, node_id: int, body: NodeAgentBody) -> dict[str, Any]:
        """Set (id or name) or clear (null) a node's own agent; takes effect on its next run."""
        pid = profile_id_of(body.agent)
        try:
            store.set_node_profile(plan_id, node_id, pid)
            n = store.get_node(plan_id, node_id)
            root_id = store.root_of(plan_id)["id"]
        except PlanError as e:
            raise _err(e) from e
        bus.publish({"type": "plan", "plan_id": plan_id, "root_id": root_id})
        agents_changed()
        return node_view(plan_id, n)

    @app.put("/api/plans/{plan_id}/nodes/{node_id}/kind")
    async def set_node_kind(plan_id: int, node_id: int, body: KindBody) -> dict[str, Any]:
        """Make a node the user's job ("user") or an agent's ("agent"); only while it is pending or ready."""
        try:
            store.set_node_kind(plan_id, node_id, body.kind)
            n = store.get_node(plan_id, node_id)
        except PlanError as e:
            raise _err(e) from e
        engine.notify_plan(plan_id)
        engine.kick()  # a ready node that just became an agent's job can start now
        return node_view(plan_id, n)

    @app.put("/api/plans/{plan_id}/nodes/{node_id}/files")
    async def set_node_files(plan_id: int, node_id: int, body: FilesBody) -> dict[str, Any]:
        """Replace the files a node may create or modify; only while it is pending or ready."""
        try:
            store.set_node_files(plan_id, node_id, body.files)
            n = store.get_node(plan_id, node_id)
        except PlanError as e:
            raise _err(e) from e
        # One plan message is enough: it carries the tree's root_id and every open view of that
        # tree refetches, including views of other subplans whose conflicts just changed.
        engine.notify_plan(plan_id)
        # A narrower set can release a held-back node right away.
        engine.kick()
        return node_view(plan_id, n)

    @app.put("/api/plans/{plan_id}/nodes/{node_id}/results-from")
    async def set_node_results_from(plan_id: int, node_id: int, body: ResultsFromBody) -> dict[str, Any]:
        """Replace the earlier nodes whose result text a node receives; only while it is pending or ready."""
        try:
            engine.check_results_from(plan_id, node_id, body.node_ids)
            store.set_node_results_from(plan_id, node_id, body.node_ids)
            n = store.get_node(plan_id, node_id)
        except PlanError as e:
            raise _err(e) from e
        engine.notify_plan(plan_id)
        return node_view(plan_id, n)

    @app.put("/api/plans/{plan_id}/agent")
    async def set_plan_agent(plan_id: int, body: PlanAgentBody) -> dict[str, Any]:
        """Set (id or name) or clear (null) a plan's default agent; subplans inherit it."""
        pid = profile_id_of(body.agent)
        try:
            store.set_plan_profile(plan_id, pid)
            root_id = store.root_of(plan_id)["id"]
        except PlanError as e:
            raise _err(e) from e
        bus.publish({"type": "plan", "plan_id": plan_id, "root_id": root_id})
        agents_changed()
        return {"ok": "set", "plan_id": plan_id, "default_agent_profile_id": pid}

    # -------------------------------------------------------------- live
    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        q = bus.subscribe()

        def stopping() -> bool:
            """True once Ctrl+C arrived. Checked here because uvicorn waits for open
            connections before the lifespan shutdown runs, and this response never ends
            on its own -- an open browser tab would otherwise block the whole exit."""
            server = getattr(app.state, "server", None)
            return bool(server is not None and server.should_exit)

        async def gen():
            last_ping = time.monotonic()
            try:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    if stopping() or await request.is_disconnected():
                        break
                    try:
                        # short wait so a stop is noticed quickly; the ping stays on 15s
                        msg = await asyncio.wait_for(q.get(), timeout=1)
                        if msg.get("type") == "shutdown":
                            break
                        yield f"data: {json.dumps(msg, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        if time.monotonic() - last_ping >= 15:
                            yield ": ping\n\n"
                            last_ping = time.monotonic()
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------ static
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
