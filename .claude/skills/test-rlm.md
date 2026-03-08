# Skill: test-rlm

Run the RLM Gateway locally and verify the full pipeline.

## Prerequisites

```bash
cd /Users/mikewahl/CC-RLM
poetry install
```

## Steps

1. **Start the RLM Gateway**:
   ```bash
   poetry run uvicorn rlm.main:app --port 8081 --reload
   ```

2. **Health check**:
   ```bash
   curl http://localhost:8081/health
   # expect: {"status":"ok"}
   ```

3. **Test context pack against its own codebase**:
   ```bash
   curl -s -X POST http://localhost:8081/context \
     -H "Content-Type: application/json" \
     -d '{
       "task": "add retry logic to the workspace mount",
       "active_file": "/Users/mikewahl/CC-RLM/rlm/workspace.py",
       "repo_path": "/Users/mikewahl/CC-RLM"
     }' | jq '{token_count: .token_count, pack: .pack, preview: .rendered[:500]}'
   ```

4. **Test each walker independently**:
   ```bash
   # Imports walker
   poetry run python -m rlm.walkers.imports \
     --repo /Users/mikewahl/CC-RLM \
     --file /Users/mikewahl/CC-RLM/rlm/main.py | jq .

   # Symbols walker
   poetry run python -m rlm.walkers.symbols \
     --repo /Users/mikewahl/CC-RLM \
     --file /Users/mikewahl/CC-RLM/rlm/context_pack.py | jq .

   # Diff walker
   poetry run python -m rlm.walkers.diff \
     --repo /Users/mikewahl/CC-RLM | jq .
   ```

5. **Verify**:
   - `token_count` is < 8000
   - `pack.slices` lists relevant files (not the whole repo)
   - `pack.has_diff` is false (no git repo yet) or true if initialized
   - No `"error"` keys in walker output

## If a walker fails

Check the RLM logs — failed walkers return `{"error": "...", "walker": "..."}` and are non-fatal.
The context pack assembles with whatever succeeded.
