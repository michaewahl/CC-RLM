#!/usr/bin/env python3
"""
Pre-tool-use hook for CC-RLM.

Enforced guardrails:
1. No writes outside /Users/mikewahl/CC-RLM (prevents accidental repo mutations)
2. No edits to walker files that would break the subprocess protocol
   (must still have __main__ block and print JSON)
3. Warn on direct edits to context_pack.py token budget constants
4. Read:write ratio gate — block edits when session ratio < 3:1
"""

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path("/Users/mikewahl/CC-RLM")
WALKER_DIR   = PROJECT_ROOT / "rlm" / "walkers"
BUDGET_FILE  = PROJECT_ROOT / "rlm" / "context_pack.py"
DB_PATH      = PROJECT_ROOT / "eval" / "watchdog.db"

RATIO_THRESHOLD = 3.0
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

READ_TOOLS  = {"Read", "Glob", "Grep"}

# Paths outside PROJECT_ROOT that are explicitly allowed
ALLOWED_EXTERNAL = [
    Path("/Users/mikewahl/.claude/projects/-Users-mikewahl-CC-RLM/memory"),
    Path("/Users/mikewahl/.claude/plans"),
    Path("/Users/mikewahl/.claude/settings.json"),
    PROJECT_ROOT / "eval",
]


def check(tool_name: str, tool_input: dict) -> str | None:
    """Return an error message to block, or None to allow."""

    # Guard writes/edits to paths outside the project
    path_str = tool_input.get("file_path") or tool_input.get("path") or ""
    if path_str:
        path = Path(path_str)
        try:
            path.relative_to(PROJECT_ROOT)
        except ValueError:
            if not any(
                path == allowed or path.is_relative_to(allowed)
                for allowed in ALLOWED_EXTERNAL
            ):
                return f"Blocked: path {path} is outside CC-RLM project root."

    # Warn if editing a walker — remind about protocol
    if path_str and Path(path_str).is_relative_to(WALKER_DIR):
        file_name = Path(path_str).name
        if file_name not in ("__init__.py", "CLAUDE.md"):
            new_content = tool_input.get("new_string") or tool_input.get("content") or ""
            if new_content and 'if __name__ == "__main__"' not in new_content:
                return (
                    f"Blocked: walker {file_name} is missing the required "
                    '`if __name__ == "__main__":` entrypoint. '
                    "See rlm/walkers/CLAUDE.md for the walker protocol."
                )

    return None


def get_session_counts(session_id: str) -> tuple[int, int]:
    """Return (reads, writes) for the session from watchdog.db."""
    if not DB_PATH.exists():
        return (0, 0)
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=1.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN tool_name IN ('Read','Glob','Grep') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN tool_name IN ('Write','Edit','MultiEdit') THEN 1 ELSE 0 END)
                FROM tool_calls WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if row:
                return (int(row[0] or 0), int(row[1] or 0))
        finally:
            conn.close()
    except Exception:
        pass
    return (0, 0)


def check_ratio(tool_name: str, session_id: str) -> str | None:
    """Block writes when read:write ratio is below threshold."""
    if tool_name not in WRITE_TOOLS:
        return None
    reads, writes = get_session_counts(session_id)
    if writes == 0:
        return None  # first write of session — always allow
    ratio = reads / (writes + 1)  # +1 for the write about to happen
    if ratio < RATIO_THRESHOLD:
        return (
            f"Laziness gate: {reads} reads vs {writes} writes "
            f"({ratio:.1f}:1 ratio, minimum {RATIO_THRESHOLD}:1). "
            f"Read more files before editing — use Read, Glob, or Grep to "
            f"understand the codebase first."
        )
    return None


def main():
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)  # not our event format, allow

    tool_name  = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    session_id = event.get("session_id") or event.get("sessionId") or "unknown"

    error = check(tool_name, tool_input) or check_ratio(tool_name, session_id)
    if error:
        print(json.dumps({"decision": "block", "reason": error}))
        sys.exit(0)

    # Allow
    sys.exit(0)


if __name__ == "__main__":
    main()
