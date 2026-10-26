# Other MCP clients

Clearbox is a plain streamable-HTTP MCP server, so any client that speaks that transport can use
it. Claude Code has its own plugin, see [Connecting Claude Code](reference.md#connecting-claude-code).
This page covers Codex CLI, Cursor, VS Code (GitHub Copilot agent mode) and Gemini CLI.

Which endpoint (see [Two endpoints](reference.md#two-endpoints-mcp-and-mcpobserve)):

* `http://127.0.0.1:8765/mcp/observe` for a session tracking its own work (the usual case). Its
  server instructions carry the workflow: submit the plan, claim and complete each step.
* `http://127.0.0.1:8765/mcp` when Clearbox should run the plan with its own agents, or for agent
  profiles and reviews. Then tell the session what to do in your prompt.

Start the server first (`clearbox`); on another port, change the URL. Then ask, for example:
*Use clearbox to plan this task and work through the steps yourself.*

## Codex CLI

Status: config verified here (codex-cli 0.116.0, with a scratch `CODEX_HOME`: `codex mcp list`
shows the server as streamable HTTP). No session was run through it.

```sh
codex mcp add clearbox --url http://127.0.0.1:8765/mcp/observe
codex mcp list
```

This writes to `~/.codex/config.toml`:

```toml
[mcp_servers.clearbox]
url = "http://127.0.0.1:8765/mcp/observe"
```

The same table in a project's `.codex/config.toml` scopes it to that project (trusted projects
only). Server instructions: the Codex docs say it reads them and uses them as guidance for the
server's tools.

## VS Code (GitHub Copilot agent mode)

Status: from the docs, not verified (VS Code 1.142.0 is installed here, but the config was not
loaded in the editor).

Workspace `.vscode/mcp.json`:

```json
{
  "servers": {
    "clearbox": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp/observe"
    }
  }
}
```

For all workspaces, run **MCP: Open User Configuration** and add the same entry, or:

```sh
code --add-mcp '{"name":"clearbox","type":"http","url":"http://127.0.0.1:8765/mcp/observe"}'
```

MCP tools are only offered in agent mode. Server instructions: VS Code lists them as a supported
MCP feature; if Copilot ignores the workflow, add the [rules snippet](#rules-snippet) to
`.github/copilot-instructions.md` or `AGENTS.md`.

## Cursor

Status: from the docs, not verified (Cursor 2.1.42 is installed here, but the config was not
loaded in the editor).

Project `.cursor/mcp.json`, or `~/.cursor/mcp.json` for every project:

```json
{
  "mcpServers": {
    "clearbox": {
      "url": "http://127.0.0.1:8765/mcp/observe"
    }
  }
}
```

Server instructions: Cursor's docs do not say whether it passes them to the model. Add the
[rules snippet](#rules-snippet) as a project rule (`.cursor/rules/clearbox.mdc`) or to
`AGENTS.md`.

## Gemini CLI

Status: from the docs, not verified (not installed here).

```sh
gemini mcp add --transport http clearbox http://127.0.0.1:8765/mcp/observe
```

This writes to the project's `.gemini/settings.json` (`-s user` for `~/.gemini/settings.json`).
By hand, note `httpUrl`: in Gemini CLI, `url` means an SSE server.

```json
{
  "mcpServers": {
    "clearbox": {
      "httpUrl": "http://127.0.0.1:8765/mcp/observe"
    }
  }
}
```

Server instructions: the Gemini CLI docs say they are appended to the system instructions.

## Rules snippet

For a client that drops server instructions, put this in its rules file (`AGENTS.md`,
`.cursor/rules/…`, `.github/copilot-instructions.md`, `GEMINI.md`):

```markdown
## Clearbox
For any task of three or more steps, track it in Clearbox (MCP server `clearbox`):
- One `submit_plan(title, working_directory, nodes, edges)` per task; it also starts the plan.
  One node per step: a short verb-phrase title, a description for the user, `files` it will
  change. An edge `[from_key, to_key]` for every real dependency; `kind="user"` for steps only
  the user can do. Give the user the link http://127.0.0.1:8765/#/plan/<plan_id>.
- Then loop: `claim_next_node(agent_name, plan_id)`, do exactly that step,
  `complete_node(plan_id, node_id, result)` with 1-3 sentences. One step at a time;
  `fail_node` with the reason if it is abandoned.
- Keep the graph true: `add_node` + `add_edge` for new work, `remove_node` for steps that
  turn out unnecessary. Blocked on the user: add a `kind="user"` node and say so.
```
