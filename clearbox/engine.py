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
import difflib
import logging
import os
import re
import secrets
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from .harness import AgentJob, Harness, HarnessError
from .harness.registry import HarnessRegistry
from .harness.tools import IGNORED_DIRS
from .store import PlanError, Store, validate_working_directory

log = logging.getLogger("clearbox")


@dataclass
class ConflictCache:
    """Per-view memo for ``Engine.file_conflicts``: plan -> ancestor nodes, plan -> edge closure."""

    parents: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    reach: dict[int, dict[int, set[int]]] = field(default_factory=dict)

SYSTEM_APPEND = """
You are one worker inside Clearbox, a service that executes a plan (a DAG of work items)
node by node. You are responsible for exactly ONE node. Stay within its scope: other nodes are
handled by other agents, before or after you. Results of earlier nodes are given to you only
when your node asked for them.

Trust your instructions. The design was settled when the plan was written, so implement what the
node asks instead of reconsidering it. Do not re-open decisions the node already made, and do not
widen the scope. If part of the node is genuinely wrong or impossible, do the sensible thing and
say so plainly in your summary.

Do not explore the codebase. Your node names the files it concerns: open those, plus whatever
they directly force you to look at, and nothing else. A scoped change does not need a mental model
of the whole project, and building one is the most common way these sessions burn their budget.
No repository-wide searches, no reading files "for background", no re-deriving what the node
already tells you. If you catch yourself grepping around to see how everything fits together,
stop -- that understanding went into the plan, and the node is the part of it you need.

Change files with the file tools. Every edit goes through Edit / MultiEdit / Write. Do not modify
files from the shell: no output redirection, no `sed -i`, no `python - <<EOF` that rewrites a
file, no scripts that patch source. Clearbox diffs your node by snapshotting its declared files
before you start and by watching your file-tool calls, so a shell edit to a file your node did
not declare is invisible -- it never reaches the reviewer and nobody can see what you did
afterwards. An edit that is awkward to express as an edit is still an edit.

Fix what your own change makes wrong. Staying in scope means not taking on new work; it does not
mean leaving a comment, docstring or prompt string that your change just made false. If it sits
in a file your node already edits, correct it and mention it in your summary. Do not defer it to
another node on the assumption that someone else owns it -- that is how a plan ends up shipping
statements it has already contradicted.

Keep it simple. Write the smallest change that does the job and matches the surrounding code.
No speculative abstraction, no extra configuration, no defensive scaffolding for problems nobody
has reported.
""".strip()

SUMMARY_NOTE = """
When you are done, end with a concise summary of what you did and what you produced (files,
decisions, findings). That final message is handed to the nodes that depend on yours.
""".strip()

# One paragraph per agent_profiles.test_level (store.TEST_LEVELS), placed between SYSTEM_APPEND
# and SUMMARY_NOTE for worker agents. Keep the wording in step with the Agents panel's help text.
TEST_LEVEL_NOTES = {
    "none": """
Testing: none. Do not spend effort on tests; the node says what to verify, if anything does.
""".strip(),
    "light": """
Testing: light. A quick sanity check that your change works -- one short script or a couple of
cases. Do not build test suites or fixtures.
""".strip(),
    "standard": """
Testing: standard. Run the existing tests that cover what you touched, and fix what you broke.
Add a test only when you added behaviour nothing covers. Do not build new harnesses.

{tests}
""".strip(),
    "thorough": """
Testing: thorough. Run the full suite and add tests for the behaviour you added. A change is not
done while a test you can run is failing.

{tests}
""".strip(),
}
DEFAULT_TEST_LEVEL = "standard"


# How to run the tests, shared by the worker test levels and the reviewers. The plan's own
# test_instructions go in verbatim; without them the agent finds out from the repository.
TESTS_GIVEN = """
How this project's tests are run, as the plan states it:

{test_instructions}
""".strip()

TESTS_UNKNOWN = """
Work out how this project's tests are run from the repository itself: its README, CLAUDE.md /
AGENTS.md, CI configuration and the test files. Use what you find there; do not invent a test
harness of your own.
""".strip()

SERVER_GUARD = """
If you start a server to test against, pick a free high port and temp paths. Never use port
{server_port} -- that is the Clearbox server running this plan -- or the project's real data, and
stop the server when you are done.
""".strip()


