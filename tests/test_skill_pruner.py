"""Unit tests for ccr.skill_pruner."""

import pytest
from ccr.skill_pruner import prune_tools, ALWAYS_KEEP


def _make_tools(*names: str) -> list[dict]:
    return [{"name": n, "description": f"tool {n}", "input_schema": {}} for n in names]


ALL_CLAUDE_TOOLS = _make_tools(
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "WebFetch", "WebSearch", "Agent", "TodoWrite", "MultiEdit",
)


def test_read_only_task():
    result = prune_tools(ALL_CLAUDE_TOOLS, "what does this function do?")
    names = {t["name"] for t in result}
    # Should include read-category tools and ALWAYS_KEEP
    assert "Read" in names
    assert "Glob" in names
    assert "Grep" in names
    # Should not include write or web tools
    assert "Write" not in names
    assert "WebFetch" not in names
    assert "Agent" not in names


def test_implement_task():
    result = prune_tools(ALL_CLAUDE_TOOLS, "implement a new endpoint for login")
    names = {t["name"] for t in result}
    assert "Read" in names
    assert "Write" in names
    assert "Edit" in names
    assert "Bash" in names


def test_always_keep_present():
    result = prune_tools(ALL_CLAUDE_TOOLS, "what is X")
    names = {t["name"] for t in result}
    for tool in ALWAYS_KEEP:
        assert tool in names


def test_mcp_tools_fallback():
    """If all tools have non-standard MCP names, return original list unchanged."""
    mcp_tools = _make_tools("mcp__slack__send", "mcp__github__pr_create", "mcp__jira__comment")
    result = prune_tools(mcp_tools, "what does this do?")
    assert result == mcp_tools


def test_empty_tools():
    assert prune_tools([], "implement something") == []


def test_max_tools_cap():
    many = _make_tools(
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
        "WebFetch", "WebSearch", "Agent", "TodoWrite",
    )
    result = prune_tools(many, "implement and search and test everything", max_tools=4)
    assert len(result) <= 4
    names = {t["name"] for t in result}
    # ALWAYS_KEEP should survive truncation
    for tool in ALWAYS_KEEP:
        assert tool in names


def test_no_match_defaults_to_read_shell():
    result = prune_tools(ALL_CLAUDE_TOOLS, "xyzzy something unfamiliar")
    names = {t["name"] for t in result}
    assert "Read" in names
    assert "Bash" in names
    assert "Write" not in names
    assert "WebFetch" not in names
