"""Worker agents' scoped MCP endpoint, against a running --mock server.

A built-in worker gets /mcp-agent/<token> instead of /mcp: the token names the node it
runs and is revoked when the run ends. This drives that endpoint the way an agent would
(a --mock server serves a running node's url on a debug route; a very slow mock profile
keeps the run alive meanwhile) and checks that it can expand its own node and build below
it, sees no agent-admin or execution tools, cannot reach any other plan or node, pins its
subplan to its own agent, and loses access once the run ends.

Run:  uv run clearbox --mock      (one shell)
      uv run python tests/test_agent_scope.py [http://127.0.0.1:<port>]  (default: $CLEARBOX_PORT, else 8765)
"""
import asyncio, json, os, sys, tempfile, time, urllib.error, urllib.request, uuid

from mcp.client.client import Client

from clearbox.mcp_bridge import McpToolBridge

URL = (sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{os.environ.get('CLEARBOX_PORT', '8765')}").rstrip("/")

AGENT_TOOLS = {"submit_plan", "create_plan", "add_node", "add_edge", "remove_edge", "remove_node",
               "update_node", "get_plan", "list_agents", "report_progress"}


def wd():
    return tempfile.mkdtemp(prefix="cb-test-")


def rest(path, method="GET", body=None):
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def caller(c):
    async def call(name, /, **a):
        r = await c.call_tool(name, a)
        if r.is_error:
            raise RuntimeError(r.content[0].text)
        sc = r.structured_content
        return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)
    return call


async def refused(call, name, /, **a):
    try:
        await call(name, **a)
    except Exception as e:  # noqa: BLE001 -- a tool error or an unreachable endpoint, either is a refusal
        return str(e)
    raise AssertionError(f"{name}({a}) was allowed")


def scoped_url(plan_id, node_id, timeout=30):
    """The run's scoped endpoint, from the --mock-only debug route (never from the transcript)."""
    end = time.time() + timeout
    while time.time() < end:
        code, d = rest(f"/api/debug/plans/{plan_id}/nodes/{node_id}/agent-mcp-url")
        assert code == 200, (code, d)
        if d["url"]:
            return d["url"]
        time.sleep(0.2)
    raise AssertionError(f"node ({plan_id}, {node_id}) never started with an agent endpoint")


