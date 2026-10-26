"""SQLite persistence for plans, nodes, edges and agent events.

Plan ids are plain auto-incrementing integers. Node ids count up per plan
(plan 1 has nodes 1, 2, 3 ...; plan 2 starts again at 1), so a node is
addressed by the tuple (plan_id, node_id).

Agent profiles are named harness configurations (which agent / model / endpoint
runs a node). Plans and nodes only reference a profile by id, so connection
details live in exactly one place. A profile may hold a literal api key; it is
write-only and must leave this module only through `public_profile`, which
replaces it with a `has_api_key` flag.

A node may declare the files it will create or modify (node_files); reads are
never declared. The scheduler uses them to keep two nodes that write the same
file from running at the same time; this module only stores them and answers
"who else touches these?". Paths are relative to the plan tree's shared working
directory and are compared only through their normalized `key`
(`normalize_file`) and the single conflict rule (`keys_conflict`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

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

CREATE TABLE IF NOT EXISTS node_files (
    plan_id  INTEGER NOT NULL,
    node_id  INTEGER NOT NULL,
    path     TEXT NOT NULL,   -- as the author wrote it, for display
    key      TEXT NOT NULL,   -- normalized form (normalize_file), the only thing ever compared
    PRIMARY KEY (plan_id, node_id, key)
);
CREATE INDEX IF NOT EXISTS node_files_key ON node_files(key);

-- Earlier nodes (same plan) whose result text this node wants in its prompt.
-- Not an edge: ordering comes from edges alone; the engine checks reachability.
CREATE TABLE IF NOT EXISTS node_results_from (
    plan_id       INTEGER NOT NULL,
    node_id       INTEGER NOT NULL,
    from_node_id  INTEGER NOT NULL,
    PRIMARY KEY (plan_id, node_id, from_node_id)
);

CREATE TABLE IF NOT EXISTS node_diffs (
    plan_id  INTEGER NOT NULL,
    node_id  INTEGER NOT NULL,
    path     TEXT NOT NULL,   -- as seen in the tool_use input, for display
    diff     TEXT NOT NULL,   -- unified diff text; absent files are never stored, not even as empty
    PRIMARY KEY (plan_id, node_id, path)
);

CREATE TABLE IF NOT EXISTS reviews (
    plan_id     INTEGER NOT NULL,
    node_id     INTEGER,            -- NULL = the plan-level review (titles/descriptions/results as a whole)
    verdict     TEXT NOT NULL,      -- 'ok' | 'concerns' | 'error' (reviewer couldn't form a verdict)
    summary     TEXT NOT NULL DEFAULT '',
    findings    TEXT,               -- reviewer's prose, or a JSON list of {severity, file, note}; not over-modeled
    agent_name  TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    created_at  REAL NOT NULL
);
-- One row per node and one per plan (node_id NULL); SQLite's PRIMARY KEY can't
-- enforce that across a nullable column, so two partial unique indexes do it
-- instead, same trick as plans_parent above.
CREATE UNIQUE INDEX IF NOT EXISTS reviews_node ON reviews(plan_id, node_id) WHERE node_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS reviews_plan ON reviews(plan_id) WHERE node_id IS NULL;
"""

REVIEW_VERDICTS = ("ok", "concerns", "error")

# Columns added after the first release. CREATE TABLE IF NOT EXISTS never alters
# an existing table, so Store._migrate adds whichever of these are missing.
# Additive only (no data loss); append new (table, column, declaration) entries here.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("nodes", "agent_profile_id", "INTEGER"),          # NULL = inherit from the plan chain
    ("plans", "default_agent_profile_id", "INTEGER"),  # NULL = inherit from the parent plan
    ("nodes", "tokens_in", "INTEGER"),                 # NULL = unknown (mock runs, older nodes)
    ("nodes", "tokens_out", "INTEGER"),
    ("plans", "max_parallel", "INTEGER"),              # NULL = no plan-specific limit (root plans only)
    ("plans", "review_agent_profile_id", "INTEGER"),   # NULL = reviewing is off (the default)
    ("nodes", "rework_brief", "TEXT"),                 # what the last rework asked for; NULL = never reworked
    ("nodes", "kind", "TEXT"),                         # NODE_KINDS; NULL = 'agent'
    ("agent_profiles", "capability", "TEXT"),          # CAPABILITIES; NULL = the registry's default for the role
    ("agent_profiles", "test_level", "TEXT"),          # TEST_LEVELS; NULL = 'standard'
    ("plans", "test_instructions", "TEXT"),            # root plans only; NULL = agents find out from the repo
    ("nodes", "undeclared_files", "TEXT"),             # JSON list of paths changed outside `files`; NULL = none
    ("nodes", "undeclared_more", "INTEGER"),           # how many more than the stored list; NULL = none
    ("nodes", "undeclared_shared", "INTEGER"),         # 1 = another step of the tree ran meanwhile
]

