"""Scheduling: walks plans from roots to leaves and drives node execution.

Node lifecycle::

    pending --(all prerequisites done)--> ready --(agent attached)--> running --> done | failed

A node that has a subplan skips "ready": once its prerequisites are done it
goes straight to "running" and its subplan is executed; the node is done when
every node of the subplan is done. A task node whose agent creates a subplan
for it while running ("expanding" itself) is done once the agent has finished
*and* that subplan is complete.

With executor="builtin" the service spawns an agent session for every ready
node (up to ``max_concurrency`` at a time). Which agent runs a node is decided
per node by the ``HarnessRegistry`` (node profile -> plan defaults -> server
default -> the CLI-configured harness); a profile's ``max_parallel`` further
caps how many of its nodes run at once. With executor="external", ready nodes
wait until an outside agent claims them through the MCP tools.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .harness import AgentJob, Harness, HarnessError
from .harness.registry import HarnessRegistry
from .store import PlanError, Store

log = logging.getLogger("visualclaude")

SYSTEM_APPEND = """
You are one worker inside VisualClaude, a service that executes a plan (a DAG of work items)
node by node. You are responsible for exactly ONE node. Stay within its scope: other nodes are
handled by other agents, before or after you. Results of prerequisite nodes are given to you.
When you are done, end with a concise summary of what you did and what you produced (files,
decisions, findings). That final message is handed to the nodes that depend on yours.
""".strip()

EXPAND_HINT = """
If your node turns out to be too large for one session, you may break it down: call the
visualclaude MCP tool create_plan with parent_plan_id={plan_id} and parent_node_id={node_id}, then
add_node / add_edge on the returned plan id. Those sub-nodes are executed by other agents after
you finish, so in that case do only the groundwork yourself and describe the breakdown.
""".strip()


class Bus:
    """Tiny in-process pub/sub used to push live updates to the browser."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, msg: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


@dataclass
class EngineConfig:
    workdir: str = "workspace"
    max_concurrency: int = 3
    expose_mcp_to_agents: bool = True