def tests_note(test_instructions: str | None, server_port: int) -> str:
    """The how-to-test paragraph(s) for one plan: its ``test_instructions`` (or the pointer to
    the repository when it has none), then the guard keeping agents off ``server_port``."""
    if test_instructions:
        howto = TESTS_GIVEN.format(test_instructions=test_instructions.strip())
    else:
        howto = TESTS_UNKNOWN
    return howto + "\n\n" + SERVER_GUARD.format(server_port=server_port)


def worker_system_append(profile: dict[str, Any] | None, tests: str) -> str:
    """The worker prompt: SYSTEM_APPEND, the test-level paragraph for ``profile`` (NULL/absent =
    standard), then SUMMARY_NOTE. ``tests`` is the plan's tests_note, which the standard and
    thorough levels include."""
    level = (profile or {}).get("test_level") or DEFAULT_TEST_LEVEL
    note = TEST_LEVEL_NOTES.get(level, TEST_LEVEL_NOTES[DEFAULT_TEST_LEVEL]).format(tests=tests)
    return "\n\n".join((SYSTEM_APPEND, note, SUMMARY_NOTE))


EXPAND_HINT = """
If your node turns out to be too large for one session, you may break it down: call the
clearbox MCP tool create_plan with parent_plan_id={plan_id} and parent_node_id={node_id}, then
add_node / add_edge on the returned plan id. Those sub-nodes are executed by other agents after
you finish, so in that case do only the groundwork yourself and describe the breakdown.
""".strip()

# Built-in workers only: their clearbox tools come from the scoped agent endpoint, not /mcp
# (see mcp_tools.build_mcp). External agents use /mcp and get EXPAND_HINT alone.
EXPAND_SCOPE_NOTE = """
Your clearbox tools reach only that subplan and the plans nested inside it: create_plan, add_node,
add_edge, remove_edge, remove_node, update_node, set_node_kind, set_node_files,
set_node_results_from and get_plan, plus list_agents and report_progress for your own node. Every
sub-node runs on your own agent; you cannot pick another one, start or stop plans, or touch any
other plan.
""".strip()

RUN_TESTS_NOTE = """
Run the test suite before you give a verdict. It is the part of a review that finds what reading
cannot: a plan that changes behaviour without updating the tests asserting the old behaviour is
exactly the failure this catches.

{tests}

Report every failure as a finding, with the assertion text. A failing test makes the verdict
`concerns` at best, even when the change itself looks right. Do NOT fix the test or the code --
you are reviewing, and someone else decides what to change. If the suite cannot be run at all,
say so in your summary instead of quietly skipping it.
""".strip()

VERDICT_FORMAT = """
End your final message with a line of exactly the form:
VERDICT: ok | concerns | error
then a short summary paragraph, a blank line, and concrete findings as a bullet list (or none).
""".strip()

REVIEW_SYSTEM_APPEND = """
You are reviewing one already-finished step of a larger plan; you did not do the step and you
must not touch it. Judge only whether the diff below does what the node was asked to do --
correctness, obvious bugs, leftovers. Diff coverage depends on the path. For the node's
declared file scope (listed in the prompt when it has one) the diff is complete: every change to
those paths is shown however it was made, so a change missing there really was not made. Outside
that scope the diff is best-effort, file-tool writes only: anything written via bash or another
shell-out is not shown, so the absence of a change there is not evidence of anything. Other nodes handle other work; do not comment on scope beyond this node. You have
read-only tools and a shell for extra context and for running the tests -- do not modify anything.

Other nodes of this plan may still be working in the same tree while you review, so a test
failure is not automatically this node's fault. Say which failures you can tie to this change
and which you cannot.
""".strip() + "\n\n" + RUN_TESTS_NOTE + "\n\n" + VERDICT_FORMAT

