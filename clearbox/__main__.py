"""Command line entry point: ``clearbox`` / ``python -m clearbox``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import uvicorn

from .app import Settings, create_app, is_loopback
from .bootstrap import StartupError


LEGACY_DB = "clearbox.db"   # the pre-0.2.2 default: relative to wherever you launched


def default_data_dir() -> str:
    """Where an installed clearbox keeps its database and workspaces.

    The old defaults were relative (``clearbox.db``, ``workspace``), so running the
    command from two directories silently gave you two unrelated databases and left
    stray files in whatever folder you happened to be in.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "clearbox")


def build_parser(data_dir: str, legacy: str | None) -> argparse.ArgumentParser:
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="clearbox", description=__doc__,
                                 epilog="clearbox ensure [options]: start a background server with these "
                                        "options unless one already answers on the port. "
                                        "clearbox stop [--port N]: stop the server on that port. "
                                        "clearbox restart [options]: stop, then ensure")
    ap.add_argument("--host", default=env("CLEARBOX_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("CLEARBOX_PORT", "8765")))
    ap.add_argument("--allowed-host", action="append", metavar="NAME[:PORT]",
                    default=[h for h in env("CLEARBOX_ALLOWED_HOSTS", "").split(",") if h.strip()],
                    help="also answer requests addressed to this name (repeatable); loopback and "
                         "--host are always allowed, any other Host or Origin is refused")
    ap.add_argument("--db", default=env("CLEARBOX_DB", legacy or os.path.join(data_dir, "clearbox.db")),
                    help=f"SQLite file (default: {os.path.join(data_dir, 'clearbox.db')})")
    ap.add_argument("--workdir", default=env("CLEARBOX_WORKDIR", os.path.join(data_dir, "workspace")),
                    help="fallback directory for plans created before a working directory was "
                         "required; each such plan's agents work in <workdir>/plan-<id>")
    ap.add_argument("--mock", action="store_true", default=env("CLEARBOX_AGENT") == "mock",
                    help="use fake agents (no Claude sessions, no cost) to try out the UI")
    ap.add_argument("--mock-speed", type=float, default=1.0)
    ap.add_argument("--max-concurrency", type=int, default=int(env("CLEARBOX_MAX_CONCURRENCY", "3")))
    ap.add_argument("--permission-mode", default=env("CLEARBOX_PERMISSION_MODE", "acceptEdits"),
                    choices=["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"],
                    help="permission mode for built-in agents")
    ap.add_argument("--default-capability", default=env("CLEARBOX_DEFAULT_CAPABILITY", "full"),
                    choices=["read", "run", "edit", "full"],
                    help="what built-in worker agents may do when their profile sets no capability "
                         "(default: full -- edit files and run any command)")
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
    return ap


def probe(host: str, port: int, timeout: float = 1.0) -> str | None:
    """What answers on host:port: "clearbox", "other" (something that is not Clearbox), or None
    (nothing listening)."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=timeout) as r:
            return "clearbox" if json.loads(r.read() or b"{}").get("app") == "clearbox" else "other"
    except urllib.error.HTTPError:
        return "other"  # it speaks HTTP, but has no Clearbox health route
    except (OSError, ValueError):
        # refused / timed out: nothing there; ValueError: an answer that is not JSON
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return "other"
        except OSError:
            return None


def ensure(argv: list[str], data_dir: str, legacy: str | None, wait: float = 20.0) -> int:
    """`clearbox ensure [options]`: make sure a Clearbox server answers on the port, starting one
    in the background with the same options if none does, and return at once. Run from the Claude
    Code plugin's SessionStart hook, so the first session brings the server up and later ones just
    connect; it outlives the session that started it. Prints one line (a hook's output reaches the
    model's context, so it stays short)."""
    args = build_parser(data_dir, legacy).parse_args(argv)  # bad options fail here, not in the background
    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{host}:{args.port}/"
    state = probe(host, args.port)
    if state == "clearbox":
        print(f"Clearbox is running: {url}")
        return 0
    if state == "other":
        print(f"clearbox: port {args.port} is taken by something that is not Clearbox; run it on another "
              f"port (clearbox --port N) and set the plugin's Clearbox port to match", file=sys.stderr)
        return 1
    os.makedirs(data_dir, exist_ok=True)
    log_path = os.path.join(data_dir, "server.log")
    kw: dict = {}
    if sys.platform == "win32":
        # its own process group, no console, and out of the hook's job object so it survives the hook
        kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    else:
        kw["start_new_session"] = True
    with open(log_path, "ab") as log:
        cmd = [sys.executable, "-m", "clearbox", *argv]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    cwd=data_dir, **kw)
        except OSError:
            if sys.platform != "win32":
                raise
            # the job does not allow breaking away: start it anyway, it may then end with the hook
            kw["creationflags"] &= ~subprocess.CREATE_BREAKAWAY_FROM_JOB
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    cwd=data_dir, **kw)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"clearbox: the server exited at once (code {proc.returncode}); see {log_path}", file=sys.stderr)
            return 1
        if probe(host, args.port, timeout=0.5) == "clearbox":
            print(f"Started Clearbox in the background: {url} (log: {log_path})")
            return 0
        time.sleep(0.25)
    print(f"clearbox: started, but it did not answer within {wait:.0f} s; see {log_path}", file=sys.stderr)
    return 1


