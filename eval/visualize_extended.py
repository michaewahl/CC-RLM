#!/usr/bin/env python3
"""
Extended visualizations for effort level analysis.

Produces two additional figures:

Figure 1 — deep_dive.png  (3×2 grid)
  1. Scatter: ratio vs score, colored by effort (correlation)
  2. Violin: ratio distributions (richer than box plot)
  3. Horizontal bar: per-scenario delta (high minus low avg ratio)
  4. Histogram: ratio frequency by effort level (overlapping)
  5. Cumulative distribution: % sessions above each ratio threshold
  6. Score vs ratio regression lines per effort level

Figure 2 — consistency.png  (2×2 grid)
  1. Variance (std dev) of ratio per effort level — how predictable?
  2. % sessions clearing 3:1 threshold per effort level
  3. Scatter: score vs ratio per session (all effort levels)
  4. Stacked bar: sessions by score band (1-4 / 5-6 / 7-8 / 9-10) per effort

Run: python3 visualize_extended.py
"""

import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from scipy import stats

DASHBOARD_PATH = Path(__file__).parent / "dashboard.jsonl"
EFFORT_COLORS  = {"low": "#e05c5c", "medium": "#e0a83a", "high": "#4caf7d"}
EFFORT_ORDER   = ["low", "medium", "high"]
DARK_BG        = "#1a1a2e"
PANEL_BG       = "#16213e"
TICK_COLOR     = "#cccccc"
LABEL_COLOR    = "#cccccc"
TITLE_COLOR    = "#eeeeee"
SPINE_COLOR    = "#444466"


