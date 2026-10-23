"""Create a demo plan through the MCP interface (the same tools a model would use).

    python examples/demo_plan.py                 # just build the plan
    python examples/demo_plan.py --start         # build and start it (builtin agents)
    python examples/demo_plan.py --url http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile

from mcp.client.client import Client


async def main(url: str, start: bool, workdir: str | None = None) -> None:
    async with Client(url) as c:
        async def call(name: str, **args):
            res = await c.call_tool(name, args)
            if res.is_error:
                raise RuntimeError(res.content[0].text)
            if res.structured_content is not None:
                return res.structured_content.get("result", res.structured_content)
            return json.loads(res.content[0].text)

        # a top-level plan must say where its agents work; a fresh temp dir by default,
        # since this demo writes a whole little app
        plan = await call("create_plan", title="Tiny CLI todo app",
                          description="Write a small Python command-line todo app with tests and docs.",
                          working_directory=workdir or tempfile.mkdtemp(prefix="clearbox-demo-"))
        print("plan", plan)

        async def node(title: str, desc: str, plan_id: int = plan) -> int:
            p, n = await call("add_node", plan_id=plan_id, title=title, description=desc)
            print(f"  node ({p}, {n}) {title}")
            return n

        spec = await node("Write spec", "Write SPEC.md describing commands add/list/done/remove and a JSON storage file.")
        core = await node("Implement core", "Implement todo.py according to SPEC.md (storage + commands).")
        cli = await node("Implement CLI", "Add an argparse CLI entry point to todo.py.")
        tests = await node("Write tests", "Write pytest tests covering the commands in SPEC.md.")
        docs = await node("Write README", "Write README.md with install and usage instructions.")
        review = await node("Final review", "Run the tests, fix failures and summarise the state of the project.")

        for a, b in [(spec, core), (spec, tests), (spec, docs), (core, cli), (cli, review), (tests, review), (docs, review)]:
            await call("add_edge", plan_id=plan, from_node_id=a, to_node_id=b)

        # 'Write tests' is itself a small plan
        sub = await call("create_plan", title="Test suite", description="Tests for the todo app",
                         parent_plan_id=plan, parent_node_id=tests)
        print("subplan", sub, "attached to node", tests)
        fx = await node("Fixtures", "Create conftest.py with a temporary storage fixture.", sub)
        t1 = await node("Command tests", "Test add/list/done/remove.", sub)
        t2 = await node("Edge-case tests", "Test empty storage, unknown ids, corrupted JSON.", sub)
        for b in (t1, t2):
            await call("add_edge", plan_id=sub, from_node_id=fx, to_node_id=b)

        if start:
            print(await call("start_plan", plan_id=plan, executor="builtin"))
        print("open http://127.0.0.1:8765/ to watch")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--dir", help="working directory for the plan (default: a fresh temp dir)")
    a = ap.parse_args()
    asyncio.run(main(a.url, a.start, a.dir))
