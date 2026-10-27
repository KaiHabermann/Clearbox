# Changelog

All notable changes to Clearbox are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.1] - 2026-10-27

### Added

- **`clearbox stop` and `clearbox restart`.** `stop [--port N]` shuts down the Clearbox on that
  port (through the new `POST /api/shutdown`, the way Ctrl+C would) and leaves anything else
  alone; `restart [options]` stops it and starts it again in the background. No more hunting
  for the process of a server the plugin started.
- **The version in the UI.** The sidebar footer shows the version of the server it talks to.

## [0.4.0] - 2026-10-27

### Added

- **Claude Code plugin.** The repository is a plugin marketplace: `/plugin marketplace add
  KaiHabermann/Clearbox` and `/plugin install clearbox@clearbox` set up the MCP connection and
  the skill that has Claude plan multi-step tasks in Clearbox. The manual `claude mcp add` route
  still works. The plugin's port is an option (`/config`, default 8765), and the plugin starts
  the server: its SessionStart hook runs the new `clearbox ensure`, which starts Clearbox in the
  background unless one already answers on the port (`GET /api/health`).
- **`/mcp/observe`**, a slim MCP endpoint for a session that tracks its own work: ten tools and
  short self-tracking instructions instead of the full set, and its `submit_plan` starts the plan
  for the session right away. `/mcp` stays for built-in execution, agent profiles and reviews.
- **Undeclared-change warning.** Every run also snapshots the whole working tree, and files a
  step changed outside its declared files are listed in the drawer, the Changes tab, the node
  views and the step reviewer's prompt. A step whose only changes are undeclared is reviewed too.
- **Export as a PR description.** **Export** in the topbar (also `export_plan` over MCP and
  `GET /api/plans/{id}/export.md`) copies the plan as Markdown: goal, steps, results, reviews,
  warnings and each step's diff.
- **Per-step worktrees.** The opt-in `isolate_steps` plan setting runs each built-in step in its
  own git worktree, so parallel steps never see each other's half-done edits. Changes are applied
  back when the step succeeds; a conflicting step fails and its patch is kept.
- **`--default-capability`** (`CLEARBOX_DEFAULT_CAPABILITY`) sets what built-in agents without
  an explicit capability may do. The startup banner, the Agents panel and the node drawer show the
  capability that actually applies.
- **Setup for other MCP clients**: Codex CLI, Cursor, VS Code and Gemini CLI, in
  `docs/clients.md`.

### Changed

- The README is short and leads with observing your own session; the full reference moves to
  `docs/reference.md` and contributor material to `docs/development.md`.
- CI runs on Linux, macOS and Windows.

### Fixed

- Git snapshots no longer stall the server on a large repository.

## [0.3.3] - 2026-10-26

### Changed

- Long step titles widen their card, then shrink, before they are cut off; the topbar and the
  legend wrap instead of overlapping.
- The README opens with screenshots and a short tour.

## [0.3.2] - 2026-10-26

### Changed

- Diffs render side by side with line numbers, the changed words marked, and short notes for new,
  deleted and binary files. The narrow drawer shows a single column.

## [0.3.1] - 2026-10-26

### Changed

- Step diffs come from git snapshots of the declared files, taken when a step starts and when it
  ends, in a git directory of Clearbox's own (the project's `.git` is never touched).

### Fixed

- Steps worked through `claim_next_node`, steps that edit files from the shell and steps run by
  the OpenAI-compatible harness now get a diff too.

## [0.3.0] - 2026-10-25

**Breaking:** eight MCP tools were removed from `/mcp`; clients calling them must switch (see
Removed).

### Added

- `submit_plan` builds a whole plan (nodes and edges) in one call, all or nothing.
- Plans carry `test_instructions`, which agents and reviewers receive verbatim, instead of prompts
  that assumed Clearbox's own test setup.
- Built-in worker agents get their own MCP endpoint, limited to their own step and its subplans.
- `--allowed-host` (`CLEARBOX_ALLOWED_HOSTS`): the server refuses requests with a foreign `Host`
  or `Origin` header, which protects it from other web pages (DNS rebinding).
- Fast unit tests for the scheduler (`uv run pytest`).

### Changed

- Automatic and on-demand reviews share one implementation, so they have the same format, get the
  plan's test instructions and can be cancelled alike.
- Agent prompts and the "do not use the server's port" guard name the port Clearbox actually runs
  on.

### Removed

- `set_node_files`, `set_node_results_from`, `set_node_kind` and `set_node_agent`: use
  `update_node`.
- `create_agent`, `update_agent`, `delete_agent` and `test_agent`: agent profiles are managed in
  the UI and the REST API.

## [0.2.2] - 2026-10-23

**Breaking:** `create_plan` requires a `working_directory` for a top-level plan.

### Changed

- The database and fallback workspace live in the platform's data directory instead of the
  directory Clearbox was started from. An existing `clearbox.db` in the launch directory is still
  used; the banner shows which database is in use.
- A top-level plan must name its working directory, so agents never edit files in a folder nobody
  chose. Subplans inherit it.

## [0.2.1] - 2026-10-23

### Fixed

- A step left ready inside a subplan whose parent step failed is never started later, where it
  could end up editing the same file as another step.

## [0.2.0] - 2026-10-23

**Breaking:** an agent profile's `permission_mode` is replaced by `capability`.

### Added

- Steps for the user (`kind="user"`): the plan waits there until you mark them done.
- A capability per agent profile (`read`, `run`, `edit`, `full`) that means the same for every
  harness.
- A test level per agent profile (`none`, `light`, `standard`, `thorough`; default `standard`).

### Fixed

- Agents could not run commands, because the old default permission mode blocked the shell.
- Reviewers can run the test suite, including on-demand reviews.

## [0.1.0] - 2026-10-22

### Added

- First release: plans as graphs of steps over MCP, run by built-in agents (Claude or
  OpenAI-compatible) or claimed by an outside session, with a live browser UI, per-step diffs,
  token counters and optional reviews.

[Unreleased]: https://github.com/KaiHabermann/Clearbox/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/KaiHabermann/Clearbox/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/KaiHabermann/Clearbox/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/KaiHabermann/Clearbox/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/KaiHabermann/Clearbox/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/KaiHabermann/Clearbox/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/KaiHabermann/Clearbox/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/KaiHabermann/Clearbox/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/KaiHabermann/Clearbox/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/KaiHabermann/Clearbox/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/KaiHabermann/Clearbox/releases/tag/v0.1.0
