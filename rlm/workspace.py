"""
Workspace manager — maps repo paths to sandboxed REPL environments.

Design:
- Each unique repo_path gets one workspace entry (idempotent).
- Workers run as subprocess pool entries; we exec walker scripts in them.
- Read-only bind mount in Docker means no risk of walkers mutating the repo.
- Inside the container, host paths appear under /host (configurable).
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from rlm.config import settings

log = logging.getLogger("rlm.workspace")

# Active workspaces: repo_path → resolved host path
_workspaces: dict[str, Path] = {}


def resolve_repo_path(repo_path: str) -> Path:
    """
    Translate a host-side repo path to the path visible inside this process.
    In Docker, host / is mounted at /host, so /Users/foo/myrepo → /host/Users/foo/myrepo.
    When running locally (not in Docker), the path is used as-is.
    """
    p = Path(repo_path)
    if not p.is_absolute():
        raise ValueError(f"repo_path must be absolute: {repo_path}")

    host_prefix = Path(settings.host_prefix)
    candidate = host_prefix / p.relative_to("/")
    if candidate.exists():
        return candidate

    # Fallback: running locally, path is directly accessible
    if p.exists():
        return p

    raise FileNotFoundError(f"Repo not found at {repo_path} (tried {candidate})")


def mount(repo_path: str) -> Path:
    """Ensure workspace is registered. Returns the resolved path."""
    if repo_path not in _workspaces:
        resolved = resolve_repo_path(repo_path)
        _workspaces[repo_path] = resolved
        log.info("Workspace mounted: %s → %s", repo_path, resolved)
    return _workspaces[repo_path]


async def run_walker(
    walker_module: str,
    repo_path: Path,
    **kwargs,
) -> dict:
    """
    Execute a walker as a subprocess and return its JSON output.

    walker_module: dotted module path, e.g. "rlm.walkers.imports"
    kwargs: passed as --key=value CLI args to the walker
    """
    timeout = settings.walker_timeout_ms / 1000.0

    cmd = [
        sys.executable, "-m", walker_module,
        "--repo", str(repo_path),
    ]
    for k, v in kwargs.items():
        cmd += [f"--{k}", str(v)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("Walker %s timed out after %.1fs", walker_module, timeout)
        return {"error": "timeout", "walker": walker_module}
    except Exception as exc:
        log.warning("Walker %s failed: %s", walker_module, exc)
        return {"error": str(exc), "walker": walker_module}

    if proc.returncode != 0:
        log.warning("Walker %s exited %d: %s", walker_module, proc.returncode, stderr.decode())
        return {"error": stderr.decode(), "walker": walker_module}

    import json
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        log.warning("Walker %s returned invalid JSON: %s", walker_module, exc)
        return {"error": "invalid json", "raw": stdout.decode()[:500]}
