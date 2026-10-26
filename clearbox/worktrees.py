"""Per-step worktrees: a built-in step of a plan with ``isolate_steps`` runs in its own git worktree.

Only allowed when the plan's working directory is inside a git repository (``in_git_repo``).
"""

from __future__ import annotations

import shutil
import subprocess


def in_git_repo(path: str) -> bool:
    """Whether ``path`` is inside the working tree of a git repository."""
    git = shutil.which("git")
    if git is None:
        return False
    try:
        out = subprocess.run([git, "-C", path, "rev-parse", "--is-inside-work-tree"],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == b"true"
