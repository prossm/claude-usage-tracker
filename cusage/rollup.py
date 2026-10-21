"""Daily rollup of `polls` into `daily`, by local day in TZ_NAME."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta

from .config import Config
from .util import day_bounds_utc, to_iso, utcnow

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 7
# Only use a pre-midnight credits reading as the day's baseline if it is this recent;
# otherwise a multi-day outage would attribute several days of spend to one day.
BASELINE_MAX_AGE = timedelta(hours=24)
METRIC_COLS = ("max_session_pct", "max_weekly_pct", "session_limit_hit", "weekly_limit_hit",
               "credits_spent_day", "overage", "poll_count")


def fingerprint(conn: sqlite3.Connection, day: str, start: str, end: str) -> str:
    p = conn.execute(
        "SELECT COUNT(*), MAX(id), TOTAL(session_pct), TOTAL(weekly_pct), TOTAL(credits_spent_month), TOTAL(ok) "
        "FROM polls WHERE polled_at >= ? AND polled_at < ?", (start, end)).fetchone()
    t = conn.execute("SELECT ok, failed FROM poll_tally WHERE day = ?", (day,)).fetchone()
    return json.dumps([list(p), list(t) if t else None])


def credits_spent(values: list[float]) -> float | None:
    """Sum of increases in month-to-date spend; a decrease is the monthly reset."""
    if not values:
        return None
    total = 0.0
    for a, b in zip(values, values[1:]):
        total += b - a if b >= a else b
    return round(total, 2)


def compute_day(conn: sqlite3.Connection, cfg: Config, day: str) -> dict | None:
    start, end = day_bounds_utc(day, cfg.tz)
    tally = conn.execute("SELECT ok, failed FROM poll_tally WHERE day = ?", (day,)).fetchone()
    agg = conn.execute(
        "SELECT COUNT(*) AS n_all, TOTAL(ok) AS n_ok, MAX(session_pct) AS ms, MAX(weekly_pct) AS mw, "
        "MAX(session_pct >= 100) AS sh, MAX(weekly_pct >= 100) AS wh "
        "FROM polls WHERE polled_at >= ? AND polled_at < ?", (start, end)).fetchone()
    if not agg["n_all"] and not tally:
        return None

    day_credits = [r[0] for r in conn.execute(
        "SELECT credits_spent_month FROM polls WHERE ok = 1 AND credits_spent_month IS NOT NULL "
        "AND polled_at >= ? AND polled_at < ? ORDER BY polled_at, id", (start, end))]
    if day_credits:
        floor = to_iso(datetime.fromisoformat(start.replace("Z", "+00:00")) - BASELINE_MAX_AGE)
        base = conn.execute(
            "SELECT credits_spent_month FROM polls WHERE ok = 1 AND credits_spent_month IS NOT NULL "
            "AND polled_at < ? AND polled_at >= ? ORDER BY polled_at DESC, id DESC LIMIT 1", (start, floor)).fetchone()
        if base:
            day_credits.insert(0, base[0])
    spent = credits_spent(day_credits)

    return {
        "max_session_pct": agg["ms"],
        "max_weekly_pct": agg["mw"],
        "session_limit_hit": int(bool(agg["sh"])),
        "weekly_limit_hit": int(bool(agg["wh"])),
        "credits_spent_day": spent,
        "overage": int(bool(spent and spent > 0)),
        "poll_count": int(tally["ok"]) if tally else int(agg["n_ok"]),
        "source_fingerprint": fingerprint(conn, day, start, end),
    }


def upsert_day(conn: sqlite3.Connection, day: str, values: dict) -> bool:
    """Write metrics without touching CC token or Notion columns. Returns True if metrics changed."""
    existing = conn.execute("SELECT * FROM daily WHERE day = ?", (day,)).fetchone()
    changed = existing is None or any(existing[c] != values[c] for c in METRIC_COLS)
    cols = METRIC_COLS + ("source_fingerprint",)
    conn.execute(
        f"INSERT INTO daily (day, {', '.join(cols)}, needs_sync, updated_at) "
        f"VALUES (?, {', '.join('?' for _ in cols)}, 1, ?) "
        f"ON CONFLICT(day) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}, "
        f"needs_sync = CASE WHEN ? THEN 1 ELSE daily.needs_sync END, "
        f"updated_at = CASE WHEN ? THEN excluded.updated_at ELSE daily.updated_at END",
        (day, *[values[c] for c in cols], to_iso(utcnow()), int(changed), int(changed)),
    )
    return changed


def days_to_roll(conn: sqlite3.Connection, cfg: Config, today: date) -> list[str]:
    """Yesterday always; earlier days in the lookback only if they have no row or their polls changed."""
    out = []
    for i in range(LOOKBACK_DAYS, 0, -1):
        day = (today - timedelta(days=i)).isoformat()
        if i == 1:
            out.append(day)
            continue
        start, end = day_bounds_utc(day, cfg.tz)
        row = conn.execute("SELECT source_fingerprint FROM daily WHERE day = ?", (day,)).fetchone()
        has_data = conn.execute("SELECT 1 FROM polls WHERE polled_at >= ? AND polled_at < ? LIMIT 1", (start, end)).fetchone()
        if not has_data:
            continue
        if row is None or row["source_fingerprint"] != fingerprint(conn, day, start, end):
            out.append(day)
    return out


def rollup(conn: sqlite3.Connection, cfg: Config, days: list[str] | None = None, today: date | None = None) -> list[str]:
    today = today or utcnow().astimezone(cfg.tz).date()
    changed = []
    for day in days or days_to_roll(conn, cfg, today):
        values = compute_day(conn, cfg, day)
        if values is None:
            log.info("rollup %s: no polls, skipped", day)
            continue
        if upsert_day(conn, day, values):
            changed.append(day)
        log.info("rollup %s: session max %s%%, weekly max %s%%, credits %s, polls %s",
                 day, values["max_session_pct"], values["max_weekly_pct"], values["credits_spent_day"], values["poll_count"])
    conn.commit()
    return changed
