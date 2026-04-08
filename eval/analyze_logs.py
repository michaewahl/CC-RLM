#!/usr/bin/env python3
"""
Standalone log analyzer — establishes pre/post March 8 baseline for Claude's
read:write ratio using local Claude Code data sources.

Run: python3 analyze_logs.py

Sources (in priority order):
  1. watchdog.db          — live data from this watchdog system (if it exists)
  2. ~/.claude/debug/*.txt — session traces with tool call log lines
  3. ~/.claude/history.jsonl — session metadata (timestamps, project, model)
  4. ~/.claude/stats-cache.json — daily activity summaries
"""

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
DB_PATH = Path(__file__).parent / "watchdog.db"
REGRESSION_DATE = datetime(2026, 3, 8, tzinfo=timezone.utc)

READ_TOOLS = {"Read", "Glob", "Grep"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Patterns seen in debug log files
TOOL_LINE_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\].*?(?:Tool|tool)[:\s]+(?P<tool>\w+)[:\s]+(?P<path>[^\s,\]]*)"
)
# Simpler fallback
TOOL_NAME_RE = re.compile(r"\b(Read|Glob|Grep|Write|Edit|MultiEdit|Bash)\b")


# ─── helpers ──────────────────────────────────────────────────────────────────

def ratio_str(reads: int, writes: int) -> str:
    if writes == 0:
        return f"{reads}:0 (no edits)"
    return f"{reads / writes:.1f}:1"


def flag(reads: int, writes: int, threshold: float = 3.0) -> str:
    if writes == 0:
        return ""
    return "⚠" if reads / writes < threshold else "✓"


# ─── source 1: watchdog.db ────────────────────────────────────────────────────

