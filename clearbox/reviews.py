"""Step and plan reviews: the reviewer prompts, the reply format and its parser, and the one
session runner every review goes through.

Two triggers share it: the engine's automatic reviews (a node finishes, a plan tree completes)
and the on-demand ``review_node`` / ``review_plan`` (MCP and REST, via ``Engine.review_now``).
Either way the reviewer gets the same prompt, runs on ``HarnessRegistry.review_harness_for``
(capability ``run``: read, search and run the suite, no edits, no clearbox tools), streams into
the node's transcript and leaves one row in ``reviews``. Reviews are advisory: nothing here
fails a node or blocks a dependant.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

from .harness import AgentJob
from .store import Store, public_profile

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("clearbox")

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

REVIEW_FORMAT = """
End your final message with exactly this shape, and nothing after it:

VERDICT: ok | concerns | error
SUMMARY: <one or two sentences>
FINDINGS:
<a bullet list of concrete issues, or "none" if you found none>

Use "error" only when you could not form a verdict at all (nothing to review, unreadable input) --
never as a severity level; a real problem you did find is "concerns".
""".strip()

NODE_REVIEW_SYSTEM_APPEND = """
You are reviewing one already-finished step of a larger plan; you did not do the step and you
must not touch it. Judge only whether the diff below does what the node was asked to do --
correctness, obvious bugs, leftovers. The diff covers exactly the node's declared file scope
(listed in the prompt): every change to those paths is shown however it was made, so a change
missing there really was not made. Files outside that scope are not diffed: the prompt names any
that changed during the run (gitignored ones excepted), but their content is not shown. Other
nodes handle other work; do not comment on scope beyond this node. You have read-only tools and a shell for extra context and for running
the tests -- do not modify anything.

Other nodes of this plan may still be working in the same tree while you review, so a test
failure is not automatically this node's fault. Say which failures you can tie to this change
and which you cannot.
""".strip() + "\n\n" + RUN_TESTS_NOTE + "\n\n" + REVIEW_FORMAT

PLAN_REVIEW_SYSTEM_APPEND = """
You are reviewing a finished plan as a whole, not any one step, and the plan is already
complete. You are not shown diffs or source code and should not need to open any file -- this is
an intent-level review, not a code review. You see the plan's own title and description (what it
set out to do) and, for every node of its tree, that node's title, status, description, result
summary and step review (if it has one).

Ask: what did the plan set out to do, and is anything missing, duplicated, or contradicted? Did
one step report something that undermines another step's assumption? Was anything the plan
description called for never actually done, even if every individual step reports success? Do
not re-judge individual steps' code or re-open scope decisions already settled by the plan; you
have read-only tools and a shell for extra context and for running the tests, and must not
modify anything.

