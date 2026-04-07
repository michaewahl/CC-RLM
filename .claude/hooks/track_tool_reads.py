#!/usr/bin/env python3
"""
PostToolUse hook — tool-call awareness tracker.

Fires after each Read, Edit, Write, or MultiEdit tool call.
Records which files Claude has in its context window as a result of tool calls,
so the RLM session deduplicator knows not to re-inject them.

This closes the gap where Claude reads a file via the Read tool, then RLM
injects the same file again in the next turn's context pack — doubling the tokens.

State file: ~/.cc-rlm/tool-reads.json
  ["abs/path/to/file.py", ...]

Cleared by inject_repo_context.py at the start of each new user turn.
"""

import json
import sys
from pathlib import Path

TOOL_READS_FILE = Path.home() / ".cc-rlm" / "tool-reads.json"


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    tool_input = event.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("path") or ""

    if not file_path:
        sys.exit(0)

    # Append to tool-reads list (deduplicated)
    try:
        existing: list[str] = json.loads(TOOL_READS_FILE.read_text())
    except Exception:
        existing = []

    if file_path not in existing:
        existing.append(file_path)
        try:
            TOOL_READS_FILE.write_text(json.dumps(existing))
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
