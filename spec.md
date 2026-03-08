# CC-RLM — Recursive Language Model Gateway

## What This Is

A programmable context engine that sits between Claude Code and a locally-served model. Instead of re-reading files on every query or burning a 1M-token context window, the RLM layer loads the repo as a live REPL workspace, writes code to walk and slice it, and hands the model a minimal, task-specific context pack — like a precompiled header for each request.

**Stack:** Claude Code → CCR → RLM Gateway → vLLM → MiniMax-M2.5

---

## The Problem It Solves

| Approach | Problem |
|---|---|
| Re-read files each turn | Slow, wastes tokens, breaks at scale |
| 1M-token context dump | Expensive, incoherent, no structural awareness |
| Vector / semantic search | Blunt instrument — semantic similarity ≠ structural relevance |
| **RLM (this)** | Run code to understand code. Build only what this task needs. |

The key insight: a developer doesn't re-read the whole codebase before each keystroke. They know the shape of the code. They navigate by structure. The RLM layer does the same — it *programs its way* to the relevant slice.

---

## Architecture

```
┌─────────────────────┐
│     Claude Code     │  developer interface
└──────────┬──────────┘
           │  OpenAI-compat API calls
           ▼
┌─────────────────────┐
│        CCR          │  Claude Code Router
│  proxy + interceptor│  rewrites model target
│  injects repo path  │  falls back to Anthropic API
└──────────┬──────────┘
           │  {task, active_file, repo_path}
           ▼
┌──────────────────────────────────────────┐
│             RLM Gateway                  │  the REPL brain
│                                          │
│  1. mount repo as sandboxed workspace    │
│  2. spin up REPL worker                  │
│  3. run walkers (import, symbol, diff)   │
│  4. assemble context pack (< 8K tokens)  │
│  5. build final prompt                   │
└──────────┬───────────────────────────────┘
           │  system = context pack + task
           ▼
┌─────────────────────┐
│        vLLM         │  inference server (OpenAI-compat)
│    MiniMax-M2.5     │  local or remote
└─────────────────────┘
```

---

## The RLM Layer — How It Actually Works

### Workspace
The repo is mounted as a read-only filesystem namespace — no copy, no token burn. The REPL worker gets the repo root on its `sys.path` and `$PATH`. It can import modules, call functions, and inspect the live object graph.

### REPL Workers
Small Python programs that answer structural questions about the codebase:

| Walker | Question it answers |
|---|---|
| `imports.py` | What does this file import? What imports this file? |
| `symbols.py` | Where is this function/class/type defined? What does it call? |
| `diff.py` | What changed since last commit? Since this branch diverged? |
| `types.py` | What is the type signature here? What implements this interface? |

Workers run in a subprocess pool. Each completes in < 500ms. Results are structured JSON.

### Context Pack
A structured object built from walker results:

```json
{
  "task": "add retry logic to the HTTP client",
  "active_file": "src/http/client.py",
  "relevant_slices": [
    {"file": "src/http/client.py", "lines": "45-82", "content": "..."},
    {"file": "src/http/retry.py", "lines": "1-30", "content": "..."}
  ],
  "symbol_graph": {
    "HttpClient.request": ["retry_with_backoff", "parse_response"],
    "retry_with_backoff": ["time.sleep", "random.uniform"]
  },
  "recent_diff": "--- a/src/http/client.py\n+++ b/src/http/client.py\n...",
  "token_count": 3840
}
```

**Target: < 8K tokens regardless of repo size.**

This pack is handed to vLLM as the system prompt preamble. The model sees exactly what it needs. Nothing else.

### The Precompiled Header Pattern
Just as a C compiler builds `.h` artifacts that let `.c` files compile without re-parsing every dependency, the context pack is a precompiled view of the repo for this specific task. Build it once per request. Throw it away after. No persistent state needed.

---

## Component Breakdown

| Component | Role | Port | Tech |
|---|---|---|---|
| **CCR** | Proxy, route rewriting, auth, fallback | 8080 | Python / FastAPI |
| **RLM Gateway** | REPL orchestration, context assembly | 8081 | Python / FastAPI |
| **REPL workers** | Walkers, symbol extractors | subprocess pool | Python |
| **vLLM** | Model inference server | 8000 | vLLM |
| **MiniMax-M2.5** | The model | — | HuggingFace via vLLM |

---

## Data Flow (end to end)

