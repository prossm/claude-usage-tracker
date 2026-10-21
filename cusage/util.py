"""Time helpers, secret scrubbing, logging setup."""

from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Anything that looks like an Anthropic credential, regardless of where it came from.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(sessionKey=)[^;\s\"']+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"(secret_|ntn_)[A-Za-z0-9]+"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"),
]

_known_secrets: list[str] = []


def register_secret(value: str | None) -> None:
    """Remember a configured secret so scrub() redacts it verbatim."""
    if value and len(value) >= 8 and value not in _known_secrets:
        _known_secrets.append(value)


def scrub(text: object) -> str:
    s = str(text)
    for secret in _known_secrets:
        s = s.replace(secret, "[redacted]")
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            s = pat.sub(lambda m: m.group(1) + "[redacted]", s)
        else:
            s = pat.sub("[redacted]", s)
    return s


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        return True


def setup_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    # journald timestamps every line itself; keep the format short.
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_ScrubFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    """UTC ISO 8601 with a Z suffix and second precision; sorts lexically."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_day(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).date().isoformat()


def day_bounds_utc(day: str, tz: ZoneInfo) -> tuple[str, str]:
    """[start, end) of a local calendar day, as UTC ISO strings. DST-safe."""
    d = date.fromisoformat(day)
    start = datetime.combine(d, time(0), tzinfo=tz)
    end = datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz)
    return to_iso(start), to_iso(end)


def fmt_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60}m"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d"
