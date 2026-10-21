"""Adaptive poller.

The interval state machine is a Python port of the algorithm in
CodeForBreakfast/claude-usage-exporter's poller.go (written fresh; that repo
has no license file). Differences from upstream are noted inline and in
DECISIONS.md.
"""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import threading
from datetime import datetime

from . import config as config_mod, events
from .alerts import Alerter
from .client import Auth, FetchResult, make_auth
from .config import Config
from .db import get_state, set_state
from .parse import Usage, parse_usage
from .util import local_day, parse_iso, scrub, to_iso, utcnow

log = logging.getLogger(__name__)


class AdaptiveInterval:
    """Fast while utilization moves, exponential back-off when idle, snap back near a reset."""

    def __init__(self, fast: float = 30, ceiling: float = 300, idle_threshold: int = 3, burst_window: float = 120):
        self.fast = fast
        self.ceiling = ceiling
        self.idle_threshold = idle_threshold
        self.burst_window = burst_window
        self.interval = fast
        self.unchanged = 0
        self._prev: tuple | None = None

    def _reset_soon(self, resets: list[datetime], now: datetime) -> bool:
        return any(0 < (r - now).total_seconds() < self.burst_window for r in resets)

    def on_success(self, session: float | None, weekly: float | None, resets: list[datetime], now: datetime) -> float:
        # Upstream starts prev at 0.0; starting at None makes the first poll count as a change.
        changed = self._prev is None or (session, weekly) != self._prev
        self._prev = (session, weekly)
        if changed or self._reset_soon(resets, now):
            self.unchanged = 0
            self.interval = self.fast
        else:
            self.unchanged += 1
            if self.unchanged >= self.idle_threshold:
                self.interval = min(self.interval * 2, self.ceiling)
        return self.interval

    def on_failure(self) -> float:
        # Upstream leaves the interval untouched on error. Backing off avoids
        # hammering claude.ai with a dead cookie every 30s (Cloudflare notices).
        self.interval = min(max(self.interval * 2, self.fast), self.ceiling)
        return self.interval

    def sleep_for(self, resets: list[datetime], now: datetime) -> float:
        """Interval, shortened so the next poll lands inside an upcoming reset's burst window.

        Upstream can sleep straight over the 2-minute window when idling at 5m.
        """
        s = self.interval
        for r in resets:
            until = (r - now).total_seconds()
            if until > self.burst_window:
                s = min(s, max(self.fast, until - self.burst_window / 2))
        return s


def resets_of(u: Usage) -> list[datetime]:
    return [d for d in (parse_iso(u.session_resets_at), parse_iso(u.weekly_resets_at)) if d]


def _norm(key) -> list:
    return json.loads(json.dumps(key))


def should_persist(change_key, last_key, last_persisted_at: datetime | None, now: datetime, heartbeat_s: float) -> bool:
    if last_key is None or last_persisted_at is None:
        return True
    if _norm(change_key) != _norm(last_key):
        return True
    return (now - last_persisted_at).total_seconds() >= heartbeat_s


def write_usage_columns(conn: sqlite3.Connection, poll_id: int, u: Usage) -> None:
    conn.execute(
        "UPDATE polls SET session_pct=?, session_resets_at=?, weekly_pct=?, weekly_resets_at=?, "
        "credits_spent_month=?, credits_balance=? WHERE id=?",
        (u.session_pct, u.session_resets_at, u.weekly_pct, u.weekly_resets_at,
         u.credits_spent_month, u.credits_balance, poll_id),
    )
    conn.execute("DELETE FROM poll_buckets WHERE poll_id = ?", (poll_id,))
    conn.executemany(
        "INSERT INTO poll_buckets (poll_id, bucket_name, pct, resets_at) VALUES (?, ?, ?, ?)",
        [(poll_id, b.name, b.pct, b.resets_at) for b in u.buckets],
    )


def insert_poll(conn, now: datetime, result: FetchResult, usage: Usage | None, interval: float, auth_mode: str) -> int:
    cur = conn.execute(
        "INSERT INTO polls (polled_at, ok, http_status, error, raw_json, interval_s, auth_mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (to_iso(now), 1 if result.ok else 0, result.http_status,
         scrub(result.error)[:500] if result.error else None,
         result.raw if result.ok else None, interval, auth_mode),
    )
    poll_id = cur.lastrowid
    if usage is not None:
        write_usage_columns(conn, poll_id, usage)
    return poll_id


