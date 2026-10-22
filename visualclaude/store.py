"""SQLite persistence for plans, nodes, edges and agent events.

Plan ids are plain auto-incrementing integers. Node ids count up per plan
(plan 1 has nodes 1, 2, 3 ...; plan 2 starts again at 1), so a node is
addressed by the tuple (plan_id, node_id).

Agent profiles are named harness configurations (which agent / model / endpoint
runs a node). Plans and nodes only reference a profile by id, so connection
details live in exactly one place. A profile may hold a literal api key; it is
write-only and must leave this module only through `public_profile`, which
replaces it with a `has_api_key` flag.
"""

from __future__ import annotations

import json
import os
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

CREATE TABLE IF NOT EXISTS agent_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,      -- short slug used by MCP tools and the UI, e.g. "gpt-cheap"
    harness         TEXT NOT NULL,             -- 'claude-sdk' | 'openai' | 'mock'
    model           TEXT,                      -- harness default when NULL
    base_url        TEXT,                      -- OpenAI-compatible endpoint, e.g. http://localhost:11434/v1
    api_key_env     TEXT,                      -- NAME of the env var holding the key
    api_key         TEXT,                      -- optional literal key; WRITE-ONLY, never serialized out
    params          TEXT NOT NULL DEFAULT '{}',-- JSON: temperature, max_tokens, max_turns, reasoning_effort, top_p, ...
    tools           TEXT NOT NULL DEFAULT '{}',-- JSON: {"preset": "all"|"readonly"|"none", "allow": [...], "deny": [...], "mcp": true}
    permission_mode TEXT,                      -- claude-sdk only
    system_append   TEXT,                      -- extra system prompt text for this profile
    max_parallel    INTEGER,                   -- NULL = no profile-specific limit
    description     TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS never alters
# an existing table, so Store._migrate adds whichever of these are missing.
# Additive only (no data loss); append new (table, column, declaration) entries here.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("nodes", "agent_profile_id", "INTEGER"),          # NULL = inherit from the plan chain
    ("plans", "default_agent_profile_id", "INTEGER"),  # NULL = inherit from the parent plan
]

HARNESSES = ("claude-sdk", "openai", "mock")

# Every profile column a caller may set; id and timestamps are store-managed.
PROFILE_FIELDS = (
    "name", "harness", "model", "base_url", "api_key_env", "api_key", "params", "tools",
    "permission_mode", "system_append", "max_parallel", "description",
)


class PlanError(ValueError):
    """Raised for invalid plan operations (unknown ids, cycles, ...)."""


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def public_profile(row: dict[str, Any]) -> dict[str, Any]:
    """A profile safe to send anywhere: the literal key is replaced by `has_api_key`.

    REST, MCP, the UI and logs must only ever see profiles through this function.
    """
    out = {k: v for k, v in row.items() if k != "api_key"}
    env_name = row.get("api_key_env")
    out["has_api_key"] = bool(row.get("api_key")) or bool(env_name and os.environ.get(env_name))
    return out


