# Clearbox

**Make your coding agent's session observable and reviewable.**

![A session's plan filling in live in Clearbox: steps are claimed and finish one after another, two at once for a while, one step's live session and another's diff open on the way, until only the user's own step is left](https://raw.githubusercontent.com/KaiHabermann/Clearbox/master/docs/images/demo.gif)

A long agent session is hard to follow and harder to review: one scrolling transcript, one big
diff at the end. Clearbox gives it structure. Your session lays the work out as a plan, a graph of
steps and what each one waits for, then works through the steps, and Clearbox shows it live in
the browser: what is planned, what is being done right now, why each step comes where it does,
and **what each step changed, as its own diff**.

## Two ways to use it

**1. Your own session, made observable (the main use).** Your agent, e.g. Claude Code, posts the
plan over MCP and then claims and completes each step itself. Clearbox runs no agents and spends
no tokens of its own (unless you turn on reviews): it watches. All you add is a live map of the session and a per-step diff to
review, instead of one transcript and one big change at the end.

**2. Clearbox runs the plan.** Optionally, Clearbox starts a fresh agent per step instead: steps
that do not depend on each other run in parallel, each with only its own task and files in its
prompt, on Claude or on any OpenAI-compatible model (a cheap one for mechanical steps, a strong
one for hard steps). You watch the same way.

## Quick start

Needs Python ≥ 3.10 and `git` on `PATH` (for the per-step diffs).

```powershell
uv tool install clearbox                 # or: pipx install clearbox, or: pip install clearbox
clearbox                                 # UI on http://127.0.0.1:8765/
```

Then, in Claude Code, install the plugin (the MCP connection plus a skill that has Claude plan
multi-step tasks in Clearbox):

```text
/plugin marketplace add KaiHabermann/Clearbox
/plugin install clearbox@clearbox
```

Or, without the plugin, `claude mcp add --transport http clearbox http://127.0.0.1:8765/mcp/observe`
(the slim endpoint for observing your own session; `/mcp` has the full tool set) and ask: *Use
clearbox to plan this task and work through the steps yourself.* See [Connecting Claude Code](https://github.com/KaiHabermann/Clearbox/blob/master/docs/reference.md#connecting-claude-code).

Codex CLI, Cursor, VS Code or Gemini CLI: see [Other MCP clients](https://github.com/KaiHabermann/Clearbox/blob/master/docs/clients.md).

That is way 1: the plan appears in the browser and fills in as your session works. For way 2, connect
the full `/mcp` endpoint and ask it to *start the plan with the built-in executor* instead; `clearbox --mock` lets you try that
without spending tokens.

## What you see

**Each step's session, live.** The agent's messages, thinking and tool calls as they happen;
*Working now* in the top bar lists every step in progress.

![The live session of one step: what the agent says, the files it read and edited, the test run in progress](https://raw.githubusercontent.com/KaiHabermann/Clearbox/master/docs/images/session.png)

**A diff per step.** Every step declares the files it will change. Clearbox snapshots them when the
step starts and again when it ends, so you review a big change one step at a time, side by side,
however the files were changed.

![The side-by-side diff of one step, with line numbers and the changed words highlighted](https://raw.githubusercontent.com/KaiHabermann/Clearbox/master/docs/images/diff.png)

**Optional reviews.** Name a review agent and every finished step is checked against its own diff
and the tests. Reviews are advisory; **Rework this step** re-runs a step with the findings.

![A step review: verdict, summary and findings, with buttons to re-review or rework the step](https://raw.githubusercontent.com/KaiHabermann/Clearbox/master/docs/images/review.png)

## How is this different

Clearbox is for observing and reviewing **your own single session**, with no extra agents.

* **The agent's own todo list** lives in the transcript and is gone when the session ends; it has
  no dependencies between items and no diff per item. A Clearbox plan stays, and each step keeps
  its own diff, result and review.
* **Multi-agent orchestrators and agent task boards** spawn and manage many agents. Clearbox's main
  mode spawns none: your session does the work. Running a fresh agent per step is optional.
* **One big pull request diff at the end** shows what changed, but not in which step or why;
  Clearbox shows the change step by step, next to the plan that explains it.

## Good to know

* **Steps for you.** A step can be yours (push a branch, approve a deploy): the plan waits there
  until you mark it done.
* **Edges order, they do not pass results.** A step gets only its own task, plus the earlier
  results it asks for with `results_from`.
* **Export for review.** **Export** in the topbar copies the plan as Markdown for a pull request
  description: goal, steps, results, reviews and each step's diff (also `export_plan` over MCP).
* **Parallel steps that share a file** run one after the other instead of overwriting each other.
* **Built-in agents are only as restricted as you make them.** By default they may edit files and
  run commands in the plan's working directory; each agent profile sets what it may do, and
  `--default-capability run` (or `read`) restricts every profile that does not. The startup banner
  and the Agents panel show what applies.
* **No authentication.** The server listens on `127.0.0.1` and refuses requests from other web
  pages; whoever can reach the port can run agents.

## Options

| Flag | Env | Default |
| --- | --- | --- |
| `--port`, `--host` | `CLEARBOX_PORT`, `CLEARBOX_HOST` | `8765`, `127.0.0.1` |
| `--mock` | `CLEARBOX_AGENT=mock` | off: built-in steps run real agents |
| `--max-concurrency` | `CLEARBOX_MAX_CONCURRENCY` | `3` built-in agents at once |
| `--agent-profile NAME` | `CLEARBOX_AGENT_PROFILE` | `claude` (`mock` with `--mock`) |
| `--openai-base-url`, `--openai-model` | `OPENAI_BASE_URL`, `CLEARBOX_OPENAI_MODEL` | none: add an OpenAI-compatible agent |
| `--db` | `CLEARBOX_DB` | `clearbox.db` in the platform's data directory |

The rest is in the [reference](https://github.com/KaiHabermann/Clearbox/blob/master/docs/reference.md#options).

## More

* [Reference](https://github.com/KaiHabermann/Clearbox/blob/master/docs/reference.md): how
  execution works, file scoping, results, reviews and rework, agents and capabilities, the UI, the
  MCP tools, every option, where data lives.
* [Development](https://github.com/KaiHabermann/Clearbox/blob/master/docs/development.md):
  working on Clearbox with Clearbox, the code layout, tests and releases.
* [Changelog](https://github.com/KaiHabermann/Clearbox/blob/master/CHANGELOG.md): what changed in
  each version.
