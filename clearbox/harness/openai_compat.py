"""Harness that drives any OpenAI-compatible chat-completions endpoint as an agent.

OpenAI, Azure, OpenRouter, vLLM, llama.cpp, LM Studio, Ollama, ... all speak the
same ``POST {base_url}/chat/completions`` dialect with function calling. Unlike
the Claude Agent SDK, such an endpoint is just a model: there is no agent loop
and no tools on the other side. This module supplies both. It runs the classic
loop (ask the model -> execute the tool calls it makes -> feed the results back
-> repeat until it answers without tool calls) with the sandboxed ``Toolbox``
from ``tools.py`` plus, when enabled, the clearbox MCP tools through
``McpToolBridge``, and translates everything into the event contract from
``base.py``. That is the whole point: a session on a local llama shows up in the
UI exactly like a Claude session, with no harness-specific code anywhere else.

Settings come from the agent-profile dict:

- ``model`` (required), ``base_url`` (default ``https://api.openai.com/v1``;
  must end in ``/v1`` or whatever path precedes ``/chat/completions``)
- api key: ``os.environ[api_key_env]``, else the literal ``api_key``, else (only
  for the default OpenAI url) ``OPENAI_API_KEY``. Local endpoints (localhost,
  private networks) get ``"not-needed"`` because they usually ignore the key;
  anything else without a key is a ``HarnessError`` naming the env var to set.
- ``params``: ``temperature``, ``top_p``, ``max_tokens``,
  ``max_completion_tokens``, ``reasoning_effort``, ``parallel_tool_calls``,
  ``max_turns`` (default 50), ``stream`` (default true; false = one plain
  request per turn, for endpoints with broken SSE), ``timeout`` (seconds,
  default 600), ``max_retries`` (default 2), ``extra_body`` (dict passed
  through verbatim for vendor-specific knobs), ``input_price_per_mtok`` /
  ``output_price_per_mtok`` (USD per 1M tokens; enables ``cost_usd``).
- ``tools``: the ``Toolbox`` config (``preset``/``allow``/``deny``) and ``mcp``.
- ``system_append``: extra system prompt text for this profile.

The api key lives only in the ``AsyncOpenAI`` client. It is never put in an
event, a log line or an error message; error texts from the endpoint are
scrubbed of it before they are shown.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import platform
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from .base import AgentJob, HarnessError, _tool_result_text, _truncate
from .tools import PRESETS, Toolbox, ToolSpec

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MAX_TURNS = 50
DEFAULT_TIMEOUT_S = 600.0
LOCAL_KEY = "not-needed"

PREAMBLE = """\
You are an autonomous software agent executing one task (a "node") of a larger plan. \
Nobody will answer questions or confirm steps: work on your own until the task is actually done.

How to work:
- Use your tools to inspect and change files and to run commands. Never guess what a file \
contains or whether a command succeeds: look, run, verify.
- Make the changes the task asks for; do not just describe them.
- Keep calling tools until the work is complete and checked. If something fails, read the \
error, fix the cause and try again.
- Paths given to the file tools are relative to the working directory below.

Your FINAL assistant message, the first one you send without any tool calls, ends the task. \
It is handed to the nodes that depend on yours as this node's result, so make it a concise, \
self-contained summary of what you did and what you produced (files, decisions, findings). \
Do not send a message without tool calls until you are done."""

# Parameters copied verbatim from the profile into the request when set.
PASSTHROUGH_PARAMS = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "reasoning_effort", "seed")


def _as_json(value: Any, default: Any) -> Any:
    """Profile columns like params/tools may arrive as JSON text or already decoded."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def is_local_url(base_url: str) -> bool:
    """True for endpoints on this machine or a private network (which rarely check keys)."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or host.endswith((".local", ".localhost", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified


def _host(base_url: str) -> str:
    """scheme://host[:port] only -- never userinfo, path or query (which might hold a key)."""
    u = urlsplit(base_url)
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")


def _extra(obj: Any, *names: str) -> Any:
    """Read a vendor extension field (reasoning_content, ...) from an SDK model or dict."""
    for name in names:
        if isinstance(obj, dict):
            v = obj.get(name)
        else:
            v = getattr(obj, name, None)
            if v is None:
                v = (getattr(obj, "model_extra", None) or {}).get(name)
        if v:
            return v
    return None