@dataclass
class Engine:
    store: Store
    bus: Bus
    harnesses: HarnessRegistry
    config: EngineConfig = field(default_factory=EngineConfig)

    def __post_init__(self) -> None:
        self._tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._stopping: set[tuple[int, int]] = set()
        # profile id each running builtin task resolved to (None = implicit harness);
        # in memory like _tasks, since builtin runs do not survive a restart either
        self._task_profiles: dict[tuple[int, int], int | None] = {}

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        # Built-in agents do not survive a restart: put their nodes back in the queue.
        for n in self.store._q("SELECT plan_id, node_id FROM nodes WHERE status='running' AND agent_kind='builtin' AND agent_done=0"):
            self._emit(n["plan_id"], n["node_id"], "system", {"text": "agent interrupted by service restart; node re-queued"})
            self._set_status(n["plan_id"], n["node_id"], "pending", agent_kind=None, started_at=None)
        self._loop_task = asyncio.create_task(self._loop())
        self.kick()

    async def shutdown(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        if self._loop_task:
            self._loop_task.cancel()

    def kick(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                self._tick()
            except Exception:  # never let the scheduler die
                log.exception("scheduler tick failed")

    # ------------------------------------------------------------------ helpers
    def _emit(self, plan_id: int, node_id: int, kind: str, data: dict[str, Any]) -> None:
        ev = self.store.add_event(plan_id, node_id, kind, data)
        self.bus.publish({"type": "event", "event": ev})

    def notify_plan(self, plan_id: int) -> None:
        try:
            root = self.store.root_of(plan_id)["id"]
        except PlanError:
            root = plan_id
        self.bus.publish({"type": "plan", "plan_id": plan_id, "root_id": root})

    def _set_status(self, plan_id: int, node_id: int, status: str, **fields: Any) -> None:
        old = self.store.get_node(plan_id, node_id)["status"]
        now = time.time()
        if status == "running" and "started_at" not in fields:
            fields["started_at"] = now
        if status in ("done", "failed"):
            fields.setdefault("finished_at", now)
        self.store.update_node(plan_id, node_id, status=status, **fields)
        if old != status:
            self._emit(plan_id, node_id, "status", {"from": old, "to": status})
        self.notify_plan(plan_id)

    def executor_of(self, plan_id: int) -> str | None:
        root = self.store.root_of(plan_id)
        return root["executor"] if root["started"] else None

    def plan_workdir(self, plan_id: int) -> str:
        root = self.store.root_of(plan_id)
        d = root["cwd"] or os.path.join(self.config.workdir, f"plan-{root['id']}")
        d = os.path.abspath(d)
        os.makedirs(d, exist_ok=True)
        return d

    def rollup(self, plan_id: int) -> dict[str, int]:
        """Status counts over all task (leaf) nodes of a plan, recursively."""
        counts = {"total": 0, "pending": 0, "ready": 0, "running": 0, "done": 0, "failed": 0}
        for n in self.store.nodes(plan_id):
            sub = self.store.subplan_of(plan_id, n["node_id"])
            if sub is not None:
                c = self.rollup(sub)
                for k in counts:
                    counts[k] += c[k]
                if n["agent_kind"] and not n["agent_done"] and n["status"] == "running":
                    counts["running"] += 1  # its own agent is still working
            else:
                counts["total"] += 1
                counts[n["status"]] = counts.get(n["status"], 0) + 1
        return counts

    def plan_status(self, plan_id: int) -> str:
        nodes = self.store.nodes(plan_id)
        if not nodes:
            return "empty"
        st = {n["status"] for n in nodes}
        if st == {"done"}:
            return "done"
        if "failed" in st:
            return "failed"
        if "running" in st or "ready" in st or (st & {"done"}):
            return "running"
        return "pending"

    # ---------------------------------------------------------------- scheduler
    def _tick(self) -> None:
        for _ in range(100):  # iterate to a fixpoint; completions cascade
            changed = False
            for root in self.store.started_root_plans():
                changed |= self._advance(root["id"], root["executor"])
            if not changed:
                break

    def _advance(self, plan_id: int, executor: str) -> bool:
        changed = False
        nodes = {n["node_id"]: n for n in self.store.nodes(plan_id)}
        preds: dict[int, list[int]] = {nid: [] for nid in nodes}
        for a, b in self.store.edges(plan_id):
            if b in preds:
                preds[b].append(a)

        for nid in sorted(nodes):
            n = nodes[nid]
            sub = self.store.subplan_of(plan_id, nid)
            if n["status"] == "pending" and all(nodes[p]["status"] == "done" for p in preds[nid]):
                if sub is not None:
                    self._set_status(plan_id, nid, "running")
                    self._emit(plan_id, nid, "system", {"text": f"executing subplan {sub}"})
                else:
                    self._set_status(plan_id, nid, "ready")
                changed = True
            elif n["status"] == "running" and sub is not None and (n["agent_kind"] is None or n["agent_done"]):
                changed |= self._advance(sub, executor)
                status = self.plan_status(sub)
                if status in ("done", "empty"):
                    result = self._subplan_summary(sub)
                    if n["result"]:
                        result = n["result"] + "\n\n" + result
                    self._set_status(plan_id, nid, "done", result=result)
                    changed = True
                elif status == "failed":
                    self._set_status(plan_id, nid, "failed", error=f"a node in subplan {sub} failed")
                    changed = True

        if executor == "builtin":
            for nid in sorted(nodes):
                if len(self._tasks) >= self.config.max_concurrency:
                    break
                n = self.store.get_node(plan_id, nid)
                if n["status"] != "ready" or (plan_id, nid) in self._tasks:
                    continue
                try:
                    profile, harness = self.harnesses.resolve(plan_id, nid)
                except HarnessError as e:
                    self._fail_misconfigured(plan_id, nid, str(e))
                    changed = True
                    continue
                if self._profile_full(profile):
                    continue  # stays ready; a finishing task kicks the scheduler again
                self._spawn(plan_id, nid, profile, harness)
                changed = True
        return changed

    def _profile_full(self, profile: dict[str, Any] | None) -> bool:
        """Whether the profile already runs ``max_parallel`` nodes (across all plans)."""
        if profile is None or not profile.get("max_parallel"):
            return False
        running = sum(1 for pid in self._task_profiles.values() if pid == profile["id"])
        return running >= profile["max_parallel"]

    def _fail_misconfigured(self, plan_id: int, node_id: int, message: str) -> None:
        """A profile that cannot even build its harness fails the node, no retry loop."""
        self.store.update_node(plan_id, node_id, result=None)
        self._emit(plan_id, node_id, "error", {"text": f"agent misconfigured: {message}"})
        self._set_status(plan_id, node_id, "failed", error=message, agent_kind="builtin", agent_done=1)

    def _subplan_summary(self, sub: int) -> str:
        nodes = self.store.nodes(sub)
        has_succ = {a for a, _ in self.store.edges(sub)}
        sinks = [n for n in nodes if n["node_id"] not in has_succ]
        lines = [f"Subplan {sub} completed. Final steps:"]
        for n in sinks:
            lines.append(f"- [{n['title']}] {(n['result'] or '').strip()}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ context
    def build_context(self, plan_id: int, node_id: int) -> str:
        node = self.store.get_node(plan_id, node_id)
        plan = self.store.get_plan(plan_id)
        chain = self.store.ancestors(plan_id)
        parts = []
        if chain:
            root = chain[0]
            parts.append(f"Overall goal (plan {root['plan_id']}): {root['title']}\n{root['description']}".strip())
            parts.append("This work is nested inside:")
            for c in chain:
                parts.append(f"  - plan {c['plan_id']} node {c['node_id']}: {c['node_title']} — {c['node_description']}")
            parts.append(f"Current subplan (plan {plan_id}): {plan['title']}\n{plan['description']}".strip())
        else:
            parts.append(f"Overall goal (plan {plan_id}): {plan['title']}\n{plan['description']}".strip())

        others = [n for n in self.store.nodes(plan_id) if n["node_id"] != node_id]
        if others:
            parts.append("Other nodes in this plan (for orientation only, not your job):")
            for n in others:
                parts.append(f"  - [{n['node_id']}] {n['title']} ({n['status']})")

        preds = self.store.predecessors(plan_id, node_id)
        if preds:
            parts.append("Results of the prerequisite nodes:")
            for p in preds:
                pn = self.store.get_node(plan_id, p)
                parts.append(f"--- [{p}] {pn['title']} ---\n{(pn['result'] or '(no result recorded)').strip()}")

        parts.append(f"Your node: {node['title']}\n(plan_id={plan_id}, node_id={node_id})")
        parts.append(f"Task description:\n{node['description'] or '(no further description)'}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------ builtin runs
    def _spawn(self, plan_id: int, node_id: int, profile: dict[str, Any] | None, harness: Harness) -> None:
        self.store.update_node(plan_id, node_id, agent_done=0, result=None, error=None)
        self._set_status(plan_id, node_id, "running", agent_kind="builtin",
                         agent_name=self.harnesses.agent_name(profile))
        self._emit(plan_id, node_id, "system", {"text": self.harnesses.label(profile),
                                                "agent": self.harnesses.describe(profile)})
        task = asyncio.create_task(self._run_builtin(plan_id, node_id, profile, harness))
        self._tasks[(plan_id, node_id)] = task
        self._task_profiles[(plan_id, node_id)] = profile["id"] if profile else None

    async def _run_builtin(self, plan_id: int, node_id: int, profile: dict[str, Any] | None,
                           harness: Harness) -> None:
        key = (plan_id, node_id)
        try:
            system_append = SYSTEM_APPEND
            if self.config.expose_mcp_to_agents:
                system_append += "\n\n" + EXPAND_HINT.format(plan_id=plan_id, node_id=node_id)
            job = AgentJob(
                plan_id=plan_id,
                node_id=node_id,
                prompt=self.build_context(plan_id, node_id),
                system_append=system_append,
                cwd=self.plan_workdir(plan_id),
                profile=profile,
                node_title=self.store.get_node(plan_id, node_id)["title"],
            )
            result: dict[str, Any] | None = None
            async for kind, data in harness.run(job):
                if kind == "system" and data.get("session_id"):
                    self.store.update_node(plan_id, node_id, session_id=data["session_id"])
                if kind == "result":
                    result = data
                self._emit(plan_id, node_id, kind, data)
            if result is None or result.get("is_error"):
                errs = (result or {}).get("errors") or [(result or {}).get("text") or "agent failed"]
                self._finish(plan_id, node_id, ok=False, text="; ".join(map(str, errs)))
            else:
                self._finish(plan_id, node_id, ok=True, text=result.get("text", ""))
        except asyncio.CancelledError:
            if key in self._stopping:
                self._stopping.discard(key)
            raise
        except HarnessError as e:  # setup problem surfaced by the harness: actionable, no traceback
            self._emit(plan_id, node_id, "error", {"text": f"agent misconfigured: {e}"})
            self._finish(plan_id, node_id, ok=False, text=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("agent for node %s failed", key)
            self._emit(plan_id, node_id, "error", {"text": f"{type(e).__name__}: {e}"})
            self._finish(plan_id, node_id, ok=False, text=f"{type(e).__name__}: {e}")
        finally:
            self._tasks.pop(key, None)
            self._task_profiles.pop(key, None)
            self.kick()

    def _finish(self, plan_id: int, node_id: int, ok: bool, text: str) -> None:
        if not ok:
            self.store.update_node(plan_id, node_id, agent_done=1)
            self._set_status(plan_id, node_id, "failed", error=text)
            self.kick()
            return
        self.store.update_node(plan_id, node_id, agent_done=1, result=text)
        if self.store.subplan_of(plan_id, node_id) is not None:
            # the agent expanded its node into a subplan; the scheduler finishes it later
            self._emit(plan_id, node_id, "system", {"text": "agent finished; now executing the subplan it created"})
            self.notify_plan(plan_id)
        else:
            self._set_status(plan_id, node_id, "done")
        self.kick()

    # ----------------------------------------------------------- user controls
    def start_plan(self, plan_id: int, executor: str = "builtin") -> None:
        if executor not in ("builtin", "external"):
            raise PlanError("executor must be 'builtin' or 'external'")
        plan = self.store.get_plan(plan_id)
        if plan["parent_plan_id"] is not None:
            raise PlanError(f"plan {plan_id} is a subplan; start its root plan {self.store.root_of(plan_id)['id']}")
        self.store.set_plan_started(plan_id, executor, True)
        self.notify_plan(plan_id)
        self.kick()

    async def stop_plan(self, plan_id: int) -> None:
        root = self.store.root_of(plan_id)
        self.store.set_plan_started(root["id"], root["executor"] or "builtin", False)
        pids = set(self.store.descendant_plans(root["id"]))
        for (pid, nid), task in list(self._tasks.items()):
            if pid in pids:
                await self._cancel(pid, nid, task)
                self._emit(pid, nid, "system", {"text": "plan stopped; node re-queued"})
                self._set_status(pid, nid, "pending", agent_kind=None, started_at=None)
        # ready nodes go back to pending; they become ready again on restart
        for pid in pids:
            for n in self.store.nodes(pid):
                if n["status"] == "ready":
                    self._set_status(pid, n["node_id"], "pending")
        self.notify_plan(root["id"])

    async def _cancel(self, plan_id: int, node_id: int, task: asyncio.Task) -> None:
        self._stopping.add((plan_id, node_id))
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        self._tasks.pop((plan_id, node_id), None)
        self._task_profiles.pop((plan_id, node_id), None)

    async def stop_node(self, plan_id: int, node_id: int) -> None:
        task = self._tasks.get((plan_id, node_id))
        if task is not None:
            await self._cancel(plan_id, node_id, task)
        n = self.store.get_node(plan_id, node_id)
        if n["status"] in ("running", "ready"):
            self._emit(plan_id, node_id, "system", {"text": "stopped by user"})
            self._set_status(plan_id, node_id, "failed", error="stopped by user", agent_done=1)
        self.kick()

    def retry_node(self, plan_id: int, node_id: int) -> None:
        n = self.store.get_node(plan_id, node_id)
        if n["status"] != "failed":
            raise PlanError(f"node ({plan_id}, {node_id}) is {n['status']}, only failed nodes can be retried")
        self._emit(plan_id, node_id, "system", {"text": "retry requested"})
        self._set_status(plan_id, node_id, "pending", error=None, agent_kind=None, agent_done=0,
                         started_at=None, finished_at=None)
        # containers that failed because of this node become running again
        for c in reversed(self.store.ancestors(plan_id)):
            parent = self.store.get_node(c["plan_id"], c["node_id"])
            if parent["status"] == "failed":
                self._set_status(c["plan_id"], c["node_id"], "running", error=None, finished_at=None)
        self.kick()

    async def reset_plan(self, plan_id: int) -> None:
        root = self.store.root_of(plan_id)
        await self.stop_plan(root["id"])
        for pid in self.store.descendant_plans(root["id"]):
            for n in self.store.nodes(pid):
                self.store.clear_events(pid, n["node_id"])
                self.store.update_node(pid, n["node_id"], status="pending", agent_kind=None, agent_name=None,
                                       session_id=None, agent_done=0, result=None, error=None,
                                       started_at=None, finished_at=None)
        self.notify_plan(root["id"])

    # ------------------------------------------------------- external agents
    def claim_next(self, agent_name: str, plan_id: int | None = None) -> dict[str, Any] | None:
        roots = [r for r in self.store.started_root_plans() if r["executor"] == "external"]
        if plan_id is not None:
            want = self.store.root_of(plan_id)["id"]
            roots = [r for r in roots if r["id"] == want]
        for r in roots:
            for pid in sorted(self.store.descendant_plans(r["id"])):
                for n in self.store.nodes(pid):
                    if n["status"] == "ready":
                        self.store.update_node(pid, n["node_id"], agent_done=0, result=None, error=None)
                        self._set_status(pid, n["node_id"], "running", agent_kind="external", agent_name=agent_name)
                        self._emit(pid, n["node_id"], "system", {"text": f"claimed by external agent '{agent_name}'"})
                        hint = EXPAND_HINT.format(plan_id=pid, node_id=n["node_id"])
                        return {
                            "plan_id": pid,
                            "node_id": n["node_id"],
                            "title": n["title"],
                            "description": n["description"],
                            "workdir": self.plan_workdir(pid),
                            "instructions": SYSTEM_APPEND + "\n\n" + hint
                            + "\n\nReport progress with report_progress, and finish with complete_node "
                              "(or fail_node).",
                            "context": self.build_context(pid, n["node_id"]),
                        }
        return None

    def _require_external_running(self, plan_id: int, node_id: int) -> dict[str, Any]:
        n = self.store.get_node(plan_id, node_id)
        if n["status"] != "running" or n["agent_done"]:
            raise PlanError(f"node ({plan_id}, {node_id}) is not being worked on (status {n['status']})")
        return n

    def report(self, plan_id: int, node_id: int, message: str, kind: str = "text") -> None:
        self.store.get_node(plan_id, node_id)
        if kind not in ("text", "thinking", "tool_use", "tool_result", "log"):
            kind = "text"
        self._emit(plan_id, node_id, kind, {"text": message})

    def complete_external(self, plan_id: int, node_id: int, result: str) -> None:
        self._require_external_running(plan_id, node_id)
        self._emit(plan_id, node_id, "result", {"text": result, "is_error": False})
        self._finish(plan_id, node_id, ok=True, text=result)

    def fail_external(self, plan_id: int, node_id: int, error: str) -> None:
        self._require_external_running(plan_id, node_id)
        self._emit(plan_id, node_id, "error", {"text": error})
        self._finish(plan_id, node_id, ok=False, text=error)
