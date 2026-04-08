#!/usr/bin/env python3
"""
PostToolUse hook — tracks read/write tool calls per session into SQLite.

Fires after Read, Glob, Grep, Edit, Write, MultiEdit.
No Ollama calls — must be fast (called after every tool use).
"""

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "eval" / "watchdog.db"

READ_TOOLS = {"Read", "Glob", "Grep"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Bash commands that are read-only
READ_BASH_PATTERN = re.compile(
    r"^\s*(ls|cat|head|tail|grep|find|wc|du|python3.*analyze|rg|ripgrep|less|more|file|stat)\b"
)


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            tool_name  TEXT NOT NULL,
            file_path  TEXT,
            ts         REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON tool_calls(session_id)")
    conn.commit()


def classify(tool_name: str, tool_input: dict) -> str | None:
    """Return 'read', 'write', or None (untracked)."""
    if tool_name in READ_TOOLS:
        return "read"
    if tool_name in WRITE_TOOLS:
        return "write"
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if READ_BASH_PATTERN.match(cmd):
            return "read"
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    session_id = event.get("session_id") or event.get("sessionId") or "unknown"

    kind = classify(tool_name, tool_input)
    if kind is None:
        sys.exit(0)

    file_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("pattern")
        or tool_input.get("command", "")[:80]
        or None
    )

    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            init_db(conn)
            conn.execute(
                "INSERT INTO tool_calls (session_id, tool_name, file_path, ts) VALUES (?, ?, ?, ?)",
                (session_id, tool_name, file_path, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # never fail the hook

    sys.exit(0)


if __name__ == "__main__":
    main()
