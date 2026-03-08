#!/usr/bin/env python3
"""
Pre-tool-use hook for CC-RLM.

Enforced guardrails:
1. No writes outside /Users/mikewahl/CC-RLM (prevents accidental repo mutations)
2. No edits to walker files that would break the subprocess protocol
   (must still have __main__ block and print JSON)
3. Warn on direct edits to context_pack.py token budget constants
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path("/Users/mikewahl/CC-RLM")
WALKER_DIR = PROJECT_ROOT / "rlm" / "walkers"
BUDGET_FILE = PROJECT_ROOT / "rlm" / "context_pack.py"


def check(tool_name: str, tool_input: dict) -> str | None:
    """Return an error message to block, or None to allow."""

    # Guard writes/edits to paths outside the project
    path_str = tool_input.get("file_path") or tool_input.get("path") or ""
    if path_str:
        path = Path(path_str)
        try:
            path.relative_to(PROJECT_ROOT)
        except ValueError:
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


def main():
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)  # not our event format, allow

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})

    error = check(tool_name, tool_input)
    if error:
        print(json.dumps({"decision": "block", "reason": error}))
        sys.exit(0)

    # Allow
    sys.exit(0)


if __name__ == "__main__":
    main()
