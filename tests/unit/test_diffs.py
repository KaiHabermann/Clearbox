"""Node diffs: git snapshots of the declared files and of the whole tree (diffs.py), and the engine
taking them for external claims as well as built-in runs."""

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
    return dict(snaps.finish(key).diffs)


def test_declared_files_any_change(tmp_path):
    wd = tmp_path / "wd"
    write(wd, "a.txt", "a\n")
    write(wd, "d/x.txt", "x\n")
    write(wd, "other.txt", "o\n")
    snaps = Snapshots(str(tmp_path / "snaps"))
    snaps.take((1, 1), str(wd), ["a.txt", "d/", "new.txt", "never.txt"])

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
    run_indexes = [f for f in os.listdir(next((tmp_path / "snaps").iterdir())) if f.startswith("index-")]
    assert run_indexes == ["index-all"]  # the run's own index is gone; the whole-tree one stays


def test_gitignore_applies_inside_directories_only(tmp_path):
    wd = tmp_path / "wd"
    write(wd, ".gitignore", "*.log\n")
    write(wd, "d/keep.txt", "k\n")
    write(wd, "d/noise.log", "1\n")
    write(wd, "named.log", "1\n")
    snaps = Snapshots(str(tmp_path / "snaps"))
    snaps.take((1, 1), str(wd), ["d", "named.log"])
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
    snaps.take((1, 1), str(wd), ["a.txt"])
    write(wd, "a.txt", "b\n")
    assert list(changed(snaps, (1, 1))) == ["a.txt"]
    assert sorted(os.listdir(wd / ".git")) == before  # no index, no objects written there


def test_no_declared_files_no_diff_but_every_change_undeclared(tmp_path):
    wd = tmp_path / "wd"
    write(wd, "a.txt", "a\n")
    snaps = Snapshots(str(tmp_path / "snaps"))
    snaps.take((1, 1), str(wd), [])
    write(wd, "a.txt", "b\n")
    write(wd, "d/new.txt", "n\n")
    done = snaps.finish((1, 1))
    assert done.diffs == [] and done.undeclared == ["a.txt", "d/new.txt"] and not done.shared


def test_undeclared_changes_are_listed_by_name(tmp_path):
    wd = tmp_path / "wd"
    write(wd, ".gitignore", "*.log\nbuild/\n")
    for f in ("src/a.py", "Docs/x.md", "other.txt", "keep.txt"):
        write(wd, f, "1\n")
    snaps = Snapshots(str(wd / "snaps"))  # inside the working tree, like a legacy data dir
    snaps.take((1, 1), str(wd), ["SRC/a.py", "docs\\"])  # compared casefolded, any slashes
    write(wd, "src/a.py", "2\n")          # declared file
    write(wd, "Docs/x.md", "2\n")         # inside a declared directory
    write(wd, "Docs/sub/y.md", "2\n")
    write(wd, "other.txt", "2\n")         # undeclared: listed
    write(wd, "src/b.py", "2\n")          # undeclared next to a declared file: listed
    write(wd, "noise.log", "2\n")         # gitignored
    write(wd, "build/out.bin", "2\n")
    write(wd, "node_modules/m.js", "2\n")  # Clearbox's own ignore list
    done = snaps.finish((1, 1))
    assert done.undeclared == ["other.txt", "src/b.py"]


def test_overlapping_runs_are_flagged_shared(tmp_path):
    wd = tmp_path / "wd"
    write(wd, "a.txt", "a\n")
    snaps = Snapshots(str(tmp_path / "snaps"))
    snaps.take((1, 1), str(wd), ["a.txt"], group=1)
    snaps.take((2, 1), str(wd), [], group=2)    # another plan tree: not flagged
    write(wd, "b.txt", "b\n")
    assert snaps.finish((2, 1)).shared is False
    snaps.take((1, 2), str(wd), [], group=1)    # starts while (1, 1) runs
    assert snaps.finish((1, 1)).shared is True
    assert snaps.finish((1, 2)).shared is True
    snaps.take((1, 3), str(wd), [], group=1)    # alone again
    write(wd, "c.txt", "c\n")
    done = snaps.finish((1, 3))
    assert done.undeclared == ["c.txt"] and done.shared is False


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
        write(wd, "stray.txt", "s\n")       # outside its declared files
        engine.complete_external(pid, 1, "done")
    diffs = store.node_diffs(pid, 1)
    assert [d["path"] for d in diffs] == ["out.txt"]
    assert "-old\n+new" in diffs[0]["diff"]
    n = store.get_node(pid, 1)
    assert n["undeclared_files"] == ["stray.txt"] and not n["undeclared_shared"]
    store.clear_node_diffs(pid, 1)            # what every rerun does first
    assert store.get_node(pid, 1)["undeclared_files"] == []


async def test_parallel_external_claims_are_flagged_shared(store, tmp_path):
    wd = tmp_path / "wd"
    write(wd, "a.txt", "a\n")
    pid = store.create_plan("p", cwd=str(wd))
    store.add_node(pid, "one")
    store.add_node(pid, "two")
    async with running(store, None, tmp_path) as engine:
        engine.start_plan(pid, executor="external")
        await settle(engine)
        assert engine.claim_next("a", pid)["node_id"] == 1
        assert engine.claim_next("b", pid)["node_id"] == 2
        write(wd, "a.txt", "b\n")
        engine.complete_external(pid, 1, "done")
        engine.complete_external(pid, 2, "done")
    for nid in (1, 2):
        n = store.get_node(pid, nid)
        assert n["undeclared_files"] == ["a.txt"] and n["undeclared_shared"]
