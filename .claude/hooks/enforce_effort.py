#!/usr/bin/env python3
"""
SessionStart hook — ensures effortLevel stays at 'high' in ~/.claude/settings.json.

CC manages effortLevel internally and can flush 'low' back to disk at session
boundaries. This hook runs at session start and silently restores 'high' if
it has been reset.
"""
import json
import os
import sys
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
TARGET_LEVEL = "high"


def main() -> None:
    try:
        raw = sys.stdin.read()  # consume stdin (required for hooks)
    except Exception:
        pass

    try:
        text = SETTINGS_PATH.read_text()
        data = json.loads(text)
    except Exception:
        sys.exit(0)

    if data.get("effortLevel") == TARGET_LEVEL:
        sys.exit(0)  # already correct, nothing to do

    data["effortLevel"] = TARGET_LEVEL
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, SETTINGS_PATH)


if __name__ == "__main__":
    main()
