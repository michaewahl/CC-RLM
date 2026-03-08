"""
Context pack assembler.

Takes raw walker results and produces a ContextPack:
- Relevant file slices (not full files)
- Symbol / call graph fragments
- Recent git diff
- Token budget enforced throughout

Target: < RLM_TOKEN_BUDGET tokens regardless of repo size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import tiktoken

log = logging.getLogger("rlm.context_pack")

# cl100k_base is close enough for MiniMax tokenization estimates
_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


@dataclass
class FileSlice:
    file: str
    lines: str          # e.g. "45-82"
    content: str
    relevance: float = 1.0   # 0–1, higher = more relevant


@dataclass
class ContextPack:
    task: str
    active_file: str
    repo_path: str
    slices: list[FileSlice] = field(default_factory=list)
    symbol_graph: dict[str, list[str]] = field(default_factory=dict)
    recent_diff: str = ""
    token_count: int = 0

    def render(self) -> str:
        """Produce the system preamble handed to the model."""
        parts = [
            f"# Codebase Context\n",
            f"**Task:** {self.task}\n",
            f"**Active file:** {self.active_file}\n",
        ]

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
) -> ContextPack:
    """
    Build a ContextPack from raw walker results, respecting the token budget.

    walker_results keys:
      - imports:  {imports: [...], imported_by: [...]}
      - symbols:  {symbols: {name: {file, line, calls: [...]}}}
      - diff:     {diff: str, changed_files: [...]}
    """
    pack = ContextPack(task=task, active_file=active_file, repo_path=repo_path)
    budget = token_budget

    # 1. Reserve tokens for task description header
    header_tokens = count_tokens(f"# Codebase Context\n**Task:** {task}\n**Active file:** {active_file}\n")
    budget -= header_tokens

    # 2. Git diff — high signal, capped at 20% of budget
    diff_data = walker_results.get("diff", {})
    diff_text = diff_data.get("diff", "") if isinstance(diff_data, dict) else ""
    if diff_text:
        diff_budget = int(token_budget * 0.20)
        if count_tokens(diff_text) > diff_budget:
            # Truncate to budget (rough: ~4 chars/token)
            diff_text = diff_text[: diff_budget * 4]
        pack.recent_diff = diff_text
        budget -= count_tokens(diff_text)

    # 3. Symbol graph — compact, high value
    symbols_data = walker_results.get("symbols", {})
    sym_graph = {}
    if isinstance(symbols_data, dict):
        for name, info in symbols_data.get("symbols", {}).items():
            calls = info.get("calls", [])
            if calls:
                sym_graph[name] = calls
    pack.symbol_graph = sym_graph
    sym_text = "\n".join(f"{k} → {', '.join(v)}" for k, v in sym_graph.items())
    budget -= count_tokens(sym_text)

    # 4. File slices from import walker — fill remaining budget
    import_data = walker_results.get("imports", {})
    relevant_files: list[tuple[str, float]] = []

    if isinstance(import_data, dict):
        # Direct imports of active file → highest relevance
        for f in import_data.get("imports", []):
            relevant_files.append((f, 1.0))
        # Files that import active file → medium relevance
        for f in import_data.get("imported_by", []):
            relevant_files.append((f, 0.7))

    for filepath, relevance in sorted(relevant_files, key=lambda x: -x[1]):
        if budget <= 0:
            break
        try:
            content = _read_file_slice(filepath, max_lines=60)
        except OSError:
            continue
        tok = count_tokens(content)
        if tok > budget:
            content = _truncate_to_tokens(content, budget)
            tok = budget
        pack.slices.append(FileSlice(
            file=filepath,
            lines="1-60",
            content=content,
            relevance=relevance,
        ))
        budget -= tok

    pack.token_count = token_budget - budget
    log.info("Context pack: %d tokens (budget %d)", pack.token_count, token_budget)
    return pack


def _read_file_slice(path: str, max_lines: int = 60) -> str:
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[:max_lines])


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    tokens = _enc.encode(text)
    return _enc.decode(tokens[:max_tokens])
