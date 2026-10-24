"""In-process fixtures for the scheduler tests: a real Store on a temp file, a Bus and an Engine,
with harnesses the test controls. No server, no network.

``async def`` tests run on a fresh event loop each (see ``pytest_pyfunc_call``), so the suite
needs no asyncio plugin.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import Any, AsyncIterator

import pytest

from clearbox.engine import Bus, Engine, EngineConfig
from clearbox.harness import AgentJob, MockHarness
from clearbox.harness.registry import HarnessDefaults, HarnessRegistry
from clearbox.store import Store


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    args = {k: pyfuncitem.funcargs[k] for k in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(pyfuncitem.obj(**args))
    return True


class GateHarness:
    """A harness whose runs finish only when the test says so: ``started`` records the
    (plan_id, node_id) of every run in start order, ``finish`` ends one."""

    def __init__(self) -> None:
        self.started: list[tuple[int, int]] = []
        self._gates: dict[tuple[int, int], asyncio.Event] = {}
        self._ok: dict[tuple[int, int], bool] = {}

    def finish(self, key: tuple[int, int], ok: bool = True) -> None:
        self._ok[key] = ok
        self._gates.setdefault(key, asyncio.Event()).set()

    async def run(self, job: AgentJob) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        key = (job.plan_id, job.node_id)
        self.started.append(key)
        gate = self._gates.setdefault(key, asyncio.Event())
        yield "system", {"text": "gate session started"}
        await gate.wait()
        del self._gates[key]  # a later run of the same node waits again
        ok = self._ok.pop(key, True)
        yield "result", {"text": f"finished {key}", "is_error": not ok,
                         "errors": [] if ok else [f"{key} failed on purpose"]}


class Registry(HarnessRegistry):
    """The real registry (profile resolution, limits, labels), but every node runs on one harness."""

    def __init__(self, store: Store, harness: Any) -> None:
        super().__init__(store, HarnessDefaults(agent="mock"))
        self.harness = harness

    def implicit(self) -> Any:
        return self.harness

    def harness_for(self, profile: dict[str, Any]) -> Any:
        return self.harness


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    yield s
    s._db.close()


@contextlib.asynccontextmanager
async def running(store: Store, harness: Any, tmp_path, max_concurrency: int = 3) -> AsyncIterator[Engine]:
    """A started Engine over ``store`` whose nodes run on ``harness``; shut down on exit."""
    engine = Engine(store, Bus(), Registry(store, harness),
                    EngineConfig(workdir=str(tmp_path / "ws"), max_concurrency=max_concurrency))
    await engine.start()
    try:
        yield engine
    finally:
        await engine.shutdown()
        await asyncio.sleep(0)  # let the cancelled tasks unwind before the loop closes


def mock_harness() -> MockHarness:
    return MockHarness(speed=1000)  # a run takes a few milliseconds


async def until(cond, timeout: float = 5.0) -> None:
    """Wait until ``cond()`` is true, failing the test after ``timeout`` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError(f"condition not reached within {timeout}s")
        await asyncio.sleep(0.002)


async def settle(engine: Engine) -> None:
    """Let the scheduler run until it has nothing left to do right now."""
    for _ in range(5):
        engine.kick()
        for _ in range(20):
            await asyncio.sleep(0)


def status(store: Store, plan_id: int, node_id: int) -> str:
    return store.get_node(plan_id, node_id)["status"]
