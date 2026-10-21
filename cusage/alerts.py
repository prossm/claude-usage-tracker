"""Slack alerting with rate-limit state persisted in `alert_state`."""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
from datetime import datetime, timedelta
from typing import Callable

from .client import AUTH, FetchResult
from .config import Config
from .parse import Usage
from .util import parse_iso, scrub, to_iso, utcnow

log = logging.getLogger(__name__)

REMIND_EVERY = timedelta(hours=12)
OTHER_FAILURE_THRESHOLD = 3
PREFIX = "Claude usage tracker:"


def slack_sender(webhook_url: str) -> Callable[[str], bool]:
    def send(text: str) -> bool:
        if not webhook_url:
            log.warning("SLACK_WEBHOOK_URL not set; alert not sent: %s", text)
            return True  # nothing to retry against
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                ok = 200 <= resp.status < 300
        except Exception as exc:
            log.error("Slack alert failed: %s", scrub(exc))
            return False
        if ok:
            log.info("Slack alert sent: %s", text)
        return ok

    return send


class Alerter:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, send: Callable[[str], bool] | None = None):
        self.conn = conn
        self.cfg = cfg
        self.send = send or slack_sender(cfg.slack_webhook_url)

    # -- state helpers -------------------------------------------------------
    def _get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM alert_state WHERE key = ?", (key,)).fetchone()

    def _put(self, key: str, **fields) -> None:
        row = dict(self._get(key) or {"key": key, "active": 0, "alerted": 0})
        row.update(fields)
        self.conn.execute(
            "INSERT INTO alert_state (key, active, alerted, first_at, last_alert_at, detail) "
            "VALUES (:key, :active, :alerted, :first_at, :last_alert_at, :detail) "
            "ON CONFLICT(key) DO UPDATE SET active=excluded.active, alerted=excluded.alerted, "
            "first_at=excluded.first_at, last_alert_at=excluded.last_alert_at, detail=excluded.detail",
            {"first_at": None, "last_alert_at": None, "detail": None, **row},
        )

    def _clear(self, key: str) -> None:
        self.conn.execute("DELETE FROM alert_state WHERE key = ?", (key,))

    # -- poll outcome ----------------------------------------------------------
    def on_poll(self, result: FetchResult, usage: Usage | None, consecutive_failures: int, now: datetime | None = None) -> None:
        now = now or utcnow()
        if result.ok:
            self._recovered(consecutive_failures, now)
            if usage is not None:
                self._schema(usage)
                self._threshold(usage)
        else:
            self._failure(result, consecutive_failures, now)

    def _failure(self, result: FetchResult, n: int, now: datetime) -> None:
        row = self._get("failure")
        if row is None or not row["active"]:
            self._put("failure", active=1, alerted=0, first_at=to_iso(now), last_alert_at=None, detail=result.kind)
            row = self._get("failure")
        if row["alerted"]:
            last = parse_iso(row["last_alert_at"])
            due = last is None or now - last >= REMIND_EVERY
            reminder = True
        else:
            due = result.kind == AUTH or n >= OTHER_FAILURE_THRESHOLD
            reminder = False
        if not due:
            return
        text = self._failure_text(result, n, row["first_at"], reminder)
        if self.send(text):
            self._put("failure", alerted=1, last_alert_at=to_iso(now), detail=result.kind)

    def _failure_text(self, result: FetchResult, n: int, since: str | None, reminder: bool) -> str:
        lead = f"{PREFIX} still failing" if reminder else PREFIX
        err = scrub(result.error or "unknown error")
        if result.kind == AUTH:
            if self.cfg.auth_mode == "oauth":
                fix = "The Claude Code OAuth token on the Pi is missing or expired. Run `claude` on the Pi to renew it."
            else:
                fix = ("The claude.ai session key has likely expired. Replace CLAUDE_SESSION_KEY in "
                       "/opt/claude-usage/.env (the poller picks it up automatically).")
            return f"{lead} auth failure ({err}). {fix} Failing since {since}."
        return f"{lead} {n} consecutive polls failed. Last error: {err}. Failing since {since}."

    def _recovered(self, n_before: int, now: datetime) -> None:
        row = self._get("failure")
        if row is None or not row["active"]:
            return
        if row["alerted"]:
            if not self.send(f"{PREFIX} recovered. Polling is succeeding again (failing since {row['first_at']})."):
                return  # try again on the next success
        self._clear("failure")

    def _schema(self, usage: Usage) -> None:
        detail = "; ".join(sorted(usage.warnings))
        row = self._get("schema")
        if not detail:
            if row is not None:
                self._clear("schema")
            return
        if row is not None and row["detail"] == detail:
            return
        text = (f"{PREFIX} usage response changed shape ({detail}). Raw JSON is still stored; "
                f"fix the parser, then run `claude-usage backfill-parse`.")
        if self.send(text):
            self._put("schema", active=1, alerted=1, first_at=to_iso(utcnow()), last_alert_at=to_iso(utcnow()), detail=detail)

    def _threshold(self, usage: Usage) -> None:
        limit = self.cfg.alert_weekly_pct
        if limit is None or usage.weekly_pct is None or usage.weekly_pct < limit:
            return
        # One alert per weekly window, identified by its reset time (hour precision).
        window = (usage.weekly_resets_at or "unknown")[:13]
        row = self._get("weekly_threshold")
        if row is not None and row["detail"] == window:
            return
        reset = parse_iso(usage.weekly_resets_at)
        when = reset.astimezone(self.cfg.tz).strftime("%a %b %-d %H:%M") if reset else "unknown"
        text = f"{PREFIX} weekly usage is at {usage.weekly_pct:.0f}% (threshold {limit:g}%). Window resets {when}."
        if self.send(text):
            self._put("weekly_threshold", active=1, alerted=1, last_alert_at=to_iso(utcnow()), detail=window)

    # -- jobs ------------------------------------------------------------------
    def job_failed(self, job: str, error: object) -> None:
        self.send(f"{PREFIX} `{job}` failed: {scrub(error)[:300]}")
