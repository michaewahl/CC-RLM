#!/usr/bin/env python3
"""
Chart the CC-RLM benchmark results.
Requires matplotlib:  poetry run pip install matplotlib
Reads bench/results/bench_results.json, writes bench/results/cc_rlm_benchmark.png
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

HERE = Path(__file__).parent
DATA = HERE / "results" / "bench_results.json"
OUT = HERE / "results" / "cc_rlm_benchmark.png"

d = json.loads(DATA.read_text())
rows = d["rows"]
s = d["summary"]

PY = "#3b82f6"   # python
TS = "#f59e0b"   # ts
def color(lang): return PY if lang == "python" else TS

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
fig.suptitle(
    f"CC-RLM benchmark — {s['repos_measured']} real repos (Python + TypeScript)\n"
    f"median {s['overall_median_savings_pct']}% token savings · "
    f"pack median {s['median_pack_tokens']:,} tok · "
    f"co-change recall {s['overall_median_recall_pct']}%",
    fontsize=14, fontweight="bold",
)

# --- Panel 1: the money chart — pack size stays bounded as repos scale ---
for r in rows:
    ax1.scatter(r["baseline_tokens"], r["pack_tokens_median"],
                c=color(r["lang"]), s=70, alpha=0.8, edgecolors="white", linewidths=0.5)
ax1.axhline(8000, ls="--", c="#ef4444", lw=1.2)
ax1.text(ax1.get_xlim()[1], 8200, "8K token budget", color="#ef4444",
         ha="right", va="bottom", fontsize=9)
ax1.set_xscale("log")
ax1.set_xlabel("Repo size — naive full-repo dump (tokens, log scale)")
ax1.set_ylabel("CC-RLM context pack (tokens)")
ax1.set_title("The pack stays tiny no matter how big the repo gets", fontsize=11)
ax1.xaxis.set_major_formatter(FuncFormatter(
    lambda x, _: f"{x/1e6:.0f}M" if x >= 1e6 else (f"{x/1e3:.0f}K" if x >= 1e3 else f"{x:.0f}")))
ax1.grid(True, alpha=0.25)
ax1.scatter([], [], c=PY, label="Python", s=70)
ax1.scatter([], [], c=TS, label="TypeScript", s=70)
ax1.legend(loc="center right")

# --- Panel 2: honest recall — bimodal, sorted ---
rs = sorted(rows, key=lambda r: r["recall_pct"])
names = [r["repo"].split("/")[-1] for r in rs]
ax2.barh(range(len(rs)), [r["recall_pct"] for r in rs],
         color=[color(r["lang"]) for r in rs], alpha=0.85)
ax2.axvline(s["overall_median_recall_pct"], ls="--", c="#111", lw=1)
ax2.text(s["overall_median_recall_pct"] + 1, 0.3,
         f"median {s['overall_median_recall_pct']}%", fontsize=9)
ax2.set_yticks(range(len(rs)))
ax2.set_yticklabels(names, fontsize=7)
ax2.set_xlabel("Co-change recall (% of a commit's changed files surfaced)")
ax2.set_title("Recall is honest: near-perfect on tight repos, weaker on sprawling ones", fontsize=11)
ax2.set_xlim(0, 105)
ax2.grid(True, axis="x", alpha=0.25)

fig.tight_layout(rect=[0, 0, 1, 0.93])
fig.savefig(OUT, dpi=150, bbox_inches="tight")
print(f"wrote {OUT}")
