"""Dashboard web server (stdlib only).

Binds to 127.0.0.1 by default; expose it to your tailnet with
`tailscale serve --bg 8787`, which adds HTTPS and keeps it off the LAN.
Read endpoints are unauthenticated (tailnet-only by design). The one write
endpoint, POST /api/cc-hourly, requires CC_INGEST_TOKEN.
"""

from __future__ import annotations

import hmac
import json
import logging
import sqlite3
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import cctokens, db, estimate
from .config import Config
from .events import label
from .util import local_day, parse_iso, scrub, to_iso, utcnow

log = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"
ASSETS = Path(__file__).parent.parent / "assets"
STATIC = {
    "/": (WEB / "index.html", "text/html; charset=utf-8"),
    "/dashboard.css": (WEB / "dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": (WEB / "dashboard.js", "text/javascript; charset=utf-8"),
    "/favicon.ico": (ASSETS / "favicon.ico", "image/x-icon"),
    "/assets/quota-mark.svg": (ASSETS / "quota-mark.svg", "image/svg+xml"),
    "/assets/quota-mark-dark.svg": (ASSETS / "quota-mark-dark.svg", "image/svg+xml"),
}
RANGES = {"7d": 7, "30d": 30, "90d": 90, "all": None}
GAP_BREAK = timedelta(minutes=25)   # persisted rows are <= 15 min apart; a longer gap is an outage
MAX_POINTS = 1500
MAX_BODY = 8 * 1024 * 1024
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _since(range_key: str) -> str | None:
    days = RANGES.get(range_key, 30)
    return to_iso(utcnow() - timedelta(days=days)) if days else None


def _downsample(points: list) -> list:
    """Keep the max per time bin so peaks survive; None entries (gaps) are kept."""
    real = [p for p in points if p[1] is not None]
    if len(real) <= MAX_POINTS:
        return points
    t0, t1 = points[0][0], points[-1][0]
    width = (t1 - t0) / MAX_POINTS or 1
    out, cur_bin, best = [], None, None
    for p in points:
        if p[1] is None:
            if best:
                out.append(best)
            out.append(p)
            cur_bin, best = None, None
            continue
        b = int((p[0] - t0) / width)
        if b != cur_bin:
            if best:
                out.append(best)
            cur_bin, best = b, list(p)
        elif p[1] > best[1]:
            best[1] = p[1]
    if best:
        out.append(best)
    return out


def _ms(iso: str) -> int:
    return int(parse_iso(iso).timestamp() * 1000)


def series(conn: sqlite3.Connection, range_key: str) -> dict:
    since = _since(range_key)
    rows = conn.execute(
        "SELECT id, polled_at, session_pct, weekly_pct FROM polls WHERE ok = 1 AND polled_at >= ? ORDER BY polled_at, id",
        (since or "",)).fetchall()
    buckets: dict[str, list] = {}
    for b in conn.execute(
            "SELECT p.polled_at, b.bucket_name, b.pct FROM poll_buckets b JOIN polls p ON p.id = b.poll_id "
            "WHERE p.ok = 1 AND p.polled_at >= ? ORDER BY p.polled_at, p.id", (since or "",)):
        buckets.setdefault(b["bucket_name"], []).append([_ms(b["polled_at"]), b["pct"]])
    session, weekly, prev = [], [], None
    for r in rows:
        t = parse_iso(r["polled_at"])
        if prev is not None and t - prev > GAP_BREAK:
            gap_t = int((prev + GAP_BREAK).timestamp() * 1000)
            session.append([gap_t, None])
            weekly.append([gap_t, None])
        ms = int(t.timestamp() * 1000)
        session.append([ms, r["session_pct"]])
        weekly.append([ms, r["weekly_pct"]])
        prev = t
    ev = conn.execute("SELECT at, kind, severity, message FROM limit_events WHERE at >= ? AND kind != 'tracking_started' "
                      "ORDER BY at", (since or "",)).fetchall()
    first = rows[0]["polled_at"] if rows else None
    return {
        "from": _ms(since) if since else (_ms(first) if first else None),
        "to": int(utcnow().timestamp() * 1000),
        "session": _downsample(session),
        "weekly": _downsample(weekly),
        "buckets": [{"name": k, "label": label(k), "points": _downsample(v)} for k, v in sorted(buckets.items())],
        "events": [{"t": _ms(e["at"]), "kind": e["kind"], "severity": e["severity"], "message": e["message"]} for e in ev],
    }


def daily(conn: sqlite3.Connection, cfg: Config, range_key: str) -> dict:
    since = _since(range_key)
    since_day = local_day(parse_iso(since), cfg.tz) if since else ""
    days = {r["day"]: dict(r) for r in conn.execute(
        "SELECT day, max_session_pct, max_weekly_pct, session_limit_hit, weekly_limit_hit, credits_spent_day, "
        "overage, poll_count FROM daily WHERE day >= ? ORDER BY day", (since_day,))}
    tokens: dict[str, dict] = {}
    for r in conn.execute("SELECT hour, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens "
                          "FROM cc_hourly WHERE hour >= ?", (since or "",)):
        day = local_day(parse_iso(r["hour"]), cfg.tz)
        fam = tokens.setdefault(day, {}).setdefault(estimate.family(r["model"]), {m: 0 for m in estimate.MEASURES})
        for m, keys in estimate.MEASURES.items():
            fam[m] += sum(r[k] for k in keys)
    for day in tokens:
        days.setdefault(day, {"day": day})
    return {"days": [{**days[d], "tokens": tokens.get(d, {})} for d in sorted(days) if d >= since_day]}


def summary(conn: sqlite3.Connection, cfg: Config) -> dict:
    st = db.get_state(conn, "poller", {}) or {}
    latest = conn.execute("SELECT * FROM polls WHERE ok = 1 ORDER BY polled_at DESC, id DESC LIMIT 1").fetchone()
    out = {
        "now": to_iso(utcnow()), "tz": cfg.tz_name,
        "poller": {k: st.get(k) for k in ("interval_s", "last_poll_at", "last_ok_at", "consecutive_failures", "last_error")},
        "alerts": [dict(a) for a in conn.execute("SELECT key, detail, first_at FROM alert_state WHERE active = 1")],
        "sources": [dict(s) for s in conn.execute(
            "SELECT source, MAX(received_at) AS received_at, MAX(hour) AS last_hour FROM cc_hourly GROUP BY source")],
        "latest": None, "breakdown": [], "pace": estimate.windows(conn)["pace"],
    }
    if latest:
        try:
            raw = json.loads(latest["raw_json"] or "{}")
        except ValueError:
            raw = {}
        extra = raw.get("extra_usage") if isinstance(raw.get("extra_usage"), dict) else {}
        out["latest"] = {
            "polled_at": latest["polled_at"],
            "session_pct": latest["session_pct"], "session_resets_at": latest["session_resets_at"],
            "weekly_pct": latest["weekly_pct"], "weekly_resets_at": latest["weekly_resets_at"],
            "credits_spent_month": latest["credits_spent_month"], "credits_balance": latest["credits_balance"],
            "extra_usage_enabled": extra.get("is_enabled"), "extra_usage_disabled_reason": extra.get("disabled_reason"),
            "buckets": [{"name": b["bucket_name"], "label": label(b["bucket_name"]), "pct": b["pct"], "resets_at": b["resets_at"]}
                        for b in conn.execute("SELECT * FROM poll_buckets WHERE poll_id = ? ORDER BY bucket_name", (latest["id"],))],
        }
        rows = ((raw.get("seven_day_breakdown") or {}).get("rows") or []) if isinstance(raw, dict) else []
        out["breakdown"] = [{"key": r.get("key"), "label": r.get("display_name") or r.get("key"), "pct": r.get("percent")}
                            for r in rows if isinstance(r, dict)]
    return out


def events_list(conn: sqlite3.Connection) -> dict:
    return {"events": [dict(e) for e in conn.execute(
        "SELECT at, kind, subject, old_value, new_value, severity, message FROM limit_events ORDER BY at DESC, id DESC LIMIT 500")]}


def make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "claude-usage"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: N802 - stdlib name
            log.debug("%s %s", self.address_string(), scrub(fmt % args))

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            rng = q.get("range", "30d") if q.get("range") in RANGES else "30d"
            if url.path in STATIC:
                path, ctype = STATIC[url.path]
                try:
                    return self._send(200, path.read_bytes(), ctype)
                except OSError:
                    return self._json(404, {"error": "not found"})
            routes = {
                "/api/summary": lambda c: summary(c, cfg),
                "/api/series": lambda c: series(c, rng),
                "/api/daily": lambda c: daily(c, cfg, rng),
                "/api/estimate": lambda c: estimate.windows(c),
                "/api/events": events_list,
            }
            if url.path not in routes:
                return self._json(404, {"error": "not found"})
            conn = db.connect(cfg.db_path, init=False)
            try:
                return self._json(200, routes[url.path](conn))
            except Exception as exc:
                log.exception("GET %s failed", url.path)
                return self._json(500, {"error": scrub(type(exc).__name__)})
            finally:
                conn.close()

        do_HEAD = do_GET

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/api/cc-hourly":
                return self._json(404, {"error": "not found"})
            if not cfg.cc_ingest_token:
                return self._json(503, {"error": "CC_INGEST_TOKEN is not configured on the Pi"})
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {cfg.cc_ingest_token}".encode()):
                return self._json(401, {"error": "bad token"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._json(413, {"error": "body missing or too large"})
            try:
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            conn = db.connect(cfg.db_path, init=False)
            try:
                n = cctokens.ingest(conn, cfg, payload)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)[:200]})
            except Exception:
                log.exception("ingest failed")
                return self._json(500, {"error": "ingest failed"})
            finally:
                conn.close()
            log.info("cc-hourly: stored %d row(s) from %s", n, str(payload.get("source"))[:64])
            return self._json(200, {"stored": n})

    return Handler


def serve(cfg: Config) -> None:
    db.connect(cfg.db_path).close()  # make sure the schema exists
    httpd = ThreadingHTTPServer((cfg.dashboard_bind, cfg.dashboard_port), make_handler(cfg))
    log.info("dashboard on http://%s:%d", cfg.dashboard_bind, cfg.dashboard_port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
