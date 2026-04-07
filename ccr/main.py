"""
CCR — Claude Code Router

Sits between Claude Code and the rest of the stack.
Intercepts /v1/chat/completions calls, enriches repo-scoped tasks via RLM,
then streams the response back from vLLM.
Falls back to Anthropic API when no repo context is available.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from ccr.config import settings
from ccr.router import Route, classify, extract_task_text, get_repo_context, _read_state
from ccr.skill_pruner import prune_tools

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ccr")

app = FastAPI(title="CCR — Claude Code Router")

_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def startup():
    global _client
    _client = httpx.AsyncClient(timeout=120.0)
    log.info("CCR started on port %d", settings.port)
    log.info("  RLM → %s", settings.rlm_url)
    log.info("  vLLM → %s", settings.vllm_url)


@app.on_event("shutdown")
async def shutdown():
    if _client:
        await _client.aclose()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str):
    route = classify(request)
    body_bytes = await request.body()

    if route == Route.PASSTHROUGH:
        return await _forward(request, path, body_bytes, settings.vllm_url)

    if route == Route.FALLBACK:
        if settings.fallback_enabled and settings.anthropic_fallback_key:
            log.info("FALLBACK → Anthropic API")
            return await _forward_anthropic(request, path, body_bytes)
        return await _forward(request, path, body_bytes, settings.vllm_url)

    # REPO_TASK: enrich via RLM
    body = json.loads(body_bytes)
    state = _read_state()
    task = extract_task_text(body, state)
    repo_path, active_file = get_repo_context(request)

    log.info("REPO_TASK  repo=%s  file=%s", repo_path, active_file)
    log.info("  task preview: %s", task[:120])

    start_time = time.monotonic()
    enriched_body, files_in_pack, pack_tokens, naive_tokens, pruned_tools, original_tools = await _enrich(body, task, active_file, repo_path)
    return await _stream_vllm(request, enriched_body, repo_path, files_in_pack, pack_tokens, naive_tokens, start_time, pruned_tools, original_tools)


_NAIVE_SKIP_DIRS = {"__pycache__", ".venv", "node_modules", "tests", ".git"}
_NAIVE_SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx"}
_NAIVE_MAX_FILES = 500  # hard cap — prevents blocking on huge repos or /


def _count_naive_tokens(repo_path: str) -> int:
    """Synchronous file scan — always call via run_in_executor."""
    total = 0
    count = 0
    root = Path(repo_path)
    root_parts = len(root.parts)
    for p in root.rglob("*"):
        if count >= _NAIVE_MAX_FILES:
            break
        if not p.is_file() or p.suffix not in _NAIVE_SOURCE_EXTS:
            continue
        if any(part in _NAIVE_SKIP_DIRS or part.startswith(".") for part in p.parts[root_parts:]):
            continue
        count += 1
        try:
            total += len(p.read_text(errors="ignore")) // 4
        except Exception:
            pass
    return total


async def _enrich(body: dict, task: str, active_file: str, repo_path: str) -> tuple[dict, list[str], int, int, int, int]:
    """Call RLM Gateway to get context pack and inject it as system message.
    Returns (enriched_body, files_in_pack, pack_tokens, naive_tokens, pruned_tools, original_tools)."""
    files_in_pack: list[str] = []
    pack_tokens = 0
    naive_tokens = 0
    try:
        resp = await _client.post(
            f"{settings.rlm_url}/context",
            json={"task": task, "active_file": active_file, "repo_path": repo_path},
            timeout=10.0,
        )
        resp.raise_for_status()
        pack = resp.json()
        system_preamble = pack.get("rendered", "")
        files_in_pack = pack.get("pack", {}).get("files_in_pack", [])
        pack_tokens = pack.get("token_count", 0)

        # Naive baseline: total tokens if all repo source files were included in full.
        # Run in executor — rglob + read_text must not block the async event loop.
        if repo_path:
            naive_tokens = await asyncio.get_event_loop().run_in_executor(
                None, _count_naive_tokens, repo_path
            )
    except Exception as exc:
        log.warning("RLM enrichment failed (%s), continuing without context", exc)
        system_preamble = ""

    if not system_preamble:
        # Still run pruner even when enrichment is skipped
        pruned_count, original_count = _apply_pruner(body, task)
        return body, files_in_pack, pack_tokens, naive_tokens, pruned_count, original_count

    messages = body.get("messages", [])
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = system_preamble + "\n\n" + messages[0]["content"]
    else:
        messages = [{"role": "system", "content": system_preamble}] + messages

    enriched = {**body, "messages": messages}
    pruned_count, original_count = _apply_pruner(enriched, task)
    return enriched, files_in_pack, pack_tokens, naive_tokens, pruned_count, original_count


def _apply_pruner(body: dict, task: str) -> tuple[int, int]:
    """Prune tools in-place. Returns (pruned_count, original_count)."""
    original = body.get("tools")
    if not original or not settings.skill_pruner_enabled:
        return 0, 0
    original_count = len(original)
    pruned = prune_tools(original, task, settings.skill_pruner_max_tools)
    body["tools"] = pruned
    pruned_count = len(pruned)
    # Clear tool_choice if it names a tool no longer in the pruned list
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, dict):
        chosen_name = tool_choice.get("function", {}).get("name") or tool_choice.get("name")
        pruned_names = {t.get("name") for t in pruned}
        if chosen_name and chosen_name not in pruned_names:
            body.pop("tool_choice", None)
    return pruned_count, original_count


async def _stream_vllm(
    request: Request,
    body: dict,
    repo_path: str = "",
    files_in_pack: list[str] | None = None,
    pack_tokens: int = 0,
    naive_tokens: int = 0,
    start_time: float | None = None,
    pruned_tools: int = 0,
    original_tools: int = 0,
):
    """
    Stream response from vLLM.
    Intercepts chunks to accumulate the response text, then fires a feedback
    POST to RLM after the stream completes (answer-driven relevance scoring).
    Prepends a CC-RLM savings annotation to the first content chunk.
    """
    body["stream"] = True
    if settings.model_override:
        body["model"] = settings.model_override
    target = f"{settings.vllm_url}/v1/chat/completions"

    async def generate():
        response_parts: list[str] = []

        # Emit savings annotation before the upstream stream begins
        if pack_tokens > 0 and naive_tokens > pack_tokens:
            latency_ms = round((time.monotonic() - start_time) * 1000) if start_time else 0
            savings_pct = round((1 - pack_tokens / naive_tokens) * 100)
            tools_part = f" · {pruned_tools}/{original_tools} tools" if original_tools > 0 else ""
            annotation = (
                f"[CC-RLM ▸ {pack_tokens / 1000:.1f}K tokens packed"
                f" · {savings_pct}% saved vs naive"
                f"{tools_part}"
                f" · {latency_ms}ms]\n\n"
            )
            annotation_event = {
                "id": "ccrlm-savings",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": annotation}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(annotation_event)}\n\n".encode()
            log.info("CC-RLM savings: %dK packed / %dK naive = %d%% saved, %dms",
                     pack_tokens // 1000, naive_tokens // 1000, savings_pct, latency_ms)

        async with _client.stream(
            "POST",
            target,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=120.0,
        ) as resp:
            async for chunk in resp.aiter_bytes():
                yield chunk
                # Accumulate SSE content for feedback
                if repo_path and files_in_pack:
                    try:
                        text = chunk.decode("utf-8", errors="ignore")
                        for line in text.splitlines():
                            if line.startswith("data:") and "[DONE]" not in line:
                                data = json.loads(line[5:].strip())
                                delta = data.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    response_parts.append(content)
                    except Exception:
                        pass

        # Stream done — fire feedback in background (non-blocking)
        if repo_path and files_in_pack and response_parts:
            full_response = "".join(response_parts)
            try:
                await _client.post(
                    f"{settings.rlm_url}/feedback",
                    json={
                        "repo_path": repo_path,
                        "files_in_pack": files_in_pack,
                        "response_text": full_response,
                    },
                    timeout=3.0,
                )
            except Exception as exc:
                log.debug("Feedback post failed (non-fatal): %s", exc)

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _forward(request: Request, path: str, body: bytes, base_url: str):
    # CRIT-3: sanitize path before appending to base_url to prevent SSRF.
    # Reject path traversal sequences, authority-injection characters, and embedded schemes.
    safe_path = path.lstrip("/")
    if ".." in safe_path or "@" in safe_path or "://" in safe_path:
        from fastapi.responses import JSONResponse
        log.warning("Blocked suspicious forwarded path: %s", path)
        return JSONResponse({"error": "invalid path"}, status_code=400)
    target = f"{base_url.rstrip('/')}/{safe_path}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    resp = await _client.request(
        method=request.method,
        url=target,
        content=body,
        headers=headers,
        params=dict(request.query_params),
    )
    return StreamingResponse(
        iter([resp.content]),
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


async def _forward_anthropic(request: Request, path: str, body: bytes):
    headers = {
        "x-api-key": settings.anthropic_fallback_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    # Anthropic uses /v1/messages, not /v1/chat/completions
    # CCR does a best-effort passthrough; full translation is Phase 1 scope
    resp = await _client.post(
        "https://api.anthropic.com/v1/messages",
        content=body,
        headers=headers,
    )
    return StreamingResponse(
        iter([resp.content]),
        status_code=resp.status_code,
        headers={"content-type": resp.headers.get("content-type", "application/json")},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ccr.main:app", host="0.0.0.0", port=settings.port, reload=True)
