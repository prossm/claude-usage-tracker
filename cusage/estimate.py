"""Estimate the weekly limit in tokens, per weekly window. Inference, not measurement.

Anthropic reports usage only as a percentage. For each 7-day window:

    implied tokens at 100% = Claude Code tokens in window
                             / (weekly % used  x  Claude Code's share of it)

Claude Code's share comes from the response's `seven_day_breakdown`. Caveats,
surfaced in the dashboard:
- models count differently toward the limit, so a change in model mix moves
  the estimate even when the limit hasn't changed;
- tokens are only known for machines that report them (local scan or cc-push);
- the breakdown percentages are rounded integers.

A completed window whose estimate differs from the median of the previous
complete windows by more than SHIFT_RATIO is flagged as a possible limit change.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta

from .util import parse_iso, to_iso, utcnow

WEEK = timedelta(days=7)
MIN_WEEKLY_PCT = 5.0       # below this the ratio is mostly rounding noise
SHIFT_RATIO = 1.33
BASELINE_WINDOWS = 3
MEASURES = {
    "no_cache_read": ("input_tokens", "output_tokens", "cache_write_tokens"),
    "all": ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"),
    "output": ("output_tokens",),
}
FAMILIES = ("fable", "opus", "sonnet", "haiku")


def family(model: str | None) -> str:
    m = (model or "").lower()
    return next((f for f in FAMILIES if f in m), "other")


def _cc_share(raw: str | None) -> float | None:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return None
    rows = ((data.get("seven_day_breakdown") or {}).get("rows") or []) if isinstance(data, dict) else []
    for r in rows:
        if isinstance(r, dict) and r.get("key") == "claude_code" and isinstance(r.get("percent"), (int, float)):
            return float(r["percent"])
    return None


def _tokens(conn: sqlite3.Connection, start: str, end: str) -> tuple[dict, dict]:
    totals = {m: 0 for m in MEASURES}
    by_family: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT model, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cache_read_tokens) cr, "
            "SUM(cache_write_tokens) cw FROM cc_hourly WHERE hour >= ? AND hour < ? GROUP BY model", (start, end)):
        cols = {"input_tokens": r["i"] or 0, "output_tokens": r["o"] or 0,
                "cache_read_tokens": r["cr"] or 0, "cache_write_tokens": r["cw"] or 0}
        fam = by_family.setdefault(family(r["model"]), {m: 0 for m in MEASURES})
        for m, keys in MEASURES.items():
            v = sum(cols[k] for k in keys)
            totals[m] += v
            fam[m] += v
    return totals, by_family


def windows(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or utcnow()
    groups: dict[str, dict] = {}
    for p in conn.execute("SELECT id, polled_at, weekly_pct, weekly_resets_at FROM polls "
                          "WHERE ok = 1 AND weekly_resets_at IS NOT NULL ORDER BY polled_at, id"):
        g = groups.setdefault(p["weekly_resets_at"][:16], {"reset": p["weekly_resets_at"], "polls": 0,
                                                           "max_pct": None, "first": p["polled_at"]})
        g["polls"] += 1
        g["last"], g["last_id"] = p["polled_at"], p["id"]
        if p["weekly_pct"] is not None:
            g["max_pct"] = max(g["max_pct"] or 0.0, p["weekly_pct"])

    cc_first = conn.execute("SELECT MIN(hour) FROM cc_hourly").fetchone()[0]
    out = []
    for key in sorted(groups):
        g = groups[key]
        end = parse_iso(g["reset"])
        start = end - WEEK
        raw = conn.execute("SELECT raw_json FROM polls WHERE id = ?", (g["last_id"],)).fetchone()["raw_json"]
        share = _cc_share(raw)
        tokens, by_family = _tokens(conn, to_iso(start), to_iso(min(end, now)))
        w = {
            "start": to_iso(start), "end": to_iso(end), "complete": end <= now,
            "weekly_pct": g["max_pct"], "cc_share_pct": share, "tokens": tokens, "by_family": by_family,
            "partial_polls": parse_iso(g["first"]) > start + timedelta(hours=6),
            "partial_tokens": cc_first is None or parse_iso(cc_first) > start + timedelta(hours=1),
            "implied": None, "flag": None,
        }
        pct = g["max_pct"] or 0.0
        if pct >= MIN_WEEKLY_PCT and share and tokens["all"] > 0:
            frac = pct / 100 * share / 100
            w["implied"] = {m: round(v / frac) for m, v in tokens.items()}
        out.append(w)

    # Flag shifts against the median of preceding complete, fully covered windows.
    history: list[float] = []
    for w in out:
        if not (w["complete"] and w["implied"]):
            continue
        val = w["implied"]["no_cache_read"]
        clean = not (w["partial_polls"] or w["partial_tokens"])
        if len(history) >= 2:
            base = statistics.median(history[-BASELINE_WINDOWS:])
            ratio = val / base if base else None
            if ratio and (ratio > SHIFT_RATIO or ratio < 1 / SHIFT_RATIO):
                w["flag"] = {"ratio": round(ratio, 2),
                             "message": f"Implied limit {'up' if ratio > 1 else 'down'} {abs(ratio - 1) * 100:.0f}% "
                                        f"vs the previous {min(len(history), BASELINE_WINDOWS)} weeks: possible limit change"}
        if clean:
            history.append(val)

    current = next((w for w in reversed(out) if not w["complete"]), None)
    pace = None
    if current and current["weekly_pct"] is not None:
        start, end = parse_iso(current["start"]), parse_iso(current["end"])
        elapsed = (now - start) / (end - start)
        if elapsed >= 0.1:
            pace = {"elapsed_pct": round(elapsed * 100, 1), "projected_pct": round(current["weekly_pct"] / elapsed, 1)}
    return {"windows": out, "pace": pace}