async def main():
    slow = f"scope-slow-{uuid.uuid4().hex[:6]}"
    async with Client(URL + "/mcp") as c:
        call = caller(c)
        # mock speed 0.02: the run sits in its first sleep for 25-75 s, long enough for the test
        code, prof = rest("/api/agents", "POST", dict(name=slow, harness="mock", params={"speed": 0.02},
                                                      capability="read"))
        assert code == 200, (code, prof)
        pid = await call("create_plan", title="Scope test", working_directory=wd())
        a = (await call("add_node", plan_id=pid, title="A", description="the worker", agent=slow))[1]
        b = (await call("add_node", plan_id=pid, title="B", description="a sibling"))[1]
        other = await call("create_plan", title="Someone else's plan", working_directory=wd())
        o = (await call("add_node", plan_id=other, title="O", description="not yours"))[1]
        try:
            await call("start_plan", plan_id=pid)
            url = scoped_url(pid, a)
            assert "/mcp-agent/" in url, url
            print("worker got a scoped endpoint:", url.rsplit("/", 1)[0] + "/<token>")

            async with Client(url) as w:
                names = {t.name for t in (await w.list_tools()).tools}
                assert names == AGENT_TOOLS, f"scoped tools differ: {sorted(names ^ AGENT_TOOLS)}"
                print("scoped tool set:", ", ".join(sorted(names)))
                wcall = caller(w)

                # expand its own node and build there
                sub = await wcall("create_plan", title="A, broken down", parent_plan_id=pid, parent_node_id=a)
                s1 = (await wcall("add_node", plan_id=sub, title="A1", description="first half"))[1]
                s2 = (await wcall("add_node", plan_id=sub, title="A2", description="second half", files=["x.txt"]))[1]
                await wcall("add_edge", plan_id=sub, from_node_id=s1, to_node_id=s2)
                await wcall("update_node", plan_id=sub, node_id=s2, description="second half, revised")
                await wcall("update_node", plan_id=sub, node_id=s2, results_from=[s1], kind="agent")
                s3 = (await wcall("add_node", plan_id=sub, title="tmp", description="removed again"))[1]
                await wcall("remove_node", plan_id=sub, node_id=s3)
                nested = await wcall("create_plan", title="A1, broken down", parent_plan_id=sub, parent_node_id=s1)
                await wcall("add_node", plan_id=nested, title="A1a", description="deeper")
                view = await wcall("get_plan", plan_id=sub)
                assert [n["title"] for n in view["nodes"]] == ["A1", "A2"], view["nodes"]
                assert view["edges"] == [[s1, s2]], view["edges"]
                await wcall("report_progress", message="expanded into a subplan")
                assert any(p["name"] == slow for p in await wcall("list_agents"))
                print("expanded its own node: subplan", sub, "with nested subplan", nested)

                # openai-harness agents reach the same endpoint through the vc_ bridge
                bridge = McpToolBridge(url)
                assert {s.name for s in await bridge.specs()} == {"vc_" + n for n in AGENT_TOOLS}
                text, is_error = await bridge.call("vc_get_plan", {"plan_id": sub})
                assert not is_error and json.loads(text)["plan_id"] == sub, text
                text, is_error = await bridge.call("vc_create_agent", {"name": "x", "harness": "mock"})
                assert is_error, text
                print("vc_ bridge: same scoped tool set")

                # the subplans run on the creating node's agent, and the worker cannot pick another
                for p in (sub, nested):
                    v = await call("get_plan", plan_id=p)
                    assert all(n["agent"]["name"] == slow for n in v["nodes"]), v["nodes"]
                assert (await call("get_plan", plan_id=sub))["default_agent"] == slow
                print("subplan agent: inherited", slow)
                # an `agent` argument is refused outright, not dropped
                for name, args in [("add_node", dict(plan_id=sub, title="X", agent="mock")),
                                   ("update_node", dict(plan_id=sub, node_id=s1, agent="mock")),
                                   ("submit_plan", dict(title="X", parent_plan_id=sub, parent_node_id=s2,
                                                        nodes=[{"key": "x", "title": "X", "agent": "mock"}]))]:
                    assert "agent is not accepted" in await refused(wcall, name, **args), name
                assert [n["title"] for n in (await call("get_plan", plan_id=sub))["nodes"]] == ["A1", "A2"]
                assert (await call("get_plan", plan_id=sub))["nodes"][0]["agent_override"] is None
                await refused(wcall, "set_node_agent", plan_id=sub, node_id=s1, agent="mock")

                # submit_plan: a whole nested subplan in one call, on the creating node's agent
                got = await wcall("submit_plan", title="A2, broken down", parent_plan_id=sub, parent_node_id=s2,
                                  nodes=[{"key": "p", "title": "A2a", "files": ["y.txt"]},
                                         {"key": "q", "title": "A2b", "results_from": ["p"]}],
                                  edges=[["p", "q"]])
                v = await call("get_plan", plan_id=got["plan_id"])
                assert v["parent"] == [sub, s2] and v["default_agent"] == slow, v
                assert v["edges"] == [[got["nodes"]["p"], got["nodes"]["q"]]], v["edges"]
                assert all(n["agent"]["name"] == slow for n in v["nodes"]), v["nodes"]
                assert v["nodes"][1]["results_from"] == [got["nodes"]["p"]], v["nodes"]
                print("scoped submit_plan: subplan", got["plan_id"], "on", slow)

                # nothing outside its own subtree, and no admin or execution tools
                for name, args in [
                    ("add_node", dict(plan_id=pid, title="X")),
                    ("add_node", dict(plan_id=other, title="X")),
                    ("add_edge", dict(plan_id=pid, from_node_id=a, to_node_id=b)),
                    ("update_node", dict(plan_id=pid, node_id=b, description="hijacked")),
                    ("remove_node", dict(plan_id=other, node_id=o)),
                    ("get_plan", dict(plan_id=pid)),
                    ("get_plan", dict(plan_id=other)),
                    ("create_plan", dict(title="X", parent_plan_id=pid, parent_node_id=b)),
                    ("create_plan", dict(title="X", parent_plan_id=other, parent_node_id=o)),
                    ("submit_plan", dict(title="X", parent_plan_id=pid, parent_node_id=b,
                                         nodes=[{"key": "x", "title": "X"}])),
                    ("submit_plan", dict(title="X", parent_plan_id=other, parent_node_id=o)),
                    ("create_agent", dict(name="escalated", harness="mock", capability="full")),
                    ("start_plan", dict(plan_id=other)),
                    ("set_plan_workdir", dict(plan_id=other, working_directory=wd())),
                    ("rework_node", dict(plan_id=pid, node_id=b, instructions="x")),
                    ("complete_node", dict(plan_id=pid, node_id=b, result="x")),
                ]:
                    await refused(wcall, name, **args)
                sibling = next(n for n in (await call("get_plan", plan_id=pid))["nodes"] if n["node_id"] == b)
                assert sibling["description"] == "a sibling", sibling
                print("outside its subtree and admin tools: all refused")

            # the run ends -> the token stops working
            code, _ = rest(f"/api/plans/{pid}/nodes/{a}/stop", "POST")
            assert code == 200, code
            await refused_client(url)
            code, _ = rest(url[len(URL):], "POST", {})
            assert code == 404, code
            print("token after the run ended: 404")

            # an unknown token never worked
            bogus = URL + "/mcp-agent/not-a-real-token"
            await refused_client(bogus)
            code, _ = rest(bogus[len(URL):], "POST", {})
            assert code == 404, code
            print("unknown token: 404")
        finally:
            await call("stop_plan", plan_id=pid)
            rest(f"/api/agents/{prof['id']}", "DELETE")
    print("OK")


async def refused_client(url):
    try:
        async with Client(url) as c:
            await c.list_tools()
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    raise AssertionError(f"{url} still answers")


asyncio.run(main())
