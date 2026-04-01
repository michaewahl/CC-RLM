#!/usr/bin/env python3
"""
UserPromptSubmit hook — repo context injector + route classifier.

Fires before every Claude Code API call.
Detects the git root of the current working directory and the most recently
modified file, then writes them to a state file that CCR reads.

Also classifies the prompt intent and writes a route_hint:
  - Explicit prefix overrides:
      /claude or /anthropic  → "fallback"   (Anthropic API — full Claude reasoning)
      /local  or /ollama     → "passthrough" (local model, skip RLM enrichment)
      /repo                  → "repo_task"  (force enrichment)
  - Auto-heuristic: pure knowledge question with no code signal → "fallback"
  - Default (in a git repo, no override): "" → CCR applies its own logic (REPO_TASK)

State file schema:
  {
    "repo_path": "/abs/path/to/repo",
    "active_file": "/abs/path/to/file.py",
    "cwd": "/current/working/dir",
    "route_hint": "fallback" | "passthrough" | "repo_task" | "",
    "prompt_stripped": "prompt with prefix removed"  (if prefix was detected)
  }
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# HIGH-3: user-owned directory instead of world-writable /tmp (prevents symlink attacks)
_STATE_DIR = Path.home() / ".cc-rlm"
STATE_FILE = str(_STATE_DIR / "state.json")

# Explicit route prefix patterns
_PREFIXES = {
    r"^/claude\s+": "fallback",
    r"^/anthropic\s+": "fallback",
    r"^/local\s+": "passthrough",
    r"^/ollama\s+": "passthrough",
    r"^/repo\s+": "repo_task",
}

# Signals that indicate a code/repo task (push toward REPO_TASK)
_CODE_SIGNALS = re.compile(
    r"\b(function|class|method|import|module|error|exception|bug|fix|refactor|"
    r"implement|test|lint|type\s+error|traceback|stack\s+trace|git|diff|commit|"
    r"\.py|\.ts|\.js|\.go|\.rs|\.java|def |async |await |return |print\(|"
    r"TODO|FIXME|PR|pull\s+request|CI|CD|deploy)\b",
    re.IGNORECASE,
)

# Signals that suggest a pure knowledge question (push toward FALLBACK)
_KNOWLEDGE_SIGNALS = re.compile(
    r"^(what\s+is|what\s+are|explain|tell\s+me\s+about|how\s+does|"
    r"what\s+does|define|describe|why\s+is|who\s+is|when\s+did|"
    r"can\s+you\s+explain|help\s+me\s+understand)\b",
    re.IGNORECASE,
)


def find_git_root(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def find_active_file(repo_root: str) -> str:
    """
    Best-effort: return the most relevant file for this task.
    Priority:
      1. Unstaged modified files (what you're actively editing)
      2. Staged files
      3. Most recent file touched in last commit
    """
    try:
        # Unstaged changes
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2,
        )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        if files:
            return str(Path(repo_root) / files[0])

        # Staged changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2,
        )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        if files:
            return str(Path(repo_root) / files[0])

        # Last commit
        result = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2,
        )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        if files:
            return str(Path(repo_root) / files[0])
    except Exception:
        pass

    return ""


def classify_prompt(prompt: str) -> tuple[str, str]:
    """
    Returns (route_hint, stripped_prompt).

    route_hint: "fallback" | "passthrough" | "repo_task" | ""
    stripped_prompt: prompt with any explicit prefix removed.
    """
    # 1. Check explicit prefix overrides
    for pattern, hint in _PREFIXES.items():
        m = re.match(pattern, prompt, re.IGNORECASE)
        if m:
            stripped = prompt[m.end():]
            return hint, stripped

    # 2. Auto-heuristic: knowledge question + no code signal → fallback
    is_knowledge = bool(_KNOWLEDGE_SIGNALS.match(prompt.strip()))
    has_code = bool(_CODE_SIGNALS.search(prompt))

    if is_knowledge and not has_code:
        return "fallback", prompt

    # 3. Default: no hint — CCR will apply its own logic
    return "", prompt


def main():
    # Read hook event from stdin (Claude Code passes JSON)
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}

    prompt = event.get("prompt", "")
    cwd = os.getcwd()

    # Clear tool-reads from previous turn — each new prompt starts fresh
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        (_STATE_DIR / "tool-reads.json").write_text("[]")
    except Exception:
        pass

    repo_root = find_git_root(cwd)

    if not repo_root:
        # Not in a git repo — clear stale state so CCR falls back
        try:
            Path(STATE_FILE).unlink(missing_ok=True)
        except Exception:
            pass
        sys.exit(0)

    active_file = find_active_file(repo_root)
    route_hint, stripped_prompt = classify_prompt(prompt)

    state = {
        "repo_path": repo_root,
        "active_file": active_file,
        "cwd": cwd,
        "route_hint": route_hint,
        "prompt_stripped": stripped_prompt if stripped_prompt != prompt else "",
    }

    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        # HIGH-3: atomic write via temp file + rename (prevents partial-read and symlink attacks)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=_STATE_DIR, prefix=".state-", suffix=".tmp", delete=False
        ) as tf:
            json.dump(state, tf)
            tmp_path = tf.name
        Path(tmp_path).rename(STATE_FILE)
    except Exception:
        pass

    # Exit 0 = allow Claude Code to continue
    sys.exit(0)


if __name__ == "__main__":
    main()