class Poller:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, auth: Auth | None = None, alerter: Alerter | None = None):
        self.conn = conn
        self.cfg = cfg
        self.auth = auth or make_auth(cfg)
        self.alerter = alerter or Alerter(conn, cfg)
        self.adaptive = AdaptiveInterval(cfg.poll_fast_s, cfg.poll_max_s, cfg.poll_idle_threshold, cfg.poll_reset_burst_s)
        self.stop = threading.Event()
        st = get_state(conn, "poller", {}) or {}
        self.consecutive_failures = int(st.get("consecutive_failures", 0))
        self.last_key = st.get("last_persisted_key")
        self.last_persisted_at = parse_iso(st.get("last_persisted_at"))
        self.last_usage: Usage | None = None

    def poll_once(self, force_persist: bool = False) -> tuple[FetchResult, Usage | None]:
        result = self.auth.fetch_usage(sleep=self.stop.wait)
        now = utcnow()
        usage = None
        day = local_day(now, self.cfg.tz)
        self.conn.execute(
            "INSERT INTO poll_tally (day, ok, failed) VALUES (?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET ok = ok + excluded.ok, failed = failed + excluded.failed",
            (day, int(result.ok), int(not result.ok)),
        )
        if result.ok:
            usage = parse_usage(result.data)
            for w in usage.warnings:
                log.warning("schema: %s", w)
            self.consecutive_failures = 0
            interval = self.adaptive.on_success(usage.session_pct, usage.weekly_pct, resets_of(usage), now)
            key = usage.change_key()
            if force_persist or should_persist(key, self.last_key, self.last_persisted_at, now, self.cfg.poll_heartbeat_s):
                insert_poll(self.conn, now, result, usage, interval, self.auth.mode)
                self.last_key, self.last_persisted_at = _norm(key), now
                self._limit_events(now)
            self.last_usage = usage
            log.info("ok session=%s%% weekly=%s%% next=%ss", usage.session_pct, usage.weekly_pct, int(interval))
        else:
            self.consecutive_failures += 1
            interval = self.adaptive.on_failure()
            insert_poll(self.conn, now, result, None, interval, self.auth.mode)
            log.error("poll failed (%s, %d in a row); next=%ss", scrub(result.error), self.consecutive_failures, int(interval))

        prev = get_state(self.conn, "poller", {}) or {}
        set_state(self.conn, "poller", {
            "interval_s": interval,
            "last_poll_at": to_iso(now),
            "last_ok_at": to_iso(now) if result.ok else prev.get("last_ok_at"),
            "last_error": None if result.ok else scrub(result.error),
            "consecutive_failures": self.consecutive_failures,
            "last_persisted_key": self.last_key,
            "last_persisted_at": to_iso(self.last_persisted_at),
            "auth_mode": self.auth.mode,
        })
        self.alerter.on_poll(result, usage, self.consecutive_failures, now)
        self.conn.commit()
        return result, usage

    def _limit_events(self, now: datetime) -> None:
        try:
            new = events.update(self.conn, self.cfg)
        except Exception as exc:  # event detection must never break polling
            log.exception("limit event detection failed: %s", scrub(exc))
            return
        for e in new:
            log.info("limit event: %s", e["message"])
            recent = (now - parse_iso(e["at"])).total_seconds() < 7200  # don't alert on history replays
            if e["severity"] == events.LIMIT and recent:
                self.alerter.send(f"Claude usage tracker: plan limit change. {e['message']}.")

    def _maybe_reload_config(self, env_path, mtime_seen):
        try:
            mtime = env_path.stat().st_mtime
        except OSError:
            return mtime_seen
        if mtime_seen is not None and mtime != mtime_seen:
            try:
                self.cfg = config_mod.load()
                self.auth = make_auth(self.cfg)
                self.alerter.cfg = self.cfg
                self.alerter.send = Alerter(self.conn, self.cfg).send
                log.info(".env changed; configuration reloaded")
            except Exception as exc:
                log.error("could not reload .env: %s", scrub(exc))
        return mtime

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        env_path = self.cfg.home / ".env"
        env_mtime = self._maybe_reload_config(env_path, None)
        log.info("adaptive poller started (auth=%s, fast=%ss, max=%ss)", self.auth.mode, self.cfg.poll_fast_s, self.cfg.poll_max_s)
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # never let one bad poll kill the daemon, but don't hide it
                log.exception("unexpected error during poll: %s", scrub(exc))
                self.conn.rollback()
                self.adaptive.on_failure()
                self.consecutive_failures += 1
                try:
                    internal = FetchResult("other", None, f"internal error: {type(exc).__name__}: {scrub(exc)[:200]}")
                    self.alerter.on_poll(internal, None, self.consecutive_failures)
                    self.conn.commit()
                except Exception:
                    log.exception("could not record internal error")
                    self.conn.rollback()
            resets = resets_of(self.last_usage) if self.last_usage else []
            delay = self.adaptive.sleep_for(resets, utcnow())
            if self.stop.wait(delay):
                break
            env_mtime = self._maybe_reload_config(env_path, env_mtime)
        log.info("poller stopped")
