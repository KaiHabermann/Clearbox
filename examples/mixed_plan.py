"""Build a mixed-harness plan through MCP: cheap model for the mechanical nodes, strong model for the judgement.

    python examples/mixed_plan.py                      # build the plan (creates missing agents)
    python examples/mixed_plan.py --start              # build and start it (builtin agents)
    python examples/mixed_plan.py --repo C:\\src\\myproject --start
    python examples/mixed_plan.py --cheap local-qwen --strong claude   # use agents you already have
    python examples/mixed_plan.py --mock --start       # force mock agents (no tokens spent)

The plan reviews a repository (default: this one) without changing it:

    Inventory files ─┐
                     ├─► Assess architecture ─► Review the assessment
    Inventory tests ─┘
       (cheap)              (strong)                 (strong)

Agents are assigned the three ways a model would do it: set_plan_agent makes the cheap agent the
plan default (the two inventory nodes inherit it), add_node(..., agent=...) pins the assessment to
the strong agent, and set_node_agent moves the review node onto it afterwards.

Agents that do not exist yet are created and printed:
- cheap:  an 'openai' profile copied from the first configured openai agent (endpoint, model, key
          env var) with the read-only toolset, max_turns 20 and max_parallel 1;
- strong: a 'claude-sdk' profile (server's --model) in permission mode 'default', which runs
          read-only tools without asking and blocks edits and shell commands.
When no openai agent is configured (e.g. a server started with --mock), both are created as
'mock' profiles instead, so the example runs anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from mcp.client.client import Client

READ_ONLY = (
    "Do not create, modify or delete any file. Your final message is this node's result; "
    "the nodes after yours only see that message."
)


async def main(a: argparse.Namespace) -> None:
    async with Client(a.url) as c:
        async def call(name: str, /, **args):
            res = await c.call_tool(name, args)
            if res.is_error:
                raise RuntimeError(res.content[0].text)
            if res.structured_content is not None:
                return res.structured_content.get("result", res.structured_content)
            return json.loads(res.content[0].text)

        agents = {p["name"]: p for p in await call("list_agents")}
        openai_agents = [p for p in agents.values() if p["harness"] == "openai"]
        template = agents.get("openai") if (agents.get("openai") or {}).get("harness") == "openai" else None
        template = template or (openai_agents[0] if openai_agents else None)
        use_mock = a.mock or template is None

        async def ensure(name: str, spec: dict) -> str:
            if name in agents:
                p = agents[name]
                print(f"using agent {name!r} ({p['harness']}" + (f" / {p['model']}" if p.get("model") else "") + ")")
                return name
            p = await call("create_agent", name=name, **spec)
            agents[name] = p
            print(f"created agent {name!r} ({p['harness']}" + (f" / {p['model']}" if p.get("model") else "")
                  + (f" @ {p['base_url']}" if p.get("base_url") else "") + ")")
            return name

        if use_mock:
            why = "--mock" if a.mock else "no openai agent is configured"
            print(f"{why}: falling back to mock agents")
            cheap = await ensure(a.cheap or "mock-cheap", {
                "harness": "mock", "params": {"speed": 3.0}, "max_parallel": 1,
                "description": "examples/mixed_plan.py: stands in for a cheap model",
            })
            strong = await ensure(a.strong or "mock-strong", {
                "harness": "mock", "params": {"speed": 1.0},
                "description": "examples/mixed_plan.py: stands in for a strong model",
            })
        else:
            cheap = await ensure(a.cheap or "cheap", {
                "harness": "openai", "model": template["model"], "base_url": template["base_url"],
                "api_key_env": template["api_key_env"],
                "params": {"temperature": 0, "max_turns": 20},
                "tools": {"preset": "readonly", "mcp": False},
                "max_parallel": 1,
                "description": f"examples/mixed_plan.py: read-only copy of {template['name']!r} for mechanical nodes",
            })
            strong = await ensure(a.strong or "strong", {
                "harness": "claude-sdk", "permission_mode": "default",
                "description": "examples/mixed_plan.py: Claude for judgement nodes (reads only)",
            })

        if not use_mock and agents[cheap]["harness"] == "openai":
            check = await call("test_agent", agent=cheap)
            print(f"test_agent {cheap}: {'ok' if check['ok'] else 'FAILED'} - {check['detail']}")
            if not check["ok"]:
                print(f"  (nodes on {cheap!r} will fail until that is fixed; edit it in the Agents panel or rerun with --mock)")

        repo = os.path.abspath(a.repo)
        plan = await call(
            "create_plan", title=f"Review {Path(repo).name} (mixed agents)", working_directory=repo,
            description=f"Assess the code base in {repo} without changing it: a cheap model inventories it, "
                        "a strong model judges the architecture, and a second pass reviews that judgement.",
        )
        print("plan", plan, "working in", repo)
        print(await call("set_plan_agent", plan_id=plan, agent=cheap))

        async def node(title: str, desc: str, agent: str | None = None) -> int:
            p, n = await call("add_node", plan_id=plan, title=title, description=desc,
                              **({"agent": agent} if agent else {}))
            print(f"  node ({p}, {n}) {title}" + (f"  [agent={agent}]" if agent else "  [inherits plan default]"))
            return n

        files = await node("Inventory files", (
            "List every source, config and documentation file in the working directory, skipping .git, "
            "virtualenvs, node_modules, caches, build output and lock files. Group them by top-level directory "
            "and give each file one line: path, approximate line count, and its purpose in at most 12 words, "
            "taken from its docstring/header or first lines. " + READ_ONLY))
        tests = await node("Inventory tests", (
            "Find the test files in the working directory. For each one give its path, how it is run "
            "(read its docstring or header), and in one line which part of the code it exercises. "
            "List source modules that no test seems to touch. " + READ_ONLY))
        assess = await node("Assess architecture", (
            "Using the two inventories from your prerequisites and by reading the code itself, assess the "
            "architecture: the main components and how they talk to each other, the three weakest points "
            "(coupling, missing tests, error handling, security), and for each a concrete, file-level "
            "suggestion. Cite file paths for every claim. " + READ_ONLY), agent=strong)
        review = await node("Review the assessment", (
            "Check the architecture assessment from your prerequisite against the actual code: for every "
            "claim, open the cited files and mark it confirmed, wrong or unverifiable, with a one-line reason. "
            "End with the suggestions you would actually prioritise. " + READ_ONLY))
        print(await call("set_node_agent", plan_id=plan, node_id=review, agent=strong))

        for x, y in [(files, assess), (tests, assess), (assess, review)]:
            await call("add_edge", plan_id=plan, from_node_id=x, to_node_id=y)

        view = await call("get_plan", plan_id=plan)
        print("resolved agents:")
        for n in view["nodes"]:
            ag = n["agent"]
            print(f"  {n['node_id']} {n['title']:<24} {ag['name']} ({ag['harness']}"
                  + (f" / {ag['model']}" if ag.get("model") else "") + ")"
                  + ("  node override" if n["agent_override"] else "  plan default"))

        if a.start:
            print(await call("start_plan", plan_id=plan, executor="builtin"))
        base = a.url.rsplit("/mcp", 1)[0]
        print(f"open {base}/#/plan/{plan} to watch")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]),
                    help="existing directory to review (default: this repository); it is not modified")
    ap.add_argument("--cheap", metavar="NAME", help="agent for the mechanical nodes (created if missing)")
    ap.add_argument("--strong", metavar="NAME", help="agent for the judgement nodes (created if missing)")
    ap.add_argument("--mock", action="store_true", help="use mock agents even if an openai agent exists")
    asyncio.run(main(ap.parse_args()))
