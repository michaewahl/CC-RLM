# RLM Gateway — REPL Brain

Core of the system. Turns a raw task request into a dense, task-specific context pack.

## Responsibility

Single endpoint: `POST /context`
Input: `{task, active_file, repo_path}`
Output: `{rendered: str, token_count: int, pack: dict}`

## Files

| File | Role |
|---|---|
| `main.py` | FastAPI app, `/context` endpoint, orchestrates the pipeline |
| `workspace.py` | Repo mount registry, `run_walker()` subprocess dispatcher |
| `context_pack.py` | `assemble()` — builds ContextPack from walker results, enforces token budget |
| `config.py` | Pydantic-settings, all config via env vars prefixed `RLM_` |

## Pipeline (main.py → /context)

```
mount(repo_path)                    # idempotent, resolves host path
↓
gather(imports, symbols, diff)      # 3 walkers run concurrently, 500ms timeout each
↓
assemble(walker_results, budget)    # context_pack.py: slice, score, fit to budget
↓
pack.render()                       # produces system preamble string
```

## Token budget strategy (context_pack.py)

Priority order (highest → lowest):
1. Header (task + active file) — always included
2. Git diff — capped at 20% of budget
3. Symbol graph — compact, high signal
4. File slices from import graph — fills remaining budget

## Extending

- To add a new walker: see `rlm/walkers/CLAUDE.md`
- To change budget allocation: edit `assemble()` in `context_pack.py`
- To change rendered format: edit `ContextPack.render()` in `context_pack.py`
