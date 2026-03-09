"""
Context pack assembler.

Takes raw walker results and produces a ContextPack:
- Relevant file slices (symbol-level, not arbitrary line ranges)
- Symbol / call graph fragments
- Recent git diff
- Token budget enforced throughout
- Session deduplication: unchanged files already seen this session are skipped

Target: < RLM_TOKEN_BUDGET tokens regardless of repo size.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken

from rlm.config import settings
from rlm import session as session_tracker

log = logging.getLogger("rlm.context_pack")

# cl100k_base is close enough for MiniMax tokenization estimates
_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


@dataclass
class FileSlice:
    file: str
    lines: str          # e.g. "45-82" or "12-45, 67-89"
    content: str
    relevance: float = 1.0   # higher = more relevant


@dataclass
class ContextPack:
    task: str
    active_file: str
    repo_path: str
    slices: list[FileSlice] = field(default_factory=list)
    symbol_graph: dict[str, list[str]] = field(default_factory=dict)
    recent_diff: str = ""
    token_count: int = 0
    deduped_files: list[str] = field(default_factory=list)  # files skipped by session dedup

    def render(self) -> str:
        """Produce the system preamble handed to the model."""
        parts = [
            f"# Codebase Context\n",
            f"**Task:** {self.task}\n",
            f"**Active file:** {self.active_file}\n",
        ]

        if self.deduped_files:
            names = ", ".join(Path(f).name for f in self.deduped_files)
            parts.append(f"*Already in context (unchanged): {names}*\n")

        if self.slices:
            parts.append("\n## Relevant Code\n")
            for s in self.slices:
                parts.append(f"```\n# {s.file}  lines {s.lines}\n{s.content}\n```\n")

        if self.symbol_graph:
            parts.append("\n## Call Graph\n")
            for sym, calls in self.symbol_graph.items():
                parts.append(f"- `{sym}` → {', '.join(f'`{c}`' for c in calls)}\n")

        if self.recent_diff:
            parts.append("\n## Recent Changes\n```diff\n")
            parts.append(self.recent_diff)
            parts.append("\n```\n")

        return "".join(parts)


def assemble(
    task: str,
    active_file: str,
    repo_path: str,
    walker_results: dict,
    token_budget: int,
    relevant_files: list[tuple[str, float]] | None = None,
) -> ContextPack:
    """
    Build a ContextPack from raw walker results, respecting the token budget.

    walker_results keys:
      - imports:  {imports: [...], imported_by: [...]}
      - symbols:  {symbols: {name: {file, line, calls: [...]}}}
      - diff:     {diff: str, changed_files: [...]}

    relevant_files: pre-ranked list from RepoIndex (overrides import walker ranking
                    when provided). Format: [(abs_path, score), ...]
    """
    pack = ContextPack(task=task, active_file=active_file, repo_path=repo_path)
    budget = token_budget

    # Extract keywords from task for symbol relevance scoring
    task_keywords = _task_keywords(task)

    # Collect names called by the active file's symbols (for cross-file relevance)
    symbols_data = walker_results.get("symbols", {})
    called_names: set[str] = set()
    if isinstance(symbols_data, dict):
        for _name, info in symbols_data.get("symbols", {}).items():
            called_names.update(info.get("calls", []))

    # 1. Reserve tokens for task description header
    header_tokens = count_tokens(
        f"# Codebase Context\n**Task:** {task}\n**Active file:** {active_file}\n"
    )
    budget -= header_tokens

    # 2. Git diff — high signal, capped at 20% of budget
    diff_data = walker_results.get("diff", {})
    diff_text = diff_data.get("diff", "") if isinstance(diff_data, dict) else ""
    if diff_text:
        diff_budget = int(token_budget * 0.20)
        if count_tokens(diff_text) > diff_budget:
            diff_text = diff_text[: diff_budget * 4]
        pack.recent_diff = diff_text
        budget -= count_tokens(diff_text)

    # 3. Symbol graph — compact, high value
    sym_graph = {}
    if isinstance(symbols_data, dict):
        for name, info in symbols_data.get("symbols", {}).items():
            calls = info.get("calls", [])
            if calls:
                sym_graph[name] = calls
    pack.symbol_graph = sym_graph
    sym_text = "\n".join(f"{k} → {', '.join(v)}" for k, v in sym_graph.items())
    budget -= count_tokens(sym_text)

    # 4. File slices — symbol-level extraction, session dedup, budget-aware
    if relevant_files is None:
        # Fallback: build from import walker results
        import_data = walker_results.get("imports", {})
        relevant_files = []
        if active_file and Path(active_file).exists():
            relevant_files.append((active_file, 1.5))
        if isinstance(import_data, dict):
            for f in import_data.get("imports", []):
                if f != active_file:
                    relevant_files.append((f, 1.0))
            for f in import_data.get("imported_by", []):
                if f != active_file:
                    relevant_files.append((f, 0.7))
    else:
        # Ensure active file is always first with highest relevance
        relevant_files = [
            (f, max(score, 1.5) if f == active_file else score)
            for f, score in relevant_files
        ]
        # If active file wasn't in the ranked list at all, inject it at top
        if active_file and active_file not in {f for f, _ in relevant_files}:
            if Path(active_file).exists():
                relevant_files.append((active_file, 2.0))
        relevant_files.sort(key=lambda x: -x[1])

    # Extensions worth slicing (code and config with structure)
    _SLICEABLE = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb", ".sh"}

    for filepath, relevance in relevant_files:
        if budget <= 0:
            break
        p = Path(filepath)
        if not p.exists():
            continue
        # Skip non-code files — they have no symbols and add noise
        if p.suffix not in _SLICEABLE:
            continue

        # Session deduplication: skip files Claude already has in context (unchanged)
        # Never dedup the active file — the user is staring at it right now
        if filepath != active_file and settings.session_dedup_enabled and session_tracker.already_seen(repo_path, filepath):
            pack.deduped_files.append(filepath)
            continue

        # Symbol-level slicing: extract relevant function/class bodies, not first N lines
        content, lines_desc = _extract_symbol_slice(filepath, task_keywords, called_names)

        tok = count_tokens(content)
        if tok > budget:
            content = _truncate_to_tokens(content, budget)
            lines_desc = lines_desc + " (truncated)"
            tok = budget

        pack.slices.append(FileSlice(
            file=filepath,
            lines=lines_desc,
            content=content,
            relevance=relevance,
        ))
        budget -= tok

    pack.token_count = token_budget - budget
    log.info(
        "Context pack: %d tokens (budget %d, %d files, %d deduped)",
        pack.token_count, token_budget, len(pack.slices), len(pack.deduped_files),
    )
    return pack


# ------------------------------------------------------------------
# Symbol-level slicing
# ------------------------------------------------------------------

def _task_keywords(task: str) -> set[str]:
    """Extract identifiers and meaningful words from the task description."""
    words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', task)
    stopwords = {
        'what', 'does', 'how', 'is', 'are', 'to', 'and', 'or', 'it',
        'in', 'of', 'for', 'this', 'that', 'do', 'get', 'set', 'the',
        'when', 'where', 'which', 'with', 'from', 'by', 'be', 'its', 'not',
        'a', 'an', 'can', 'will', 'via', 'use', 'used', 'using',
    }
    return {w for w in words if w.lower() not in stopwords and len(w) > 2}


def _extract_symbol_slice(
    path: str,
    task_keywords: set[str],
    target_names: set[str],
) -> tuple[str, str]:
    """
    Extract relevant function/class bodies from a file using AST.

    Instead of blindly taking the first 60 lines, we:
    1. Parse the file's AST to find all top-level symbol definitions
    2. Score each symbol by relevance to the task
    3. Extract the actual body lines for the top symbols
    4. Merge adjacent ranges to avoid gaps

    Returns (content, lines_desc) e.g. ("def foo():\n    ...", "12-45, 67-89")
    Falls back to first-N-lines if parsing fails.
    """
    p = Path(path)
    if p.suffix not in (".py",):
        # Non-Python: fall back to simple slice (TS walker handles those separately)
        return _read_file_slice(path, max_lines=60), "1-60"

    try:
        source = p.read_text(errors="replace")
        lines = source.splitlines(keepends=True)
        tree = ast.parse(source, filename=path)
    except (OSError, SyntaxError):
        return _read_file_slice(path, max_lines=60), "1-60"

    # Collect top-level symbols (functions and classes at module scope)
    top_level_nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level_nodes.append(node)

    if not top_level_nodes:
        return _read_file_slice(path, max_lines=60), "1-60"

    # Score each symbol
    scored: list[tuple[float, int, int, str]] = []  # (score, start, end, name)
    kw_lower = {k.lower() for k in task_keywords}

    for node in top_level_nodes:
        name = node.name
        end = getattr(node, "end_lineno", node.lineno + 10)
        start = node.lineno - 1  # 0-indexed

        # Score: task mention > called by active file > everything else
        if name.lower() in kw_lower or name in task_keywords:
            score = 2.0
        elif name in target_names:
            score = 1.5
        else:
            score = 0.4

        scored.append((score, start, end, name))

    # Always include all high-relevance symbols; include others if budget allows
    high = [s for s in scored if s[0] >= 1.5]
    low = [s for s in scored if s[0] < 1.5]

    selected = list(high) or list(scored[:3])  # at least top 3 if no high-relevance

    # Also add low-relevance symbols for context (the reader needs imports, constants, etc.)
    # up to ~40% of the file by count
    filler_count = max(0, len(top_level_nodes) // 3 - len(selected))
    selected.extend(low[:filler_count])

    # Sort by line number for readable output
    selected.sort(key=lambda x: x[1])

    # Merge adjacent/overlapping ranges (gap ≤ 3 lines gets merged)
    merged = _merge_ranges([(s, e) for _, s, e, _ in selected], gap=3)

    # Extract content
    content_parts = []
    desc_parts = []
    for start, end in merged:
        chunk = "".join(lines[start:min(end, len(lines))])
        content_parts.append(chunk)
        desc_parts.append(f"{start + 1}-{end}")

    return "\n".join(content_parts), ", ".join(desc_parts)


def _merge_ranges(ranges: list[tuple[int, int]], gap: int = 3) -> list[tuple[int, int]]:
    if not ranges:
        return []
    sorted_r = sorted(ranges)
    merged = [sorted_r[0]]
    for start, end in sorted_r[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + gap:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _read_file_slice(path: str, max_lines: int = 60) -> str:
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[:max_lines])


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    tokens = _enc.encode(text)
    return _enc.decode(tokens[:max_tokens])
