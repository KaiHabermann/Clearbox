"""Store-level guarantees submit_plan builds on: a plan written inside one transaction is all or
nothing."""

from __future__ import annotations

import pytest

from clearbox.store import PlanError


def test_transaction_rolls_back_a_plan_with_a_bad_edge(store):
    with pytest.raises(PlanError, match="cycle"):
        with store.transaction():
            pid = store.create_plan("p")
            store.add_node(pid, "a", files=["src/a.py"])
            store.add_node(pid, "b", results_from=[1])
            store.add_edge(pid, 1, 2)
            store.add_edge(pid, 2, 1)
    assert store.list_root_plans() == []
    assert store._q("SELECT * FROM nodes") == []
    assert store._q("SELECT * FROM edges") == []


def test_transaction_rolls_back_on_a_bad_node(store):
    with pytest.raises(PlanError):
        with store.transaction():
            pid = store.create_plan("p")
            store.add_node(pid, "a")
            store.add_node(pid, "b", kind="robot")
    assert store.list_root_plans() == []
    assert store._q("SELECT * FROM nodes") == []


def test_transaction_commits_when_everything_is_valid(store):
    with store.transaction():
        pid = store.create_plan("p")
        store.add_node(pid, "a")
        store.add_node(pid, "b")
        store.add_edge(pid, 1, 2)
    assert [n["title"] for n in store.nodes(pid)] == ["a", "b"]
    assert store.edges(pid) == [(1, 2)]
