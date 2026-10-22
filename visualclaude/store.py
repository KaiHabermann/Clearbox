"""SQLite persistence for plans, nodes, edges and agent events.

Plan ids are plain auto-incrementing integers. Node ids count up per plan
(plan 1 has nodes 1, 2, 3 ...; plan 2 starts again at 1), so a node is
addressed by the tuple (plan_id, node_id).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    parent_plan_id  INTEGER,
    parent_node_id  INTEGER,
    executor        TEXT,              -- 'builtin' | 'external' (only meaningful on root plans)
    started         INTEGER NOT NULL DEFAULT 0,
    cwd             TEXT,
    next_node_id    INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS plans_parent ON plans(parent_plan_id, parent_node_id)
    WHERE parent_plan_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS nodes (
    plan_id      INTEGER NOT NULL,
    node_id      INTEGER NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|ready|running|done|failed
    agent_kind   TEXT,             -- 'builtin' | 'external' | NULL (container / not started)
    agent_name   TEXT,
    session_id   TEXT,
    agent_done   INTEGER NOT NULL DEFAULT 0,
    result       TEXT,
    error        TEXT,
    started_at   REAL,
    finished_at  REAL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (plan_id, node_id)
);

CREATE TABLE IF NOT EXISTS edges (
    plan_id  INTEGER NOT NULL,
    src      INTEGER NOT NULL,
    dst      INTEGER NOT NULL,
    PRIMARY KEY (plan_id, src, dst)
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id  INTEGER NOT NULL,
    node_id  INTEGER NOT NULL,
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,
    data     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_node ON events(plan_id, node_id, id);
"""


