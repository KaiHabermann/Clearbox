"""Per-step worktrees: a built-in step of a plan with ``isolate_steps`` runs in its own git worktree.

The worktree is checked out from Clearbox's own shadow git dir of the working directory (diffs.py),
never from the project's repository, and lives under the snapshots directory, outside the project.
Its commit is the whole-tree snapshot taken when the step starts, so uncommitted and untracked
(non-ignored) files are in it too; ignored files (build output, virtualenvs) are not. That commit's
parent is the project's HEAD (the shadow dir borrows the project's objects), so ``git log`` and
``git diff HEAD`` inside the worktree make sense to an agent.

When the step succeeds, its changes -- the base tree against the worktree's end tree, binary-safe --
are applied to the real working directory with ``git apply``, which is all or nothing. If the real
tree changed in the same places meanwhile, nothing is applied and the step fails naming the files;
the patch stays on disk. The worktree is removed afterwards whatever happened.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .diffs import Snapshots

# commit-tree needs an identity; the commits only ever live in Clearbox's shadow git dir
_IDENTITY = {"GIT_AUTHOR_NAME": "Clearbox", "GIT_AUTHOR_EMAIL": "clearbox@localhost",
             "GIT_COMMITTER_NAME": "Clearbox", "GIT_COMMITTER_EMAIL": "clearbox@localhost"}


class WorktreeError(Exception):
    """A worktree could not be created, or a step's changes could not be applied back."""


@dataclass
class Worktree:
    path: str
    git_dir: str   # the shadow git dir it was added from
    base: str      # the tree it was checked out from: the working directory when the step started
    index: str     # its own index file


def in_git_repo(path: str) -> bool:
    """Whether ``path`` is inside the working tree of a git repository."""
    if shutil.which("git") is None:
        return False
    try:
        return _git("-C", path, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _git(*args: str, cwd: str | None = None, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(
        [shutil.which("git") or "git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *args],
        cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, timeout=600, check=True,
    )
    return out.stdout.decode("utf-8", errors="replace")


def _error(e: Exception) -> str:
    err = getattr(e, "stderr", None)
    return err.decode("utf-8", errors="replace").strip() if isinstance(err, bytes) and err else str(e)


def _project_head(git_dir: str, workdir: str) -> str | None:
    """The project's HEAD commit, made readable from the shadow git dir; None without a commit."""
    try:
        head = _git("-C", workdir, "rev-parse", "--verify", "-q", "HEAD").strip()
        common = _git("-C", workdir, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    objects = common.rstrip("/\\") + "/objects"
    alternates = os.path.join(git_dir, "objects", "info", "alternates")
    lines = []
    if os.path.exists(alternates):
        with open(alternates, encoding="utf-8") as f:
            lines = f.read().splitlines()
    if objects not in lines:
        with open(alternates, "a", encoding="utf-8", newline="") as f:  # git reads a CR as part of the path
            f.write(objects + "\n")
    return head


def create(snaps: Snapshots, key: tuple[int, int], workdir: str) -> Worktree:
    """A worktree holding the working directory as it is now, for the run ``key``. Blocking."""
    if snaps._git is None:
        raise WorktreeError("git is not on PATH, so the step cannot get its own worktree")
    try:
        git_dir = snaps._git_dir(workdir)
        base = snaps._whole_tree(key, git_dir, workdir)
        if not base:
            raise WorktreeError("could not snapshot the working directory for the step's worktree")
        parent = _project_head(git_dir, workdir)
        commit = _git(f"--git-dir={git_dir}", "commit-tree", base, *(["-p", parent] if parent else []),
                      "-m", f"Clearbox: working directory at the start of step {key[0]}.{key[1]}",
                      env=_IDENTITY).strip()
        path = os.path.join(snaps.root, "worktrees", f"{os.path.basename(git_dir)[:-4]}-{key[0]}-{key[1]}")
        _drop(git_dir, path)  # left behind by a run the server never finished
        _git(f"--git-dir={git_dir}", "worktree", "add", "--detach", "-f", path, commit)
        admin = _git("-C", path, "rev-parse", "--absolute-git-dir").strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise WorktreeError(f"could not create the step's worktree: {_error(e)}") from e
    return Worktree(path, git_dir, base, os.path.join(admin, "index"))


_FAILED = re.compile(r"^error: (?:patch failed: (.+):\d+|(.+?): (?:patch does not apply|already exists in "
                     r"working directory|No such file or directory|does not exist in index|wrong type))$")


def apply_back(snaps: Snapshots, wt: Worktree, workdir: str, key: tuple[int, int]) -> list[str]:
    """Apply what the step changed in its worktree to ``workdir``; return the changed paths.
    Raises WorktreeError, having applied nothing, if the changes do not apply cleanly. Blocking."""
    try:
        _git("-C", wt.path, "add", "-A")
        end = _git("-C", wt.path, "write-tree").strip()
        names = [n for n in _git(f"--git-dir={wt.git_dir}", "diff", "--name-only", "--no-renames", "-z",
                                 wt.base, end).split("\0") if n]
        if not names:
            return []
        patch = subprocess.run(
            [shutil.which("git") or "git", f"--git-dir={wt.git_dir}", "diff", "--binary", "--full-index",
             "--no-renames", "--no-color", "--no-ext-diff", wt.base, end],
            capture_output=True, timeout=600, check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise WorktreeError(f"could not read the step's changes from its worktree: {_error(e)}") from e
    patch_dir = os.path.join(snaps.root, "patches")
    os.makedirs(patch_dir, exist_ok=True)
    path = os.path.join(patch_dir, f"plan{key[0]}-node{key[1]}-{int(time.time())}.patch")
    with open(path, "wb") as f:
        f.write(patch)
    try:
        with snaps._tree_locks[wt.git_dir]:  # one writer of the real tree's snapshots at a time
            _git(f"--git-dir={wt.git_dir}", f"--work-tree={workdir}", "apply", "--whitespace=nowarn", path,
                 cwd=workdir)
    except (OSError, subprocess.SubprocessError) as e:
        msg = _error(e)
        failed = list(dict.fromkeys(m.group(1) or m.group(2) for m in map(_FAILED.match, msg.splitlines()) if m))
        raise WorktreeError(
            "the step's changes do not apply to the working directory, which changed in the same places "
            f"while the step ran: {', '.join(failed) or 'see below'}. Nothing was applied; the step's "
            f"patch is kept at {path} (apply it by hand with git apply once that is resolved).\n{msg}"
        ) from e
    os.remove(path)
    return names


def _drop(git_dir: str, path: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _git(f"--git-dir={git_dir}", "worktree", "remove", "--force", path)
    shutil.rmtree(path, ignore_errors=True)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _git(f"--git-dir={git_dir}", "worktree", "prune")


def remove(wt: Worktree) -> None:
    """Remove a step's worktree and prune it from the shadow git dir. Blocking."""
    _drop(wt.git_dir, wt.path)


def remove_all(root: str) -> None:
    """Remove every worktree under a snapshots directory: on startup, no step is running. Blocking."""
    if not os.path.isdir(os.path.join(root, "worktrees")) or shutil.which("git") is None:
        return
    shutil.rmtree(os.path.join(root, "worktrees"), ignore_errors=True)
    for name in os.listdir(root):
        if name.endswith(".git"):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                _git(f"--git-dir={os.path.join(root, name)}", "worktree", "prune")
