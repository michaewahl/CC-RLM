# CC-RLM

A self-improving context engine that sits between Claude Code and a local LLM. Instead of dumping the whole repo into tokens, CC-RLM maintains a live structural model — import graph, symbol index, diff state, relevance history — and hands the model only what it needs.

**70–80%+ token reduction. 90% recall. < 200ms latency.**

## How it works

```
You type in Claude Code
    ↓
Hook detects git root, classifies prompt, writes state file
    ↓
CCR proxy routes to Ollama + RLM enrichment (or Anthropic for knowledge Qs)
    ↓
RLM Gateway:
  1. Load persistent index from SQLite
  2. Seed BFS from git diff changed files
  3. BM25 fallback if import graph is sparse
  4. Apply learned relevance multipliers
  5. AST-slice only relevant function/class bodies
  6. Skip files Claude already saw this turn
  7. Assemble context pack (< 8K tokens)
    ↓
Model gets exactly what it needs. Nothing else.
```

## Quick start (Docker)

```bash
docker compose up -d
export ANTHROPIC_BASE_URL=http://localhost:8080
claude
```

That's it. Ollama, RLM, and CCR all start together. The model (`qwen2.5-coder:7b`) is pulled automatically on first run.

## Quick start (local)

```bash
# Pull a local model
ollama pull qwen2.5-coder:7b

# Install deps
cd CC-RLM && poetry install

# Start services (bound to localhost only)
poetry run uvicorn rlm.main:app --port 8081 --host 127.0.0.1 &
poetry run uvicorn ccr.main:app --port 8080 --host 127.0.0.1 &

# Point Claude Code at CCR
export ANTHROPIC_BASE_URL=http://localhost:8080
claude
```

## Routing

| Prefix | Destination |
|---|---|
| `/claude` | Anthropic API (full Claude reasoning) |
| `/local` | Ollama direct, no context enrichment |
| `/repo` | Force RLM enrichment |
| (none) | Auto-detect: code tasks → Ollama+RLM, knowledge Qs → Anthropic |

## Architecture

```
Claude Code → CCR (proxy, port 8080) → RLM Gateway (port 8081) → Ollama (port 11434)
                                              ↓
                                     SQLite (~/.cc-rlm/store.db)
```

**CCR** handles routing, auth, and Anthropic fallback. **RLM** handles context — walkers, index, slicing, assembly. Either can be replaced independently.

## Live savings in the terminal

Every response starts with a real-time annotation showing exactly what CC-RLM saved:

```
[CC-RLM ▸ 2.3K tokens packed · 90% saved vs naive · 216ms]
```

No benchmarks needed — the proxy reports its own work on every turn.

## What makes it different

- **Structure over semantics** — import graphs and call chains, not embeddings
- **AST slicing** — function/class bodies, not full files or line ranges
- **Diff-first ranking** — BFS starts from files you actually changed
- **Self-improving** — tracks which files get cited in answers, biases future context toward them
- **Persistent** — SQLite index + relevance scores survive restarts, no cold start
- **Pre-warming** — watchdog re-indexes on file save before you ask

## Configuration

```env
# CCR
CCR_VLLM_URL=http://localhost:11434    # Ollama (or :8000 for vLLM)
CCR_MODEL_OVERRIDE=qwen2.5-coder:7b
CCR_FALLBACK_ENABLED=true
CCR_ANTHROPIC_FALLBACK_KEY=sk-ant-...

# RLM Gateway
RLM_TOKEN_BUDGET=8000
RLM_WALKER_TIMEOUT_MS=500
RLM_STORE_PATH=~/.cc-rlm/store.db
```

## Security

Both services bind to `127.0.0.1` by default — not reachable from the network. For shared/multi-user machines, restrict which repos RLM can read:

```env
RLM_ALLOWED_REPO_ROOTS=/Users/me/projects,/home/me/src
```

See [docs/adr/](docs/adr/) for architecture decisions.

## License

MIT
