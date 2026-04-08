#!/usr/bin/env python3
"""
Visualize effort level analysis from dashboard.jsonl.

Produces a 2x2 grid of charts:
  1. Box plot — read:write ratio distribution by effort level
  2. Bar chart — avg score by effort level
  3. Heatmap — avg ratio per (effort, scenario)
  4. Intra-session decay — avg ratio by session third, per effort level

Run: python3 visualize.py [--output effort_analysis.png]
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np

DASHBOARD_PATH = Path(__file__).parent / "dashboard.jsonl"
EFFORT_COLORS = {"low": "#e05c5c", "medium": "#e0a83a", "high": "#4caf7d"}
EFFORT_ORDER  = ["low", "medium", "high"]


def load_dashboard() -> list[dict]:
    entries = []
    if not DASHBOARD_PATH.exists():
        raise FileNotFoundError(f"Not found: {DASHBOARD_PATH}")
    with open(DASHBOARD_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def load_decay_data() -> dict[str, list[list[float]]]:
    """
    Returns {effort_level: [[bucket1_ratios], [bucket2_ratios], [bucket3_ratios]]}.
    Reads from watchdog.db if available to compute intra-session buckets.
    """
    import sqlite3
    db_path = Path(__file__).parent / "watchdog.db"
    if not db_path.exists():
        return {}

    READ_TOOLS  = {"Read", "Glob", "Grep"}
    WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

    # Map session_id → effort_level from dashboard
    effort_map: dict[str, str] = {}
    for e in load_dashboard():
        effort_map[e["session_id"]] = e.get("effort_level", "unknown")

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute("SELECT DISTINCT session_id FROM tool_calls")
    session_ids = [r[0] for r in cur.fetchall()]

    decay: dict[str, list[list[float]]] = {e: [[], [], []] for e in EFFORT_ORDER}

    for sid in session_ids:
        effort = effort_map.get(sid)
        if effort not in EFFORT_ORDER:
            continue
        cur = conn.execute(
            "SELECT tool_name, ts FROM tool_calls WHERE session_id=? ORDER BY ts", (sid,)
        )
        rows = cur.fetchall()
        if len(rows) < 6:
            continue
        min_ts, max_ts = rows[0][1], rows[-1][1]
        span = max_ts - min_ts
        if span < 1:
            continue
        bsize = span / 3
        for b in range(3):
            lo = min_ts + b * bsize
            hi = min_ts + (b + 1) * bsize
            chunk = [r for r in rows if lo <= r[1] < hi]
            reads  = sum(1 for r in chunk if r[0] in READ_TOOLS)
            writes = sum(1 for r in chunk if r[0] in WRITE_TOOLS)
            if writes > 0:
                decay[effort][b].append(reads / writes)

    conn.close()
    return decay


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="effort_analysis.png")
    args = parser.parse_args()

    entries = load_dashboard()
    print(f"Loaded {len(entries)} dashboard entries.")

    # ── aggregate ──────────────────────────────────────────────────────────────
    by_effort: dict[str, list[dict]] = defaultdict(list)
    by_scenario: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for e in entries:
        effort = e.get("effort_level", "unknown")
        sc     = e.get("scenario", "unknown")
        ratio  = e.get("ratio", 0.0)
        score  = e.get("score", 0)
        by_effort[effort].append(e)
        by_scenario[sc][effort].append(ratio)

    # ── figure setup ───────────────────────────────────────────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle(
        f"Effort Level Analysis  ·  {len(entries)} sessions  ·  "
        f"{len(by_scenario)} scenarios",
        fontsize=14, fontweight="bold", y=0.98,
    )
    fig.patch.set_facecolor("#1a1a2e")
    for ax in axes.flat:
        ax.set_facecolor("#16213e")
        ax.tick_params(colors="#cccccc")
        ax.xaxis.label.set_color("#cccccc")
        ax.yaxis.label.set_color("#cccccc")
        ax.title.set_color("#eeeeee")
        for spine in ax.spines.values():
            spine.set_color("#444466")

    # ── 1. Box plot: ratio distribution ───────────────────────────────────────
    ax1 = axes[0, 0]
    data = [
        [e["ratio"] for e in by_effort.get(ef, [])]
        for ef in EFFORT_ORDER
    ]
    bp = ax1.boxplot(
        data, labels=EFFORT_ORDER, patch_artist=True,
        medianprops=dict(color="white", linewidth=2),
        whiskerprops=dict(color="#aaaacc"),
        capprops=dict(color="#aaaacc"),
        flierprops=dict(markerfacecolor="#888899", markersize=4),
    )
    for patch, ef in zip(bp["boxes"], EFFORT_ORDER):
        patch.set_facecolor(EFFORT_COLORS[ef])
        patch.set_alpha(0.75)

    ax1.axhline(3.0, color="#ffcc44", linestyle="--", linewidth=1, label="3:1 threshold")
    ax1.set_title("Read:Write Ratio Distribution", fontsize=11)
    ax1.set_ylabel("Ratio")
    ax1.legend(facecolor="#222244", labelcolor="#eeeeee", fontsize=8)

    for ef, d in zip(EFFORT_ORDER, data):
        if d:
            ax1.text(
                EFFORT_ORDER.index(ef) + 1,
                max(d) + 0.2,
                f"n={len(d)}\nμ={sum(d)/len(d):.1f}",
                ha="center", va="bottom", fontsize=7.5, color="#cccccc",
            )

    # ── 2. Bar + error bars: avg score by effort ───────────────────────────────
    ax2 = axes[0, 1]
    for i, ef in enumerate(EFFORT_ORDER):
        group = by_effort.get(ef, [])
        scores = [e["score"] for e in group if isinstance(e.get("score"), (int, float))]
        ratios = [e["ratio"] for e in group]
        if not scores:
            continue
        avg_s = sum(scores) / len(scores)
        std_s = np.std(scores) if len(scores) > 1 else 0
        avg_r = sum(ratios) / len(ratios) if ratios else 0
        bar = ax2.bar(i, avg_s, color=EFFORT_COLORS[ef], alpha=0.8,
                      yerr=std_s, capsize=5,
                      error_kw=dict(ecolor="#aaaaaa", elinewidth=1.5))
        ax2.text(i, avg_s + std_s + 0.15, f"μ={avg_s:.1f}\n±{std_s:.1f}", ha="center",
                 fontsize=8, color="#cccccc")
        ax2.text(i, 0.2, f"ratio\n{avg_r:.1f}:1", ha="center", fontsize=7.5, color="#eeeeee")

    ax2.set_xticks(range(len(EFFORT_ORDER)))
    ax2.set_xticklabels(EFFORT_ORDER)
    ax2.set_ylim(0, 12)
    ax2.set_title("Avg Session Score (1–10) with Std Dev", fontsize=11)
    ax2.set_ylabel("Score")
    ax2.axhline(7, color="#ffcc44", linestyle="--", linewidth=1, alpha=0.6, label="score=7")
    ax2.legend(facecolor="#222244", labelcolor="#eeeeee", fontsize=8)

    # ── 3. Heatmap: avg ratio per (scenario × effort) ─────────────────────────
    ax3 = axes[1, 0]
    scenarios_sorted = sorted(by_scenario.keys())
    matrix = np.zeros((len(scenarios_sorted), len(EFFORT_ORDER)))
    for ri, sc in enumerate(scenarios_sorted):
        for ci, ef in enumerate(EFFORT_ORDER):
            vals = by_scenario[sc].get(ef, [])
            matrix[ri, ci] = sum(vals) / len(vals) if vals else 0.0

    im = ax3.imshow(matrix, aspect="auto", cmap="RdYlGn", vmin=0, vmax=9)
    ax3.set_xticks(range(len(EFFORT_ORDER)))
    ax3.set_xticklabels(EFFORT_ORDER, fontsize=9)
    ax3.set_yticks(range(len(scenarios_sorted)))
    ax3.set_yticklabels(
        [s.replace("-", " ") for s in scenarios_sorted], fontsize=7
    )
    ax3.set_title("Avg Ratio per Scenario × Effort Level", fontsize=11)
    cbar = fig.colorbar(im, ax=ax3, fraction=0.046, pad=0.04)
    cbar.set_label("Avg Ratio", color="#cccccc")
    cbar.ax.yaxis.set_tick_params(color="#cccccc")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="#cccccc")

    for ri in range(len(scenarios_sorted)):
        for ci in range(len(EFFORT_ORDER)):
            val = matrix[ri, ci]
            if val > 0:
                ax3.text(ci, ri, f"{val:.1f}", ha="center", va="center",
                         fontsize=6.5, color="black" if val > 4 else "white")

    # ── 4. Intra-session decay ─────────────────────────────────────────────────
    ax4 = axes[1, 1]
    decay = load_decay_data()
    bucket_labels = ["Early\n(first third)", "Mid\n(middle third)", "Late\n(final third)"]
    has_decay = False
    for ef in EFFORT_ORDER:
        buckets = decay.get(ef, [[], [], []])
        avgs = [sum(b) / len(b) if b else None for b in buckets]
        if any(a is not None for a in avgs):
            has_decay = True
            xs = [i for i, a in enumerate(avgs) if a is not None]
            ys = [a for a in avgs if a is not None]
            ax4.plot(xs, ys, "o-", color=EFFORT_COLORS[ef], label=ef,
                     linewidth=2, markersize=7)
            for x, y in zip(xs, ys):
                ax4.text(x, y + 0.1, f"{y:.1f}", ha="center", fontsize=8,
                         color=EFFORT_COLORS[ef])

    if has_decay:
        ax4.axhline(3.0, color="#ffcc44", linestyle="--", linewidth=1, label="3:1 threshold")
        ax4.set_xticks(range(3))
        ax4.set_xticklabels(bucket_labels, fontsize=9)
        ax4.set_title("Intra-Session Ratio Decay (avg per third)", fontsize=11)
        ax4.set_ylabel("Avg Ratio")
        ax4.legend(facecolor="#222244", labelcolor="#eeeeee", fontsize=9)
    else:
        ax4.text(0.5, 0.5, "Insufficient data\nfor decay analysis\n(need sessions with ≥6 tool calls)",
                 ha="center", va="center", transform=ax4.transAxes,
                 color="#888899", fontsize=10)
        ax4.set_title("Intra-Session Ratio Decay", fontsize=11)

    # ── save ───────────────────────────────────────────────────────────────────
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    out = Path(__file__).parent / args.output
    plt.savefig(str(out), dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"Saved: {out}")
    plt.close()


if __name__ == "__main__":
    main()
