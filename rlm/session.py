"""
Session deduplication tracker.

Prevents re-injecting file content that Claude has already seen this session
and that hasn't changed since. On multi-turn conversations, this is the biggest
lever for token reduction — turns 2-N cost near-zero for unchanged context.

Session identity: (repo_path, date-hour bucket) — rough enough to group a
work session together without bleeding across different tasks or days.

Design:
- In-memory only (per-process). Clears on server restart or session expiry.
- Tracks {file_path: last_mtime_seen} per session.
- If a file's current mtime == last mtime seen, skip it.
- Files with changes since last injection are always re-included.
"""

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("rlm.session")

# {session_id: {file_path: {"mtime": float, "ts": float}}}
_sessions: dict[str, dict[str, dict]] = {}

# {session_id: repo_path} — session ids are hashed and not reversible, so this
# index is what lets invalidate()/stats() reach every agent-scoped session
# belonging to one repo.
_session_repos: dict[str, str] = {}

# Written by .claude/hooks/track_tool_reads.py, cleared by inject_repo_context.py.
# HIGH-3: use user-owned directory, not world-writable /tmp (prevents symlink attacks)
_TOOL_READS_FILE = Path.home() / ".cc-rlm" / "tool-reads.json"


def _tool_reads() -> set[str]:
    """Files Claude already has in context window from tool calls this turn."""
    try:
        return set(json.loads(_TOOL_READS_FILE.read_text()))
    except Exception:
        return set()

SESSION_TTL_SECONDS = 3600  # 1 hour of inactivity clears session


def _session_id(repo_path: str, agent_key: str = "") -> str:
    """
    Stable session ID: repo_path + agent + current hour bucket.

    agent_key partitions dedup state per context window. The main agent uses ""
    (preserving prior behaviour); each subagent gets its own key, because a
    subagent starts with an empty context window and has seen none of what the
    main agent was shown.
    """
    bucket = datetime.now().strftime("%Y-%m-%d-%H")
    raw = f"{repo_path}::{agent_key}::{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _register(sid: str, repo_path: str) -> None:
    _session_repos[sid] = repo_path


def _sids_for_repo(repo_path: str) -> list[str]:
    return [
        sid for sid, repo in _session_repos.items()
        if repo == repo_path and sid in _sessions
    ]


def _gc() -> None:
    """Remove expired sessions."""
    now = time.monotonic()
    expired = [
        sid for sid, files in _sessions.items()
        if files and all(now - v["ts"] > SESSION_TTL_SECONDS for v in files.values())
    ]
    for sid in expired:
        del _sessions[sid]
        _session_repos.pop(sid, None)
        log.debug("Session expired: %s", sid)


def already_seen(repo_path: str, file_path: str, agent_key: str = "") -> bool:
    """
    Return True if this file is already in Claude's context and hasn't changed.

    Two sources of "already seen":
    1. RLM injection: this session previously injected the file (unchanged since)
    2. Tool-call: Claude read/edited this file as a tool call this turn

    Scoped to agent_key — a subagent has its own context window, so what the
    main agent was shown does not count as seen for it.

    Side effect: records the current mtime if the file is new to the session.
    """
    # Tool-call check first (fastest — just a set lookup).
    # Main agent only: the PostToolUse hook records reads with no agent
    # attribution, so a main-agent Read must not suppress injection for a
    # subagent that never saw the file. Skipping the check is the safe
    # direction — worst case we inject a slice the agent already had.
    if not agent_key and file_path in _tool_reads():
        log.debug("Tool-read dedup: skip %s", Path(file_path).name)
        return True

    sid = _session_id(repo_path, agent_key)
    _register(sid, repo_path)
    session = _sessions.setdefault(sid, {})

    try:
        current_mtime = Path(file_path).stat().st_mtime
    except OSError:
        return False

    entry = session.get(file_path)
    if entry and entry["mtime"] == current_mtime:
        log.debug("Session dedup: skip %s (unchanged)", Path(file_path).name)
        return True

    # New or changed — record it for future turns
    session[file_path] = {"mtime": current_mtime, "ts": time.monotonic()}
    _gc()
    return False


def mark_seen(repo_path: str, file_path: str, agent_key: str = "") -> None:
    """Explicitly mark a file as seen (called after injection)."""
    sid = _session_id(repo_path, agent_key)
    _register(sid, repo_path)
    session = _sessions.setdefault(sid, {})
    try:
        mtime = Path(file_path).stat().st_mtime
        session[file_path] = {"mtime": mtime, "ts": time.monotonic()}
    except OSError:
        pass


def invalidate(repo_path: str, file_path: str | None = None) -> None:
    """
    Force re-injection of a file (or all files in repo) next turn.

    Applies across every agent-scoped session for the repo — a reset should
    clear the main agent and all subagents, not just whichever bucket the
    caller happened to hash into.
    """
    for sid in _sids_for_repo(repo_path):
        if file_path is None:
            _sessions[sid] = {}
        else:
            _sessions[sid].pop(file_path, None)


def clear_all() -> None:
    """Clear all sessions (used by DELETE /session with no repo_path)."""
    _sessions.clear()
    _session_repos.clear()


def stats(repo_path: str) -> dict:
    sids = _sids_for_repo(repo_path)
    return {
        "session_id": _session_id(repo_path),
        "tracked_files": sum(len(_sessions[sid]) for sid in sids),
        "agent_sessions": len(sids),
    }
