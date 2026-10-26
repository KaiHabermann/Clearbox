"""Per-step worktrees (worktrees.py): built-in steps of a plan with isolate_steps run in their own
git worktree, and their changes are applied back to the working directory when they succeed."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from typing import Any, AsyncIterator

import pytest

from clearbox.harness import AgentJob
from clearbox.store import PlanError
from conftest import running, status, until

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def write(root, path: str, text: str | bytes) -> None:
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if isinstance(text, bytes):
        with open(full, "wb") as f:
            f.write(text)
        return
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read(root, path: str) -> str:
    with open(os.path.join(root, path), encoding="utf-8", newline="") as f:
        return f.read()


def git(cwd, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          capture_output=True, check=True, text=True).stdout


def repo(tmp_path, files: dict[str, str]):
    wd = tmp_path / "wd"
    wd.mkdir()
    git(wd, "init", "-q")
    for p, t in files.items():
        write(wd, p, t)
    git(wd, "add", "-A")
    git(wd, "commit", "-q", "-m", "init")
    return wd


class EditHarness:
    """Each run applies ``edits[node_id]`` (path -> new text) in its job.cwd, then waits until the
    test calls ``finish``. ``seen`` keeps each run's job.cwd and what ``look`` found there."""

    def __init__(self, edits: dict[int, dict[str, str]], look: tuple[str, ...] = ()) -> None:
        self.edits, self.look = edits, look
        self.seen: dict[int, dict[str, Any]] = {}
        self._gates: dict[int, asyncio.Event] = {}
        self._ok: dict[int, bool] = {}

    def finish(self, node_id: int, ok: bool = True) -> None:
        self._ok[node_id] = ok
        self._gates.setdefault(node_id, asyncio.Event()).set()

    async def run(self, job: AgentJob) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.seen[job.node_id] = {"cwd": job.cwd, **{p: os.path.exists(os.path.join(job.cwd, p)) and read(job.cwd, p)
                                                     for p in self.look}}
        for p, t in self.edits.get(job.node_id, {}).items():
            write(job.cwd, p, t)
        yield "system", {"text": "edit session started"}
        await self._gates.setdefault(job.node_id, asyncio.Event()).wait()
        ok = self._ok.get(job.node_id, True)
        yield "result", {"text": "edited", "is_error": not ok, "errors": [] if ok else ["failed on purpose"]}


def worktrees_left(engine) -> list[str]:
    d = os.path.join(engine._snapshots.root, "worktrees")
    return os.listdir(d) if os.path.isdir(d) else []


async def test_parallel_steps_editing_different_files_both_apply(store, tmp_path):
    wd = repo(tmp_path, {"a.txt": "a\n", "b.txt": "b\n"})
    pid = store.create_plan("p", cwd=str(wd))
    store.add_node(pid, "one", files=["a.txt"])
    store.add_node(pid, "two", files=["b.txt", "img.bin"])
    blob = bytes(range(256)) * 4  # binary, applied byte for byte
    h = EditHarness({1: {"a.txt": "a2\n"}, 2: {"b.txt": "b2\n", "img.bin": blob}})
    async with running(store, h, tmp_path) as engine:
        engine.set_isolate_steps(pid, True)
        engine.start_plan(pid)
        await until(lambda: len(h.seen) == 2)
        assert read(wd, "a.txt") == "a\n"           # nothing lands before a step finishes
        h.finish(1)
        h.finish(2)
        await until(lambda: status(store, pid, 1) == status(store, pid, 2) == "done")
        await until(lambda: worktrees_left(engine) == [])  # removed once the run is over
    assert read(wd, "a.txt") == "a2\n" and read(wd, "b.txt") == "b2\n"
    assert (wd / "img.bin").read_bytes() == blob
    assert h.seen[1]["cwd"] != str(wd) and not os.path.exists(h.seen[1]["cwd"])
    for nid, paths in ((1, ["a.txt"]), (2, ["b.txt", "img.bin"])):
        assert [d["path"] for d in store.node_diffs(pid, nid)] == paths
        n = store.get_node(pid, nid)
        assert n["undeclared_files"] == [] and not n["undeclared_shared"]  # exact, despite running together
    assert git(wd, "status", "--porcelain").split() == ["M", "a.txt", "M", "b.txt", "??", "img.bin"]


async def test_parallel_steps_editing_different_parts_of_one_file_both_apply(store, tmp_path):
    def text(lines: list[str]) -> str:
        return "".join(x + "\n" for x in lines)

    lines = [f"line {i}" for i in range(12)]
    wd = repo(tmp_path, {"c.txt": text(lines)})
    pid = store.create_plan("p", cwd=str(wd))
    store.add_node(pid, "top")      # no declared files, so the two run at the same time
    store.add_node(pid, "bottom")
    h = EditHarness({1: {"c.txt": text(["TOP", *lines[1:]])}, 2: {"c.txt": text([*lines[:-1], "BOTTOM"])}})
    async with running(store, h, tmp_path) as engine:
        engine.set_isolate_steps(pid, True)
        engine.start_plan(pid)
        await until(lambda: len(h.seen) == 2)
        h.finish(1)
        await until(lambda: status(store, pid, 1) == "done")
        h.finish(2)
        await until(lambda: status(store, pid, 2) == "done")
    assert read(wd, "c.txt") == text(["TOP", *lines[1:-1], "BOTTOM"])