def tool_param(spec: ToolSpec) -> dict[str, Any]:
    """A provider-neutral ToolSpec in OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {"name": spec.name, "description": spec.description, "parameters": spec.schema},
    }


@dataclass
class _PendingCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class TurnAccumulator:
    """Reassembles one assistant response from streamed chunks (or a whole response).

    The fiddly part is tool calls: they arrive fragmented, keyed by ``index``.
    The first fragment of a call usually carries ``id`` and ``function.name``,
    the following ones only pieces of the ``function.arguments`` string, which
    must be concatenated in order. Some servers repeat ``id``/``name`` in every
    fragment, some omit ``index`` (single-call servers), some send the whole
    call in one fragment; all of these are handled here.
    """

    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    calls: dict[int, _PendingCall] = field(default_factory=dict)
    finish_reason: str | None = None
    usage: Any = None

    def feed_chunk(self, chunk: Any) -> None:
        usage = getattr(chunk, "usage", None)
        if usage:
            self.usage = usage
        for choice in getattr(chunk, "choices", None) or []:
            if getattr(choice, "index", 0) not in (0, None):
                continue  # n > 1 is never requested; ignore stray alternatives
            delta = getattr(choice, "delta", None)
            if delta is not None:
                self._feed_delta(delta)
            if getattr(choice, "finish_reason", None):
                self.finish_reason = choice.finish_reason

    def _feed_delta(self, delta: Any) -> None:
        if getattr(delta, "content", None):
            self.content.append(delta.content)
        reasoning = _extra(delta, "reasoning_content", "reasoning")
        if isinstance(reasoning, str):
            self.reasoning.append(reasoning)
        for tc in getattr(delta, "tool_calls", None) or []:
            self._feed_tool_call(tc)

    def _feed_tool_call(self, tc: Any) -> None:
        idx = getattr(tc, "index", None)
        tc_id = getattr(tc, "id", None)
        if idx is None:
            # No index: match by id if we have seen it, else a new call when an
            # id appears, else continue the most recent call.
            idx = next((i for i, c in self.calls.items() if tc_id and c.id == tc_id), None)
            if idx is None:
                idx = len(self.calls) if (tc_id or not self.calls) else max(self.calls)
        call = self.calls.setdefault(idx, _PendingCall())
        if tc_id and not call.id:
            call.id = tc_id
        fn = getattr(tc, "function", None)
        if fn is not None:
            name = getattr(fn, "name", None)
            if name and name != call.name:  # repeated full names are not re-appended
                call.name += name
            args = getattr(fn, "arguments", None)
            if args:
                call.arguments += args if isinstance(args, str) else json.dumps(args)

    def feed_message(self, response: Any) -> None:
        """Non-streaming: take a whole ChatCompletion."""
        self.usage = getattr(response, "usage", None)
        choices = getattr(response, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        self.finish_reason = getattr(choice, "finish_reason", None)
        msg = choice.message
        if msg.content:
            self.content.append(msg.content)
        reasoning = _extra(msg, "reasoning_content", "reasoning")
        if isinstance(reasoning, str):
            self.reasoning.append(reasoning)
        for i, tc in enumerate(msg.tool_calls or []):
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            args = fn.arguments
            self.calls[i] = _PendingCall(
                id=tc.id or "", name=fn.name or "", arguments=args if isinstance(args, str) else json.dumps(args)
            )

    @property
    def text(self) -> str:
        return "".join(self.content)

    @property
    def thinking(self) -> str:
        return "".join(self.reasoning)

    def tool_calls(self) -> list[_PendingCall]:
        return [self.calls[i] for i in sorted(self.calls) if self.calls[i].name or self.calls[i].arguments]


def parse_arguments(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Decode a tool call's argument string -> (args, error)."""
    if not raw.strip():
        return {}, None  # many models send "" for tools without parameters
    try:
        value = json.loads(raw)
    except ValueError as e:
        return None, f"invalid JSON in tool arguments ({e})"
    if not isinstance(value, dict):
        return None, f"tool arguments must be a JSON object, got {type(value).__name__}"
    return value, None