# At most this many undeclared paths are stored per node; the rest are only counted.
UNDECLARED_CAP = 200

# nodes.kind: who does the work. 'agent' runs on an agent profile; 'user' is the
# user's own job (push, open an MR, deploy, approve) and the plan waits there
# until they mark it done.
NODE_KINDS = ("agent", "user")

# agent_profiles.capability: what the harness lets the agent do. The registry maps
# it to each harness's own gating (allowed tools / tools preset); NULL means the
# registry's default for the role (full for workers, run for reviewers).
CAPABILITIES = ("read", "run", "edit", "full")

# agent_profiles.test_level: how much a worker agent is told to test. Prompt text
# lives in prompts.py; NULL means 'standard'.
TEST_LEVELS = ("none", "light", "standard", "thorough")

# Every plan column a caller may change through update_plan; id, parentage and timestamps
# are store-managed.
PLAN_FIELDS = ("max_parallel", "cwd", "test_instructions")

HARNESSES = ("claude-sdk", "openai", "mock")

# Every profile column a caller may set; id and timestamps are store-managed.
PROFILE_FIELDS = (
    "name", "harness", "model", "base_url", "api_key_env", "api_key", "params", "tools",
    "permission_mode", "system_append", "max_parallel", "description", "capability", "test_level",
)


class PlanError(ValueError):
    """Raised for invalid plan operations (unknown ids, cycles, ...)."""


def normalize_file(path: str) -> str:
    """The match key of a declared file path. Raises PlanError if it is unusable.

    Paths are relative to the plan tree's working directory, so absolute paths
    (`/x`, `\\\\server\\x`, `C:\\x`, also drive-relative `C:x`) are rejected, as is
    anything that escapes the root via `..` or names the root itself. Otherwise:
    backslashes become forward slashes, repeated slashes collapse, `.` segments
    are dropped, `..` is resolved lexically (no filesystem access) and a
    trailing slash is stripped.

    The key is casefolded on EVERY platform, not just Windows. The project is
    developed on Windows; on a case-sensitive filesystem the only cost is that
    two genuinely distinct files differing only in case get serialized
    unnecessarily -- a safe false positive. The opposite mistake (treating
    `App.js` and `app.js` as different on Windows) would let two agents write
    the same file concurrently and lose an edit.
    """
    if not isinstance(path, str) or not path.strip():
        raise PlanError("file path must be a non-empty string")
    p = path.strip().replace("\\", "/")
    if p.startswith("/") or (len(p) >= 2 and p[1] == ":" and p[0].isalpha()):
        raise PlanError(f"file path {path!r} is absolute; paths are relative to the plan's working directory")
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                raise PlanError(f"file path {path!r} escapes the plan's working directory")
            parts.pop()
            continue
        parts.append(seg)
    if not parts:
        raise PlanError(f"file path {path!r} names the working directory itself, not a file or directory in it")
    return "/".join(parts).casefold()


def keys_conflict(a: str, b: str) -> bool:
    """Whether two normalized keys may name the same file.

    True when equal or when one is a directory prefix of the other at a segment
    boundary: `src` conflicts with `src/foo.py` (a node may declare a whole
    directory it reorganises), `src` does not conflict with `srcfoo`. Every
    caller compares keys through this function, never with plain equality.
    """
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


