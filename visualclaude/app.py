"""FastAPI app: REST + Server-Sent Events for the UI, MCP at /mcp."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agents import ClaudeBackend, MockBackend
from .engine import Bus, Engine, EngineConfig
from .mcp_tools import build_mcp
from .store import PlanError, Store

STATIC = Path(__file__).parent / "static"


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    db: str = "visualclaude.db"
    workdir: str = "workspace"
    agent: str = "claude"          # 'claude' | 'mock'
    max_concurrency: int = 3
    permission_mode: str = "acceptEdits"
    model: str | None = None
    max_turns: int | None = None
    expose_mcp_to_agents: bool = True
    mock_speed: float = 1.0

    @property
    def mcp_url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/mcp"


class StartBody(BaseModel):
    executor: str = "builtin"


def create_app(settings: Settings) -> FastAPI:
    store = Store(settings.db)
    bus = Bus()
    if settings.agent == "mock":
        backend: Any = MockBackend(speed=settings.mock_speed)
    else:
        backend = ClaudeBackend(
            permission_mode=settings.permission_mode,
            model=settings.model,
            max_turns=settings.max_turns,
            mcp_url=settings.mcp_url if settings.expose_mcp_to_agents else None,
        )
    engine = Engine(
        store,
        bus,
        backend,
        EngineConfig(
            workdir=settings.workdir,
            max_concurrency=settings.max_concurrency,
            expose_mcp_to_agents=settings.expose_mcp_to_agents,
        ),
    )
    mcp = build_mcp(engine)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=settings.host,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp.session_manager.run():
            await engine.start()
            try:
                yield
            finally:
                await engine.shutdown()

    app = FastAPI(title="VisualClaude", lifespan=lifespan)
    app.state.engine = engine
    app.router.routes.extend(mcp_app.routes)  # POST/GET /mcp

    def _err(e: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(e))

    # ------------------------------------------------------------- views
    def node_view(plan_id: int, n: dict[str, Any]) -> dict[str, Any]:
        sub = store.subplan_of(plan_id, n["node_id"])
        return {
            **n,
            "subplan_id": sub,
            "progress": engine.rollup(sub) if sub is not None else None,
        }

    def active_nodes(root_id: int) -> list[dict[str, Any]]:
        """Nodes in the whole plan tree that currently have an agent working on them."""
        out = []
        for pid in store.descendant_plans(root_id):
            for n in store.nodes(pid):
                if n["status"] == "running" and n["agent_kind"] and not n["agent_done"]:
                    out.append({"plan_id": pid, "node_id": n["node_id"], "title": n["title"],
                                "agent_kind": n["agent_kind"], "agent_name": n["agent_name"]})
        return sorted(out, key=lambda x: (x["plan_id"], x["node_id"]))

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
        return {
            "plan": p,
            "root": {"id": root["id"], "started": bool(root["started"]), "executor": root["executor"]},
            "status": engine.plan_status(plan_id),
            "progress": engine.rollup(plan_id),
            "ancestors": store.ancestors(plan_id),
            "active": active_nodes(root["id"]),
            "nodes": [node_view(plan_id, n) for n in store.nodes(plan_id)],
            "edges": store.edges(plan_id),
        }

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

    @app.post("/api/plans/{plan_id}/nodes/{node_id}/stop")
    async def stop_node(plan_id: int, node_id: int) -> dict[str, str]:
        await engine.stop_node(plan_id, node_id)
        return {"ok": "stopped"}

    # -------------------------------------------------------------- live
    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        q = bus.subscribe()

        async def gen():
            try:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(msg, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
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
