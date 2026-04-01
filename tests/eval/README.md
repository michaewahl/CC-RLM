# Eval Harness

Measures RLM context pack quality vs. naive full-repo injection.

## Metrics

| Metric | What it measures |
|---|---|
| **Token reduction** | (1 - rlm_tokens / naive_tokens) × 100%. Higher = more efficient. |
| **Recall** | % of expected files that appeared in the context pack. Higher = more relevant. |
| **Latency** | Time for RLM to build the context pack (ms). |

## Running

```bash
# Start RLM first
poetry run uvicorn rlm.main:app --port 8081

# Run all cases
poetry run python tests/eval/run_eval.py

# Filter by category
poetry run python tests/eval/run_eval.py --category exploration
poetry run python tests/eval/run_eval.py --category refactor
poetry run python tests/eval/run_eval.py --category bugfix

# Single case
poetry run python tests/eval/run_eval.py --case classify-function

# Save JSON report
poetry run python tests/eval/run_eval.py --json
```

## Adding cases

Edit `cases.json`. Each case:

```json
{
  "id":             "short-kebab-id",
  "category":       "exploration | refactor | bugfix",
  "task":           "What the dev would type in Claude Code",
  "active_file":    "path/relative/to/repo/root.py",
  "expected_files": ["files/that/should/appear/in/pack.py"]
}
```

`expected_files` drives the recall metric — list files that any reasonable context pack
for this task should include.

## Interpreting results

- **Reduction 80–90%** — healthy; model gets a focused slice, not a firehose
- **Reduction < 50%** — check if active file is huge or import graph is wide
- **Recall < 100%** — a relevant file was left out; may indicate a relevance scoring gap
- **Latency > 1500ms** — a walker probably hit its timeout; check RLM logs

## What to do with gaps

If recall is low on a case, look at `detail_section` output to see which expected files
were missing. Common causes:
- File isn't imported by active file → won't appear in import walker results
- File was renamed → diff walker may not see it
- File is in a different language (JS/TS) → walkers are Python-only today

These gaps drive the backlog: relevance scoring improvements, call-graph distance
weighting, TS walker.
