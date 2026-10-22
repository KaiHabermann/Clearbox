"""Command line entry point: ``visualclaude`` / ``python -m visualclaude``."""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from .app import Settings, create_app
from .bootstrap import StartupError


def main() -> None:
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="visualclaude", description=__doc__)
    ap.add_argument("--host", default=env("VISUALCLAUDE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("VISUALCLAUDE_PORT", "8765")))
    ap.add_argument("--db", default=env("VISUALCLAUDE_DB", "visualclaude.db"), help="SQLite file")
    ap.add_argument("--workdir", default=env("VISUALCLAUDE_WORKDIR", "workspace"),
                    help="base directory; each plan's agents work in <workdir>/plan-<id>")
    ap.add_argument("--mock", action="store_true", default=env("VISUALCLAUDE_AGENT") == "mock",
                    help="use fake agents (no Claude sessions, no cost) to try out the UI")
    ap.add_argument("--mock-speed", type=float, default=1.0)
    ap.add_argument("--max-concurrency", type=int, default=int(env("VISUALCLAUDE_MAX_CONCURRENCY", "3")))
    ap.add_argument("--permission-mode", default=env("VISUALCLAUDE_PERMISSION_MODE", "acceptEdits"),
                    choices=["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"],
                    help="permission mode for built-in agents")
    ap.add_argument("--model", default=env("VISUALCLAUDE_MODEL"))
    ap.add_argument("--max-turns", type=int, default=None)
    ap.add_argument("--no-agent-mcp", action="store_true",
                    help="do not give built-in agents access to the visualclaude MCP tools")
    ap.add_argument("--agent-profile", default=env("VISUALCLAUDE_AGENT_PROFILE"), metavar="NAME",
                    help="server-wide default agent profile (default: 'mock' with --mock, else 'claude')")
    ap.add_argument("--openai-base-url", default=env("OPENAI_BASE_URL"), metavar="URL",
                    help="bootstrap the 'openai' profile for this OpenAI-compatible endpoint, "
                         "e.g. http://localhost:11434/v1")
    ap.add_argument("--openai-model", default=env("VISUALCLAUDE_OPENAI_MODEL"), metavar="NAME",
                    help="model of the bootstrapped 'openai' profile")
    ap.add_argument("--openai-api-key-env", default="OPENAI_API_KEY", metavar="NAME",
                    help="NAME of the environment variable holding the 'openai' profile's api key")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings(
        host=args.host,
        port=args.port,
        db=args.db,
        workdir=args.workdir,
        agent="mock" if args.mock else "claude",
        mock_speed=args.mock_speed,
        max_concurrency=args.max_concurrency,
        permission_mode=args.permission_mode,
        model=args.model,
        max_turns=args.max_turns,
        expose_mcp_to_agents=not args.no_agent_mcp,
        default_agent=args.agent_profile,
        openai_base_url=args.openai_base_url,
        openai_model=args.openai_model,
        openai_api_key_env=args.openai_api_key_env,
    )
    try:
        app = create_app(settings)
    except StartupError as e:
        print(f"visualclaude: error: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"VisualClaude UI:  http://{settings.host}:{settings.port}/")
    print(f"MCP endpoint:     {settings.mcp_url}   (agents: {settings.agent})")
    print(f"Agents:           {app.state.agents_line}")
    # timeout_graceful_shutdown: Ctrl+C must always get the port back. Long-lived connections
    # (the SSE stream, MCP sessions) are asked to close first; this drops whatever is left.
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning",
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
