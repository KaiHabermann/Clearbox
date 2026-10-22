"""Build a plan with two nodes that declare the same file, to show them serialize instead of
clobbering it, plus one node with no files that runs alongside them freely:

    python examples/file_conflict_plan.py --start             # against a --mock server
    python examples/file_conflict_plan.py --start --url ...

None of the three nodes has a dependency on another, so without `files` all three would be free to
run at once. `writer-a` and `writer-b` both declare `shared.txt`; `writer-c` declares nothing. Run
against `uv run visualclaude --mock` and the three show up in the same graph layer, but `writer-b`
(the higher node number) is nudged down and waits for `writer-a` to finish before it starts, while
`writer-c` runs at the same time as either.

This polls `get_plan` until all three are done and prints when each one started and finished, so the
serialization is visible in the output without opening the UI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from mcp.client.client import Client


async def main(a: argparse.Namespace) -> None:
    async with Client(a.url) as c:
        async def call(name: str, /, **args):
            res = await c.call_tool(name, args)
            if res.is_error:
                raise RuntimeError(res.content[0].text)
            if res.structured_content is not None:
                return res.structured_content.get("result", res.structured_content)
            return json.loads(res.content[0].text)

        plan = await call(
            "create_plan", title="File conflict demo",
            description="Two nodes write shared.txt and must serialize; a third writes its own file freely.",
        )
        print("plan", plan)

        writer_a = await call(
            "add_node", plan_id=plan, title="writer-a",
            description="Append a line to shared.txt.", files=["shared.txt"],
        )
        writer_b = await call(
            "add_node", plan_id=plan, title="writer-b",
            description="Append a different line to shared.txt.", files=["shared.txt"],
        )
        writer_c = await call(
            "add_node", plan_id=plan, title="writer-c",
            description="Write its own file, unrelated to the other two.", files=["only-c.txt"],
        )
        for p, n in (writer_a, writer_b, writer_c):
            print(f"  node ({p}, {n})")

        view = await call("get_plan", plan_id=plan)
        for node in view["nodes"]:
            print(f"  node {node['node_id']} {node['title']:<10} files={node['files']} "
                  f"conflicts={node['conflicts']}")

        if not a.start:
            print(f"open {a.url.rsplit('/mcp', 1)[0]}/#/plan/{plan} to watch, or rerun with --start")
            return

        print(await call("start_plan", plan_id=plan, executor="builtin"))
        print(f"open {a.url.rsplit('/mcp', 1)[0]}/#/plan/{plan} to watch")

        seen: dict[int, str] = {}
        t0 = time.monotonic()
        while True:
            view = await call("get_plan", plan_id=plan)
            nodes = {n["node_id"]: n for n in view["nodes"]}
            for nid, n in nodes.items():
                if n["status"] != seen.get(nid):
                    seen[nid] = n["status"]
                    print(f"  t={time.monotonic() - t0:5.1f}s  node {nid} {n['title']:<10} -> {n['status']}")
            if all(n["status"] in ("done", "failed") for n in nodes.values()):
                break
            await asyncio.sleep(0.2)
        print("done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    ap.add_argument("--start", action="store_true")
    asyncio.run(main(ap.parse_args()))
