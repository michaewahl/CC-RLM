"""
Tests for two critical security fixes:
  1. track_tool_reads.py uses ~/.cc-rlm/ not /tmp
  2. rlm/main.py rejects symlinks in active_file
"""

import importlib
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fix 1: tool-reads state file location
# ---------------------------------------------------------------------------

def test_track_tool_reads_uses_home_dir():
    """TOOL_READS_FILE must be under ~/.cc-rlm/, not /tmp."""
    import sys
    # Force reload to avoid stale cached module
    if "track_tool_reads" in sys.modules:
        del sys.modules["track_tool_reads"]

    hook_path = Path(__file__).parent.parent / ".claude" / "hooks"
    sys.path.insert(0, str(hook_path))
    try:
        import track_tool_reads
        assert str(track_tool_reads.TOOL_READS_FILE).startswith(str(Path.home())), (
            f"TOOL_READS_FILE should be under $HOME, got: {track_tool_reads.TOOL_READS_FILE}"
        )
        assert "/tmp" not in str(track_tool_reads.TOOL_READS_FILE), (
            "TOOL_READS_FILE must not use world-writable /tmp"
        )
    finally:
        sys.path.remove(str(hook_path))


def test_track_tool_reads_matches_session_path():
    """TOOL_READS_FILE in hook must match _TOOL_READS_FILE in session.py."""
    import sys

    hook_path = Path(__file__).parent.parent / ".claude" / "hooks"
    sys.path.insert(0, str(hook_path))
    try:
        import track_tool_reads
        from rlm import session
        assert track_tool_reads.TOOL_READS_FILE == session._TOOL_READS_FILE, (
            f"Path mismatch: hook writes to {track_tool_reads.TOOL_READS_FILE}, "
            f"session reads from {session._TOOL_READS_FILE}"
        )
    finally:
        sys.path.remove(str(hook_path))


# ---------------------------------------------------------------------------
# Fix 2: symlink rejection in active_file validation
# ---------------------------------------------------------------------------

def test_active_file_symlink_rejected():
    """active_file pointing to a symlink must return HTTP 400."""
    from fastapi.testclient import TestClient
    from rlm.main import app

    client = TestClient(app, raise_server_exceptions=False)

    with tempfile.TemporaryDirectory() as repo_dir:
        # Create a real file outside the repo
        outside = Path(repo_dir).parent / "outside_secret.txt"
        outside.write_text("secret")

        # Create a symlink inside the repo pointing outside
        symlink = Path(repo_dir) / "link.py"
        symlink.symlink_to(outside)

        try:
            resp = client.post("/context", json={
                "task": "test task",
                "repo_path": repo_dir,
                "active_file": str(symlink),
            })
            assert resp.status_code == 400, (
                f"Expected 400 for symlink active_file, got {resp.status_code}"
            )
            assert "symlink" in resp.json().get("detail", "").lower()
        finally:
            outside.unlink(missing_ok=True)


def test_active_file_normal_file_allowed():
    """active_file pointing to a regular file within repo must not be rejected for symlink reason."""
    from fastapi.testclient import TestClient
    from rlm.main import app

    client = TestClient(app, raise_server_exceptions=False)

    with tempfile.TemporaryDirectory() as repo_dir:
        real_file = Path(repo_dir) / "main.py"
        real_file.write_text("x = 1")

        resp = client.post("/context", json={
            "task": "test task",
            "repo_path": repo_dir,
            "active_file": str(real_file),
        })
        # Should not 400 with "symlink" error (may fail for other reasons)
        if resp.status_code == 400:
            assert "symlink" not in resp.json().get("detail", "").lower(), (
                "Regular file incorrectly rejected as symlink"
            )