PLAN_REVIEW_SYSTEM_APPEND = """
You are reviewing a finished plan as a whole, not any one step, and the plan is already
complete. You are not shown diffs or source code and should not need to open any file -- this is
an intent-level review, not a code review. You see the plan's own title and description (what it
set out to do) and, for every node of its tree, that node's title, description, result summary
and step-review verdict (if it has one).

Ask: what did the plan set out to do, and is anything missing, duplicated, or contradicted? Did
one step report something that undermines another step's assumption? Was anything the plan
description called for never actually done, even if every individual step reports success? Do
not re-judge individual steps' code or re-open scope decisions already settled by the plan; you
have read-only tools and a shell for extra context and for running the tests, and must not
modify anything.

The suite is the one place this review does touch the code, and it is where whole-plan damage
shows up: a plan can change behaviour step by step, with every step correct on its own, and
leave a test asserting the behaviour it just replaced. Nobody sees that from the results alone.
""".strip() + "\n\n" + RUN_TESTS_NOTE + "\n\n" + VERDICT_FORMAT

_VERDICT_RE = re.compile(r"VERDICT:\s*(ok|concerns|error)", re.IGNORECASE)


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
        # (plan_id, node_id) -> {normalised full path: (display path, text-before-or-None)},
        # taken for the declared files at _spawn and from tool_use events while the node runs;
        # diffed against the current text when it finishes. _snapshot_dirs holds the declared
        # directories (full path -> display path) rescanned at finish for added files.
        # In memory only, like _tasks: a restart loses in-flight diffs the same way it re-queues
        # the node.
        self._snapshots: dict[tuple[int, int], dict[str, tuple[str, str | None]]] = {}
        self._snapshot_dirs: dict[tuple[int, int], dict[str, str]] = {}
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
        if kind == "tool_use":
            self._snapshot_file_tool(plan_id, node_id, data)
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

    # -------------------------------------------------------------- node diffs
    # Two sources, unioned: the node's declared files are snapshotted at _spawn, before the agent
    # does anything, so any change to them shows up however it was made (file tool, bash, sed);
    # file-tool writes (Write, Edit, MultiEdit, NotebookEdit) are snapshotted as they happen, so
    # nodes without declared files, and writes outside the declared scope, still get diffs. A
    # shell-out write outside the declared scope stays invisible -- catching it would mean
    # snapshotting the whole tree before every node.
    FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
    # one declared directory snapshots at most this many files; past it the directory is only
    # diffed for the files already taken, and not rescanned for added ones (they would all look new)
    MAX_DIR_SNAPSHOT_FILES = 2000

    def _full_path(self, plan_id: int, path: str) -> str:
        full = path if os.path.isabs(path) else os.path.join(self.plan_workdir(plan_id), path)
        return os.path.normcase(os.path.abspath(full))

    def _snapshot(self, plan_id: int, node_id: int, path: str) -> None:
        """Take a file's before-text, unless this run already has it (the first snapshot wins)."""
        snaps = self._snapshots.setdefault((plan_id, node_id), {})
        full = self._full_path(plan_id, path)
        if full not in snaps:
            snaps[full] = (path, self._read_text(plan_id, path))

    def _walk_dir(self, full_dir: str, display: str) -> tuple[list[str], bool]:
        """Display paths of the files below a directory (skipping IGNORED_DIRS), and whether
        the walk hit MAX_DIR_SNAPSHOT_FILES."""
        out: list[str] = []
        for d, dirs, names in os.walk(full_dir):
            dirs[:] = sorted(x for x in dirs if x not in IGNORED_DIRS)
            rel = os.path.relpath(d, full_dir)
            for name in sorted(names):
                if len(out) >= self.MAX_DIR_SNAPSHOT_FILES:
                    return out, True
                sub = name if rel == "." else os.path.join(rel, name)
                out.append(os.path.join(display, sub).replace("\\", "/"))
        return out, False

    def _snapshot_declared(self, plan_id: int, node_id: int) -> None:
        """Baseline every declared path before the run starts. A missing file snapshots as None
        (a creation); a directory snapshots each file below it and is rescanned at finish."""
        key = (plan_id, node_id)
        self._snapshots[key] = {}
        self._snapshot_dirs[key] = {}
        for f in self.store.node_files(plan_id, node_id):
            path = f["path"]
            full = self._full_path(plan_id, path)
            if not os.path.isdir(full):
                self._snapshot(plan_id, node_id, path)
                continue
            files, capped = self._walk_dir(full, path.rstrip("/\\"))
            for p in files:
                self._snapshot(plan_id, node_id, p)
            if capped:
                log.warning("node %s: declared directory %s has over %d files; added files there "
                            "will not be diffed", key, path, self.MAX_DIR_SNAPSHOT_FILES)
            else:
                self._snapshot_dirs[key][full] = path.rstrip("/\\")

    def _snapshot_file_tool(self, plan_id: int, node_id: int, data: dict[str, Any]) -> None:
        if data.get("name") not in self.FILE_TOOLS:
            return
        inp = data.get("input")
        path = inp.get("file_path") if isinstance(inp, dict) else None
        if path:
            self._snapshot(plan_id, node_id, path)

    def _read_text(self, plan_id: int, path: str) -> str | None:
        """The current text of a file the node may have written, or None if it can't be read as text."""
        full = path if os.path.isabs(path) else os.path.join(self.plan_workdir(plan_id), path)
        try:
            with open(full, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None

    def _record_diffs(self, plan_id: int, node_id: int) -> None:
        """Diff every snapshotted file against its current text and store the changed ones:
        modified, created (before None), deleted (gone now), and files added inside a declared
        directory."""
        key = (plan_id, node_id)
        snaps = self._snapshots.pop(key, None) or {}
        for full_dir, display in self._snapshot_dirs.pop(key, {}).items():
            for p in self._walk_dir(full_dir, display)[0]:
                full = self._full_path(plan_id, p)
                if full not in snaps:
                    snaps[full] = (p, None)
        diffs = []
        for full, (path, before) in snaps.items():
            after = self._read_text(plan_id, path)
            if after is None:
                if before is None or os.path.exists(full):
                    continue  # never text, or no longer readable as text: nothing to show
                after = ""  # existed, now gone: a deletion
            if after == before:
                continue
            diff = "".join(difflib.unified_diff(
                (before or "").splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=path, tofile=path,
            ))
            if diff:
                diffs.append((path, diff))
        if diffs:
            self.store.set_node_diffs(plan_id, node_id, diffs)

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
        no review agent for this plan, or no diff was captured for this node."""
        profile = self._review_profile(plan_id)
        if profile is None or not self.store.node_diffs(plan_id, node_id):
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
            task = asyncio.create_task(self._run_review(plan_id, node_id, profile))
            self._review_tasks[(plan_id, node_id)] = task
            changed = True
        return changed

    def _review_prompt(self, plan_id: int, node_id: int) -> str:
        node = self.store.get_node(plan_id, node_id)
        parts = [
            f"Node: {node['title']}",
            f"Task description:\n{node['description'] or '(no further description)'}",
            f"Result summary the node reported:\n{(node['result'] or '').strip() or '(none)'}",
        ]
        files = self.store.node_files(plan_id, node_id)
        if files:
            parts.append(
                "Declared file scope (diff coverage is complete for these paths; a directory "
                "covers everything below it):\n" + "\n".join(f"  - {f['path']}" for f in files)
            )
        parts.append(
            "Unified diffs captured for this node (complete for the declared scope; elsewhere "
            "file-tool writes only, and files written there via bash are not shown):"
            if files else
            "Unified diffs captured for this node (file-tool writes only; a node may have "
            "written other files via bash that are not shown here):"
        )
        for d in self.store.node_diffs(plan_id, node_id):
            parts.append(f"--- {d['path']} ---\n{d['diff']}")
        return "\n\n".join(parts)

    def _parse_review(self, text: str) -> tuple[str, str, str | None]:
        """A reviewer's final message -> (verdict, summary, findings or None)."""
        m = _VERDICT_RE.search(text)
        verdict = m.group(1).lower() if m else "concerns"
        rest = (text[:m.start()] + text[m.end():] if m else text).strip()
        summary, _, findings = rest.partition("\n\n")
        return verdict, summary.strip(), (findings.strip() or None)

    async def _run_review(self, plan_id: int, node_id: int, profile: dict[str, Any]) -> None:
        key = (plan_id, node_id)
        try:
            harness = self.harnesses.review_harness_for(profile)
            job = AgentJob(
                plan_id=plan_id,
                node_id=node_id,
                prompt=self._review_prompt(plan_id, node_id),
                system_append=REVIEW_SYSTEM_APPEND.format(tests=self.tests_note(plan_id)),
                cwd=self.plan_workdir(plan_id),
                profile=profile,
                node_title=self.store.get_node(plan_id, node_id)["title"],
            )
            self._emit(plan_id, node_id, "system",
                       {"text": f"review: {self.harnesses.label(profile)}", "review": True})
            result: dict[str, Any] | None = None
            async with aclosing(harness.run(job)) as stream:
                async for kind, data in stream:
                    if kind == "usage":
                        continue  # a review's tokens are the node's own cost, counted below only
                    if kind == "result":
                        result = data
                    self._emit(plan_id, node_id, kind, {**data, "review": True})
                    if result is not None:
                        break
            if result is None or result.get("is_error"):
                errs = (result or {}).get("errors") or [(result or {}).get("text") or "review failed"]
                self.store.set_review(plan_id, node_id, "error", summary="; ".join(map(str, errs)),
                                      agent_name=self.harnesses.agent_name(profile))
            else:
                verdict, summary, findings = self._parse_review(result.get("text", ""))
                self.store.set_review(plan_id, node_id, verdict, summary=summary, findings=findings,
                                      agent_name=self.harnesses.agent_name(profile),
                                      tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- a review must never fail the node it reviews
            log.exception("review for node %s failed", key)
            self.store.set_review(plan_id, node_id, "error", summary=f"{type(e).__name__}: {e}")
        finally:
            self._review_tasks.pop(key, None)
            self.notify_plan(plan_id)
            self.kick()

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

    def request_plan_review(self, plan_id: int) -> None:
        """Queue the whole-plan review on demand, bypassing the once-per-completion guard: the
        REST/MCP trigger calls this so a finished plan can be re-reviewed after fixes without
        re-running any node. ``plan_id`` may be any node of the tree; the review is always keyed
        to its root."""
        root_id = self.store.root_of(plan_id)["id"]
        self._cancel_plan_review(root_id)
        self._plan_review_queue.append(root_id)
        self.kick()

    def _spawn_plan_reviews(self) -> bool:
        """Start queued whole-plan reviews up to the free share of ``max_concurrency``."""
        changed = False
        while self._plan_review_queue and self._review_slots_free():
            plan_id = self._plan_review_queue.pop(0)
            profile = self._review_profile(plan_id)
            if profile is None:
                continue  # review agent was removed while queued
            task = asyncio.create_task(self._run_plan_review(plan_id, profile))
            self._plan_review_tasks[plan_id] = task
            changed = True
        return changed

    def _plan_review_prompt(self, plan_id: int) -> str:
        plan = self.store.get_plan(plan_id)
        parts = [
            f"Plan: {plan['title']}",
            f"Description:\n{plan['description'] or '(no further description)'}",
            "Every node of the plan tree (including nested subplans), each with its own "
            "description, result summary and step-review verdict if it has one:",
        ]
        for pid in sorted(self.store.descendant_plans(plan_id)):
            for n in self.store.nodes(pid):
                review = self.store.review(pid, n["node_id"])
                verdict = review["verdict"] if review else "(not reviewed)"
                parts.append(
                    f"--- [{pid}:{n['node_id']}] {n['title']} ---\n"
                    f"Description: {n['description'] or '(none)'}\n"
                    f"Result: {(n['result'] or '').strip() or '(none)'}\n"
                    f"Step review verdict: {verdict}"
                )
        return "\n\n".join(parts)

    async def _run_plan_review(self, plan_id: int, profile: dict[str, Any]) -> None:
        # Plan-level events have no real node: 0 is never a valid node_id (they start at 1),
        # so it is a safe sentinel for streaming this session like a node's own.
        try:
            harness = self.harnesses.review_harness_for(profile)
            job = AgentJob(
                plan_id=plan_id,
                node_id=0,
                prompt=self._plan_review_prompt(plan_id),
                system_append=PLAN_REVIEW_SYSTEM_APPEND.format(tests=self.tests_note(plan_id)),
                cwd=self.plan_workdir(plan_id),
                profile=profile,
                node_title=self.store.get_plan(plan_id)["title"],
            )
            self._emit(plan_id, 0, "system",
                       {"text": f"plan review: {self.harnesses.label(profile)}", "review": True})
            result: dict[str, Any] | None = None
            async with aclosing(harness.run(job)) as stream:
                async for kind, data in stream:
                    if kind == "usage":
                        continue
                    if kind == "result":
                        result = data
                    self._emit(plan_id, 0, kind, {**data, "review": True})
                    if result is not None:
                        break
            if result is None or result.get("is_error"):
                errs = (result or {}).get("errors") or [(result or {}).get("text") or "review failed"]
                self.store.set_review(plan_id, None, "error", summary="; ".join(map(str, errs)),
                                      agent_name=self.harnesses.agent_name(profile))
            else:
                verdict, summary, findings = self._parse_review(result.get("text", ""))
                self.store.set_review(plan_id, None, verdict, summary=summary, findings=findings,
                                      agent_name=self.harnesses.agent_name(profile),
                                      tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- a review must never fail the plan it reviews
            log.exception("plan review for plan %s failed", plan_id)
            self.store.set_review(plan_id, None, "error", summary=f"{type(e).__name__}: {e}")
        finally:
            self._plan_review_tasks.pop(plan_id, None)
            self.notify_plan(plan_id)
            self.kick()

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

        wanted = self.store.node_results_from(plan_id, node_id)
        if wanted:
            parts.append("Finished results of earlier nodes, requested by this node. Build on them; "
                         "this work is done, do not redo or re-verify it:")
            for w in wanted:
                wn = self.store.get_node(plan_id, w)
                if wn["status"] == "done":
                    body = (wn["result"] or "(no result recorded)").strip()
                else:
                    body = f"(not done yet: status {wn['status']}, no result to build on)"
                parts.append(f"--- [{w}] {wn['title']} ---\n{body}")

        parts.append(f"Your node: {node['title']}\n(plan_id={plan_id}, node_id={node_id})")
        parts.append(f"Task description:\n{node['description'] or '(no further description)'}")

        files = self.store.node_files(plan_id, node_id)
        if files:
            parts.append(
                "Your file scope. You may create or modify ONLY these paths (relative to the working "
                "directory; a directory covers everything below it):\n"
                + "\n".join(f"  - {f['path']}" for f in files)
                + "\nYou may read any file. Every other file is owned by other nodes; do not write it."
            )

        if node["rework_brief"]:
            parts.append(
                "REWORK. This node already ran and its work is in the working tree. Do not redo the "
                "step from scratch. Address exactly what is raised below, change only what that "
                "needs, and summarise what you changed:\n" + node["rework_brief"]
            )
        return "\n\n".join(parts)

    # ------------------------------------------------------------ builtin runs
    def _spawn(self, plan_id: int, node_id: int, profile: dict[str, Any] | None, harness: Harness) -> None:
        # tokens_in/out count up from the "usage" events of this run, so a retry starts at zero
        self.store.update_node(plan_id, node_id, agent_done=0, result=None, error=None,
                               tokens_in=None, tokens_out=None)
        self.store.clear_node_diffs(plan_id, node_id)
        self._cancel_review(plan_id, node_id)
        self._snapshot_declared(plan_id, node_id)
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
            self._agent_tokens.pop(token, None)
            self._tasks.pop(key, None)
            self._task_profiles.pop(key, None)
            self._task_roots.pop(key, None)
            self.kick()

    def agent_run(self, token: str) -> tuple[int, int, int | None] | None:
        """(plan_id, node_id, profile id) of the running built-in node a scoped-endpoint token
        was minted for, or None once that run has ended (or for a token never minted)."""
        return self._agent_tokens.get(token)

    def _finish(self, plan_id: int, node_id: int, ok: bool, text: str) -> None:
        self._record_diffs(plan_id, node_id)
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
        self._snapshots.pop((plan_id, node_id), None)
        self._snapshot_dirs.pop((plan_id, node_id), None)
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
        self._record_diffs(plan_id, node_id)
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
    def claim_next(self, agent_name: str, plan_id: int | None = None) -> dict[str, Any] | None:
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
                        self._set_status(pid, n["node_id"], "running", agent_kind="external", agent_name=agent_name)
                        self._emit(pid, n["node_id"], "system", {"text": f"claimed by external agent '{agent_name}'"})
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
