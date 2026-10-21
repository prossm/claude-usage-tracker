"""Usage API client with swappable auth.

Cookie mode (default) talks to claude.ai with curl_cffi browser TLS
impersonation, the approach jimdawdy-hub/claude-usage-tracker found necessary
to get past Cloudflare. OAuth mode reads Claude Code's credentials file
strictly read-only and talks to api.anthropic.com.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .config import Config
from .util import scrub

log = logging.getLogger(__name__)

TIMEOUT_S = 20
RETRY_DELAY_S = 60
CLAUDE_AI = "https://claude.ai"
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# Failure kinds. "auth" drives the immediate session-key alert.
OK, AUTH, NETWORK, SERVER, OTHER = "ok", "auth", "network", "server", "other"


@dataclass
class HttpResponse:
    status: int
    content_type: str
    text: str
    cf_mitigated: bool = False


@dataclass
class FetchResult:
    kind: str
    http_status: int | None = None
    error: str | None = None
    data: dict | list | None = None
    raw: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind == OK


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> HttpResponse: ...


class CurlTransport:
    """curl_cffi session; `impersonate=None` behaves like plain requests."""

    def __init__(self, impersonate: str | None):
        from curl_cffi import requests as cffi_requests  # imported lazily for tests

        self._session = cffi_requests.Session(impersonate=impersonate) if impersonate else cffi_requests.Session()

    def get(self, url: str, headers: dict[str, str]) -> HttpResponse:
        r = self._session.get(url, headers=headers, timeout=TIMEOUT_S, allow_redirects=False)
        return HttpResponse(
            status=r.status_code,
            content_type=r.headers.get("content-type", ""),
            text=r.text,
            cf_mitigated=r.headers.get("cf-mitigated", "").lower() == "challenge",
        )


def looks_like_html(resp: HttpResponse) -> bool:
    head = resp.text.lstrip()[:200].lower()
    return "html" in resp.content_type.lower() or head.startswith("<!doctype") or head.startswith("<html") or head.startswith("<")


def classify(resp: HttpResponse) -> FetchResult:
    """Map an HTTP response to a FetchResult. HTML is never data."""
    s = resp.status
    if resp.cf_mitigated or (s in (403, 503) and "just a moment" in resp.text[:5000].lower()):
        return FetchResult(AUTH, s, f"Cloudflare challenge (HTTP {s})")
    if s in (401, 403):
        return FetchResult(AUTH, s, f"HTTP {s} (credential rejected)")
    if 500 <= s < 600:
        return FetchResult(SERVER, s, f"HTTP {s}")
    if s != 200:
        return FetchResult(OTHER, s, f"HTTP {s}")
    if looks_like_html(resp):
        return FetchResult(AUTH, s, "HTML response where JSON was expected (login page or Cloudflare challenge)")
    try:
        data = json.loads(resp.text)
    except ValueError:
        return FetchResult(OTHER, s, "response is not valid JSON")
    return FetchResult(OK, s, data=data, raw=resp.text)


def get_json(
    transport: Transport, url: str, headers: dict[str, str], sleep: Callable[[float], object] = time.sleep
) -> FetchResult:
    """GET with one retry after 60s on network error or 5xx. Never retries auth failures."""
    for attempt in (1, 2):
        try:
            result = classify(transport.get(url, headers))
        except Exception as exc:  # curl_cffi raises its own RequestsError subclasses
            result = FetchResult(NETWORK, None, f"network error: {type(exc).__name__}: {scrub(exc)[:200]}")
        if result.kind in (NETWORK, SERVER) and attempt == 1:
            log.warning("usage fetch failed (%s); retrying in %ss", result.error, RETRY_DELAY_S)
            if sleep(RETRY_DELAY_S):  # Event.wait returns True when asked to stop
                return result
            continue
        return result
    return result  # pragma: no cover


class Auth(Protocol):
    mode: str

    def fetch_usage(self, sleep: Callable[[float], object]) -> FetchResult: ...


class CookieAuth:
    """Path A: claude.ai sessionKey cookie."""

    mode = "cookie"

    def __init__(self, cfg: Config, transport: Transport | None = None):
        self._cfg = cfg
        self._transport = transport
        self._org_id = cfg.org_id or None

    def _headers(self) -> dict[str, str]:
        # User-Agent comes from the curl_cffi impersonation profile so it matches
        # the TLS fingerprint; overriding it would make the fingerprint inconsistent.
        return {
            "Cookie": f"sessionKey={self._cfg.session_key}",
            "Accept": "application/json",
            "Referer": f"{CLAUDE_AI}/settings/usage",
        }

    def _get_transport(self) -> Transport:
        if self._transport is None:
            self._transport = CurlTransport(self._cfg.impersonate)
        return self._transport

    def _discover_org(self, sleep) -> FetchResult | str:
        res = get_json(self._get_transport(), f"{CLAUDE_AI}/api/organizations", self._headers(), sleep)
        if not res.ok:
            res.error = f"org discovery: {res.error}"
            return res
        orgs = res.data if isinstance(res.data, list) else []
        chat = [o for o in orgs if isinstance(o, dict) and "chat" in (o.get("capabilities") or [])]
        pick = (chat or [o for o in orgs if isinstance(o, dict)] or [None])[0]
        if not pick or not pick.get("uuid"):
            return FetchResult(OTHER, res.http_status, "org discovery: no organization in response")
        log.info("using organization %s (%s)", pick["uuid"], pick.get("name", "unnamed"))
        return str(pick["uuid"])

    def fetch_usage(self, sleep=time.sleep) -> FetchResult:
        if not self._cfg.session_key:
            return FetchResult(AUTH, None, "CLAUDE_SESSION_KEY is not set")
        if not self._org_id:
            org = self._discover_org(sleep)
            if isinstance(org, FetchResult):
                return org
            self._org_id = org
        return get_json(
            self._get_transport(), f"{CLAUDE_AI}/api/organizations/{self._org_id}/usage", self._headers(), sleep
        )


class OAuthAuth:
    """Path B: Claude Code's OAuth token. READ-ONLY: never refresh, rotate or write.

    Anthropic rotates refresh tokens with reuse detection; a refresh from here
    could revoke the live Claude Code session (see georgewhewell/quota-exporter).
    An expired on-disk token is a gap that heals when `claude` next runs.
    """

    mode = "oauth"

    def __init__(self, cfg: Config, transport: Transport | None = None):
        self._path: Path = cfg.oauth_credentials
        self._transport = transport

    def _token(self) -> str | FetchResult:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:  # read-only by construction
                raw = json.load(fh)
        except FileNotFoundError:
            return FetchResult(AUTH, None, f"OAuth credentials file not found: {self._path}")
        except (OSError, ValueError) as exc:
            return FetchResult(AUTH, None, f"OAuth credentials unreadable: {type(exc).__name__}")
        oauth = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
        if not isinstance(oauth, dict) or not oauth.get("accessToken"):
            return FetchResult(AUTH, None, "OAuth credentials file has no claudeAiOauth.accessToken")
        expires = oauth.get("expiresAt")
        if isinstance(expires, (int, float)) and expires / 1000 < time.time() + 60:
            return FetchResult(AUTH, None, "OAuth token on disk has expired; run `claude` on the Pi so it renews it")
        return str(oauth["accessToken"])

    def fetch_usage(self, sleep=time.sleep) -> FetchResult:
        token = self._token()
        if isinstance(token, FetchResult):
            return token
        if self._transport is None:
            self._transport = CurlTransport(None)
        headers = {"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA, "Accept": "application/json"}
        return get_json(self._transport, OAUTH_USAGE_URL, headers, sleep)


def make_auth(cfg: Config) -> Auth:
    return OAuthAuth(cfg) if cfg.auth_mode == "oauth" else CookieAuth(cfg)
