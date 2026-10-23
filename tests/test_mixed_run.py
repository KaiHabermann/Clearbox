"""Mixed-profile builtin run against a running server started with --mock.

One plan, eight independent nodes: four pinned to profile A (mock, max_parallel=1)
by node override, four inheriting profile B (mock, max_parallel=2) as the plan
default. After the run, the node rows must show that every node ran under its
assigned profile (agent_name and the "agent: ..." transcript line) and the
started_at/finished_at windows must never overlap more than each cap allows.
The server's --max-concurrency must be at least 3 (the default) so that the
caps, not the global limit, are what holds the nodes back.

Run:  uv run clearbox --mock      (one shell)
      uv run python tests/test_mixed_run.py [http://127.0.0.1:8765]
"""
import asyncio, json, sys, tempfile, time, uuid
import urllib.error
import urllib.request
from mcp.client.client import Client

# Each top-level plan needs its own working directory: it is required, and two plans
# must not run in the same folder at once.
def wd():
    return tempfile.mkdtemp(prefix="cb-test-")

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"


def rest(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode()}") from None


def max_overlap(rows):
    """Largest number of nodes whose [started_at, finished_at) windows overlap at one instant."""
    marks = sorted([(n["started_at"], 1) for n in rows] + [(n["finished_at"], -1) for n in rows],
                   key=lambda m: (m[0], m[1]))  # an end at the same instant as a start counts first
    cur = best = 0
    for _, d in marks:
        cur += d
        best = max(best, cur)
    return best


tag = uuid.uuid4().hex[:6]
A = rest("POST", "/api/agents", {"name": f"mix-a-{tag}", "harness": "mock", "params": {"speed": 8}, "max_parallel": 1})
B = rest("POST", "/api/agents", {"name": f"mix-b-{tag}", "harness": "mock", "params": {"speed": 8}, "max_parallel": 2})


async def build() -> tuple[int, list[int]]:
    """Plans are built over MCP (there is no REST endpoint for creating them)."""
    async with Client(URL + "/mcp") as c:
        async def call(name, **a):
            r = await c.call_tool(name, a)
            sc = r.structured_content
            return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)
        pid = await call("create_plan", title=f"Mixed run {tag}", working_directory=wd())
        return pid, [(await call("add_node", plan_id=pid, title=f"N{i}"))[1] for i in range(8)]


pid, nids = asyncio.run(build())
assigned = {}
for i, nid in enumerate(nids):
    if i % 2 == 0:
        rest("PUT", f"/api/plans/{pid}/nodes/{nid}/agent", {"agent": A["id"]})
    assigned[nid] = A["name"] if i % 2 == 0 else B["name"]
rest("PUT", f"/api/plans/{pid}/agent", {"agent": B["name"]})
rest("POST", f"/api/plans/{pid}/start", {"executor": "builtin"})

t0 = time.time()
while time.time() - t0 < 180:
    plan = rest("GET", f"/api/plans/{pid}")
    if all(n["status"] in ("done", "failed") for n in plan["nodes"]):
        break
    time.sleep(0.5)
print(f"mixed run finished in {time.time() - t0:.1f}s")
nodes = {n["node_id"]: n for n in plan["nodes"]}
assert all(n["status"] == "done" for n in nodes.values()), [(n["node_id"], n["status"], n["error"]) for n in nodes.values()]

for nid, name in assigned.items():
    n = nodes[nid]
    assert n["agent_name"] == name, (nid, n["agent_name"], name)
    assert n["agent"]["name"] == name and n["agent"]["source"] == ("node" if name == A["name"] else "plan"), n["agent"]
    events = rest("GET", f"/api/plans/{pid}/nodes/{nid}")["events"]
    first = next(e for e in events if e["kind"] == "system")
    assert first["data"]["text"] == f"agent: {name} (mock)", first
    assert n["started_at"] and n["finished_at"] and n["finished_at"] > n["started_at"], n

rows_a = [nodes[n] for n, name in assigned.items() if name == A["name"]]
rows_b = [nodes[n] for n, name in assigned.items() if name == B["name"]]
oa, ob = max_overlap(rows_a), max_overlap(rows_b)
print(f"max overlap: {A['name']}={oa} (cap 1), {B['name']}={ob} (cap 2), all={max_overlap(list(nodes.values()))}")
assert oa == 1, oa
assert ob <= 2, ob
assert ob == 2, "profile B never ran two nodes at once: the cap held but parallelism did not happen"

rest("DELETE", f"/api/plans/{pid}")
rest("DELETE", f"/api/agents/{A['id']}")
rest("DELETE", f"/api/agents/{B['id']}")
print("mixed run OK")
