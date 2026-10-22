"""OpenAiHarness against the scripted fake endpoint (tests/fake_openai.py).

No Clearbox server and no database: the harness is constructed from a
profile dict and run on a temp working directory, exactly as the engine would.
Covers the full event sequence, real tool effects on disk, streamed tool-call
fragments (single and interleaved parallel calls), the non-streaming path,
max_turns, and each failure mode (401, 404 model, 404 path, malformed tool
arguments, abrupt stream close, connection refused) ending in one clean result.

Run:  uv run python tests/test_openai_harness.py
"""
import asyncio, json, os, socket, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_openai import FakeOpenAI
from clearbox.harness.base import AgentJob
from clearbox.harness.openai_compat import OpenAiHarness

SECRET = "sk-test-0123456789abcdefSECRET"


async def run(fake, script, cwd, **params):
    fake.set_script(script)
    profile = {"name": "fake", "model": "fake-model", "base_url": params.pop("base_url", fake.url),
               "api_key": params.pop("api_key", None), "params": {"max_retries": 0, "timeout": 10, **params}}
    h = OpenAiHarness(profile=profile)
    job = AgentJob(plan_id=1, node_id=1, prompt="Your node: test\nDo the thing.", system_append="", cwd=cwd)
    events = [e async for e in h.run(job)]
    # contract checks that hold for every run
    kinds = [k for k, _ in events]
    assert kinds[0] == "system" and kinds.count("result") == 1 and kinds[-1] == "result", kinds
    dumped = json.dumps(events)  # must be JSON-serialisable
    assert "Traceback" not in dumped and SECRET not in dumped, dumped
    for k, d in events:
        if k == "tool_result":
            assert {"tool_use_id", "content", "is_error", "parent_tool_use_id"} <= set(d), d
    return kinds, events, events[-1][1]


def check_failed(res, *needles):
    assert res["is_error"] is True, res
    msg = " ".join(res["errors"])
    for n in needles:
        assert n in msg, (n, msg)
    print("   ->", msg[:150])


async def main():
    with FakeOpenAI() as fake, tempfile.TemporaryDirectory() as cwd:
        # 1) streamed multi-turn tool session: write, read back, then answer
        content = 'hello from the fake model\nline "two" {with} json-ish chars\n'
        kinds, ev, res = await run(fake, [
            {"tool": "write_file", "args": {"path": "out/hello.txt", "content": content}},
            {"tool": "read_file", "args": {"path": "out/hello.txt"}},
            {"text": "Wrote out/hello.txt and verified it."},
        ], cwd)
        assert kinds == ["system", "tool_use", "tool_result", "tool_use", "tool_result", "text", "result"], kinds
        with open(os.path.join(cwd, "out", "hello.txt"), encoding="utf-8") as f:
            assert f.read() == content
        assert ev[1][1]["input"] == {"path": "out/hello.txt", "content": content}, ev[1]  # 3 fragments reassembled
        assert ev[2][1]["tool_use_id"] == ev[1][1]["id"] and not ev[2][1]["is_error"], ev[2]
        assert "hello from the fake model" in ev[4][1]["content"], ev[4]
        assert res["is_error"] is False and res["text"] == "Wrote out/hello.txt and verified it.", res
        assert res["num_turns"] == 3 and res["prompt_tokens"] == 30, res
        assert all(r["stream"] for r in fake.requests)
        tool_msgs = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"]
        assert tool_msgs and tool_msgs[0]["tool_call_id"] == ev[1][1]["id"], fake.requests[1]["messages"]
        assert fake.auth[0] == "Bearer not-needed", fake.auth  # local endpoint, no key configured
        print("streamed tool session OK")

        # 2) two parallel calls whose argument fragments are interleaved in the stream
        kinds, ev, res = await run(fake, [
            {"calls": [{"tool": "write_file", "args": {"path": "a.txt", "content": "AAA"}},
                       {"tool": "write_file", "args": {"path": "b.txt", "content": "BBBBBB"}}]},
            {"text": "both written"},
        ], cwd)
        assert kinds == ["system", "tool_use", "tool_use", "tool_result", "tool_result", "text", "result"] or \
            kinds == ["system", "tool_use", "tool_result", "tool_use", "tool_result", "text", "result"], kinds
        assert open(os.path.join(cwd, "a.txt")).read() == "AAA" and open(os.path.join(cwd, "b.txt")).read() == "BBBBBB"
        assert not res["is_error"], res
        print("interleaved parallel calls OK")

        # 3) non-streaming path, literal api key is sent but never shows up in events
        kinds, ev, res = await run(fake, [
            {"tool": "write_file", "args": {"path": "c.txt", "content": "non-stream"}},
            {"text": "done without streaming"},
        ], cwd, stream=False, api_key=SECRET)
        assert kinds == ["system", "tool_use", "tool_result", "text", "result"], kinds
        assert open(os.path.join(cwd, "c.txt")).read() == "non-stream"
        assert not any(r.get("stream") for r in fake.requests) and fake.auth[0] == f"Bearer {SECRET}"
        assert res["text"] == "done without streaming" and not res["is_error"], res
        print("non-streaming OK")

        # 4) max_turns: the model never stops calling tools
        kinds, ev, res = await run(fake, [{"tool": "list_dir", "args": {}}] * 5, cwd, max_turns=2)
        assert kinds.count("tool_use") == 2 and len(fake.requests) == 2, kinds
        assert res["subtype"] == "error_max_turns", res
        check_failed(res, "max_turns=2")

        # 5) failure modes
        print("failure modes:")
        _, _, res = await run(fake, [{"fail": 401}], cwd, api_key=SECRET)  # the fake echoes the key back
        check_failed(res, "401", "api key")
        _, _, res = await run(fake, [{"fail": 404}], cwd)
        check_failed(res, "model 'fake-model' was not found")
        _, _, res = await run(fake, [], cwd, base_url=fake.root)  # base_url without /v1
        check_failed(res, "does not exist (404)", "/v1")
        _, _, res = await run(fake, [{"fail": "abort"}], cwd)
        check_failed(res, "closed the connection in the middle", "params.stream")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]
        _, _, res = await run(fake, [], cwd, base_url=f"http://127.0.0.1:{dead}/v1")
        check_failed(res, "could not connect")

        # malformed tool arguments: an is_error tool_result, then the model recovers
        kinds, ev, res = await run(fake, [
            {"tool": "write_file", "raw_args": '{"path": "bad.txt", "content": '},
            {"tool": "write_file", "args": {"path": "good.txt", "content": "fixed"}},
            {"text": "recovered"},
        ], cwd)
        bad = ev[2][1]
        assert bad["is_error"] and "invalid JSON" in bad["content"], bad
        assert ev[1][1]["input"] == {"_raw_arguments": '{"path": "bad.txt", "content": '}, ev[1]
        assert not os.path.exists(os.path.join(cwd, "bad.txt")) and open(os.path.join(cwd, "good.txt")).read() == "fixed"
        assert res["is_error"] is False and res["text"] == "recovered", res
        history = fake.requests[1]["messages"]
        assert history[2]["tool_calls"][0]["function"]["arguments"] == "{}", history[2]  # malformed args replaced
        print("   -> malformed arguments: tool error fed back, run recovered")
    print("openai harness OK")


asyncio.run(main())
