"""The rework loop, against a running --mock server.

Covers the path that spans engine, store and REST: a finished node is re-run with
a rework brief, the brief reaches the agent's prompt (the thing a silent
regression would break invisibly), the node completes again, and the guard rails
hold -- no rework of a running node, and no rework with nothing to act on.

Mock agents write no files, so they produce no diff and therefore no step review.
The review-driven variant of rework (brief taken from the review findings instead
of from explicit instructions) is not covered here; it needs an agent that really
edits something.

Run:  uv run clearbox --mock      (one shell)
      uv run python tests/test_rework.py [http://127.0.0.1:8765]
"""
import asyncio, json, sys, tempfile, time, urllib.error, urllib.request

from mcp.client.client import Client

# Each top-level plan needs its own working directory: it is required, and two plans
# must not run in the same folder at once.
def wd():
    return tempfile.mkdtemp(prefix="cb-test-")

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
MARKER = "MARKER-rework-brief-must-reach-the-prompt"


def rest(path, method="GET", body=None):
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


def wait_for(plan_id, status, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        _, d = rest(f"/api/plans/{plan_id}")
        if d["status"] == status:
            return d
        time.sleep(0.3)
    raise AssertionError(f"plan {plan_id} still {d['status']} after {timeout}s, wanted {status}")


async def main():
    async with Client(URL + "/mcp") as c:
        async def call(name, /, **a):
            r = await c.call_tool(name, a)
            if r.is_error:
                raise RuntimeError(r.content[0].text)
            sc = r.structured_content
            return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)

        pid = await call("create_plan", title="Rework test", working_directory=wd())
        a = (await call("add_node", plan_id=pid, title="A", description="first step"))[1]
        b = (await call("add_node", plan_id=pid, title="B", description="second step"))[1]
        await call("add_edge", plan_id=pid, from_node_id=a, to_node_id=b)
        try:
            await call("start_plan", plan_id=pid)
            wait_for(pid, "done")
            print("plan ran:", "done")

            # a finished node with no review and no instructions has nothing to act on
            code, body = rest(f"/api/plans/{pid}/nodes/{a}/rework", "POST", {})
            assert code == 400, f"expected 400, got {code} {body}"
            print("no brief -> refused:", body["detail"][:80])

            # rework with explicit instructions: the brief must reach the prompt
            code, _ = rest(f"/api/plans/{pid}/nodes/{a}/rework", "POST", {"instructions": MARKER})
            assert code == 200, f"rework failed: {code}"
            _, ctx = rest(f"/api/plans/{pid}/nodes/{a}/context")
            assert MARKER in ctx["context"], "rework brief missing from the node's prompt"
            print("brief reached the prompt: yes")

            # a node being reworked is running again, and must not be reworked concurrently
            code, body = rest(f"/api/plans/{pid}/nodes/{a}/rework", "POST", {"instructions": "again"})
            assert code == 400, f"rework of a running node should be refused, got {code}"
            print("rework while running -> refused:", body["detail"][:80])

            d = wait_for(pid, "done")
            node = next(n for n in d["nodes"] if n["node_id"] == a)
            assert node["status"] == "done", node["status"]
            assert node["result"], "reworked node recorded no result"
            dep = next(n for n in d["nodes"] if n["node_id"] == b)
            assert dep["status"] == "done", "a rework must not reset its dependants"
            print("reworked node finished again, dependant untouched")
            print("rework OK")
        finally:
            rest(f"/api/plans/{pid}", "DELETE")


asyncio.run(main())
