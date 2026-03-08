"""
Route decision logic for CCR.

Every incoming request is classified as one of:
  - REPO_TASK   → enrich via RLM, then forward to vLLM
  - FALLBACK    → forward directly to Anthropic API (no repo context)
  - PASSTHROUGH → forward directly to vLLM (health checks, embeddings, etc.)
"""

from enum import Enum

from fastapi import Request


class Route(str, Enum):
    REPO_TASK = "repo_task"
    FALLBACK = "fallback"
    PASSTHROUGH = "passthrough"


def classify(request: Request) -> Route:
    path = request.url.path

    # Non-chat endpoints go straight through to vLLM
    if not path.endswith("/chat/completions"):
        return Route.PASSTHROUGH

    # If no repo path header, fall back to Anthropic (or vLLM passthrough)
    repo_path = request.headers.get("x-cc-repo-path")
    if not repo_path:
        return Route.FALLBACK

    return Route.REPO_TASK


def extract_task_text(body: dict) -> str:
    """Pull the last user message content as the task description."""
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            # content can be a list of blocks
            if isinstance(content, list):
                return " ".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                )
    return ""
