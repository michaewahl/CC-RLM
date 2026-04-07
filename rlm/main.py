"""
RLM Gateway — the REPL brain.

Single endpoint: POST /context
Input:  {task, active_file, repo_path}
Output: {rendered: str, token_count: int, pack: dict}

Also exposes:
  POST /feedback  — answer-driven relevance scoring (called by CCR after each response)
  GET  /health    — health + cache + relevance stats
  DELETE /cache   — clear walker cache
  DELETE /session — reset session dedup state
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from rlm.config import settings
from rlm.context_pack import assemble
from rlm import bm25 as bm25_module
from rlm import cache as walker_cache
from rlm import relevance_store
from rlm import repo_index
from rlm import store as sqlite_store
from rlm import watcher
from rlm.workspace import mount, run_walker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rlm")

_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs"}


async def _session_gc_loop() -> None:
    """HIGH-4: periodically evict expired sessions so _sessions dict doesn't grow unboundedly."""
    from rlm import session as session_tracker
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        session_tracker._gc()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open persistent store first — RepoIndex + relevance_store both need it
    sqlite_store.open_db(Path(settings.store_path))
    log.info("RLM Gateway started on port %d", settings.port)
    log.info("  token_budget=%d  walker_timeout=%dms", settings.token_budget, settings.walker_timeout_ms)
    log.info("  cache=%s  session_dedup=%s  repo_index=%s",
             settings.cache_enabled, settings.session_dedup_enabled, settings.repo_index_enabled)
    log.info("  store=%s", sqlite_store.db_stats())
    gc_task = asyncio.create_task(_session_gc_loop())
    yield
    gc_task.cancel()
    watcher.stop_all()
    sqlite_store.close_db()
    log.info("Walker cache stats on exit: %s", walker_cache.stats())


app = FastAPI(title="RLM Gateway", lifespan=lifespan)


class ContextRequest(BaseModel):
    task: str = Field(max_length=4_000)
    active_file: str = ""
    repo_path: str = Field(max_length=1_000)


class ContextResponse(BaseModel):
    rendered: str
    token_count: int
    pack: dict


class FeedbackRequest(BaseModel):
    repo_path: str = Field(max_length=1_000)
    files_in_pack: list[str] = Field(default=[], max_length=200)
    response_text: str = Field(max_length=50_000)