@dataclass
class _RunState:
    session_id: str
    started: float
    num_turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    saw_usage: bool = False
    text: str = ""
    is_error: bool = False
    subtype: str = "success"
    errors: list[str] = field(default_factory=list)

    def fail(self, subtype: str, message: str, text: str | None = None) -> None:
        self.is_error = True
        self.subtype = subtype
        self.errors.append(message)
        if text is not None:
            self.text = text

    def add_usage(self, usage: Any) -> None:
        if not usage:
            return
        self.saw_usage = True
        self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)


class OpenAiHarness:
    """Runs an agent loop against an OpenAI-compatible chat-completions endpoint."""

    def __init__(self, profile: dict[str, Any] | None = None):
        """The clearbox MCP endpoint comes per run, as ``job.mcp_url`` (None when the
        server runs with ``--no-agent-mcp``, and for reviews); the profile's ``tools.mcp``
        can still switch the bridge off."""
        profile = profile or {}
        self.profile_name = profile.get("name") or "openai"
        self.params: dict[str, Any] = _as_json(profile.get("params"), {}) or {}
        self.tools_config: dict[str, Any] = _as_json(profile.get("tools"), {}) or {}
        self.system_append = profile.get("system_append") or ""
        self.model = profile.get("model") or self.params.get("model")
        if not self.model:
            raise HarnessError(f"agent profile {self.profile_name!r}: no model set (e.g. 'gpt-4o-mini', 'qwen2.5-coder:14b')")
        self.base_url = (profile.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        if urlsplit(self.base_url).scheme not in ("http", "https"):
            raise HarnessError(
                f"agent profile {self.profile_name!r}: base_url {self.base_url!r} must start with http:// or https:// "
                "(e.g. http://localhost:11434/v1)"
            )
        preset = self.tools_config.get("preset") or "all"
        if preset not in PRESETS:
            raise HarnessError(f"agent profile {self.profile_name!r}: unknown tools preset {preset!r}; expected one of {', '.join(PRESETS)}")
        self._api_key = self._resolve_key(profile)
        self.max_turns = int(self.params.get("max_turns") or DEFAULT_MAX_TURNS)
        self.stream = self.params.get("stream", True) is not False
        from ..mcp_bridge import mcp_enabled  # lazy: mcp_bridge imports the harness package

        self.use_mcp = mcp_enabled(self.tools_config, default=True)

    # ------------------------------------------------------------------ setup

    def _resolve_key(self, profile: dict[str, Any]) -> str:
        env_name = (profile.get("api_key_env") or "").strip()
        key = os.environ.get(env_name) if env_name else None
        key = key or profile.get("api_key")
        if not key and not profile.get("base_url"):
            # Only for the real OpenAI url: never send OPENAI_API_KEY to a third party.
            env_name = env_name or "OPENAI_API_KEY"
            key = os.environ.get("OPENAI_API_KEY")
        if key:
            return str(key).strip()
        if is_local_url(self.base_url):
            return LOCAL_KEY
        if env_name:
            raise HarnessError(
                f"agent profile {self.profile_name!r}: environment variable {env_name} is not set; "
                f"set it to the api key for {_host(self.base_url)} and restart the server"
            )
        raise HarnessError(
            f"agent profile {self.profile_name!r}: no api key for {_host(self.base_url)}; set api_key_env to the "
            "name of an environment variable that holds the key (e.g. OPENROUTER_API_KEY)"
        )

    def _client(self) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url=self.base_url,
            api_key=self._api_key,
            timeout=float(self.params.get("timeout") or DEFAULT_TIMEOUT_S),
            max_retries=int(self.params.get("max_retries", 2)),
        )

    async def ping(self, prompt: str, timeout: float) -> str:
        """One tool-less, non-streamed request: the connection test behind ``harness.probe``.

        Returns the model's reply; raises ``HarnessError`` with the same actionable
        message a failed node would get (scrubbed of the key).
        """
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=self.base_url, api_key=self._api_key, timeout=timeout, max_retries=0)
        try:
            response = await client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}], **self._request_kwargs([])
            )
        except Exception as e:  # noqa: BLE001 -- every failure becomes a readable message
            raise HarnessError(self._explain(e)[1]) from None
        finally:
            await client.close()
        acc = TurnAccumulator()
        acc.feed_message(response)
        return self._scrub(acc.text or acc.thinking)

    def _scrub(self, text: str) -> str:
        """Remove anything key-like from text that came back from the endpoint."""
        if self._api_key and self._api_key != LOCAL_KEY and len(self._api_key) >= 6:
            text = text.replace(self._api_key, "***")
        return re.sub(r"\b(sk|key|rk)-[A-Za-z0-9_\-*]{8,}", r"\1-***", text)

    def _system_prompt(self, job: AgentJob, specs: list[ToolSpec]) -> str:
        parts = [PREAMBLE]
        if job.system_append:
            parts.append(job.system_append)
        if self.system_append:
            parts.append(self.system_append)
        env = [
            "Environment:",
            f"- Working directory: {job.cwd}",
            f"- Platform: {platform.system()} {platform.release()}",
            f"- Date: {date.today().isoformat()}",
        ]
        if specs:
            env.append("- Tools: " + ", ".join(s.name for s in specs))
            if any(s.name.startswith("vc_") for s in specs):
                env.append(
                    "- The clearbox plan tools mentioned above (create_plan, add_node, add_edge, ...) "
                    "are available with a vc_ prefix: vc_create_plan, vc_add_node, vc_add_edge, ..."
                )
        else:
            env.append("- Tools: none. Answer from the task description alone.")
        parts.append("\n".join(env))
        return "\n\n".join(parts)

    def _request_kwargs(self, tools: list[dict[str, Any]]) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model}
        for name in PASSTHROUGH_PARAMS:
            if self.params.get(name) is not None:
                kw[name] = self.params[name]
        if tools:  # some endpoints reject an empty tools array
            kw["tools"] = tools
            if self.params.get("parallel_tool_calls") is not None:
                kw["parallel_tool_calls"] = bool(self.params["parallel_tool_calls"])
        if isinstance(self.params.get("extra_body"), dict):
            kw["extra_body"] = self.params["extra_body"]
        return kw

    def _cost(self, state: _RunState) -> float | None:
        pin = self.params.get("input_price_per_mtok")
        pout = self.params.get("output_price_per_mtok")
        if not state.saw_usage or (pin is None and pout is None):
            return None
        return (float(pin or 0) * state.prompt_tokens + float(pout or 0) * state.completion_tokens) / 1_000_000

    def _explain(self, e: Exception) -> tuple[str, str]:
        """Map an SDK exception to (subtype, actionable message)."""
        import openai

        where = _host(self.base_url)
        body = getattr(e, "body", None)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            body = body["error"]
        raw = body.get("message") or body.get("detail") if isinstance(body, dict) else None
        # trailing dots stripped: the templates below punctuate the detail themselves
        detail = self._scrub(str(raw or getattr(e, "message", None) or e))[:600].rstrip(" .")
        if isinstance(e, openai.APITimeoutError):
            return "error_api", (
                f"request to {where} timed out after retries. Is the server overloaded or the model still loading? "
                "Raise params.timeout for slow local models."
            )
        if isinstance(e, openai.APIConnectionError):
            cause = e.__cause__ or e
            # matched by name: depending on the openai version the transport is httpx or httpx2
            if type(cause).__name__ in ("RemoteProtocolError", "ReadError"):
                # The server was reachable but dropped the connection mid-response;
                # "is the server running" would send the operator the wrong way.
                return "error_api", (
                    f"{where} closed the connection in the middle of the response ({type(cause).__name__}: "
                    f"{self._scrub(str(cause))[:200]}). The server may have crashed, restarted or run out of "
                    "memory; retry the node. If it keeps happening, the endpoint's streaming may be broken: "
                    "set params.stream to false."
                )
            return "error_api", (
                f"could not connect to {self.base_url} ({type(cause).__name__}: {self._scrub(str(cause))[:200]}). "
                "Is the server running and the base_url right (e.g. http://localhost:11434/v1 for Ollama, "
                "http://localhost:1234/v1 for LM Studio)?"
            )
        if isinstance(e, openai.AuthenticationError):
            return "error_api", (
                f"{where} rejected the api key (401). Check the environment variable named in the profile's "
                f"api_key_env, or the profile's api key. Server said: {detail}"
            )
        if isinstance(e, openai.PermissionDeniedError):
            return "error_api", f"{where} denied access (403): {detail}. Does this key have access to model {self.model!r}?"
        if isinstance(e, openai.NotFoundError):
            if "model" in detail.lower():
                return "error_api", (
                    f"model {self.model!r} was not found at {where} (404): {detail}. Check the profile's model name "
                    "(for Ollama, `ollama list`; for OpenRouter, the vendor/model slug)."
                )
            return "error_api", (
                f"{self.base_url}/chat/completions does not exist (404). The base_url usually has to end in /v1, "
                f"e.g. {where}/v1. Server said: {detail}"
            )
        if isinstance(e, openai.RateLimitError):
            return "error_api", f"{where} rate-limited the request (429) after retries: {detail}"
        if isinstance(e, openai.BadRequestError):
            hint = ""
            low = detail.lower()
            if "tool" in low or "function" in low:
                hint = (
                    " The model or server may not support function calling: pick a tool-capable model, enable tool "
                    "calling on the server (vLLM: --enable-auto-tool-choice), or set the profile's tools preset to 'none'."
                )
            elif "model" in low:
                hint = f" Check that model {self.model!r} exists on this endpoint."
            return "error_api", f"{where} rejected the request (400): {detail}.{hint}"
        if isinstance(e, openai.APIStatusError):
            return "error_api", f"{where} returned HTTP {e.status_code}: {detail}"
        return "error_during_execution", (
            f"{type(e).__name__} while talking to {where}: {detail}. If the endpoint's streaming is broken, "
            "set params.stream to false."
        )

    # ------------------------------------------------------------------- run

    async def run(self, job: AgentJob) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        state = _RunState(session_id=str(uuid.uuid4()), started=time.monotonic())
        yield "system", {
            "text": "session started",
            "session_id": state.session_id,
            "model": self.model,
            "base_url": _host(self.base_url),
            "cwd": job.cwd,
        }
        try:
            async for event in self._loop(job, state):
                yield event
        except Exception as e:  # never escape run(); CancelledError is BaseException and propagates
            subtype, message = self._explain(e)
            state.fail(subtype, message)
        yield "result", {
            "text": state.text,
            "is_error": state.is_error,
            "subtype": state.subtype,
            "num_turns": state.num_turns,
            "duration_ms": int((time.monotonic() - state.started) * 1000),
            "cost_usd": self._cost(state),
            "session_id": state.session_id,
            "errors": state.errors,
            "model": self.model,
            "tokens_in": state.prompt_tokens if state.saw_usage else None,
            "tokens_out": state.completion_tokens if state.saw_usage else None,
        }

    async def _loop(self, job: AgentJob, state: _RunState) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        toolbox = Toolbox(root=job.cwd, config=self.tools_config)
        bridge = None
        specs = toolbox.specs()
        if self.use_mcp and job.mcp_url:
            from ..mcp_bridge import McpToolBridge

            bridge = McpToolBridge(job.mcp_url)
            vc_specs = await bridge.specs()
            if not vc_specs:
                yield "error", {"text": "clearbox MCP tools unavailable; continuing without them"}
            specs = specs + vc_specs
        tools = [tool_param(s) for s in specs]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(job, specs)},
            {"role": "user", "content": job.prompt},
        ]
        base_kwargs = self._request_kwargs(tools)
        used_ids: set[str] = set()
        include_usage = True
        nudged = False

        async with self._client() as client:
            while True:
                if state.num_turns >= self.max_turns:
                    state.fail(
                        "error_max_turns",
                        f"stopped after max_turns={self.max_turns} model turns without a final answer; "
                        "raise params.max_turns or split the node",
                        text=state.text,
                    )
                    return
                state.num_turns += 1
                acc = TurnAccumulator()
                try:
                    await self._complete(client, base_kwargs, messages, acc, include_usage)
                except Exception as e:
                    # Some servers reject stream_options; retry the turn once without it.
                    if include_usage and self.stream and "stream_options" in str(e):
                        include_usage = False
                        acc = TurnAccumulator()
                        await self._complete(client, base_kwargs, messages, acc, include_usage)
                    else:
                        raise
                if acc.usage:
                    # per-turn delta so the UI can count up while the node runs
                    yield "usage", {
                        "tokens_in": int(getattr(acc.usage, "prompt_tokens", 0) or 0),
                        "tokens_out": int(getattr(acc.usage, "completion_tokens", 0) or 0),
                    }
                state.add_usage(acc.usage)

                text = acc.text
                if acc.thinking.strip():
                    yield "thinking", {"text": acc.thinking, "parent_tool_use_id": None}
                if text.strip():
                    yield "text", {"text": text, "parent_tool_use_id": None}
                    state.text = text

                calls = acc.tool_calls()
                if not calls:
                    if acc.finish_reason == "content_filter":
                        state.fail("error_during_execution", "the endpoint's content filter stopped the response", text=text)
                        return
                    if acc.finish_reason == "length":
                        note = (
                            f"[response truncated: the model hit its output token limit "
                            f"(params.max_tokens={self.params.get('max_tokens')}); the result may be incomplete]"
                        )
                        yield "error", {"text": note}
                        state.text = (text + "\n\n" + note).strip()
                        state.subtype = "success_truncated"
                        return
                    if not text.strip() and not nudged:
                        # An empty final message would hand nothing to dependent nodes; ask once.
                        nudged = True
                        messages.append({"role": "assistant", "content": text})
                        messages.append({
                            "role": "user",
                            "content": "Your last message was empty. If the task is done, reply with the final "
                            "summary of what you did and produced; otherwise continue working with your tools.",
                        })
                        continue
                    state.text = text
                    return

                prepared = []
                for call in calls:
                    call_id = call.id
                    if not call_id or call_id in used_ids:  # some servers reuse "call_0" every turn
                        call_id = f"call_{uuid.uuid4().hex[:24]}"
                    used_ids.add(call_id)
                    args, err = parse_arguments(call.arguments)
                    prepared.append((call_id, call, args, err))
                messages.append({
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            # Malformed arguments are replaced by "{}" so servers that re-parse
                            # the history do not choke; the tool result carries the raw text.
                            "function": {"name": c.name, "arguments": c.arguments if err is None else "{}"},
                        }
                        for cid, c, _, err in prepared
                    ],
                })
                for call_id, call, args, err in prepared:
                    yield "tool_use", {
                        "id": call_id,
                        "name": call.name,
                        "input": args if args is not None else {"_raw_arguments": call.arguments[:2000]},
                        "parent_tool_use_id": None,
                    }
                    if err is not None:
                        content, is_error = (
                            f"Error: {err}. Raw arguments: {call.arguments[:1000]!r}. "
                            "Call the tool again with a valid JSON object.",
                            True,
                        )
                    elif bridge is not None and bridge.owns(call.name):
                        content, is_error = await bridge.call(call.name, args)
                    else:
                        content, is_error = await toolbox.call(call.name, args)
                    content = _truncate(_tool_result_text(content))
                    yield "tool_result", {
                        "tool_use_id": call_id,
                        "content": content,
                        "is_error": bool(is_error),
                        "parent_tool_use_id": None,
                    }
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    async def _complete(
        self,
        client: Any,
        base_kwargs: dict[str, Any],
        messages: list[dict[str, Any]],
        acc: TurnAccumulator,
        include_usage: bool,
    ) -> None:
        """One model turn, streamed or not, into ``acc``."""
        if not self.stream:
            response = await client.chat.completions.create(messages=messages, **base_kwargs)
            acc.feed_message(response)
            return
        kwargs = dict(base_kwargs, stream=True)
        if include_usage:
            kwargs["stream_options"] = {"include_usage": True}
        stream = await client.chat.completions.create(messages=messages, **kwargs)
        async with stream:
            async for chunk in stream:
                acc.feed_chunk(chunk)
                await asyncio.sleep(0)  # stay cancellable on very fast local streams
