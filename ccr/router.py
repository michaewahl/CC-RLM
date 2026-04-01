"""
Route decision logic for CCR.

Every incoming request is classified as one of:
  - REPO_TASK   → enrich via RLM, then forward to vLLM
  - FALLBACK    → forward directly to Anthropic API (no repo context)
  - PASSTHROUGH → forward directly to vLLM (health checks, embeddings, etc.)

Classification priority:
  1. Non-chat path                    → PASSTHROUGH
  2. Explicit route_hint in state file → that route (set by UserPromptSubmit hook)
  3. Explicit x-cc-route-hint header  → that route (useful for tests/curl)
  4. Has repo context                 → REPO_TASK
  5. No repo context                  → FALLBACK
"""

import json
import logging
from enum import Enum
from pathlib import Path

from fastapi import Request

log = logging.getLogger("ccr.router")

# Written by .claude/hooks/inject_repo_context.py before each Claude Code turn.
# HIGH-3: use user-owned directory, not world-writable /tmp (prevents symlink attacks)
_STATE_FILE = Path.home() / ".cc-rlm" / "state.json"

_VALID_HINTS = {"fallback", "passthrough", "repo_task", ""}


class Route(str, Enum):
    REPO_TASK = "repo_task"
    FALLBACK = "fallback"
    PASSTHROUGH = "passthrough"


def _read_state() -> dict:
    """Read repo context + route hint written by the UserPromptSubmit hook."""
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def get_repo_context(request: Request) -> tuple[str, str]:
    """
    Return (repo_path, active_file).
    Prefers explicit headers (curl / tests) when from localhost, falls back to hook state file
    (normal Claude Code usage — no headers needed).
    """
    repo_path = ""
    active_file = ""

    # Only trust explicit headers from localhost (HIGH-1 fix)
    if _is_localhost(request):
        repo_path = request.headers.get("x-cc-repo-path", "")
        active_file = request.headers.get("x-cc-active-file", "")

    if not repo_path:
        state = _read_state()
        repo_path = state.get("repo_path", "")
        active_file = active_file or state.get("active_file", "")

    return repo_path, active_file


_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_localhost(request: Request) -> bool:
    return bool(request.client) and request.client.host in _LOCALHOST_HOSTS


def get_route_hint(request: Request) -> str:
    """
    Return an explicit route override if one was set.

    Priority:
      1. x-cc-route-hint header — only honoured from localhost (HIGH-1 fix)
      2. route_hint in state file (set by UserPromptSubmit hook classify_prompt())
      3. "" — no override, fall through to default logic
    """
    hint = request.headers.get("x-cc-route-hint", "")
    if hint in _VALID_HINTS and hint:
        if not _is_localhost(request):
            log.warning("Ignoring x-cc-route-hint from non-localhost client: %s", request.client)
        else:
            log.info("Route override via header: %s", hint)
            return hint

    state = _read_state()
    hint = state.get("route_hint", "")
    if hint and hint in _VALID_HINTS:
        log.info("Route override via hook: %s", hint)
        return hint

    return ""


def classify(request: Request) -> Route:
    path = request.url.path

    # Non-chat endpoints go straight through to vLLM
    if not path.endswith("/chat/completions"):
        return Route.PASSTHROUGH

    # Explicit route override (from hook or header)
    hint = get_route_hint(request)
    if hint == "fallback":
        return Route.FALLBACK
    if hint == "passthrough":
        return Route.PASSTHROUGH
    if hint == "repo_task":
        return Route.REPO_TASK

    # Default: presence of repo context decides
    repo_path, _ = get_repo_context(request)
    if not repo_path:
        return Route.FALLBACK

    return Route.REPO_TASK


def extract_task_text(body: dict, state: dict | None = None) -> str:
    """
    Pull the last user message content as the task description.

    If the hook stripped a route prefix (e.g. /claude was removed), use the
    stripped version so the model doesn't see the routing syntax.
    """
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                raw = content
            elif isinstance(content, list):
                raw = " ".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                )
            else:
                raw = ""

            # Use stripped prompt if the hook removed a prefix
            if state is None:
                state = _read_state()
            stripped = state.get("prompt_stripped", "")
            return stripped if stripped else raw

    return ""
