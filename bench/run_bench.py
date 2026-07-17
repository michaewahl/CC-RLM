#!/usr/bin/env python3
"""
CC-RLM multi-repo benchmark.

For each repo: measure the NAIVE full-repo token dump (baseline) vs the RLM
context pack (smart), across git-diff-seeded tasks (recent real commits).
Reports token savings + absolute pack size by language and repo size.

Baseline definition (transparent, matches the original CC-RLM eval):
  naive = sum of tiktoken(cl100k_base) tokens over ALL source files of the
  repo's language, skipping vendor/build/cache dirs. This is "dump the whole
  repo into the prompt" — the thing CC-RLM replaces.

The RLM context pack is built by the PRODUCTION /context endpoint (same path
that produced the original 82% number). No LLM/Ollama needed — /context is
pure structural assembly.

Usage:
  # 1. start the RLM server:
  poetry run uvicorn rlm.main:app --port 8081 --host 127.0.0.1
  # 2. run the benchmark:
  poetry run python bench/run_bench.py                 # all repos
  poetry run python bench/run_bench.py --only pallets/click psf/requests
  poetry run python bench/run_bench.py --cases 5 --depth 80
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
import tiktoken

RLM_URL = os.environ.get("RLM_URL", "http://localhost:8081")
ENC = tiktoken.get_encoding("cl100k_base")
LANG_EXT = {"python": {".py"}, "ts": {".ts", ".tsx", ".js", ".jsx"}}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", "out", "coverage", ".mypy_cache", ".pytest_cache", "vendor", ".turbo",
}
HERE = Path(__file__).parent
CLONES = HERE / "clones"
RESULTS = HERE / "results"


def count_tokens(text: str) -> int:
    return len(ENC.encode(text))


def clone(url: str, dest: Path, depth: int) -> None:
    if dest.exists():
        return
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", str(depth), url, str(dest)],
        check=True,
    )


def source_files(repo: Path, exts: set[str]):
    for p in repo.rglob("*"):
        if (
            p.suffix.lower() in exts
            and p.is_file()
            and not any(s in p.parts for s in SKIP_DIRS)
        ):
            yield p


def naive_baseline(repo: Path, exts: set[str]) -> tuple[int, int]:
    """Total tokens + file count for a full-repo dump of the language's source."""
    total, n = 0, 0
    for p in source_files(repo, exts):
        try:
            total += count_tokens(p.read_text(errors="replace"))
            n += 1
        except OSError:
            continue
    return total, n


def git_diff_cases(repo: Path, exts: set[str], n_cases: int) -> list[dict]:
    """Seed realistic tasks from recent commits that touched source files."""
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--pretty=%H%x1f%s", "-n", "300"],
        capture_output=True, text=True,
    ).stdout
    cases: list[dict] = []
    for line in log.splitlines():
        h, _, subj = line.partition("\x1f")
        if not h:
            continue
        names = subprocess.run(
            ["git", "-C", str(repo), "show", "--name-only", "--pretty=format:", h],
            capture_output=True, text=True,
        ).stdout.split()
        src = [f for f in names if Path(f).suffix.lower() in exts and (repo / f).is_file()]
        if src:
            cases.append({
                "commit": h[:8],
                "task": (subj or f"update {src[0]}")[:200],
                "active_file": src[0],
                "changed_files": src,  # all source files this commit touched
            })
        if len(cases) >= n_cases:
            break
    return cases


def rlm_pack(repo: Path, active_file: str, task: str) -> tuple[int, set[str]]:
    """Return (pack token count, set of absolute file paths included in the pack)."""
    payload = {
        "task": task,
        "active_file": str((repo / active_file).resolve()),
        "repo_path": str(repo.resolve()),
    }
    r = httpx.post(f"{RLM_URL}/context", json=payload, timeout=180.0)
    r.raise_for_status()
    data = r.json()
    files = set()
    for s in data.get("pack", {}).get("slices", []):
        f = s.get("file")
        if not f:
            continue
        p = Path(f)
        files.add(str((repo / p).resolve() if not p.is_absolute() else p.resolve()))
    return data["token_count"], files


def recall(repo: Path, changed_files: list[str], pack_files: set[str]) -> float:
    """Fraction of the commit's changed source files that appear in the pack."""
    changed = {str((repo / f).resolve()) for f in changed_files}
    if not changed:
        return 0.0
    return len(changed & pack_files) / len(changed)


