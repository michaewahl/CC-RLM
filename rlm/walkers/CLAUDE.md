# RLM Walkers

Stateless subprocess scripts. Each walker answers one structural question about the repo.

## Protocol

Every walker must:
1. Accept `--repo <path>` and optionally `--file <path>` as CLI args
2. Print a single JSON object to stdout
3. Exit 0 on success, non-zero on failure
4. Complete within 500ms (enforced by `workspace.run_walker()`)

## Walkers

| Walker | Question | Key output fields |
|---|---|---|
| `imports.py` | What does this file import? What imports it? | `imports`, `imported_by`, `resolved` |
| `symbols.py` | What functions/classes are defined here? What do they call? | `symbols: {name: {file, line, calls}}` |
| `diff.py` | What changed recently? | `diff`, `changed_files`, `branch` |

## Adding a walker

1. Create `rlm/walkers/yourwalker.py`
2. Implement `run(repo: str, ...) -> dict`
3. Add `if __name__ == "__main__":` block with argparse + `print(json.dumps(run(...)))`
4. Call it in `rlm/main.py` via `run_walker("rlm.walkers.yourwalker", repo, ...)`
5. Handle its output key in `context_pack.assemble()`

## Rules

- No network calls
- No writes to disk
- No imports outside stdlib + gitpython (already a dep)
- If something fails, return `{"error": "...", "walker": "module.name"}` — never raise