def _profile_values(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate caller-supplied profile fields and convert them to column values."""
    unknown = set(fields) - set(PROFILE_FIELDS)
    if unknown:
        raise PlanError(f"unknown profile field(s): {', '.join(sorted(unknown))}")
    out = dict(fields)
    if "name" in out:
        name = out["name"]
        if not isinstance(name, str) or not name.strip():
            raise PlanError("profile name must be a non-empty string")
        name = name.strip()
        if name.isdigit():
            # ids and names share one reference syntax (resolve_profile_ref)
            raise PlanError("profile name must not be purely numeric")
        out["name"] = name
    if "harness" in out and out["harness"] not in HARNESSES:
        raise PlanError(f"unknown harness {out['harness']!r}; expected one of {', '.join(HARNESSES)}")
    for key in ("params", "tools"):
        if key in out:
            val = {} if out[key] is None else out[key]
            if not isinstance(val, dict):
                raise PlanError(f"profile {key} must be a JSON object")
            out[key] = json.dumps(val)
    mp = out.get("max_parallel")
    if mp is not None and (isinstance(mp, bool) or not isinstance(mp, int) or mp < 1):
        raise PlanError("max_parallel must be a positive integer or null")
    if "description" in out and out["description"] is None:
        out["description"] = ""
    if out.get("api_key") == "":
        out["api_key"] = None
    return out


class Store:
    def __init__(self, path: str):
        path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        """Add the MIGRATIONS columns an older database lacks. Idempotent."""
        with self._lock:
            for table, column, decl in MIGRATIONS:
                cols = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in cols:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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
    def add_node(
        self,
        plan_id: int,
        title: str,
        description: str = "",
        agent_profile_id: int | None = None,
    ) -> tuple[int, int]:
        with self._lock:
            p = self.get_plan(plan_id)
            if agent_profile_id is not None:
                self.get_profile(agent_profile_id)
            nid = p["next_node_id"]
            self._exec("UPDATE plans SET next_node_id=? WHERE id=?", (nid + 1, plan_id))
            self._exec(
                "INSERT INTO nodes(plan_id, node_id, title, description, agent_profile_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (plan_id, nid, title, description, agent_profile_id, time.time()),
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

    # ----------------------------------------------------------- agent profiles
    @staticmethod
    def _profile_row(r: dict[str, Any] | None) -> dict[str, Any] | None:
        if r is not None:
            r["params"] = json.loads(r["params"] or "{}")
            r["tools"] = json.loads(r["tools"] or "{}")
        return r

    def create_profile(self, **fields: Any) -> int:
        for required in ("name", "harness"):
            if not fields.get(required):
                raise PlanError(f"profile {required} is required")
        vals = _profile_values(fields)
        vals.setdefault("params", "{}")
        vals.setdefault("tools", "{}")
        vals.setdefault("description", "")
        vals["created_at"] = vals["updated_at"] = time.time()
        cols = ", ".join(vals)
        marks = ",".join("?" * len(vals))
        try:
            cur = self._exec(f"INSERT INTO agent_profiles({cols}) VALUES ({marks})", tuple(vals.values()))
        except sqlite3.IntegrityError:
            raise PlanError(f"an agent profile named {vals['name']!r} already exists") from None
        return int(cur.lastrowid)

    def get_profile(self, profile_id: int) -> dict[str, Any]:
        p = self._profile_row(self._one("SELECT * FROM agent_profiles WHERE id=?", (profile_id,)))
        if p is None:
            raise PlanError(f"unknown agent profile {profile_id}")
        return p

    def get_profile_by_name(self, name: str) -> dict[str, Any] | None:
        return self._profile_row(self._one("SELECT * FROM agent_profiles WHERE name=?", (name,)))

    def resolve_profile_ref(self, ref: int | str) -> dict[str, Any]:
        """Look a profile up by id or by name (MCP/REST callers may use either)."""
        if isinstance(ref, int) and not isinstance(ref, bool):
            return self.get_profile(ref)
        if isinstance(ref, str):
            ref = ref.strip()
            if ref.isdigit():  # names are never purely numeric, so this is unambiguous
                return self.get_profile(int(ref))
            p = self.get_profile_by_name(ref)
            if p is not None:
                return p
        raise PlanError(f"unknown agent profile {ref!r}")

    def list_profiles(self) -> list[dict[str, Any]]:
        return [self._profile_row(r) for r in self._q("SELECT * FROM agent_profiles ORDER BY name")]

    def update_profile(self, profile_id: int, **fields: Any) -> None:
        """Partial update. api_key=None keeps the stored key; api_key="" clears it."""
        if "api_key" in fields and fields["api_key"] is None:
            del fields["api_key"]
        for required in ("name", "harness"):
            if required in fields and not fields[required]:
                raise PlanError(f"profile {required} cannot be empty")
        vals = _profile_values(fields)
        with self._lock:
            self.get_profile(profile_id)
            if not vals:
                return
            vals["updated_at"] = time.time()
            cols = ", ".join(f"{k}=?" for k in vals)
            try:
                self._exec(f"UPDATE agent_profiles SET {cols} WHERE id=?", (*vals.values(), profile_id))
            except sqlite3.IntegrityError:
                raise PlanError(f"an agent profile named {vals['name']!r} already exists") from None

    def delete_profile(self, profile_id: int) -> None:
        with self._lock:
            self.get_profile(profile_id)
            self._exec("UPDATE nodes SET agent_profile_id=NULL WHERE agent_profile_id=?", (profile_id,))
            self._exec(
                "UPDATE plans SET default_agent_profile_id=NULL WHERE default_agent_profile_id=?", (profile_id,)
            )
            self._exec("DELETE FROM agent_profiles WHERE id=?", (profile_id,))

    def set_node_profile(self, plan_id: int, node_id: int, profile_id: int | None) -> None:
        with self._lock:
            self.get_node(plan_id, node_id)
            if profile_id is not None:
                self.get_profile(profile_id)
            self.update_node(plan_id, node_id, agent_profile_id=profile_id)

    def set_plan_profile(self, plan_id: int, profile_id: int | None) -> None:
        with self._lock:
            self.get_plan(plan_id)
            if profile_id is not None:
                self.get_profile(profile_id)
            self._exec("UPDATE plans SET default_agent_profile_id=? WHERE id=?", (profile_id, plan_id))

    def profile_for_node(self, plan_id: int, node_id: int) -> dict[str, Any] | None:
        """The profile that should run a node, or None (= use the server-wide default).

        Order: the node's own profile, then its plan's default, then each
        ancestor plan's default up to the root. First hit wins.
        """
        with self._lock:
            pid = self.get_node(plan_id, node_id)["agent_profile_id"]
            p = self.get_plan(plan_id)
            while pid is None:
                pid = p["default_agent_profile_id"]
                if p["parent_plan_id"] is None:
                    break
                p = self.get_plan(p["parent_plan_id"])
            if pid is None:
                return None
            # delete_profile clears references, but tolerate a hand-edited db
            return self._profile_row(self._one("SELECT * FROM agent_profiles WHERE id=?", (pid,)))
