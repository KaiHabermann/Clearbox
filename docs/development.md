# Developing Clearbox

How to work on Clearbox itself; the [reference](reference.md) covers using it.

## Developing Clearbox with Clearbox

Run a *frozen* copy as the service so agents can edit the source without breaking the running
instance, and keep the database outside the repo:

```powershell
uv tool install --force .            # snapshot of the current code -> `clearbox` on PATH
clearbox --db $HOME\.clearbox\vc.db --workdir $HOME\.clearbox\workspace
```

The repo contains a `.mcp.json`, so Claude Code started in this folder offers the `clearbox`
server automatically. Plans take `working_directory`, so point it at the repo path and built-in agents
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

## Layout of the code

```
clearbox/
  store.py        SQLite schema + DAG operations (ids, cycle check) + agent profiles
  engine.py       scheduler, node lifecycle, subplans, external-agent protocol
  prompts.py      what a worker agent is told: system prompt, test levels, the per-node context
  diffs.py        git snapshots of a node's declared files at start and finish, and the diff between them
  reviews.py      step and plan reviews: reviewer prompts, reply parser, the review session
  export.py       a plan as Markdown for a PR description (export_plan, GET /api/plans/{id}/export.md)
  bootstrap.py    built-in profiles, server default, startup banner and validation
  mcp_bridge.py   MCP client that hands the clearbox tools to non-Claude harnesses (vc_*)
  mcp_tools.py    MCP tool definitions, and the scoped subset worker agents get
  app.py          FastAPI: REST, SSE live stream, MCP mounted at /mcp and the workers' /mcp-agent/<token>
  harness/
    base.py          the event contract every harness implements (AgentJob, Harness, HarnessError)
    claude_sdk.py    Claude Agent SDK harness
    openai_compat.py OpenAI-compatible harness: the agent loop (stream, tool calls, usage)
    mock.py          fake harness
    tools.py         sandboxed toolset for harnesses that bring none (read_file … bash)
    registry.py      picks the harness for a node; per-profile caches and limits
    openai_check.py  /models check behind POST /api/agents/{id}/test
    probe.py         tiny completion round trip when there is no /models listing
  static/         UI (plain HTML/CSS and ES modules, no build step): app.js (entry: routing, plan
                  view, live feed), core.js (state, helpers), graph.js, drawer.js, diffview.js
                  (side-by-side and unified diff tables), agents.js
examples/demo_plan.py       builds a demo plan through MCP
examples/mixed_plan.py      builds a plan that mixes a cheap and a strong agent
docs/reference.md           the full reference; docs/development.md is this file
docs/images/                the README's screenshots
tests/unit/                 in-process unit tests of the scheduler, the store and the diffs: `uv run pytest`
tests/test_flow.py          end-to-end test (run against a server started with --mock)
tests/test_agents_api.py    REST agent lifecycle, incl. the secret-handling rules
tests/test_mixed_run.py     per-node agents and max_parallel in a real run
tests/test_file_conflicts.py  file scoping: conflicting nodes run one after the other
tests/test_rework.py        automatic and on-demand reviews, and rework with the findings
tests/test_agent_scope.py   the workers' scoped MCP endpoint: subtree rule, inherited agent, token lifetime
tests/test_host_check.py    Host and Origin checks: foreign names and pages refused, own names and the stream pass
tests/test_openai_harness.py  the agent loop against tests/fake_openai.py (no network, no DB)
tests/test_review_format.py   the reviewer reply parser (no server)
```

## Releasing

The git tag is the trigger and the single source of truth. Bump the version, tag it, push:

```powershell
uv version --bump patch         # or minor / major; edits pyproject.toml
# set the same version in plugin/.claude-plugin/plugin.json
# rename CHANGELOG.md's [Unreleased] section to the new version, and add its compare link
git commit -am "Release 0.1.1: what changed"
git tag v0.1.1
git push origin master v0.1.1
```

Changes are recorded as they land under `## [Unreleased]` in `CHANGELOG.md`; on release that
section becomes the new version's, with the tag's date, and a fresh empty Unreleased section goes
on top. The plugin's version in `plugin/.claude-plugin/plugin.json` is bumped together with
`pyproject.toml`, so the two always match.

`.github/workflows/release.yml` then builds the sdist and wheel, refuses to continue if the tag
disagrees with `pyproject.toml`, checks the wheel really contains `clearbox/static/`, publishes to
PyPI, and only then creates the GitHub release with the artifacts attached. PyPI goes first
because an upload there is permanent, while a GitHub release can be deleted and recreated.

Publishing uses **PyPI Trusted Publishing**, so there is no API token in the repository. Configure
it once on PyPI under *Publishing* with: owner `KaiHabermann`, repository `Clearbox`, workflow
`release.yml`, environment `pypi`. Before the first release the project does not exist on PyPI
yet — add it as a *pending publisher* on your account instead, with the same four values.

`.github/workflows/ci.yml` runs the same build plus the unit tests (`uv run pytest`) and the
mock-agent test suite on every push and pull request, and installs the built wheel into a clean
environment to catch a packaging mistake that an editable install would hide. Action versions
are pinned exactly and kept current by Dependabot.
