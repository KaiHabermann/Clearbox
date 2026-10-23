"""File-scoped nodes: normalization, mutual exclusion, ordering, against a running --mock server.

Part 1 needs no server: it drives a Store on a throwaway database and checks the
path rules (absolute and escaping paths rejected, duplicates collapsed, the
author's spelling kept for display, no orphan node_files rows after remove_node
or delete_plan). Part 2 builds plans over MCP, runs them on the server and
proves every scheduling claim from the started_at/finished_at windows, like
test_mixed_run.py: conflicting nodes never overlap and run lowest number first,
unrelated nodes still run in parallel, directory/case/separator spellings
conflict, sibling files do not, dependency-ordered nodes are unaffected, a
subplan conflicts with its parent plan, a busy lower node is not overtaken, the
external executor's claim_next_node honours held files, and a lower-numbered
node stranded in a failed subplan does not starve the rest of the tree.
Every plan and profile it creates is deleted again.

The server's --max-concurrency must be exactly 3 (the default) -- part 2 asserts
that three file-disjoint nodes overlap.

Run:  uv run clearbox --mock      (one shell)
      uv run python tests/test_file_conflicts.py [http://127.0.0.1:8765]
"""
import asyncio, json, os, sys, tempfile, time, uuid
import urllib.error
import urllib.request
from mcp.client.client import Client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clearbox.store import PlanError, Store, keys_conflict, normalize_file

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
TAG = uuid.uuid4().hex[:6]


# ----------------------------------------------------------- part 1: store rules
def store_rules() -> None:
    assert normalize_file("Src/A.py") == normalize_file("src/a.py") == normalize_file("src\\a.py") \
        == normalize_file("./src//a.py/") == normalize_file("src/x/../a.py") == "src/a.py"
    assert keys_conflict("src", "src/a.py") and keys_conflict("src/a.py", "src")
    assert not keys_conflict("src", "srcfoo") and not keys_conflict("src/a.py", "src/b.py")
    for bad in ["/etc/passwd", "C:\\x.py", "C:x.py", "c:/x", "\\\\server\\share\\x", "../x", "src/../../x",
                ".", "./", "", "   "]:
        try:
            normalize_file(bad); raise AssertionError(f"accepted {bad!r}")
        except PlanError:
            pass

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = Store(os.path.join(d, "t.db"))
        orphans = lambda: s._q("SELECT COUNT(*) AS n FROM node_files WHERE NOT EXISTS "
                               "(SELECT 1 FROM nodes WHERE nodes.plan_id=node_files.plan_id "
                               "AND nodes.node_id=node_files.node_id)")[0]["n"]
        rows = lambda: s._q("SELECT COUNT(*) AS n FROM node_files")[0]["n"]
        p = s.create_plan("rules")
        before = s.get_plan(p)["next_node_id"]
        for bad in (["../x.py"], ["C:\\x.py"], ["ok.py", "/abs.py"], "notalist.py"):
            try:
                s.add_node(p, "bad", files=bad); raise AssertionError(f"accepted {bad!r}")
            except PlanError:
                pass
        assert s.get_plan(p)["next_node_id"] == before and s.nodes(p) == [] and rows() == 0  # nothing half-written

        _, n1 = s.add_node(p, "dups", files=["Static/App.js", "static\\app.js", "./static/app.js", "README.md"])
        f = s.node_files(p, n1)
        assert [(x["path"], x["key"]) for x in f] == [("README.md", "readme.md"), ("Static/App.js", "static/app.js")], f
        _, n2 = s.add_node(p, "plain")
        assert s.node_files(p, n2) == [] and s.conflicting_nodes(p, n2) == []
        s.set_node_files(p, n2, [".\\STATIC\\"])
        assert [x["path"] for x in s.node_files(p, n2)] == [".\\STATIC\\"]  # spelling kept verbatim
        assert s.conflicting_nodes(p, n1) == [(p, n2)] and s.conflicting_nodes(p, n2) == [(p, n1)]
        try:
            s.set_node_files(p, n2, ["../up"]); raise AssertionError("escaping path accepted")
        except PlanError:
            pass
        assert [x["path"] for x in s.node_files(p, n2)] == [".\\STATIC\\"]  # a bad set changes nothing

        sp = s.create_plan("sub", parent_plan_id=p, parent_node_id=n2)
        _, s1 = s.add_node(sp, "sub leaf", files=["static/app.js"])
        assert s.conflicting_nodes(sp, s1) == [(p, n1), (p, n2)]  # across the tree, sorted
        s.set_node_files(p, n2, [])
        assert s.node_files(p, n2) == []
        s.update_node(p, n1, status="running")
        try:
            s.set_node_files(p, n1, ["x"]); raise AssertionError("files of a running node changed")
        except PlanError as e:
            assert "running" in str(e), e

        s.remove_node(p, n2)  # takes its subplan with it
        assert orphans() == 0 and rows() == 2, rows()
        s.delete_plan(p)
        assert orphans() == 0 and rows() == 0, rows()
        s._db.close()  # Windows cannot delete an open database file
    print("store rules OK")


