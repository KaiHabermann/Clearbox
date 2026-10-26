"""Scheduling: walks plans from roots to leaves and drives node execution.

Node lifecycle::

    pending --(all prerequisites done)--> ready --(agent attached)--> running --> done | failed

A node that has a subplan skips "ready": once its prerequisites are done it
goes straight to "running" and its subplan is executed; the node is done when
every node of the subplan is done. A task node whose agent creates a subplan
for it while running ("expanding" itself) is done once the agent has finished
*and* that subplan is complete.

With executor="builtin" the service spawns an agent session for every ready
node (up to ``max_concurrency`` at a time, round-robin across started root
plans so one plan cannot take every slot). Which agent runs a node is decided
per node by the ``HarnessRegistry`` (node profile -> plan defaults -> server
default -> the CLI-configured harness); a profile's ``max_parallel`` further
caps how many of its nodes run at once, and a root plan's own ``max_parallel``
caps how many nodes of its whole tree run at once. With executor="external",
ready nodes wait until an outside agent claims them through the MCP tools.

File scoping: a node may declare the files it creates or modifies. Two nodes
of one plan tree whose files conflict never run at the same time; this is a
runtime lock, not an extra edge, so the DAG, layering and prompts are
unchanged. A ready node is held back (it stays "ready") while a conflicting
node is running, and also while a conflicting ready node with a lower
(plan_id, node_id) is waiting for its turn, so conflicting nodes run lowest
number first -- the same order the UI staggers them in. Nodes without
declared files are never held back.

User nodes (``kind='user'``): the user's own job (push a branch, open an MR,
deploy, approve). It becomes "ready" like any node, but no scheduler ever picks
it up: it waits there (``waiting_for_user``) until ``complete_node_manually``
marks it done, and its dependants start from there as usual. It has no agent,
so no diff, review or token count is ever taken for it, and it holds no file
lock: the file conflict rules treat it as inert, so a user node parked for an
hour never blocks an agent node that declares the same files.

Rework: ``rework_node`` re-runs one finished node with a brief (the user's
instructions, else its review's findings) added to its prompt, so the agent
fixes what was raised instead of redoing the step. It is always a user action:
nothing re-runs a node because a review disliked it. Dependants are left alone.
The rework finishes through ``_finish`` like any run, so it is re-reviewed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from . import reviews
from .diffs import Snapshots
from .harness import AgentJob, Harness, HarnessError
from .harness.registry import HarnessRegistry
from .prompts import EXPAND_HINT, EXPAND_SCOPE_NOTE, build_context, tests_note, worker_system_append
from .store import PlanError, Store, validate_working_directory

log = logging.getLogger("clearbox")


@dataclass
class ConflictCache:
    """Per-view memo for ``Engine.file_conflicts``: plan -> ancestor nodes, plan -> edge closure."""

    parents: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    reach: dict[int, dict[int, set[int]]] = field(default_factory=dict)


def is_user_node(node: dict[str, Any]) -> bool:
    """Whether a node row is the user's own job (``kind='user'``; NULL means 'agent')."""
    return node.get("kind") == "user"


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
    # base url of the scoped agent MCP endpoint (".../mcp-agent"); each built-in run gets
    # "<agent_mcp_url>/<token>". None = built-in agents get no clearbox tools (--no-agent-mcp).
    agent_mcp_url: str | None = None
    server_port: int = 8765   # the port this server listens on; agents are told to keep clear of it
    # where the git snapshots behind node diffs live (diffs.py); None = <workdir>/snapshots
    snapshot_dir: str | None = None


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
        # root plan id each running builtin task belongs to, for the per-plan max_parallel cap
        self._task_roots: dict[tuple[int, int], int] = {}
        # token -> (plan_id, node_id, profile id) of the built-in run it was minted for; that
        # run's agent reaches the scoped MCP endpoint with it. Revoked when the run ends.
        self._agent_tokens: dict[str, tuple[int, int, int | None]] = {}
        # round-robin cursor into started_root_plans(): which root gets first pick of a free
        # slot next tick, so the oldest plan does not always take every slot (see _spawn_round_robin)
        self._rr_index = 0
        # git snapshots of each run's declared files and whole working tree, taken when it is
        # spawned or claimed; diffed by _record_diffs when the node finishes (see diffs.py)
        self._snapshots = Snapshots(self.config.snapshot_dir or os.path.join(self.config.workdir, "snapshots"))
        # runs whose end snapshot is being taken (off the event loop): a second complete/fail of
        # the same external claim must not slip in while the first is still finishing
        self._finishing: set[tuple[int, int]] = set()
        # review tasks run alongside node tasks but are tracked separately so a node's
        # (plan_id, node_id) task-dict slot is never mistaken for "node still running";
        # queued review requests wait here until a slot of the shared max_concurrency is free.
        self._review_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._review_queue: list[tuple[int, int]] = []
        # the whole-plan review runs once per root plan completion; tracked by root plan_id
        # alone (there is at most one in flight or queued per root), same lifecycle as the
        # per-node review tasks/queue above but kept separate since it has no node_id.
        self._plan_review_tasks: dict[int, asyncio.Task] = {}
        self._plan_review_queue: list[int] = []

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
        for t in list(self._review_tasks.values()):
            t.cancel()
        for t in list(self._plan_review_tasks.values()):
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
        if kind == "usage":
            self._add_usage(plan_id, node_id, data)
            return  # a running total, not something to show in the transcript
        ev = self.store.add_event(plan_id, node_id, kind, data)
        self.bus.publish({"type": "event", "event": ev})

    def _add_usage(self, plan_id: int, node_id: int, data: dict[str, Any]) -> None:
        """Add one turn's tokens to the node's running total and push it to the browser.

        Sent as its own small message rather than a plan refresh: this arrives once
        per model turn, and the card only needs two numbers.
        """
        n = self.store.get_node(plan_id, node_id)
        tokens_in = (n["tokens_in"] or 0) + int(data.get("tokens_in") or 0)
        tokens_out = (n["tokens_out"] or 0) + int(data.get("tokens_out") or 0)
        self.store.update_node(plan_id, node_id, tokens_in=tokens_in, tokens_out=tokens_out)
        self.bus.publish({"type": "tokens", "plan_id": plan_id, "node_id": node_id,
                          "tokens_in": tokens_in, "tokens_out": tokens_out})

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

    def set_plan_workdir(self, plan_id: int, directory: str | None) -> None:
        """Change a top-level plan's working directory; only while it has not been started.

        None/empty resets it to the default ``<workdir>/plan-<id>``.
        """
        root = self.store.root_of(plan_id)
        if root["id"] != plan_id:
            raise PlanError("working_directory can only be set on top-level plans")
        if root["started"]:
            raise PlanError("cannot change the working directory of a started plan")
        if directory:
            validate_working_directory(directory)
        self.store.update_plan(plan_id, cwd=directory or None)

    def plan_test_instructions(self, plan_id: int) -> str | None:
        """How to run the project's tests, as the plan's top-level plan states it (or None)."""
        return self.store.root_of(plan_id)["test_instructions"]

    def tests_note(self, plan_id: int) -> str:
        """The how-to-test text a plan's workers and reviewers get (see ``tests_note``)."""
        return tests_note(self.plan_test_instructions(plan_id), self.config.server_port)

    def waiting_for_user(self, node: dict[str, Any]) -> bool:
        """Whether a node row is a user node parked for the user: ready, and no agent will
        take it. The API/UI show it as "waiting for you" rather than "ready to run"."""
        return is_user_node(node) and node["status"] == "ready"

    def rollup(self, plan_id: int) -> dict[str, int]:
        """Status counts over all task (leaf) nodes of a plan, recursively.

        A user node waiting for the user is counted under ``ready`` (its real status, so the
        plan stays unfinished for ``_dir_clash`` and the plan review) and once more under
        ``waiting``, so a plan parked on one is neither "finished" nor "an agent is running".
        """
        counts = {"total": 0, "pending": 0, "ready": 0, "running": 0, "done": 0, "failed": 0,
                  "waiting": 0}
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
                if self.waiting_for_user(n):
                    counts["waiting"] += 1
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
            changed |= self._spawn_round_robin()
            changed |= self._spawn_reviews()
            for root in self.store.started_root_plans():
                changed |= self._maybe_queue_plan_review(root["id"])
            changed |= self._spawn_plan_reviews()
            if not changed:
                break

    def _advance(self, plan_id: int, executor: str) -> bool:
        """Advance pending/subplan-container status for one plan tree (recursing into subplans).

        Does not spawn builtin runs: that is a separate, cross-plan step (``_spawn_round_robin``)
        so that every started root plan gets a fair turn at free slots instead of whichever one
        ``started_root_plans`` happens to list first.
        """
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
                    if is_user_node(n):
                        self._emit(plan_id, nid, "system",
                                   {"text": "waiting for you: this is your job, mark it done when it is"})
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
        return changed

    def _spawn_round_robin(self) -> bool:
        """Give every started builtin root plan a turn at the free slots, not oldest-first.

        One attempt per root per pass, cycling root plans starting where the previous tick
        left off (``_rr_index``), until the global cap is full or a whole pass spawns nothing.
        """
        roots = [r for r in self.store.started_root_plans() if r["executor"] == "builtin"]
        if not roots:
            return False
        n = len(roots)
        start = self._rr_index % n
        order = roots[start:] + roots[:start]
        changed = False
        stalled = 0
        i = 0
        while len(self._tasks) < self.config.max_concurrency and stalled < len(order):
            root = order[i % n]
            i += 1
            if self._spawn_one(root["id"]):
                changed = True
                stalled = 0
            else:
                stalled += 1
        self._rr_index = (start + i) % n
        return changed

    def _spawn_one(self, root_id: int) -> bool:
        """Start (or fail) the lowest-numbered spawnable ready node of one plan tree.

        Returns True if it made progress (a node started or was failed as misconfigured), so
        the caller's round-robin loop knows this root is not stalled.
        """
        if self._plan_full(root_id):
            return False  # stays ready; a finishing task of this plan kicks the scheduler again
        for pid in sorted(self.store.descendant_plans(root_id)):
            # A failed container's subplan is no longer advanced, so its leftover ready nodes
            # must not start either -- `_files_turn_pending` already waves higher conflicting
            # nodes past them, so starting one would put both on the same file at once.
            if not self._still_scheduled(pid):
                continue
            for nid in sorted(n["node_id"] for n in self.store.nodes(pid)):
                n = self.store.get_node(pid, nid)
                if n["status"] != "ready" or (pid, nid) in self._tasks:
                    continue
                if is_user_node(n):
                    continue  # the user's job: stays ready until complete_node_manually
                try:
                    profile, harness = self.harnesses.resolve(pid, nid)
                except HarnessError as e:
                    self._fail_misconfigured(pid, nid, str(e))
                    return True
                if self._profile_full(profile):
                    continue  # stays ready; a finishing task kicks the scheduler again
                if self._files_held_back(pid, nid):
                    continue  # stays ready; the releasing node kicks the scheduler
                self._spawn(pid, nid, profile, harness)
                return True
        return False

    def _profile_full(self, profile: dict[str, Any] | None) -> bool:
        """Whether the profile already runs ``max_parallel`` nodes (across all plans)."""
        if profile is None or not profile.get("max_parallel"):
            return False
        running = sum(1 for pid in self._task_profiles.values() if pid == profile["id"])
        return running >= profile["max_parallel"]

    def _plan_full(self, root_id: int) -> bool:
        """Whether a top-level plan's own ``max_parallel`` already runs that many nodes,
        counting every node of its whole tree (its own nodes and every subplan's)."""
        mp = self.store.get_plan(root_id)["max_parallel"]
        if not mp:
            return False
        running = sum(1 for rid in self._task_roots.values() if rid == root_id)
        return running >= mp

    def _files_busy(self, plan_id: int, node_id: int) -> tuple[int, int] | None:
        """The running node of the same plan tree holding a file that conflicts with this node's.

        Derived from node status in the DB on every call instead of an in-memory
        lock table: a node holds its files exactly while status == 'running' and
        its agent is not done (built-in or external alike; a node whose agent
        finished but whose subplan is still executing holds nothing). ``_spawn``
        and ``claim_next`` set 'running' synchronously, so two nodes started in
        the same tick see each other, and a node that finishes, fails, is
        stopped, retried or re-queued by a restart releases its files with no
        extra bookkeeping. That automatic release is why the DB is the source of
        truth. A running node is never dependency-ordered with a ready one (all
        of a ready node's prerequisites are done), so no path check is needed.
        A user node never runs, so it never holds anything (``agent_kind`` stays
        NULL); the check below is enough, no kind test needed.
        """
        for ref in self.store.conflicting_nodes(plan_id, node_id):
            n = self.store.get_node(*ref)
            if n["status"] == "running" and n["agent_kind"] and not n["agent_done"]:
                return ref
        return None

    def _files_turn_pending(self, plan_id: int, node_id: int) -> tuple[int, int] | None:
        """A lower-numbered ready node of the same plan tree that conflicts with this one.

        Conflicting nodes run in (plan_id, node_id) order. Without this rule a
        higher node could overtake a lower one that is ready but waiting on an
        unrelated limit (``max_concurrency``, its profile's ``max_parallel``), or
        merely visited later: ``_advance`` recurses into subplans before it
        spawns, so traversal order is not rank order and the tuples are compared
        explicitly. Unlike ``_files_busy`` this waits for a turn, not for a run
        to finish. Two ready nodes are never dependency-ordered either.

        No deadlock: every wait points either at a running node (which finishes
        on its own) or at a strictly lower-ranked ready node, and files are taken
        all at once at spawn time, so there is no hold-and-wait cycle; the lowest
        ranked ready node of any wait chain waits only on running nodes or on
        limits that free up as runs finish.

        No starvation: only a ready node the scheduler will still start counts
        (``_still_scheduled``). A node left "ready" inside a subplan whose
        container failed is never spawned, and waiting for its turn would hold
        the higher node back forever. The same goes for a ready user node: it is
        waiting for the user, not for a turn, and must not block anyone.
        """
        for ref in self.store.conflicting_nodes(plan_id, node_id):
            if ref >= (plan_id, node_id):
                break  # sorted by (plan_id, node_id)
            other = self.store.get_node(*ref)
            if other["status"] == "ready" and not is_user_node(other) and self._still_scheduled(ref[0]):
                return ref
        return None

    def _still_scheduled(self, plan_id: int) -> bool:
        """Whether ``_advance`` still descends into this plan: every containing node is running
        with its own agent finished. Otherwise (a container failed) its ready nodes stay ready."""
        for a in self.store.ancestors(plan_id):
            c = self.store.get_node(a["plan_id"], a["node_id"])
            if c["status"] != "running" or (c["agent_kind"] is not None and not c["agent_done"]):
                return False
        return True

    def files_blocked_by(self, plan_id: int, node_id: int) -> tuple[tuple[int, int], str] | None:
        """Why a ready node is held back by its files: (other node, "running" | "turn"), or None.

        "running": the other node is running and holds a conflicting file
        (``_files_busy``). "turn": it is a lower-numbered conflicting node that is
        ready too and goes first (``_files_turn_pending``). The scheduler decides
        through this method and the REST node view explains a waiting node
        through it, so the two can never disagree.
        """
        ref = self._files_busy(plan_id, node_id)
        if ref is not None:
            return ref, "running"
        ref = self._files_turn_pending(plan_id, node_id)
        if ref is not None:
            return ref, "turn"
        return None

    def _files_held_back(self, plan_id: int, node_id: int) -> bool:
        """Whether a ready node must wait because of its declared files (either rule)."""
        return self.files_blocked_by(plan_id, node_id) is not None

    def file_conflicts(
        self,
        plan_id: int,
        node_id: int,
        tree: dict[tuple[int, int], list[dict[str, Any]]] | None = None,
        cache: ConflictCache | None = None,
    ) -> list[tuple[int, int]]:
        """Nodes of the plan tree that share a file with this one AND could run at the same time.

        The static view of the rule (MCP get_plan, REST node view, the UI's
        stagger): ``Store.conflicting_nodes`` minus pairs that dependencies keep
        apart anyway. The scheduler does not need that filter -- a ready node is
        never dependency-ordered with another ready or running node -- but a view
        of the whole plan does, or nodes in successive layers would look like
        they compete for a file. Pass the tree's file map and one cache when
        rendering many nodes, so a whole plan costs one file query. User nodes
        are inert here as in the scheduler: they neither have nor are conflicts.
        """
        cache = cache if cache is not None else ConflictCache()
        if is_user_node(self.store.get_node(plan_id, node_id)):
            return []
        return [
            ref for ref in self.store.conflicting_nodes(plan_id, node_id, tree=tree)
            if not self._dependency_ordered((plan_id, node_id), ref, cache)
            and not is_user_node(self.store.get_node(*ref))
        ]

    def _anchors(self, plan_id: int, node_id: int, cache: ConflictCache) -> list[tuple[int, int]]:
        """The node and the nodes whose subplans contain it, the root plan's node first."""
        if plan_id not in cache.parents:
            chain: list[tuple[int, int]] = []
            p = self.store.get_plan(plan_id)
            while p["parent_plan_id"] is not None:
                chain.append((p["parent_plan_id"], p["parent_node_id"]))
                p = self.store.get_plan(p["parent_plan_id"])
            cache.parents[plan_id] = chain[::-1]
        return [*cache.parents[plan_id], (plan_id, node_id)]

    def _reach(self, plan_id: int, cache: ConflictCache) -> dict[int, set[int]]:
        """node_id -> every node_id reachable from it along the plan's edges."""
        if plan_id not in cache.reach:
            succ: dict[int, set[int]] = {}
            for src, dst in self.store.edges(plan_id):
                succ.setdefault(src, set()).add(dst)
            closure: dict[int, set[int]] = {}
            for start in succ:
                seen: set[int] = set()
                stack = [start]
                while stack:
                    for nxt in succ.get(stack.pop(), ()):
                        if nxt not in seen:
                            seen.add(nxt)
                            stack.append(nxt)
                closure[start] = seen
            cache.reach[plan_id] = closure
        return cache.reach[plan_id]

    def _dependency_ordered(self, a: tuple[int, int], b: tuple[int, int], cache: ConflictCache) -> bool:
        """True if dependencies force a and b apart, so they can never run at the same time.

        Walk both anchor chains down to where they diverge: there both anchors are nodes of
        the same plan, and they are ordered iff one reaches the other. If one chain is a prefix
        of the other, one node lies inside the other's subplan, which only starts once that
        node's own agent has finished.
        """
        for x, y in zip(self._anchors(*a, cache), self._anchors(*b, cache)):
            if x != y:
                r = self._reach(x[0], cache)
                return y[1] in r.get(x[1], ()) or x[1] in r.get(y[1], ())
        return True

    def check_results_from(self, plan_id: int, node_id: int, ids: list[int] | None) -> None:
        """Raise PlanError unless every requested node is one this node transitively depends on.

        Requesting a result does not order anything, so only an upstream node's result is
        sure to exist by the time this node starts.
        """
        r = self._reach(plan_id, ConflictCache())
        bad = sorted({i for i in ids or () if node_id not in r.get(i, ())})
        if bad:
            raise PlanError(
                f"results_from: node ({plan_id}, {node_id}) does not depend on node(s) {bad}, so their "
                "result may not exist when it runs. Add an edge from each of them (directly or via "
                "other nodes) to this node, or request results from nodes it already depends on."
            )

    # --------------------------------------------------------------- node review
    def _review_profile(self, plan_id: int) -> dict[str, Any] | None:
        """The agent profile that reviews this plan's steps: its own, else inherited from an
        ancestor plan (same walk as ``Store.profile_for_node``'s plan-default lookup). None
        means reviewing is off."""
        p = self.store.get_plan(plan_id)
        while p["review_agent_profile_id"] is None:
            if p["parent_plan_id"] is None:
                return None
            p = self.store.get_plan(p["parent_plan_id"])
        try:
            return self.store.get_profile(p["review_agent_profile_id"])
        except PlanError:
            return None

    def _queue_review(self, plan_id: int, node_id: int) -> None:
        """Queue a reviewer run for a just-finished task node, unless there is nothing to do:
        no review agent for this plan, or neither a diff nor an undeclared change was captured."""
        profile = self._review_profile(plan_id)
        if profile is None or not (self.store.node_diffs(plan_id, node_id)
                                   or self.store.get_node(plan_id, node_id)["undeclared_files"]):
            return
        self._review_queue.append((plan_id, node_id))
        self.kick()

    def _cancel_review(self, plan_id: int, node_id: int) -> None:
        """Drop a node's queued or in-flight review and its stale row: called wherever a node
        is about to run again, so an old review is never left describing a diff that no longer
        exists (mirrors ``clear_node_diffs`` at the same call sites)."""
        key = (plan_id, node_id)
        self._review_queue[:] = [k for k in self._review_queue if k != key]
        task = self._review_tasks.pop(key, None)
        if task is not None:
            task.cancel()
        self.store.clear_review(plan_id, node_id)

    def _review_slots_free(self) -> bool:
        """Whether node runs, per-node reviews and the whole-plan review still share a free slot
        of ``max_concurrency``, so a wide plan cannot spawn dozens of reviewers at once just
        because its nodes finished in a burst."""
        return (len(self._tasks) + len(self._review_tasks) + len(self._plan_review_tasks)
                < self.config.max_concurrency)

    def _spawn_reviews(self) -> bool:
        """Start queued per-node reviews up to the free share of ``max_concurrency``."""
        changed = False
        while self._review_queue and self._review_slots_free():
            plan_id, node_id = self._review_queue.pop(0)
            profile = self._review_profile(plan_id)
            if profile is None:
                continue  # review agent was removed while queued
            self._start_review(plan_id, node_id, profile)
            changed = True
        return changed

    # -------------------------------------------------------------- plan review
    def _maybe_queue_plan_review(self, plan_id: int) -> bool:
        """Queue the whole-plan review exactly once per completion of a root plan tree.

        Guarded on the plan-level review row already existing (``store.review(plan_id, None)``),
        not on anything per-tick, so later ticks after the first are no-ops; a retry or reset
        clears that row (``_cancel_plan_review``) so the next completion fires again.
        """
        if plan_id in self._plan_review_tasks or plan_id in self._plan_review_queue:
            return False
        if self._review_profile(plan_id) is None or self.store.review(plan_id, None) is not None:
            return False
        counts = self.rollup(plan_id)
        if counts["total"] == 0 or counts["done"] != counts["total"]:
            return False
        self._plan_review_queue.append(plan_id)
        return True

    def _cancel_plan_review(self, plan_id: int) -> None:
        """Drop a root plan's queued/in-flight whole-plan review and its stale row: called
        wherever a node of its tree starts running again, so a stale review never blocks the
        next once-per-completion fire (mirrors ``_cancel_review`` for a single node)."""
        self._plan_review_queue[:] = [p for p in self._plan_review_queue if p != plan_id]
        task = self._plan_review_tasks.pop(plan_id, None)
        if task is not None:
            task.cancel()
        self.store.clear_review(plan_id, None)

    def _spawn_plan_reviews(self) -> bool:
        """Start queued whole-plan reviews up to the free share of ``max_concurrency``."""
        changed = False
        while self._plan_review_queue and self._review_slots_free():
            plan_id = self._plan_review_queue.pop(0)
            profile = self._review_profile(plan_id)
            if profile is None:
                continue  # review agent was removed while queued
            self._start_review(plan_id, None, profile)
            changed = True
        return changed

    def _start_review(self, plan_id: int, node_id: int | None, profile: dict[str, Any]) -> asyncio.Task:
        """Run ``reviews.run_review`` as a tracked task, so ``_cancel_review`` /
        ``_cancel_plan_review`` drop it when what it reviews runs again; node_id=None is the
        whole-plan review."""
        tasks, key = ((self._plan_review_tasks, plan_id) if node_id is None
                      else (self._review_tasks, (plan_id, node_id)))
        task = asyncio.create_task(reviews.run_review(self, plan_id, node_id, profile))
        tasks[key] = task

        def done(t: asyncio.Task) -> None:
            if tasks.get(key) is t:
                del tasks[key]
            self.kick()
        task.add_done_callback(done)
        return task

    async def review_now(self, plan_id: int, node_id: int | None,
                         agent: int | str | None = None) -> dict[str, Any] | None:
        """Review a node (or, node_id=None, the plan as a whole) right now and return the new
        review row: the on-demand trigger behind MCP review_node / review_plan and REST. Same
        session as an automatic review, on ``agent`` for this run only, else the plan's review
        agent (inherited like ``_review_profile``). Replaces a queued or running review of the
        same thing. Does not wait for a slot of ``max_concurrency``: someone asked for it now."""
        if node_id is None:
            self.store.get_plan(plan_id)
        else:
            self.store.get_node(plan_id, node_id)
        profile = self.store.resolve_profile_ref(agent) if agent is not None else self._review_profile(plan_id)
        if profile is None:
            raise PlanError(
                f"plan {plan_id} has no review agent set; pass agent=<name|id> to review it this once, "
                "or set_plan_review_agent to enable automatic reviewing"
            )
        if node_id is None:
            self._cancel_plan_review(plan_id)
        else:
            self._cancel_review(plan_id, node_id)
        task = self._start_review(plan_id, node_id, profile)
        await asyncio.wait({task})
        if task.cancelled():
            what = f"plan {plan_id}" if node_id is None else f"node ({plan_id}, {node_id})"
            raise PlanError(f"review of {what} was cancelled: it is running again, or the server is stopping")
        return self.store.review(plan_id, node_id)

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
        return build_context(self.store, plan_id, node_id)

    # ------------------------------------------------------------ builtin runs
    def _spawn(self, plan_id: int, node_id: int, profile: dict[str, Any] | None, harness: Harness) -> None:
        # tokens_in/out count up from the "usage" events of this run, so a retry starts at zero
        self.store.update_node(plan_id, node_id, agent_done=0, result=None, error=None,
                               tokens_in=None, tokens_out=None)
        self.store.clear_node_diffs(plan_id, node_id)
        self._cancel_review(plan_id, node_id)
        self._set_status(plan_id, node_id, "running", agent_kind="builtin",
                         agent_name=self.harnesses.agent_name(profile))
        self._emit(plan_id, node_id, "system", {"text": self.harnesses.label(profile),
                                                "agent": self.harnesses.describe(profile)})
        task = asyncio.create_task(self._run_builtin(plan_id, node_id, profile, harness))
        self._tasks[(plan_id, node_id)] = task
        self._task_profiles[(plan_id, node_id)] = profile["id"] if profile else None
        self._task_roots[(plan_id, node_id)] = self.store.root_of(plan_id)["id"]

    async def _run_builtin(self, plan_id: int, node_id: int, profile: dict[str, Any] | None,
                           harness: Harness) -> None:
        key = (plan_id, node_id)
        token = None
        try:
            await self._take_snapshot(plan_id, node_id)  # before the agent can touch anything
            system_append = worker_system_append(profile, self.tests_note(plan_id))
            if self.config.agent_mcp_url:
                token = secrets.token_urlsafe(24)
                self._agent_tokens[token] = (plan_id, node_id, profile["id"] if profile else None)
                system_append += ("\n\n" + EXPAND_HINT.format(plan_id=plan_id, node_id=node_id)
                                  + "\n" + EXPAND_SCOPE_NOTE)
            job = AgentJob(
                plan_id=plan_id,
                node_id=node_id,
                prompt=self.build_context(plan_id, node_id),
                system_append=system_append,
                cwd=self.plan_workdir(plan_id),
                profile=profile,
                node_title=self.store.get_node(plan_id, node_id)["title"],
                mcp_url=f"{self.config.agent_mcp_url}/{token}" if token else None,
            )
            result: dict[str, Any] | None = None
            # aclosing + the break below: the "result" event finishes the node, so we never wait
            # for a harness to close its own stream. The Claude SDK's iterator can stay open after
            # its final message, which used to leave the node "running" forever with its result
            # already in the transcript.
            async with aclosing(harness.run(job)) as stream:
                async for kind, data in stream:
                    if kind == "system" and data.get("session_id"):
                        self.store.update_node(plan_id, node_id, session_id=data["session_id"])
                    if kind == "result":
                        result = data
                    self._emit(plan_id, node_id, kind, data)
                    if result is not None:
                        break
            if result is not None:
                self.store.update_node(plan_id, node_id, tokens_in=result.get("tokens_in"),
                                       tokens_out=result.get("tokens_out"))
            if result is None or result.get("is_error"):
                errs = (result or {}).get("errors") or [(result or {}).get("text") or "agent failed"]
                await self._finish(plan_id, node_id, ok=False, text="; ".join(map(str, errs)))
            else:
                await self._finish(plan_id, node_id, ok=True, text=result.get("text", ""))
        except asyncio.CancelledError:
            if key in self._stopping:
                self._stopping.discard(key)
            raise
        except HarnessError as e:  # setup problem surfaced by the harness: actionable, no traceback
            self._emit(plan_id, node_id, "error", {"text": f"agent misconfigured: {e}"})
            await self._finish(plan_id, node_id, ok=False, text=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("agent for node %s failed", key)
            self._emit(plan_id, node_id, "error", {"text": f"{type(e).__name__}: {e}"})
            await self._finish(plan_id, node_id, ok=False, text=f"{type(e).__name__}: {e}")
        finally:
            self._agent_tokens.pop(token, None)
            self._tasks.pop(key, None)
            self._task_profiles.pop(key, None)
            self._task_roots.pop(key, None)
            self.kick()

    def agent_run(self, token: str) -> tuple[int, int, int | None] | None:
        """(plan_id, node_id, profile id) of the running built-in node a scoped-endpoint token
        was minted for, or None once that run has ended (or for a token never minted)."""
        return self._agent_tokens.get(token)

    def agent_token(self, plan_id: int, node_id: int) -> str | None:
        """The live scoped-endpoint token of a running built-in node, or None. For tests only
        (app.py serves it under --mock): nothing else may hand a token out."""
        return next((t for t, run in self._agent_tokens.items() if run[:2] == (plan_id, node_id)), None)

    async def _take_snapshot(self, plan_id: int, node_id: int) -> None:
        """Snapshot a run's declared files and working tree before its agent starts, the baseline
        of its diff and of its undeclared changes. Runs of one plan tree overlap in one folder.
        The git work runs in a thread; a run cancelled meanwhile never registers its snapshot."""
        key = (plan_id, node_id)
        self._snapshots.discard(key)
        run = await asyncio.to_thread(
            self._snapshots.capture, key, self.plan_workdir(plan_id),
            [f["path"] for f in self.store.node_files(plan_id, node_id)], self.store.root_of(plan_id)["id"])
        if run is not None:
            self._snapshots.register(key, run)

    async def _record_diffs(self, plan_id: int, node_id: int) -> None:
        """Store the diffs and undeclared changes of the run that just ended, if anything was
        snapshotted for it (the git work in a thread)."""
        run = self._snapshots.pop((plan_id, node_id))
        if run is None:
            return
        done = await asyncio.to_thread(self._snapshots.compare, (plan_id, node_id), run)
        if done.diffs:
            self.store.set_node_diffs(plan_id, node_id, done.diffs)
        if done.undeclared:
            self.store.set_node_undeclared(plan_id, node_id, done.undeclared, done.shared)

    async def _finish(self, plan_id: int, node_id: int, ok: bool, text: str) -> None:
        key = (plan_id, node_id)
        self._finishing.add(key)
        try:
            await self._record_diffs(plan_id, node_id)
        finally:
            self._finishing.discard(key)
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
            self._queue_review(plan_id, node_id)
        self.kick()

    # ----------------------------------------------------------- user controls
    def _dir_clash(self, plan_id: int) -> dict[str, Any] | None:
        """A started plan with unfinished work whose folder overlaps this one's.

        File-conflict locking only orders nodes inside one plan tree, so two plans
        in the same folder (or nested folders) would edit the same files with
        nothing coordinating them. Refusing to start is better than the clobbering.
        """
        mine = os.path.normcase(os.path.normpath(self.plan_workdir(plan_id)))
        for other in self.store.started_root_plans():
            if other["id"] == plan_id:
                continue
            counts = self.rollup(other["id"])
            if counts["pending"] + counts["ready"] + counts["running"] == 0:
                continue  # finished: it is not going to touch anything any more
            theirs = os.path.normcase(os.path.normpath(self.plan_workdir(other["id"])))
            if mine == theirs or mine.startswith(theirs + os.sep) or theirs.startswith(mine + os.sep):
                return other
        return None

    def start_plan(self, plan_id: int, executor: str = "builtin") -> None:
        if executor not in ("builtin", "external"):
            raise PlanError("executor must be 'builtin' or 'external'")
        plan = self.store.get_plan(plan_id)
        if plan["parent_plan_id"] is not None:
            raise PlanError(f"plan {plan_id} is a subplan; start its root plan {self.store.root_of(plan_id)['id']}")
        clash = self._dir_clash(plan_id)
        if clash is not None:
            raise PlanError(
                f"plan {clash['id']} ({clash['title']}) is still running in "
                f"{self.plan_workdir(clash['id'])}. Two plans must not work in the same folder at "
                "once: stop it, or give one of them a different working directory."
            )
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
                self._snapshots.discard((pid, nid))  # an abandoned run must not flag others shared
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
        self._task_roots.pop((plan_id, node_id), None)

    async def stop_node(self, plan_id: int, node_id: int) -> None:
        task = self._tasks.get((plan_id, node_id))
        if task is not None:
            await self._cancel(plan_id, node_id, task)
        self._snapshots.discard((plan_id, node_id))
        n = self.store.get_node(plan_id, node_id)
        if n["status"] in ("running", "ready"):
            self._emit(plan_id, node_id, "system", {"text": "stopped by user"})
            self._set_status(plan_id, node_id, "failed", error="stopped by user", agent_done=1)
        self.kick()

    def _last_result_text(self, plan_id: int, node_id: int) -> str:
        """The agent's own closing summary, if it produced one.

        A node can hold a finished transcript without having been finished (an
        agent whose stream never closed, a run cut short). When the user marks
        such a node done, its real summary is better downstream context than a
        placeholder, so prefer the last ``result`` event, then the last ``text``.
        """
        for kinds in (("result",), ("text",)):
            for ev in reversed(self.store.events(plan_id, node_id)):
                if ev["kind"] in kinds:
                    text = (ev["data"] or {}).get("text") or ""
                    if text.strip():
                        return text
        return ""

    async def complete_node_manually(self, plan_id: int, node_id: int, result: str | None = None) -> None:
        """Stop whatever is working on a node and mark it done (user's decision).

        The escape hatch for a node the service cannot finish by itself: an agent
        that hung, work the user finished by hand, a step that turned out to be
        unnecessary -- and the only way a user node (``kind='user'``) finishes.
        Dependants start as if the node had completed normally, so the recorded
        result is what they will read.
        """
        node = self.store.get_node(plan_id, node_id)
        if node["status"] == "done":
            raise PlanError(f"node ({plan_id}, {node_id}) is already done")
        task = self._tasks.get((plan_id, node_id))
        if task is not None:
            await self._cancel(plan_id, node_id, task)
        await self._record_diffs(plan_id, node_id)
        text = (result or "").strip() or self._last_result_text(plan_id, node_id)
        note = "marked done by user" + ("" if text else " (no result recorded)")
        self._emit(plan_id, node_id, "system", {"text": note})
        self.store.update_node(plan_id, node_id, agent_done=1, error=None)
        sub = self.store.subplan_of(plan_id, node_id)
        if sub is not None:
            # Leave the subplan alone: marking the container done must not silently
            # discard nodes the user may still want. _advance skips a done container.
            self._emit(plan_id, node_id, "system",
                       {"text": f"subplan {sub} is no longer waited on"})
        self._set_status(plan_id, node_id, "done", result=text or note)
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
        self._cancel_plan_review(self.store.root_of(plan_id)["id"])
        self.kick()

    def rework_node(self, plan_id: int, node_id: int, instructions: str | None = None) -> None:
        """Re-run one finished node with a brief to address (user's decision, never automatic).

        The brief is ``instructions`` if given, else the node's review summary and findings.
        It is kept on the node after the run (for display, and so a retry of a failed rework
        still carries it) and replaced by the next rework. Dependants are not reset, even if
        they built on the old output; the run's diffs, counters and review are reset by
        ``_spawn`` and it is re-reviewed through ``_finish`` like any other run.
        """
        n = self.store.get_node(plan_id, node_id)
        if n["status"] not in ("done", "failed"):
            raise PlanError(f"node ({plan_id}, {node_id}) is {n['status']}; only a finished node can be "
                            "reworked" + (" -- stop it first" if n["status"] == "running" else ""))
        if self.store.subplan_of(plan_id, node_id) is not None:
            raise PlanError(f"node ({plan_id}, {node_id}) has a subplan; rework a step inside it instead")
        brief = (instructions or "").strip()
        if not brief:
            review = self.store.review(plan_id, node_id)
            if review:
                brief = "\n\n".join(s for s in (review["summary"].strip(), (review["findings"] or "").strip()) if s)
                if brief:
                    brief = f"The reviewer's verdict was '{review['verdict']}':\n{brief}"
        if not brief:
            raise PlanError(f"node ({plan_id}, {node_id}) has no review findings to address; give "
                            "instructions, or use retry / a new plan for a plain re-run")
        self.store.update_node(plan_id, node_id, rework_brief=brief)
        self._emit(plan_id, node_id, "system", {"text": "rework requested"})
        self._set_status(plan_id, node_id, "pending", error=None, agent_kind=None, agent_done=0,
                         started_at=None, finished_at=None)
        # finished containers must run again or _advance never reaches this node
        for c in reversed(self.store.ancestors(plan_id)):
            parent = self.store.get_node(c["plan_id"], c["node_id"])
            if parent["status"] in ("done", "failed"):
                # a pure container's result is just the subplan summary; _advance rebuilds it
                extra = {"result": None} if parent["agent_kind"] is None else {}
                self._set_status(c["plan_id"], c["node_id"], "running", error=None, finished_at=None, **extra)
        self._cancel_plan_review(self.store.root_of(plan_id)["id"])
        self.kick()

    def retry_failed(self, plan_id: int) -> int:
        """Retry every failed node in the plan tree, leaving done nodes alone."""
        root = self.store.root_of(plan_id)
        count = 0
        for pid in self.store.descendant_plans(root["id"]):
            for n in self.store.nodes(pid):
                if n["status"] == "failed":
                    self.retry_node(pid, n["node_id"])
                    count += 1
        return count

    async def reset_plan(self, plan_id: int) -> None:
        root = self.store.root_of(plan_id)
        await self.stop_plan(root["id"])
        self._cancel_plan_review(root["id"])
        for pid in self.store.descendant_plans(root["id"]):
            for n in self.store.nodes(pid):
                self.store.clear_events(pid, n["node_id"])
                self.store.clear_node_diffs(pid, n["node_id"])
                self._cancel_review(pid, n["node_id"])
                self.store.update_node(pid, n["node_id"], status="pending", agent_kind=None, agent_name=None,
                                       session_id=None, agent_done=0, result=None, error=None,
                                       started_at=None, finished_at=None, rework_brief=None)
        self.notify_plan(root["id"])

    # ------------------------------------------------------- external agents
    async def claim_next(self, agent_name: str, plan_id: int | None = None) -> dict[str, Any] | None:
        roots = [r for r in self.store.started_root_plans() if r["executor"] == "external"]
        if plan_id is not None:
            want = self.store.root_of(plan_id)["id"]
            roots = [r for r in roots if r["id"] == want]
        for r in roots:
            for pid in sorted(self.store.descendant_plans(r["id"])):
                for n in self.store.nodes(pid):
                    if (n["status"] == "ready" and not is_user_node(n)
                            and not self._files_held_back(pid, n["node_id"])):
                        self.store.update_node(pid, n["node_id"], agent_done=0, result=None, error=None)
                        self.store.clear_node_diffs(pid, n["node_id"])
                        self._cancel_review(pid, n["node_id"])
                        # running first, so no other claim takes the node while the snapshot runs;
                        # it is complete before this returns, so before the agent does anything
                        self._set_status(pid, n["node_id"], "running", agent_kind="external", agent_name=agent_name)
                        self._emit(pid, n["node_id"], "system", {"text": f"claimed by external agent '{agent_name}'"})
                        await self._take_snapshot(pid, n["node_id"])
                        hint = EXPAND_HINT.format(plan_id=pid, node_id=n["node_id"])
                        return {
                            "plan_id": pid,
                            "node_id": n["node_id"],
                            "title": n["title"],
                            "description": n["description"],
                            "workdir": self.plan_workdir(pid),
                            "instructions": worker_system_append(None, self.tests_note(pid)) + "\n\n" + hint
                            + "\n\nReport progress with report_progress, and finish with complete_node "
                              "(or fail_node).",
                            "context": self.build_context(pid, n["node_id"]),
                        }
        return None

    def _require_external_running(self, plan_id: int, node_id: int) -> dict[str, Any]:
        n = self.store.get_node(plan_id, node_id)
        if n["status"] != "running" or n["agent_done"] or (plan_id, node_id) in self._finishing:
            raise PlanError(f"node ({plan_id}, {node_id}) is not being worked on (status {n['status']})")
        return n

    def report(self, plan_id: int, node_id: int, message: str, kind: str = "text") -> None:
        self.store.get_node(plan_id, node_id)
        if kind not in ("text", "thinking", "tool_use", "tool_result", "log"):
            kind = "text"
        self._emit(plan_id, node_id, kind, {"text": message})

    async def complete_external(self, plan_id: int, node_id: int, result: str) -> None:
        self._require_external_running(plan_id, node_id)
        self._emit(plan_id, node_id, "result", {"text": result, "is_error": False})
        await self._finish(plan_id, node_id, ok=True, text=result)

    async def fail_external(self, plan_id: int, node_id: int, error: str) -> None:
        self._require_external_running(plan_id, node_id)
        self._emit(plan_id, node_id, "error", {"text": error})
        await self._finish(plan_id, node_id, ok=False, text=error)
