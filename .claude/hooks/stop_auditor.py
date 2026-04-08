#!/usr/bin/env python3
"""
Stop hook — scores the completed session using Ollama and appends to dashboard.jsonl.

Fires when Claude finishes a response. Has a 3s Ollama budget since it doesn't
block anything. Appends one JSON line to dashboard.jsonl per session stop.
"""

import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "eval" / "watchdog.db"
DASHBOARD_PATH = Path(__file__).parent.parent.parent / "eval" / "dashboard.jsonl"
GLOBAL_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5-coder:7b"
OLLAMA_TIMEOUT = 3.0


def read_effort_level() -> str:
    """Read effortLevel from ~/.claude/settings.json. Returns 'unknown' on failure."""
    try:
        data = json.loads(GLOBAL_SETTINGS_PATH.read_text())
        return str(data.get("effortLevel") or "unknown")
    except Exception:
        return "unknown"


def get_session_counts(session_id: str) -> tuple[int, int]:
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=1.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN tool_name IN ('Read','Glob','Grep') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN tool_name IN ('Write','Edit','MultiEdit') THEN 1 ELSE 0 END)
                FROM tool_calls
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if row:
                return (int(row[0] or 0), int(row[1] or 0))
        finally:
            conn.close()
    except Exception:
        pass
    return (0, 0)


def score_session(reads: int, writes: int, ratio: float) -> tuple[int, str]:
    """Ask Ollama to score the session. Returns (score, verdict)."""
    prompt = (
        f"An AI coding assistant finished a session with these tool call stats: "
        f"{reads} research calls (Read/Glob/Grep), {writes} edit operations, "
        f"ratio {ratio:.1f}:1. "
        f"Score from 1-10 how thoroughly it researched before acting "
        f"(10=excellent depth, 1=edited without reading anything). "
        f"Respond ONLY with valid JSON, no other text: "
        f'{{\"score\": <integer>, \"verdict\": \"<one sentence>\"}}'
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 80},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
            raw = result.get("response", "").strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)
            return (int(parsed.get("score", 0)), str(parsed.get("verdict", "")))
    except Exception:
        pass
    # Fallback heuristic score
    if ratio >= 6.0:
        score, verdict = 9, "Strong research depth."
    elif ratio >= 3.0:
        score, verdict = 7, "Adequate research ratio."
    elif ratio >= 1.5:
        score, verdict = 4, "Below-threshold research ratio."
    else:
        score, verdict = 2, "Minimal research before editing."
    return score, verdict


def main() -> None:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    session_id = event.get("session_id") or event.get("sessionId") or "unknown"

    # Skip sessions with no tool calls tracked (pure conversation turns)
    reads, writes = get_session_counts(session_id)
    if reads == 0 and writes == 0:
        sys.exit(0)

    ratio = reads / max(writes, 1)
    score, verdict = score_session(reads, writes, ratio)
    effort_level = read_effort_level()

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "effort_level": effort_level,
        "reads": reads,
        "writes": writes,
        "ratio": round(ratio, 2),
        "score": score,
        "verdict": verdict,
    }

    try:
        with open(DASHBOARD_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
