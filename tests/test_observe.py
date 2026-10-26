"""The slim /mcp/observe endpoint for a session tracking its own work, against a running --mock server.

It offers exactly the eleven self-tracking tools; its submit_plan starts a top-level plan with the
external executor at once and leaves a subplan to run with its parent; a full round (claim,
complete) records the diff of the step's declared file, which export_plan shows; and an auto-start that fails (another
running plan in the folder) leaves nothing behind.

Run:  uv run clearbox --mock      (one shell)
      uv run python tests/test_observe.py [http://127.0.0.1:<port>]  (default: $CLEARBOX_PORT, else 8765)
"""
import asyncio, json, os, sys, tempfile, time, urllib.request
from pathlib import Path

from mcp.client.client import Client

URL = (sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{os.environ.get('CLEARBOX_PORT', '8765')}").rstrip("/")

TOOLS = {"submit_plan", "claim_next_node", "report_progress", "complete_node", "fail_node",
         "add_node", "add_edge", "remove_node", "remove_edge", "get_plan", "export_plan"}


def rest(path):
    with urllib.request.urlopen(URL + path) as r:
        return json.loads(r.read())


async def main():
    work = Path(tempfile.mkdtemp(prefix="cb-test-"))
    (work / "notes.txt").write_text("one\n")
    async with Client(URL + "/mcp/observe") as c:
        async def call(name, /, **a):
            r = await c.call_tool(name, a)
            if r.is_error:
                raise RuntimeError(r.content[0].text)
            sc = r.structured_content
            return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)

        names = {t.name for t in (await c.list_tools()).tools}
        assert names == TOOLS, sorted(names ^ TOOLS)
        assert "claim_next_node" in c.instructions and "/#/plan/" in c.instructions, c.instructions
        print("ok  exactly the eleven tools, workflow instructions")

        out = await call("submit_plan", title="Observe test", working_directory=str(work),
                         nodes=[{"key": "edit", "title": "Edit notes", "files": ["notes.txt"]},
                                {"key": "more", "title": "Expand later"}],
                         edges=[["edit", "more"]])
        pid, edit, more = out["plan_id"], out["nodes"]["edit"], out["nodes"]["more"]
        assert "external" in out["started"], out
        root = rest(f"/api/plans/{pid}")["root"]
        assert root["started"] and root["executor"] == "external", root
        print("ok  submit_plan started the top-level plan as external")

        node = await call("claim_next_node", agent_name="t", plan_id=pid)
        assert node and (node["plan_id"], node["node_id"]) == (pid, edit), node
        await call("report_progress", plan_id=pid, node_id=edit, message="editing")
        (work / "notes.txt").write_text("one\ntwo\n")
        assert await call("complete_node", plan_id=pid, node_id=edit, result="added a line") == "ok"
        diffs = []
        for _ in range(50):
            diffs = rest(f"/api/plans/{pid}/nodes/{edit}/diffs")["diffs"]
            if diffs:
                break
            time.sleep(0.1)
        assert [d["path"] for d in diffs] == ["notes.txt"] and "+two" in diffs[0]["diff"], diffs
        md = await call("export_plan", plan_id=pid)
        assert md.startswith("# Observe test") and "<summary>notes.txt (+1 −0)</summary>" in md, md
        print("ok  claim and complete, with the declared file's diff, in the export too")

        node = await call("claim_next_node", agent_name="t", plan_id=pid)
        assert node and node["node_id"] == more, node
        sub = await call("submit_plan", title="Sub", parent_plan_id=pid, parent_node_id=more,
                         nodes=[{"key": "a", "title": "Sub step"}])
        assert "parent" in sub["started"], sub
        assert rest(f"/api/plans/{sub['plan_id']}")["plan"]["started"] in (0, False, None)
        await call("complete_node", plan_id=pid, node_id=more, result="expanded")  # its subplan runs now
        node = await call("claim_next_node", agent_name="t", plan_id=pid)
        assert node and (node["plan_id"], node["node_id"]) == (sub["plan_id"], sub["nodes"]["a"]), node
        await call("complete_node", plan_id=sub["plan_id"], node_id=node["node_id"], result="done")
        plan = await call("get_plan", plan_id=pid)
        assert [n["status"] for n in plan["nodes"]] == ["done", "done"], plan["nodes"]
        print("ok  a subplan is not started on its own and runs with its parent")

        # an auto-start that fails is all or nothing: a second plan in a folder still in use
        await call("add_node", plan_id=pid, title="Keep it running")
        before = {p["id"] for p in rest("/api/plans")}
        try:
            await call("submit_plan", title="Clash", working_directory=str(work),
                       nodes=[{"key": "x", "title": "X"}])
        except RuntimeError as e:
            assert "nothing was created" in str(e), e
        else:
            raise AssertionError("a second plan in a busy folder was started")
        assert {p["id"] for p in rest("/api/plans")} == before
        print("ok  a failed auto-start leaves no plan behind")


if __name__ == "__main__":
    asyncio.run(main())
