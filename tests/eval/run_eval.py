#!/usr/bin/env python3
"""
CC-RLM Eval Harness

Compares RLM context pack (smart) vs naive full-file injection (dumb baseline).

Usage:
    # RLM server must be running on port 8081
    poetry run python tests/eval/run_eval.py

    # Or point at a different RLM endpoint
    RLM_URL=http://localhost:8081 poetry run python tests/eval/run_eval.py

    # Run a single case
    poetry run python tests/eval/run_eval.py --case classify-function

Outputs a comparison table to stdout. JSON report saved to tests/eval/results/.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import httpx
import tiktoken

REPO_ROOT = Path(__file__).parent.parent.parent
CASES_FILE = Path(__file__).parent / "cases.json"
RESULTS_DIR = Path(__file__).parent / "results"
RLM_URL = os.environ.get("RLM_URL", "http://localhost:8081")

_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def naive_token_count(repo_path: Path) -> tuple[int, list[str]]:
    """Count tokens in all .py files — naive full-repo injection baseline."""
    total = 0
    files = []
    for p in sorted(repo_path.rglob("*.py")):
        # Skip venv, __pycache__, test cache
        parts = p.parts
        if any(skip in parts for skip in (".venv", "__pycache__", ".git", "node_modules")):
            continue
        try:
            text = p.read_text(errors="replace")
            total += count_tokens(text)
            files.append(str(p.relative_to(repo_path)))
        except OSError:
            continue
    return total, files


@dataclass
class EvalResult:
    case_id: str
    task: str
    active_file: str
    expected_files: list[str]
    category: str = "exploration"

    # RLM (smart)
    rlm_tokens: int = 0
    rlm_files_included: list[str] = field(default_factory=list)
    rlm_has_diff: bool = False
    rlm_symbol_count: int = 0
    rlm_latency_ms: float = 0.0
    rlm_error: str = ""

    # Naive baseline
    naive_tokens: int = 0
    naive_file_count: int = 0

    # Derived
    @property
    def reduction_pct(self) -> float:
        if self.naive_tokens == 0:
            return 0.0
        return (1 - self.rlm_tokens / self.naive_tokens) * 100

    @property
    def expected_files_present(self) -> list[str]:
        """Which expected files actually appeared in the RLM slices."""
        included_bases = {Path(f).name for f in self.rlm_files_included}
        return [f for f in self.expected_files if Path(f).name in included_bases]

    @property
    def recall(self) -> float:
        if not self.expected_files:
            return 1.0
        return len(self.expected_files_present) / len(self.expected_files)


def run_rlm_case(case: dict, repo_path: Path) -> tuple[int, dict, float, str]:
    """Hit RLM /context endpoint. Returns (tokens, pack, latency_ms, error)."""
    active_file = str(repo_path / case["active_file"]) if case.get("active_file") else ""
    payload = {
        "task": case["task"],
        "active_file": active_file,
        "repo_path": str(repo_path),
    }
    t0 = time.monotonic()
    try:
        resp = httpx.post(f"{RLM_URL}/context", json=payload, timeout=15.0)
        latency_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()
        data = resp.json()
        return data["token_count"], data["pack"], latency_ms, ""
    except httpx.ConnectError:
        return 0, {}, 0.0, f"Cannot connect to RLM at {RLM_URL} — is it running?"
    except Exception as exc:
        return 0, {}, (time.monotonic() - t0) * 1000, str(exc)


def format_table(results: list[EvalResult]) -> str:
    lines = []
    header = f"{'Case':<28} {'Cat':>10} {'RLM tok':>8} {'Naive tok':>10} {'Reduction':>10} {'Recall':>7} {'Latency':>9}"
    lines.append(header)
    lines.append("-" * len(header))

    for r in results:
        if r.rlm_error:
            lines.append(f"{r.case_id:<28} {r.category:>10} ERROR: {r.rlm_error}")
            continue
        lines.append(
            f"{r.case_id:<28} "
            f"{r.category:>10} "
            f"{r.rlm_tokens:>8,} "
            f"{r.naive_tokens:>10,} "
            f"{r.reduction_pct:>9.1f}% "
            f"{r.recall:>6.0%}  "
            f"{r.rlm_latency_ms:>7.0f}ms"
        )

    if any(not r.rlm_error for r in results):
        good = [r for r in results if not r.rlm_error]
        avg_reduction = sum(r.reduction_pct for r in good) / len(good)
        avg_recall = sum(r.recall for r in good) / len(good)
        avg_latency = sum(r.rlm_latency_ms for r in good) / len(good)
        lines.append("-" * len(header))
        lines.append(
            f"{'AVERAGE':<28} {'':>10} "
            f"{'':>8} "
            f"{'':>10} "
            f"{avg_reduction:>9.1f}% "
            f"{avg_recall:>6.0%}  "
            f"{avg_latency:>7.0f}ms"
        )

        # Per-category breakdown
        categories = sorted(set(r.category for r in good))
        if len(categories) > 1:
            lines.append("")
            lines.append("=== By Category ===")
            cat_header = f"{'Category':<16} {'Cases':>6} {'Avg Reduction':>14} {'Avg Recall':>11} {'Avg Latency':>12}"
            lines.append(cat_header)
            lines.append("-" * len(cat_header))
            for cat in categories:
                cat_results = [r for r in good if r.category == cat]
                cat_reduction = sum(r.reduction_pct for r in cat_results) / len(cat_results)
                cat_recall = sum(r.recall for r in cat_results) / len(cat_results)
                cat_latency = sum(r.rlm_latency_ms for r in cat_results) / len(cat_results)
                lines.append(
                    f"{cat:<16} "
                    f"{len(cat_results):>6} "
                    f"{cat_reduction:>13.1f}% "
                    f"{cat_recall:>10.0%}  "
                    f"{cat_latency:>10.0f}ms"
                )

    return "\n".join(lines)


def detail_section(results: list[EvalResult]) -> str:
    lines = ["\n=== File Coverage Detail ===\n"]
    for r in results:
        if r.rlm_error:
            continue
        lines.append(f"[{r.case_id}]")
        lines.append(f"  Task: {r.task[:80]}{'...' if len(r.task) > 80 else ''}")
        lines.append(f"  Active file: {r.active_file}")
        lines.append(f"  Files in pack ({len(r.rlm_files_included)}):")
        for f in r.rlm_files_included:
            tag = " ✓" if any(Path(f).name == Path(e).name for e in r.expected_files) else ""
            lines.append(f"    {Path(f).name}{tag}")
        missing = [e for e in r.expected_files if Path(e).name not in {Path(f).name for f in r.rlm_files_included}]
        if missing:
            lines.append(f"  Missing expected: {', '.join(missing)}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="CC-RLM eval harness")
    parser.add_argument("--case", help="Run a single case by ID")
    parser.add_argument("--category", help="Run only cases in a category (exploration, refactor, bugfix)")
    parser.add_argument("--json", action="store_true", help="Write JSON report")
    parser.add_argument(
        "--isolate", action="store_true", default=True,
        help="Reset session dedup between cases (default: True — per-case recall)",
    )
    parser.add_argument(
        "--no-isolate", dest="isolate", action="store_false",
        help="Shared session across cases (simulates real multi-turn use)",
    )
    args = parser.parse_args()

    cases = json.loads(CASES_FILE.read_text())
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"No case with id '{args.case}'")
            sys.exit(1)
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]
        if not cases:
            print(f"No cases with category '{args.category}'")
            sys.exit(1)

    # Check RLM health
    try:
        resp = httpx.get(f"{RLM_URL}/health", timeout=3.0)
        resp.raise_for_status()
    except Exception:
        print(f"ERROR: RLM not reachable at {RLM_URL}")
        print("Start it with: poetry run uvicorn rlm.main:app --port 8081")
        sys.exit(1)

    # Naive baseline (same for all cases — it's just all .py files in repo)
    naive_tokens, naive_files = naive_token_count(REPO_ROOT)
    print(f"Naive baseline: {naive_tokens:,} tokens across {len(naive_files)} .py files")
    if args.isolate:
        print("Session isolation: ON (each case gets a fresh session)\n")
    else:
        print("Session isolation: OFF (shared session across cases — real multi-turn sim)\n")

    results = []
    # Clear tool-reads file so it doesn't interfere with eval dedup
    tool_reads_file = Path("/tmp/cc-rlm-tool-reads.json")
    if tool_reads_file.exists():
        tool_reads_file.unlink()

    for case in cases:
        if args.isolate:
            # Reset session so each case is scored independently
            try:
                httpx.delete(f"{RLM_URL}/session", timeout=3.0)
            except Exception:
                pass

        print(f"Running: {case['id']}...", end=" ", flush=True)
        tokens, pack, latency, error = run_rlm_case(case, REPO_ROOT)

        r = EvalResult(
            case_id=case["id"],
            task=case["task"],
            active_file=case.get("active_file", ""),
            expected_files=case.get("expected_files", []),
            category=case.get("category", "exploration"),
            rlm_tokens=tokens,
            rlm_files_included=[s["file"] for s in pack.get("slices", [])],
            rlm_has_diff=pack.get("has_diff", False),
            rlm_symbol_count=pack.get("symbol_count", 0),
            rlm_latency_ms=latency,
            rlm_error=error,
            naive_tokens=naive_tokens,
            naive_file_count=len(naive_files),
        )
        results.append(r)

        if error:
            print(f"ERROR: {error}")
        else:
            print(f"{tokens:,} tok  {r.reduction_pct:.1f}% reduction  recall={r.recall:.0%}")

    print()
    print(format_table(results))
    print(detail_section(results))

    if args.json:
        RESULTS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = RESULTS_DIR / f"eval_{ts}.json"
        report = {
            "timestamp": ts,
            "rlm_url": RLM_URL,
            "naive_tokens": naive_tokens,
            "naive_file_count": len(naive_files),
            "cases": [
                {
                    **asdict(r),
                    "reduction_pct": r.reduction_pct,
                    "recall": r.recall,
                    "expected_files_present": r.expected_files_present,
                }
                for r in results
            ],
        }
        out.write_text(json.dumps(report, indent=2))
        print(f"Report saved → {out}")


if __name__ == "__main__":
    main()
