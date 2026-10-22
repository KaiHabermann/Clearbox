# Clearbox

**Observable, reviewable agent work.** Big changes, broken into a graph you can watch and audit.

A small service that lets a model **post a plan as a DAG of work items over MCP**, executes the
plan node by node (roots first, then their dependents, in parallel where possible) with agent
sessions attached to the nodes, and shows everything live in the browser: which nodes are being
worked on right now, and — on click — the agent's session in that node as it happens.

```
 model ──MCP──►  create_plan / add_node / add_edge / start_plan
                         │
                 ┌───────▼────────┐  SQLite (plans, nodes, edges, agent profiles, transcripts)
                 │    Clearbox    │──► built-in agent per ready node, chosen by its agent profile:
                 │   scheduler    │      · Claude Agent SDK
                 │                │      · OpenAI-compatible endpoints (OpenAI, vLLM, Ollama, …)
                 └───────┬────────┘◄── or external agents: claim_next_node / complete_node
                         │ SSE
                     browser UI  (DAG view, running markers, live transcripts)
```

## Quick start

Needs Python ≥ 3.10. With [uv](https://docs.astral.sh/uv/):

```powershell
cd Clearbox
uv run clearbox --mock          # fake agents: try the UI without spending tokens
uv run python examples/demo_plan.py --start
```

Open <http://127.0.0.1:8765/>.

For real agents drop `--mock`. Built-in agents run through the Claude Agent SDK, which ships its
own Claude Code CLI and uses your existing Claude login (or `ANTHROPIC_API_KEY`).

```powershell
uv run clearbox                                   # real Claude sessions, 3 in parallel
uv run clearbox --max-concurrency 5 --permission-mode acceptEdits
```

Nodes can also run on any OpenAI-compatible endpoint instead — a local Ollama or vLLM model, or
OpenAI itself — and one plan can mix them per node, see
[Agents and harnesses](#agents-and-harnesses):

```powershell
uv run clearbox --openai-base-url http://localhost:11434/v1 --openai-model qwen2.5-coder:14b
```

Without uv: `pip install -e .` then `clearbox ...` (or `python -m clearbox ...`).

### Connect a model

The MCP endpoint is `http://127.0.0.1:8765/mcp` (streamable HTTP).

```powershell
claude mcp add --transport http clearbox http://127.0.0.1:8765/mcp
```

Then ask, e.g. *"Use clearbox to make a plan for … and start it."*

## MCP tools

| Tool | What it does |
| --- | --- |
| `create_plan(title, description="", parent_plan_id=None, parent_node_id=None)` | New plan → integer id (1, 2, 3 …). With a parent it becomes the **subplan** of that node. |
| `add_node(plan_id, title, description="", agent=None, files=None)` | New node → `[plan_id, node_id]`. Node ids count up per plan. `agent` (profile name or id) pins the node to an agent profile; omitted = inherit. `files` declares the paths it will create or modify, see [File scoping](#file-scoping-avoiding-clobbered-edits). |
| `add_edge(plan_id, from_node_id, to_node_id)` | `from` must finish before `to` starts. Cycles are rejected. |
| `remove_edge`, `remove_node` | Corrections while building. |
| `update_node(plan_id, node_id, title=None, description=None, files=None)` | Change only what you pass; `files` omitted keeps the current set, `[]` clears it. |
| `set_node_files(plan_id, node_id, files)` | Replace a node's declared files on their own, without touching title/description; `[]` or `null` clears them. Only before the node has run. |
| `get_plan(plan_id)`, `list_plans()` | Structure, status, results, progress. `get_plan` also shows `default_agent` and, per node, the resolved `agent` and its `agent_override`, plus `files` and `conflicts` (see [File scoping](#file-scoping-avoiding-clobbered-edits)). |
| `list_agents()` | Agent profiles (secrets redacted to `has_api_key`; `default: true` marks the server default). |
| `create_agent(name, harness, model=None, base_url=None, api_key_env=None, params=None, tools=None, permission_mode=None, system_append=None, max_parallel=None, description="")` | New agent profile. Deliberately takes no literal `api_key`: secrets should not pass through a model's context. |
| `update_agent(agent, …same fields…)` | Change only the fields passed; `""` clears a text field, `max_parallel=0` removes the limit, `params`/`tools` replace the whole object. |
| `delete_agent(agent)` | Delete a profile; nodes and plans that used it inherit again. |
| `set_node_agent(plan_id, node_id, agent)` | Per-node override; `null` clears it. Applies the next time the node starts. |
| `set_plan_agent(plan_id, agent)` | Default for the plan and its subplans; `null` clears it. |
| `test_agent(agent)` | Liveness check: for `openai` lists the endpoint's models (falls back to a tiny completion) → `{ok, detail, reachable, model_listed, models_sample, credential}`. |
| `start_plan(plan_id, executor="builtin" \| "external")`, `stop_plan(plan_id)` | Run / pause a top-level plan. |
| `claim_next_node(agent_name, plan_id=None)` | External agents: take the next ready node (with full context). |
| `report_progress(plan_id, node_id, message, kind="text")` | Append to the node's live transcript. |
| `complete_node(plan_id, node_id, result)`, `fail_node(plan_id, node_id, error)` | Finish a claimed node. |
| `set_plan_review_agent(plan_id, agent)` | Name (or `null` to clear) the agent profile that automatically reviews this plan's steps and, once it finishes, the plan as a whole; see [Reviewing](#reviewing). |
| `review_node(plan_id, node_id, agent=None)`, `review_plan(plan_id, agent=None)` | Review a done node's diff, or a finished plan's steps as a whole, right now; `agent` is a one-off override, used for this run only. |
| `get_reviews(plan_id)` | Every review recorded for this plan: the plan-level one plus each node's verdict and findings. |

## How execution works

* Node states: `pending → ready → running → done | failed`.
* A node becomes **ready** when all its prerequisites are done. Root nodes are ready immediately.
* **builtin** executor: the service starts one agent session per ready node (up to
  `--max-concurrency` in total, and up to each agent profile's `max_parallel`), on the harness the
  node's agent profile names (see [Agents and harnesses](#agents-and-harnesses)). The prompt contains the overall goal, the chain of parent nodes for
  subplans, the other nodes of the plan, and the **results of the prerequisite nodes**. The agent's
  final message becomes the node's result and is handed downstream.
* **external** executor: ready nodes wait until something calls `claim_next_node`.
* **Subplans**: a node with a subplan skips the agent; when it is reached its subplan runs, and the
  node is done once the whole subplan is done (its result summarises the subplan's final nodes).
  Built-in agents also get the MCP tools, so an agent can decide mid-run that its node is too big
  and *expand* it into a subplan (`create_plan(parent_plan_id=…, parent_node_id=…)`); the node then
  finishes after that subplan (disable with `--no-agent-mcp`). `openai` agents get the same tools
  through a built-in MCP client, prefixed `vc_` (`vc_create_plan`, …).
* All agents of one plan tree share a working directory: `<workdir>/plan-<root id>`
  (default `./workspace/plan-N`).
* A failed node blocks its dependents. Use **Retry** in the UI. **Stop** cancels running built-in
  agents and re-queues their nodes; **Reset** clears all results and transcripts.
* Everything is stored in `clearbox.db`. Built-in sessions that were running when the service
  stopped are re-queued on the next start.

### File scoping: avoiding clobbered edits

Independent nodes run in parallel, and nothing stops two of them from editing the same file at the
same time — in practice they overwrite each other's work. `files` on a node lists the paths it will
**create or modify**; reading a file never needs declaring, since reads cannot clobber anything.
Declare nothing and a node behaves exactly as before — no conflicts, no waiting; this is also how
every plan made before this feature keeps working unchanged.

Two nodes that could otherwise run at the same time and declare a shared file never actually run
together: they run one after the other, **lowest node number first**. This spans the whole plan
tree — a node in a subplan can conflict with one in the root plan or a sibling subplan — because all
agents of one plan tree share a single working directory (`<workdir>/plan-<root id>`, see above).
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
  so it never delays whatever depends on it. The diff comes only from the file tools (`write_file`,
  `edit_file`, the Claude harness's own edit tools); anything a node wrote through `bash` is
  invisible to it, and the reviewer is told to say so rather than assume the diff is complete.
* **Plan review** (`review_plan` / `POST /api/plans/{id}/review`): once every node in the tree is
  done, one reviewer looks at the plan as a whole — every node's title, description, result and
  step-review verdict — and asks what was missed, contradicted or left out across steps. This is
  explicitly **not** a code review: it never reads diffs or source, only reads what the step
  reviews and results already say.

Both levels run automatically once `set_plan_review_agent` names an agent (the plan review fires
once, right after the last node completes), and both can also be triggered on demand with an
optional one-off `agent=` override, whether or not the plan has one set — so a plan finished before
reviewing existed can still be reviewed after the fact. `get_reviews(plan_id)` /
`GET /api/plans/{id}` read every verdict and finding back. The intended loop: read the plan-level
review's oversights, then turn each one into a follow-up node (or plan) rather than editing by
hand — reviews describe gaps, they don't fill them.

## Agents and harnesses

A **harness** is the runtime that executes a node; an **agent profile** is a named, persisted
configuration of one (harness + model + connection + limits). Nodes and plans refer to a profile by
name or id and never hold connection details themselves, so one plan can run its mechanical nodes
on a cheap or local model and its hard nodes on a strong one. Every harness emits the same
transcript events, so the UI, the database and the live stream look identical whichever ran.

| Harness | What runs the node | Tools |
| --- | --- | --- |
| `claude-sdk` | The Claude Agent SDK (its bundled Claude Code CLI, your Claude login or `ANTHROPIC_API_KEY`). | Its own (Bash, Read, Edit, …), gated by `permission_mode`. |
| `openai` | Any endpoint speaking the OpenAI chat-completions API, driven by Clearbox's own agent loop (ask → run the tool calls → feed results back → repeat until a reply without tool calls). | Clearbox's built-in toolset (below) + the MCP tools. |
| `mock` | A fake agent that prints a plausible transcript. No cost. | Pretend ones. |

On every start the server makes sure these profiles exist (created only when absent, never
overwritten): `claude`, `mock`, and — only if `--openai-base-url`, `OPENAI_BASE_URL` or
`OPENAI_API_KEY` is set — `openai`. The `claude` profile stores no model / permission mode / max
turns, so `--model`, `--permission-mode` and `--max-turns` keep applying to it; a value set on the
profile overrides the flag. Manage profiles in the UI (**Agents** in the sidebar, with a **Test
connection** button), over MCP (`create_agent` …) or over REST (`/api/agents`).

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
| `tools` | JSON object, `openai` only: `{"preset": "all" \| "readonly" \| "none", "allow": […], "deny": […], "mcp": true \| false}`. `allow` adds to the preset, `deny` removes (deny wins); `mcp` overrides `--no-agent-mcp`'s on/off for this profile (it cannot turn the tools on when the server has `--no-agent-mcp`). | preset `all`, MCP as the server |
| `permission_mode` | `claude-sdk` only, same values as `--permission-mode`. | `--permission-mode` |
| `system_append` | Extra system-prompt text for every node on this profile. | none |
| `max_parallel` | Max nodes running on this profile at once, across all plans (on top of `--max-concurrency`). Useful for a local GPU. | no limit |
| `description` | Free text for humans. | `""` |

### Which agent runs a node

First hit wins: **the node's own profile → its plan's default → each ancestor plan's default, up to
the root plan → the server default (`--agent-profile`)**. With no profile anywhere the node runs on
the CLI-configured harness (`claude`, or `mock` with `--mock`) — exactly what a plan without any
agent configuration did before profiles existed. The choice is made each time a node starts, so
editing a profile or an assignment affects the next node to start, not running ones.

In the UI the node drawer shows the resolved agent and where it came from (*set on this node*,
*plan default*, *inherited from plan N*, *server default*) and can override it; the topbar sets the
plan's default for the plan and its subplans. Graph nodes get a coloured chip only when their agent
differs from the plan's default.

### Secrets

Put api keys in environment variables of the Clearbox process and reference them by name with
`api_key_env`; the value is read when a node starts and lives only in the HTTP client. A literal
key can be typed into the Agents panel (or sent as `api_key` to `POST`/`PATCH /api/agents`) for
convenience: it is stored in the database but **never returned** by REST, the stream, MCP or the
logs — they only show `has_api_key`. `""` clears it; leaving the field out keeps it. MCP's
`create_agent`/`update_agent` do not accept a key at all. Error texts from endpoints are scrubbed of
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
| `vc_*` | The clearbox MCP tools (`vc_create_plan`, `vc_add_node`, …) so the agent can expand its node into a subplan; off with `tools.mcp=false` or `--no-agent-mcp`. | |

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
* Unsure? **Test connection** in the Agents panel (or `test_agent`) checks reachability, the key and
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
(`claude-sdk` in permission mode `default`, i.e. read tools only), then runs `test_agent` on
`cheap`. It assigns them the three ways: `set_plan_agent` makes `cheap` the plan default (the
inventory nodes inherit it), `add_node(..., agent="strong")` pins the assessment, and
`set_node_agent` moves the review node. `--cheap NAME` / `--strong NAME` use profiles you already
have; `--repo PATH` reviews another directory. With no `openai` profile configured (e.g. against
`uv run clearbox --mock`) or with `--mock` it creates `mock-cheap` / `mock-strong` instead, so
the whole flow can be watched for free.

## UI

* Left: plans with progress bars. Top: breadcrumb (for subplans), status, Start / Stop / Reset.
* "Working now" lists every node in the plan tree that has an agent on it right now — click to jump.
* Graph: top-down layered DAG. Running nodes glow and spin, edges into them animate;
  subplan nodes are drawn as a stack with a progress bar (double-click to open). Nodes within a
  layer that share a declared file are nudged down a step each and joined by a dashed connector —
  the file stagger, see [File scoping](#file-scoping-avoiding-clobbered-edits).
* Click a node: task, prerequisites, result/error, the **agent** that runs it (with where that
  came from, and a select to override it), the **Files** row (its declared paths, which other nodes
  share one, and — while waiting — which node to wait for) and the **live agent session**
  (messages, thinking, tool calls with their results, final result with turns / time / cost).
* Nodes whose agent differs from the plan's default carry a coloured chip naming it; a dot marks
  an override set on the node itself. The topbar sets the plan's default agent.
* **Agents** in the sidebar manages the profiles: add one, pick its harness, model, endpoint and
  key, choose its toolset, cap its parallelism, and **Test connection** before trusting it.
* A reviewed node carries a small verdict marker (✓ / ! / ?) in its top-right corner; its full-view
  **Review** tab shows the verdict, summary and findings and a button to (re-)review it on demand.
  A review chip next to the plan status does the same for the plan-level review, see
  [Reviewing](#reviewing).
* URLs are deep-linkable: `/#/plan/2/node/3`, `/#/agents`, `/#/agents/4`.

## Developing Clearbox with Clearbox

Run a *frozen* copy as the service so agents can edit the source without breaking the running
instance, and keep the database outside the repo:

```powershell
uv tool install --force .            # snapshot of the current code -> `clearbox` on PATH
clearbox --db $HOME\.clearbox\vc.db --workdir $HOME\.clearbox\workspace
```

The repo contains a `.mcp.json`, so Claude Code started in this folder offers the `clearbox`
server automatically. Create plans with `working_directory` set to the repo path so built-in agents
work on the code. To pick up the agents' changes, stop the service and run
`uv tool install --force .` again.

Agent profiles live in that database, so they survive the reinstall — configure an endpoint once
and every later plan can use it. Two things worth knowing when a plan builds Clearbox itself:

* Built-in `claude-sdk` agents use the login of the **service** process, not of the Claude Code
  session that planned the work. After `claude /login`, or when a session limit hits agents but not
  your own terminal, restart the service so it picks the credentials up.
* Nodes of one plan can run on different accounts or vendors by giving them different profiles —
  useful for spreading a long plan over a rate limit, or for keeping mechanical nodes off a
  metered model.

## Options

| Flag | Env | Default |
| --- | --- | --- |
| `--host`, `--port` | `CLEARBOX_HOST`, `CLEARBOX_PORT` | `127.0.0.1`, `8765` |
| `--db` | `CLEARBOX_DB` | `clearbox.db` |
| `--workdir` | `CLEARBOX_WORKDIR` | `workspace` |
| `--max-concurrency` | `CLEARBOX_MAX_CONCURRENCY` | `3` |
| `--permission-mode` | `CLEARBOX_PERMISSION_MODE` | `acceptEdits` |
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
profile is the source of truth — edit it in the Agents panel or with `update_agent`. An
`--agent-profile` naming a profile that does not exist stops the server with a message listing the
ones that do.

`acceptEdits` lets agents edit files in their workspace but blocks other tools that would need
approval; `bypassPermissions` lets them run anything (only use it in a sandbox). For `openai`
agents the equivalent control is the profile's `tools` preset.

## Layout of the code

```
clearbox/
  store.py        SQLite schema + DAG operations (ids, cycle check) + agent profiles
  engine.py       scheduler, node lifecycle, subplans, external-agent protocol
  bootstrap.py    built-in profiles, server default, startup banner and validation
  mcp_bridge.py   MCP client that hands the clearbox tools to non-Claude harnesses (vc_*)
  mcp_tools.py    MCP tool definitions
  app.py          FastAPI: REST, SSE live stream, MCP mounted at /mcp
  agents.py       backwards-compatible aliases; the implementations moved to harness/
  harness/
    base.py          the event contract every harness implements (AgentJob, Harness, HarnessError)
    claude_sdk.py    Claude Agent SDK harness
    openai_compat.py OpenAI-compatible harness: the agent loop (stream, tool calls, usage)
    mock.py          fake harness
    tools.py         sandboxed toolset for harnesses that bring none (read_file … bash)
    registry.py      picks the harness for a node; per-profile caches and limits
    openai_check.py  /models check behind test_agent
    probe.py         tiny completion round trip when there is no /models listing
  static/         UI (plain HTML/CSS/JS, no build step)
examples/demo_plan.py       builds a demo plan through MCP
examples/mixed_plan.py      builds a plan that mixes a cheap and a strong agent
tests/test_flow.py          end-to-end test (run against a server started with --mock)
tests/test_agents_api.py    REST agent lifecycle, incl. the secret-handling rules
tests/test_mixed_run.py     per-node agents and max_parallel in a real run
tests/test_openai_harness.py  the agent loop against tests/fake_openai.py (no network, no DB)
```
