"""Node diffs: git snapshots of a node's declared files, taken when it starts and when it ends.

Each snapshot stages the declared paths into a throwaway index and writes them as a git tree; the
node's diff is ``git diff <start tree> <end tree>``. That sees every change to those paths however
it was made (file tool, bash, sed, a code generator), by builtin and external agents alike, and
the start snapshot is complete before the agent can touch anything. Files the node did not
declare get no diff: a node is trusted to stay inside its own files (but see below).

The git directory is Clearbox's own, one per working directory under its data dir, never the
project's ``.git``: no branch, commit, ref or index of the project is touched, and the working
directory does not even have to be a repository. The project's ``.gitignore`` files still apply
inside a declared directory; a file declared by name is snapshotted even if it is ignored.

Every run, with or without declared files, also gets a cheap safety net for what it should not
have touched: the whole working tree is written as a tree object at start and end, through one
persistent index per working directory (so ``git add -A`` only rehashes files whose stat changed),
and the changed paths no declared path covers are reported by name only, never as a diff. The
project's ``.gitignore`` and Clearbox's own ignore list apply. A run that overlapped another run of
the same plan tree is flagged as shared, since some of those changes may be the other run's.

The git work runs in a worker thread (``capture`` / ``compare``), so a big tree's first snapshot
does not stall the server; only ``register`` / ``pop`` touch the runs in flight, on the event loop.
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
from dataclasses import dataclass, field

from .harness.tools import IGNORED_DIRS
from .store import normalize_file

log = logging.getLogger("clearbox")

Key = tuple[int, int]  # (plan_id, node_id)


@dataclass
class _Run:
    git_dir: str
    index: str
    workdir: str
    paths: list[str]
    tree: str          # the declared paths at start; "" when none were declared (or git failed)
    whole: str = ""    # the whole working tree at start; "" when that snapshot failed
    group: int = 0     # overlapping runs of one group (plan tree) are flagged shared
    shared: bool = False


@dataclass
class Finished:
    """What a run changed: (path, git diff) per declared file, and the names of the changed paths
    outside the declared ones (None = not known: no git, or a snapshot failed)."""
    diffs: list[tuple[str, str]] = field(default_factory=list)
    undeclared: list[str] | None = None
    shared: bool = False  # another run of the same group was in flight at some point


def _clean(path: str) -> str:
    """A declared path as git wants it: forward slashes, no ``./`` segments, no trailing slash."""
    return "/".join(seg for seg in path.replace("\\", "/").split("/") if seg not in ("", "."))


class Snapshots:
    """The start snapshot of every node run in flight, keyed by (plan_id, node_id)."""

    def __init__(self, root: str) -> None:
        self.root = root  # holds one bare git dir per working directory
        self._runs: dict[Key, _Run] = {}
        self._init_lock = threading.Lock()
        # the persistent whole-tree index of each git dir is shared by every run in that working
        # directory, and runs start and finish concurrently: one lock per git dir
        self._tree_locks: dict[str, threading.Lock] = {}
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
            if git_dir not in self._tree_locks:
                self._tree_locks[git_dir] = threading.Lock()
                self._exclude_root(git_dir, workdir)
        return git_dir

    def _exclude_root(self, git_dir: str, workdir: str) -> None:
        """Keep these snapshots out of the whole-tree snapshot when they live inside the working
        directory (an old database next to the project puts them there)."""
        try:
            rel = os.path.relpath(os.path.abspath(self.root), os.path.abspath(workdir))
        except ValueError:  # another drive
            return
        if rel == "." or rel.split(os.sep)[0] == "..":
            return
        line = "/" + rel.replace(os.sep, "/") + "/\n"
        path = os.path.join(git_dir, "info", "exclude")
        with open(path, encoding="utf-8") as f:
            if line in f.read().splitlines(keepends=True):
                return
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def _whole_tree(self, key: Key, git_dir: str, workdir: str) -> str:
        """The whole working tree as a tree object, or "" (logged) if git failed."""
        whole = _Run(git_dir, os.path.join(git_dir, "index-all"), workdir, [], "")
        try:
            with self._tree_locks[git_dir]:
                self._run_git(whole, "add", "-A")
                return self._run_git(whole, "write-tree").strip()
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("node %s: no working-tree snapshot, so no undeclared-change check: %s",
                        key, _git_error(e))
            return ""

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

    def take(self, key: Key, workdir: str, paths: list[str], group: int = 0) -> None:
        """``capture`` and ``register`` in one call (the engine runs ``capture`` in a thread)."""
        self.discard(key)
        run = self.capture(key, workdir, paths, group)
        if run is not None:
            self.register(key, run)

    def capture(self, key: Key, workdir: str, paths: list[str], group: int = 0) -> _Run | None:
        """Take a run's start snapshots before the agent does anything: its declared paths (a run
        without declared files gets none, and so no diff) and the whole working tree. Only git
        work, no shared state: safe in a worker thread. None if nothing could be snapshotted."""
        paths = sorted({_clean(p) for p in paths} - {""})
        if self._git is None:
            return None
        try:
            git_dir = self._git_dir(workdir)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("node %s: no snapshot git dir, so no diff: %s", key, _git_error(e))
            return None
        run = _Run(git_dir, os.path.join(git_dir, f"index-{key[0]}-{key[1]}"), workdir, paths, "", group=group)
        with contextlib.suppress(OSError):
            os.remove(run.index)  # left over by a capture whose run was abandoned before register
        if paths:
            try:
                run.tree = self._snapshot(run)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("node %s: no start snapshot, so no diff: %s", key, _git_error(e))
        run.whole = self._whole_tree(key, git_dir, workdir)
        if not run.tree and not run.whole:
            return None
        return run

    def register(self, key: Key, run: _Run) -> None:
        """Make a captured run the one in flight for ``key`` (on the event loop, like ``pop``)."""
        self._runs.pop(key, None)  # a stale run of the key had the same index file: keep it
        for other in self._runs.values():
            if other.group == run.group:
                other.shared = run.shared = True
        self._runs[key] = run

    def pop(self, key: Key) -> _Run | None:
        """Take a run out of flight, for ``compare``."""
        return self._runs.pop(key, None)

    def discard(self, key: Key) -> None:
        run = self._runs.pop(key, None)
        if run is not None:
            with contextlib.suppress(OSError):
                os.remove(run.index)

    def finish(self, key: Key) -> Finished:
        """``pop`` and ``compare`` in one call (the engine runs ``compare`` in a thread)."""
        run = self.pop(key)
        return Finished() if run is None else self.compare(key, run)

    def compare(self, key: Key, run: _Run) -> Finished:
        """What changed since a popped run's snapshots were taken: the diff of every declared file
        modified, created, deleted, or added inside a declared directory, and the names of the
        changed paths outside the declared ones. Removes the run's index; safe in a thread."""
        out = Finished()
        try:
            if run.tree:
                end = self._snapshot(run)
                names = [n for n in self._run_git(run, "diff", "--name-only", "-z", run.tree, end).split("\0")
                         if n]
                out.diffs = [(n, self._run_git(run, "diff", "--no-color", "--no-ext-diff", run.tree, end, "--", n))
                             for n in names]
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("node %s: diff failed: %s", key, _git_error(e))
        try:
            end = self._whole_tree(key, run.git_dir, run.workdir) if run.whole else ""
            if end:
                names = self._run_git(run, "diff", "--name-only", "--no-renames", "-z", run.whole, end).split("\0")
                declared = [normalize_file(p) for p in run.paths]
                out.undeclared = [n for n in names if n and not any(
                    n.casefold() == d or n.casefold().startswith(d + "/") for d in declared)]
                out.shared = run.shared
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("node %s: undeclared-change check failed: %s", key, _git_error(e))
        finally:
            with contextlib.suppress(OSError):
                os.remove(run.index)
        return out


def _git_error(e: Exception) -> str:
    err = getattr(e, "stderr", None)
    return err.decode("utf-8", errors="replace").strip() if isinstance(err, bytes) and err else str(e)

