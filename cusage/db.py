"""SQLite schema and small helpers. WAL mode; all timestamps UTC ISO 8601 (Z)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id                  INTEGER PRIMARY KEY,
    polled_at           TEXT NOT NULL,
    ok                  INTEGER NOT NULL,
    http_status         INTEGER,
    error               TEXT,
    session_pct         REAL,
    session_resets_at   TEXT,
    weekly_pct          REAL,
    weekly_resets_at    TEXT,
    credits_spent_month REAL,
    credits_balance     REAL,
    raw_json            TEXT,
    interval_s          REAL,        -- poll interval chosen after this poll
    auth_mode           TEXT
);
CREATE INDEX IF NOT EXISTS idx_polls_polled_at ON polls(polled_at);

CREATE TABLE IF NOT EXISTS poll_buckets (
    poll_id     INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    bucket_name TEXT NOT NULL,
    pct         REAL,
    resets_at   TEXT,
    PRIMARY KEY (poll_id, bucket_name)
);

-- Every poll attempt is counted here, including ones not persisted to `polls`.
CREATE TABLE IF NOT EXISTS poll_tally (
    day     TEXT PRIMARY KEY,      -- local day in TZ_NAME
    ok      INTEGER NOT NULL DEFAULT 0,
    failed  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily (
    day                   TEXT PRIMARY KEY,
    max_session_pct       REAL,
    max_weekly_pct        REAL,
    session_limit_hit     INTEGER,
    weekly_limit_hit      INTEGER,
    credits_spent_day     REAL,
    overage               INTEGER,
    poll_count            INTEGER,
    cc_input_tokens       INTEGER,
    cc_output_tokens      INTEGER,
    cc_cache_read_tokens  INTEGER,
    cc_cache_write_tokens INTEGER,
    notion_page_id        TEXT,
    synced_at             TEXT,
    source_fingerprint    TEXT,     -- detects polls changing after rollup
    needs_sync            INTEGER NOT NULL DEFAULT 1,
    updated_at            TEXT
);

CREATE TABLE IF NOT EXISTS alert_state (
    key            TEXT PRIMARY KEY,
    active         INTEGER NOT NULL DEFAULT 0,
    alerted        INTEGER NOT NULL DEFAULT 0,
    first_at       TEXT,
    last_alert_at  TEXT,
    detail         TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS cc_messages (
    message_id         TEXT PRIMARY KEY,
    ts                 TEXT,
    day                TEXT,
    model              TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cc_messages_day ON cc_messages(day);

CREATE TABLE IF NOT EXISTS cc_processed_files (
    path  TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size  INTEGER NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def get_state(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
