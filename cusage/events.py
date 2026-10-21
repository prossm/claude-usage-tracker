"""Detect plan-limit changes from the stored raw responses.

The usage API reports percentages only, so a change in the *size* of a limit is
invisible here (see estimate.py for the token-based inference). What can be
seen directly, and is logged to `limit_events`:

- a limit appearing or disappearing (e.g. a new per-model weekly limit);
- the weekly reset moving off its 7-day schedule;
- a per-model allowance changing (`weekly_scoped_shares`, % of the weekly limit);
- the extra-usage monthly cap changing;
- fields added to or removed from the response.

Processing is incremental (state key `events`) and only the poller writes, so
events are recorded once. `rebuild()` replays all stored polls.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from .config import Config
from .db import get_state, set_state
from .parse import parse_usage, scoped_limit_name
from .util import parse_iso

log = logging.getLogger(__name__)

REMOVAL_GRACE = timedelta(hours=24)   # a limit must be absent this long before it counts as removed
WEEK = timedelta(days=7)
LIMIT, INFO = "limit", "info"


def snapshot(data: dict) -> dict:
    u = parse_usage(data)
    limits = set()
    if isinstance(data.get("five_hour"), dict):
        limits.add("session")
    if isinstance(data.get("seven_day"), dict):
        limits.add("weekly")
    for entry in data.get("limits") or []:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if kind == "session":
            limits.add("session")
        elif kind == "weekly_all":
            limits.add("weekly")
    limits |= {b.name for b in u.buckets}

    allowances = {}
    entries = data.get("limits") if isinstance(data.get("limits"), list) else []
    for share in data.get("weekly_scoped_shares") or []:
        if not isinstance(share, dict) or not isinstance(share.get("limit_index"), int):
            continue
        idx = share["limit_index"]
        name = scoped_limit_name(entries[idx]) if 0 <= idx < len(entries) else None
        if name and isinstance(share.get("allowance_percent_of_weekly"), (int, float)):
            allowances[name] = share["allowance_percent_of_weekly"]

    extra = data.get("extra_usage") if isinstance(data.get("extra_usage"), dict) else {}
    cap = extra.get("monthly_limit")
    return {
        "limits": sorted(limits),
        "weekly_reset": u.weekly_resets_at,
        "allowances": allowances,
        "cap_cents": cap if isinstance(cap, (int, float)) and not isinstance(cap, bool) else None,
        "keys": sorted(data.keys()),
    }


def label(name: str) -> str:
    if name == "session":
        return "5-hour session limit"
    if name == "weekly":
        return "weekly limit (all models)"
    if name.startswith("seven_day_surface_"):
        return f"{name[len('seven_day_surface_'):].replace('_', ' ').title()} weekly limit"
    if name.startswith("seven_day_"):
        return f"{name[len('seven_day_'):].replace('_', ' ').title()} weekly limit"
    return name


def _reset_moved(prev: str | None, cur: str | None, at: datetime) -> bool:
    p, c = parse_iso(prev), parse_iso(cur)
    if p is None or c is None or abs((c - p).total_seconds()) < 120:
        return False
    k = round((c - p) / WEEK)
    on_schedule = k >= 1 and abs(c - p - k * WEEK) <= timedelta(hours=1)
    window_ended = at >= p - timedelta(hours=1)
    return not (on_schedule and window_ended)


def diff(prev: dict | None, cur: dict, st: dict, at: datetime, tz) -> list[tuple]:
    """(kind, subject, old, new, severity, message) tuples; mutates the debounce state `st`."""
    out = []
    present = set(st.setdefault("present", []))
    absent = st.setdefault("absent", {})
    if prev is None:
        present = set(cur["limits"])
        shares = ", ".join(f"{label(k)} {v:g}% of weekly" for k, v in sorted(cur["allowances"].items()))
        msg = "Tracking started. Limits: " + ", ".join(label(n) for n in cur["limits"]) + (f". Allowances: {shares}" if shares else "")
        out.append(("tracking_started", None, None, json.dumps(cur["limits"]), INFO, msg))
    else:
        for name in sorted(set(cur["limits"]) - present):
            out.append(("limit_added", name, None, name, LIMIT, f"New limit: {label(name)}"))
            present.add(name)
        for name in sorted(present - set(cur["limits"])):
            since = parse_iso(absent.get(name))
            if since is None:
                absent[name] = at.isoformat()
            elif at - since >= REMOVAL_GRACE:
                out.append(("limit_removed", name, name, None, LIMIT, f"Limit removed: {label(name)}"))
                present.discard(name)
                absent.pop(name, None)
        for name in list(absent):
            if name in cur["limits"]:
                absent.pop(name)

        if _reset_moved(prev.get("weekly_reset"), cur.get("weekly_reset"), at):
            fmt = lambda iso: parse_iso(iso).astimezone(tz).strftime("%a %b %-d %H:%M")
            out.append(("weekly_reset_moved", "weekly", prev["weekly_reset"], cur["weekly_reset"], LIMIT,
                        f"Weekly reset moved off schedule: was {fmt(prev['weekly_reset'])}, now {fmt(cur['weekly_reset'])}"))

        for name in sorted(set(prev["allowances"]) | set(cur["allowances"])):
            a, b = prev["allowances"].get(name), cur["allowances"].get(name)
            if a is not None and b is not None and a != b:
                out.append(("allowance_changed", name, str(a), str(b), LIMIT,
                            f"{label(name)} allowance changed from {a:g}% to {b:g}% of the weekly limit"))

        a, b = prev.get("cap_cents"), cur.get("cap_cents")
        if a is not None and b is not None and a != b:
            out.append(("extra_cap_changed", "extra_usage", str(a), str(b), INFO,
                        f"Extra-usage monthly cap changed from ${a / 100:,.2f} to ${b / 100:,.2f}"))

        added = sorted(set(cur["keys"]) - set(prev["keys"]))
        removed = sorted(set(prev["keys"]) - set(cur["keys"]))
        if added or removed:
            parts = ([f"added {', '.join(added)}"] if added else []) + ([f"removed {', '.join(removed)}"] if removed else [])
            out.append(("api_fields_changed", None, json.dumps(removed), json.dumps(added), INFO,
                        "Usage response fields " + "; ".join(parts)))
    st["present"] = sorted(present)
    return out


def update(conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Process polls not yet seen; returns the new events (oldest first). Caller commits."""
    st = get_state(conn, "events", {}) or {}
    last_id = st.get("last_poll_id", 0)
    prev = st.get("snapshot")
    new = []
    for row in conn.execute("SELECT id, polled_at, raw_json FROM polls WHERE ok = 1 AND id > ? AND raw_json IS NOT NULL "
                            "ORDER BY id", (last_id,)).fetchall():
        try:
            data = json.loads(row["raw_json"])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        cur = snapshot(data)
        at = parse_iso(row["polled_at"])
        for kind, subject, old, newv, sev, msg in diff(prev, cur, st, at, cfg.tz):
            cur_ins = conn.execute(
                "INSERT OR IGNORE INTO limit_events (poll_id, at, kind, subject, old_value, new_value, severity, message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (row["id"], row["polled_at"], kind, subject, old, newv, sev, msg))
            if cur_ins.rowcount:
                new.append({"at": row["polled_at"], "kind": kind, "subject": subject, "severity": sev, "message": msg})
        prev = cur
        st["last_poll_id"] = row["id"]
    st["snapshot"] = prev
    set_state(conn, "events", st)
    return new


def rebuild(conn: sqlite3.Connection, cfg: Config) -> int:
    conn.execute("DELETE FROM limit_events")
    conn.execute("DELETE FROM state WHERE key = 'events'")
    n = len(update(conn, cfg))
    conn.commit()
    return n
