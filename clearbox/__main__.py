"""Command line entry point: ``clearbox`` / ``python -m clearbox``."""

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
    ap = argparse.ArgumentParser(prog="clearbox", description=__doc__)
    ap.add_argument("--host", default=env("CLEARBOX_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("CLEARBOX_PORT", "8765")))
    ap.add_argument("--db", default=env("CLEARBOX_DB", "clearbox.db"), help="SQLite file")
    ap.add_argument("--workdir", default=env("CLEARBOX_WORKDIR", "workspace"),
                    help="base directory; each plan's agents work in <workdir>/plan-<id>")
    ap.add_argument("--mock", action="store_true", default=env("CLEARBOX_AGENT") == "mock",
                    help="use fake agents (no Claude sessions, no cost) to try out the UI")
    ap.add_argument("--mock-speed", type=float, default=1.0)
    ap.add_argument("--max-concurrency", type=int, default=int(env("CLEARBOX_MAX_CONCURRENCY", "3")))
    ap.add_argument("--permission-mode", default=env("CLEARBOX_PERMISSION_MODE", "acceptEdits"),
                    choices=["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"],
                    help="permission mode for built-in agents")
    ap.add_argument("--model", default=env("CLEARBOX_MODEL"))
    ap.add_argument("--max-turns", type=int, default=None)
    ap.add_argument("--no-agent-mcp", action="store_true",
                    help="do not give built-in agents access to the clearbox MCP tools")
    ap.add_argument("--agent-profile", default=env("CLEARBOX_AGENT_PROFILE"), metavar="NAME",
                    help="server-wide default agent profile (default: 'mock' with --mock, else 'claude')")
    ap.add_argument("--openai-base-url", default=env("OPENAI_BASE_URL"), metavar="URL",
                    help="bootstrap the 'openai' profile for this OpenAI-compatible endpoint, "
                         "e.g. http://localhost:11434/v1")
    ap.add_argument("--openai-model", default=env("CLEARBOX_OPENAI_MODEL"), metavar="NAME",
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
        print(f"clearbox: error: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"Clearbox UI:  http://{settings.host}:{settings.port}/")
    print(f"MCP endpoint:     {settings.mcp_url}   (agents: {settings.agent})")
    print(f"Agents:           {app.state.agents_line}")
    # Ctrl+C must always get the port back. uvicorn waits for open connections *before* running
    # the lifespan shutdown, and /api/stream is endless, so the app has to notice the stop itself:
    # it watches server.should_exit, which is set the moment the signal arrives. The timeout is
    # the backstop for anything else still holding a connection.
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning",
                            timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    app.state.server = server
    server.run()


if __name__ == "__main__":
    main()