def load_watchdog_db() -> list[dict]:
    """Returns list of {session_id, reads, writes, first_ts}."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute("""
            SELECT
                session_id,
                SUM(CASE WHEN tool_name IN ('Read','Glob','Grep') THEN 1 ELSE 0 END) as reads,
                SUM(CASE WHEN tool_name IN ('Write','Edit','MultiEdit') THEN 1 ELSE 0 END) as writes,
                MIN(ts) as first_ts
            FROM tool_calls
            GROUP BY session_id
            ORDER BY first_ts DESC
        """)
        rows = [
            {
                "session_id": r[0],
                "reads": r[1],
                "writes": r[2],
                "first_ts": r[3],
                "source": "watchdog.db",
            }
            for r in cur.fetchall()
        ]
        conn.close()
        return rows
    except Exception as e:
        print(f"  [watchdog.db error: {e}]")
        return []


def load_session_decay(session_id: str, buckets: int = 3) -> list[dict] | None:
    """
    Split a session's tool calls into time buckets and return per-bucket read:write ratios.
    Returns None if insufficient data (< 6 tool calls or no writes).
    """
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute(
            "SELECT tool_name, ts FROM tool_calls WHERE session_id = ? ORDER BY ts",
            (session_id,),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return None

    if len(rows) < 6:
        return None

    min_ts = rows[0][1]
    max_ts = rows[-1][1]
    span = max_ts - min_ts
    if span < 1:
        return None

    bucket_size = span / buckets
    result = []
    for b in range(buckets):
        lo = min_ts + b * bucket_size
        hi = min_ts + (b + 1) * bucket_size
        chunk = [r for r in rows if lo <= r[1] < hi]
        reads = sum(1 for r in chunk if r[0] in READ_TOOLS)
        writes = sum(1 for r in chunk if r[0] in WRITE_TOOLS)
        result.append({"bucket": b + 1, "reads": reads, "writes": writes})

    # Only return if there are writes somewhere (otherwise decay is meaningless)
    if not any(b["writes"] > 0 for b in result):
        return None
    return result


# ─── source 2: debug files ────────────────────────────────────────────────────

def load_debug_files() -> list[dict]:
    debug_dir = CLAUDE_DIR / "debug"
    if not debug_dir.exists():
        return []

    sessions = []
    for path in sorted(debug_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
        reads = writes = 0
        try:
            text = path.read_text(errors="replace")
            for match in TOOL_NAME_RE.finditer(text):
                t = match.group(1)
                if t in READ_TOOLS:
                    reads += 1
                elif t in WRITE_TOOLS:
                    writes += 1
        except Exception:
            continue
        if reads + writes == 0:
            continue
        sessions.append({
            "session_id": path.stem[:16],
            "reads": reads,
            "writes": writes,
            "first_ts": path.stat().st_mtime,
            "source": "debug/",
        })
    return sessions


# ─── source 3: history.jsonl ─────────────────────────────────────────────────

def load_history() -> dict[str, dict]:
    """Returns {sessionId: {ts, project}} map."""
    history_path = CLAUDE_DIR / "history.jsonl"
    if not history_path.exists():
        return {}
    sessions: dict[str, dict] = {}
    try:
        with open(history_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                sid = entry.get("sessionId") or entry.get("session_id")
                if not sid:
                    continue
                if sid not in sessions:
                    sessions[sid] = {
                        "ts": entry.get("timestamp"),
                        "project": entry.get("cwd") or entry.get("projectPath") or "",
                    }
    except Exception:
        pass
    return sessions


# ─── source 4: stats-cache.json ──────────────────────────────────────────────

def load_stats_cache() -> list[dict]:
    stats_path = CLAUDE_DIR / "stats-cache.json"
    if not stats_path.exists():
        return []
    try:
        data = json.loads(stats_path.read_text())
        daily = data.get("dailyActivity") or data.get("daily_activity") or {}
        rows = []
        for date_str, info in daily.items():
            if isinstance(info, dict):
                rows.append({
                    "date": date_str,
                    "messages": info.get("messageCount") or info.get("messages") or 0,
                    "tool_calls": info.get("toolCallCount") or info.get("tool_calls") or 0,
                    "model": info.get("model") or "",
                })
        return sorted(rows, key=lambda r: r["date"])
    except Exception:
        return []


# ─── report ───────────────────────────────────────────────────────────────────

def print_section(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print('═' * 60)


def main() -> None:
    print("Claude Code Effort Baseline Report")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Regression date: {REGRESSION_DATE.strftime('%Y-%m-%d')} (March 8 2026)")

    # ── stats cache daily summary ──────────────────────────────────────────
    print_section("Daily Activity (stats-cache.json)")
    stats = load_stats_cache()
    if stats:
        pre = [r for r in stats if r["date"] < "2026-03-08"]
        post = [r for r in stats if r["date"] >= "2026-03-08"]

        def avg_ratio(rows: list[dict]) -> str:
            totals = [r["tool_calls"] / max(r["messages"], 1) for r in rows if r["messages"]]
            return f"{sum(totals)/len(totals):.2f}" if totals else "n/a"

        print(f"  Pre-March-8  ({len(pre)} days): avg tool_calls/message = {avg_ratio(pre)}")
        print(f"  Post-March-8 ({len(post)} days): avg tool_calls/message = {avg_ratio(post)}")
        print()
        print(f"  {'Date':<12} {'Messages':>9} {'Tool calls':>11} {'Ratio':>7}  {'Model'}")
        print(f"  {'-'*12} {'-'*9} {'-'*11} {'-'*7}  {'-'*20}")
        for r in stats[-30:]:  # last 30 days
            ratio = r["tool_calls"] / max(r["messages"], 1)
            marker = "⚠" if ratio < 2.0 and r["messages"] > 0 else ""
            print(f"  {r['date']:<12} {r['messages']:>9} {r['tool_calls']:>11} {ratio:>7.2f}  {r['model'][:20]}{marker}")
    else:
        print("  [No data found]")

    # ── watchdog.db ────────────────────────────────────────────────────────
    watchdog_rows = load_watchdog_db()
    print_section(f"Watchdog DB — Live Session Data ({len(watchdog_rows)} sessions)")
    if watchdog_rows:
        history = load_history()
        pre_wd = [r for r in watchdog_rows if r["first_ts"] and r["first_ts"] < REGRESSION_DATE.timestamp()]
        post_wd = [r for r in watchdog_rows if r["first_ts"] and r["first_ts"] >= REGRESSION_DATE.timestamp()]

        def avg_rw_ratio(rows: list[dict]) -> str:
            ratios = [r["reads"] / r["writes"] for r in rows if r["writes"] > 0]
            return f"{sum(ratios)/len(ratios):.2f}:1" if ratios else "n/a"

        print(f"  Pre-March-8 avg ratio:  {avg_rw_ratio(pre_wd)}")
        print(f"  Post-March-8 avg ratio: {avg_rw_ratio(post_wd)}")
        print()
        print(f"  {'Session':<18} {'Date':<12} {'Reads':>6} {'Writes':>7} {'Ratio':>8}  {'Project'}")
        print(f"  {'-'*18} {'-'*12} {'-'*6} {'-'*7} {'-'*8}  {'-'*30}")
        for r in watchdog_rows[:30]:
            ts_str = datetime.fromtimestamp(r["first_ts"]).strftime("%Y-%m-%d") if r["first_ts"] else "unknown"
            sid = r["session_id"][:16]
            meta = history.get(r["session_id"], {})
            project = Path(meta.get("project", "")).name[:28] if meta.get("project") else ""
            f_str = flag(r["reads"], r["writes"])
            print(f"  {sid:<18} {ts_str:<12} {r['reads']:>6} {r['writes']:>7} {ratio_str(r['reads'], r['writes']):>8}  {project} {f_str}")
    else:
        print("  [No watchdog.db yet — hooks not yet active for any session]")

    # ── debug files ───────────────────────────────────────────────────────
    debug_rows = load_debug_files()
    print_section(f"Debug File Analysis ({len(debug_rows)} sessions with tool calls)")
    if debug_rows:
        below = [r for r in debug_rows if r["writes"] > 0 and r["reads"] / r["writes"] < 3.0]
        print(f"  Sessions below 3:1 threshold: {len(below)}/{len(debug_rows)}")
        ratios = [r["reads"] / r["writes"] for r in debug_rows if r["writes"] > 0]
        if ratios:
            print(f"  Average ratio: {sum(ratios)/len(ratios):.2f}:1")
            print(f"  Median ratio:  {sorted(ratios)[len(ratios)//2]:.2f}:1")
        print()
        print(f"  {'Session':<18} {'Date':<12} {'Reads':>6} {'Writes':>7} {'Ratio':>8}")
        print(f"  {'-'*18} {'-'*12} {'-'*6} {'-'*7} {'-'*8}")
        for r in sorted(debug_rows, key=lambda x: x["first_ts"], reverse=True)[:20]:
            ts_str = datetime.fromtimestamp(r["first_ts"]).strftime("%Y-%m-%d")
            f_str = flag(r["reads"], r["writes"])
            print(f"  {r['session_id']:<18} {ts_str:<12} {r['reads']:>6} {r['writes']:>7} {ratio_str(r['reads'], r['writes']):>8} {f_str}")
    else:
        print("  [No debug files found or no tool calls parsed]")

    # ── recommendations ───────────────────────────────────────────────────
    print_section("Recommendations")
    try:
        _settings = json.loads((CLAUDE_DIR / "settings.json").read_text())
        _effort = _settings.get("effortLevel", "not set")
    except Exception:
        _effort = "unknown"
    print(f"  1. effortLevel in ~/.claude/settings.json is currently '{_effort}'.")
    if _effort not in ("high", "max"):
        print("     Change to 'high' or 'max' for a significant immediate improvement.")
    else:
        print("     Already at a good level.")
    print()
    print("  2. Run with /effort high or /effort max per-session for Opus 4.6.")
    print()
    print("  3. Set CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1 in your shell profile")
    print("     to restore pre-March-8 thinking depth.")
    print()
    print("  4. Add showThinkingSummaries: true to ~/.claude/settings.json.")
    print()
    print("  5. Watchdog hooks are now active in this project. Ratio threshold: 3:1.")
    print("     Review dashboard.jsonl after each session to track scores.")
    print()

    # ── dashboard summary + effort comparison ─────────────────────────────
    dashboard_path = Path(__file__).parent / "dashboard.jsonl"
    if dashboard_path.exists():
        entries = []
        try:
            with open(dashboard_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass

        print_section("Dashboard — Session Scores by Effort Level")
        if entries:
            # Group by effort level for comparison
            by_effort: dict[str, list] = defaultdict(list)
            for e in entries:
                by_effort[e.get("effort_level", "unknown")].append(e)

            if len(by_effort) > 1:
                print(f"  {'Effort':<10} {'Sessions':>9} {'Avg ratio':>10} {'Avg score':>10} {'Min ratio':>10} {'Max ratio':>10}")
                print(f"  {'-'*10} {'-'*9} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
                for effort in sorted(by_effort):
                    group = by_effort[effort]
                    ratios = [e.get("ratio", 0) for e in group]
                    scores = [e.get("score", 0) for e in group if isinstance(e.get("score"), (int, float))]
                    avg_r = sum(ratios) / len(ratios) if ratios else 0
                    avg_s = sum(scores) / len(scores) if scores else 0
                    print(f"  {effort:<10} {len(group):>9} {avg_r:>10.2f} {avg_s:>10.1f} {min(ratios):>10.2f} {max(ratios):>10.2f}")
                print()
            else:
                level = list(by_effort.keys())[0] if by_effort else "unknown"
                print(f"  Only one effort level recorded so far: '{level}'.")
                print(f"  Run sessions at different effort levels to see comparison.")
                print()

            print(f"  {'Date':<12} {'Effort':<10} {'Score':>6} {'Ratio':>8}  {'Verdict'}")
            print(f"  {'-'*12} {'-'*10} {'-'*6} {'-'*8}  {'-'*40}")
            for e in entries[-20:]:
                ts_str = e.get("ts", "")[:10]
                effort = e.get("effort_level", "?")[:9]
                score = e.get("score", "?")
                r = e.get("ratio", 0)
                verdict = e.get("verdict", "")[:45]
                print(f"  {ts_str:<12} {effort:<10} {str(score):>6} {r:>8.2f}  {verdict}")
        else:
            print("  [No sessions scored yet]")

    # ── intra-session decay ───────────────────────────────────────────────
    watchdog_rows_for_decay = load_watchdog_db()
    decay_sessions = [r for r in watchdog_rows_for_decay if r["writes"] > 0][:10]
    if decay_sessions:
        print_section("Intra-Session Decay (does ratio drop over time?)")
        print(f"  Showing up to 10 sessions split into 3 time buckets (early / mid / late).")
        print(f"  A dropping ratio left→right means the model got lazier as the session went on.")
        print()
        print(f"  {'Session':<18} {'Early':>8} {'Mid':>8} {'Late':>8}  {'Trend'}")
        print(f"  {'-'*18} {'-'*8} {'-'*8} {'-'*8}  {'-'*20}")
        for r in decay_sessions:
            buckets = load_session_decay(r["session_id"])
            if not buckets:
                continue
            def bratio(b: dict) -> str:
                if b["writes"] == 0:
                    return f"{b['reads']}R/0W"
                return f"{b['reads']/b['writes']:.1f}:1"
            ratios_num = [b["reads"] / b["writes"] if b["writes"] > 0 else None for b in buckets]
            valid = [x for x in ratios_num if x is not None]
            if len(valid) >= 2:
                trend = "↓ decay" if valid[-1] < valid[0] * 0.7 else ("↑ improving" if valid[-1] > valid[0] * 1.2 else "→ stable")
            else:
                trend = "→ stable"
            cols = [bratio(b) for b in buckets]
            while len(cols) < 3:
                cols.append("—")
            print(f"  {r['session_id'][:16]:<18} {cols[0]:>8} {cols[1]:>8} {cols[2]:>8}  {trend}")


if __name__ == "__main__":
    main()