```
1. Dev types in Claude Code
2. CCR intercepts the API call
3. CCR extracts: task message, active file hint, repo path from headers
4. CCR calls RLM Gateway: POST /context  {task, active_file, repo_path}
5. RLM mounts workspace (idempotent — already mounted if same repo)
6. RLM dispatches walker jobs to subprocess pool
7. Workers return structured JSON results
8. context_pack.py assembles pack, enforces 8K token budget
9. prompt.py builds: system = pack.render() + "\n\n" + task
10. RLM calls vLLM: POST /v1/chat/completions  (streaming)
11. vLLM streams tokens back through RLM → CCR → Claude Code
```

---

## What This Is / Is Not

**Is:**
- A programmable context engine — runs code to understand code
- A thin, stateless proxy layer (workspaces are cheap mounts, not sessions)
- Local-first — runs on dev machine or on-prem GPU node
- Model-agnostic — point CCR at any vLLM-served model

**Is not:**
- A RAG pipeline (no vector DB, no embedding index)
- An agent framework (no tool-calling loop, no multi-step planning)
- A 1M-token context manager
- A replacement for Claude — CCR falls back to Anthropic API for tasks outside the repo scope

---

## Build Phases

### Phase 0 — Skeleton (day 1)
CCR proxy running. Routes all traffic to vLLM passthrough. No RLM yet. End-to-end call confirmed.

### Phase 1 — RLM Stub (day 2)
RLM Gateway receives `{task, active_file, repo_path}`. Returns a hardcoded context pack. Confirms end-to-end: CCR → RLM → vLLM → Claude Code.

### Phase 2 — REPL Workers (week 1)
Live walkers: file walker, import tracer, symbol extractor. Context pack built from real repo data.

### Phase 3 — Context Pack Optimizer (week 2)
Token budget enforcement. Relevance scoring (structural proximity, recency, call-graph distance). Pack never exceeds 8K tokens.

### Phase 4 — Git-Aware (week 2)
Diff walker. Blame context. Recent change awareness — if a file changed 10 minutes ago, it's probably relevant.

### Phase 5 — Eval Harness (week 3)
A/B test: same task with vs. without RLM context pack. Measure: answer quality, token cost, latency. Tune walker weights.

---

## Key Design Decisions

**Why subprocess workers, not in-process?**
Isolation. A buggy walker crashes the worker, not the gateway. Workers can be restarted without downtime.

**Why < 8K token budget?**
MiniMax-M2.5 performs best with a sharp, dense context. A 100K dump with 95% irrelevant content is worse than 8K of exactly the right thing. Budget is configurable.

**Why not persist the context pack?**
Repo state changes constantly. Stale packs are worse than fresh ones. Stateless = correct by construction. Mount caching handles the repeated-read cost.

**Why CCR as a separate service from RLM?**
Separation of concerns. CCR handles auth, routing, fallback logic, and Claude Code compatibility. RLM handles only context. Either can be replaced independently.

**Why vLLM + MiniMax-M2.5?**
MiniMax-M2.5 is a strong code-capable model with efficient inference. vLLM provides OpenAI-compatible streaming API out of the box. Swap freely.

---

## Configuration

```env
# CCR
CCR_PORT=8080
CCR_RLM_URL=http://localhost:8081
CCR_VLLM_URL=http://localhost:8000
CCR_ANTHROPIC_FALLBACK_KEY=sk-ant-...  # used when task has no repo context
CCR_FALLBACK_ENABLED=true

# RLM Gateway
RLM_PORT=8081
RLM_TOKEN_BUDGET=8000
RLM_WORKER_POOL_SIZE=4
RLM_WALKER_TIMEOUT_MS=500

# vLLM
VLLM_MODEL=MiniMaxAI/MiniMax-M2.5
VLLM_PORT=8000
VLLM_GPU_MEMORY_UTILIZATION=0.9
VLLM_MAX_MODEL_LEN=32768
```

---

## File Structure

```
CC-RLM/
├── spec.md                     ← this file
├── docker-compose.yml          ← wires all services
├── pyproject.toml              ← shared deps
├── .env.example
│
├── ccr/                        ← Claude Code Router (proxy)
│   ├── main.py                 ← FastAPI app, lifespan
│   ├── router.py               ← request interception, route decision
│   └── config.py               ← settings (pydantic-settings)
│
└── rlm/                        ← RLM Gateway (REPL brain)
    ├── main.py                 ← FastAPI app, /context endpoint
    ├── workspace.py            ← repo mount, REPL pool management
    ├── context_pack.py         ← assemble pack, enforce token budget
    ├── prompt.py               ← build final prompt from pack
    └── walkers/
        ├── __init__.py
        ├── imports.py          ← import graph walker
        ├── symbols.py          ← symbol/call graph walker
        └── diff.py             ← git diff walker
```