def _normalize_files(paths: list[str] | None) -> list[tuple[str, str]]:
    """Validate paths into (display path, key) pairs; the first spelling of a key wins."""
    if paths is None:
        return []
    if isinstance(paths, str) or not isinstance(paths, (list, tuple)):
        raise PlanError("files must be a list of paths")
    out: dict[str, str] = {}
    for p in paths:
        out.setdefault(normalize_file(p), p.strip())
    return [(path, key) for key, path in out.items()]


def _decode_node(n: dict[str, Any]) -> dict[str, Any]:
    """A node row as callers see it: the undeclared paths as a list (empty = none recorded),
    the overflow count as an int and the shared flag as a bool."""
    n["undeclared_files"] = json.loads(n["undeclared_files"]) if n.get("undeclared_files") else []
    n["undeclared_more"] = n.get("undeclared_more") or 0
    n["undeclared_shared"] = bool(n.get("undeclared_shared"))
    return n


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def _check_max_parallel(value: Any) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise PlanError("max_parallel must be a positive integer or null")


def _check_choice(name: str, value: Any, allowed: tuple[str, ...]) -> None:
    """Raise PlanError unless value is None or one of `allowed`."""
    if value is not None and value not in allowed:
        raise PlanError(f"unknown {name} {value!r}; expected one of {', '.join(allowed)}")


