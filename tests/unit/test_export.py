"""The Markdown export of a plan (export.py): what a pasted PR description contains."""

from __future__ import annotations

from clearbox import export
from clearbox.export import fenced, plan_markdown

DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n-x\n+def greet():\n+    print('hello')\n"


def seed(store) -> int:
    """A plan with a done step (diff, review, undeclared change), a user step and a subplan."""
    p = store.create_plan("Add a greeting", "Say hello on startup.")
    _, a = store.add_node(p, "Write greet()", "Add greet() to app.py.", files=["app.py"])
    _, b = store.add_node(p, "Push the branch", kind="user")
    _, c = store.add_node(p, "Docs")
    store.add_edge(p, c, b)  # topological order differs from node order: 1, 3, 2
    store.add_edge(p, a, c)
    sub = store.create_plan("Docs", parent_plan_id=p, parent_node_id=c)
    _, d = store.add_node(sub, "README line")
    store.update_node(p, a, status="done", result="Added greet().\nDetails follow.")
    store.update_node(sub, d, status="done", result="One line in README.")
    store.set_node_diffs(p, a, [("app.py", DIFF)])
    store.set_node_undeclared(p, a, ["notes.txt"], shared=True)
    store.set_review(p, a, "concerns", "Works, one nit.", "- app.py: no docstring")
    return p


def test_plan_markdown(store):
    md = plan_markdown(store, seed(store))
    assert md.startswith("# Add a greeting\n\nSay hello on startup.\n")
    steps = md.split("## Steps")[1].split("## Details")[0]
    assert steps.strip().splitlines() == [
        "- ✅ **1** Write greet() — Added greet().",
        "- ⏳ **3** Docs",
        "  - ✅ **3.1** README line — One line in README.",
        "- ⏳ **2** Push the branch *(user step)*",
    ]
    details = md.split("## Details")[1]
    assert details.index("### ✅ 1. Write greet()") < details.index("#### ✅ 3.1. README line") \
        < details.index("### ⏳ 2. Push the branch")
    assert "**Result:** Added greet().\nDetails follow." in details
    assert "**Review: ⚠️ concerns** — Works, one nit.\n\n- app.py: no docstring" in details
    assert "> Changed outside the declared files (1): `notes.txt`" in details
    assert "another step ran at the same time" in details
    assert "*User step: done by a person, not an agent.*" in details
    assert "<details><summary>app.py (+2 −1)</summary>\n\n```diff\n" + DIFF + "```\n\n</details>" in details


def test_without_diffs(store):
    md = plan_markdown(store, seed(store), diffs=False)
    assert "<details>" not in md and "```" not in md
    assert "**Changed:** `app.py` (+2 −1)" in md


def test_subplan_alone(store):
    p = seed(store)
    md = plan_markdown(store, store.subplan_of(p, 3))
    assert md.startswith("# Docs\n") and "Write greet()" not in md
    assert "- ✅ **1** README line" in md


def test_fence_guard():
    assert fenced("a\n") == "```\na\n```"
    assert fenced("x = '```'\n", "diff") == "````diff\nx = '```'\n````"
    assert fenced("`````").startswith("``````\n")


def test_long_diffs_are_cut(store, monkeypatch):
    monkeypatch.setattr(export, "DIFF_LINES", 3)
    monkeypatch.setattr(export, "DIFF_BUDGET", 10)
    p = store.create_plan("Big")
    _, n = store.add_node(p, "Change two files")
    store.set_node_diffs(p, n, [("a.py", DIFF), ("b.py", DIFF)])
    md = plan_markdown(store, p)
    assert "*Cut after 3 of 6 lines.*" in md and "+def greet" not in md  # a.py: cut
    assert "<summary>b.py (+2 −1)</summary>" in md and md.count("```diff") == 1  # b.py: over budget
    assert "65,536-character limit" in md
