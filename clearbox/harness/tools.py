"""Sandboxed toolset for harnesses that bring no tools of their own.

The Claude Agent SDK ships Bash/Read/Write/Edit/Grep/... built in. A raw
OpenAI-compatible endpoint ships nothing, so a model behind it can only do
real work if we hand it tools. This module is that toolset. It is deliberately
provider-neutral: a ``ToolSpec`` is just a name, a description, a JSON Schema
for the parameters and an async handler. Turning specs into the OpenAI
function-calling wire format (or any other) is the harness's job.

WARNING: these tools execute arbitrary code in the plan's workspace. The file
tools are jailed to the node's working directory (``root``), but ``bash`` is
NOT jailed -- a shell can always ``cd`` out -- so it is gated only by the
agent profile's tools config. The operator chooses the preset:

- ``"all"``      every tool, including ``bash`` and the write tools
- ``"readonly"`` read_file, list_dir, glob, grep
- ``"none"``     no tools at all (pure chat)

``allow`` adds tools on top of the preset, ``deny`` removes tools from it
(deny wins). Unknown names are ignored so a profile written for a newer
version keeps loading.

Tool names are lowercase_snake so they are valid OpenAI function names; the
descriptions stay close to the Claude Code tools the models already know.
Every result goes through the shared ``_truncate`` and has NUL bytes stripped,
so all harnesses produce identical, SQLite-safe event payloads.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .base import MAX_TOOL_RESULT_CHARS, _truncate

READONLY_TOOLS = ("read_file", "list_dir", "glob", "grep")
PRESETS = ("all", "readonly", "none")

# Directories that are never descended into by list_dir/glob/grep walks unless
# the caller points at them explicitly. They are huge and almost never what a
# model is looking for.
IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}

MAX_LINE_CHARS = 2000
MAX_LIST_ENTRIES = 1000
MAX_GLOB_RESULTS = 1000
MAX_GREP_FILE_BYTES = 2_000_000
MAX_BASH_TIMEOUT_MS = 600_000


class ToolError(Exception):
    """An expected tool failure; its message is shown to the model verbatim."""


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema object for the parameters
    handler: Callable[..., Awaitable[str]]


def _clean(text: str) -> str:
    return _truncate(text.replace("\x00", ""))


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


class Toolbox:
    """The tools one agent session may use, bound to one working directory."""

    def __init__(self, root: str, config: dict[str, Any] | None = None):
        self.root = os.path.realpath(root)
        self.config = config or {}
        self._all = self._build_specs()
        self._enabled = self._select(self.config)

    # ----------------------------------------------------------------- config

    def _select(self, config: dict[str, Any]) -> set[str]:
        preset = config.get("preset") or "all"
        if preset not in PRESETS:
            raise ValueError(f"unknown tools preset {preset!r}; expected one of {', '.join(PRESETS)}")
        names = {s.name for s in self._all}
        if preset == "all":
            enabled = set(names)
        elif preset == "readonly":
            enabled = set(READONLY_TOOLS)
        else:
            enabled = set()
        enabled |= {n for n in config.get("allow") or [] if n in names}
        enabled -= set(config.get("deny") or [])
        return enabled

    def specs(self) -> list[ToolSpec]:
        return [s for s in self._all if s.name in self._enabled]

    async def call(self, name: str, args: dict[str, Any] | None) -> tuple[str, bool]:
        """Run a tool. Never raises: every failure becomes ``(message, True)``."""
        try:
            spec = next((s for s in self._all if s.name == name), None)
            if spec is None or name not in self._enabled:
                avail = ", ".join(s.name for s in self.specs()) or "(none)"
                return _clean(f"Error: tool {name!r} is not available. Available tools: {avail}"), True
            if not isinstance(args, dict):
                return _clean(f"Error: arguments for {name} must be a JSON object"), True
            props = spec.schema.get("properties", {})
            unknown = [k for k in args if k not in props]
            missing = [k for k in spec.schema.get("required", []) if k not in args]
            if unknown or missing:
                parts = []
                if missing:
                    parts.append(f"missing required argument(s): {', '.join(missing)}")
                if unknown:
                    parts.append(f"unknown argument(s): {', '.join(unknown)}")
                return _clean(f"Error: {name}: {'; '.join(parts)}. Parameters: {', '.join(props)}"), True
            text = await spec.handler(**args)
            return _clean(text), False
        except ToolError as e:
            return _clean(f"Error: {e}"), True
        except Exception as e:  # a tool error is agent input, not a crash
            return _clean(f"Error: {name} failed: {type(e).__name__}: {e}"), True

    # -------------------------------------------------------------- path jail

    def _resolve(self, path: str) -> str:
        """Map a model-supplied path into root, refusing anything outside it.

        Relative paths are joined against root; absolute paths are allowed if
        they land inside root. ``realpath`` resolves ``..`` and symlinks before
        the check, so neither can be used to escape.
        """
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path must be a non-empty string")
        full = os.path.realpath(os.path.join(self.root, os.path.expanduser(path)))
        try:
            inside = os.path.commonpath([os.path.normcase(full), os.path.normcase(self.root)]) == os.path.normcase(self.root)
        except ValueError:  # different drives on Windows
            inside = False
        if not inside:
            raise ToolError(f"path {path!r} is outside the working directory {self.root}; access refused")
        return full

    def _rel(self, full: str) -> str:
        rel = os.path.relpath(full, self.root)
        return rel.replace(os.sep, "/")

    # ------------------------------------------------------------------ tools

    async def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return await asyncio.to_thread(self._read_file, path, int(offset), int(limit))

    def _read_file(self, path: str, offset: int, limit: int) -> str:
        full = self._resolve(path)
        if os.path.isdir(full):
            raise ToolError(f"{path} is a directory; use list_dir")
        if not os.path.exists(full):
            raise ToolError(f"file does not exist: {path}")
        data = Path(full).read_bytes()
        if _is_binary(data):
            return f"(binary file, {len(data)} bytes; not shown)"
        lines = data.decode("utf-8", errors="replace").splitlines()
        if not lines:
            return "(empty file)"
        offset, limit = max(offset, 0), max(limit, 1)
        if offset >= len(lines):
            raise ToolError(f"offset {offset} is past the end of the file ({len(lines)} lines)")
        out = []
        for n, line in enumerate(lines[offset:offset + limit], start=offset + 1):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " ... [line truncated]"
            out.append(f"{n:>6}\t{line}")
        rest = len(lines) - (offset + limit)
        if rest > 0:
            out.append(f"... [{rest} more lines; use offset={offset + limit} to continue]")
        return "\n".join(out)

    async def write_file(self, path: str, content: str) -> str:
        return await asyncio.to_thread(self._write_file, path, content)

    def _write_file(self, path: str, content: str) -> str:
        full = self._resolve(path)
        if os.path.isdir(full):
            raise ToolError(f"{path} is a directory")
        existed = os.path.exists(full)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        data = str(content).encode("utf-8")
        Path(full).write_bytes(data)
        return f"{'Overwrote' if existed else 'Created'} {self._rel(full)} ({len(data)} bytes)"

    async def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        return await asyncio.to_thread(self._edit_file, path, old_string, new_string, bool(replace_all))

    def _edit_file(self, path: str, old: str, new: str, replace_all: bool) -> str:
        full = self._resolve(path)
        if not os.path.isfile(full):
            raise ToolError(f"file does not exist: {path}")
        if old == "":
            raise ToolError("old_string must not be empty; use write_file to create a file")
        if old == new:
            raise ToolError("old_string and new_string are identical; nothing to change")
        text = Path(full).read_bytes().decode("utf-8", errors="surrogateescape")
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\r\n" not in old and "\n" in old:
            # Models write \n; the file uses CRLF. Match in the file's convention.
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = text.count(old)
        if count == 0:
            raise ToolError(f"old_string not found in {path}; it must match the file exactly, including whitespace")
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string occurs {count} times in {path}; add surrounding context to make it unique, "
                "or set replace_all=true to replace every occurrence")
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        Path(full).write_bytes(text.encode("utf-8", errors="surrogateescape"))
        n = count if replace_all else 1
        return f"Edited {self._rel(full)}: replaced {n} occurrence{'s' if n != 1 else ''}"

    async def list_dir(self, path: str = ".", depth: int = 1) -> str:
        return await asyncio.to_thread(self._list_dir, path, int(depth))

    def _list_dir(self, path: str, depth: int) -> str:
        full = self._resolve(path)
        if not os.path.isdir(full):
            raise ToolError(f"not a directory: {path}")
        depth = max(depth, 1)
        out: list[str] = []

        def walk(d: str, level: int) -> None:
            try:
                entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError as e:
                out.append("  " * level + f"[unreadable: {e.strerror}]")
                return
            for e in entries:
                if len(out) >= MAX_LIST_ENTRIES:
                    return
                is_dir = e.is_dir()
                out.append("  " * level + e.name + ("/" if is_dir else ""))
                if is_dir and level + 1 < depth and e.name not in IGNORED_DIRS:
                    walk(e.path, level + 1)

        walk(full, 0)
        if not out:
            return f"(empty directory: {self._rel(full)})"
        if len(out) >= MAX_LIST_ENTRIES:
            out.append(f"... [stopped after {MAX_LIST_ENTRIES} entries]")
        return "\n".join(out)

    async def glob(self, pattern: str) -> str:
        return await asyncio.to_thread(self._glob, pattern)

    def _glob(self, pattern: str) -> str:
        if not pattern or os.path.isabs(pattern) or pattern.startswith(("/", "\\")):
            raise ToolError("pattern must be relative to the working directory, e.g. '**/*.py'")
        if ".." in re.split(r"[\\/]", pattern):
            raise ToolError("pattern must not contain '..'")
        explicit = {p for p in re.split(r"[\\/]", pattern) if p in IGNORED_DIRS}
        hits: list[tuple[float, str]] = []
        for p in Path(self.root).glob(pattern):
            rel_parts = p.relative_to(self.root).parts
            if any(part in IGNORED_DIRS and part not in explicit for part in rel_parts):
                continue
            try:
                full = self._resolve(str(p))
                hits.append((os.path.getmtime(full), self._rel(full)))
            except (ToolError, OSError):
                continue
        if not hits:
            return f"No files match {pattern!r}"
        hits.sort(key=lambda h: h[0], reverse=True)
        out = [rel for _, rel in hits[:MAX_GLOB_RESULTS]]
        if len(hits) > MAX_GLOB_RESULTS:
            out.append(f"... [{len(hits) - MAX_GLOB_RESULTS} more matches]")
        return "\n".join(out)

    async def grep(self, pattern: str, path: str = ".", glob: str | None = None, max_results: int = 100) -> str:
        return await asyncio.to_thread(self._grep, pattern, path, glob, int(max_results))

    def _grep(self, pattern: str, path: str, glob: str | None, max_results: int) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"invalid regular expression: {e}")
        full = self._resolve(path)
        if not os.path.exists(full):
            raise ToolError(f"path does not exist: {path}")
        max_results = max(max_results, 1)

        def files():
            if os.path.isfile(full):
                yield full
                return
            for d, dirs, names in os.walk(full):
                dirs[:] = sorted(x for x in dirs if x not in IGNORED_DIRS)
                for name in sorted(names):
                    yield os.path.join(d, name)

        out: list[str] = []
        truncated = False
        for f in files():
            rel = self._rel(f)
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(os.path.basename(f), glob)):
                continue
            try:
                if os.path.getsize(f) > MAX_GREP_FILE_BYTES:
                    continue
                data = Path(f).read_bytes()
            except OSError:
                continue
            if _is_binary(data):
                continue
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                if rx.search(line):
                    if len(out) >= max_results:
                        truncated = True
                        break
                    if len(line) > MAX_LINE_CHARS:
                        line = line[:MAX_LINE_CHARS] + " ... [line truncated]"
                    out.append(f"{rel}:{n}: {line}")
            if truncated:
                break
        if not out:
            return f"No matches for {pattern!r}"
        if truncated:
            out.append(f"... [stopped at max_results={max_results}]")
        return "\n".join(out)

    async def bash(self, command: str, timeout_ms: int = 120000) -> str:
        """Run a shell command with cwd=root; stdout and stderr are merged.

        NOT jailed: the shell can reach anything the server process can. On
        Windows the shell is cmd.exe, elsewhere /bin/sh. On timeout the whole
        process group / tree is killed and the partial output is returned.
        """
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        timeout = min(max(int(timeout_ms), 1), MAX_BASH_TIMEOUT_MS) / 1000
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_shell(
            command, cwd=self.root, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs)
        chunks: list[bytes] = []

        async def pump() -> None:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(65536):
                chunks.append(chunk)

        reader = asyncio.create_task(pump())
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await _kill_tree(proc)
        except asyncio.CancelledError:
            await _kill_tree(proc)
            raise
        try:
            await asyncio.wait_for(reader, 2)
        except Exception:
            reader.cancel()
        text = b"".join(chunks).decode("utf-8", errors="replace").rstrip()
        if timed_out:
            tail = f"[timed out after {timeout_ms} ms; process killed]"
        else:
            tail = f"[exit code {proc.returncode}]"
        # Keep the status line visible even when the output itself is huge.
        budget = MAX_TOOL_RESULT_CHARS - 500
        if len(text) > budget:
            text = text[:budget // 2] + f"\n... [{len(text) - budget} chars omitted] ...\n" + text[-budget // 2:]
        return f"{text}\n{tail}" if text else tail

    # ------------------------------------------------------------------ specs

    def _build_specs(self) -> list[ToolSpec]:
        def obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
            return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

        path_desc = "Path relative to the working directory (absolute paths inside it are also accepted)."
        return [
            ToolSpec(
                "read_file",
                "Read a text file from the working directory. Returns lines prefixed with their 1-based "
                "line number and a tab (like `cat -n`). By default up to 2000 lines from the start; use "
                "offset/limit to page through long files. Lines longer than 2000 chars are truncated.",
                obj({
                    "path": {"type": "string", "description": path_desc},
                    "offset": {"type": "integer", "minimum": 0, "default": 0,
                               "description": "Number of lines to skip before reading (0 = start of file)."},
                    "limit": {"type": "integer", "minimum": 1, "default": 2000,
                              "description": "Maximum number of lines to return."},
                }, ["path"]),
                self.read_file),
            ToolSpec(
                "write_file",
                "Write a file in the working directory, creating parent directories as needed and "
                "overwriting any existing file. Prefer edit_file for changing existing files; read a "
                "file before overwriting it.",
                obj({
                    "path": {"type": "string", "description": path_desc},
                    "content": {"type": "string", "description": "The full content to write (UTF-8)."},
                }, ["path", "content"]),
                self.write_file),
            ToolSpec(
                "edit_file",
                "Perform an exact string replacement in a file. old_string must match the file exactly, "
                "including whitespace and indentation, and must be unique in the file unless replace_all "
                "is true; otherwise the edit fails. Include enough surrounding context to make it unique. "
                "Read the file first.",
                obj({
                    "path": {"type": "string", "description": path_desc},
                    "old_string": {"type": "string", "description": "The exact text to replace."},
                    "new_string": {"type": "string", "description": "The replacement text (must differ from old_string)."},
                    "replace_all": {"type": "boolean", "default": False,
                                    "description": "Replace every occurrence instead of requiring a unique match."},
                }, ["path", "old_string", "new_string"]),
                self.edit_file),
            ToolSpec(
                "list_dir",
                "List the entries of a directory in the working directory. Directories end with '/'. "
                "depth > 1 recurses (indented), skipping .git, node_modules, __pycache__ and virtualenvs.",
                obj({
                    "path": {"type": "string", "default": ".", "description": path_desc},
                    "depth": {"type": "integer", "minimum": 1, "default": 1,
                              "description": "How many directory levels to show."},
                }, []),
                self.list_dir),
            ToolSpec(
                "glob",
                "Find files by glob pattern relative to the working directory, e.g. '**/*.py' or "
                "'src/**/*.ts'. Returns relative paths sorted by modification time, newest first.",
                obj({
                    "pattern": {"type": "string", "description": "Glob pattern; '**' matches any number of directories."},
                }, ["pattern"]),
                self.glob),
            ToolSpec(
                "grep",
                "Search file contents with a Python regular expression. Returns matches as "
                "'path:line: text'. Binary files, files over 2 MB and .git/node_modules/virtualenvs "
                "are skipped.",
                obj({
                    "pattern": {"type": "string", "description": "Python `re` regular expression."},
                    "path": {"type": "string", "default": ".", "description": "File or directory to search. " + path_desc},
                    "glob": {"type": ["string", "null"], "default": None,
                             "description": "Only search files whose relative path or name matches this glob, e.g. '*.py'."},
                    "max_results": {"type": "integer", "minimum": 1, "default": 100,
                                    "description": "Stop after this many matching lines."},
                }, ["pattern"]),
                self.grep),
            ToolSpec(
                "bash",
                "Run a shell command in the working directory and return its combined stdout/stderr "
                "followed by the exit code. "
                + ("The shell is Windows cmd.exe, so use cmd syntax (dir, type, &&). "
                   if sys.platform == "win32" else "The shell is /bin/sh. ")
                + "Commands get no stdin. Default timeout 120000 ms, maximum 600000 ms; on timeout the "
                "process is killed. Prefer the file tools for reading, searching and editing files.",
                obj({
                    "command": {"type": "string", "description": "The command line to execute."},
                    "timeout_ms": {"type": "integer", "minimum": 1, "maximum": MAX_BASH_TIMEOUT_MS,
                                   "default": 120000, "description": "Timeout in milliseconds."},
                }, ["command"]),
                self.bash),
        ]


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a shell and everything it started (the shell alone would orphan children)."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        pass