class PlanError(ValueError):
    """Raised for invalid plan operations (unknown ids, cycles, ...)."""


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    def _q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return _row(self._db.execute(sql, args).fetchone())

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # -------------------------------------------------------------------- plans
    def create_plan(
        self,
        title: str,
        description: str = "",
        parent_plan_id: int | None = None,
        parent_node_id: int | None = None,
        cwd: str | None = None,
    ) -> int:
        with self._lock:
            if (parent_plan_id is None) != (parent_node_id is None):
                raise PlanError("parent_plan_id and parent_node_id must be given together")
            if parent_plan_id is not None:
                parent = self.get_node(parent_plan_id, parent_node_id)
                if parent["status"] in ("done", "failed"):
                    raise PlanError(
                        f"node ({parent_plan_id}, {parent_node_id}) is already {parent['status']}; "
                        "cannot attach a subplan"
                    )
                if self.subplan_of(parent_plan_id, parent_node_id) is not None:
                    raise PlanError(f"node ({parent_plan_id}, {parent_node_id}) already has a subplan")
            cur = self._exec(
                "INSERT INTO plans(title, description, parent_plan_id, parent_node_id, cwd, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (title, description, parent_plan_id, parent_node_id, cwd, time.time()),
            )
            return int(cur.lastrowid)

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        p = self._one("SELECT * FROM plans WHERE id=?", (plan_id,))
        if p is None:
            raise PlanError(f"unknown plan {plan_id}")
        return p

    def list_root_plans(self) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM plans WHERE parent_plan_id IS NULL ORDER BY id DESC")

    def started_root_plans(self) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM plans WHERE parent_plan_id IS NULL AND started=1")

    def subplan_of(self, plan_id: int, node_id: int) -> int | None:
        r = self._one(
            "SELECT id FROM plans WHERE parent_plan_id=? AND parent_node_id=?", (plan_id, node_id)
        )
        return r["id"] if r else None

    def root_of(self, plan_id: int) -> dict[str, Any]:
        p = self.get_plan(plan_id)
        while p["parent_plan_id"] is not None:
            p = self.get_plan(p["parent_plan_id"])
        return p

    def ancestors(self, plan_id: int) -> list[dict[str, Any]]:
        """Chain from the root plan down to (excluding) plan_id.

        Each entry: {plan_id, title, node_id, node_title} where node_id is the
        node inside that plan that contains the next plan of the chain.
        """
        chain = []
        p = self.get_plan(plan_id)
        while p["parent_plan_id"] is not None:
            parent = self.get_plan(p["parent_plan_id"])
            node = self.get_node(p["parent_plan_id"], p["parent_node_id"])
            chain.append(
                {
                    "plan_id": parent["id"],
                    "title": parent["title"],
                    "description": parent["description"],
                    "node_id": node["node_id"],
                    "node_title": node["title"],
                    "node_description": node["description"],
                }
            )
            p = parent
        return list(reversed(chain))

    def set_plan_started(self, plan_id: int, executor: str, started: bool = True) -> None:
        self._exec("UPDATE plans SET started=?, executor=? WHERE id=?", (int(started), executor, plan_id))

    def descendant_plans(self, plan_id: int) -> list[int]:
        out, stack = [], [plan_id]
        while stack:
            pid = stack.pop()
            out.append(pid)
            stack.extend(r["id"] for r in self._q("SELECT id FROM plans WHERE parent_plan_id=?", (pid,)))
        return out

    def delete_plan(self, plan_id: int) -> None:
        with self._lock:
            for pid in self.descendant_plans(plan_id):
                self._exec("DELETE FROM events WHERE plan_id=?", (pid,))
                self._exec("DELETE FROM edges WHERE plan_id=?", (pid,))
                self._exec("DELETE FROM nodes WHERE plan_id=?", (pid,))
                self._exec("DELETE FROM plans WHERE id=?", (pid,))

    # -------------------------------------------------------------------- nodes
    def add_node(self, plan_id: int, title: str, description: str = "") -> tuple[int, int]:
        with self._lock:
            p = self.get_plan(plan_id)
            nid = p["next_node_id"]
            self._exec("UPDATE plans SET next_node_id=? WHERE id=?", (nid + 1, plan_id))
            self._exec(
                "INSERT INTO nodes(plan_id, node_id, title, description, created_at) VALUES (?,?,?,?,?)",
                (plan_id, nid, title, description, time.time()),
            )
            return plan_id, nid

    def get_node(self, plan_id: int, node_id: int) -> dict[str, Any]:
        n = self._one("SELECT * FROM nodes WHERE plan_id=? AND node_id=?", (plan_id, node_id))
        if n is None:
            raise PlanError(f"unknown node ({plan_id}, {node_id})")
        return n

    def nodes(self, plan_id: int) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM nodes WHERE plan_id=? ORDER BY node_id", (plan_id,))

    def update_node(self, plan_id: int, node_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(
            f"UPDATE nodes SET {cols} WHERE plan_id=? AND node_id=?",
            (*fields.values(), plan_id, node_id),
        )

    def remove_node(self, plan_id: int, node_id: int) -> None:
        with self._lock:
            n = self.get_node(plan_id, node_id)
            if n["status"] not in ("pending", "ready"):
                raise PlanError(f"node ({plan_id}, {node_id}) is {n['status']}; cannot remove")
            sub = self.subplan_of(plan_id, node_id)
            if sub is not None:
                self.delete_plan(sub)
            self._exec("DELETE FROM edges WHERE plan_id=? AND (src=? OR dst=?)", (plan_id, node_id, node_id))
            self._exec("DELETE FROM events WHERE plan_id=? AND node_id=?", (plan_id, node_id))
            self._exec("DELETE FROM nodes WHERE plan_id=? AND node_id=?", (plan_id, node_id))

    # -------------------------------------------------------------------- edges
    def edges(self, plan_id: int) -> list[tuple[int, int]]:
        return [(r["src"], r["dst"]) for r in self._q("SELECT src, dst FROM edges WHERE plan_id=?", (plan_id,))]

    def add_edge(self, plan_id: int, src: int, dst: int) -> None:
        with self._lock:
            if src == dst:
                raise PlanError("self-loops are not allowed")
            self.get_node(plan_id, src)
            d = self.get_node(plan_id, dst)
            if d["status"] not in ("pending", "ready"):
                raise PlanError(f"node ({plan_id}, {dst}) is already {d['status']}; cannot add a prerequisite")
            # cycle check: is src reachable from dst?
            succ: dict[int, list[int]] = {}
            for a, b in self.edges(plan_id):
                succ.setdefault(a, []).append(b)
            stack, seen = [dst], set()
            while stack:
                cur = stack.pop()
                if cur == src:
                    raise PlanError(f"edge {src} -> {dst} would create a cycle")
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(succ.get(cur, []))
            self._exec("INSERT OR IGNORE INTO edges(plan_id, src, dst) VALUES (?,?,?)", (plan_id, src, dst))
            if d["status"] == "ready":
                self.update_node(plan_id, dst, status="pending")

    def remove_edge(self, plan_id: int, src: int, dst: int) -> None:
        self._exec("DELETE FROM edges WHERE plan_id=? AND src=? AND dst=?", (plan_id, src, dst))

    def predecessors(self, plan_id: int, node_id: int) -> list[int]:
        return [r["src"] for r in self._q("SELECT src FROM edges WHERE plan_id=? AND dst=?", (plan_id, node_id))]

    # ------------------------------------------------------------------- events
    def add_event(self, plan_id: int, node_id: int, kind: str, data: Any) -> dict[str, Any]:
        ts = time.time()
        cur = self._exec(
            "INSERT INTO events(plan_id, node_id, ts, kind, data) VALUES (?,?,?,?,?)",
            (plan_id, node_id, ts, kind, json.dumps(data, default=str)),
        )
        return {"id": cur.lastrowid, "plan_id": plan_id, "node_id": node_id, "ts": ts, "kind": kind, "data": data}

    def events(self, plan_id: int, node_id: int, after_id: int = 0) -> list[dict[str, Any]]:
        rows = self._q(
            "SELECT * FROM events WHERE plan_id=? AND node_id=? AND id>? ORDER BY id",
            (plan_id, node_id, after_id),
        )
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def clear_events(self, plan_id: int, node_id: int) -> None:
        self._exec("DELETE FROM events WHERE plan_id=? AND node_id=?", (plan_id, node_id))