# --------------------------------------------------------- part 2: scheduling
def rest(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode()}") from None


def overlap(a, b) -> bool:
    return a["started_at"] < b["finished_at"] and b["started_at"] < a["finished_at"]


def max_overlap(rows):
    marks = sorted([(n["started_at"], 1) for n in rows] + [(n["finished_at"], -1) for n in rows],
                   key=lambda m: (m[0], m[1]))
    cur = best = 0
    for _, d in marks:
        cur += d
        best = max(best, cur)
    return best


def strictly_in_order(rows) -> None:
    """Each node finished before the next one started: no overlap at all, in node order."""
    for a, b in zip(rows, rows[1:]):
        assert a["finished_at"] <= b["started_at"], (a["node_id"], a["finished_at"], b["node_id"], b["started_at"])


def node(pid, nid):
    return rest("GET", f"/api/plans/{pid}/nodes/{nid}")["node"]


def wait_nodes(refs, timeout=60.0, until=("done", "failed")):
    """Poll until every (plan, node) is in one of `until`; returns {ref: node view}."""
    t0 = time.time()
    while True:
        views = {r: node(*r) for r in refs}
        if all(v["status"] in until for v in views.values()):
            return views
        if time.time() - t0 > timeout:
            raise AssertionError("timed out: " + ", ".join(
                f"{r}={v['status']} blocked_by={v['blocked_by']}" for r, v in views.items()))
        time.sleep(0.2)


def wait_for(pred, timeout=30.0):
    t0 = time.time()
    while not pred():
        if time.time() - t0 > timeout:
            raise AssertionError("condition never became true")
        time.sleep(0.05)


