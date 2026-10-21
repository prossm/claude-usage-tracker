"""Defensive parser for the usage response.

Observed shape (claude.ai /api/organizations/{org}/usage and
api.anthropic.com/api/oauth/usage share it):

    {
      "five_hour":      {"utilization": 12.0, "resets_at": "2026-...+00:00"},
      "seven_day":      {"utilization": 40.0, "resets_at": "..."},
      "seven_day_opus": {"utilization": 3.0,  "resets_at": "..."} | null,
      "seven_day_sonnet": ..., "seven_day_oauth_apps": ...,
      "extra_usage": {"is_enabled": true, "monthly_limit": 10000,
                      "used_credits": 250, "utilization": 2.5}
      "spend": {"used": {"amount_minor": 250, "exponent": 2}, "balance": ...},
      "limits": [{"kind": "weekly_scoped", "percent": 0, "resets_at": "...",
                  "scope": {"model": {"display_name": "Fable"}, "surface": null}}, ...]
    }

Seen live on 2026-10-20 (tests/fixtures/usage_2026-10-20.json): per-model
weekly limits now appear only in `limits` (kind weekly_scoped), while the
seven_day_<model> keys are null. Both are mapped to seven_day_<slug> buckets.

Unknown keys are ignored here (they stay in raw_json). A missing expected key
yields NULL plus a warning; it never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .util import parse_iso, to_iso

SESSION_KEY = "five_hour"
WEEKLY_KEY = "seven_day"
EXPECTED_KEYS = (SESSION_KEY, WEEKLY_KEY)
# Older/other clients have reported these names; accept them as fallbacks.
_PCT_FIELDS = ("utilization", "utilization_pct", "percent")
_RESET_FIELDS = ("resets_at", "reset_at")
_NOT_BUCKETS = {"extra_usage", "spend", "limits"}


@dataclass
class Bucket:
    name: str
    pct: float | None
    resets_at: str | None


@dataclass
class Usage:
    session_pct: float | None = None
    session_resets_at: str | None = None
    weekly_pct: float | None = None
    weekly_resets_at: str | None = None
    credits_spent_month: float | None = None
    credits_balance: float | None = None
    credits_monthly_limit: float | None = None
    buckets: list[Bucket] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def change_key(self) -> tuple:
        """Values whose change makes a poll worth persisting."""
        return (
            self.session_pct, _minute(self.session_resets_at),
            self.weekly_pct, _minute(self.weekly_resets_at),
            self.credits_spent_month, self.credits_balance,
            tuple((b.name, b.pct, _minute(b.resets_at)) for b in self.buckets),
        )


def _minute(iso: str | None) -> str | None:
    # resets_at can carry sub-second jitter between calls; compare at minute precision.
    return iso[:16] if iso else None


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _money(obj) -> float | None:
    """{"amount_minor": 250, "exponent": 2} -> 2.5"""
    if not isinstance(obj, dict) or _num(obj.get("amount_minor")) is None:
        return None
    exp = obj.get("exponent")
    return round(obj["amount_minor"] / (10 ** exp if isinstance(exp, int) else 100), 2)


def _scoped_limit_buckets(limits, existing: set[str]) -> list[Bucket]:
    out = []
    for entry in limits if isinstance(limits, list) else []:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        surface = scope.get("surface")
        surface_name = (surface.get("display_name") or surface.get("id")) if isinstance(surface, dict) else surface
        if model.get("display_name") or model.get("id"):
            name = f"seven_day_{_slug(str(model.get('display_name') or model.get('id')))}"
        elif surface_name:
            name = f"seven_day_surface_{_slug(str(surface_name))}"
        else:
            continue
        if name in existing:
            continue  # already reported as a top-level seven_day_<model> object
        existing.add(name)
        out.append(Bucket(name, _num(entry.get("percent")), to_iso(parse_iso(entry.get("resets_at")))))
    return out


def _window(obj) -> tuple[float | None, str | None, bool]:
    """(pct, resets_at, pct_field_found)."""
    if not isinstance(obj, dict):
        return None, None, False
    pct, found = None, False
    for f in _PCT_FIELDS:
        if f in obj:
            found = True
            pct = _num(obj[f])
            break
    reset = None
    for f in _RESET_FIELDS:
        if obj.get(f):
            reset = to_iso(parse_iso(obj[f]))
            break
    return pct, reset, found


def parse_usage(data) -> Usage:
    u = Usage()
    if not isinstance(data, dict):
        u.warnings.append("response is not a JSON object")
        return u

    for key in EXPECTED_KEYS:
        if key not in data:
            u.warnings.append(f"missing key '{key}'")
            continue
        value = data[key]
        if value is None:
            # Present but null: no active window. Not a schema change.
            continue
        pct, reset, found = _window(value)
        if not found:
            u.warnings.append(f"'{key}' has no utilization field")
        if key == SESSION_KEY:
            u.session_pct, u.session_resets_at = pct, reset
        else:
            u.weekly_pct, u.weekly_resets_at = pct, reset

    for key, value in data.items():
        if key in EXPECTED_KEYS or key in _NOT_BUCKETS or not isinstance(value, dict):
            continue
        pct, reset, found = _window(value)
        if found:
            u.buckets.append(Bucket(key, pct, reset))
    u.buckets += _scoped_limit_buckets(data.get("limits"), {b.name for b in u.buckets})
    u.buckets.sort(key=lambda b: b.name)

    extra = data.get("extra_usage")
    if isinstance(extra, dict):
        used = _num(extra.get("used_credits"))
        limit = _num(extra.get("monthly_limit"))
        # Both denominated in cents.
        if used is not None:
            u.credits_spent_month = round(used / 100, 2)
        if limit is not None:
            u.credits_monthly_limit = round(limit / 100, 2)
            if used is not None:
                u.credits_balance = round((limit - used) / 100, 2)
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
    if u.credits_spent_month is None:
        u.credits_spent_month = _money(spend.get("used"))
    # Prefer an explicit balance. Without one, fall back to headroom under the
    # monthly cap, which overstates what's spendable when extra usage is
    # disabled (e.g. disabled_reason "out_of_credits"); `status` shows that.
    balance = _money(spend.get("balance"))
    if balance is not None:
        u.credits_balance = balance
    return u


def detect_plan(data) -> str | None:
    """Heuristic from jimdawdy-hub/claude-usage-tracker: extra_usage.monthly_limit
    in cents (2000 = Pro, 10000 = Max 5x, 20000 = Max 20x). The limit may be
    user-configurable, so this is a hint for `status`, never stored as fact."""
    extra = data.get("extra_usage") if isinstance(data, dict) else None
    limit = _num(extra.get("monthly_limit")) if isinstance(extra, dict) else None
    if limit is None:
        return None
    return {2000: "Pro", 10000: "Max 5x", 20000: "Max 20x"}.get(int(limit))
