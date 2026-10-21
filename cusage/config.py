"""Configuration from environment variables and /opt/claude-usage/.env.

The .env file is read directly (not via systemd EnvironmentFile) so that manual
runs over SSH see exactly the same configuration as the services. Real
environment variables take precedence over the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from .util import register_secret

DEFAULT_HOME = Path("/opt/claude-usage")


class ConfigError(Exception):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser: comments, blank lines, optional quotes, `export`."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _float(env: dict[str, str], key: str, default: float | None) -> float | None:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number") from exc


@dataclass
class Config:
    home: Path
    db_path: Path
    auth_mode: str = "cookie"
    session_key: str = ""
    org_id: str = ""
    oauth_credentials: Path = field(default_factory=lambda: Path("~/.claude/.credentials.json").expanduser())
    impersonate: str = "firefox133"
    notion_token: str = ""
    notion_database_id: str = ""
    notion_data_source_id: str = ""
    slack_webhook_url: str = ""
    tz_name: str = "America/New_York"
    cc_log_dir: Path | None = None
    alert_weekly_pct: float | None = None
    poll_fast_s: float = 30
    poll_max_s: float = 300
    poll_idle_threshold: int = 3
    poll_reset_burst_s: float = 120
    poll_heartbeat_s: float = 900
    # Dashboard (Pi) and Claude Code token push (Mac -> Pi)
    dashboard_bind: str = "127.0.0.1"
    dashboard_port: int = 8787
    cc_ingest_token: str = ""
    cc_push_url: str = ""
    cc_push_token: str = ""
    cc_source_name: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)


def load(env_file: str | None = None) -> Config:
    home = Path(os.environ.get("CLAUDE_USAGE_HOME", str(DEFAULT_HOME)))
    path = Path(env_file) if env_file else Path(os.environ.get("CLAUDE_USAGE_ENV", str(home / ".env")))
    env = read_env_file(path)
    env.update({k: v for k, v in os.environ.items() if v != ""})

    cfg = Config(home=home, db_path=Path(env.get("CLAUDE_USAGE_DB", str(home / "usage.db"))).expanduser())
    cfg.auth_mode = env.get("CLAUDE_AUTH_MODE", "cookie").strip().lower() or "cookie"
    if cfg.auth_mode not in ("cookie", "oauth"):
        raise ConfigError("CLAUDE_AUTH_MODE must be 'cookie' or 'oauth'")
    cfg.session_key = env.get("CLAUDE_SESSION_KEY", "").strip()
    cfg.org_id = env.get("CLAUDE_ORG_ID", "").strip()
    if env.get("CLAUDE_OAUTH_CREDENTIALS"):
        cfg.oauth_credentials = Path(env["CLAUDE_OAUTH_CREDENTIALS"]).expanduser()
    cfg.impersonate = env.get("CLAUDE_IMPERSONATE", cfg.impersonate).strip() or cfg.impersonate
    cfg.notion_token = env.get("NOTION_TOKEN", "").strip()
    cfg.notion_database_id = env.get("NOTION_DATABASE_ID", "").strip()
    cfg.notion_data_source_id = env.get("NOTION_DATA_SOURCE_ID", "").strip()
    cfg.slack_webhook_url = env.get("SLACK_WEBHOOK_URL", "").strip()
    cfg.tz_name = env.get("TZ_NAME", cfg.tz_name).strip() or cfg.tz_name
    try:
        cfg.tz
    except Exception as exc:
        raise ConfigError(f"TZ_NAME {cfg.tz_name!r} is not a valid IANA timezone") from exc
    if env.get("CLAUDE_CODE_LOG_DIR", "").strip():
        cfg.cc_log_dir = Path(env["CLAUDE_CODE_LOG_DIR"].strip()).expanduser()
    cfg.alert_weekly_pct = _float(env, "ALERT_WEEKLY_PCT", None)
    cfg.poll_fast_s = _float(env, "POLL_FAST_SECONDS", cfg.poll_fast_s)
    cfg.poll_max_s = _float(env, "POLL_MAX_SECONDS", cfg.poll_max_s)
    cfg.poll_idle_threshold = int(_float(env, "POLL_IDLE_THRESHOLD", cfg.poll_idle_threshold))
    cfg.poll_reset_burst_s = _float(env, "POLL_RESET_BURST_SECONDS", cfg.poll_reset_burst_s)
    cfg.poll_heartbeat_s = _float(env, "POLL_HEARTBEAT_SECONDS", cfg.poll_heartbeat_s)

    cfg.dashboard_bind = env.get("DASHBOARD_BIND", cfg.dashboard_bind).strip() or cfg.dashboard_bind
    cfg.dashboard_port = int(_float(env, "DASHBOARD_PORT", cfg.dashboard_port))
    cfg.cc_ingest_token = env.get("CC_INGEST_TOKEN", "").strip()
    cfg.cc_push_url = env.get("CC_PUSH_URL", "").strip()
    cfg.cc_push_token = env.get("CC_PUSH_TOKEN", "").strip()
    cfg.cc_source_name = env.get("CC_SOURCE_NAME", "").strip()

    for secret in (cfg.session_key, cfg.notion_token, cfg.slack_webhook_url, cfg.cc_ingest_token, cfg.cc_push_token):
        register_secret(secret)
    return cfg
