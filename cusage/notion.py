"""Upsert `daily` rows into a Notion database (stdlib HTTP, ~3 req/s, 429 backoff).

Targets Notion API 2026-03-11. Since 2025-09-03 a database is a container of
one or more data sources; schema reads, queries and page parents all use the
data source ID, which is resolved from NOTION_DATABASE_ID at startup. Trash
state is `in_trash` (the old `archived` field is gone as of 2026-03-11).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Callable

from .util import scrub, to_iso, utcnow

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MIN_GAP_S = 0.35
MAX_ATTEMPTS = 6

REQUIRED_PROPERTIES = {
    "Day": "title",
    "Date": "date",
    "Max session %": "number",
    "Max weekly %": "number",
    "Session limit hit": "checkbox",
    "Weekly limit hit": "checkbox",
    "Credits spent": "number",
    "Overage": "checkbox",
    "Polls": "number",
    "CC input tokens": "number",
    "CC output tokens": "number",
}

Request = Callable[[str, str, dict | None, dict], tuple[int, dict, dict]]


class NotionError(Exception):
    pass


def normalize_id(value: str) -> str:
    """Accept a bare ID, a dashed UUID, or a full Notion URL."""
    path = value.split("?", 1)[0].split("#", 1)[0]  # ?v= is the view ID, not the database
    m = re.findall(r"[0-9a-fA-F]{32}", path.replace("-", ""))
    return m[-1].lower() if m else value


def _urllib_request(method: str, url: str, body: dict | None, headers: dict) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            payload = json.loads(err.read() or b"{}")
        except ValueError:
            payload = {}
        return err.code, dict(err.headers or {}), payload


class Notion:
    def __init__(self, token: str, database_id: str, request: Request | None = None,
                 sleep: Callable[[float], None] = time.sleep, min_gap: float = MIN_GAP_S,
                 data_source_id: str = ""):
        if not token or not (database_id or data_source_id):
            raise NotionError("NOTION_TOKEN and NOTION_DATABASE_ID must be set")
        self._token = token
        self.database_id = normalize_id(database_id) if database_id else ""
        self.data_source_id = normalize_id(data_source_id) if data_source_id else ""
        self._request = request or _urllib_request
        self._sleep = sleep
        self._min_gap = min_gap
        self._last = 0.0

    def _call(self, method: str, path: str, body: dict | None = None, allow: tuple[int, ...] = ()) -> tuple[int, dict]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        for attempt in range(MAX_ATTEMPTS):
            wait = self._min_gap - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            try:
                status, resp_headers, payload = self._request(method, f"{API}{path}", body, headers)
            except Exception as exc:
                status, resp_headers, payload = 0, {}, {"message": f"{type(exc).__name__}: {scrub(exc)}"}
            self._last = time.monotonic()
            if status == 429 or status == 0 or status >= 500:
                retry_after = {k.lower(): v for k, v in resp_headers.items()}.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                log.warning("Notion %s %s -> %s; retrying in %.1fs", method, path, status or "network error", delay)
                self._sleep(delay)
                continue
            if 200 <= status < 300 or status in allow:
                return status, payload
            raise NotionError(f"Notion {method} {path} failed: HTTP {status}: {payload.get('message', '')}")
        raise NotionError(f"Notion {method} {path} failed after {MAX_ATTEMPTS} attempts")

    def resolve_data_source(self) -> str:
        """Data source ID for the configured database (cached)."""
        if self.data_source_id:
            return self.data_source_id
        status, db = self._call("GET", f"/databases/{self.database_id}", allow=(400, 404))
        if status in (400, 404):
            # Maybe the configured ID is already a data source ID.
            status, _ = self._call("GET", f"/data_sources/{self.database_id}", allow=(400, 404))
            if status == 200:
                self.data_source_id = self.database_id
                return self.data_source_id
            raise NotionError(f"database {self.database_id} not found, or not shared with the integration "
                              "(open the database, ••• menu > Connections > + Add connection)")
        sources = db.get("data_sources") or []
        if len(sources) != 1:
            listing = ", ".join(f"{d.get('name') or 'unnamed'} = {d.get('id')}" for d in sources) or "none"
            raise NotionError(f"database {self.database_id} has {len(sources)} data sources ({listing}); "
                              "set NOTION_DATA_SOURCE_ID to the one to sync into")
        self.data_source_id = sources[0]["id"]
        return self.data_source_id

    def verify_schema(self) -> list[str]:
        try:
            ds = self.resolve_data_source()
        except NotionError as exc:
            return [str(exc)]
        _, source = self._call("GET", f"/data_sources/{ds}")
        props = source.get("properties", {})
        problems = []
        for name, kind in REQUIRED_PROPERTIES.items():
            if name not in props:
                problems.append(f"missing property '{name}' (type {kind})")
            elif props[name].get("type") != kind:
                problems.append(f"property '{name}' is {props[name].get('type')}, expected {kind}")
        return problems

    def find_page(self, day: str) -> str | None:
        _, res = self._call("POST", f"/data_sources/{self.resolve_data_source()}/query", {
            "filter": {"property": "Day", "title": {"equals": day}}, "page_size": 5, "result_type": "page"})
        pages = [p for p in res.get("results", []) if not p.get("in_trash") and not p.get("is_archived")]
        if len(pages) > 1:
            log.warning("Notion has %d pages for %s; updating the first", len(pages), day)
        return pages[0]["id"] if pages else None

    def update_page(self, page_id: str, props: dict) -> bool:
        """False if the page is gone or in the trash (caller falls back to find/create)."""
        status, payload = self._call("PATCH", f"/pages/{page_id}", {"properties": props}, allow=(400, 404))
        message = payload.get("message", "").lower()
        if status == 404 or (status == 400 and ("archived" in message or "trash" in message)):
            return False
        if status == 400:
            raise NotionError(f"Notion PATCH /pages failed: HTTP 400: {payload.get('message', '')}")
        return not payload.get("in_trash", False)

    def create_page(self, props: dict) -> str:
        _, page = self._call("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self.resolve_data_source()},
            "properties": props})
        return page["id"]


def page_properties(row: sqlite3.Row) -> dict:
    def num(v):
        return {"number": v}

    return {
        "Day": {"title": [{"text": {"content": row["day"]}}]},
        "Date": {"date": {"start": row["day"]}},
        "Max session %": num(row["max_session_pct"]),
        "Max weekly %": num(row["max_weekly_pct"]),
        "Session limit hit": {"checkbox": bool(row["session_limit_hit"])},
        "Weekly limit hit": {"checkbox": bool(row["weekly_limit_hit"])},
        "Credits spent": num(row["credits_spent_day"]),
        "Overage": {"checkbox": bool(row["overage"])},
        "Polls": num(row["poll_count"]),
        "CC input tokens": num(row["cc_input_tokens"]),
        "CC output tokens": num(row["cc_output_tokens"]),
    }


def sync(conn: sqlite3.Connection, notion: Notion, force: bool = False) -> int:
    problems = notion.verify_schema()
    if problems:
        raise NotionError("Notion database is not set up as expected:\n  - " + "\n  - ".join(problems))
    where = "1=1" if force else "needs_sync = 1 OR notion_page_id IS NULL"
    rows = conn.execute(f"SELECT * FROM daily WHERE {where} ORDER BY day").fetchall()
    for row in rows:
        props = page_properties(row)
        page_id = row["notion_page_id"]
        if not (page_id and notion.update_page(page_id, props)):
            page_id = notion.find_page(row["day"])
            if page_id:
                notion.update_page(page_id, props)
            else:
                page_id = notion.create_page(props)
                log.info("Notion: created page for %s", row["day"])
        conn.execute("UPDATE daily SET notion_page_id = ?, synced_at = ?, needs_sync = 0 WHERE day = ?",
                     (page_id, to_iso(utcnow()), row["day"]))
        conn.commit()  # per row, so a crash midway doesn't create duplicates next run
    log.info("Notion: synced %d day(s)", len(rows))
    return len(rows)
