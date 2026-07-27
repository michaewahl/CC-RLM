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

import hashlib
import json
import logging
from enum import Enum
from pathlib import Path

from fastapi import Request

from ccr.config import settings

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


# Tools only the top-level agent is given. Subagents cannot spawn further
# subagents, so a chat request advertising tools but none of these is a subagent
# turn. This has to come from the body: the UserPromptSubmit hook fires only for
# real user prompts, so the state file is written once per turn and every
# subagent in that turn would otherwise read the main agent's routing.
_MAIN_AGENT_ONLY_TOOLS = frozenset({"Task", "Agent"})


def _tool_names(body: dict) -> set[str]:
    names = set()
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or tool.get("function", {}).get("name")
        if name:
            names.add(name)
    return names


def _text_of(content) -> str:
    """Flatten a string-or-content-block-list into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("text")
        )
    return ""


def is_subagent(body: dict | None) -> bool:
    """
    True when this chat request comes from a subagent rather than the main agent.

    Heuristic, not a guarantee — Claude Code exposes no explicit agent id in the
    request. A request that advertises no tools at all is treated as the main
    agent, keeping the pre-existing behaviour as the default.
    """
    if not body:
        return False
    names = _tool_names(body)
    if not names:
        return False
    return not (names & _MAIN_AGENT_ONLY_TOOLS)


def agent_key(body: dict | None) -> str:
    """
    Stable per-context-window identity, used to partition RLM session dedup.

    Main agent → "" (preserves existing session behaviour).
    Subagent   → hash of (system prompt + first user message). The system prompt
                 alone collides across two parallel instances of the same agent
                 type; the first user message separates them by task.
    """
    if not is_subagent(body):
        return ""
    system = _text_of(body.get("system", ""))
    first_user = ""
    for msg in body.get("messages", []):
        if msg.get("role") == "user":
            first_user = _text_of(msg.get("content", ""))
            break
    return hashlib.sha256(f"{system}\x00{first_user}".encode()).hexdigest()[:16]


def classify(request: Request, body: dict | None = None) -> Route:
    path = request.url.path

    # Non-chat endpoints go straight through to vLLM
    if not path.endswith("/chat/completions"):
        return Route.PASSTHROUGH

    # Subagent split: send high-volume subagent traffic somewhere cheaper while
    # the main agent keeps its normal route. Checked before the state-file hint,
    # which describes the user's prompt, not this subagent's task.
    sub_route = settings.subagent_route
    if sub_route and sub_route in _VALID_HINTS and is_subagent(body):
        log.info("Subagent request → %s", sub_route)
        return Route(sub_route)

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

            # Only the main agent inherits the hook's stripped prompt. The hook
            # writes one prompt per user turn; applying it to a subagent would
            # replace that subagent's own task with the user's, and RLM would
            # then keyword-score the pack against the wrong text.
            if is_subagent(body):
                return raw

            # Use stripped prompt if the hook removed a prefix
            if state is None:
                state = _read_state()
            stripped = state.get("prompt_stripped", "")
            return stripped if stripped else raw

    return ""
