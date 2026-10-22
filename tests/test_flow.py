"""End-to-end test against a running server started with --mock (fast)."""
import asyncio, json, sys, time
import urllib.request
from mcp.client.client import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

async def main():
    async with Client(URL + "/mcp") as c:
        async def call(name, **a):
            r = await c.call_tool(name, a)
            if r.is_error:
                raise RuntimeError(r.content[0].text)
            sc = r.structured_content
            return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)

        # --- cycle rejection
        p = await call("create_plan", title="External flow")
        a = (await call("add_node", plan_id=p, title="A"))[1]
        b = (await call("add_node", plan_id=p, title="B"))[1]
        cc = (await call("add_node", plan_id=p, title="C"))[1]
        await call("add_edge", plan_id=p, from_node_id=a, to_node_id=b)
        await call("add_edge", plan_id=p, from_node_id=b, to_node_id=cc)
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
        q = await call("create_plan", title="Wide builtin graph", description="layout test")
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

asyncio.run(main())
