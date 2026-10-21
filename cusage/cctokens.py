"""Claude Code token totals per local day, from the CLI's JSONL transcripts.

Parsing approach adapted from phuryn/claude-usage scanner.py
(MIT License, Copyright (c) 2026 Pawel Huryn), by way of
jimdawdy-hub/claude-usage-tracker: incremental scan by file mtime, and
deduplication by message.id where the last streamed record per message wins.
See THIRD_PARTY_NOTICES.md.

Changes from upstream: messages are upserted across files (upstream's
INSERT OR IGNORE keeps the first, possibly partial, tally when a file is
rescanned), records without a message.id get a stable synthetic key so a
rescan can't double count them, and totals are bucketed by local day.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from .config import Config
from .util import local_day, parse_iso, to_iso, utcnow

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


def scan(conn: sqlite3.Connection, cfg: Config) -> set[str]:
    """Ingest new/changed JSONL files; returns the set of local days touched."""
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
            day = local_day(r["ts"], cfg.tz)
            old = conn.execute("SELECT day FROM cc_messages WHERE message_id = ?", (key,)).fetchone()
            if old:
                touched.add(old["day"])
            touched.add(day)
            conn.execute(
                "INSERT INTO cc_messages (message_id, ts, day, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET ts=excluded.ts, day=excluded.day, model=excluded.model, "
                "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
                "cache_read_tokens=excluded.cache_read_tokens, cache_write_tokens=excluded.cache_write_tokens",
                (key, to_iso(r["ts"]), day, r["model"], r["input_tokens"], r["output_tokens"],
                 r["cache_read_tokens"], r["cache_write_tokens"]),
            )
        conn.execute(
            "INSERT INTO cc_processed_files (path, mtime, size) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
            (str(path), st.st_mtime, st.st_size),
        )
        conn.commit()
    log.info("cc-tokens: %d files, %d new or changed, %d day(s) affected", files, scanned, len(touched))
    return touched


def update_daily(conn: sqlite3.Connection, days: set[str]) -> int:
    changed = 0
    for day in sorted(days):
        t = conn.execute(
            "SELECT TOTAL(input_tokens), TOTAL(output_tokens), TOTAL(cache_read_tokens), TOTAL(cache_write_tokens) "
            "FROM cc_messages WHERE day = ?", (day,)).fetchone()
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
    return update_daily(conn, scan(conn, cfg))
