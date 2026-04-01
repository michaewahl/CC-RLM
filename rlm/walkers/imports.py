"""
Import graph walker.

Given a repo and an active file, answers:
  - What does this file import?
  - What other files in the repo import this file?

Runs as a subprocess. Outputs JSON to stdout.

Usage:
  python -m rlm.walkers.imports --repo /path/to/repo --file src/foo.py
"""

import argparse
import ast
import json
import os
import sys
from pathlib import Path


def get_imports(file_path: Path) -> list[str]:
    """Extract all import module names from a Python file."""
    try:
        source = file_path.read_text(errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def module_to_file(module: str, repo: Path) -> str | None:
    """Try to resolve a module name to a file path within the repo."""
    parts = module.replace(".", os.sep)
    candidates = [
        repo / (parts + ".py"),
        repo / parts / "__init__.py",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def find_importers(target_file: Path, repo: Path, max_files: int = 5_000) -> list[str]:
    """Find all Python files in the repo that import the target file."""
    target_stem = target_file.stem
    importers = []
    scanned = 0
    for py_file in repo.rglob("*.py"):
        if scanned >= max_files:
            break
        scanned += 1
        if py_file == target_file:
            continue
        for mod in get_imports(py_file):
            if mod.endswith(target_stem) or mod.endswith(f".{target_stem}"):
                importers.append(str(py_file))
                break
    return importers


def run(repo: str, file: str) -> dict:
    repo_path = Path(repo)
    file_path = Path(file) if file else None

    if not file_path or not file_path.exists():
        # No active file — return empty
        return {"imports": [], "imported_by": [], "resolved": {}}

    raw_imports = get_imports(file_path)
    resolved = {}
    for mod in raw_imports:
        resolved_path = module_to_file(mod, repo_path)
        if resolved_path:
            resolved[mod] = resolved_path

    imported_by = find_importers(file_path, repo_path)

    return {
        "imports": list(resolved.values()),
        "imported_by": imported_by,
        "resolved": resolved,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--file", default="")
    args = parser.parse_args()
    result = run(args.repo, args.file)
    print(json.dumps(result))
