"""Node diffs: git snapshots of the declared files (diffs.py), and the engine taking them for
external claims as well as built-in runs."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from clearbox.diffs import Snapshots
from conftest import running, settle

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def write(root, path: str, text: str) -> None:
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def changed(snaps: Snapshots, key) -> dict[str, str]:
    return dict(snaps.finish(key))


def test_declared_files_any_change(tmp_path):
    wd = tmp_path / "wd"
    write(wd, "a.txt", "a\n")
    write(wd, "d/x.txt", "x\n")
    write(wd, "other.txt", "o\n")
    snaps = Snapshots(str(tmp_path / "snaps"))
    snaps.take_declared((1, 1), str(wd), ["a.txt", "d/", "new.txt", "never.txt"])

    write(wd, "a.txt", "a2\n")             # modified
    os.remove(wd / "d" / "x.txt")          # deleted
    write(wd, "d/y.txt", "y\n")            # added inside a declared directory
    write(wd, "new.txt", "n\n")            # created
    write(wd, "other.txt", "o2\n")         # not declared: out of scope

    diffs = changed(snaps, (1, 1))
    assert sorted(diffs) == ["a.txt", "d/x.txt", "d/y.txt", "new.txt"]
    assert "-a\n+a2" in diffs["a.txt"]
    assert "deleted file" in diffs["d/x.txt"]
    assert "new file" in diffs["new.txt"]
    assert (1, 1) not in snaps
    assert not [f for f in os.listdir(next((tmp_path / "snaps").iterdir())) if f.startswith("index")]


def test_gitignore_applies_inside_directories_only(tmp_path):
    wd = tmp_path / "wd"
    write(wd, ".gitignore", "*.log\n")
    write(wd, "d/keep.txt", "k\n")
    write(wd, "d/noise.log", "1\n")
    write(wd, "named.log", "1\n")
    snaps = Snapshots(str(tmp_path / "snaps"))
    snaps.take_declared((1, 1), str(wd), ["d", "named.log"])
    write(wd, "d/noise.log", "2\n")
    write(wd, "named.log", "2\n")          # declared by name: snapshotted although ignored
    write(wd, "d/node_modules/m.js", "x\n")  # Clearbox's own ignore list
    assert sorted(changed(snaps, (1, 1))) == ["named.log"]


def test_project_repository_is_untouched(tmp_path):
    wd = tmp_path / "wd"
    write(wd, "a.txt", "a\n")
    subprocess.run(["git", "init", "-q", str(wd)], check=True)
    before = sorted(os.listdir(wd / ".git"))
    snaps = Snapshots(str(tmp_path / "snaps"))
    snaps.take_declared((1, 1), str(wd), ["a.txt"])
    write(wd, "a.txt", "b\n")
    assert list(changed(snaps, (1, 1))) == ["a.txt"]
    assert sorted(os.listdir(wd / ".git")) == before  # no index, no objects written there


def test_no_declared_files_no_snapshot(tmp_path):
    snaps = Snapshots(str(tmp_path / "snaps"))
    snaps.take_declared((1, 1), str(tmp_path), [])
    assert (1, 1) not in snaps and snaps.finish((1, 1)) == []


async def test_external_claim_gets_a_diff(store, tmp_path):
    wd = tmp_path / "wd"
    write(wd, "out.txt", "old\n")
    pid = store.create_plan("p", cwd=str(wd))
    store.add_node(pid, "write", files=["out.txt"])
    async with running(store, None, tmp_path) as engine:
        engine.start_plan(pid, executor="external")
        await settle(engine)
        claim = engine.claim_next("tester", pid)
        assert claim["node_id"] == 1
        write(wd, "out.txt", "new\n")       # written by the external agent, from any tool
        engine.complete_external(pid, 1, "done")
    diffs = store.node_diffs(pid, 1)
    assert [d["path"] for d in diffs] == ["out.txt"]
    assert "-old\n+new" in diffs[0]["diff"]
