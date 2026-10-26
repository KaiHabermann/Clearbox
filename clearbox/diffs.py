"""Node diffs: git snapshots of a node's declared files, taken when it starts and when it ends.

Each snapshot stages the declared paths into a throwaway index and writes them as a git tree; the
node's diff is ``git diff <start tree> <end tree>``. That sees every change to those paths however
it was made (file tool, bash, sed, a code generator), by builtin and external agents alike, and
the start snapshot is complete before the agent can touch anything. Files the node did not
declare are out of scope: a node is trusted to stay inside its own files.

The git directory is Clearbox's own, one per working directory under its data dir, never the
project's ``.git``: no branch, commit, ref or index of the project is touched, and the working
directory does not even have to be a repository. The project's ``.gitignore`` files still apply
inside a declared directory; a file declared by name is snapshotted even if it is ignored.

In memory only, like the engine's running tasks: a restart loses in-flight start snapshots the
same way it re-queues the node.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass

from .harness.tools import IGNORED_DIRS

log = logging.getLogger("clearbox")

Key = tuple[int, int]  # (plan_id, node_id)


@dataclass
class _Run:
    git_dir: str
    index: str
    workdir: str
    paths: list[str]
    tree: str


def _clean(path: str) -> str:
    """A declared path as git wants it: forward slashes, no ``./`` segments, no trailing slash."""
    return "/".join(seg for seg in path.replace("\\", "/").split("/") if seg not in ("", "."))


class Snapshots:
    """The start snapshot of every node run in flight, keyed by (plan_id, node_id)."""

    def __init__(self, root: str) -> None:
        self.root = root  # holds one bare git dir per working directory
        self._runs: dict[Key, _Run] = {}
        self._init_lock = threading.Lock()
        self._git = shutil.which("git")
        if self._git is None:
            log.warning("git is not on PATH: node diffs are disabled")

    def __contains__(self, key: Key) -> bool:
        return key in self._runs

    def _run_git(self, run: _Run, *args: str) -> str:
        env = {**os.environ, "GIT_INDEX_FILE": run.index}
        out = subprocess.run(
            [self._git, f"--git-dir={run.git_dir}", f"--work-tree={run.workdir}", "--literal-pathspecs",
             "-c", "core.autocrlf=false", "-c", "core.quotepath=false", "-c", "advice.addIgnoredFile=false",
             *args],
            env=env, capture_output=True, timeout=120, check=True,
        )
        return out.stdout.decode("utf-8", errors="replace")

    def _git_dir(self, workdir: str) -> str:
        name = hashlib.sha1(os.path.normcase(os.path.abspath(workdir)).encode()).hexdigest()[:16]
        git_dir = os.path.join(self.root, name + ".git")
        with self._init_lock:
            if not os.path.isdir(git_dir):
                os.makedirs(self.root, exist_ok=True)
                subprocess.run([self._git, "init", "-q", "--bare", git_dir], capture_output=True,
                               timeout=60, check=True)
                with open(os.path.join(git_dir, "info", "exclude"), "a", encoding="utf-8") as f:
                    f.write("".join(f"{d}/\n" for d in sorted(IGNORED_DIRS)))
        return git_dir

    def _snapshot(self, run: _Run) -> str:
        """Stage the declared paths into the run's index and return the tree they make.

        git refuses a pathspec that matches nothing, so a path is passed only while it exists on
        disk or is still in the index (a file deleted since the start snapshot). Files declared by
        name are added with -f so a .gitignore cannot hide them; directories keep the ignores.
        """
        indexed = set(self._run_git(run, "ls-files", "-z", "--", *run.paths).split("\0")) if run.paths else set()

        def present(p: str) -> bool:
            return os.path.lexists(os.path.join(run.workdir, p)) or p in indexed or any(
                f.startswith(p + "/") for f in indexed)

        live = [p for p in run.paths if present(p)]
        files = [p for p in live if not os.path.isdir(os.path.join(run.workdir, p))]
        dirs = [p for p in live if p not in files]
        if files:
            self._run_git(run, "add", "-A", "-f", "--", *files)
        if dirs:
            self._run_git(run, "add", "-A", "--", *dirs)
        return self._run_git(run, "write-tree").strip()

    def take_declared(self, key: Key, workdir: str, paths: list[str]) -> None:
        """Take the start snapshot of a run's declared paths, before the agent does anything.
        A run without declared files gets none, and so no diff."""
        self.discard(key)
        paths = sorted({_clean(p) for p in paths} - {""})
        if self._git is None or not paths:
            return
        try:
            git_dir = self._git_dir(workdir)
            run = _Run(git_dir, os.path.join(git_dir, f"index-{key[0]}-{key[1]}"), workdir, paths, "")
            run.tree = self._snapshot(run)
            self._runs[key] = run
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("node %s: no start snapshot, so no diff: %s", key, _git_error(e))

    def discard(self, key: Key) -> None:
        run = self._runs.pop(key, None)
        if run is not None:
            with contextlib.suppress(OSError):
                os.remove(run.index)

    def finish(self, key: Key) -> list[tuple[str, str]]:
        """Drop a run's snapshot and return (path, git diff) for every declared file that changed
        since it was taken: modified, created, deleted, or added inside a declared directory."""
        run = self._runs.get(key)
        if run is None:
            return []
        try:
            end = self._snapshot(run)
            names = [n for n in self._run_git(run, "diff", "--name-only", "-z", run.tree, end).split("\0") if n]
            return [(n, self._run_git(run, "diff", "--no-color", "--no-ext-diff", run.tree, end, "--", n))
                    for n in names]
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("node %s: diff failed: %s", key, _git_error(e))
            return []
        finally:
            self.discard(key)


def _git_error(e: Exception) -> str:
    err = getattr(e, "stderr", None)
    return err.decode("utf-8", errors="replace").strip() if isinstance(err, bytes) and err else str(e)

