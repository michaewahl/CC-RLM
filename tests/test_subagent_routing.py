"""
Subagent awareness: detection, agent-scoped session dedup, and the routing split.

Covers two bugs that predate the split:
  - task-text bleed: the hook's per-turn prompt leaking into subagent requests
  - dedup leak: a subagent denied files it never saw because the main agent did
"""

import importlib
import os
import tempfile
from pathlib import Path

import pytest

from ccr import router
from rlm import session


MAIN_BODY = {
    "tools": [{"name": "Read"}, {"name": "Grep"}, {"name": "Task"}],
    "system": "You are Claude Code.",
    "messages": [{"role": "user", "content": "fix the parser"}],
}

SUB_BODY = {
    "tools": [{"name": "Read"}, {"name": "Grep"}],
    "system": "You are the Explore agent.",
    "messages": [{"role": "user", "content": "find the token budget constant"}],
}


# ------------------------------------------------------------------
# Subagent detection
# ------------------------------------------------------------------

def test_main_agent_not_flagged_as_subagent():
    assert router.is_subagent(MAIN_BODY) is False


def test_subagent_detected_by_missing_task_tool():
    assert router.is_subagent(SUB_BODY) is True


def test_openai_style_function_wrapper_is_read():
    body = {"tools": [{"function": {"name": "Task"}}]}
    assert router.is_subagent(body) is False


def test_no_tools_defaults_to_main_agent():
    """Ambiguous request must keep pre-existing behaviour, not guess subagent."""
    assert router.is_subagent({"messages": []}) is False
    assert router.is_subagent(None) is False


# ------------------------------------------------------------------
# Agent key
# ------------------------------------------------------------------

def test_main_agent_key_is_empty():
    assert router.agent_key(MAIN_BODY) == ""


def test_subagent_key_is_stable():
    assert router.agent_key(SUB_BODY) == router.agent_key(dict(SUB_BODY))


def test_parallel_same_type_subagents_get_distinct_keys():
    """Same system prompt, different task — must not share dedup state."""
    other = {**SUB_BODY, "messages": [{"role": "user", "content": "find the cache TTL"}]}
    assert router.agent_key(SUB_BODY) != router.agent_key(other)


def test_agent_key_handles_content_block_lists():
    body = {
        "tools": [{"name": "Read"}],
        "system": [{"type": "text", "text": "You are Explore."}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "find X"}]}],
    }
    assert router.agent_key(body)  # non-empty, no crash


# ------------------------------------------------------------------
# Bug 1: task-text bleed
# ------------------------------------------------------------------

def test_main_agent_uses_hook_stripped_prompt():
    state = {"prompt_stripped": "fix the parser"}
    assert router.extract_task_text(MAIN_BODY, state) == "fix the parser"


def test_subagent_keeps_own_task_when_prefix_was_used():
    """
    User types '/repo fix the parser'; the hook writes prompt_stripped for the
    turn. Every subagent in that turn must still see its own task.
    """
    state = {"prompt_stripped": "fix the parser"}
    assert router.extract_task_text(SUB_BODY, state) == "find the token budget constant"


# ------------------------------------------------------------------
# Bug 2: dedup leaking across context windows
# ------------------------------------------------------------------

@pytest.fixture
def repo_file():
    session.clear_all()
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "mod.py"
        f.write_text("def a():\n    pass\n")
        yield d, str(f)
    session.clear_all()


def test_main_agent_dedups_on_second_pass(repo_file):
    repo, f = repo_file
    assert session.already_seen(repo, f, "") is False
    assert session.already_seen(repo, f, "") is True


def test_subagent_not_deduped_by_main_agent_history(repo_file):
    """The core fix: a fresh context window has seen nothing."""
    repo, f = repo_file
    session.already_seen(repo, f, "")            # main agent sees it
    assert session.already_seen(repo, f, "sub1") is False
    assert session.already_seen(repo, f, "sub1") is True


def test_subagents_do_not_dedup_each_other(repo_file):
    repo, f = repo_file
    session.already_seen(repo, f, "sub1")
    assert session.already_seen(repo, f, "sub2") is False


def test_changed_file_reinjected_for_same_agent(repo_file):
    repo, f = repo_file
    session.already_seen(repo, f, "sub1")
    p = Path(f)
    p.write_text("def a():\n    return 1\n")
    os.utime(p, (p.stat().st_atime + 10, p.stat().st_mtime + 10))
    assert session.already_seen(repo, f, "sub1") is False


def test_invalidate_clears_every_agent_session(repo_file):
    repo, f = repo_file
    session.already_seen(repo, f, "")
    session.already_seen(repo, f, "sub1")
    session.invalidate(repo)
    assert session.already_seen(repo, f, "") is False
    assert session.already_seen(repo, f, "sub1") is False


def test_stats_counts_agent_sessions(repo_file):
    repo, f = repo_file
    session.already_seen(repo, f, "")
    session.already_seen(repo, f, "sub1")
    assert session.stats(repo)["agent_sessions"] == 2


# ------------------------------------------------------------------
# Routing split
# ------------------------------------------------------------------

class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    def __init__(self, path: str):
        self.url = type("U", (), {"path": path})()
        self.headers = {}
        self.client = _FakeClient()


@pytest.fixture
def router_with_split(monkeypatch):
    """Reload router with CCR_SUBAGENT_ROUTE set, then restore."""
    monkeypatch.setenv("CCR_SUBAGENT_ROUTE", "passthrough")
    import ccr.config
    importlib.reload(ccr.config)
    importlib.reload(router)
    yield router
    monkeypatch.delenv("CCR_SUBAGENT_ROUTE", raising=False)
    importlib.reload(ccr.config)
    importlib.reload(router)


def test_split_disabled_by_default():
    from ccr.config import CCRSettings
    assert CCRSettings(_env_file=None).subagent_route == ""


def test_split_sends_subagents_to_configured_route(router_with_split, monkeypatch):
    monkeypatch.setattr(router_with_split, "_read_state", lambda: {"repo_path": "/tmp/x"})
    req = _FakeRequest("/v1/chat/completions")
    assert router_with_split.classify(req, SUB_BODY) == router_with_split.Route.PASSTHROUGH


def test_split_leaves_main_agent_alone(router_with_split, monkeypatch):
    monkeypatch.setattr(router_with_split, "_read_state", lambda: {"repo_path": "/tmp/x"})
    req = _FakeRequest("/v1/chat/completions")
    assert router_with_split.classify(req, MAIN_BODY) == router_with_split.Route.REPO_TASK


def test_split_ignores_non_chat_paths(router_with_split):
    req = _FakeRequest("/health")
    assert router_with_split.classify(req, SUB_BODY) == router_with_split.Route.PASSTHROUGH
