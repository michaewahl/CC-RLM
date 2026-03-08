"""
Symbol and call graph walker.

Given a repo and an active file, extracts:
  - All top-level functions and classes defined in the file
  - What each function calls (one level deep)
  - Line numbers for navigation

Runs as a subprocess. Outputs JSON to stdout.

Usage:
  python -m rlm.walkers.symbols --repo /path/to/repo --file src/foo.py
"""

import argparse
import ast
import json
from pathlib import Path


class CallCollector(ast.NodeVisitor):
    def __init__(self):
        self.calls: list[str] = []

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            self.calls.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.calls.append(node.func.attr)
        self.generic_visit(node)


def extract_symbols(file_path: Path) -> dict:
    try:
        source = file_path.read_text(errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError):
        return {}

    symbols = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            collector = CallCollector()
            collector.visit(node)
            symbols[node.name] = {
                "file": str(file_path),
                "line": node.lineno,
                "type": "function",
                "calls": list(dict.fromkeys(collector.calls)),  # dedupe, preserve order
            }
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in ast.walk(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n is not node
            ]
            symbols[node.name] = {
                "file": str(file_path),
                "line": node.lineno,
                "type": "class",
                "methods": methods,
                "calls": [],
            }

    return symbols


def run(repo: str, file: str) -> dict:
    file_path = Path(file) if file else None

    if not file_path or not file_path.exists():
        return {"symbols": {}}

    symbols = extract_symbols(file_path)
    return {"symbols": symbols, "file": str(file_path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--file", default="")
    args = parser.parse_args()
    result = run(args.repo, args.file)
    print(json.dumps(result))
