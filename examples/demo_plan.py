"""Create a demo plan through the MCP interface (the same tools a model would use).

    python examples/demo_plan.py                 # just build the plan
    python examples/demo_plan.py --start         # build and start it (builtin agents)
    python examples/demo_plan.py --url http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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

        # The whole plan in one call: nodes carry your own keys, edges and results_from use them.
        # A top-level plan must say where its agents work; a fresh temp dir by default, since this
        # demo writes a whole little app.
        got = await call(
            "submit_plan", title="Tiny CLI todo app",
            description="Write a small Python command-line todo app with tests and docs.",
            working_directory=workdir or tempfile.mkdtemp(prefix="clearbox-demo-"),
            nodes=[
                {"key": "spec", "title": "Write spec", "files": ["SPEC.md"],
                 "description": "Write SPEC.md describing commands add/list/done/remove and a JSON storage file."},
                {"key": "core", "title": "Implement core", "files": ["todo.py"],
                 "description": "Implement todo.py according to SPEC.md (storage + commands)."},
                {"key": "cli", "title": "Implement CLI", "files": ["todo.py"],
                 "description": "Add an argparse CLI entry point to todo.py."},
                {"key": "tests", "title": "Write tests",
                 "description": "Write pytest tests covering the commands in SPEC.md."},
                {"key": "docs", "title": "Write README", "files": ["README.md"],
                 "description": "Write README.md with install and usage instructions."},
                {"key": "review", "title": "Final review", "results_from": ["core", "cli", "tests", "docs"],
                 "description": "Run the tests, fix failures and summarise the state of the project."},
            ],
            edges=[["spec", "core"], ["spec", "tests"], ["spec", "docs"], ["core", "cli"],
                   ["cli", "review"], ["tests", "review"], ["docs", "review"]],
        )
        plan, ids = got["plan_id"], got["nodes"]
        print("plan", plan)
        for key, n in ids.items():
            print(f"  node ({plan}, {n}) {key}")

        # 'Write tests' is itself a small plan: the same call with a parent node
        sub = await call(
            "submit_plan", title="Test suite", description="Tests for the todo app",
            parent_plan_id=plan, parent_node_id=ids["tests"],
            nodes=[
                {"key": "fx", "title": "Fixtures", "files": ["tests/conftest.py"],
                 "description": "Create conftest.py with a temporary storage fixture."},
                {"key": "cmd", "title": "Command tests", "files": ["tests/test_commands.py"],
                 "description": "Test add/list/done/remove."},
                {"key": "edge", "title": "Edge-case tests", "files": ["tests/test_edge_cases.py"],
                 "description": "Test empty storage, unknown ids, corrupted JSON."},
            ],
            edges=[["fx", "cmd"], ["fx", "edge"]],
        )
        print("subplan", sub["plan_id"], "attached to node", ids["tests"])

        if start:
            print(await call("start_plan", plan_id=plan, executor="builtin"))
        print(f"open {url.removesuffix('/mcp')}/ to watch")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.environ.get('CLEARBOX_PORT', '8765')}/mcp")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--dir", help="working directory for the plan (default: a fresh temp dir)")
    a = ap.parse_args()
    asyncio.run(main(a.url, a.start, a.dir))