The suite is the one place this review does touch the code, and it is where whole-plan damage
shows up: a plan can change behaviour step by step, with every step correct on its own, and
leave a test asserting the behaviour it just replaced. Nobody sees that from the results alone.
""".strip() + "\n\n" + RUN_TESTS_NOTE + "\n\n" + REVIEW_FORMAT

# Labels may come wrapped in markdown (**VERDICT:**, ## Findings:), so leading/trailing * _ # > are skipped.
_VERDICT_RE = re.compile(r"(?im)^[\s*_#>]*verdict[*_\s]*:[*_\s]*(ok|concerns|error)\b[*_]*")
_SUMMARY_RE = re.compile(r"(?im)^[\s*_#>]*summary[*_\s]*:[*_ \t]*")
_FINDINGS_RE = re.compile(r"(?im)^[\s*_#>]*findings[*_\s]*:[*_ \t]*")


def parse_review(text: str) -> tuple[str, str, str | None]:
    """A reviewer's final message -> (verdict, summary, findings or None).

    Reviewers are asked for the REVIEW_FORMAT shape but are not forced into it. Anything before
    the last VERDICT line (the reviewer's working) is dropped; without SUMMARY/FINDINGS labels the
    first paragraph after it is the summary and the rest the findings. A reply with no VERDICT
    line at all (or the mock harness) becomes verdict "error" with the whole reply as summary, so
    callers never special-case a parse failure.
    """
    text = (text or "").strip()
    matches = list(_VERDICT_RE.finditer(text))
    if not matches:
        return "error", text or "(reviewer produced no output)", None
    m = matches[-1]
    rest = text[m.end():].strip()
    fm = _FINDINGS_RE.search(rest)
    if fm is not None:
        summary, findings = rest[:fm.start()], rest[fm.end():]
    else:
        summary, _, findings = rest.partition("\n\n")
    summary = _SUMMARY_RE.sub("", summary.strip(), count=1).strip()
    findings = findings.strip()
    if findings.lower().strip("-*. ") == "none":
        findings = ""
    return m.group(1).lower(), summary, findings or None


def node_review_prompt(store: Store, plan_id: int, node_id: int) -> str:
    """One node's task, reported result, declared scope and captured diffs."""
    node = store.get_node(plan_id, node_id)
    parts = [
        f"Node [{plan_id}:{node_id}]: {node['title']}",
        f"Task description:\n{node['description'] or '(no further description)'}",
        f"Result summary the node reported:\n{(node['result'] or '').strip() or '(none)'}",
    ]
    files = store.node_files(plan_id, node_id)
    if files:
        parts.append(
            "Declared file scope (diff coverage is complete for these paths; a directory "
            "covers everything below it):\n" + "\n".join(f"  - {f['path']}" for f in files)
        )
    if node["undeclared_files"]:
        more = f"\n  ... and {node['undeclared_more']} more" if node["undeclared_more"] else ""
        shared = (" Other steps of this plan ran at the same time, so some of these may be their changes, "
                  "not this node's." if node["undeclared_shared"] else "")
        parts.append(
            "Files changed during this node's run OUTSIDE its declared scope (names only: the content "
            "of these changes is not shown, and a file may have been changed by a tool run rather "
            "than edited on purpose)." + shared + "\n"
            + "\n".join(f"  - {f}" for f in node["undeclared_files"]) + more
        )
    diffs = store.node_diffs(plan_id, node_id)
    if not diffs:
        # an automatic review gets here only for a node with undeclared changes alone
        parts.append(
            "No diffs were captured for this node." + (
                " Nothing in the declared scope changed." if files else
                " It declared no files, and only declared files are diffed; say so rather than "
                "assume nothing changed."
            )
        )
        return "\n\n".join(parts)
    parts.append("Git diffs of the declared scope between the start and the end of this node's run:")
    for d in diffs:
        parts.append(f"--- {d['path']} ---\n{d['diff']}")
    return "\n\n".join(parts)


def plan_review_prompt(store: Store, plan_id: int) -> str:
    """The plan's intent and every node of its tree (subplans included) with its outcome."""
    plan = store.get_plan(plan_id)
    parts = [
        f"Plan: {plan['title']}",
        f"Description:\n{plan['description'] or '(no further description)'}",
        "Every node of the plan tree (including nested subplans), each with its status, "
        "description, result summary and step review if it has one:",
    ]
    for pid in sorted(store.descendant_plans(plan_id)):
        if pid != plan_id:
            sub = store.get_plan(pid)
            parts.append(f"Subplan {pid}: {sub['title']}\n{sub['description'] or '(no description)'}")
        for n in store.nodes(pid):
            review = store.review(pid, n["node_id"])
            lines = [
                f"--- [{pid}:{n['node_id']}] {n['title']} ({n['status']}) ---",
                f"Description: {n['description'] or '(none)'}",
                f"Result: {(n['result'] or '').strip() or '(none)'}",
            ]
            if n["error"]:
                lines.append(f"Error: {n['error']}")
            lines.append(f"Step review: {review['verdict']} -- {review['summary']}" if review
                         else "Step review: (not reviewed)")
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def run_review(engine: Engine, plan_id: int, node_id: int | None, profile: dict[str, Any]) -> None:
    """Run one review session on ``profile`` and record its verdict; node_id=None reviews the
    plan as a whole. Streams into the node's transcript, plan reviews under node 0 (never a real
    node_id, they start at 1). Never raises except on cancellation: a failed review is recorded
    as verdict "error", it must never fail what it reviews."""
    store = engine.store
    ev_node = 0 if node_id is None else node_id
    try:
        if node_id is None:
            prompt, system, title = (plan_review_prompt(store, plan_id), PLAN_REVIEW_SYSTEM_APPEND,
                                     store.get_plan(plan_id)["title"])
        else:
            prompt, system, title = (node_review_prompt(store, plan_id, node_id), NODE_REVIEW_SYSTEM_APPEND,
                                     store.get_node(plan_id, node_id)["title"])
        harness = engine.harnesses.review_harness_for(profile)
        job = AgentJob(
            plan_id=plan_id,
            node_id=ev_node,
            prompt=prompt,
            system_append=system.format(tests=engine.tests_note(plan_id)),
            cwd=engine.plan_workdir(plan_id),
            profile=public_profile(profile),
            node_title=title,
        )
        what = "plan review" if node_id is None else "review"
        engine._emit(plan_id, ev_node, "system",
                     {"text": f"{what}: {engine.harnesses.label(profile)}", "review": True})
        result: dict[str, Any] | None = None
        async with aclosing(harness.run(job)) as stream:
            async for kind, data in stream:
                if kind == "usage":
                    continue  # a review's tokens are its own cost, recorded below only
                if kind == "result":
                    result = data
                engine._emit(plan_id, ev_node, kind, {**data, "review": True})
                if result is not None:
                    break
        if result is None or result.get("is_error"):
            errs = (result or {}).get("errors") or [(result or {}).get("text") or "review failed"]
            store.set_review(plan_id, node_id, "error", summary="; ".join(map(str, errs)),
                             agent_name=engine.harnesses.agent_name(profile))
        else:
            verdict, summary, findings = parse_review(result.get("text", ""))
            store.set_review(plan_id, node_id, verdict, summary=summary, findings=findings,
                             agent_name=engine.harnesses.agent_name(profile),
                             tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 -- a review must never fail what it reviews
        log.exception("review of (%s, %s) failed", plan_id, node_id)
        store.set_review(plan_id, node_id, "error", summary=f"{type(e).__name__}: {e}",
                         agent_name=engine.harnesses.agent_name(profile))
    finally:
        engine.notify_plan(plan_id)
