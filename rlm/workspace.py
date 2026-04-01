"""
Workspace manager — maps repo paths to sandboxed REPL environments.

Design:
- Each unique repo_path gets one workspace entry (idempotent).
- Workers run as subprocess pool entries; we exec walker scripts in them.
- Read-only bind mount in Docker means no risk of walkers mutating the repo.
- Inside the container, host paths appear under /host (configurable).
- Walker results are cached by file mtime to avoid redundant subprocess calls.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from rlm.config import settings
from rlm import cache as walker_cache

log = logging.getLogger("rlm.workspace")

# Active workspaces: repo_path → resolved host path
_workspaces: dict[str, Path] = {}

# CRIT-1: hard allowlist of permitted walker modules — prevents arbitrary python -m execution
_ALLOWED_WALKERS: frozenset[str] = frozenset({
    "rlm.walkers.imports",
    "rlm.walkers.symbols",
    "rlm.walkers.diff",
    "rlm.walkers.ts_imports",
})


def _check_allowed_root(resolved: Path) -> None:
    """Raise ValueError if resolved path is outside RLM_ALLOWED_REPO_ROOTS (when configured)."""
    roots = settings.allowed_repo_roots
    if not roots:
        return  # unconstrained — single-user local install
    for root in roots:
        try:
            resolved.relative_to(root)
            return  # within an allowed root
        except ValueError:
            continue
    raise ValueError(
        f"repo_path {resolved} is outside RLM_ALLOWED_REPO_ROOTS: {roots}"
    )


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
        resolved = candidate.resolve()
        _check_allowed_root(resolved)
        return resolved

    # Fallback: running locally, path is directly accessible
    if p.exists():
        resolved = p.resolve()
        _check_allowed_root(resolved)
        return resolved

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

    Results are cached by (walker_module, repo_path, file) + file mtime.
    A cache hit avoids the subprocess entirely — returns in microseconds.

    walker_module: dotted module path, e.g. "rlm.walkers.imports"
    kwargs: passed as --key=value CLI args to the walker
    """
    # CRIT-1: reject any module not on the hard allowlist
    if walker_module not in _ALLOWED_WALKERS:
        log.error("Blocked disallowed walker module: %s", walker_module)
        return {"error": f"walker not allowed: {walker_module}"}

    timeout = settings.walker_timeout_ms / 1000.0
    file_arg = str(kwargs.get("file", ""))

    # Cache lookup (only when a specific file is targeted)
    if settings.cache_enabled and file_arg:
        cached = walker_cache.get(walker_module, str(repo_path), file_arg)
        if cached is not None:
            log.debug("Cache hit: %s %s", walker_module, Path(file_arg).name)
            return cached

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

    try:
        result = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        log.warning("Walker %s returned invalid JSON: %s", walker_module, exc)
        return {"error": "invalid json", "raw": stdout.decode()[:500]}

    # Store in cache
    if settings.cache_enabled and file_arg and "error" not in result:
        walker_cache.set(walker_module, str(repo_path), file_arg, result)

    return result
