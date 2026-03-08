# ADR-001: REPL Walkers Instead of Vector Search

**Status:** Accepted
**Date:** 2026-03-07

## Context

To give a model useful codebase context, we need to select the right ~8K tokens from a repo that may contain millions. The two obvious approaches are:

1. **Vector/semantic search** — embed all files, retrieve by cosine similarity to the task
2. **Structural walkers** — run code that navigates the repo graph (imports, calls, diffs)

## Decision

Use structural walkers. Do not build a vector index.

## Reasons

**Semantic similarity is the wrong signal for code.**
A task like "add retry logic to the HTTP client" retrieves files that *talk about* retry logic semantically — but what the model actually needs is the HTTP client's import dependencies, its call graph, and what changed recently. These are structural facts, not semantic ones.

**Walkers return ground truth.**
An AST import graph is always correct. A vector search over embeddings of last week's files is stale the moment you rename a function. Walkers run against live state.

**No index to maintain.**
Vector search requires embedding every file, storing vectors, keeping the index current as files change. Walkers have no state — they run, answer, exit. Zero maintenance.

**Speed is competitive.**
A targeted AST walk of a 50K-line repo completes in < 200ms. Embedding retrieval with a warm index is comparable, but walkers win at cold start and on large repos.

## Trade-offs

- Walkers are language-specific. The current implementation handles Python via `ast`. TypeScript/Go require separate walkers.
- Semantic search handles natural-language similarity better (e.g., "something to do with authentication" maps to files that never import each other). We accept this gap for now — most coding tasks are structurally anchored.

## Future

Add a TypeScript walker using the `ts-morph` AST library. Add a fallback heuristic walker (grep-based) for unsupported languages.
