"""Node diffs: before-texts of the files a running node may change, diffed when it finishes.

Two sources, unioned: the node's declared files are snapshotted when it is spawned, before the
agent does anything, so any change to them shows up however it was made (file tool, bash, sed);
file-tool writes (Write, Edit, MultiEdit, NotebookEdit) are snapshotted as they happen, so nodes
without declared files, and writes outside the declared scope, still get diffs. A shell-out write
outside the declared scope stays invisible -- catching it would mean snapshotting the whole tree
before every node.

In memory only, like the engine's running tasks: a restart loses in-flight diffs the same way it
re-queues the node.
"""

from __future__ import annotations

import difflib
import logging
import os
from typing import Any

from .harness.tools import IGNORED_DIRS

log = logging.getLogger("clearbox")

FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# one declared directory snapshots at most this many files; past it the directory is only
# diffed for the files already taken, and not rescanned for added ones (they would all look new)
MAX_DIR_SNAPSHOT_FILES = 2000

Key = tuple[int, int]  # (plan_id, node_id)


def file_tool_path(data: dict[str, Any]) -> str | None:
    """The path a ``tool_use`` event writes to, if it is one of the FILE_TOOLS."""
    if data.get("name") not in FILE_TOOLS:
        return None
    inp = data.get("input")
    return (inp.get("file_path") if isinstance(inp, dict) else None) or None


def _full_path(workdir: str, path: str) -> str:
    full = path if os.path.isabs(path) else os.path.join(workdir, path)
    return os.path.normcase(os.path.abspath(full))


def _read_text(workdir: str, path: str) -> str | None:
    """The current text of a file the node may have written, or None if it can't be read as text."""
    full = path if os.path.isabs(path) else os.path.join(workdir, path)
    try:
        with open(full, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _walk_dir(full_dir: str, display: str) -> tuple[list[str], bool]:
    """Display paths of the files below a directory (skipping IGNORED_DIRS), and whether
    the walk hit MAX_DIR_SNAPSHOT_FILES."""
    out: list[str] = []
    for d, dirs, names in os.walk(full_dir):
        dirs[:] = sorted(x for x in dirs if x not in IGNORED_DIRS)
        rel = os.path.relpath(d, full_dir)
        for name in sorted(names):
            if len(out) >= MAX_DIR_SNAPSHOT_FILES:
                return out, True
            sub = name if rel == "." else os.path.join(rel, name)
            out.append(os.path.join(display, sub).replace("\\", "/"))
    return out, False


class Snapshots:
    """The before-texts of every node run in flight, keyed by (plan_id, node_id)."""

    def __init__(self) -> None:
        # key -> {normalised full path: (display path, text-before-or-None)}, taken for the
        # declared files at spawn and from tool_use events while the node runs; diffed against
        # the current text when it finishes. _dirs holds the declared directories (full path ->
        # display path) rescanned at finish for added files.
        self._files: dict[Key, dict[str, tuple[str, str | None]]] = {}
        self._dirs: dict[Key, dict[str, str]] = {}

    def __contains__(self, key: Key) -> bool:
        return key in self._files or key in self._dirs

    def take(self, key: Key, workdir: str, path: str) -> None:
        """Take a file's before-text, unless this run already has it (the first snapshot wins)."""
        snaps = self._files.setdefault(key, {})
        full = _full_path(workdir, path)
        if full not in snaps:
            snaps[full] = (path, _read_text(workdir, path))

    def take_declared(self, key: Key, workdir: str, paths: list[str]) -> None:
        """Baseline every declared path before the run starts. A missing file snapshots as None
        (a creation); a directory snapshots each file below it and is rescanned at finish."""
        self._files[key] = {}
        self._dirs[key] = {}
        for path in paths:
            full = _full_path(workdir, path)
            if not os.path.isdir(full):
                self.take(key, workdir, path)
                continue
            files, capped = _walk_dir(full, path.rstrip("/\\"))
            for p in files:
                self.take(key, workdir, p)
            if capped:
                log.warning("node %s: declared directory %s has over %d files; added files there "
                            "will not be diffed", key, path, MAX_DIR_SNAPSHOT_FILES)
            else:
                self._dirs[key][full] = path.rstrip("/\\")

    def discard(self, key: Key) -> None:
        self._files.pop(key, None)
        self._dirs.pop(key, None)

    def finish(self, key: Key, workdir: str) -> list[tuple[str, str]]:
        """Drop a run's snapshots and return (path, unified diff) for every changed file:
        modified, created (before None), deleted (gone now), and files added inside a declared
        directory."""
        snaps = self._files.pop(key, None) or {}
        for full_dir, display in self._dirs.pop(key, {}).items():
            for p in _walk_dir(full_dir, display)[0]:
                full = _full_path(workdir, p)
                if full not in snaps:
                    snaps[full] = (p, None)
        diffs = []
        for full, (path, before) in snaps.items():
            after = _read_text(workdir, path)
            if after is None:
                if before is None or os.path.exists(full):
                    continue  # never text, or no longer readable as text: nothing to show
                after = ""  # existed, now gone: a deletion
            if after == before:
                continue
            diff = "".join(difflib.unified_diff(
                (before or "").splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=path, tofile=path,
            ))
            if diff:
                diffs.append((path, diff))
        return diffs
