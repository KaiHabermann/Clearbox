# Clearbox reference

Everything the [README](../README.md) leaves out: how a plan runs, the options and where data
lives. Working on Clearbox itself is in [development.md](development.md).

## How execution works

* Node states: `pending → ready → running → done | failed`.
* A node becomes **ready** when all its prerequisites are done. Root nodes are ready immediately.
* **builtin** executor: the service starts one agent session per ready node (up to
  `--max-concurrency` in total, and up to each agent profile's `max_parallel`), on the harness the
  node's agent profile names (see [Agents and harnesses](#agents-and-harnesses)). The prompt contains the overall goal, the chain of parent nodes for
  subplans, a one-line list of the other nodes of the plan, the node's own task, its file scope, and
  **only the earlier results it asked for** (see [Inherited results](#inherited-results-edges-order-results_from-passes)).
  The agent's final message becomes the node's result; it reaches a later node only if that node
  requests it.
* **external** executor: ready nodes wait until something calls `claim_next_node`.
* **User-job nodes** (`kind="user"`): no agent runs them; the plan stops there until you mark the
  node done, see [User-job nodes](#user-job-nodes-a-step-a-person-does).
* **Subplans**: a node with a subplan skips the agent; when it is reached its subplan runs, and the
  node is done once the whole subplan is done (its result summarises the subplan's final nodes).
  Built-in agents also get MCP tools, so an agent can decide mid-run that its node is too big
  and *expand* it into a subplan (`submit_plan(parent_plan_id=…, parent_node_id=…, nodes=…)`); the node then
  finishes after that subplan (disable with `--no-agent-mcp`). `openai` agents get the same tools
  through a built-in MCP client, prefixed `vc_` (`vc_submit_plan`, …). These are a restricted set
  on their own endpoint, see [Worker agents' MCP tools](#worker-agents-mcp-tools).
* All agents of one plan tree share its working directory: the `working_directory` the top-level
  plan names (usually the repository). Subplans inherit it. With `isolate_steps`, each built-in
  step works in its own git worktree instead, see [Isolated steps](#isolated-steps-a-worktree-per-step).
* A failed node blocks its dependents. Use **Retry** in the UI. **Stop** cancels running built-in
  agents and re-queues their nodes; **Reset** clears all results and transcripts.
* Everything is stored in one SQLite database, see
  [Where Clearbox keeps its data](#where-clearbox-keeps-its-data). Built-in sessions that were running when the service
  stopped are re-queued on the next start.

### User-job nodes: a step a person does

Some steps of a plan are not an agent's to do: push a branch, open a merge request, deploy, approve
something. Mark such a node with `kind="user"` (on a `submit_plan` node, `add_node`, `update_node`, or the
**Done by** select in the node drawer; changeable while the node is pending or ready). Nothing else
about the node changes: it has edges, it can declare `files` and request `results_from`, and it
blocks its dependents like any other node.

When its prerequisites finish the node becomes **ready** and the plan simply stops there: the
built-in scheduler never starts a session for it, `claim_next_node` never hands it out, and it
holds no file lock while it waits (a parked user node never delays an agent node that shares a
file). It stays `ready` until you mark it done — the **✓ Mark done** button in its drawer, MCP's
`complete_node`, or `POST /api/plans/{id}/nodes/{node_id}/complete` — with an optional result (the
MR link, say) that later nodes can request through `results_from`. Then the nodes after it start as
usual. A plan with a waiting user node is not finished: its plan review does not fire, and its
`progress` rollup counts it under `waiting`.

Write the description **for a person**, not for an agent: it is shown verbatim in the drawer as the
instruction. No agent, prompt, transcript, diff, step review or token cost is involved; **Stop** on a
waiting user node fails it ("stopped by user") and **Retry** puts it back to ready.

Node views carry `kind` (`"agent"` | `"user"`) and `waiting_for_user` (true for a ready user node).
`GET /api/plans/{id}` also lists every waiting user node of the tree under `waiting` as
`{plan_id, node_id, title}`, which is what the topbar's "Waiting for you" chips show. REST sets the
kind with `PUT /api/plans/{plan_id}/nodes/{node_id}/kind` (body `{"kind": "user"}`); switching a
ready node back to `"agent"` starts it.

### File scoping: avoiding clobbered edits

Independent nodes run in parallel, and nothing stops two of them from editing the same file at the
same time — in practice they overwrite each other's work. `files` on a node lists the paths it will
**create or modify**; reading a file never needs declaring, since reads cannot clobber anything.
Declare nothing and a node behaves exactly as before — no conflicts, no waiting; this is also how
every plan made before this feature keeps working unchanged.

Declaring `files` buys three things: conflicting nodes are serialized (below), the agent is told its
scope, and the node gets a **diff** for review. When a node starts (a built-in run or an external
claim), Clearbox snapshots every declared path and compares it again when the node finishes, so a
change shows up however it was made, from the shell as well as from the file tools. Only declared
files are diffed: a node that declares nothing gets no diff, and a change outside its files shows
up only by name (see [Changes outside the declared files](#changes-outside-the-declared-files) and
[Reviewing](#reviewing)).

Two nodes that could otherwise run at the same time and declare a shared file never actually run
together: they run one after the other, **lowest node number first**. This spans the whole plan
tree — a node in a subplan can conflict with one in the root plan or a sibling subplan — because all
agents of one plan tree share a single working directory (the plan's `working_directory`, see above).
Nodes already ordered by a dependency edge are never held back for this: if `A` must finish before
`B` starts, they cannot overlap, so sharing a file is fine and costs nothing extra. A node takes all
of its declared files at once, never one at a time, so two nodes that each declare the other's file
cannot deadlock — the lower-numbered one always gets to start.

Paths are relative to the plan tree's working directory, exactly like the ones a node's agent reads
and writes; they may name a file that does not exist yet (the node is about to create it) or a
directory, which claims everything under it (e.g. `files=["clearbox/"]` for a node that
restructures a whole package). Matching is case-insensitive and treats `\` and `/` alike on every
platform, not just Windows — `App.js` and `app.js` are treated as the same file even on a
case-sensitive filesystem. That occasionally serializes two genuinely different files; the
alternative is the opposite mistake, two agents writing what they each think is a different file and
one edit getting silently lost, which is worse.

Example: node 3 declares `files=["src/parser.py"]`, node 5 (no dependency between them) declares
`files=["src/"]`. Both become ready at the same time. Node 3 starts; node 5 is held back with
`blocked_by` reason `"running"` until node 3 finishes, then starts. Had node 5 instead been ready
first purely by luck of scheduling, it would still wait — the rule is node number, not arrival order.

REST exposes the same thing as `PUT /api/plans/{plan_id}/nodes/{node_id}/files` (body `{"files":
[...]}`). Every node view, from REST and from MCP's `get_plan` alike, carries `files` (its own
declared paths) and `conflicts` (the other nodes in the tree that share one and could run at the
same time — these are the ones that get serialized); REST's node view adds `blocked_by` while a node
is waiting: which node holds the file, and whether that node is `"running"` or merely has an earlier
`"turn"`.

In the UI, nodes that share a file are drawn one step lower than the last within their layer and
joined by a dashed connector, so the stagger is visible at a glance even though it is not a
dependency edge; a node waiting on one says so in its drawer ("waiting for 3 "Parse config" to
release `src/parser.py`"), and the drawer's **Files** row lets you view and edit a node's declared
files directly (disabled once it has started).

### Changes outside the declared files

A step that edits a file it did not declare would otherwise look clean: its diff covers only its
declared paths. So every run, built-in or external claim, with or without declared files, also
snapshots the **whole working tree** when it starts and when it finishes, and Clearbox lists the
**names** of the files that changed in between but are not covered by the declared paths (a declared
directory covers everything below it; matching is case-insensitive and slash-agnostic, as above).
Only names are kept, never content: these files are not diffed. A step without declared files gets
the list too, and there every change counts — the most useful case, since such a step has no diff
at all.

The list shows as an amber block at the top of the drawer's **Changes** row and of the full view's
**Changes** tab, and the step reviewer's prompt lists it, saying that the content of those changes
is not shown. Node views carry it as `undeclared_files` (at most 200 names, sorted),
`undeclared_more` (how many more were left out) and `undeclared_shared`, in REST and in MCP's
`get_plan` alike. It is cleared whenever the step's diff is (rerun, retry, rework, reset).

Limits:

* **Attribution.** All steps of a plan tree share one working directory, so a change made by a
  step running at the same time lands in this step's list as well. If any other step of the same
  plan tree was running at any point during this run, the list is marked `undeclared_shared` and
  the UI and reviewer say it may include other steps' changes. Anything else touching the folder
  while the step runs (you, an editor, a build watcher) is not told apart at all. A step in its
  own worktree ([Isolated steps](#isolated-steps-a-worktree-per-step)) has an exact list.
* **Ignored files are invisible.** The project's `.gitignore` files and Clearbox's own ignore list
  (`.git`, `node_modules`, `__pycache__`, `.venv`, …) apply, so build output and caches stay out
  of the list — and so does any real change to an ignored file.
* **Net change only.** A file changed and changed back during the run is not listed.
* **Cost.** The snapshot goes through one persistent git index per working directory, in
  Clearbox's own snapshot git directory, shared by all steps there (a lock serialises it), so after
  the first run `git add -A` rehashes only files whose size or timestamp changed. The **first**
  snapshot of a working directory reads, hashes and stores every non-ignored file once — on a big
  repository that takes a while and its size in disk space under `<data dir>/snapshots`. A large
  non-ignored directory (data files, an unignored build tree) costs the same on every change to it.
* Without `git` on `PATH`, or if a snapshot fails, the check is skipped for that run with one log
  line; the step itself is not affected.

### Isolated steps: a worktree per step

File scoping keeps parallel steps off each other's *declared* files, but they still share one
folder: a step sees the half-done edits of the others, a test run can fail on them, and a change
outside the declared files cannot be told apart from theirs. `isolate_steps` (off by default)
gives every **built-in** step of a plan its own copy instead. Set it with `submit_plan` / `create_plan` /
`update_plan(isolate_steps=True)`, `PATCH /api/plans/{id}` (`{"isolate_steps": true}`) or the
topbar's ⑂ pill. It is a top-level plan setting (subplans follow it), allowed only when the working
directory is inside a git repository, and it can only be changed while no step of the plan is
running.

* **Start.** Before the agent starts, Clearbox snapshots the whole working directory (the same
  snapshot as for [undeclared changes](#changes-outside-the-declared-files), so uncommitted and
  untracked files are in it; ignored ones are not), makes a commit of it in its own snapshot git
  directory and checks that out as a git worktree under `<data dir>/snapshots/worktrees`, outside
  the project. The commit's parent is the project's `HEAD`, so inside the worktree `git log` shows
  the project's history and `git status` / `git diff` show exactly what the step changed. The agent's
  working directory is the worktree; the project's own `.git`, branches and index are not touched.
* **Finish.** When the step succeeds, its changes (the worktree against the snapshot it started
  from, binary files included) are applied to the real working directory in one go. The step's diff
  and its undeclared-change list come from its worktree, so they are exact even with steps running
  in parallel, and never marked shared. A step that fails or is stopped applies nothing.
* **Conflicts.** If the real tree changed in the same places while the step ran (another step's
  changes landed there, or you edited it), nothing is applied and the step **fails** with a message
  naming the files. Its patch is kept under `<data dir>/snapshots/patches` (the path is in the
  error), so nothing is lost: resolve it and `git apply` it by hand, or **Retry** the step, which
  starts again from the current tree. Changes in other parts of the same file apply fine.
* **Cleanup.** The worktree is removed when the step ends, however it ends, and any left over from
  a server that stopped mid-run are removed on the next start.

When to use it: built-in plans with steps that run in parallel, especially without declared files
or with tests or builds that would trip over each other's edits. File scoping still applies on top,
so steps that declare the same file still run one after the other.

Cost and limits:

* Every step checks out a full copy of the non-ignored tree, which takes time and disk space on a big
  repository. Ignored files are **not** copied: no `node_modules`, `.venv` or build output, so a
  step that needs them has to recreate them in its worktree (and whatever it builds there is not
  applied back).
* **Built-in steps only.** A step claimed with `claim_next_node` is your own session, which works in
  the real working directory; such plans are not isolated.
* It isolates the working directory, not the machine: a tool given an absolute path, a shell
  command that `cd`s elsewhere, a server or database outside the folder all still reach the real
  thing. Commits an agent makes inside its worktree stay in Clearbox's snapshot git directory; only
  the resulting file changes are applied back.

### Inherited results: edges order, `results_from` passes

An edge means "runs after" and nothing more. A node starts with **no earlier results** in its prompt
unless it asks for them with `results_from=[…]`, so every description must be self-contained: what to
change, where, and any decision an earlier node made that this one has to follow. This is deliberate.
Predecessor summaries run to hundreds of words each; a node handed three of them spends its budget
reading them, and then re-verifying them in the code, instead of doing its own task. Most nodes need
nothing beyond their description.

What a node's prompt contains, in order: the overall goal (plan title and description, plus the parent
chain for subplans), the other nodes of the plan as one-liners (so nodes don't overlap), the requested
results, if any, then its own task and its file scope. The requested results are introduced as finished
work to build on, not to redo.

`results_from` only *passes*; it never orders. A node may only request nodes of its own plan that it
already depends on, directly or through other nodes — anything else might not have a result yet when
it runs, and is rejected with a message saying which edge is missing. `submit_plan` checks it
against the edges of the same call; `add_node` accepts the list before the edges exist (you add
those next), so then the check runs when the edges are known: `update_node` and `start_plan`
reject it. Like `files`, it can only change
while the node is `pending` or `ready`.

The useful pattern: implementation nodes request nothing and start with a short prompt; a final docs or
summary node depends on all of them and requests every result, writing from them instead of
rediscovering the work:

```
submit_plan(title, working_directory, nodes=[
    {"key": "store",  "title": "Store: add the column", …, "files": ["clearbox/store.py"]},
    {"key": "engine", "title": "Engine: use it", …, "files": ["clearbox/engine.py"]},
    {"key": "docs",   "title": "Docs", "description": "Document the change in README.md …",
     "files": ["README.md"], "results_from": ["store", "engine"]},
], edges=[["store", "engine"], ["engine", "docs"]])   # docs depends on store via engine
```

REST: `PUT /api/plans/{plan_id}/nodes/{node_id}/results-from` (body `{"node_ids": [...]}`), and
`GET /api/plans/{plan_id}/nodes/{node_id}/context` → `{"context": "…"}`, the exact prompt the node gets,
built from the plan as it is now (for a node that already ran: what it got, unless the plan changed
since). In the UI, the node drawer's **Inherits results from** row shows and edits the list (checkboxes
offer only the node's dependencies; editable while pending or ready), the **Depends on** row is marked
"ordering only", and the collapsed **Prompt** block shows that same context.

## Reviewing

Reviewing is a second, separate layer on top of execution: it never edits anything, never fails a
node and never blocks a dependent — it only records a verdict (`ok` | `concerns` | `error`) and
findings for a human to act on. Reviewing is **off** for a plan until it names a review agent with
`set_plan_review_agent` (MCP) / `PUT /api/plans/{id}/review-agent`; a plan that never sets one
behaves exactly as it did before reviewing existed, and nobody pays for review tokens by accident.

There are two levels, deliberately kept apart so each stays inside a context it can actually judge:

* **Step review** (`review_node` / `POST /api/plans/{id}/nodes/{node_id}/review`): once a node is
  `done`, a reviewer looks at *that node's* captured diff against *that node's* task description —
  one diff, one task, nothing else. It runs in the background after the node has already finished,
  so it never delays whatever depends on it. The diff covers exactly the node's declared `files`:
  Clearbox stages them into a git tree when the node starts and again when it finishes, and
  diffs the two. Every change to those paths shows up, including changes made through `bash`,
  `sed -i` or a heredoc, as do added and deleted files inside a declared directory (where the
  project's `.gitignore` applies; a file declared by name is captured even if it is ignored). Files
  the node did not declare are not diffed: the reviewer only gets their names, see
  [Changes outside the declared files](#changes-outside-the-declared-files). This needs `git` on
  `PATH`. The snapshots live in a git directory of Clearbox's own next to its
  database (`<data dir>/snapshots`), never in the project's `.git`, and the working directory does
  not have to be a repository. The reviewer is asked to run the test suite before its verdict (with the
  plan's `test_instructions`, if it has them), so reviews get capability `run` (read, search,
  shell; no edits) whatever the profile allows for normal runs, see
  [Capability](#capability-what-an-agent-may-do).
* **Plan review** (`review_plan` / `POST /api/plans/{id}/review`): once every node in the tree is
  done, one reviewer looks at the plan as a whole — every node's title, status, description, result
  and step review — and asks what was missed, contradicted or left out across steps. This is
  explicitly **not** a code review: it never reads diffs or source, only reads what the step
  reviews and results already say.

Both levels run automatically once `set_plan_review_agent` names an agent (the plan review fires
once, right after the last node completes), and both can also be triggered on demand with an
optional one-off `agent=` override, whether or not the plan has one set — so a plan finished before
reviewing existed can still be reviewed after the fact. An automatic and an on-demand review are
the same session: same prompt, same tools, same transcript (marked as a review), same record; an
on-demand one replaces a queued or running review of the same node or plan. Every reviewer ends
its reply with

    VERDICT: ok | concerns | error
    SUMMARY: <one or two sentences>
    FINDINGS:
    <bullet list, or "none">

Anything before the last `VERDICT:` line is dropped; a reply with no `VERDICT:` line at all is
recorded as `error` with the whole reply as its summary. `get_reviews(plan_id)` /
`GET /api/plans/{id}` read every verdict and finding back. The intended loop: read the plan-level
review's oversights, then turn each one into a follow-up node (or plan) rather than editing by
hand — reviews describe gaps, they don't fill them.

### Rework: closing the review loop

For a single step, the loop closes in place: **step runs → step review → rework with the findings →
automatic re-review**. `rework_node(plan_id, node_id, instructions=None)` (MCP) /
`POST /api/plans/{id}/nodes/{node_id}/rework` (body `{"instructions": "…"}`, optional) re-runs that
one node's agent. Its prompt is the node's normal context plus a final **REWORK** section: the
previous attempt is already in the working tree, so do not redo the step from scratch, address
exactly what is listed and change only what that needs. What is listed is your `instructions`
or, if you leave them out, the review's verdict, summary and findings. "Re-run this step and fix X"
works without any review too. With neither instructions nor a review the call is refused; use
retry or a new node instead.

* **Always explicit.** Nothing in Clearbox re-runs a node because a review disliked it. Reviews stay
  advisory, and a rework is always something a person or a planning model asked for.
* **One node only.** Only a `done` or `failed` node can be reworked. A running one has to be stopped
  first, and a node that wraps a subplan is refused: rework a step inside it instead. Nodes that depend on it are
  **not** reset or re-run, even though they may have built on the old output. That limit is
  deliberate: cascading resets would discard finished work that may be fine, and deciding what
  needs redoing is a judgement for whoever asked for the rework. The node view lists its
  `started_dependants` (direct dependants that already ran or are running), and the UI warns when
  some are done, so you know what you are choosing. If they need redoing, rework them too or add
  follow-up nodes.
* **Re-review by itself.** A rework is just another run of the node: its transcript, token counters,
  diff and file locks reset as on any start, and it finishes through the normal path. So if the plan
  has a review agent and the run captured a diff (or changed files outside its declared ones), a new
  step review is queued with no extra trigger.
  Read it with `get_reviews` and repeat if needed. Without a review agent, trigger one with
  `review_node`.

The brief stays on the node as `rework_brief` after the run (so a retry of a failed rework still
carries it); the next rework replaces it, and **Reset** clears it.

## Exporting a plan as a PR description

Review usually happens on GitHub or GitLab, so a plan can be exported as Markdown to paste into a
pull/merge request description (or to send to someone): **Export** in the topbar (**Copy as
Markdown**, **Download .md**, **Include diffs**), `GET /api/plans/{id}/export.md?diffs=1`
(`text/markdown`) or the MCP tool `export_plan(plan_id, diffs=True)` on both endpoints. It contains:

* the plan's title and description (the goal);
* a compact step list in dependency order: status glyph, number, title and the first line of the
  result, user steps marked, a subplan's steps nested under its step (numbered `3.1`, `3.2` …);
* per step: description, result (or error), the review's verdict, summary and findings, the files
  it changed outside its declared ones (a `[!WARNING]` alert), and its diff per file in a collapsed
  `<details><summary>path (+added −deleted)</summary>` block with a fenced `diff`. A fence is always
  longer than any run of backticks inside the diff, so a diff that contains ```` ``` ```` cannot
  break out of it;
* the plan-level review, if there is one.

An id of a subplan exports just that subplan (and the subplans below it), not its top-level plan:
that is the part a branch usually covers. GitHub cuts a PR description at 65,536 characters, so
each file's diff is cut after 400 lines and, once 50,000 characters of diff are in the export, the
remaining files show only their name and line counts, each with a note. `diffs=0` (or unticking
**Include diffs**) leaves the diffs out and lists the changed files with their line counts instead.

## Agents and harnesses

A **harness** is the runtime that executes a node; an **agent profile** is a named, persisted
configuration of one (harness + model + connection + limits). Nodes and plans refer to a profile by
name or id and never hold connection details themselves, so one plan can run its mechanical nodes
on a cheap or local model and its hard nodes on a strong one. Every harness emits the same
transcript events, so the UI, the database and the live stream look identical whichever ran.

| Harness | What runs the node | Tools |
| --- | --- | --- |
| `claude-sdk` | The Claude Agent SDK (its bundled Claude Code CLI, your Claude login or `ANTHROPIC_API_KEY`). | Its own (Bash, Read, Edit, …), gated by the profile's [capability](#capability-what-an-agent-may-do). |
| `openai` | Any endpoint speaking the OpenAI chat-completions API, driven by Clearbox's own agent loop (ask → run the tool calls → feed results back → repeat until a reply without tool calls). | Clearbox's built-in toolset (below) + the MCP tools, gated by the same capability. |
| `mock` | A fake agent that prints a plausible transcript. No cost. | Pretend ones. |

On every start the server makes sure these profiles exist (created only when absent, never
overwritten): `claude`, `mock`, and — only if `--openai-base-url`, `OPENAI_BASE_URL` or
`OPENAI_API_KEY` is set — `openai`. The `claude` profile stores no model / permission mode / max
turns, so `--model`, `--permission-mode` and `--max-turns` keep applying to it; a value set on the
profile overrides the flag. Manage profiles in the UI (**Agents** in the sidebar, with a **Test
connection** button) or over REST (`/api/agents`). MCP only lists them (`list_agents`), so a
planning model can pick one; setting them up is not in its context.

```powershell
uv run clearbox --openai-base-url http://localhost:11434/v1 --openai-model qwen2.5-coder:14b
$env:OPENAI_API_KEY = "sk-…"; uv run clearbox --agent-profile openai   # every node on gpt-4o-mini
```

### Profile fields

| Field | Meaning | Default |
| --- | --- | --- |
| `name` | Unique, not purely numeric; what nodes, plans, MCP tools and the UI refer to. | required |
| `harness` | `claude-sdk`, `openai` or `mock`. | required |
| `model` | Model name as the endpoint knows it (`gpt-4o-mini`, `qwen2.5-coder:14b`, `vendor/model` on OpenRouter, `claude-opus-5-5`). | `claude-sdk`: `--model`, else the SDK default. `openai`: **required**. |
| `base_url` | `openai` only: endpoint root, the part before `/chat/completions` — usually ends in `/v1`. | `https://api.openai.com/v1` |
| `api_key_env` | `openai` only: **name** of the server environment variable holding the key, read when a node starts. | none (`OPENAI_API_KEY` for the default OpenAI url) |
| `api_key` | Literal key, settable from the UI / REST only. Write-only: responses only say `has_api_key`. | none |
| `params` | JSON object. `openai`: `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `reasoning_effort`, `seed`, `parallel_tool_calls`, `max_turns` (50), `stream` (true; false for endpoints with broken streaming), `timeout` (600 s), `max_retries` (2), `extra_body` (passed through verbatim), `input_price_per_mtok` / `output_price_per_mtok` (USD, enables the cost shown in the transcript). `claude-sdk`: `max_turns`. `mock`: `speed`. | `{}` |
| `capability` | What the agent may do, the same for every harness: `read` \| `run` \| `edit` \| `full`, see [Capability](#capability-what-an-agent-may-do). | `null` = `--default-capability` (`full`) as a worker, `run` as a reviewer |
| `test_level` | How much a worker agent is told to test: `none` \| `light` \| `standard` \| `thorough`, see [Test levels](#test-levels-how-much-a-worker-agent-tests). | `null` = `standard` |
| `tools` | JSON object, `openai` only, **raw escape hatch** (the capability picks this for you): `{"preset": "all" \| "readonly" \| "none", "allow": […], "deny": […], "mcp": true \| false}`. `allow` adds to the preset, `deny` removes (deny wins); `mcp` overrides `--no-agent-mcp`'s on/off for this profile (it cannot turn the tools on when the server has `--no-agent-mcp`) and is the one key the capability keeps. | from the capability; `mcp` as the server |
| `permission_mode` | `claude-sdk` only, **raw escape hatch**: same values as `--permission-mode`, overrides the mode the capability chose. | from the capability |
| `system_append` | Extra system-prompt text for every node on this profile. | none |
| `max_parallel` | Max nodes running on this profile at once, across all plans (on top of `--max-concurrency`). Useful for a local GPU. | no limit |
| `description` | Free text for humans. | `""` |

### Worker agents' MCP tools

A built-in worker never sees the `/mcp` or `/mcp/observe` endpoint your own session uses. When the
engine starts a built-in node it mints a random token for that run and hands the agent
`http://127.0.0.1:<port>/mcp-agent/<token>` instead; the token names the node and is revoked the
moment the run ends (finished, failed or stopped), after which the endpoint answers 404, as it
does for a token it never minted. That endpoint serves only what expanding a node needs:
`submit_plan`, `create_plan`, `add_node`, `add_edge`, `remove_edge`, `remove_node`, `update_node`
and `get_plan`, plus read-only `list_agents` and `report_progress` for the agent's own node. Every
plan argument must be inside the node's own subtree: `submit_plan` / `create_plan` attach a subplan
to the agent's own node or to a node of a subplan below it, and the other tools work only on those
subplans. There is no `start_plan` / `stop_plan`, no agent-profile admin, no review, rework or
working-directory tool, no `claim_next_node` / `complete_node` / `fail_node`, and no way to pick an
agent: `submit_plan`, `add_node` and `update_node` refuse an `agent` argument with an error.
Instead a subplan a worker creates gets **the creating node's resolved agent profile as its plan
default**, so expanding a node never runs the sub-work on a more capable agent than the node itself
had (see [Which agent runs a node](#which-agent-runs-a-node)). Reviewers get no clearbox tools at
all; external agents claiming over `/mcp` or `/mcp/observe` are trusted like you. The token never
appears in a transcript; only a `--mock` server hands it out, on a debug route the tests use.

This closes the tool layer, not the machine: an agent with capability `run` or `full` has a shell
and can reach `/mcp` on localhost directly. The scoping protects `read` and `edit` agents, and
stops a prompt injection from steering an agent through its own clearbox tools; it is no sandbox
for a shell-capable agent.

### Capability: what an agent may do

A profile's `capability` is one setting that means the same thing on every harness; the registry
(`clearbox/harness/registry.py`) maps it to each harness's own gating.

| `capability` | Allows | `claude-sdk` | `openai` |
| --- | --- | --- | --- |
| `read` | read and search only; no edits, no commands | Read, Grep, Glob | preset `readonly` |
| `run` | read, search and run commands; no edits | + Bash | `readonly` + `allow: ["bash"]` |
| `edit` | read, search and edit files; no commands | + Write, Edit, MultiEdit | `readonly` + `allow: ["write_file", "edit_file"]` |
| `full` | everything | all of the above, in the server's `--permission-mode` | preset `all` |

Unset (`null`) means **the server's `--default-capability` for a worker agent — `full` unless you
set it — and `run` for a reviewer**: a reviewer has to run the test suite but must never edit. The
flag covers every harness and the implicit CLI harness; a profile with its own capability is
unaffected, and so are reviewers. **`full` lets an agent run arbitrary commands in the plan's
working directory**, with the rights of the Clearbox process; that is the operator's choice, made
when picking the profile (see the warning under [Tools of the `openai` harness](#tools-of-the-openai-harness)
and use a sandbox for anything you do not trust). `--default-capability run` (or `read`) makes a
stricter default one flag away; give the profiles that must edit an explicit capability.

You can always see what applies. The startup banner has a `Built-in agents:` line naming the
default capability (and the server-default profile's, when it differs); the Agents panel shows each
profile's effective capability as a badge, marked `default` when it comes from the server setting
and `custom` when the profile's raw `permission_mode` / `tools` decide; and the node drawer's Agent
row shows the capability of the agent that runs the step. Over REST and `list_agents` a profile
carries `effective_capability`, `capability_source` (`profile` | `default` | `custom`) and
`review_capability`; a node's `agent` carries `capability` and `capability_source`.

For `claude-sdk` the gating is done with the SDK's `allowed_tools`: everything listed is
pre-approved and runs without a prompt. Below `full` the session runs in permission mode `dontAsk`,
which *denies* any tool that is not pre-approved instead of prompting — an SDK session has nobody to
answer a prompt, so any prompting mode would silently block the tool. `full` pre-approves every file
and shell tool and keeps `--permission-mode` for whatever else the model reaches for. A profile's
raw `permission_mode` still overrides the chosen mode, and `openai`'s raw `tools` config is still
honoured for a profile that sets **no** capability, so profiles made before this setting existed
behave as they did. Reviewers ignore the profile's normal-run capability and `tools` /
`permission_mode` entirely and get `run`; `params.review_capability` changes that for one profile.
An unknown value is rejected when the profile is saved.

### Test levels: how much a worker agent tests

Every worker prompt ends with a testing paragraph chosen by the profile's `test_level`; the UI's
**Test level** select uses the same wording:

| `test_level` | Meaning |
| --- | --- |
| `none` | do not test; the node says what to verify, or nothing needs verifying |
| `light` | a quick sanity check that the change works — one script or a couple of cases |
| `standard` | run the existing tests covering what you touched, and fix what you broke |
| `thorough` | run the full suite, and add tests for behaviour you added |

**`standard` is the default**, for new profiles and for every existing one (`null`), and for
external agents claiming through `claim_next_node`. `standard` and `thorough` also say how the
project's tests are run: the plan's `test_instructions` verbatim when it has them (set them on
`submit_plan`, `create_plan`, `update_plan` or the 🧪 pill in the UI's topbar), otherwise an
instruction to work it out from the repository itself (README, CLAUDE.md / AGENTS.md, CI config,
the test files) rather than invent a harness. Either way the agent is told that any server it starts for testing
gets a free port, never the port of the Clearbox server running the plan. The level is for worker
agents only: reviewers keep their own prompt and **always run the suite** before giving a verdict,
whatever the profile's level — which is why they need capability `run`. They get the same
how-to-test text.

### Which agent runs a node

First hit wins: **the node's own profile → its plan's default → each ancestor plan's default, up to
the root plan → the server default (`--agent-profile`)**. With no profile anywhere the node runs on
the CLI-configured harness (`claude`, or `mock` with `--mock`) — exactly what a plan without any
agent configuration did before profiles existed. The choice is made each time a node starts, so
editing a profile or an assignment affects the next node to start, not running ones.

A subplan that a **worker agent** creates by expanding its node starts with that node's resolved
profile as its plan default, so its nodes run on the same agent as the node that made them unless
you change it (see [Worker agents' MCP tools](#worker-agents-mcp-tools)).

In the UI the node drawer shows the resolved agent and where it came from (*set on this node*,
*plan default*, *inherited from plan N*, *server default*) and can override it; the topbar sets the
plan's default for the plan and its subplans. Graph nodes get a coloured chip only when their agent
differs from the plan's default.

### Secrets

Put api keys in environment variables of the Clearbox process and reference them by name with
`api_key_env`; the value is read when a node starts and lives only in the HTTP client. A literal
key can be typed into the Agents panel (or sent as `api_key` to `POST`/`PATCH /api/agents`) for
convenience: it is stored in the database but **never returned** by REST, the stream, MCP or the
logs — they only show `has_api_key`. `""` clears it; leaving the field out keeps it. MCP has no
tool that creates or edits a profile, so a key never passes through a model's context. Error texts from endpoints are scrubbed of
the key before they reach a transcript. Endpoints on localhost or a private network get a
placeholder key when none is configured, since they rarely check it.

### Tools of the `openai` harness

> **Warning:** these tools run arbitrary code in the plan's working directory, with the rights of
> the Clearbox process. The file tools are jailed to that directory (`..` and symlinks are
> resolved before the check), but `bash` is **not** jailed — a shell can always `cd` out. Use preset
> `readonly` (or `deny: ["bash"]`) for models you would not hand a shell, and a sandbox for
> anything you do not trust.

| Tool | What it does | `readonly` |
| --- | --- | --- |
| `read_file(path, offset=0, limit=2000)` | Read a text file with line numbers. | ✓ |
| `write_file(path, content)` | Create or overwrite a file (creates parent directories). | |
| `edit_file(path, old_string, new_string, replace_all=false)` | Exact string replacement; `old_string` must be unique unless `replace_all`. | |
| `list_dir(path, depth)` | Directory listing, optionally recursive. | ✓ |
| `glob(pattern)` | Find files, e.g. `**/*.py`, newest first. | ✓ |
| `grep(pattern, path, glob, max_results=100)` | Regex search over file contents → `path:line: text`. | ✓ |
| `bash(command, timeout_ms=120000)` | Run a shell command (`cmd.exe` on Windows, `/bin/sh` elsewhere), max 600 000 ms; kills the process tree on timeout. | |
| `vc_*` | The worker's scoped clearbox MCP tools (`vc_submit_plan`, `vc_add_node`, …, see [Worker agents' MCP tools](#worker-agents-mcp-tools)) so the agent can expand its node into a subplan; off with `tools.mcp=false` or `--no-agent-mcp`. | |

Walks skip `.git`, `node_modules`, `__pycache__` and virtualenvs; results are truncated like the
Claude harness's.

### Which endpoint works

| Server | `base_url` | Notes |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` (leave empty) | `api_key_env=OPENAI_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | model is the `vendor/model` slug; `api_key_env=OPENROUTER_API_KEY` |
| vLLM | `http://<host>:8000/v1` | start it with `--enable-auto-tool-choice --tool-call-parser …` or tool calls are rejected |
| LM Studio | `http://localhost:1234/v1` | load a model with tool-use support first |
| Ollama | `http://localhost:11434/v1` | model is the tag from `ollama list`, e.g. `qwen2.5-coder:14b` |

Common pitfalls (the transcript error says which one hit):

* **`base_url` must include `/v1`** (the path before `/chat/completions`); without it the endpoint
  answers 404.
* **The model must support tool / function calling.** One that doesn't either gets a 400 or simply
  answers in prose and never touches a file. For pure-text nodes use preset `none`.
* **Small local models often loop** — re-reading the same file, repeating a call. Cap them with
  `params.max_turns` (e.g. 15–25), keep their nodes mechanical, and set `max_parallel: 1` if they
  share one GPU.
* Unsure? **Test connection** in the Agents panel (or `POST /api/agents/{id}/test`) checks reachability, the key and
  whether the model name is listed, without spending tokens.

### Walkthrough: a mixed-harness plan

`examples/mixed_plan.py` builds a plan that reviews a repository (default: this one) without
changing it. Two cheap-model nodes do the mechanical work (inventory the files, inventory the
tests) in parallel, a strong-model node assesses the architecture from their results, and a
strong-model node reviews that assessment:

```powershell
uv run clearbox --openai-base-url http://localhost:11434/v1 --openai-model qwen2.5-coder:14b
uv run python examples/mixed_plan.py --start
```

It creates the agents it needs if missing and prints them: `cheap` (an `openai` copy of the first
configured `openai` profile with preset `readonly`, `max_turns` 20, `max_parallel` 1) and `strong`
(`claude-sdk` with raw permission mode `default` — note that with no `capability` set this is the
server's `--default-capability` (`full` unless set: every tool pre-approved); set `capability="read"` on it for a genuinely read-only
reviewer) over REST, then tests
`cheap` (`POST /api/agents/{id}/test`). It builds the plan node by node, the incremental way, and
assigns the agents the three ways: `set_plan_agent` makes `cheap` the plan default (the inventory
nodes inherit it), `add_node(..., agent="strong")` pins the assessment, and
`update_node(..., agent="strong")` moves the review node. `--cheap NAME` / `--strong NAME` use profiles you already
have; `--repo PATH` reviews another directory. With no `openai` profile configured (e.g. against
`uv run clearbox --mock`) or with `--mock` it creates `mock-cheap` / `mock-strong` instead, so
the whole flow can be watched for free.

## UI

* Left: plans with progress bars. Top: breadcrumb (for subplans), status, Start / Stop / Export /
  Reset (**Export** copies or downloads the plan as Markdown, see
  [Exporting a plan](#exporting-a-plan-as-a-pr-description)).
* "Working now" lists every node in the plan tree that has an agent on it right now — click to jump.
* Graph: top-down layered DAG. A long step title first widens its card (up to about one and a
  half times the normal width), then shrinks a little, and only then is cut; the drawer always shows
  it whole. Running nodes glow and spin, edges into them animate;
  subplan nodes are drawn as a stack with a progress bar (double-click to open). Nodes within a
  layer that share a declared file are nudged down a step each and joined by a dashed connector —
  the file stagger, see [File scoping](#file-scoping-avoiding-clobbered-edits).
* Click a node for its drawer, or **⤢** for the full view with the tabs **Task**, **Changes** (the
  step's diff, side by side, under the names of any files it changed outside its declared ones), **Agent output** and **Review** (`←` / `→` step through the nodes).
* The drawer: task, prerequisites, result/error, the **agent** that runs it (with where that
  came from, and a select to override it), **Inherits results from** and the **Prompt** block (the
  exact prompt it gets, see [Inherited results](#inherited-results-edges-order-results_from-passes)), the **Files** row (its declared paths, which other nodes
  share one, and — while waiting — which node to wait for) and the **live agent session**
  (messages, thinking, tool calls with their results, final result with turns / time / cost).
* Nodes whose agent differs from the plan's default carry a coloured chip naming it; a dot marks
  an override set on the node itself. The topbar sets the plan's default agent.
* A user-job node is drawn as a pill with a double border, a person glyph and a "your job" chip;
  while it waits for you it lights up and pulses. "Waiting for you:" in the topbar lists every such
  node in the plan tree — click to jump. Its drawer shows the instruction in a box with a **✓ Mark
  done** button under it, and the **Done by** row (an agent / you) switches a pending or ready
  node's kind, see [User-job nodes](#user-job-nodes-a-step-a-person-does).
* **Agents** in the sidebar manages the profiles: add one, pick its harness, model, endpoint and
  key, choose **What may this agent do** (the [capability](#capability-what-an-agent-may-do)) and
  its **Test level**, cap its parallelism, and **Test connection** before trusting it. The raw
  **Permission mode** and **Tools preset** selects sit under *Advanced* as escape hatches.
* The topbar's 📁 pill shows a top-level plan's working directory (click to change it before the
  plan starts); the 🧪 pill next to it holds its `test_instructions` (click to edit, any time); the
  ⑂ pill shows whether built-in steps run in their own worktree ("worktrees") or in the working
  directory ("shared tree"); click to switch while no step runs, see
  [Isolated steps](#isolated-steps-a-worktree-per-step).
* A reviewed node carries a small verdict marker (✓ / ! / ?) in its top-right corner; its full-view
  **Review** tab shows the verdict, summary and findings and a button to (re-)review it on demand.
  A review chip next to the plan status does the same for the plan-level review, see
  [Reviewing](#reviewing).
* **⟲ Rework this step** (in the Review tab and the drawer of a finished step) opens a box
  pre-filled with the review's findings. Edit it or clear it to fall back on the findings, then
  **Re-run with these instructions**. A line warns when later steps that depend on it are already
  done and will not be re-run, see [Rework](#rework-closing-the-review-loop).
* URLs are deep-linkable: `/#/plan/2/node/3`, `/#/agents`, `/#/agents/4`.

## Connecting Claude Code

This repository is a Claude Code plugin marketplace. Its `clearbox` plugin (in `plugin/`) brings
two things: the MCP server entry `clearbox` (streamable HTTP, `http://127.0.0.1:8765/mcp/observe`,
see [Two endpoints](#two-endpoints-mcp-and-mcpobserve)) and the
`clearbox-tracking` skill, which tells Claude when to lay out a plan in Clearbox and how to keep
the graph in step with the work. In Claude Code:

```text
/plugin marketplace add KaiHabermann/Clearbox
/plugin install clearbox@clearbox
```

The same works from a shell with `claude plugin marketplace add …` and `claude plugin install …`.
**The plugin starts the server.** Its SessionStart hook runs `clearbox ensure --port <the plugin's
port>` whenever a session starts. `clearbox ensure [options]` takes the same options as `clearbox`:
if a Clearbox already answers on the port (it asks `GET /api/health`), it only says so; if something
else holds the port, it says that and exits with 1; otherwise it starts the server in the
background with those options, waits until it answers (up to 20 s) and exits. So the first session
brings the server up, later ones just connect, and the server keeps running after the sessions end.
It needs `clearbox` on `PATH`; without it the hook fails with a short error and the skill says in
one line that the tools are missing.

A background server writes its output to `server.log` in the data directory (see
[Where Clearbox keeps its data](#where-clearbox-keeps-its-data)). To stop it, end the process that
listens on the port, e.g. on Windows `Get-NetTCPConnection -LocalPort 8765 -State Listen |
ForEach-Object { Stop-Process -Id $_.OwningProcess }`, elsewhere `kill $(lsof -ti tcp:8765)`. A
server you start yourself with `clearbox` in a terminal is found by `ensure` like any other.

**Without the plugin**, add the MCP server by hand. For your session tracking its own work, the
observe endpoint is enough, and its instructions already carry the workflow:

```powershell
claude mcp add --transport http clearbox http://127.0.0.1:8765/mcp/observe
```

To have Clearbox run plans with its own agents, or to use reviews and agent profiles, add the full
endpoint `http://127.0.0.1:8765/mcp` instead and tell Claude what to do in your prompt.

On another port (`clearbox --port 9000`, or `CLEARBOX_PORT`), set the plugin's **Clearbox port**
option under `/config` (it defaults to 8765); the plugin's MCP entry and the skill's links use it.
Without the plugin, use that port in the URL.

Codex CLI, Cursor, VS Code and Gemini CLI: see [Other MCP clients](clients.md).

## MCP tools

### Two endpoints: `/mcp` and `/mcp/observe`

The server offers the same tools on two endpoints (both streamable HTTP, same Host/Origin checks):

* **`/mcp/observe`**, for a session that tracks its own work (way 1, what the plugin uses): eleven
  tools, `submit_plan`, `claim_next_node`, `report_progress`, `complete_node`, `fail_node`,
  `add_node`, `add_edge`, `remove_node`, `remove_edge`, `get_plan`, `export_plan`, and server instructions that
  describe the self-tracking workflow rather than the API. Its `submit_plan` takes no
  `test_instructions` / `max_parallel` (only built-in agents use them) and **starts a top-level plan
  right away with executor `"external"`**, saying so in its result (`"started"`); if the plan cannot
  start (another running plan works in the same folder) nothing is created. A subplan
  (`parent_plan_id`, `parent_node_id`) is not started on its own: it runs with its parent plan.
* **`/mcp`**, the full set below: building, built-in execution, agent profiles, reviews and rework.
  Use it when Clearbox runs the plan (way 2) or you want those features.

What a client loads up front, measured with an MCP client against a mock server (instructions plus
the JSON of the tools list, about four characters per token):

| Endpoint | Tools | Instructions | Tool schemas |
| --- | --- | --- | --- |
| `/mcp` | 25 | 1.6k chars | 26.3k chars (~7.0k tokens with the instructions) |
| `/mcp/observe` | 11 | 2.4k chars | 11.4k chars (~3.5k tokens with the instructions) |

### All tools (`/mcp`)

| Tool | What it does |
| --- | --- |
| `submit_plan(title, working_directory=None, description="", test_instructions=None, nodes=[…], edges=[…], parent_plan_id=None, parent_node_id=None, max_parallel=None, isolate_steps=None)` | **The way to build a plan**: the plan, all its nodes and all its edges in one call → `{"plan_id": …, "nodes": {key: node_id}}`. `nodes` are `{key, title, description, files, results_from, kind, agent}` objects, where `key` is your own label; `edges` are `[from_key, to_key]` pairs and `results_from` names keys too. All or nothing: every problem (duplicate or unknown keys, cycles, a `results_from` the node does not depend on, a path outside the working directory, an unknown agent or kind, the working-directory and subplan rules) is reported in one error, and then nothing has been created. Plan fields as for `create_plan`; with a parent it becomes the **subplan** of that node. |
| `create_plan(title, description="", parent_plan_id=None, parent_node_id=None, working_directory=None, max_parallel=None, test_instructions=None, isolate_steps=None)` | New empty plan → integer id (1, 2, 3 …). With a parent it becomes the **subplan** of that node. A top-level plan must name its `working_directory`; `test_instructions` is free text saying how the project's tests are run, see [Test levels](#test-levels-how-much-a-worker-agent-tests); `isolate_steps=True` runs each built-in step in its own git worktree, see [Isolated steps](#isolated-steps-a-worktree-per-step). Subplans inherit these and may not set them. |
| `update_plan(plan_id, max_parallel=None, test_instructions=None, isolate_steps=None)` | Change a top-level plan's settings; only what you pass changes. `max_parallel=0` removes the limit, `test_instructions=""` removes the instructions, `isolate_steps` switches per-step worktrees on or off (not while a step runs). Same as `PATCH /api/plans/{plan_id}`. |
| `add_node(plan_id, title, description="", agent=None, files=None, results_from=None, kind=None)` | Extend or correct a plan one node at a time → `[plan_id, node_id]`. Node ids count up per plan. `agent` (profile name or id) pins the node to an agent profile; omitted = inherit. `files` declares the paths it will create or modify, see [File scoping](#file-scoping-avoiding-clobbered-edits). `results_from` lists earlier node ids whose result text it receives, see [Inherited results](#inherited-results-edges-order-results_from-passes). `kind="user"` makes it the user's own job, see [User-job nodes](#user-job-nodes-a-step-a-person-does); default `"agent"`. |
| `add_edge(plan_id, from_node_id, to_node_id)` | `from` must finish before `to` starts. Cycles are rejected. Passes no result; that is `results_from`. |
| `remove_edge`, `remove_node` | Corrections. |
| `update_node(plan_id, node_id, title=None, description=None, files=None, results_from=None, kind=None, agent=None)` | Change only what you pass, all or nothing. `files` / `results_from` omitted keeps the current value, `[]` clears it; they and `kind` change only before the node has run. `agent=""` clears the node's own agent so it inherits again; a new agent applies the next time the node starts. |
| `get_plan(plan_id)`, `list_plans()` | Structure, status, results, progress. `get_plan` also shows `working_directory`, `test_instructions` (the top-level plan's, for a subplan too), `default_agent` and, per node, the resolved `agent` and its `agent_override`, plus `files` and `conflicts` (see [File scoping](#file-scoping-avoiding-clobbered-edits)), `results_from`, `kind` and `waiting_for_user` (a user node that is ready and waiting to be marked done). |
| `export_plan(plan_id, diffs=True)` | The plan and its subplans as Markdown for a PR description, see [Exporting a plan](#exporting-a-plan-as-a-pr-description). |
| `list_agents()` | Agent profiles (secrets redacted to `has_api_key`; `default: true` marks the server default), including each one's `capability` and `test_level`. Creating, editing, deleting and testing profiles is setup work, done in the web UI's Agents panel or over REST (`/api/agents`), not over MCP. |
| `set_plan_agent(plan_id, agent)` | Default for the plan and its subplans; `null` clears it. |
| `start_plan(plan_id, executor="builtin" \| "external")`, `stop_plan(plan_id)` | Run / pause a top-level plan. |
| `claim_next_node(agent_name, plan_id=None)` | External agents: take the next ready node, with the same prompt a built-in agent would get. |
| `report_progress(plan_id, node_id, message, kind="text")` | Append to the node's live transcript. |
| `complete_node(plan_id, node_id, result)`, `fail_node(plan_id, node_id, error)` | Finish a claimed node. |
| `set_plan_review_agent(plan_id, agent)` | Name (or `null` to clear) the agent profile that automatically reviews this plan's steps and, once it finishes, the plan as a whole; see [Reviewing](#reviewing). |
| `review_node(plan_id, node_id, agent=None)`, `review_plan(plan_id, agent=None)` | Review a done node's diff, or a finished plan's steps as a whole, right now; `agent` is a one-off override, used for this run only. |
| `get_reviews(plan_id)` | Every review recorded for this plan: the plan-level one plus each node's verdict and findings. |
| `rework_node(plan_id, node_id, instructions=None)` | Re-run one finished (`done` or `failed`) step with its review findings, or your `instructions`, in its prompt; dependants are left alone. It is reviewed again by itself afterwards, see [Rework](#rework-closing-the-review-loop). |

## Options

| Flag | Env | Default |
| --- | --- | --- |
| `--host`, `--port` | `CLEARBOX_HOST`, `CLEARBOX_PORT` | `127.0.0.1`, `8765` |
| `--allowed-host NAME[:PORT]` (repeatable) | `CLEARBOX_ALLOWED_HOSTS` (comma-separated) | loopback and `--host` only |
| `--db` | `CLEARBOX_DB` | `<data dir>/clearbox.db` |
| `--workdir` | `CLEARBOX_WORKDIR` | `<data dir>/workspace` |
| `--max-concurrency` | `CLEARBOX_MAX_CONCURRENCY` | `3` |
| `--permission-mode` | `CLEARBOX_PERMISSION_MODE` | `acceptEdits` |
| `--default-capability {read,run,edit,full}` | `CLEARBOX_DEFAULT_CAPABILITY` | `full` |
| `--model` | `CLEARBOX_MODEL` | SDK default |
| `--max-turns` | | unlimited |
| `--mock`, `--mock-speed` | `CLEARBOX_AGENT=mock` | off |
| `--no-agent-mcp` | | agents get the MCP tools |
| `--agent-profile NAME` | `CLEARBOX_AGENT_PROFILE` | `mock` with `--mock`, else `claude` |
| `--openai-base-url URL` | `OPENAI_BASE_URL` | `https://api.openai.com/v1` |
| `--openai-model NAME` | `CLEARBOX_OPENAI_MODEL` | `gpt-4o-mini` for the OpenAI endpoint, else none |
| `--openai-api-key-env NAME` | | `OPENAI_API_KEY` |

`--model`, `--permission-mode` and `--max-turns` configure the `claude` profile's empty fields (and
any `claude-sdk` profile that leaves them empty). The three `--openai-*` flags only **bootstrap**
the `openai` profile the first time: once it exists they are ignored with a warning, because the
profile is the source of truth — edit it in the Agents panel or with `PATCH /api/agents/{id}`. An
`--agent-profile` naming a profile that does not exist stops the server with a message listing the
ones that do.

What an agent may do is set per profile by its [capability](#capability-what-an-agent-may-do), not
by `--permission-mode`; `--default-capability` is what a worker gets when its profile sets none (an
unknown value stops the server at startup). The flag is the fallback: it is the mode a `claude-sdk` session runs in at
capability `full`, after every file and shell tool has already been pre-approved, so it only
governs what the model reaches for beyond those. Below `full` the session runs in `dontAsk`
regardless. On its own `acceptEdits` is **not** a usable default for unattended agents — it
prompts for Bash, and an SDK session has nobody to answer, so agents could not run anything; that
is what capability replaced. `bypassPermissions` lifts every check (only in a sandbox). For
`openai` agents the raw equivalent is the profile's `tools` preset.

**Security.** The API has no authentication: whoever can reach the port can run agents with
shell access on this machine. It listens on `127.0.0.1` by default, and every request must be
addressed to one of the server's own names (`127.0.0.1`, `localhost`, `[::1]`, the `--host` it
binds unless that is a wildcard, and each `--allowed-host`) or it gets a `421`; a request whose
`Origin` is not one of those gets a `403`. That keeps other web pages in your browser away from
it, including through DNS rebinding. Serving it under a name or on the network (`--host 0.0.0.0`,
which prints a warning) needs that name in `--allowed-host`, e.g. `--allowed-host mybox.lan`; a
name without a port is allowed with the server's port and without one (behind a reverse proxy).
Only do that on a network you trust.

## Where Clearbox keeps its data

The database and the fallback workspace live in the platform's data directory:

| OS | path |
| --- | --- |
| Windows | `%LOCALAPPDATA%\clearbox\` |
| macOS | `~/Library/Application Support/clearbox/` |
| Linux | `$XDG_DATA_HOME/clearbox/`, else `~/.local/share/clearbox/` |

`--db` / `CLEARBOX_DB` and `--workdir` / `CLEARBOX_WORKDIR` override both. Before 0.2.2 these
defaulted to `clearbox.db` and `workspace/` relative to the current directory, so launching from
two folders gave you two unrelated databases; a `clearbox.db` already sitting in the directory you
start from is still used, and the banner says so.

Every top-level plan must name its own `working_directory` -- the directory its agents work in,
normally a repository. Subplans inherit it. Two plans with overlapping directories cannot run at
the same time, since file-conflict locking only orders nodes within one plan tree.
