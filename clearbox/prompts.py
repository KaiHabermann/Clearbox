"""What a worker agent is told: the system-prompt text (role, test level, how to run the tests,
how to expand a node) and the per-node context built from the plan.

The engine hands these to built-in runs and to external agents claiming a node; reviewers have
their own prompts in reviews.py but share ``tests_note``.
"""

from __future__ import annotations

from typing import Any

from .store import Store

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
clearbox MCP tool submit_plan with parent_plan_id={plan_id}, parent_node_id={node_id} and the
sub-nodes and edges, all in one call. Those sub-nodes are executed by other agents after you
finish, so in that case do only the groundwork yourself and describe the breakdown.
""".strip()

# Built-in workers only: their clearbox tools come from the scoped agent endpoint, not /mcp
# (see mcp_tools.build_mcp). External agents use /mcp and get EXPAND_HINT alone.
EXPAND_SCOPE_NOTE = """
Your clearbox tools reach only that subplan and the plans nested inside it: submit_plan,
create_plan, add_node, add_edge, remove_edge, remove_node, update_node and get_plan, plus
list_agents and report_progress for your own node. Every sub-node runs on your own agent; you
cannot pick another one, start or stop plans, or touch any other plan.
""".strip()


def build_context(store: Store, plan_id: int, node_id: int) -> str:
    """The prompt a worker gets for one node: overall goal (and parent chain), the other nodes,
    the results it requested, its own task, its file scope and any rework brief."""
    node = store.get_node(plan_id, node_id)
    plan = store.get_plan(plan_id)
    chain = store.ancestors(plan_id)
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

    others = [n for n in store.nodes(plan_id) if n["node_id"] != node_id]
    if others:
        parts.append("Other nodes in this plan (for orientation only, not your job):")
        for n in others:
            parts.append(f"  - [{n['node_id']}] {n['title']} ({n['status']})")

    wanted = store.node_results_from(plan_id, node_id)
    if wanted:
        parts.append("Finished results of earlier nodes, requested by this node. Build on them; "
                     "this work is done, do not redo or re-verify it:")
        for w in wanted:
            wn = store.get_node(plan_id, w)
            if wn["status"] == "done":
                body = (wn["result"] or "(no result recorded)").strip()
            else:
                body = f"(not done yet: status {wn['status']}, no result to build on)"
            parts.append(f"--- [{w}] {wn['title']} ---\n{body}")

    parts.append(f"Your node: {node['title']}\n(plan_id={plan_id}, node_id={node_id})")
    parts.append(f"Task description:\n{node['description'] or '(no further description)'}")

    files = store.node_files(plan_id, node_id)
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
