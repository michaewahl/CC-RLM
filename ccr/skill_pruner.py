"""
Skill Pruner — reduce the tool schema array before sending to the local model.

Local 7B/8B models degrade when given too many tool schemas. This module
trims the `tools` list to only what the task likely needs, plus a small
set of always-safe tools.
"""

from __future__ import annotations

# Tools always included regardless of task keywords.
ALWAYS_KEEP = {"Read", "Bash"}

# Named groups of Claude Code tool names.
_CATEGORIES: dict[str, set[str]] = {
    "read":   {"Read", "Glob", "Grep"},
    "write":  {"Write", "Edit", "MultiEdit", "NotebookEdit"},
    "shell":  {"Bash"},
    "search": {"WebFetch", "WebSearch"},
    "agent":  {"Agent", "TodoWrite"},
}

# Keyword → list of category names to include.
_KEYWORD_MAP: list[tuple[str, list[str]]] = [
    # Read-only / exploration
    ("what",     ["read"]),
    ("explain",  ["read"]),
    ("show",     ["read"]),
    ("find",     ["read"]),
    ("list",     ["read"]),
    ("where",    ["read"]),
    ("how does", ["read"]),
    # Write / implementation
    ("write",      ["read", "write", "shell"]),
    ("create",     ["read", "write", "shell"]),
    ("add",        ["read", "write", "shell"]),
    ("implement",  ["read", "write", "shell"]),
    ("refactor",   ["read", "write", "shell"]),
    ("fix",        ["read", "write", "shell"]),
    ("update",     ["read", "write", "shell"]),
    ("delete",     ["read", "write", "shell"]),
    ("remove",     ["read", "write", "shell"]),
    # Shell / execution
    ("test",   ["read", "shell"]),
    ("run",    ["read", "shell"]),
    ("exec",   ["read", "shell"]),
    ("build",  ["read", "shell"]),
    # Web search
    ("search",   ["read", "search"]),
    ("web",      ["read", "search"]),
    ("look up",  ["read", "search"]),
    ("fetch",    ["read", "search"]),
]

# Default categories when no keyword matches.
_DEFAULT_CATEGORIES = ["read", "shell"]


def prune_tools(tools: list[dict], task: str, max_tools: int = 6) -> list[dict]:
    """Return a pruned copy of *tools* relevant to *task*.

    Safe fallback: if the pruned list is empty (e.g. all tools are MCP tools
    with non-standard names that don't match any category), the original list
    is returned unchanged so the model still has something to work with.
    """
    if not tools:
        return tools

    lower_task = task.lower()

    # Collect allowed categories based on keyword hits.
    matched_categories: set[str] = set()
    for keyword, categories in _KEYWORD_MAP:
        if keyword in lower_task:
            matched_categories.update(categories)

    if not matched_categories:
        matched_categories.update(_DEFAULT_CATEGORIES)

    # Build the allowed tool name set.
    allowed: set[str] = set(ALWAYS_KEEP)
    for cat in matched_categories:
        allowed.update(_CATEGORIES.get(cat, set()))

    # Filter — keep known tools in the allowed set; unknown names (MCP, custom)
    # are dropped because they're likely irrelevant for a local-model turn.
    pruned = [t for t in tools if t.get("name") in allowed]

    # Safe fallback: all tools were non-standard names, pass through unchanged.
    if not pruned:
        return tools

    # Guard: cap at max_tools, keeping ALWAYS_KEEP entries first.
    if len(pruned) > max_tools:
        priority = [t for t in pruned if t.get("name") in ALWAYS_KEEP]
        rest = [t for t in pruned if t.get("name") not in ALWAYS_KEEP]
        pruned = (priority + rest)[:max_tools]

    return pruned
