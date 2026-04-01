"""
Incremental live repo index.

The key architectural upgrade: instead of re-running import walkers as subprocesses
on every request, maintain a full import graph in memory that updates only when
files change. Context pack assembly becomes a dict lookup, not a subprocess call.

Think of it as the difference between `make` and recompiling everything from scratch:
only changed files get re-walked.

Structure maintained per repo:
- import_graph: {file_path: {"imports": [...], "imported_by": [...]}}
- symbol_index: {file_path: {symbol_name: {line, type, calls}}}
- mtimes: {file_path: float}

Refresh strategy:
- `refresh_file(path)` — re-index a single changed file (fast, <50ms)
- `refresh_repo(repo)` — scan all files, update only changed ones (slow, on startup)
- `get_relevant(active_file, n)` — return top-N relevant files ranked by graph distance

Thread safety: single-writer (event loop), multiple readers. Python GIL is sufficient.
"""

import ast
import logging
import os
import time
from collections import deque, OrderedDict
from pathlib import Path

from rlm import store

log = logging.getLogger("rlm.repo_index")

# HIGH-4: LRU-capped dict — evicts least-recently-used repo when limit is reached
_MAX_INDEXES = 20
_indexes: OrderedDict[str, "RepoIndex"] = OrderedDict()


class RepoIndex:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.import_graph: dict[str, dict] = {}   # file → {imports, imported_by}
        self.symbol_index: dict[str, dict] = {}   # file → {name: {line, type, calls}}
        self.mtimes: dict[str, float] = {}
        self._ts = 0.0  # last full refresh timestamp
        self._load_from_store()

    def _load_from_store(self) -> None:
        """Warm the in-memory graph from SQLite on first creation."""
        persisted = store.load_import_graph(str(self.repo_path))
        if not persisted:
            return
        for file_path, data in persisted.items():
            self.import_graph[file_path] = {
                "imports": data["imports"],
                "imported_by": data["imported_by"],
            }
            self.mtimes[file_path] = data["mtime"]
        log.info(
            "RepoIndex warmed from SQLite: %d files for %s",
            len(persisted),
            self.repo_path.name,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_relevant(self, active_file: str, n: int = 8, task: str = "") -> list[tuple[str, float]]:
        """
        BFS from active_file through import graph.
        Returns list of (file_path, relevance_score) sorted by score desc.
        Relevance decays with graph distance: 1.5 → 1.0 → 0.7 → 0.4...
        """
        if active_file not in self.import_graph and not Path(active_file).exists():
            return []

        visited: dict[str, float] = {}
        queue: deque[tuple[str, float]] = deque()
        queue.append((active_file, 1.5))
        results = self._bfs(queue, visited, n)

        # Task-keyword file matching: inject files whose stems match task keywords
        if task:
            results = self._boost_by_task_keywords(results, task, n)

        return results

    def get_relevant_from_diff(
        self,
        changed_files: list[str],
        active_file: str,
        n: int = 10,
        task: str = "",
    ) -> list[tuple[str, float]]:
        """
        Diff-first BFS: seed from files that actually changed, then active file.

        Changed files are the work-in-progress ground truth — they get score 2.0.
        Active file gets 1.5 as before. Graph neighbors decay normally from there.

        This produces context centered on what you're actually working on right now,
        not just what the active file imports.
        """
        visited: dict[str, float] = {}
        queue: deque[tuple[str, float]] = deque()

        seen_seeds: set[str] = set()
        for f in changed_files:
            if Path(f).exists():
                queue.append((f, 2.0))
                seen_seeds.add(f)

        if active_file and active_file not in seen_seeds and Path(active_file).exists():
            queue.append((active_file, 1.5))

        if not queue:
            return self.get_relevant(active_file, n, task=task)

        results = self._bfs(queue, visited, n)

        # Task-keyword file matching
        if task:
            results = self._boost_by_task_keywords(results, task, n)

        return results

    def _bfs(
        self,
        queue: deque[tuple[str, float]],
        visited: dict[str, float],
        n: int,
    ) -> list[tuple[str, float]]:
        """Shared BFS kernel for get_relevant and get_relevant_from_diff."""
        while queue:
            current, score = queue.popleft()
            if current in visited:
                # Keep higher score if we visit via multiple paths
                if score > visited[current]:
                    visited[current] = score
                continue
            visited[current] = score

            if len(visited) >= n * 2:
                break

            next_score = score * 0.65
            if next_score < 0.1:
                continue

            neighbors = self.import_graph.get(current, {})
            for imp in neighbors.get("imports", []):
                if imp not in visited:
                    queue.append((imp, next_score))
            for importer in neighbors.get("imported_by", []):
                if importer not in visited:
                    queue.append((importer, next_score * 0.9))

        results = sorted(visited.items(), key=lambda x: -x[1])
        return results[:n]

    def _boost_by_task_keywords(
        self,
        results: list[tuple[str, float]],
        task: str,
        n: int,
    ) -> list[tuple[str, float]]:
        """
        Inject files whose stem matches task keywords (e.g. task mentions 'store'
        → rlm/store.py gets added). Scores below graph results so structural
        edges still dominate.
        """
        import re
        keywords = {
            w.lower()
            for w in re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', task)
            if len(w) > 2
        }
        # Remove common stopwords
        keywords -= {
            'what', 'does', 'how', 'the', 'and', 'for', 'bug', 'fix',
            'when', 'from', 'with', 'that', 'this', 'into', 'not', 'are',
            'refactor', 'extract', 'rename', 'move', 'add', 'remove',
            'file', 'files', 'module', 'function', 'class', 'method',
            'use', 'used', 'using', 'still', 'has', 'get', 'set',
        }
        if not keywords:
            return results

        existing = {f for f, _ in results}
        min_score = min((s for _, s in results), default=0.5) * 0.8

        injected = []
        for file_path in self.import_graph:
            if file_path in existing:
                continue
            stem = Path(file_path).stem.lower()
            # Match if any keyword appears in the file stem
            if any(kw in stem or stem in kw for kw in keywords):
                injected.append((file_path, min_score))

        if injected:
            log.info(
                "Task-keyword match: injecting %s",
                [Path(f).name for f, _ in injected],
            )
            results = results + injected
            results.sort(key=lambda x: -x[1])

        return results[:n]

    def get_symbols(self, file_path: str) -> dict:
        return self.symbol_index.get(file_path, {})

    def needs_refresh(self, file_path: str) -> bool:
        try:
            return Path(file_path).stat().st_mtime != self.mtimes.get(file_path, 0.0)
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def refresh_file(self, file_path: str) -> bool:
        """Re-index a single file. Returns True if it changed."""
        p = Path(file_path)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return False

        if self.mtimes.get(file_path) == mtime:
            return False

        imports = self._parse_imports(p)
        symbols = self._parse_symbols(p)

        # Remove this file from old imported_by entries
        old_imports = self.import_graph.get(file_path, {}).get("imports", [])
        for old_imp in old_imports:
            if old_imp in self.import_graph:
                self.import_graph[old_imp]["imported_by"] = [
                    f for f in self.import_graph[old_imp]["imported_by"]
                    if f != file_path
                ]

        # Update import graph
        resolved_imports = [
            str(self.repo_path / self._module_to_rel(imp))
            for imp in imports
            if self._module_to_rel(imp)
        ]
        self.import_graph[file_path] = {
            "imports": resolved_imports,
            "imported_by": self.import_graph.get(file_path, {}).get("imported_by", []),
        }

        # Update imported_by for files this file imports
        for imp_path in resolved_imports:
            if imp_path not in self.import_graph:
                self.import_graph[imp_path] = {"imports": [], "imported_by": []}
            if file_path not in self.import_graph[imp_path]["imported_by"]:
                self.import_graph[imp_path]["imported_by"].append(file_path)

        self.symbol_index[file_path] = symbols
        self.mtimes[file_path] = mtime

        # Persist to SQLite so graph survives server restart
        store.save_file_graph(
            str(self.repo_path),
            file_path,
            self.import_graph[file_path]["imports"],
            self.import_graph[file_path]["imported_by"],
            mtime,
        )
        return True

    def refresh_repo(self, extensions: tuple[str, ...] = (".py",)) -> dict:
        """
        Scan repo for all matching files, update only changed ones.
        Returns stats dict.
        """
        t0 = time.monotonic()
        updated = 0
        total = 0
        skip_dirs = {".venv", "__pycache__", ".git", "node_modules", ".mypy_cache", "tests", "test"}

        for p in self._iter_files(extensions, skip_dirs):
            total += 1
            if self.refresh_file(str(p)):
                updated += 1

        self._ts = time.monotonic()
        elapsed = self._ts - t0
        log.info(
            "Repo index refreshed: %d/%d files updated in %.2fs (repo=%s)",
            updated, total, elapsed, self.repo_path.name,
        )
        return {"total": total, "updated": updated, "elapsed_s": elapsed}

    def refresh_neighborhood(self, active_file: str) -> int:
        """
        Fast incremental refresh: re-index active file + its direct neighbors.
        Call this on every request instead of full refresh.
        Returns count of files updated.
        """
        targets = {active_file}
        neighbors = self.import_graph.get(active_file, {})
        targets.update(neighbors.get("imports", []))
        targets.update(neighbors.get("imported_by", []))

        updated = sum(1 for f in targets if self.refresh_file(f))
        if updated:
            log.debug("Neighborhood refresh: %d files updated around %s", updated, Path(active_file).name)
        return updated

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _iter_files(self, extensions, skip_dirs):
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                if any(fname.endswith(ext) for ext in extensions):
                    yield Path(root) / fname

    def _parse_imports(self, p: Path) -> list[str]:
        if p.suffix == ".py":
            return self._parse_python_imports(p)
        return []

    def _parse_python_imports(self, p: Path) -> list[str]:
        try:
            source = p.read_text(errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            return []
        mods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
        return mods

    def _parse_symbols(self, p: Path) -> dict:
        if p.suffix != ".py":
            return {}
        try:
            source = p.read_text(errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            return {}

        symbols = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = []
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Name):
                            calls.append(child.func.id)
                        elif isinstance(child.func, ast.Attribute):
                            calls.append(child.func.attr)
                symbols[node.name] = {
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno + 5),
                    "type": "function",
                    "calls": list(dict.fromkeys(calls)),
                }
            elif isinstance(node, ast.ClassDef):
                symbols[node.name] = {
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno + 5),
                    "type": "class",
                    "calls": [],
                }
        return symbols

    def _module_to_rel(self, module: str) -> str | None:
        parts = module.replace(".", os.sep)
        for candidate in (parts + ".py", parts + os.sep + "__init__.py"):
            if (self.repo_path / candidate).exists():
                return candidate
        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def get_or_create(repo_path: str) -> "RepoIndex":
    if repo_path in _indexes:
        _indexes.move_to_end(repo_path)  # mark as recently used
        return _indexes[repo_path]
    # HIGH-4: evict LRU entry when at capacity
    if len(_indexes) >= _MAX_INDEXES:
        evicted, _ = _indexes.popitem(last=False)
        log.info("RepoIndex LRU evict: %s", evicted)
    _indexes[repo_path] = RepoIndex(Path(repo_path))
    log.info("RepoIndex created for: %s", repo_path)
    return _indexes[repo_path]


def warm(repo_path: str) -> dict:
    """Initial full repo scan. Call once on startup or first request."""
    idx = get_or_create(repo_path)
    return idx.refresh_repo()
