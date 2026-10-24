"""The rework loop, against a running --mock server.

Covers the path that spans engine, store and REST: a finished node is re-run with
a rework brief, the brief reaches the agent's prompt (the thing a silent
regression would break invisibly), the node completes again, and the guard rails
hold -- no rework of a running node, and no rework with nothing to act on.

Mock agents write no files, so the review-driven part gets its diff from the test
itself: the node declares a file and the test writes it while the (slowed-down)
mock runs. That node is then reviewed automatically and on demand, both must leave
the same review shape in get_reviews (one review implementation, two triggers), and
a rework without instructions must carry the review into the prompt.

Run:  uv run clearbox --mock      (one shell)
      uv run python tests/test_rework.py [http://127.0.0.1:<port>]  (default: $CLEARBOX_PORT, else 8765)
"""
import asyncio, json, os, sys, tempfile, time, urllib.error, urllib.request

from mcp.client.client import Client

# Each top-level plan needs its own working directory: it is required, and two plans
# must not run in the same folder at once.
def wd():
    return tempfile.mkdtemp(prefix="cb-test-")

URL = (sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{os.environ.get('CLEARBOX_PORT', '8765')}").rstrip("/")
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


REVIEW_KEYS = {"plan_id", "node_id", "verdict", "summary", "findings", "agent_name", "tokens_in", "tokens_out"}


def same_shape(auto, manual, what):
    """An automatic and an on-demand review of the same thing must look alike."""
    assert auto and manual, (what, auto, manual)
    assert REVIEW_KEYS <= set(auto) and set(auto) == set(manual), (what, sorted(auto), sorted(manual))
    for k in ("plan_id", "node_id", "verdict", "summary", "findings", "agent_name"):
        assert auto[k] == manual[k], (what, k, auto[k], manual[k])
    # the mock reviewer answers without a VERDICT line: recorded as error, whole reply as summary
    assert auto["verdict"] == "error" and auto["summary"] and auto["findings"] is None, (what, auto)


async def reviews():
    tag = str(int(time.time() * 1000))[-8:]
    # speed 1: the mock takes seconds, long enough for the test to write the declared file
    _, slow = rest("/api/agents", "POST", {"name": f"rw-slow-{tag}", "harness": "mock", "params": {"speed": 1}})
    _, rev = rest("/api/agents", "POST", {"name": f"rw-rev-{tag}", "harness": "mock", "params": {"speed": 20}})
    work = wd()
    async with Client(URL + "/mcp") as c:
        async def call(name, /, **a):
            r = await c.call_tool(name, a)
            if r.is_error:
                raise RuntimeError(r.content[0].text)
            sc = r.structured_content
            return sc.get("result", sc) if sc is not None else json.loads(r.content[0].text)

        pid = await call("create_plan", title="Review test", working_directory=work)
        a = (await call("add_node", plan_id=pid, title="Write out", description="write out.txt",
                        files=["out.txt"], agent=slow["name"]))[1]
        await call("set_plan_review_agent", plan_id=pid, agent=rev["name"])
        try:
            await call("start_plan", plan_id=pid)
            end = time.time() + 30
            while rest(f"/api/plans/{pid}")[1]["nodes"][0]["status"] != "running":
                assert time.time() < end, "node never started"
                time.sleep(0.05)
            with open(os.path.join(work, "out.txt"), "w") as f:  # snapshot is taken: this is a diff
                f.write("hello\n")
            wait_for(pid, "done")

            end = time.time() + 30
            while True:
                got = await call("get_reviews", plan_id=pid)
                if got["node_reviews"] and got["plan_review"]:
                    break
                assert time.time() < end, f"automatic reviews never arrived: {got}"
                time.sleep(0.2)
            auto_node, auto_plan = got["node_reviews"][0], got["plan_review"]
            print("automatic reviews:", auto_node["verdict"], "/", auto_plan["verdict"])

            manual = await call("review_node", plan_id=pid, node_id=a)  # falls back to the plan's reviewer
            same_shape(auto_node, manual, "node review")
            code, body = rest(f"/api/plans/{pid}/review", "POST", {"agent": rev["name"]})
            assert code == 200, (code, body)
            got = await call("get_reviews", plan_id=pid)
            same_shape(auto_node, got["node_reviews"][0], "node review via get_reviews")
            same_shape(auto_plan, got["plan_review"], "plan review")
            print("on-demand reviews match the automatic ones: yes")

            # both review sessions stream into the transcript, marked as reviews
            _, nd = rest(f"/api/plans/{pid}/nodes/{a}")
            assert any(e["data"].get("review") for e in nd["events"]), "review left no transcript"

            # rework with no instructions: the brief is the review's
            code, body = rest(f"/api/plans/{pid}/nodes/{a}/rework", "POST", {})
            assert code == 200, (code, body)
            _, ctx = rest(f"/api/plans/{pid}/nodes/{a}/context")
            assert "REWORK" in ctx["context"] and auto_node["summary"] in ctx["context"], ctx["context"][-400:]
            print("review findings reached the rework prompt: yes")
            await call("stop_plan", plan_id=pid)
            print("reviews OK")
        finally:
            rest(f"/api/plans/{pid}", "DELETE")
            for p in (slow, rev):
                rest(f"/api/agents/{p['id']}", "DELETE")


asyncio.run(main())
asyncio.run(reviews())