@app.post("/context", response_model=ContextResponse)
async def build_context(req: ContextRequest) -> ContextResponse:
    try:
        repo = mount(req.repo_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    active_file = req.active_file
    # CRIT-2: ensure active_file is within the resolved repo root (path traversal guard)
    # Use lstat() to detect symlinks before resolve() — a symlink inside the repo
    # pointing outside would otherwise pass the relative_to() check.
    if active_file:
        af_path = Path(active_file)
        try:
            if af_path.exists() and af_path.lstat().st_mode & 0o170000 == 0o120000:
                raise HTTPException(
                    status_code=400,
                    detail=f"active_file must not be a symlink: {active_file}",
                )
            af_path.resolve().relative_to(repo)
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"active_file must be within repo_path: {active_file}",
            )
    log.info("Building context pack for: %s", Path(active_file).name if active_file else "(no active file)")

    # ------------------------------------------------------------------
    # Walkers: run concurrently (imports + symbols + diff)
    # Diff runs first conceptually — its output drives relevance seeding
    # ------------------------------------------------------------------
    walker_kwargs = {"file": active_file} if active_file else {}
    suffix = Path(active_file).suffix if active_file else ""
    import_walker = "rlm.walkers.ts_imports" if suffix in _TS_EXTENSIONS else "rlm.walkers.imports"

    results = await asyncio.gather(
        run_walker(import_walker, repo, **walker_kwargs),
        run_walker("rlm.walkers.symbols", repo, **walker_kwargs),
        run_walker("rlm.walkers.diff", repo),
        return_exceptions=False,
    )

    walker_results = {
        "imports": results[0],
        "symbols": results[1],
        "diff":    results[2],
    }

    # ------------------------------------------------------------------
    # Diff-first relevance: changed files seed the BFS at score 2.0
    # ------------------------------------------------------------------
    pre_ranked: list[tuple[str, float]] | None = None
    if settings.repo_index_enabled and active_file:
        idx = repo_index.get_or_create(str(repo))

        if not idx.mtimes:
            log.info("RepoIndex: first request, doing initial scan...")
            await asyncio.get_event_loop().run_in_executor(None, idx.refresh_repo)
            # Start file watcher now that we have a full index
            watcher.start_watching(str(repo))
            # Build BM25 index from fresh symbol data
            if settings.bm25_enabled:
                bm25_idx = bm25_module.get_or_create(str(repo))
                await asyncio.get_event_loop().run_in_executor(None, bm25_idx.build, idx)
        else:
            await asyncio.get_event_loop().run_in_executor(
                None, idx.refresh_neighborhood, active_file
            )
            # Ensure watcher is running (idempotent)
            watcher.start_watching(str(repo))

        # Resolve diff changed_files to absolute paths (skip test/eval files)
        _SKIP_PREFIXES = ("tests/", "test/", "docs/", ".claude/")
        diff_data = walker_results.get("diff", {})
        changed_rel = diff_data.get("changed_files", []) if isinstance(diff_data, dict) else []
        changed_abs = [
            str(repo / f) for f in changed_rel
            if (repo / f).exists() and not any(f.startswith(p) for p in _SKIP_PREFIXES)
        ]

        if changed_abs:
            log.info("Diff-first: seeding from %d changed file(s): %s",
                     len(changed_abs), [Path(f).name for f in changed_abs])
            pre_ranked = idx.get_relevant_from_diff(changed_abs, active_file, n=10, task=req.task)
        else:
            pre_ranked = idx.get_relevant(active_file, n=10, task=req.task)

        # BM25 fallback when import graph is sparse
        if settings.bm25_enabled:
            bm25_idx = bm25_module.get_or_create(str(repo))
            if bm25_idx._built:
                pre_ranked = bm25_idx.query_if_sparse(pre_ranked or [], req.task, n=10)

        # Apply learned relevance multipliers
        if pre_ranked:
            pre_ranked = [
                (f, score * relevance_store.get_multiplier(str(repo), f))
                for f, score in pre_ranked
            ]
            pre_ranked.sort(key=lambda x: -x[1])

    # ------------------------------------------------------------------
    # Assemble context pack
    # ------------------------------------------------------------------
    pack = assemble(
        task=req.task,
        active_file=active_file,
        repo_path=str(repo),
        walker_results=walker_results,
        token_budget=settings.token_budget,
        relevant_files=pre_ranked,
    )

    rendered = pack.render()
    return ContextResponse(
        rendered=rendered,
        token_count=pack.token_count,
        pack={
            "slices": [{"file": s.file, "lines": s.lines} for s in pack.slices],
            "files_in_pack": [s.file for s in pack.slices],
            "symbol_count": len(pack.symbol_graph),
            "has_diff": bool(pack.recent_diff),
            "deduped_files": pack.deduped_files,
            "cache_stats": walker_cache.stats() if settings.cache_enabled else {},
        },
    )


@app.post("/feedback")
async def record_feedback(req: FeedbackRequest):
    """
    Answer-driven relevance update.

    Called by CCR after each streamed REPO_TASK response.
    Parses the response text for cited symbol/file names and updates the
    relevance store so future context packs are better weighted.
    """
    await asyncio.get_event_loop().run_in_executor(
        None,
        relevance_store.record,
        req.repo_path,
        req.files_in_pack,
        req.response_text,
    )
    return {"ok": True}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "cache": walker_cache.stats(),
        "indexes": list(repo_index._indexes.keys()),
        "store": sqlite_store.db_stats(),
        "watchers": list(watcher._observers.keys()),
    }


@app.delete("/cache")
async def clear_cache(repo_path: str | None = None):
    cleared = walker_cache.clear(repo_path)
    return {"cleared": cleared}


@app.delete("/session")
async def reset_session(repo_path: str | None = None):
    from rlm import session as session_tracker
    if repo_path:
        session_tracker.invalidate(repo_path)
    else:
        session_tracker.clear_all()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rlm.main:app", host="127.0.0.1", port=settings.port, reload=True)
