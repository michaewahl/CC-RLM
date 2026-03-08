# CCR — Claude Code Router

Thin proxy between Claude Code and the rest of the stack.

## Responsibility

- Intercept all `/v1/chat/completions` calls from Claude Code
- Classify: REPO_TASK → enrich via RLM | FALLBACK → Anthropic API | PASSTHROUGH → vLLM
- Inject the RLM context pack as a system preamble before forwarding
- Stream responses back to Claude Code

## Files

| File | Role |
|---|---|
| `main.py` | FastAPI app, lifespan, catch-all route handler |
| `router.py` | `classify()` and `extract_task_text()` — pure functions, no I/O |
| `config.py` | Pydantic-settings, all config via env vars prefixed `CCR_` |

## Classification logic (router.py)

1. Non-chat path → PASSTHROUGH (health checks, embeddings)
2. No `x-cc-repo-path` header → FALLBACK
3. Everything else → REPO_TASK

## Key behaviours

- RLM enrichment failure is non-fatal — logs warning, continues with unenriched prompt
- Streaming is always on for REPO_TASK (vLLM SSE passthrough)
- Anthropic fallback does NOT do message format translation yet (Phase 1 scope)

## Adding new route logic

Edit `router.py:classify()`. Return one of the `Route` enum values.
Do not add I/O to `router.py` — it should stay pure for testability.
