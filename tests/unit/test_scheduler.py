"""The scheduler rules, in-process: readiness, failures, file conflicts, user nodes, the per-plan
cap, round-robin across root plans and stranded subplan nodes."""

from __future__ import annotations

from conftest import GateHarness, mock_harness, running, settle, status, until


def plan(store, nodes: int, edges=(), **kw) -> int:
    pid = store.create_plan("p", **kw)
    for i in range(nodes):
        store.add_node(pid, f"n{i + 1}")
    for a, b in edges:
        store.add_edge(pid, a, b)
    return pid


async def test_readiness_follows_edges(store, tmp_path):
    # 1 -> 2 -> 4, 1 -> 3 -> 4: a node starts only once every prerequisite is done
    pid = plan(store, 4, [(1, 2), (1, 3), (2, 4), (3, 4)])
    async with running(store, mock_harness(), tmp_path) as engine:
        engine.start_plan(pid)
        await until(lambda: status(store, pid, 4) == "done")
    nodes = {n["node_id"]: n for n in store.nodes(pid)}
    assert {n["status"] for n in nodes.values()} == {"done"}
    for a, b in [(1, 2), (1, 3), (2, 4), (3, 4)]:
        assert nodes[a]["finished_at"] <= nodes[b]["started_at"]


async def test_failed_node_blocks_dependents(store, tmp_path):
    pid = plan(store, 3, [(1, 2)])  # 3 is independent
    gate = GateHarness()
    async with running(store, gate, tmp_path) as engine:
        engine.start_plan(pid)
        await until(lambda: len(gate.started) == 2)
        gate.finish((pid, 1), ok=False)
        gate.finish((pid, 3))
        await until(lambda: status(store, pid, 3) == "done")
        await settle(engine)
        assert status(store, pid, 1) == "failed"
        assert status(store, pid, 2) == "pending"
        assert (pid, 2) not in gate.started
        assert engine.plan_status(pid) == "failed"


async def test_file_conflicts_run_lowest_number_first(store, tmp_path):
    # 2 and 3 share a file and are both ready; 2 sits behind a busy profile (max_parallel 1,
    # taken by 1). 3 has a free slot but must still wait for its turn, then for 2's run.
    pid = store.create_plan("p")
    prof = store.create_profile(name="one-at-a-time", harness="mock", max_parallel=1)
    store.add_node(pid, "n1", agent_profile_id=prof)
    store.add_node(pid, "n2", agent_profile_id=prof, files=["src/"])
    store.add_node(pid, "n3", files=["src/a.py"])
    gate = GateHarness()
    async with running(store, gate, tmp_path) as engine:
        engine.start_plan(pid)
        await until(lambda: gate.started == [(pid, 1)])
        await settle(engine)
        assert gate.started == [(pid, 1)]
        assert engine.files_blocked_by(pid, 3) == ((pid, 2), "turn")

        gate.finish((pid, 1))
        await until(lambda: (pid, 2) in gate.started)
        await settle(engine)
        assert (pid, 3) not in gate.started
        assert engine.files_blocked_by(pid, 3) == ((pid, 2), "running")

        gate.finish((pid, 2))
        await until(lambda: (pid, 3) in gate.started)
        assert gate.started == [(pid, 1), (pid, 2), (pid, 3)]


async def test_user_node_is_never_started(store, tmp_path):
    pid = store.create_plan("p")
    store.add_node(pid, "push the branch", kind="user", files=["src/a.py"])
    store.add_node(pid, "after", files=["src/a.py"])  # shares a file, but a parked user node holds none
    store.add_node(pid, "independent", files=["src/a.py"])
    store.add_edge(pid, 1, 2)
    gate = GateHarness()
    async with running(store, gate, tmp_path) as engine:
        engine.start_plan(pid)
        await until(lambda: (pid, 3) in gate.started)
        await settle(engine)
        assert gate.started == [(pid, 3)]
        assert status(store, pid, 1) == "ready" and engine.waiting_for_user(store.get_node(pid, 1))
        assert status(store, pid, 2) == "pending"

        await engine.complete_node_manually(pid, 1, "https://example.invalid/mr/1")
        gate.finish((pid, 3))
        await until(lambda: (pid, 2) in gate.started)
        assert store.get_node(pid, 1)["result"] == "https://example.invalid/mr/1"
        assert (pid, 1) not in gate.started


async def test_plan_max_parallel_caps_the_whole_tree(store, tmp_path):
    pid = plan(store, 3, max_parallel=2)
    sub = store.create_plan("sub", parent_plan_id=pid, parent_node_id=3)
    store.add_node(sub, "s1")
    gate = GateHarness()
    async with running(store, gate, tmp_path, max_concurrency=10) as engine:
        engine.start_plan(pid)
        await until(lambda: len(gate.started) == 2)
        await settle(engine)
        assert sorted(gate.started) == [(pid, 1), (pid, 2)]  # (sub, 1) is ready but over the cap
        assert status(store, sub, 1) == "ready"

        gate.finish((pid, 1))
        await until(lambda: (sub, 1) in gate.started)
        await settle(engine)
        assert len(gate.started) == 3


async def test_round_robin_across_root_plans(store, tmp_path):
    a = plan(store, 2)
    b = plan(store, 2)
    gate = GateHarness()
    async with running(store, gate, tmp_path, max_concurrency=1) as engine:
        engine.start_plan(a)
        engine.start_plan(b)
        for i in range(4):
            await until(lambda: len(gate.started) == i + 1)
            gate.finish(gate.started[-1])
        await until(lambda: engine.plan_status(a) == engine.plan_status(b) == "done")
    assert gate.started == [(a, 1), (b, 1), (a, 2), (b, 2)]


async def test_stranded_node_in_failed_subplan_never_starts(store, tmp_path):
    # Root nodes 1 and 2 wrap subplans A = {a1, a2} and B = {b1}; a2 and b1 share a file, and
    # a2 ranks lower. a1 fails, failing container 1: a2 is left "ready" and must never start,
    # nor keep b1 waiting for a turn that never comes.
    pid = plan(store, 2)
    sub_a = store.create_plan("A", parent_plan_id=pid, parent_node_id=1)
    store.add_node(sub_a, "a1")
    store.add_node(sub_a, "a2", files=["shared.txt"])
    sub_b = store.create_plan("B", parent_plan_id=pid, parent_node_id=2)
    store.add_node(sub_b, "b1", files=["shared.txt"])
    gate = GateHarness()
    async with running(store, gate, tmp_path, max_concurrency=1) as engine:
        engine.start_plan(pid)
        await until(lambda: gate.started == [(sub_a, 1)])
        gate.finish((sub_a, 1), ok=False)
        await until(lambda: (sub_b, 1) in gate.started)
        assert status(store, pid, 1) == "failed"
        gate.finish((sub_b, 1))
        await until(lambda: status(store, pid, 2) == "done")
        await settle(engine)
        assert status(store, sub_a, 2) == "ready"
        assert gate.started == [(sub_a, 1), (sub_b, 1)]
