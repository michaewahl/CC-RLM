"""
Git diff walker.

Extracts recent changes from the repo's git history:
  - Uncommitted changes (working tree vs HEAD)
  - Files changed in the last N commits on the current branch

Runs as a subprocess. Outputs JSON to stdout.

Usage:
  python -m rlm.walkers.diff --repo /path/to/repo
"""

import argparse
import json
import subprocess
from pathlib import Path


MAX_DIFF_CHARS = 6000   # hard cap before token budgeting


def run_git(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def run(repo: str) -> dict:
    repo_path = Path(repo)
    if not (repo_path / ".git").exists():
        return {"diff": "", "changed_files": [], "branch": ""}

    cwd = str(repo_path)

    # Current branch
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()

    # Uncommitted diff (staged + unstaged)
    diff = run_git(["diff", "HEAD", "--unified=3"], cwd)
    if not diff:
        # Nothing uncommitted — show last commit diff instead
        diff = run_git(["show", "--unified=3", "--no-notes"], cwd)

    # Truncate aggressively
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (truncated)"

    # List of changed files
    changed_files_raw = run_git(["diff", "HEAD", "--name-only"], cwd)
    changed_files = [f for f in changed_files_raw.splitlines() if f]

    return {
        "diff": diff,
        "changed_files": changed_files,
        "branch": branch,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    result = run(args.repo)
    print(json.dumps(result))
