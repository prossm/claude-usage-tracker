"""Claude Code token totals per local day, from the CLI's JSONL transcripts.

Parsing approach adapted from phuryn/claude-usage scanner.py
(MIT License, Copyright (c) 2026 Pawel Huryn), by way of
jimdawdy-hub/claude-usage-tracker: incremental scan by file mtime, and
deduplication by message.id where the last streamed record per message wins.
See THIRD_PARTY_NOTICES.md.

Changes from upstream: messages are upserted across files (upstream's
INSERT OR IGNORE keeps the first, possibly partial, tally when a file is
rescanned), records without a message.id get a stable synthetic key so a
rescan can't double count them, and totals are kept per UTC hour and model.

Totals from several machines are combined in `cc_hourly`, keyed by source:
"local" is the scan on this machine; a Mac running Claude Code runs `cc-push`,
which scans its own logs and POSTs hourly totals (counts only, never
conversation content) to the Pi's dashboard at /api/cc-hourly.
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

from .config import Config
from .util import day_bounds_utc, local_day, parse_iso, scrub, to_iso, utcnow

log = logging.getLogger(__name__)


def parse_file(path: Path) -> dict[str, dict]:
    """message key -> token record; the last record per key wins."""
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(usage, dict):
                continue
            tokens = {
                "input_tokens": usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
                "cache_write_tokens": usage.get("cache_creation_input_tokens") or 0,
            }
            if not any(tokens.values()):
                continue
            key = msg.get("id") or (f"noid:{rec.get('sessionId', '')}:{rec['uuid']}" if rec.get("uuid") else None)
            ts = parse_iso(rec.get("timestamp"))
            if not key or ts is None:
                continue
            out[key] = {"ts": ts, "model": msg.get("model"), **tokens}
    return out


TOKEN_COLS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
PUSH_WINDOW = timedelta(days=14)
PUSH_BATCH = 2000
MAX_INGEST_ROWS = 100_000


def hour_of(ts: str) -> str:
    """'2026-10-20T15:42:07Z' -> '2026-10-20T15:00:00Z'"""
    return ts[:13] + ":00:00Z"


def scan(conn: sqlite3.Connection, cfg: Config) -> set[str]:
    """Ingest new/changed JSONL files into cc_messages; returns the UTC hours touched."""
    root = cfg.cc_log_dir
    if root is None or not root.is_dir():
        raise FileNotFoundError(f"CLAUDE_CODE_LOG_DIR {root} is not a readable directory")
    touched: set[str] = set()
    files = scanned = 0
    for path in sorted(root.rglob("*.jsonl")):
        files += 1
        try:
            st = path.stat()
        except OSError:
            continue
        seen = conn.execute("SELECT mtime, size FROM cc_processed_files WHERE path = ?", (str(path),)).fetchone()
        if seen and abs(seen["mtime"] - st.st_mtime) < 0.01 and seen["size"] == st.st_size:
            continue
        try:
            records = parse_file(path)
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
            continue
        scanned += 1
        for key, r in records.items():
            ts = to_iso(r["ts"])
            old = conn.execute("SELECT ts FROM cc_messages WHERE message_id = ?", (key,)).fetchone()
            if old and old["ts"]:
                touched.add(hour_of(old["ts"]))
            touched.add(hour_of(ts))
            conn.execute(
                "INSERT INTO cc_messages (message_id, ts, day, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET ts=excluded.ts, day=excluded.day, model=excluded.model, "
                "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
                "cache_read_tokens=excluded.cache_read_tokens, cache_write_tokens=excluded.cache_write_tokens",
                (key, ts, local_day(r["ts"], cfg.tz), r["model"], r["input_tokens"], r["output_tokens"],
                 r["cache_read_tokens"], r["cache_write_tokens"]),
            )
        conn.execute(
            "INSERT INTO cc_processed_files (path, mtime, size) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
            (str(path), st.st_mtime, st.st_size),
        )
        conn.commit()
    log.info("cc-tokens: %d files, %d new or changed, %d hour(s) affected", files, scanned, len(touched))
    return touched


def hourly_rows(conn: sqlite3.Connection, hours: set[str] | None = None, since: str | None = None) -> list[dict]:
    """Aggregate cc_messages into per-(hour, model) totals."""
    sql = ("SELECT substr(ts, 1, 13) || ':00:00Z' AS hour, COALESCE(model, 'unknown') AS model, "
           "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
           "SUM(cache_read_tokens) AS cache_read_tokens, SUM(cache_write_tokens) AS cache_write_tokens, "
           "COUNT(*) AS messages FROM cc_messages WHERE ts IS NOT NULL")
    args: list = []
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " GROUP BY 1, 2 ORDER BY 1, 2"
    rows = [dict(r) for r in conn.execute(sql, args)]
    if hours is not None:
        rows = [r for r in rows if r["hour"] in hours]
    return rows


def store_hourly(conn: sqlite3.Connection, cfg: Config, source: str, hours: set[str], rows: list[dict]) -> int:
    """Replace `source`'s totals for `hours` with `rows`, then refresh the affected daily rows."""
    now = to_iso(utcnow())
    for h in hours:
        conn.execute("DELETE FROM cc_hourly WHERE source = ? AND hour = ?", (source, h))
    conn.executemany(
        "INSERT INTO cc_hourly (source, hour, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, messages, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(source, r["hour"], r["model"], *(int(r[c]) for c in TOKEN_COLS), int(r.get("messages") or 0), now)
         for r in rows],
    )
    conn.commit()
    days = {local_day(parse_iso(h), cfg.tz) for h in hours}
    return update_daily(conn, cfg, days)


