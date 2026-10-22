"""A scriptable fake OpenAI-compatible endpoint for testing the openai harness.

Real endpoints are slow, cost money and are not deterministic, so the harness's
interesting code paths (tool-call delta accumulation, the tool loop, error
mapping) would otherwise go untested. This server answers
``GET /v1/models`` and ``POST /v1/chat/completions`` (streaming and not) from a
script: each chat request consumes the next scripted turn.

A turn is one of:

- ``{"text": "..."}``                    a final answer (content split over several deltas)
- ``{"tool": name, "args": {...}}``      one tool call; when streamed, ``arguments`` is
                                         split into 3 fragments over 3 deltas
- ``{"calls": [{"tool":..,"args":..}, ...]}``  several tool calls in one turn; streamed
                                         fragments of the calls are interleaved by index
- ``{"tool": name, "raw_args": "{not json"}``  a tool call with malformed arguments
- ``{"fail": 401}`` / ``{"fail": 404}``  an OpenAI-style error response (404 = unknown model)
- ``{"fail": "abort"}``                  start streaming, then drop the connection mid-stream

When the script is exhausted every further request gets ``{"text": "(script exhausted)"}``.
Paths outside ``/v1`` 404 with FastAPI's ``{"detail": "Not Found"}``, which is what a
base_url missing its ``/v1`` looks like.

Use it from a test (it is not run on its own)::

    from fake_openai import FakeOpenAI
    with FakeOpenAI(script=[{"tool": "write_file", "args": {...}}, {"text": "done"}]) as fake:
        profile = {"name": "fake", "model": "fake-model", "base_url": fake.url}
        ...
        fake.requests  # every chat request body received, in order
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class _Abort(Exception):
    """Raised inside the stream body: uvicorn then drops the connection mid-response."""


def _split3(s: str) -> list[str]:
    k = max(1, len(s) // 3)
    return [s[:k], s[k:2 * k], s[2 * k:]]


class FakeOpenAI:
    def __init__(self, script: list[dict[str, Any]] | None = None, models: tuple[str, ...] = ("fake-model",)):
        self.script: list[dict[str, Any]] = list(script or [])
        self.models = models
        self.requests: list[dict[str, Any]] = []   # chat request bodies
        self.auth: list[str | None] = []           # Authorization header per chat request
        self._lock = threading.Lock()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self.app = self._build_app()

    # ---------------------------------------------------------------- control
    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        """The base_url to give the harness."""
        return self.root + "/v1"

    def set_script(self, script: list[dict[str, Any]]) -> None:
        with self._lock:
            self.script = list(script)
            self.requests.clear()
            self.auth.clear()

    def start(self) -> "FakeOpenAI":
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="critical", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        t0 = time.time()
        while not self._server.started:
            if time.time() - t0 > 10:
                raise RuntimeError("fake OpenAI server did not start")
            time.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._thread.join(timeout=5)

    def __enter__(self) -> "FakeOpenAI":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------- app
    def _next_turn(self, body: dict[str, Any], auth: str | None) -> dict[str, Any]:
        with self._lock:
            self.requests.append(body)
            self.auth.append(auth)
            return self.script.pop(0) if self.script else {"text": "(script exhausted)"}

    @staticmethod
    def _calls(turn: dict[str, Any]) -> list[dict[str, Any]]:
        """The turn's tool calls as [{id, name, arguments}] (arguments always a string)."""
        raw = turn["calls"] if "calls" in turn else [turn] if "tool" in turn else []
        out = []
        for c in raw:
            args = c["raw_args"] if "raw_args" in c else json.dumps(c.get("args", {}))
            out.append({"id": f"call_{uuid.uuid4().hex[:12]}", "name": c["tool"], "arguments": args})
        return out

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/v1/models")
        def models() -> dict[str, Any]:
            return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "fake"} for m in self.models]}

        @app.post("/v1/chat/completions")
        async def chat(request: Request) -> Any:
            body = await request.json()
            turn = self._next_turn(body, request.headers.get("authorization"))
            model = body.get("model")
            fail = turn.get("fail")
            if fail == 401:
                # Real OpenAI echoes (part of) the key; the harness must scrub it.
                key = (request.headers.get("authorization") or "").removeprefix("Bearer ")
                return JSONResponse(status_code=401, content={"error": {
                    "message": f"Incorrect API key provided: {key}. You can find your API key at ...",
                    "type": "invalid_request_error", "code": "invalid_api_key"}})
            if fail == 404:
                return JSONResponse(status_code=404, content={"error": {
                    "message": f"The model `{model}` does not exist or you do not have access to it.",
                    "type": "invalid_request_error", "code": "model_not_found"}})
            calls = self._calls(turn)
            if body.get("stream"):
                usage = (body.get("stream_options") or {}).get("include_usage")
                return StreamingResponse(self._stream(model, turn, calls, usage, abort=fail == "abort"),
                                         media_type="text/event-stream")
            msg: dict[str, Any] = {"role": "assistant", "content": turn.get("text")}
            if calls:
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]
            return {"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

        return app

    async def _stream(self, model: str, turn: dict[str, Any], calls: list[dict[str, Any]], usage: bool, abort: bool):
        def chunk(delta: dict[str, Any] | None, finish: str | None = None, **extra: Any) -> bytes:
            c = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                 "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
            return f"data: {json.dumps(c)}\n\n".encode()

        yield chunk({"role": "assistant", "content": ""})
        if abort:
            yield chunk({"content": "I was about to "})
            raise _Abort("connection dropped on purpose")
        text = turn.get("text")
        if text:
            for part in _split3(text):
                yield chunk({"content": part})
        # first fragment of every call: id + name, empty arguments
        for i, c in enumerate(calls):
            yield chunk({"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                         "function": {"name": c["name"], "arguments": ""}}]})
        # then the arguments in 3 pieces each, interleaved across calls (index only)
        pieces = [_split3(c["arguments"]) for c in calls]
        for k in range(3):
            for i, p in enumerate(pieces):
                yield chunk({"tool_calls": [{"index": i, "function": {"arguments": p[k]}}]})
        yield chunk({}, "tool_calls" if calls else "stop")
        if usage:
            yield chunk(None, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        yield b"data: [DONE]\n\n"