async def main():
    profiles: list[dict] = []
    plans: list[int] = []

    def profile(name, **body):
        p = rest("POST", "/api/agents", {"name": f"fc-{name}-{TAG}", **body})
        profiles.append(p)
        return p

    async with Client(URL + "/mcp") as c:
        async def call(name, /, **a):
            r = await c.call_tool(name, a)
            if r.is_error:
                raise RuntimeError(r.content[0].text)
            sc = r.structured_content
            return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)

        fast = profile("fast", harness="mock", params={"speed": 6})

        async def plan(title, specs, edges=(), parent=None, start=True):
            """specs: list of files lists (None = declare nothing); returns (plan_id, [node ids])."""
            kw = {"parent_plan_id": parent[0], "parent_node_id": parent[1]} if parent else {}
            pid = await call("create_plan", title=f"{title} {TAG}", **kw)
            if parent is None:
                plans.append(pid)
                rest("PUT", f"/api/plans/{pid}/agent", {"agent": fast["id"]})
            ids = []
            for i, files in enumerate(specs):
                extra = {"files": files} if files is not None else {}
                ids.append((await call("add_node", plan_id=pid, title=f"N{i + 1}", **extra))[1])
            for a, b in edges:
                await call("add_edge", plan_id=pid, from_node_id=ids[a], to_node_id=ids[b])
            if start and parent is None:
                rest("POST", f"/api/plans/{pid}/start", {"executor": "builtin"})
            return pid, ids

        async def run(title, specs, edges=()):
            pid, ids = await plan(title, specs, edges)
            v = wait_nodes([(pid, n) for n in ids])
            rows = [v[(pid, n)] for n in ids]
            assert all(r["status"] == "done" for r in rows), [(r["node_id"], r["status"], r["error"]) for r in rows]
            return pid, rows

        try:
            # 1) 1 and 3 share a file, 2 declares nothing
            pid, (n1, n2, n3) = await plan("shared pair", [["shared.txt"], None, ["shared.txt"]])
            wait_for(lambda: node(pid, 1)["status"] == "running")
            v3 = node(pid, 3)
            if v3["status"] == "ready":  # held back while 1 runs
                assert v3["blocked_by"] == {"plan_id": pid, "node_id": 1, "title": "N1",
                                            "files": ["shared.txt"], "reason": "running"}, v3["blocked_by"]
            v = wait_nodes([(pid, n) for n in (n1, n2, n3)])
            r1, r2, r3 = v[(pid, n1)], v[(pid, n2)], v[(pid, n3)]
            assert not overlap(r1, r3) and r1["finished_at"] <= r3["started_at"], (r1, r3)
            assert overlap(r2, r1) or overlap(r2, r3), "the file-less node was serialized too"
            print(f"shared pair OK   1:[{r1['started_at']:.2f},{r1['finished_at']:.2f}] "
                  f"2:[{r2['started_at']:.2f},{r2['finished_at']:.2f}] 3:[{r3['started_at']:.2f},{r3['finished_at']:.2f}]")

            # 2) three independent nodes on one file: strictly 1, 2, 3
            _, rows = await run("one file x3", [["notes.md"]] * 3)
            strictly_in_order(rows)
            assert max_overlap(rows) == 1
            print("one file x3 OK   order", [r["node_id"] for r in sorted(rows, key=lambda r: r["started_at"])])

            # 3) directory prefix
            _, rows = await run("dir prefix", [["src"], ["src/a.py"]])
            strictly_in_order(rows)
            print("dir prefix OK")

            # 4) case and separator spellings of one file
            _, rows = await run("spellings", [["Src/A.py"], ["src/a.py"], ["src\\a.py"]])
            strictly_in_order(rows)
            print("spellings OK")

            # 5) no false positives: siblings and a name that merely shares a prefix run together
            _, rows = await run("siblings", [["src/a.py"], ["src/b.py"], ["srcfoo/a.py", "src.py"]])
            assert all(r["conflicts"] == [] for r in rows), rows
            assert max_overlap(rows) == 3, max_overlap(rows)
            print("siblings OK      max overlap 3")

            # 6) dependency-ordered nodes sharing a file: no conflict listed, never blocked, run fine
            pid, ids = await plan("dep ordered", [["x.txt"], ["x.txt"], None], edges=[(0, 1)])
            assert all(node(pid, n)["conflicts"] == [] for n in ids)
            v = wait_nodes([(pid, n) for n in ids])
            assert all(x["status"] == "done" for x in v.values())
            assert v[(pid, ids[0])]["finished_at"] <= v[(pid, ids[1])]["started_at"]
            print("dep ordered OK")

            # 7) a subplan node and a root-plan node on one file
            pid, (c1, c2) = await plan("cross tree", [None, ["shared/x.py"]], start=False)
            sp, (s1,) = await plan("cross sub", [["shared"]], parent=(pid, c1))
            assert [(x["plan_id"], x["node_id"]) for x in node(pid, c2)["conflicts"]] == [(sp, s1)]
            assert [(x["plan_id"], x["node_id"]) for x in node(sp, s1)["conflicts"]] == [(pid, c2)]
            rest("POST", f"/api/plans/{pid}/start", {"executor": "builtin"})
            v = wait_nodes([(pid, c1), (pid, c2), (sp, s1)])
            assert all(x["status"] == "done" for x in v.values()), v
            assert v[(pid, c2)]["finished_at"] <= v[(sp, s1)]["started_at"], "(root, 2) must run before (sub, 1)"
            print("cross tree OK")

            # 8) ordering under pressure: the lower conflicting node waits for a busy profile;
            #    the higher one must wait its turn instead of overtaking
            one = profile("one", harness="mock", params={"speed": 4}, max_parallel=1)
            pid, (o1, o2, o3) = await plan("pressure", [None, ["p.txt"], ["p.txt"]], start=False)
            for n in (o1, o2):
                rest("PUT", f"/api/plans/{pid}/nodes/{n}/agent", {"agent": one["id"]})
            rest("POST", f"/api/plans/{pid}/start", {"executor": "builtin"})
            wait_for(lambda: node(pid, o1)["status"] == "running")
            time.sleep(0.3)
            v2, v3 = node(pid, o2), node(pid, o3)
            assert v2["status"] == "ready" and v2["blocked_by"] is None, v2  # held by the profile cap, not files
            assert v3["status"] == "ready" and v3["blocked_by"] == {
                "plan_id": pid, "node_id": o2, "title": "N2", "files": ["p.txt"], "reason": "turn"}, v3
            v = wait_nodes([(pid, n) for n in (o1, o2, o3)])
            r1, r2, r3 = (v[(pid, n)] for n in (o1, o2, o3))
            assert r1["finished_at"] <= r2["started_at"] and r2["finished_at"] <= r3["started_at"], (r1, r2, r3)
            print("pressure OK      3 waited for 2, 2 waited for the profile")

            # 9) external executor: claim_next_node respects a claimed-but-unfinished holder
            pid, (e1, e2, e3) = await plan("external", [["e.txt"], ["E.TXT"], None], start=False)
            await call("start_plan", plan_id=pid, executor="external")
            await asyncio.sleep(0.2)
            job = await call("claim_next_node", agent_name="ext-1", plan_id=pid)
            assert job["node_id"] == e1 and "e.txt" in job["context"], job
            job = await call("claim_next_node", agent_name="ext-2", plan_id=pid)
            assert job["node_id"] == e3, job  # the file-less node is handed out, node 2 is not
            assert await call("claim_next_node", agent_name="ext-3", plan_id=pid) is None
            assert node(pid, e2)["blocked_by"]["node_id"] == e1
            await call("complete_node", plan_id=pid, node_id=e1, result="1 ok")
            await asyncio.sleep(0.2)
            job = await call("claim_next_node", agent_name="ext-3", plan_id=pid)
            assert job["node_id"] == e2, job
            await call("complete_node", plan_id=pid, node_id=e2, result="2 ok")
            await call("complete_node", plan_id=pid, node_id=e3, result="3 ok")
            await asyncio.sleep(0.2)
            assert (await call("get_plan", plan_id=pid))["status"] == "done"
            print("external OK")

            # 10) starvation: a lower-numbered conflicting node left 'ready' inside a subplan that
            #     failed is never spawned; it must not hold a higher node of the tree back forever
            dead = profile("dead", harness="openai", model="none", base_url="http://127.0.0.1:9/v1",
                           params={"max_retries": 0, "timeout": 2})
            slow = profile("slow", harness="mock", params={"speed": 3}, max_parallel=1)
            pid, (k1, k2, k3) = await plan("strand", [None, None, None], start=False)
            sa, (a1, a2) = await plan("strand A", [None, ["z.txt"]], parent=(pid, k1))
            sb, (b1,) = await plan("strand B", [["z.txt"]], parent=(pid, k2))
            assert sa < sb
            rest("PUT", f"/api/plans/{sa}/nodes/{a1}/agent", {"agent": dead["id"]})
            rest("PUT", f"/api/plans/{sa}/nodes/{a2}/agent", {"agent": slow["id"]})
            rest("PUT", f"/api/plans/{pid}/nodes/{k3}/agent", {"agent": slow["id"]})
            rest("POST", f"/api/plans/{pid}/start", {"executor": "builtin"})
            v = wait_nodes([(sa, a1), (pid, k1), (pid, k3)], timeout=30)
            assert v[(sa, a1)]["status"] == "failed" and v[(pid, k1)]["status"] == "failed", v
            # (sub A, 2) may legitimately have been spawned before its container failed -- who
            # wins the single 'slow' slot first is a race. What must never happen is the
            # scheduler starting it *after* the failure, since _advance no longer descends into
            # that subplan and _files_turn_pending already lets higher nodes past it. Assert that
            # property rather than the snapshot, so the check does not depend on the race.
            was = node(sa, a2)
            time.sleep(3)
            now = node(sa, a2)
            if was["status"] == "ready":
                assert now["status"] == "ready", now        # stranded: never spawned afterwards
            else:
                assert now["started_at"] == was["started_at"], now   # not restarted
            v = wait_nodes([(sb, b1)], timeout=20)
            assert v[(sb, b1)]["status"] == "done", v
            print("strand OK        (sub B, 1) ran although (sub A, 2) is stranded 'ready'")
        finally:
            for p in plans:
                try:
                    rest("POST", f"/api/plans/{p}/stop")
                    rest("DELETE", f"/api/plans/{p}")
                except RuntimeError as e:
                    print("cleanup:", e)
            for p in profiles:
                try:
                    rest("DELETE", f"/api/agents/{p['id']}")
                except RuntimeError as e:
                    print("cleanup:", e)
    print("file conflicts OK")


store_rules()
asyncio.run(main())
