# Skill: eval

Compare context pack quality vs. naive full-file injection for a given task.

## What this measures

- Token count: context pack vs. raw file dump
- Relevance: did the pack include the files the model actually needed?
- Answer quality: subjective, but log both responses for review

## Steps

1. **Pick a real task** — ideally one you just completed with Claude Code.

2. **Capture the RLM context pack** for that task:
   ```bash
   curl -X POST http://localhost:8081/context \
     -H "Content-Type: application/json" \
     -d '{"task":"<your task>","active_file":"<file>","repo_path":"<repo>"}' \
     | jq '{token_count, pack}'
   ```

3. **Capture the naive baseline** — count tokens in the raw active file + all its imports:
   ```bash
   poetry run python -c "
   import tiktoken, pathlib
   enc = tiktoken.get_encoding('cl100k_base')
   files = ['<active_file>', '<import1>', '<import2>']
   total = sum(len(enc.encode(pathlib.Path(f).read_text())) for f in files)
   print(f'Naive token count: {total}')
   "
   ```

4. **Compare and log**:
   - RLM pack token count vs. naive count
   - Which relevant files were included in the pack?
   - Which were missed?

5. **Tune if needed**:
   - Missing important files → adjust walker relevance scoring in `context_pack.assemble()`
   - Pack too large → tighten the file slice max_lines or symbol graph pruning
   - Diff dominating → reduce the 20% diff cap in `context_pack.py`
