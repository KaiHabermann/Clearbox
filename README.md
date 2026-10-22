# VisualClaude

A small service that lets a model **post a plan as a DAG of work items over MCP**, executes the
plan node by node (roots first, then their dependents, in parallel where possible) with agent
sessions attached to the nodes, and shows everything live in the browser: which nodes are being
worked on right now, and — on click — the agent's session in that node as it happens.

```
 model ──MCP──►  create_plan / add_node / add_edge / start_plan
                         │
                 ┌───────▼────────┐  SQLite (plans, nodes, edges, transcripts)
                 │  VisualClaude  │
                 │   scheduler    │──► built-in Claude agent per ready node (Claude Agent SDK)
                 └───────┬────────┘◄── or external agents: claim_next_node / complete_node
                         │ SSE
                     browser UI  (DAG view, running markers, live transcripts)
```

## Quick start

Needs Python ≥ 3.10. With [uv](https://docs.astral.sh/uv/):

```powershell
cd VisualClaude
uv run visualclaude --mock          # fake agents: try the UI without spending tokens
uv run python examples/demo_plan.py --start
```

Open <http://127.0.0.1:8765/>.

For real agents drop `--mock`. Built-in agents run through the Claude Agent SDK, which ships its
own Claude Code CLI and uses your existing Claude login (or `ANTHROPIC_API_KEY`).

```powershell
uv run visualclaude                                   # real Claude sessions, 3 in parallel
uv run visualclaude --max-concurrency 5 --permission-mode acceptEdits
```

Without uv: `pip install -e .` then `visualclaude ...` (or `python -m visualclaude ...`).

### Connect a model

The MCP endpoint is `http://127.0.0.1:8765/mcp` (streamable HTTP).

```powershell
claude mcp add --transport http visualclaude http://127.0.0.1:8765/mcp
```

Then ask, e.g. *"Use visualclaude to make a plan for … and start it."*

## MCP tools

| Tool | What it does |
| --- | --- |
| `create_plan(title, description="", parent_plan_id=None, parent_node_id=None)` | New plan → integer id (1, 2, 3 …). With a parent it becomes the **subplan** of that node. |
| `add_node(plan_id, title, description="")` | New node → `[plan_id, node_id]`. Node ids count up per plan. |
| `add_edge(plan_id, from_node_id, to_node_id)` | `from` must finish before `to` starts. Cycles are rejected. |
| `remove_edge`, `remove_node`, `update_node` | Corrections while building. |
| `get_plan(plan_id)`, `list_plans()` | Structure, status, results, progress. |
| `start_plan(plan_id, executor="builtin" \| "external")`, `stop_plan(plan_id)` | Run / pause a top-level plan. |
| `claim_next_node(agent_name, plan_id=None)` | External agents: take the next ready node (with full context). |
| `report_progress(plan_id, node_id, message, kind="text")` | Append to the node's live transcript. |
| `complete_node(plan_id, node_id, result)`, `fail_node(plan_id, node_id, error)` | Finish a claimed node. |

## How execution works

* Node states: `pending → ready → running → done | failed`.
* A node becomes **ready** when all its prerequisites are done. Root nodes are ready immediately.
* **builtin** executor: the service starts one Claude session per ready node (up to
  `--max-concurrency`). The prompt contains the overall goal, the chain of parent nodes for
  subplans, the other nodes of the plan, and the **results of the prerequisite nodes**. The agent's
  final message becomes the node's result and is handed downstream.
* **external** executor: ready nodes wait until something calls `claim_next_node`.
* **Subplans**: a node with a subplan skips the agent; when it is reached its subplan runs, and the
  node is done once the whole subplan is done (its result summarises the subplan's final nodes).
  Built-in agents also get the MCP tools, so an agent can decide mid-run that its node is too big
  and *expand* it into a subplan (`create_plan(parent_plan_id=…, parent_node_id=…)`); the node then
  finishes after that subplan (disable with `--no-agent-mcp`).
* All agents of one plan tree share a working directory: `<workdir>/plan-<root id>`
  (default `./workspace/plan-N`).
* A failed node blocks its dependents. Use **Retry** in the UI. **Stop** cancels running built-in
  agents and re-queues their nodes; **Reset** clears all results and transcripts.
* Everything is stored in `visualclaude.db`. Built-in sessions that were running when the service
  stopped are re-queued on the next start.

## UI

* Left: plans with progress bars. Top: breadcrumb (for subplans), status, Start / Stop / Reset.
* "Working now" lists every node in the plan tree that has an agent on it right now — click to jump.
* Graph: top-down layered DAG. Running nodes glow and spin, edges into them animate;
  subplan nodes are drawn as a stack with a progress bar (double-click to open).
* Click a node: task, prerequisites, result/error, and the **live agent session**
  (messages, thinking, tool calls with their results, final result with turns / time / cost).
* URLs are deep-linkable: `/#/plan/2/node/3`.

## Developing VisualClaude with VisualClaude

Run a *frozen* copy as the service so agents can edit the source without breaking the running
instance, and keep the database outside the repo:

```powershell
uv tool install --force .            # snapshot of the current code -> `visualclaude` on PATH
visualclaude --db $HOME\.visualclaude\vc.db --workdir $HOME\.visualclaude\workspace
```

The repo contains a `.mcp.json`, so Claude Code started in this folder offers the `visualclaude`
server automatically. Create plans with `working_directory` set to the repo path so built-in agents
work on the code. To pick up the agents' changes, stop the service and run
`uv tool install --force .` again.

## Options

| Flag | Env | Default |
| --- | --- | --- |
| `--host`, `--port` | `VISUALCLAUDE_HOST`, `VISUALCLAUDE_PORT` | `127.0.0.1`, `8765` |
| `--db` | `VISUALCLAUDE_DB` | `visualclaude.db` |
| `--workdir` | `VISUALCLAUDE_WORKDIR` | `workspace` |
| `--max-concurrency` | `VISUALCLAUDE_MAX_CONCURRENCY` | `3` |
| `--permission-mode` | `VISUALCLAUDE_PERMISSION_MODE` | `acceptEdits` |
| `--model` | `VISUALCLAUDE_MODEL` | SDK default |
| `--max-turns` | | unlimited |
| `--mock`, `--mock-speed` | `VISUALCLAUDE_AGENT=mock` | off |
| `--no-agent-mcp` | | agents get the MCP tools |

`acceptEdits` lets agents edit files in their workspace but blocks other tools that would need
approval; `bypassPermissions` lets them run anything (only use it in a sandbox).

## Layout of the code

```
visualclaude/
  store.py      SQLite schema + DAG operations (ids, cycle check)
  engine.py     scheduler, node lifecycle, subplans, external-agent protocol
  agents.py     Claude Agent SDK backend (+ mock backend)
  mcp_tools.py  MCP tool definitions
  app.py        FastAPI: REST, SSE live stream, MCP mounted at /mcp
  static/       UI (plain HTML/CSS/JS, no build step)
examples/demo_plan.py   builds a demo plan through MCP
tests/test_flow.py      end-to-end test (run against a server started with --mock)
```