async def test_overlapping_edits_fail_the_later_step_and_keep_its_patch(store, tmp_path):
    wd = repo(tmp_path, {"c.txt": "one\ntwo\nthree\n"})
    pid = store.create_plan("p", cwd=str(wd))
    store.add_node(pid, "first")    # no declared files, so the two are not serialised
    store.add_node(pid, "second")
    h = EditHarness({1: {"c.txt": "one\nTWO\nthree\n"}, 2: {"c.txt": "one\nzwei\nthree\n"}})
    async with running(store, h, tmp_path) as engine:
        engine.set_isolate_steps(pid, True)
        engine.start_plan(pid)
        await until(lambda: len(h.seen) == 2)
        h.finish(1)
        await until(lambda: status(store, pid, 1) == "done")
        h.finish(2)
        await until(lambda: status(store, pid, 2) == "failed")
        await until(lambda: worktrees_left(engine) == [])  # removed once the run is over
    assert read(wd, "c.txt") == "one\nTWO\nthree\n"
    err = store.get_node(pid, 2)["error"]
    assert "c.txt" in err and "Nothing was applied" in err
    patch = err.split("patch is kept at ", 1)[1].split(" (", 1)[0]
    assert os.path.isfile(patch) and "+zwei" in open(patch, encoding="utf-8").read()
    assert store.get_node(pid, 2)["undeclared_files"] == ["c.txt"]  # its own change, from its worktree


async def test_a_failed_step_applies_nothing(store, tmp_path):
    wd = repo(tmp_path, {"a.txt": "a\n"})
    pid = store.create_plan("p", cwd=str(wd))
    store.add_node(pid, "one", files=["a.txt"])
    h = EditHarness({1: {"a.txt": "a2\n", "new.txt": "n\n"}})
    async with running(store, h, tmp_path) as engine:
        engine.set_isolate_steps(pid, True)
        engine.start_plan(pid)
        await until(lambda: len(h.seen) == 1)
        h.finish(1, ok=False)
        await until(lambda: status(store, pid, 1) == "failed")
        await until(lambda: worktrees_left(engine) == [])  # removed once the run is over
    assert read(wd, "a.txt") == "a\n" and not os.path.exists(wd / "new.txt")


async def test_worktree_holds_uncommitted_and_untracked_files(store, tmp_path):
    wd = repo(tmp_path, {"tracked.txt": "committed\n", ".gitignore": "*.log\n"})
    write(wd, "tracked.txt", "uncommitted\n")
    write(wd, "untracked.txt", "untracked\n")
    write(wd, "noise.log", "ignored\n")
    pid = store.create_plan("p", cwd=str(wd))
    store.add_node(pid, "look")
    h = EditHarness({}, look=("tracked.txt", "untracked.txt", "noise.log"))
    async with running(store, h, tmp_path) as engine:
        engine.set_isolate_steps(pid, True)
        engine.start_plan(pid)
        await until(lambda: 1 in h.seen)
        cwd = h.seen[1]["cwd"]
        assert git(cwd, "log", "--format=%s")[-5:] == "init\n"  # parented on the project's HEAD
        assert git(cwd, "status", "--porcelain") == ""
        h.finish(1)
        await until(lambda: status(store, pid, 1) == "done")
    assert h.seen[1] == {"cwd": cwd, "tracked.txt": "uncommitted\n", "untracked.txt": "untracked\n",
                         "noise.log": False}


async def test_the_setting_needs_a_git_repository_and_no_running_step(store, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    pid = store.create_plan("p", cwd=str(plain))
    store.add_node(pid, "one")
    wd = repo(tmp_path, {"a.txt": "a\n"})
    pid2 = store.create_plan("q", cwd=str(wd))
    store.add_node(pid2, "one")
    h = EditHarness({})
    async with running(store, h, tmp_path) as engine:
        with pytest.raises(PlanError, match="git repository"):
            engine.set_isolate_steps(pid, True)
        engine.start_plan(pid2)
        await until(lambda: 1 in h.seen)
        with pytest.raises(PlanError, match="running"):
            engine.set_isolate_steps(pid2, True)
        h.finish(1)
        await until(lambda: status(store, pid2, 1) == "done")
        engine.set_isolate_steps(pid2, True)
    assert not store.get_plan(pid)["isolate_steps"] and store.get_plan(pid2)["isolate_steps"]
