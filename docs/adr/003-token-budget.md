# ADR-003: 8K Token Context Pack Budget

**Status:** Accepted
**Date:** 2026-03-07

## Context

The context pack is injected as a system preamble before every model call. The question is how large to make it.

Options considered:
1. **No limit** — pass everything the walkers return
2. **Match model context window** — e.g., 32K for MiniMax-M2.5
3. **Fixed small budget** — 8K tokens, enforced in `context_pack.assemble()`

## Decision

Fixed 8K token budget, configurable via `RLM_TOKEN_BUDGET`.

## Reasons

**More context is not always better.**
Models exhibit "lost in the middle" behaviour — attention degrades for content far from the prompt boundaries. A 32K context pack with 28K of marginally relevant code performs *worse* than 8K of exactly the right things.

**Forces the walker/assembler to be smart.**
An unlimited budget lets sloppy relevance scoring slide. A hard cap requires the assembler to rank and trim aggressively, which produces better packs and incentivises better walkers.

**Cost and latency.**
Even with a local vLLM serving MiniMax-M2.5, prefill time scales with context length. 8K adds ~50ms of prefill. 32K adds ~200ms. The user is already waiting for walker execution; we should not add avoidable model latency on top.

**The precompiled header analogy holds.**
A C header file doesn't include the entire standard library — it includes exactly what this translation unit needs. 8K is enough for: the active file's import deps, its call graph, and the recent diff.

## Budget allocation (context_pack.py)

| Slot | Share | Reasoning |
|---|---|---|
| Header (task + active file) | Fixed ~100 tokens | Always present |
| Git diff | ≤ 20% (≤ 1600 tokens) | High signal, recent changes |
| Symbol graph | Variable, compact | Structural skeleton |
| File slices | Remaining budget | Filled by import relevance rank |

## Configurability

`RLM_TOKEN_BUDGET` can be raised to 16K or 32K for experimentation. The eval harness (Phase 5) will measure quality vs. cost at different budget levels to validate or revise the 8K default.
