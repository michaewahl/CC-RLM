# CCR — Claude Code Router

Thin proxy between Claude Code and the rest of the stack.

## Responsibility

- Intercept all `/v1/chat/completions` calls from Claude Code
- Classify → route → enrich (if REPO_TASK) → stream response back
- Routes: REPO_TASK → RLM enriched → Ollama | FALLBACK → Anthropic | PASSTHROUGH → Ollama direct

## Files

| File | Role |
|---|---|
| `main.py` | FastAPI app, lifespan, catch-all route handler |
| `router.py` | `classify()`, `get_repo_context()`, `get_route_hint()`, `extract_task_text()`, `is_subagent()`, `agent_key()` |
| `config.py` | Pydantic-settings, all config via env vars prefixed `CCR_` |

## How repo context reaches CCR (no headers needed)

Claude Code doesn't send repo headers automatically. Instead:

1. `UserPromptSubmit` hook fires before each turn (`inject_repo_context.py`)
2. Hook runs `git rev-parse --show-toplevel` from CWD → gets repo root
3. Hook detects active file via `git diff --name-only`
4. Hook classifies prompt intent → writes `route_hint` to state file
5. Hook writes `{repo_path, active_file, route_hint, prompt_stripped}` to `/tmp/cc-rlm-state.json`
6. CCR `get_repo_context()` + `get_route_hint()` read state file as fallback when headers absent

Explicit headers (`x-cc-repo-path`, `x-cc-active-file`, `x-cc-route-hint`) take priority — useful for tests/curl.

## Classification logic (router.py)

Priority order:

1. Non-chat path → PASSTHROUGH (health checks, embeddings)
2. Subagent request + `CCR_SUBAGENT_ROUTE` set → that route
3. Explicit `x-cc-route-hint` header → that route
4. `route_hint` in state file (set by hook) → that route
5. Has repo context → REPO_TASK
6. No repo context → FALLBACK

`classify(request, body)` needs the parsed body — `main.py` reads the body
*before* classifying, because the subagent check is a body signal.

## Subagent routing split

`CCR_SUBAGENT_ROUTE` keeps the main agent on frontier Claude while sending
high-volume subagent fan-out somewhere cheaper. Empty (default) = no split.

Detection is a heuristic: Claude Code exposes no agent id, but subagents cannot
spawn subagents, so a chat request advertising tools but no `Task`/`Agent` tool
is a subagent turn. A request with no tools at all is treated as the main agent
— the ambiguous case keeps the pre-existing behaviour.

This *must* be a body signal, not a state-file one. The `UserPromptSubmit` hook
fires only for real user prompts, so one `route_hint` is written per user turn
and every subagent in that turn would otherwise inherit the main agent's route.

Two consequences elsewhere:

- `extract_task_text()` ignores `prompt_stripped` for subagents. The hook's
  prompt belongs to the user's turn; applying it to a subagent would replace
  that subagent's own task and misdirect RLM's keyword scoring.
- `agent_key()` — hash of (system prompt + first user message), `""` for the
  main agent — is sent to RLM `/context` to partition session dedup per context
  window. Without it a subagent is denied files it never saw. System prompt
  alone would collide across parallel instances of the same agent type.

Known limitation: `active_file` still comes from the user's `git diff`, so a
subagent working on something unrelated gets seeded from the wrong file.

## Prompt-driven routing (inject_repo_context.py)

Users can prefix their message to override routing:

| Prefix | Route | Effect |
|---|---|---|
| `/claude ...` or `/anthropic ...` | FALLBACK | Full Claude via Anthropic API |
| `/local ...` or `/ollama ...` | PASSTHROUGH | Local model, skip RLM enrichment |
| `/repo ...` | REPO_TASK | Force enrichment even if heuristic says otherwise |
| (none, code signal) | REPO_TASK | Default when in a git repo |
| (none, knowledge Q) | FALLBACK | Auto-detected: "what is X", "explain Y" → Anthropic |

The prefix is stripped from the prompt before it reaches the model (`prompt_stripped` field).

## Model routing

`CCR_MODEL_OVERRIDE` rewrites the `model` field before forwarding. Required for Ollama (model name must match a pulled model). Leave empty to pass through unchanged for vLLM or Anthropic.

## Key behaviours

- RLM enrichment failure is non-fatal — logs warning, continues with unenriched prompt
- Streaming is always on for REPO_TASK (SSE passthrough)
- Anthropic fallback does NOT do message format translation yet (known gap)

## Adding new route logic

Edit `router.py:classify()`. Return one of the `Route` enum values.
`router.py` is kept pure — `get_repo_context()` and `get_route_hint()` reading the state file are the only exceptions.
