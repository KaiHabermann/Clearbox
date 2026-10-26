---
name: clearbox-tracking
description: Use at the start of any task with more than two steps (implementing a feature, a refactor, debugging, an investigation, a multi-file change) and whenever you are about to lay out a plan of action. Puts the plan into Clearbox as a DAG, runs it with yourself as the executor, and keeps the graph in step with the actual work, so the user can follow progress in the Clearbox UI instead of reading long text.
---

# Clearbox tracking

The user follows your work in the Clearbox UI (<http://127.0.0.1:${user_config.port}/>), not in the chat.
The graph there must always answer three questions at a glance: **what is the plan, what is
being worked on right now, and why does each step come where it does.** A person should be able
to judge whether the approach is reasonable from the flowchart alone, without reading your
messages or tool calls.

You are the executor: `submit_plan` starts the plan with `executor="external"` right away, and
you claim and complete its nodes yourself. Clearbox runs no agents of its own here.

## When to use it

- Any task with three or more steps, whether or not there is a separate planning phase.
- If a task that looked small grows, create the plan at that point. The steps already done
  become the first nodes, completed right away with their results.
- Skip it for questions, explanations, and one-step edits.

The tools come from the Clearbox server, which the user starts with `clearbox` (it serves the UI
and the MCP endpoint `/mcp/observe` on port ${user_config.port}). If the Clearbox MCP tools are not available (server not
running), say so once in one line, suggesting `clearbox`, and work normally. Do not stall on it.

## Starting a plan

1. Create and start the plan in one call with `submit_plan`:
   - `title`: the task in a few words. `description`: the goal and what "done" means.
   - `working_directory`: the absolute path of the project you are working in.
   - `nodes`: one per step. Write `title` as a short verb phrase ("Parse config file"), so the
     graph reads as a sequence of actions. Write `description` for the user: what the step does
     and why. Set `files` to the paths the step will create or modify (relative to the working
     directory); this gives the user a per-step diff in the UI.
   - `edges`: `[from_key, to_key]` for every real dependency. An edge means "must finish
     first", so it is what tells the user why a step comes where it does. Steps that do not
     depend on each other get no edge, and the graph shows them side by side.
   - Use `kind="user"` for steps only the user can do (push, deploy, approve, provide a
     credential). The plan visibly stops there and waits for them. See "Blocked on the user"
     below for ones that turn up during the work.
2. Instead of writing the plan out in chat, give one or two lines plus the link
   `http://127.0.0.1:${user_config.port}/#/plan/<plan_id>`.

## Working through it

- `claim_next_node(agent_name="claude", plan_id=<id>)` before you start a step. It hands out
  the **lowest-numbered ready node**, not one you pick. So number nodes in the order you intend
  to do them, and work on the node you were given. If the order matters, it has to be an edge.
- Claim one node at a time, so the graph shows exactly what you are on. Hold several only when
  you really work on them in parallel (for example, with subagents).
- `report_progress(plan_id, node_id, message)` at real milestones inside a long step: a finding,
  a decision, a dead end. Not for every tool call.
- `complete_node(plan_id, node_id, result)` as soon as the step is actually finished, with one to
  three sentences: what was done, and any deviation from the description. Do not complete a step
  in a batch afterwards.
- `fail_node(plan_id, node_id, error)` if a step is abandoned or impossible, with the reason.

## Keeping the graph true while you work

The graph is only useful if its connections are right. Change nodes and edges together, at the
moment you learn something:

- **New work discovered:** `add_node`, then immediately `add_edge` for each prerequisite (from
  the node that revealed it, or whatever it really depends on) and from it to each pending node
  that now needs it. A new node without edges shows up as a root that can start at any time,
  which is false whenever it depends on something.
- **A step needs something first while you are on it:** a running node cannot gain
  prerequisites. Complete the current node with what it did so far, add the missing step and a
  follow-up node for the rest, and wire `current → missing → follow-up → (former dependants)`.
- **A step turns out unnecessary:** `remove_node` while it is pending or ready, and reconnect
  its neighbours with `add_edge` if they still depend on each other through it.
- **A dependency was wrong:** `remove_edge` / `add_edge`. Adding an edge into a ready node puts
  it back to pending, which is correct.
- **A step is too big:** expand it into a subplan with
  `submit_plan(parent_plan_id=…, parent_node_id=…, nodes=…, edges=…)`; it runs with its plan, not
  on its own. If you hold the step, complete it with what it did so far: then the subplan's nodes
  are claimable like any other, and the step finishes when they all have.
- **Direction changes** (the user redirects you, or the approach fails): remove or fail the
  nodes that no longer apply and add the new path, connected to the work it builds on. Do not
  leave stale pending nodes behind.

## Blocked on the user

When work cannot continue without something from the user, such as a missing access token or
API key, a tool or package that is not installed, a login, a permission, or a decision only
they can make, put that in the graph as a user node instead of only asking in chat:

1. Complete the current node with what it got done, saying what is missing.
2. `add_node(..., kind="user")` with a description written as an instruction for a person:
   exactly what to do and where. For example: "Set `GITLAB_TOKEN` in your environment (scope
   `read_api`)" or "Install `ffmpeg` and make sure it is on PATH".
3. Add a follow-up node for the remaining work, and wire
   `current → user node → follow-up → (former dependants)`.
4. Tell the user in one line in chat that the plan is waiting on them, with the plan link.
   Meanwhile, keep working on any other ready nodes that do not depend on it.

The user marks the node done in the UI (**✓ Mark done**). If they tell you in chat instead,
call `complete_node` on the user node yourself, then continue. Do not try to work around the
blocker (for example by hard-coding a placeholder token or skipping the step) unless the user
says to.

## Before each claim

Before each `claim_next_node`, make sure the pending part of the graph still matches what you
intend to do next. If it does not, fix it first.

## Finishing

The plan is finished when every node is done, failed with a reason, or removed. End with a
short summary in chat and the plan link.
