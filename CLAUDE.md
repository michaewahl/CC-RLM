# CC-RLM

REPL-based context engine for Claude Code.
Stack: Claude Code → CCR (proxy) → RLM Gateway (REPL brain) → vLLM → MiniMax-M2.5.

## What this does

Instead of dumping the whole repo into tokens, the RLM layer:
1. Mounts the repo as a live workspace
2. Runs code walkers (AST import graph, symbol extractor, git diff)
3. Builds a context pack < 8K tokens
4. Hands it to the model as a precompiled header

## Layout

```
ccr/          proxy — route, auth, fallback         → ccr/CLAUDE.md
rlm/          REPL brain — walkers, context pack     → rlm/CLAUDE.md
rlm/walkers/  subprocess walker scripts              → rlm/walkers/CLAUDE.md
docs/adr/     why decisions were made
.claude/      skills, hooks
```

## Key constraints

- Walker timeout: 500ms each (configurable via RLM_WALKER_TIMEOUT_MS)
- Context pack hard cap: 8K tokens (configurable via RLM_TOKEN_BUDGET)
- Walkers are stateless subprocess scripts — they print JSON and exit
- CCR falls back to Anthropic API when no repo context is present

## Architecture decisions

- [ADR-001](docs/adr/001-repl-over-rag.md) — why REPL walkers, not vector search
- [ADR-002](docs/adr/002-subprocess-walkers.md) — why subprocess isolation
- [ADR-003](docs/adr/003-token-budget.md) — why 8K cap, not bigger

## Running locally (no Docker)

```bash
poetry install
poetry run uvicorn rlm.main:app --port 8081   # RLM
poetry run uvicorn ccr.main:app --port 8080   # CCR
```

Point Claude Code at CCR: `ANTHROPIC_BASE_URL=http://localhost:8080`
