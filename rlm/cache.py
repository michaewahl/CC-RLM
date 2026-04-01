"""
Walker result cache — mtime-invalidated in-memory store.

Eliminates redundant subprocess walker invocations for files that haven't changed.
Cache is per-process (in-memory); clears on server restart.

Key insight: the import graph of a file only changes when the file itself changes.
We check mtime before returning a cached result and re-run the walker if stale.
"""

import logging
import time
from pathlib import Path

log = logging.getLogger("rlm.cache")

# HIGH-4: cap cache size to prevent unbounded memory growth
_MAX_CACHE_SIZE = 1_000

# {cache_key: {"data": dict, "mtime": float, "ts": float}}
_cache: dict[str, dict] = {}

_hits = 0
_misses = 0


def _key(walker_module: str, repo_path: str, file_path: str) -> str:
    return f"{walker_module}::{repo_path}::{file_path}"


def get(walker_module: str, repo_path: str, file_path: str) -> dict | None:
    """
    Return cached walker result if the file hasn't changed since caching.
    Returns None on miss or stale entry.
    """
    global _hits, _misses
    k = _key(walker_module, repo_path, file_path)
    entry = _cache.get(k)
    if not entry:
        _misses += 1
        return None

    # Validate mtime
    try:
        current_mtime = Path(file_path).stat().st_mtime
    except OSError:
        _misses += 1
        return None

    if current_mtime != entry["mtime"]:
        _misses += 1
        log.debug("Cache stale: %s (mtime changed)", file_path)
        del _cache[k]
        return None

    _hits += 1
    return entry["data"]


def set(walker_module: str, repo_path: str, file_path: str, data: dict) -> None:
    """Store walker result with current file mtime."""
    try:
        mtime = Path(file_path).stat().st_mtime
    except OSError:
        return  # Can't determine mtime — don't cache

    k = _key(walker_module, repo_path, file_path)
    # HIGH-4: evict oldest entry when at capacity (insertion-order FIFO)
    if len(_cache) >= _MAX_CACHE_SIZE and k not in _cache:
        oldest = next(iter(_cache))
        del _cache[oldest]
    _cache[k] = {"data": data, "mtime": mtime, "ts": time.monotonic()}


def stats() -> dict:
    total = _hits + _misses
    return {
        "size": len(_cache),
        "hits": _hits,
        "misses": _misses,
        "hit_rate": _hits / total if total else 0.0,
    }


def clear(repo_path: str | None = None) -> int:
    """Clear all cache entries, or just entries for a specific repo."""
    global _cache
    before = len(_cache)
    if repo_path is None:
        _cache = {}
    else:
        _cache = {k: v for k, v in _cache.items() if f"::{repo_path}::" not in k}
    return before - len(_cache)
