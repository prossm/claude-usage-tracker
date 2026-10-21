from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cusage import db
from cusage.client import OK, FetchResult
from cusage.config import Config


def make_cfg(**kw) -> Config:
    cfg = Config(home=Path("/nonexistent"), db_path=Path(":memory:"))
    cfg.session_key = "sk-ant-sid01-TESTSECRET"
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def make_conn():
    return db.connect(":memory:")


def usage_json(session=10.0, weekly=20.0, used_cents=None, limit_cents=10000, opus=None,
               session_reset="2026-10-20T20:00:00.123456+00:00", weekly_reset="2026-10-24T13:00:00+00:00") -> dict:
    data = {
        "five_hour": {"utilization": session, "resets_at": session_reset},
        "seven_day": {"utilization": weekly, "resets_at": weekly_reset},
        "seven_day_opus": None if opus is None else {"utilization": opus, "resets_at": weekly_reset},
        "some_future_key": {"x": 1},
    }
    if used_cents is not None:
        data["extra_usage"] = {"is_enabled": True, "monthly_limit": limit_cents,
                               "used_credits": used_cents, "utilization": used_cents / limit_cents * 100}
    return data


def ok_result(data: dict) -> FetchResult:
    return FetchResult(OK, 200, data=data, raw=json.dumps(data))


class FakeAuth:
    mode = "cookie"

    def __init__(self, results):
        self.results = list(results)

    def fetch_usage(self, sleep=None):
        return self.results.pop(0)


class Outbox:
    def __init__(self, ok=True):
        self.sent: list[str] = []
        self.ok = ok

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


def t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if "+" not in s else datetime.fromisoformat(s)


def insert_poll_row(conn, polled_at, ok=1, session=None, weekly=None, credits=None):
    conn.execute(
        "INSERT INTO polls (polled_at, ok, session_pct, weekly_pct, credits_spent_month) VALUES (?, ?, ?, ?, ?)",
        (polled_at, ok, session, weekly, credits))