def load() -> list[dict]:
    entries = []
    with open(DASHBOARD_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def style_ax(ax):
    ax.set_facecolor(PANEL_BG)
    ax.tick_params(colors=TICK_COLOR)
    ax.xaxis.label.set_color(LABEL_COLOR)
    ax.yaxis.label.set_color(LABEL_COLOR)
    ax.title.set_color(TITLE_COLOR)
    for spine in ax.spines.values():
        spine.set_color(SPINE_COLOR)


def style_fig(fig):
    fig.patch.set_facecolor(DARK_BG)


def legend(ax, **kw):
    leg = ax.legend(facecolor="#222244", labelcolor="#eeeeee", **kw)
    return leg


# ══════════════════════════════════════════════════════════════════════════════
# Figure 1 — deep_dive.png
# ══════════════════════════════════════════════════════════════════════════════

def make_deep_dive(entries: list[dict]) -> None:
    by_effort: dict[str, list[dict]] = defaultdict(list)
    by_scenario: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for e in entries:
        ef = e.get("effort_level", "unknown")
        sc = e.get("scenario", "unknown")
        by_effort[ef].append(e)
        by_scenario[sc][ef].append(e.get("ratio", 0.0))

    fig, axes = plt.subplots(3, 2, figsize=(16, 18))
    style_fig(fig)
    fig.suptitle("Effort Level — Deep Dive", fontsize=14, fontweight="bold",
                 color=TITLE_COLOR, y=0.99)

    for ax in axes.flat:
        style_ax(ax)

    # ── 1. Scatter: ratio vs score ─────────────────────────────────────────
    ax = axes[0, 0]
    for ef in EFFORT_ORDER:
        group = by_effort[ef]
        xs = [e["ratio"] for e in group]
        ys = [e.get("score", 0) for e in group if isinstance(e.get("score"), (int, float))]
        xs_valid = [e["ratio"] for e in group if isinstance(e.get("score"), (int, float))]
        ax.scatter(xs_valid, ys, color=EFFORT_COLORS[ef], alpha=0.55, s=28, label=ef)
        if len(xs_valid) > 1:
            m, b, r, p, _ = stats.linregress(xs_valid, ys)
            xl = np.linspace(min(xs_valid), max(xs_valid), 50)
            ax.plot(xl, m * xl + b, color=EFFORT_COLORS[ef], linewidth=1.5,
                    linestyle="--", alpha=0.8)
            ax.text(max(xs_valid) - 0.3, m * max(xs_valid) + b + 0.1,
                    f"r={r:.2f}", fontsize=7.5, color=EFFORT_COLORS[ef])
    ax.axvline(3.0, color="#ffcc44", linestyle=":", linewidth=1, alpha=0.7)
    ax.set_xlabel("Read:Write Ratio")
    ax.set_ylabel("Score (1–10)")
    ax.set_title("Ratio vs Score (with regression lines)")
    legend(ax, fontsize=8)

    # ── 2. Violin plot ─────────────────────────────────────────────────────
    ax = axes[0, 1]
    data = [[e["ratio"] for e in by_effort.get(ef, [])] for ef in EFFORT_ORDER]
    parts = ax.violinplot(data, positions=range(len(EFFORT_ORDER)),
                          showmedians=True, showextrema=True)
    for i, (body, ef) in enumerate(zip(parts["bodies"], EFFORT_ORDER)):
        body.set_facecolor(EFFORT_COLORS[ef])
        body.set_alpha(0.7)
    parts["cmedians"].set_color("white")
    parts["cmins"].set_color(SPINE_COLOR)
    parts["cmaxes"].set_color(SPINE_COLOR)
    parts["cbars"].set_color(SPINE_COLOR)
    ax.axhline(3.0, color="#ffcc44", linestyle="--", linewidth=1, label="3:1 threshold")
    ax.set_xticks(range(len(EFFORT_ORDER)))
    ax.set_xticklabels(EFFORT_ORDER)
    ax.set_ylabel("Read:Write Ratio")
    ax.set_title("Ratio Violin Plot (distribution shape)")
    legend(ax, fontsize=8)

    # ── 3. Horizontal bar: per-scenario delta (high − low) ─────────────────
    ax = axes[1, 0]
    deltas = []
    for sc in sorted(by_scenario):
        lo_vals  = by_scenario[sc].get("low",  [])
        hi_vals  = by_scenario[sc].get("high", [])
        lo_avg = sum(lo_vals) / len(lo_vals) if lo_vals else 0
        hi_avg = sum(hi_vals) / len(hi_vals) if hi_vals else 0
        deltas.append((sc, hi_avg - lo_avg, hi_avg, lo_avg))

    deltas.sort(key=lambda x: x[1], reverse=True)
    names  = [d[0].replace("-", " ") for d in deltas]
    values = [d[1] for d in deltas]
    colors = [plt.cm.RdYlGn(v / max(values)) for v in values]

    bars = ax.barh(range(len(names)), values, color=colors, alpha=0.85)
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names, fontsize=7)
    ax.set_xlabel("Δ Ratio (high − low)")
    ax.set_title("Per-Scenario: Effort Impact (high minus low avg ratio)")
    ax.axvline(0, color=SPINE_COLOR, linewidth=0.8)
    for i, (bar, d) in enumerate(zip(bars, deltas)):
        ax.text(d[1] + 0.05, i, f"+{d[1]:.1f}", va="center", fontsize=6.5, color="#dddddd")

    # ── 4. Overlapping histogram ───────────────────────────────────────────
    ax = axes[1, 1]
    bins = np.linspace(0, 12, 30)
    for ef in EFFORT_ORDER:
        ratios = [e["ratio"] for e in by_effort[ef]]
        ax.hist(ratios, bins=bins, color=EFFORT_COLORS[ef], alpha=0.45,
                label=ef, density=True)
    ax.axvline(3.0, color="#ffcc44", linestyle="--", linewidth=1.2, label="3:1 threshold")
    ax.set_xlabel("Read:Write Ratio")
    ax.set_ylabel("Density")
    ax.set_title("Ratio Frequency Distribution (overlapping histogram)")
    legend(ax, fontsize=8)

    # ── 5. Cumulative distribution (% sessions above threshold) ───────────
    ax = axes[2, 0]
    thresholds = np.linspace(0, 12, 200)
    for ef in EFFORT_ORDER:
        ratios = np.array([e["ratio"] for e in by_effort[ef]])
        pct_above = [np.mean(ratios >= t) * 100 for t in thresholds]
        ax.plot(thresholds, pct_above, color=EFFORT_COLORS[ef], linewidth=2, label=ef)

    ax.axvline(3.0, color="#ffcc44", linestyle="--", linewidth=1, label="3:1 threshold")
    for ef in EFFORT_ORDER:
        ratios = np.array([e["ratio"] for e in by_effort[ef]])
        pct = np.mean(ratios >= 3.0) * 100
        ax.annotate(f"{pct:.0f}%", xy=(3.0, pct),
                    xytext=(3.5, pct + 3),
                    color=EFFORT_COLORS[ef], fontsize=8,
                    arrowprops=dict(arrowstyle="->", color=EFFORT_COLORS[ef], lw=0.8))

    ax.set_xlabel("Ratio Threshold")
    ax.set_ylabel("% Sessions Above Threshold")
    ax.set_title("Cumulative: % Sessions Clearing Each Ratio Threshold")
    ax.set_ylim(0, 105)
    legend(ax, fontsize=8)

    # ── 6. Regression: score ~ ratio per effort ────────────────────────────
    ax = axes[2, 1]
    all_ratios = [e["ratio"] for e in entries]
    xl = np.linspace(0, max(all_ratios) + 0.5, 100)

    for ef in EFFORT_ORDER:
        group = [e for e in by_effort[ef] if isinstance(e.get("score"), (int, float))]
        xs = [e["ratio"] for e in group]
        ys = [e["score"] for e in group]
        if len(xs) < 2:
            continue
        m, b, r, p, se = stats.linregress(xs, ys)
        ax.fill_between(xl, (m * xl + b) - se * 2, (m * xl + b) + se * 2,
                        color=EFFORT_COLORS[ef], alpha=0.15)
        ax.plot(xl, m * xl + b, color=EFFORT_COLORS[ef], linewidth=2.5, label=ef)
        ax.text(xl[-1], m * xl[-1] + b, f"  {ef}\n  r={r:.2f}, p={p:.3f}",
                fontsize=7.5, color=EFFORT_COLORS[ef], va="center")

    ax.axvline(3.0, color="#ffcc44", linestyle=":", linewidth=1)
    ax.set_xlabel("Read:Write Ratio")
    ax.set_ylabel("Score")
    ax.set_title("Score ~ Ratio Regression (with 2σ confidence band)")
    ax.set_ylim(0, 12)
    legend(ax, fontsize=8)

    plt.tight_layout(rect=[0, 0, 1, 0.98])
    out = Path(__file__).parent / "deep_dive.png"
    plt.savefig(str(out), dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"Saved: {out}")
    plt.close()


