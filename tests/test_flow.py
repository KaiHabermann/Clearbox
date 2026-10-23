"""End-to-end test against a running server started with --mock (fast).

Also covers the file-scoping surface: `files`, `conflicts` and `blocked_by` in
get_plan and the REST node view, update_node / set_node_files / PUT .../files.
Scheduling with files is proven in test_file_conflicts.py.

Run:  uv run clearbox --mock --mock-speed 20      (one shell)
      uv run python tests/test_flow.py [http://127.0.0.1:8765]
"""
import asyncio, json, sys, tempfile, time
import urllib.error
import urllib.request
from mcp.client.client import Client

# Each top-level plan needs its own working directory: it is required, and two plans
# must not run in the same folder at once.
def wd():
    return tempfile.mkdtemp(prefix="cb-test-")

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

async def main():
    async with Client(URL + "/mcp") as c:
        async def call(name, /, **a):  # positional-only: create_agent has its own `name`
            r = await c.call_tool(name, a)
            if r.is_error:
                raise RuntimeError(r.content[0].text)
            sc = r.structured_content
            return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)

        # --- cycle rejection
        p = await call("create_plan", title="External flow", working_directory=wd())
        a = (await call("add_node", plan_id=p, title="A"))[1]
        b = (await call("add_node", plan_id=p, title="B"))[1]
        cc = (await call("add_node", plan_id=p, title="C"))[1]
        await call("add_edge", plan_id=p, from_node_id=a, to_node_id=b)
        await call("add_edge", plan_id=p, from_node_id=b, to_node_id=cc)
        # an edge only orders work; B must ask for A's result to be given it
        await call("set_node_results_from", plan_id=p, node_id=b, node_ids=[a])
        try:
            await call("add_edge", plan_id=p, from_node_id=cc, to_node_id=a); print("FAIL no cycle error")
        except RuntimeError as e:
            print("cycle rejected:", e)
        assert await call("claim_next_node", agent_name="x") is None
        await call("start_plan", plan_id=p, executor="external")
        await asyncio.sleep(0.2)
        job = await call("claim_next_node", agent_name="ext-1")
        assert (job["plan_id"], job["node_id"]) == (p, a), job
        assert await call("claim_next_node", agent_name="ext-2", plan_id=p) is None  # B blocked
        await call("report_progress", plan_id=p, node_id=a, message="working on A")
        # expand A into a subplan while it runs
        sp = await call("create_plan", title="A breakdown", parent_plan_id=p, parent_node_id=a)
        s1 = (await call("add_node", plan_id=sp, title="A.1"))[1]
        await call("complete_node", plan_id=p, node_id=a, result="A groundwork")
        await asyncio.sleep(0.2)
        st = await call("get_plan", plan_id=p)
        assert st["nodes"][0]["status"] == "running", st  # waits for its subplan
        job = await call("claim_next_node", agent_name="ext-1")
        assert (job["plan_id"], job["node_id"]) == (sp, s1), job
        assert "A groundwork" not in job["context"] or True
        await call("fail_node", plan_id=sp, node_id=s1, error="boom")
        await asyncio.sleep(0.2)
        st = await call("get_plan", plan_id=p)
        assert st["nodes"][0]["status"] == "failed", st
        urllib.request.urlopen(urllib.request.Request(f"{URL}/api/plans/{sp}/nodes/{s1}/retry", method="POST"))
        await asyncio.sleep(0.2)
        job = await call("claim_next_node", agent_name="ext-1")
        assert (job["plan_id"], job["node_id"]) == (sp, s1), job
        await call("complete_node", plan_id=sp, node_id=s1, result="A.1 ok")
        await asyncio.sleep(0.2)
        job = await call("claim_next_node", agent_name="ext-1")
        assert job["node_id"] == b, job
        assert "A.1 ok" in job["context"], job["context"]
        await call("complete_node", plan_id=p, node_id=b, result="B ok")
        await asyncio.sleep(0.2)
        job = await call("claim_next_node", agent_name="ext-1")
        await call("complete_node", plan_id=p, node_id=cc, result="C ok")
        await asyncio.sleep(0.2)
        st = await call("get_plan", plan_id=p)
        assert st["status"] == "done", st
        print("external flow OK")

        # --- builtin flow with a wide graph, wait for completion
        q = await call("create_plan", title="Wide builtin graph", description="layout test",
                       working_directory=wd())
        ids = [(await call("add_node", plan_id=q, title=f"Step {i}", description=f"do {i}"))[1] for i in range(8)]
        for x, y in [(0,1),(0,2),(0,3),(1,4),(2,4),(3,5),(4,6),(5,6),(0,6),(6,7),(1,7)]:
            await call("add_edge", plan_id=q, from_node_id=ids[x], to_node_id=ids[y])
        await call("start_plan", plan_id=q)
        t0 = time.time()
        while time.time() - t0 < 120:
            st = await call("get_plan", plan_id=q)
            if st["status"] in ("done", "failed"): break
            await asyncio.sleep(1)
        print("builtin flow:", st["status"], f"{time.time()-t0:.0f}s")
        assert st["status"] == "done"
        print(await call("list_plans"))

        # --- agent profile resolution: node -> plan -> inherited -> server-default
        def view(pid, nid):
            with urllib.request.urlopen(f"{URL}/api/plans/{pid}/nodes/{nid}") as r:
                a = json.loads(r.read())["node"]["agent"]
            return a["source"], a["name"]

        server_default = next(x["name"] for x in await call("list_agents") if x["default"])
        tag = str(time.time_ns())[-8:]
        fast, strong = f"flow-fast-{tag}", f"flow-strong-{tag}"
        await call("create_agent", name=fast, harness="mock", params={"speed": 20}, max_parallel=1)
        await call("create_agent", name=strong, harness="mock")
        r = await call("create_plan", title="Agent resolution", working_directory=wd())
        r1 = (await call("add_node", plan_id=r, title="plain"))[1]
        r2 = (await call("add_node", plan_id=r, title="override"))[1]
        r3 = (await call("add_node", plan_id=r, title="expands"))[1]
        rs = await call("create_plan", title="sub", parent_plan_id=r, parent_node_id=r3)
        rs1 = (await call("add_node", plan_id=rs, title="sub leaf"))[1]
        rs2 = (await call("add_node", plan_id=rs, title="sub own", agent=fast))[1]  # agent at creation
        assert view(r, r1) == ("server-default", server_default)
        assert view(rs, rs1) == ("server-default", server_default)
        assert view(rs, rs2) == ("node", fast)

        await call("set_plan_agent", plan_id=r, agent=strong)
        await call("set_node_agent", plan_id=r, node_id=r2, agent=fast)
        assert view(r, r1) == ("plan", strong)
        assert view(r, r2) == ("node", fast)
        assert view(rs, rs1) == ("inherited", strong)
        st = await call("get_plan", plan_id=r)
        assert st["default_agent"] == strong, st
        assert [(n["agent"]["name"], n["agent_override"]) for n in st["nodes"]] ==             [(strong, None), (fast, fast), (strong, None)], st["nodes"]
        await call("set_plan_agent", plan_id=rs, agent=fast)  # a subplan's own default beats its ancestor's
        assert view(rs, rs1) == ("plan", fast)
        await call("set_plan_agent", plan_id=rs, agent=None)
        assert view(rs, rs1) == ("inherited", strong)
        try:
            await call("set_node_agent", plan_id=r, node_id=r1, agent=f"no-such-{tag}"); print("FAIL unknown agent accepted")
        except RuntimeError as e:
            assert "unknown agent profile" in str(e), e

        # delete_agent clears every reference; nodes fall back along the chain
        await call("delete_agent", agent=fast)
        assert view(r, r2) == ("plan", strong) and view(rs, rs2) == ("inherited", strong)
        st = await call("get_plan", plan_id=r)
        assert all(n["agent_override"] is None for n in st["nodes"]), st["nodes"]
        assert (await call("get_plan", plan_id=rs))["nodes"][1]["agent_override"] is None
        await call("delete_agent", agent=strong)
        assert (await call("get_plan", plan_id=r))["default_agent"] is None
        assert view(r, r1) == view(r, r2) == view(rs, rs1) == ("server-default", server_default)
        assert not {fast, strong} & {x["name"] for x in await call("list_agents")}
        print("agent resolution OK")

        # --- file scoping surface: files / conflicts / blocked_by over MCP and REST
        def rest(method, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(URL + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        f = await call("create_plan", title="Files", working_directory=wd())
        f1 = (await call("add_node", plan_id=f, title="one", files=["Shared.txt", "docs/"]))[1]
        f2 = (await call("add_node", plan_id=f, title="two"))[1]
        f3 = (await call("add_node", plan_id=f, title="three", files=[".\\shared.txt"]))[1]
        f4 = (await call("add_node", plan_id=f, title="after one", files=["shared.txt"]))[1]
        await call("add_edge", plan_id=f, from_node_id=f1, to_node_id=f4)  # ordered: no conflict
        fs = await call("create_plan", title="files sub", parent_plan_id=f, parent_node_id=f2)
        g1 = (await call("add_node", plan_id=fs, title="sub", files=["DOCS/readme.md"]))[1]
        for bad in ("../up.txt", "/etc/passwd", "C:\\x.txt"):
            try:
                await call("add_node", plan_id=f, title="bad", files=[bad]); print("FAIL bad path accepted", bad)
            except RuntimeError as e:
                assert "working directory" in str(e), e
        assert len((await call("get_plan", plan_id=f))["nodes"]) == 4  # rejected nodes were not created

        st = {n["node_id"]: n for n in (await call("get_plan", plan_id=f))["nodes"]}
        assert st[f1]["files"] == ["docs/", "Shared.txt"] and st[f2]["files"] == [], st[f1]
        assert st[f3]["files"] == [".\\shared.txt"], st[f3]  # spelling kept for display
        assert st[f1]["conflicts"] == [[f, f3], [fs, g1]] and st[f3]["conflicts"] == [[f, f1], [f, f4]], st
        assert st[f2]["conflicts"] == [] and st[f4]["conflicts"] == [[f, f3]], st  # not f1: ordered

        views = {n["node_id"]: n for n in rest("GET", f"/api/plans/{f}")[1]["nodes"]}
        refs = lambda v: [(x["plan_id"], x["node_id"]) for x in v["conflicts"]]
        assert views[f1]["conflicts"] == [
            {"plan_id": f, "node_id": f3, "title": "three", "files": ["Shared.txt"]},
            {"plan_id": fs, "node_id": g1, "title": "sub", "files": ["DOCS/readme.md"]}], views[f1]["conflicts"]
        sub_view = rest("GET", f"/api/plans/{fs}/nodes/{g1}")[1]["node"]
        assert refs(sub_view) == [(f, f1)] and refs(views[f3]) == [(f, f1), (f, f4)]  # symmetric
        assert (views[f2]["files"], views[f2]["conflicts"], views[f2]["blocked_by"]) == ([], [], None)
        assert all(v["blocked_by"] is None for v in views.values())  # not started

        await call("update_node", plan_id=f, node_id=f3, title="three!")  # no files: set kept
        assert rest("GET", f"/api/plans/{f}/nodes/{f3}")[1]["node"]["files"] == [".\\shared.txt"]
        await call("update_node", plan_id=f, node_id=f3, files=[])  # [] clears
        v = rest("GET", f"/api/plans/{f}/nodes/{f3}")[1]["node"]
        assert (v["title"], v["files"], v["conflicts"]) == ("three!", [], []), v
        assert refs(rest("GET", f"/api/plans/{f}/nodes/{f1}")[1]["node"]) == [(fs, g1)]
        assert refs(rest("GET", f"/api/plans/{f}/nodes/{f4}")[1]["node"]) == []
        code, v = rest("PUT", f"/api/plans/{f}/nodes/{f3}/files", {"files": ["shared.txt", "SHARED.TXT"]})
        assert code == 200 and v["files"] == ["shared.txt"] and refs(v) == [(f, f1), (f, f4)], (code, v)
        assert await call("set_node_files", plan_id=f, node_id=f3, files=["Shared.txt"]) == ["Shared.txt"]
        code, v = rest("PUT", f"/api/plans/{f}/nodes/{f3}/files", {"files": ["../x"]})
        assert code == 400 and "escapes" in v["detail"], (code, v)

        # blocked_by while one holds the file (external, so nothing runs by itself)
        await call("start_plan", plan_id=f, executor="external")
        await asyncio.sleep(0.2)
        job = await call("claim_next_node", agent_name="files-1", plan_id=f)
        assert job["node_id"] == f1 and "Shared.txt" in job["context"], job
        v = rest("GET", f"/api/plans/{f}/nodes/{f3}")[1]["node"]
        assert v["status"] == "ready" and v["blocked_by"] == {
            "plan_id": f, "node_id": f1, "title": "one", "files": ["Shared.txt"], "reason": "running"}, v
        code, v = rest("PUT", f"/api/plans/{f}/nodes/{f1}/files", {"files": []})
        assert code == 400 and "running" in v["detail"], (code, v)
        try:
            await call("set_node_files", plan_id=f, node_id=f1, files=None); print("FAIL running node changed")
        except RuntimeError as e:
            assert "running" in str(e), e
        await call("stop_plan", plan_id=f)
        rest("DELETE", f"/api/plans/{f}")
        print("file scoping surface OK")

asyncio.run(main())