def stop(argv: list[str], data_dir: str, legacy: str | None, wait: float = 15.0) -> int:
    """`clearbox stop [--host H] [--port N]`: ask the Clearbox on that port to shut down (as Ctrl+C
    would) and wait until the port is free. Nothing running is not an error."""
    args = build_parser(data_dir, legacy).parse_args(argv)
    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    state = probe(host, args.port)
    if state is None:
        print(f"No Clearbox is running on port {args.port}")
        return 0
    if state == "other":
        print(f"clearbox: port {args.port} is held by something that is not Clearbox; not touching it",
              file=sys.stderr)
        return 1
    try:
        req = urllib.request.Request(f"http://{host}:{args.port}/api/shutdown", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5).close()
    except urllib.error.HTTPError as e:
        hint = ("it predates `clearbox stop` (0.4.1); end its process instead" if e.code in (404, 405)
                else f"it answered {e.code}")
        print(f"clearbox: could not stop the server on port {args.port}: {hint}", file=sys.stderr)
        return 1
    except OSError:
        pass  # it may close the connection while going down; the wait below decides
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if probe(host, args.port, timeout=0.5) is None:
            print(f"Stopped Clearbox on port {args.port}")
            return 0
        time.sleep(0.25)
    print(f"clearbox: the server on port {args.port} did not stop within {wait:.0f} s", file=sys.stderr)
    return 1


def main() -> None:
    data_dir = default_data_dir()
    # A database left in the current directory by an older version still wins, so nobody's
    # plans disappear on upgrade. The banner says where the new default lives.
    legacy = os.path.abspath(LEGACY_DB) if os.path.isfile(LEGACY_DB) else None
    command, argv = sys.argv[1:2], sys.argv[2:]
    if command == ["ensure"]:
        sys.exit(ensure(argv, data_dir, legacy))
    if command == ["stop"]:
        sys.exit(stop(argv, data_dir, legacy))
    if command == ["restart"]:  # stop whatever runs on the port, then start it again in the background
        sys.exit(stop(argv, data_dir, legacy) or ensure(argv, data_dir, legacy))
    args = build_parser(data_dir, legacy).parse_args()

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
        default_capability=args.default_capability,
        model=args.model,
        max_turns=args.max_turns,
        expose_mcp_to_agents=not args.no_agent_mcp,
        default_agent=args.agent_profile,
        openai_base_url=args.openai_base_url,
        openai_model=args.openai_model,
        openai_api_key_env=args.openai_api_key_env,
        allowed_hosts=args.allowed_host,
    )
    try:
        app = create_app(settings)
    except StartupError as e:
        print(f"clearbox: error: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"Clearbox UI:  http://{settings.host}:{settings.port}/")
    print(f"MCP endpoint:     {settings.mcp_url}   (agents: {settings.agent})")
    print(f"Observe endpoint: {settings.observe_mcp_url}   (a session tracking its own work)")
    print(f"Agents:           {app.state.agents_line}")
    print(f"Built-in agents:  {app.state.capability_line}")
    print(f"Database:         {os.path.abspath(settings.db)}")
    if legacy and os.path.abspath(settings.db) == legacy:
        print(f"                  (a clearbox.db in this folder; the default is now "
              f"{os.path.join(data_dir, 'clearbox.db')} -- move it there, or keep passing --db)")
    if not is_loopback(settings.host):
        print(f"WARNING:          listening on {settings.host}: the API has no authentication, so anyone "
              f"who can reach port {settings.port} can run agents with shell access on this machine. "
              f"Requests must name an allowed host (--allowed-host).")
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
