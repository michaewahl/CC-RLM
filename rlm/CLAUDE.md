# RLM Gateway — REPL Brain

Core of the system. Turns a raw task request into a dense, task-specific context pack.

## Responsibility

Single endpoint: `POST /context`
Input: `{task, active_file, repo_path, agent_key}`
Output: `{rendered: str, token_count: int, pack: dict}`

`agent_key` identifies the requesting context window (`""` = main agent).
See "Session dedup is per-agent" below.

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

## Session dedup is per-agent (session.py)

Session identity is `(repo_path, agent_key, hour-bucket)`, not just repo + hour.

A subagent starts with an empty context window — it has seen nothing the main
agent was shown. Keying dedup on the repo alone made `already_seen()` return
True for a subagent that never received the file, so the slice was silently
dropped and the pack rendered a false "*Already in context (unchanged)*" line.
The subagent then had to `Read` the file itself, costing more than the slice.

The `_tool_reads()` check is applied for the main agent only. The PostToolUse
hook records file reads with no agent attribution, so it cannot be trusted to
describe a subagent's context. Skipping it is the safe direction — worst case a
slice is injected that the agent already had.

`invalidate()` and `stats()` span every agent-scoped session for the repo, via
the `_session_repos` index (session ids are hashed and not reversible).

## Extending

- To add a new walker: see `rlm/walkers/CLAUDE.md`
- To change budget allocation: edit `assemble()` in `context_pack.py`
- To change rendered format: edit `ContextPack.render()` in `context_pack.py`