def update_daily(conn: sqlite3.Connection, cfg: Config, days: set[str]) -> int:
    """Set daily.cc_* to the sum over all sources for each local day."""
    changed = 0
    for day in sorted(days):
        start, end = day_bounds_utc(day, cfg.tz)
        t = conn.execute(
            "SELECT TOTAL(input_tokens), TOTAL(output_tokens), TOTAL(cache_read_tokens), TOTAL(cache_write_tokens) "
            "FROM cc_hourly WHERE hour >= ? AND hour < ?", (start, end)).fetchone()
        vals = tuple(int(v) for v in t)
        cur = conn.execute(
            "SELECT cc_input_tokens, cc_output_tokens, cc_cache_read_tokens, cc_cache_write_tokens FROM daily WHERE day = ?",
            (day,)).fetchone()
        if cur is not None and tuple(cur) == vals:
            continue
        conn.execute(
            "INSERT INTO daily (day, cc_input_tokens, cc_output_tokens, cc_cache_read_tokens, cc_cache_write_tokens, "
            "needs_sync, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT(day) DO UPDATE SET "
            "cc_input_tokens=excluded.cc_input_tokens, cc_output_tokens=excluded.cc_output_tokens, "
            "cc_cache_read_tokens=excluded.cc_cache_read_tokens, cc_cache_write_tokens=excluded.cc_cache_write_tokens, "
            "needs_sync=1, updated_at=excluded.updated_at",
            (day, *vals, to_iso(utcnow())),
        )
        changed += 1
    conn.commit()
    return changed


def run(conn: sqlite3.Connection, cfg: Config) -> int:
    """Pi-side: scan this machine's logs as source 'local'."""
    hours = scan(conn, cfg)
    return store_hourly(conn, cfg, "local", hours, hourly_rows(conn, hours))


# -- Mac -> Pi -------------------------------------------------------------------

def push(conn: sqlite3.Connection, cfg: Config, everything: bool = False, send=None) -> int:
    """Mac-side: scan local logs and POST hourly totals (no content) to the Pi."""
    if not cfg.cc_push_url or not cfg.cc_push_token:
        raise ValueError("CC_PUSH_URL and CC_PUSH_TOKEN must be set")
    scan(conn, cfg)
    since = None if everything else hour_of(to_iso(utcnow() - PUSH_WINDOW))
    rows = hourly_rows(conn, since=since)
    hours = sorted({r["hour"] for r in rows})
    source = cfg.cc_source_name or socket.gethostname().split(".")[0]
    send = send or _post
    for i in range(0, max(len(hours), 1), PUSH_BATCH):
        batch_hours = set(hours[i:i + PUSH_BATCH])
        payload = {"source": source, "hours": sorted(batch_hours),
                   "rows": [r for r in rows if r["hour"] in batch_hours]}
        send(cfg.cc_push_url, cfg.cc_push_token, payload)
    log.info("cc-push: sent %d hour(s), %d row(s) as source %r", len(hours), len(rows), source)
    return len(rows)


def _post(url: str, token: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"push rejected: HTTP {err.code} {err.read()[:200].decode(errors='replace')}") from None
    except OSError as exc:
        raise RuntimeError(f"push failed: {scrub(exc)}") from None


def ingest(conn: sqlite3.Connection, cfg: Config, payload) -> int:
    """Pi-side handler for POST /api/cc-hourly. Validates strictly; returns rows stored."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    source = payload.get("source")
    if not isinstance(source, str) or not 0 < len(source) <= 64 or source == "local":
        raise ValueError("source must be a short name other than 'local'")
    hours, rows = payload.get("hours"), payload.get("rows")
    if not isinstance(hours, list) or not isinstance(rows, list) or len(rows) > MAX_INGEST_ROWS:
        raise ValueError("hours and rows must be lists")
    clean_hours = set()
    for h in hours:
        if not isinstance(h, str) or parse_iso(h) is None or hour_of(h) != h:
            raise ValueError(f"bad hour {h!r}")
        clean_hours.add(h)
    clean = []
    for r in rows:
        if not isinstance(r, dict) or r.get("hour") not in clean_hours or not isinstance(r.get("model"), str):
            raise ValueError("each row needs an hour listed in hours and a model")
        vals = [r.get(c, 0) for c in TOKEN_COLS + ("messages",)]
        if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**12 for v in vals):
            raise ValueError("token counts must be non-negative integers")
        clean.append({"hour": r["hour"], "model": r["model"][:100], **dict(zip(TOKEN_COLS + ("messages",), vals))})
    store_hourly(conn, cfg, source, clean_hours, clean)
    return len(clean)
