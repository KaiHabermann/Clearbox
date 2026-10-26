"""What an agent may do: an unset capability follows --default-capability for workers of every
harness, an explicit one wins, reviewers keep their own rule."""

from __future__ import annotations

import pytest

from clearbox.harness.registry import HarnessDefaults, HarnessRegistry

LOCAL = "http://localhost:11434/v1"  # local endpoint: no api key needed to build the harness


def registry(store, default_capability: str = "full") -> HarnessRegistry:
    return HarnessRegistry(store, HarnessDefaults(default_capability=default_capability))


def profile(store, name: str, harness: str, **fields):
    if harness == "openai":
        fields = {"model": "m", "base_url": LOCAL, **fields}
    return store.get_profile(store.create_profile(name=name, harness=harness, **fields))


def granted(harness) -> set[str]:
    """The capability-relevant tools a built harness got, in one vocabulary for both kinds."""
    if hasattr(harness, "allowed_tools"):  # claude-sdk
        tools = set(harness.allowed_tools)
        return ({"shell"} if "Bash" in tools else set()) | ({"edit"} if "Edit" in tools else set())
    cfg = harness.tools_config  # openai
    if cfg.get("preset") == "all":
        return {"shell", "edit"}
    allow = set(cfg.get("allow") or [])
    return ({"shell"} if "bash" in allow else set()) | ({"edit"} if "edit_file" in allow else set())


ACCESS = {"read": set(), "run": {"shell"}, "edit": {"edit"}, "full": {"shell", "edit"}}


@pytest.mark.parametrize("harness", ["claude-sdk", "openai"])
@pytest.mark.parametrize("default", ["full", "run", "read"])
def test_unset_capability_follows_the_server_default(store, harness, default):
    reg = registry(store, default)
    p = profile(store, "w", harness)
    assert reg.capability_for(p) == {"capability": default, "source": "default"}
    assert granted(reg.harness_for(p)) == ACCESS[default]


@pytest.mark.parametrize("harness", ["claude-sdk", "openai"])
def test_explicit_capability_ignores_the_server_default(store, harness):
    reg = registry(store, "read")
    p = profile(store, "w", harness, capability="edit")
    assert reg.capability_for(p) == {"capability": "edit", "source": "profile"}
    assert granted(reg.harness_for(p)) == ACCESS["edit"]


@pytest.mark.parametrize("harness", ["claude-sdk", "openai"])
def test_reviewers_keep_their_own_rule(store, harness):
    reg = registry(store, "read")
    p = profile(store, "r", harness, capability="full")
    assert reg.review_capability_for(p) == "run"
    assert granted(reg.review_harness_for(p)) == ACCESS["run"]
    q = profile(store, "r2", harness, params={"review_capability": "read"})
    assert granted(reg.review_harness_for(q)) == ACCESS["read"]


def test_raw_tools_config_is_reported_as_custom(store):
    reg = registry(store, "read")
    p = profile(store, "w", "openai", tools={"preset": "all"})
    assert reg.capability_for(p)["source"] == "custom"
    assert granted(reg.harness_for(p)) == ACCESS["full"]  # the escape hatch still wins


def test_implicit_harness_follows_the_server_default(store):
    reg = registry(store, "run")
    assert reg.capability_for(None) == {"capability": "run", "source": "default"}
    assert granted(reg.implicit()) == ACCESS["run"]
    assert reg.describe(None)["capability"] == "run"
