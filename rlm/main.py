"""
RLM Gateway — the REPL brain.

Single endpoint: POST /context
Input:  {task, active_file, repo_path}
Output: {rendered: str, token_count: int, pack: dict}
"""

import asyncio
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from rlm.config import settings
from rlm.context_pack import assemble
from rlm.workspace import mount, run_walker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rlm")

app = FastAPI(title="RLM Gateway")


class ContextRequest(BaseModel):
    task: str
    active_file: str = ""
    repo_path: str


class ContextResponse(BaseModel):
    rendered: str
    token_count: int
    pack: dict


@app.on_event("startup")
async def startup():
    log.info("RLM Gateway started on port %d", settings.port)
    log.info("  token_budget=%d  walker_timeout=%dms", settings.token_budget, settings.walker_timeout_ms)


@app.post("/context", response_model=ContextResponse)
async def build_context(req: ContextRequest) -> ContextResponse:
    try:
        repo = mount(req.repo_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log.info("Building context pack for: %s", req.active_file or "(no active file)")

    # Run walkers concurrently
    walker_kwargs = {"file": req.active_file} if req.active_file else {}
    results = await asyncio.gather(
        run_walker("rlm.walkers.imports", repo, **walker_kwargs),
        run_walker("rlm.walkers.symbols", repo, **walker_kwargs),
        run_walker("rlm.walkers.diff", repo),
        return_exceptions=False,
    )

    walker_results = {
        "imports": results[0],
        "symbols": results[1],
        "diff":    results[2],
    }

    pack = assemble(
        task=req.task,
        active_file=req.active_file,
        repo_path=str(repo),
        walker_results=walker_results,
        token_budget=settings.token_budget,
    )

    rendered = pack.render()
    return ContextResponse(
        rendered=rendered,
        token_count=pack.token_count,
        pack={
            "slices": [{"file": s.file, "lines": s.lines} for s in pack.slices],
            "symbol_count": len(pack.symbol_graph),
            "has_diff": bool(pack.recent_diff),
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rlm.main:app", host="0.0.0.0", port=settings.port, reload=True)
