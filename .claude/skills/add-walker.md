# Skill: add-walker

Add a new REPL walker to the RLM Gateway.

## Steps

1. **Read the protocol** — `rlm/walkers/CLAUDE.md` defines the contract every walker must follow.

2. **Create the walker file** at `rlm/walkers/<name>.py`:
   - Implement `run(repo: str, ...) -> dict`
   - Add `if __name__ == "__main__":` block with argparse + `print(json.dumps(run(...)))`
   - No network calls, no disk writes, return `{"error": ..., "walker": ...}` on failure

3. **Wire it into the gateway** — edit `rlm/main.py`:
   - Add `run_walker("rlm.walkers.<name>", repo, ...)` to the `asyncio.gather()` call
   - Add its key to `walker_results`

4. **Handle its output** — edit `rlm/context_pack.py:assemble()`:
   - Pull data from `walker_results["<name>"]`
   - Slot it into the token budget (follow the priority order in `rlm/CLAUDE.md`)

5. **Test it directly**:
   ```bash
   poetry run python -m rlm.walkers.<name> --repo . --file rlm/main.py | jq .
   ```

6. **Test end-to-end**:
   ```bash
   curl -X POST http://localhost:8081/context \
     -H "Content-Type: application/json" \
     -d '{"task":"<describe task>","active_file":"rlm/main.py","repo_path":"."}'
   ```
   Verify the new walker's data appears in the rendered context pack.
