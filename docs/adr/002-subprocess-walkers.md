# ADR-002: Walkers as Subprocess Scripts

**Status:** Accepted
**Date:** 2026-03-07

## Context

The walkers need to execute code against the live repo — importing modules, calling AST parsers, running git commands. They could run:

1. **In-process** — imported directly into the RLM gateway
2. **As subprocesses** — spawned per request, communicate via stdout JSON

## Decision

Run walkers as subprocesses. Each walker is an independently executable Python script invoked via `python -m rlm.walkers.foo`.

## Reasons

**Fault isolation.**
A walker that crashes (malformed Python file, git repo corruption, infinite loop in user code) kills the subprocess, not the gateway. The gateway catches the failure, logs it, and continues without that walker's data. In-process, the same crash would bring down the RLM service.

**Timeout enforcement is clean.**
`asyncio.wait_for(proc.communicate(), timeout=0.5)` gives hard deadline enforcement. In-process, enforcing timeouts on arbitrary code requires threads or signal gymnastics.

**Walkers can import user code safely.**
If a user's repo has a module that takes 10 seconds to import, or that calls `sys.exit()` on import, the subprocess dies. The gateway is unaffected.

**Easy to test independently.**
`python -m rlm.walkers.imports --repo . --file rlm/main.py | jq .` — each walker is testable from the command line with zero framework setup.

## Trade-offs

- Subprocess spawn overhead: ~20-50ms per walker. Acceptable given 500ms budget.
- No shared memory with the gateway. Walkers must serialize all results to JSON. Fine for our data shapes.
- Cannot use gateway's cached state (e.g., mounted workspaces). Walkers must be fully self-contained.

## Future

Consider a persistent worker pool (e.g., using `multiprocessing.Pool`) if spawn overhead becomes measurable. Current design makes this a drop-in optimization.