def validate_working_directory(path: str) -> None:
    """Raise PlanError unless path is an existing absolute directory.

    Shared by create_plan and the later working-directory change, so both apply the
    same rule a plan's agents can rely on.
    """
    if not os.path.isabs(path) or not os.path.isdir(path):
        raise PlanError(f"working_directory must be an existing absolute path: {path}")


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
    if "max_parallel" in out:
        _check_max_parallel(out["max_parallel"])
    if "capability" in out:
        _check_choice("capability", out["capability"], CAPABILITIES)
    if "test_level" in out:
        _check_choice("test_level", out["test_level"], TEST_LEVELS)
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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block of store calls all or nothing: any exception rolls every write in it back.

        Holds the lock throughout, so no other thread sees (or interleaves with) the block's
        writes. A nested block joins the outer one.
        """
        with self._lock:
            if self._db.in_transaction:
                yield
                return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # -------------------------------------------------------------------- plans
    def create_plan(
        self,
        title: str,
        description: str = "",
        parent_plan_id: int | None = None,
        parent_node_id: int | None = None,
        cwd: str | None = None,
        max_parallel: int | None = None,
        test_instructions: str | None = None,
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
            _check_max_parallel(max_parallel)
            cur = self._exec(
                "INSERT INTO plans(title, description, parent_plan_id, parent_node_id, cwd, max_parallel, "
                "test_instructions, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (title, description, parent_plan_id, parent_node_id, cwd, max_parallel, test_instructions,
                 time.time()),
            )
            return int(cur.lastrowid)

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        p = self._one("SELECT * FROM plans WHERE id=?", (plan_id,))
        if p is None:
            raise PlanError(f"unknown plan {plan_id}")
        return p

    def update_plan(self, plan_id: int, **fields: Any) -> None:
        """Change plan settings (``max_parallel``, ``cwd``, ``test_instructions``); only the given
        fields change."""
        unknown = set(fields) - set(PLAN_FIELDS)
        if unknown:
            raise PlanError(f"unknown plan field(s): {', '.join(sorted(unknown))}")
        if "max_parallel" in fields:
            _check_max_parallel(fields["max_parallel"])
        if not fields:
            return
        with self._lock:
            if self.get_plan(plan_id)["parent_plan_id"] is not None and "test_instructions" in fields:
                raise PlanError("test_instructions can only be set on top-level plans; a subplan "
                                "uses its top-level plan's")
            sets = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE plans SET {sets} WHERE id=?", (*fields.values(), plan_id))

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
                self._exec("DELETE FROM node_files WHERE plan_id=?", (pid,))
                self._exec("DELETE FROM node_results_from WHERE plan_id=?", (pid,))
                self._exec("DELETE FROM node_diffs WHERE plan_id=?", (pid,))
                self._exec("DELETE FROM reviews WHERE plan_id=?", (pid,))
                self._exec("DELETE FROM nodes WHERE plan_id=?", (pid,))
                self._exec("DELETE FROM plans WHERE id=?", (pid,))

    # -------------------------------------------------------------------- nodes
    def add_node(
        self,
        plan_id: int,
        title: str,
        description: str = "",
        agent_profile_id: int | None = None,
        files: list[str] | None = None,
        results_from: list[int] | None = None,
        kind: str | None = None,
    ) -> tuple[int, int]:
        _check_choice("node kind", kind, NODE_KINDS)
        with self._lock:
            p = self.get_plan(plan_id)
            if agent_profile_id is not None:
                self.get_profile(agent_profile_id)
            entries = _normalize_files(files)  # validate before inserting anything
            from_ids = self._check_results_from(plan_id, results_from)
            nid = p["next_node_id"]
            self._exec("UPDATE plans SET next_node_id=? WHERE id=?", (nid + 1, plan_id))
            self._exec(
                "INSERT INTO nodes(plan_id, node_id, title, description, agent_profile_id, kind, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (plan_id, nid, title, description, agent_profile_id, kind, time.time()),
            )
            self._write_files(plan_id, nid, entries)
            self._write_results_from(plan_id, nid, from_ids)
            return plan_id, nid

    def get_node(self, plan_id: int, node_id: int) -> dict[str, Any]:
        n = self._one("SELECT * FROM nodes WHERE plan_id=? AND node_id=?", (plan_id, node_id))
        if n is None:
            raise PlanError(f"unknown node ({plan_id}, {node_id})")
        return _decode_node(n)

    def nodes(self, plan_id: int) -> list[dict[str, Any]]:
        return [_decode_node(n) for n in self._q("SELECT * FROM nodes WHERE plan_id=? ORDER BY node_id", (plan_id,))]

    def update_node(self, plan_id: int, node_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(
            f"UPDATE nodes SET {cols} WHERE plan_id=? AND node_id=?",
            (*fields.values(), plan_id, node_id),
        )

    def set_node_kind(self, plan_id: int, node_id: int, kind: str | None) -> None:
        """Change who does a node (NODE_KINDS; None = 'agent'), only while it is pending/ready."""
        _check_choice("node kind", kind, NODE_KINDS)
        with self._lock:
            n = self.get_node(plan_id, node_id)
            if n["status"] not in ("pending", "ready"):
                raise PlanError(f"node ({plan_id}, {node_id}) is {n['status']}; cannot change its kind")
            self.update_node(plan_id, node_id, kind=kind)

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
            self._exec("DELETE FROM node_files WHERE plan_id=? AND node_id=?", (plan_id, node_id))
            # Both directions: a dangling from_node_id would silently drop context later.
            self._exec(
                "DELETE FROM node_results_from WHERE plan_id=? AND (node_id=? OR from_node_id=?)",
                (plan_id, node_id, node_id),
            )
            self._exec("DELETE FROM node_diffs WHERE plan_id=? AND node_id=?", (plan_id, node_id))
            self._exec("DELETE FROM reviews WHERE plan_id=? AND node_id=?", (plan_id, node_id))
            self._exec("DELETE FROM nodes WHERE plan_id=? AND node_id=?", (plan_id, node_id))

    # --------------------------------------------------------------- node files
    def _write_files(self, plan_id: int, node_id: int, entries: list[tuple[str, str]]) -> None:
        with self._lock:
            self._exec("DELETE FROM node_files WHERE plan_id=? AND node_id=?", (plan_id, node_id))
            for path, key in entries:
                self._exec(
                    "INSERT INTO node_files(plan_id, node_id, path, key) VALUES (?,?,?,?)",
                    (plan_id, node_id, path, key),
                )

    def set_node_files(self, plan_id: int, node_id: int, paths: list[str] | None) -> list[dict[str, Any]]:
        """Replace the whole set of files a node may create or modify (None or [] clears it).

        Only while the node is pending/ready: the scheduler acts on the set before
        spawning, so changing it once the node has run would be meaningless.
        """
        entries = _normalize_files(paths)
        with self._lock:
            n = self.get_node(plan_id, node_id)
            if n["status"] not in ("pending", "ready"):
                raise PlanError(f"node ({plan_id}, {node_id}) is {n['status']}; cannot change its files")
            self._write_files(plan_id, node_id, entries)
            return self.node_files(plan_id, node_id)

    def node_files(self, plan_id: int, node_id: int) -> list[dict[str, Any]]:
        return self._q(
            "SELECT path, key FROM node_files WHERE plan_id=? AND node_id=? ORDER BY key", (plan_id, node_id)
        )

    # ------------------------------------------------------- node results_from
    def _check_results_from(self, plan_id: int, ids: list[int] | None) -> list[int]:
        """Dedupe and sort `ids`, rejecting any that is not a node of this plan.

        Only existence is checked here; whether each is a transitive dependency
        is the engine's call (it owns the edge reachability).
        """
        out = sorted({int(i) for i in ids or []})
        for i in out:
            if self._one("SELECT 1 FROM nodes WHERE plan_id=? AND node_id=?", (plan_id, i)) is None:
                raise PlanError(f"results_from: unknown node ({plan_id}, {i})")
        return out

    def _write_results_from(self, plan_id: int, node_id: int, ids: list[int]) -> None:
        with self._lock:
            self._exec("DELETE FROM node_results_from WHERE plan_id=? AND node_id=?", (plan_id, node_id))
            for i in ids:
                self._exec(
                    "INSERT INTO node_results_from(plan_id, node_id, from_node_id) VALUES (?,?,?)",
                    (plan_id, node_id, i),
                )

    def set_node_results_from(self, plan_id: int, node_id: int, ids: list[int] | None) -> list[int]:
        """Replace the set of earlier nodes whose result text this node gets (None or [] clears it).

        Only while the node is pending/ready, like set_node_files: the prompt is
        built when the node starts.
        """
        with self._lock:
            n = self.get_node(plan_id, node_id)
            if n["status"] not in ("pending", "ready"):
                raise PlanError(f"node ({plan_id}, {node_id}) is {n['status']}; cannot change its results_from")
            self._write_results_from(plan_id, node_id, self._check_results_from(plan_id, ids))
            return self.node_results_from(plan_id, node_id)

    def node_results_from(self, plan_id: int, node_id: int) -> list[int]:
        rows = self._q(
            "SELECT from_node_id FROM node_results_from WHERE plan_id=? AND node_id=? ORDER BY from_node_id",
            (plan_id, node_id),
        )
        return [r["from_node_id"] for r in rows]

    def files_of_plan_tree(self, root_plan_id: int) -> dict[tuple[int, int], list[dict[str, Any]]]:
        """{(plan_id, node_id): [{path, key}, ...]} for every node in the tree that declares files.

        One query for the whole tree, so the scheduler can rebuild its conflict
        map on every tick cheaply. Nodes without files are absent.
        """
        with self._lock:
            pids = self.descendant_plans(root_plan_id)
            marks = ",".join("?" * len(pids))
            rows = self._q(
                f"SELECT plan_id, node_id, path, key FROM node_files WHERE plan_id IN ({marks}) "
                "ORDER BY plan_id, node_id, key",
                tuple(pids),
            )
        out: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault((r["plan_id"], r["node_id"]), []).append({"path": r["path"], "key": r["key"]})
        return out

    def conflicting_nodes(
        self,
        plan_id: int,
        node_id: int,
        among: Iterable[tuple[int, int]] | None = None,
        tree: dict[tuple[int, int], list[dict[str, Any]]] | None = None,
    ) -> list[tuple[int, int]]:
        """Nodes of the same plan tree that share a conflicting file key with this one.

        Excludes the node itself; optionally restricted to `among`. Sorted by
        (plan_id, node_id), which is also the order conflicting nodes run in.
        Dependency paths are NOT considered here -- dependency-ordered nodes
        never overlap, and filtering them out is the scheduler's job.
        `tree` is a files_of_plan_tree() result to reuse when checking many nodes.
        """
        if tree is None:
            with self._lock:
                tree = self.files_of_plan_tree(self.root_of(plan_id)["id"])
        mine = [f["key"] for f in tree.get((plan_id, node_id), [])]
        if not mine:
            return []
        wanted = None if among is None else {(int(a), int(b)) for a, b in among}
        out = []
        for ref, files in tree.items():
            if ref == (plan_id, node_id) or (wanted is not None and ref not in wanted):
                continue
            if any(keys_conflict(k, f["key"]) for k in mine for f in files):
                out.append(ref)
        return sorted(out)

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

    # -------------------------------------------------------------- node diffs
    def set_node_diffs(self, plan_id: int, node_id: int, diffs: list[tuple[str, str]]) -> None:
        """Replace a node's stored diffs (one unified diff per changed file)."""
        with self._lock:
            self.clear_node_diffs(plan_id, node_id)
            for path, diff in diffs:
                self._exec(
                    "INSERT INTO node_diffs(plan_id, node_id, path, diff) VALUES (?,?,?,?)",
                    (plan_id, node_id, path, diff),
                )

    def node_diffs(self, plan_id: int, node_id: int) -> list[dict[str, Any]]:
        return self._q(
            "SELECT path, diff FROM node_diffs WHERE plan_id=? AND node_id=? ORDER BY path", (plan_id, node_id)
        )

    def clear_node_diffs(self, plan_id: int, node_id: int) -> None:
        """Drop a node's diffs and its undeclared changes: both describe one run."""
        self._exec("DELETE FROM node_diffs WHERE plan_id=? AND node_id=?", (plan_id, node_id))
        self.update_node(plan_id, node_id, undeclared_files=None, undeclared_more=None, undeclared_shared=None)

    def set_node_undeclared(self, plan_id: int, node_id: int, paths: list[str], shared: bool) -> None:
        """Record the paths a node's run changed outside its declared files (names only), capped
        at UNDECLARED_CAP; ``shared`` = other steps of the tree ran meanwhile, so some may be theirs."""
        paths = sorted(paths)
        self.update_node(plan_id, node_id, undeclared_files=json.dumps(paths[:UNDECLARED_CAP]),
                         undeclared_more=max(len(paths) - UNDECLARED_CAP, 0) or None,
                         undeclared_shared=1 if shared else None)

    # ---------------------------------------------------------------- reviews
    def set_review(
        self,
        plan_id: int,
        node_id: int | None,
        verdict: str,
        summary: str = "",
        findings: str | None = None,
        agent_name: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        """Record a review, replacing any existing one for this (plan_id, node_id).

        node_id=None is the plan-level review (the whole plan, not one step).
        """
        if verdict not in REVIEW_VERDICTS:
            raise PlanError(f"unknown review verdict {verdict!r}; expected one of {', '.join(REVIEW_VERDICTS)}")
        with self._lock:
            self.clear_review(plan_id, node_id)
            self._exec(
                "INSERT INTO reviews(plan_id, node_id, verdict, summary, findings, agent_name, tokens_in, "
                "tokens_out, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (plan_id, node_id, verdict, summary, findings, agent_name, tokens_in, tokens_out, time.time()),
            )

    def review(self, plan_id: int, node_id: int | None = None) -> dict[str, Any] | None:
        if node_id is None:
            return self._one("SELECT * FROM reviews WHERE plan_id=? AND node_id IS NULL", (plan_id,))
        return self._one("SELECT * FROM reviews WHERE plan_id=? AND node_id=?", (plan_id, node_id))

    def reviews_of_plan(self, plan_id: int) -> list[dict[str, Any]]:
        """Every review of this plan: each node's review plus the plan-level one, if present."""
        return self._q("SELECT * FROM reviews WHERE plan_id=? ORDER BY node_id", (plan_id,))

    def clear_review(self, plan_id: int, node_id: int | None = None) -> None:
        if node_id is None:
            self._exec("DELETE FROM reviews WHERE plan_id=? AND node_id IS NULL", (plan_id,))
        else:
            self._exec("DELETE FROM reviews WHERE plan_id=? AND node_id=?", (plan_id, node_id))

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
            self._exec(
                "UPDATE plans SET review_agent_profile_id=NULL WHERE review_agent_profile_id=?", (profile_id,)
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

    def set_plan_review_agent(self, plan_id: int, profile_id: int | None) -> None:
        """Set the agent that reviews this plan's steps; None (the default) turns reviewing off."""
        with self._lock:
            self.get_plan(plan_id)
            if profile_id is not None:
                self.get_profile(profile_id)
            self._exec("UPDATE plans SET review_agent_profile_id=? WHERE id=?", (profile_id, plan_id))

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
