"""A plan as Markdown, for a pull/merge request description or to send to a colleague.

The plan's title and goal, a compact list of its steps in dependency order, then each step in
detail: description, result, review, the files it changed outside its declared ones, and its diff
per file in a collapsed ``<details>`` block. Subplans are nested under their step. Read-only: it
only reads the store.

GitHub caps a pull request description at 65,536 characters, so a long diff is cut after
DIFF_LINES lines, and once DIFF_BUDGET characters of diff are in the export the remaining files get
only their name and line counts. ``diffs=False`` leaves the diffs out altogether.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .store import Store

DIFF_LINES = 400          # per file; the rest is cut with a note
DIFF_BUDGET = 50_000      # characters of diff text in one export, leaving room for the prose
UNDECLARED_SHOWN = 20     # names listed per step; the rest are counted

GLYPHS = {"done": "✅", "failed": "❌", "running": "🔄", "ready": "⏳", "pending": "⏳"}
VERDICTS = {"ok": "✅ ok", "concerns": "⚠️ concerns", "error": "❌ error"}


def diff_stat(diff: str) -> tuple[int, int]:
    """Added and deleted lines of a unified diff, headers not counted."""
    add = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    dele = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return add, dele


def fenced(text: str, lang: str = "") -> str:
    """A fenced code block that the text cannot close early: the fence is one backtick longer
    than the longest backtick run inside it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text.rstrip(chr(10))}\n{fence}"


def _one_line(text: str | None, limit: int = 120) -> str:
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _ordered(store: Store, plan_id: int) -> list[dict[str, Any]]:
    """The plan's nodes in topological order, lowest node id first among those ready."""
    nodes = {n["node_id"]: n for n in store.nodes(plan_id)}
    preds: dict[int, set[int]] = {i: set() for i in nodes}
    for a, b in store.edges(plan_id):
        preds[b].add(a)
    out: list[dict[str, Any]] = []
    while preds:
        nid = min((i for i, p in preds.items() if not p), default=min(preds))  # no cycles, but never hang
        out.append(nodes[nid])
        del preds[nid]
        for p in preds.values():
            p.discard(nid)
    return out


class _Export:
    def __init__(self, store: Store, diffs: bool) -> None:
        self.store = store
        self.diffs = diffs
        self.budget = DIFF_BUDGET

    def steps(self, plan_id: int, prefix: str) -> list[tuple[str, dict[str, Any], int | None]]:
        """(number, node, subplan id) of every step of the plan."""
        return [(f"{prefix}{n['node_id']}", n, self.store.subplan_of(plan_id, n["node_id"]))
                for n in _ordered(self.store, plan_id)]

    def step_list(self, plan_id: int, prefix: str = "", indent: str = "") -> list[str]:
        out = []
        for num, n, sub in self.steps(plan_id, prefix):
            user = " *(user step)*" if n.get("kind") == "user" else ""
            result = _one_line(n["result"] or n["error"])
            out.append(f"{indent}- {GLYPHS.get(n['status'], '⏳')} **{num}** {n['title']}{user}"
                       + (f" — {result}" if result else ""))
            if sub is not None:
                out += self.step_list(sub, f"{num}.", indent + "  ")
        return out

    def details(self, plan_id: int, prefix: str = "", level: int = 3) -> list[str]:
        out = []
        for num, n, sub in self.steps(plan_id, prefix):
            out += [f"{'#' * min(level, 6)} {GLYPHS.get(n['status'], '⏳')} {num}. {n['title']}", ""]
            if n.get("kind") == "user":
                out += ["*User step: done by a person, not an agent.*", ""]
            if n["description"].strip():
                out += [n["description"].strip(), ""]
            if n["result"]:
                out += [f"**Result:** {n['result'].strip()}", ""]
            if n["error"]:
                out += [f"**Error:** {n['error'].strip()}", ""]
            out += self.review(self.store.review(plan_id, n["node_id"]))
            out += self.undeclared(n)
            out += self.file_diffs(plan_id, n["node_id"])
            if sub is not None:
                out += [f"Subplan: {self.store.get_plan(sub)['title']}", ""]
                out += self.details(sub, f"{num}.", level + 1)
        return out

    def review(self, r: dict[str, Any] | None, label: str = "Review") -> list[str]:
        if r is None:
            return []
        out = [f"**{label}: {VERDICTS.get(r['verdict'], r['verdict'])}**"
               + (f" — {r['summary'].strip()}" if r["summary"].strip() else ""), ""]
        if r["findings"]:
            out += [r["findings"].strip(), ""]
        return out

    def undeclared(self, n: dict[str, Any]) -> list[str]:
        files = n["undeclared_files"]
        if not files:
            return []
        total = len(files) + n["undeclared_more"]
        names = ", ".join(f"`{f}`" for f in files[:UNDECLARED_SHOWN])
        more = total - min(len(files), UNDECLARED_SHOWN)
        out = ["> [!WARNING]",
               f"> Changed outside the declared files ({total}): {names}" + (f" and {more} more" if more else ""),
               "> These changes are not diffed."
               + (" Some may be other steps': another step ran at the same time." if n["undeclared_shared"] else ""),
               ""]
        return out

    def file_diffs(self, plan_id: int, node_id: int) -> list[str]:
        rows = self.store.node_diffs(plan_id, node_id)
        if not rows:
            return []
        if not self.diffs:
            stats = ", ".join("`{}` (+{} −{})".format(d["path"], *diff_stat(d["diff"])) for d in rows)
            return [f"**Changed:** {stats}", ""]
        out = []
        for d in rows:
            add, dele = diff_stat(d["diff"])
            summary = f"<summary>{html.escape(d['path'])} (+{add} −{dele})</summary>"
            lines = d["diff"].splitlines()
            if self.budget <= 0:
                out += [f"<details>{summary}", "", "*Diff left out to keep the export under GitHub's "
                        "65,536-character limit; see it in Clearbox.*", "", "</details>", ""]
                continue
            note = ""
            if len(lines) > DIFF_LINES:
                note = f"*Cut after {DIFF_LINES} of {len(lines)} lines.*"
                lines = lines[:DIFF_LINES]
            text = "\n".join(lines)
            self.budget -= len(text)
            out += [f"<details>{summary}", "", fenced(text, "diff"), ""]
            out += [note, ""] if note else []
            out += ["</details>", ""]
        return out


def plan_markdown(store: Store, plan_id: int, diffs: bool = True) -> str:
    """The plan ``plan_id`` (top-level or a subplan) and the subplans below it as Markdown."""
    p = store.get_plan(plan_id)
    ex = _Export(store, diffs)
    out = [f"# {p['title']}", ""]
    if p["description"].strip():
        out += [p["description"].strip(), ""]
    out += ["## Steps", "", *ex.step_list(plan_id), ""]
    out += ex.review(store.review(plan_id, None), "Plan review")
    out += ["## Details", "", *ex.details(plan_id)]
    return "\n".join(out).rstrip() + "\n"