# ══════════════════════════════════════════════════════════════════════════════
# Figure 2 — consistency.png
# ══════════════════════════════════════════════════════════════════════════════

def make_consistency(entries: list[dict]) -> None:
    by_effort: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_effort[e.get("effort_level", "unknown")].append(e)

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    style_fig(fig)
    fig.suptitle("Effort Level — Consistency & Reliability", fontsize=14,
                 fontweight="bold", color=TITLE_COLOR, y=0.99)
    for ax in axes.flat:
        style_ax(ax)

    # ── 1. Std dev of ratio per effort ─────────────────────────────────────
    ax = axes[0, 0]
    for i, ef in enumerate(EFFORT_ORDER):
        ratios = [e["ratio"] for e in by_effort[ef]]
        mean_r = np.mean(ratios)
        std_r  = np.std(ratios)
        cv     = std_r / mean_r * 100  # coefficient of variation
        bar = ax.bar(i, std_r, color=EFFORT_COLORS[ef], alpha=0.8)
        ax.text(i, std_r + 0.02, f"σ={std_r:.2f}\nCV={cv:.0f}%",
                ha="center", fontsize=8.5, color="#dddddd")
    ax.set_xticks(range(len(EFFORT_ORDER)))
    ax.set_xticklabels(EFFORT_ORDER)
    ax.set_ylabel("Std Dev of Ratio")
    ax.set_title("Ratio Variability (σ) — how predictable is each effort level?")

    # ── 2. % sessions clearing 3:1 threshold ──────────────────────────────
    ax = axes[0, 1]
    thresholds = [1.5, 2.0, 3.0, 4.0, 5.0, 6.0]
    x = np.arange(len(thresholds))
    width = 0.25
    for i, ef in enumerate(EFFORT_ORDER):
        ratios = np.array([e["ratio"] for e in by_effort[ef]])
        pcts = [np.mean(ratios >= t) * 100 for t in thresholds]
        bars = ax.bar(x + i * width, pcts, width, color=EFFORT_COLORS[ef],
                      alpha=0.8, label=ef)
        for bar, pct in zip(bars, pcts):
            if pct > 5:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                        f"{pct:.0f}", ha="center", va="bottom", fontsize=6.5,
                        color="#cccccc")
    ax.set_xticks(x + width)
    ax.set_xticklabels([f"≥{t}" for t in thresholds], fontsize=8)
    ax.set_ylabel("% of Sessions")
    ax.set_xlabel("Ratio Threshold")
    ax.set_title("% Sessions Clearing Each Ratio Threshold")
    ax.set_ylim(0, 110)
    legend(ax, fontsize=8)

    # ── 3. Scatter: all sessions, score vs ratio ───────────────────────────
    ax = axes[1, 0]
    for ef in EFFORT_ORDER:
        group = [e for e in by_effort[ef] if isinstance(e.get("score"), (int, float))]
        xs = [e["ratio"] for e in group]
        ys = [e["score"] for e in group]
        ax.scatter(xs, ys, color=EFFORT_COLORS[ef], alpha=0.45, s=22, label=ef)

    # overall regression
    all_valid = [e for e in entries if isinstance(e.get("score"), (int, float))]
    xs_all = np.array([e["ratio"] for e in all_valid])
    ys_all = np.array([e["score"] for e in all_valid])
    m, b, r, p, _ = stats.linregress(xs_all, ys_all)
    xl = np.linspace(xs_all.min(), xs_all.max(), 100)
    ax.plot(xl, m * xl + b, color="white", linewidth=1.5, linestyle="--",
            label=f"overall r={r:.2f}")
    ax.axvline(3.0, color="#ffcc44", linestyle=":", linewidth=1, alpha=0.7)
    ax.set_xlabel("Read:Write Ratio")
    ax.set_ylabel("Score")
    ax.set_title(f"All Sessions: Score vs Ratio  (overall r={r:.2f}, p={p:.2e})")
    legend(ax, fontsize=7.5)

    # ── 4. Stacked bar: sessions by score band ─────────────────────────────
    ax = axes[1, 1]
    bands = {"1–4 (poor)": (1, 4), "5–6 (ok)": (5, 6), "7–8 (good)": (7, 8), "9–10 (excellent)": (9, 10)}
    band_colors = ["#e05c5c", "#e0a83a", "#4caf7d", "#56c8e8"]
    bottoms = np.zeros(len(EFFORT_ORDER))

    for (label, (lo, hi)), col in zip(bands.items(), band_colors):
        counts = []
        for ef in EFFORT_ORDER:
            group = by_effort[ef]
            n = sum(1 for e in group
                    if isinstance(e.get("score"), (int, float)) and lo <= e["score"] <= hi)
            pct = n / len(group) * 100 if group else 0
            counts.append(pct)
        bars = ax.bar(range(len(EFFORT_ORDER)), counts, bottom=bottoms,
                      color=col, alpha=0.85, label=label)
        for i, (bar, cnt) in enumerate(zip(bars, counts)):
            if cnt > 4:
                ax.text(i, bottoms[i] + cnt / 2, f"{cnt:.0f}%",
                        ha="center", va="center", fontsize=8, color="black", fontweight="bold")
        bottoms += np.array(counts)

    ax.set_xticks(range(len(EFFORT_ORDER)))
    ax.set_xticklabels(EFFORT_ORDER)
    ax.set_ylabel("% of Sessions")
    ax.set_ylim(0, 110)
    ax.set_title("Session Quality Distribution by Score Band")
    legend(ax, fontsize=8, loc="upper left")

    plt.tight_layout(rect=[0, 0, 1, 0.98])
    out = Path(__file__).parent / "consistency.png"
    plt.savefig(str(out), dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"Saved: {out}")
    plt.close()


if __name__ == "__main__":
    entries = load()
    print(f"Loaded {len(entries)} entries.")
    make_deep_dive(entries)
    make_consistency(entries)
    print("Done.")