def current_ref(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()


def checkout(repo: Path, ref: str) -> None:
    """Detached checkout so `git show HEAD` = that commit's diff (RLM's seed)."""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-f", ref], check=True)


def clear_rlm(repo: Path) -> None:
    """Invalidate RLM's cached index/session so it re-reads the new working tree."""
    rp = str(repo.resolve())
    for ep in ("cache", "session"):
        try:
            httpx.delete(f"{RLM_URL}/{ep}", params={"repo_path": rp}, timeout=10.0)
        except Exception:
            pass


def rlm_healthy() -> bool:
    try:
        return httpx.get(f"{RLM_URL}/health", timeout=5.0).status_code == 200
    except Exception:
        return False


def tier_for(baseline_tokens: int) -> str:
    if baseline_tokens < 50_000:
        return "small"
    if baseline_tokens < 300_000:
        return "medium"
    return "large"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos-file", default=str(HERE / "repos.json"))
    ap.add_argument("--only", nargs="*", help="subset of repo names to run")
    ap.add_argument("--cases", type=int, default=5, help="git-diff-seeded cases per repo")
    ap.add_argument("--depth", type=int, default=80, help="git clone depth")
    args = ap.parse_args()

    if not rlm_healthy():
        print(f"ERROR: RLM server not reachable at {RLM_URL}")
        print("Start it: poetry run uvicorn rlm.main:app --port 8081 --host 127.0.0.1")
        return 1

    repos = json.loads(Path(args.repos_file).read_text())["repos"]
    if args.only:
        want = set(args.only)
        repos = [r for r in repos if r["name"] in want]
    CLONES.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for i, repo_meta in enumerate(repos, 1):
        name, url, lang = repo_meta["name"], repo_meta["url"], repo_meta["lang"]
        exts = LANG_EXT[lang]
        dest = CLONES / name.replace("/", "__")
        print(f"\n[{i}/{len(repos)}] {name} ({lang})")
        try:
            print("  cloning…", flush=True)
            clone(url, dest, args.depth)
            orig_ref = current_ref(dest)
            cases = git_diff_cases(dest, exts, args.cases)
            if not cases:
                print("  SKIP: no git-diff-seeded cases")
                continue
            print("  measuring naive baseline…", flush=True)
            baseline, nfiles = naive_baseline(dest, exts)
            if baseline == 0:
                print("  SKIP: no source files found")
                continue
            packs, recalls = [], []
            for c in cases:
                try:
                    # Check out the commit so RLM seeds relevance from ITS diff
                    # (git show HEAD), reproducing the real mid-task condition.
                    checkout(dest, c["commit"])
                    clear_rlm(dest)
                    t, pack_files = rlm_pack(dest, c["active_file"], c["task"])
                    packs.append(t)
                    recalls.append(recall(dest, c["changed_files"], pack_files))
                except Exception as exc:
                    print(f"    case {c['commit']} failed: {exc}")
            checkout(dest, orig_ref)  # restore
            if not packs:
                print("  SKIP: all cases failed")
                continue
            med_pack = int(statistics.median(packs))
            savings = 1 - (med_pack / baseline)
            med_recall = round(statistics.median(recalls) * 100, 1)
            row = {
                "repo": name, "lang": lang,
                "baseline_tokens": baseline, "source_files": nfiles,
                "tier": tier_for(baseline),
                "cases": len(packs),
                "pack_tokens_median": med_pack,
                "pack_tokens_min": min(packs), "pack_tokens_max": max(packs),
                "savings_pct": round(savings * 100, 1),
                "recall_pct": med_recall,
            }
            rows.append(row)
            print(f"  baseline={baseline:,} tok ({nfiles} files) | "
                  f"pack~{med_pack:,} tok | savings={row['savings_pct']}% | recall={med_recall}%")
        except subprocess.CalledProcessError as exc:
            print(f"  SKIP (clone failed): {exc}")
        except Exception as exc:
            print(f"  SKIP (error): {exc}")

    if not rows:
        print("\nNo results.")
        return 1

    # ---- aggregate ----
    all_sav = [r["savings_pct"] for r in rows]
    all_pack = [r["pack_tokens_median"] for r in rows]
    all_recall = [r["recall_pct"] for r in rows]
    summary = {
        "repos_measured": len(rows),
        "overall_median_savings_pct": round(statistics.median(all_sav), 1),
        "overall_mean_savings_pct": round(statistics.mean(all_sav), 1),
        "overall_median_recall_pct": round(statistics.median(all_recall), 1),
        "median_pack_tokens": int(statistics.median(all_pack)),
        "min_pack_tokens": min(all_pack), "max_pack_tokens": max(all_pack),
        "by_lang": {}, "by_tier": {},
    }
    for key, field in (("by_lang", "lang"), ("by_tier", "tier")):
        for val in sorted({r[field] for r in rows}):
            grp = [r for r in rows if r[field] == val]
            summary[key][val] = {
                "n": len(grp),
                "median_savings_pct": round(statistics.median([r["savings_pct"] for r in grp]), 1),
                "median_pack_tokens": int(statistics.median([r["pack_tokens_median"] for r in grp])),
                "median_recall_pct": round(statistics.median([r["recall_pct"] for r in grp]), 1),
            }

    out = {"summary": summary, "rows": rows, "rlm_url": RLM_URL,
           "generated_by": "bench/run_bench.py"}
    (RESULTS / "bench_results.json").write_text(json.dumps(out, indent=2))

    print("\n" + "=" * 64)
    print(f"CC-RLM benchmark — {summary['repos_measured']} repos")
    print(f"  Median savings:   {summary['overall_median_savings_pct']}%")
    print(f"  Median recall:    {summary['overall_median_recall_pct']}%")
    print(f"  Median pack size: {summary['median_pack_tokens']:,} tokens "
          f"({summary['min_pack_tokens']:,}–{summary['max_pack_tokens']:,})")
    print(f"  By language:      {summary['by_lang']}")
    print(f"  By size tier:     {summary['by_tier']}")
    print(f"\nSaved: {RESULTS / 'bench_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
