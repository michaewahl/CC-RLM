"""
CCR — Claude Code Router

Sits between Claude Code and the rest of the stack.
Intercepts /v1/chat/completions calls, enriches repo-scoped tasks via RLM,
then streams the response back from vLLM.
Falls back to Anthropic API when no repo context is available.
"""

import json
import logging
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from ccr.config import settings
from ccr.router import Route, classify, extract_task_text, get_repo_context, _read_state

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
    enriched_body, files_in_pack, pack_tokens, naive_tokens = await _enrich(body, task, active_file, repo_path)
    return await _stream_vllm(request, enriched_body, repo_path, files_in_pack, pack_tokens, naive_tokens, start_time)


async def _enrich(body: dict, task: str, active_file: str, repo_path: str) -> tuple[dict, list[str], int, int]:
    """Call RLM Gateway to get context pack and inject it as system message.
    Returns (enriched_body, files_in_pack, pack_tokens, naive_tokens)."""
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

        # Naive baseline: total tokens if all repo source files were included in full
        _SKIP_DIRS = {"__pycache__", ".venv", "node_modules", "tests", ".git"}
        _SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx"}
        if repo_path:
            for p in Path(repo_path).rglob("*"):
                if not p.is_file() or p.suffix not in _SOURCE_EXTS:
                    continue
                if any(part in _SKIP_DIRS or part.startswith(".") for part in p.parts[len(Path(repo_path).parts):]):
                    continue
                try:
                    naive_tokens += len(p.read_text(errors="ignore")) // 4
                except Exception:
                    pass
    except Exception as exc:
        log.warning("RLM enrichment failed (%s), continuing without context", exc)
        system_preamble = ""

    if not system_preamble:
        return body, files_in_pack, pack_tokens, naive_tokens

    messages = body.get("messages", [])
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = system_preamble + "\n\n" + messages[0]["content"]
    else:
        messages = [{"role": "system", "content": system_preamble}] + messages

    return {**body, "messages": messages}, files_in_pack, pack_tokens, naive_tokens


async def _stream_vllm(
    request: Request,
    body: dict,
    repo_path: str = "",
    files_in_pack: list[str] | None = None,
    pack_tokens: int = 0,
    naive_tokens: int = 0,
    start_time: float | None = None,
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
            annotation = (
                f"[CC-RLM ▸ {pack_tokens / 1000:.1f}K tokens packed"
                f" · {savings_pct}% saved vs naive"
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
    target = f"{base_url}/{path}"
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
