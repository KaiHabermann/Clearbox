"""REST /api/agents test against a running server started with --mock.

Covers create / list / patch (api_key kept, then cleared) / test / node + plan
assignment with the resolved `agent.source` / delete, the "agents" SSE message,
and -- explicitly -- that no response body ever contains `api_key` or the secret.

Run:  uv run clearbox --mock      (one shell)
      uv run python tests/test_agents_api.py [http://127.0.0.1:8765]
"""
import asyncio, json, sys, tempfile, threading, time, uuid
import urllib.error
import urllib.request
from mcp.client.client import Client

# Each top-level plan needs its own working directory: it is required, and two plans
# must not run in the same folder at once.
def wd():
    return tempfile.mkdtemp(prefix="cb-test-")

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
SECRET = "sk-test-" + uuid.uuid4().hex
bodies: list[str] = []  # every REST response body, scanned for leaks at the end


def rest(method, path, body=None, expect=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()
    bodies.append(raw)
    assert status == expect, (method, path, status, raw)
    return json.loads(raw)


def sse_listener(seen: list, stop: threading.Event):
    with urllib.request.urlopen(URL + "/api/stream") as r:
        while not stop.is_set():
            line = r.readline().decode()
            if line.startswith("data:"):
                seen.append(json.loads(line[5:]))


async def main():
    seen: list = []
    stop = threading.Event()
    threading.Thread(target=sse_listener, args=(seen, stop), daemon=True).start()
    time.sleep(0.3)

    async with Client(URL + "/mcp") as c:
        async def call(name, **a):
            r = await c.call_tool(name, a)
            sc = r.structured_content
            return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)

        p = await call("create_plan", title="Agents API", working_directory=wd())
        sp_node = (await call("add_node", plan_id=p, title="parent"))[1]
        n2 = (await call("add_node", plan_id=p, title="leaf"))[1]
        sp = await call("create_plan", title="sub", parent_plan_id=p, parent_node_id=sp_node)
        s1 = (await call("add_node", plan_id=sp, title="sub leaf"))[1]

    tag = uuid.uuid4().hex[:6]
    # --- create (literal api_key accepted, never echoed)
    a = rest("POST", "/api/agents", {"name": f"cheap-{tag}", "harness": "openai", "model": "m-1",
                                     "base_url": "http://127.0.0.1:9/v1", "api_key": SECRET,
                                     "params": {"temperature": 0}, "max_parallel": 2})
    aid = a["id"]
    assert a["has_api_key"] is True and a["default"] is False and a["in_use"] == 0, a
    rest("POST", "/api/agents", {"name": f"cheap-{tag}", "harness": "openai"}, expect=400)  # duplicate
    rest("POST", "/api/agents", {"name": f"bad-{tag}", "harness": "nope"}, expect=400)      # bad harness
    m = rest("POST", "/api/agents", {"name": f"mock-{tag}", "harness": "mock", "params": {"speed": 0.05}})

    lst = rest("GET", "/api/agents")
    assert {aid, m["id"]} <= {x["id"] for x in lst}, lst

    # --- patch without api_key keeps it; api_key="" clears it
    a = rest("PATCH", f"/api/agents/{aid}", {"model": "m-2"})
    assert a["model"] == "m-2" and a["has_api_key"] is True and a["params"] == {"temperature": 0}, a
    a = rest("PATCH", f"/api/agents/{aid}", {"api_key": None})
    assert a["has_api_key"] is True, a
    a = rest("PATCH", f"/api/agents/{aid}", {"api_key": ""})
    assert a["has_api_key"] is False, a
    a = rest("PATCH", f"/api/agents/{aid}", {"api_key": SECRET})
    assert a["has_api_key"] is True, a
    rest("PATCH", "/api/agents/999999", {"model": "x"}, expect=404)
    rest("PATCH", f"/api/agents/{aid}", {"harness": "nope"}, expect=400)

    # --- connection test: mock answers, a dead local endpoint reports a readable error
    t = rest("POST", f"/api/agents/{m['id']}/test")
    assert t["ok"] is True and t["agent"]["harness"] == "mock", t
    t = rest("POST", f"/api/agents/{aid}/test")
    assert t["ok"] is False and "could not connect" in t["detail"] and t["credential"] == "literal", t
    rest("POST", "/api/agents/999999/test", expect=404)

    # --- resolution sources: server-default -> plan -> inherited -> node
    def agent_of(plan_id, node_id):
        return rest("GET", f"/api/plans/{plan_id}/nodes/{node_id}")["node"]

    node = agent_of(p, n2)
    assert node["agent"]["source"] == "server-default" and node["agent_override"] is None, node
    rest("PUT", f"/api/plans/{p}/agent", {"agent": f"mock-{tag}"})
    assert agent_of(p, n2)["agent"]["source"] == "plan"
    sub = agent_of(sp, s1)["agent"]
    assert sub["source"] == "inherited" and sub["profile_id"] == m["id"], sub
    node = rest("PUT", f"/api/plans/{p}/nodes/{n2}/agent", {"agent": aid})
    assert node["agent_override"] == aid and node["agent"]["source"] == "node", node
    assert node["agent"]["name"] == f"cheap-{tag}" and node["agent"]["model"] == "m-2", node
    plan = rest("GET", f"/api/plans/{p}")
    assert plan["default_agent"]["id"] == m["id"], plan["default_agent"]
    assert [x["agent"]["source"] for x in plan["nodes"]] == ["plan", "node"], plan["nodes"]
    rest("PUT", f"/api/plans/{p}/nodes/{n2}/agent", {"agent": "no-such-agent"}, expect=400)
    in_use = {x["id"]: x["in_use"] for x in rest("GET", "/api/agents")}
    assert in_use[aid] == 1 and in_use[m["id"]] == 1, in_use

    # --- delete clears references
    rest("DELETE", f"/api/agents/{aid}")
    assert agent_of(p, n2)["agent_override"] is None
    assert agent_of(p, n2)["agent"]["source"] == "plan"
    rest("PUT", f"/api/plans/{p}/agent", {"agent": None})
    assert rest("GET", f"/api/plans/{p}")["default_agent"] is None
    rest("DELETE", f"/api/agents/{m['id']}")
    rest("DELETE", f"/api/agents/{m['id']}", expect=404)
    rest("DELETE", f"/api/plans/{p}")

    # --- SSE: agent mutations are announced
    time.sleep(0.3)
    stop.set()
    n_agents = sum(1 for msg in seen if msg.get("type") == "agents")
    assert n_agents >= 8, seen

    # --- explicit: no endpoint returned the key column or the secret
    for raw in bodies:
        assert SECRET not in raw, raw
        assert '"api_key"' not in raw, raw
    print(f"agents api OK ({len(bodies)} responses checked, {n_agents} 'agents' SSE messages)")


asyncio.run(main())
