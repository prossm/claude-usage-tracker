"""Command line: claude_usage.py <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta

from . import cctokens, config as config_mod, db
from .alerts import Alerter
from .config import Config, ConfigError
from .notion import Notion, NotionError, sync as notion_sync
from .parse import detect_plan, parse_usage
from .poller import Poller, write_usage_columns
from .rollup import rollup
from .util import fmt_age, parse_iso, scrub, setup_logging, utcnow

log = logging.getLogger("claude_usage")

STALE_POLLER_S = 3600


def _local(cfg: Config, iso: str | None, fmt: str = "%a %Y-%m-%d %H:%M %Z") -> str:
    dt = parse_iso(iso)
    return dt.astimezone(cfg.tz).strftime(fmt) if dt else "-"


def _until(iso: str | None) -> str:
    dt = parse_iso(iso)
    if not dt:
        return ""
    secs = (dt - utcnow()).total_seconds()
    return f"in {fmt_age(secs)}" if secs > 0 else "passed"


def cmd_poll(cfg: Config, conn, args) -> int:
    poller = Poller(conn, cfg)
    if args.no_alert:
        poller.alerter.send = lambda text: (log.info("alert suppressed: %s", text), True)[1]
    if args.daemon:
        poller.run_forever()
        return 0
    result, usage = poller.poll_once(force_persist=True)
    if args.print_raw and result.raw:
        print(json.dumps(json.loads(result.raw), indent=2))
    if not result.ok:
        print(f"poll failed: {scrub(result.error)}", file=sys.stderr)
        return 1
    print(f"session {usage.session_pct}%  weekly {usage.weekly_pct}%  "
          f"credits ${usage.credits_spent_month}  buckets {[(b.name, b.pct) for b in usage.buckets]}")
    for w in usage.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_status(cfg: Config, conn, args) -> int:
    st = db.get_state(conn, "poller", {}) or {}
    last_ok = conn.execute("SELECT * FROM polls WHERE ok = 1 ORDER BY polled_at DESC, id DESC LIMIT 1").fetchone()
    last_fail = conn.execute("SELECT * FROM polls WHERE ok = 0 ORDER BY polled_at DESC, id DESC LIMIT 1").fetchone()
    now = utcnow()

    print(f"Claude usage tracker  (auth: {cfg.auth_mode}, db: {cfg.db_path})")
    ok_at = st.get("last_ok_at") or (last_ok["polled_at"] if last_ok else None)
    if ok_at:
        print(f"Last successful poll : {_local(cfg, ok_at)} ({fmt_age((now - parse_iso(ok_at)).total_seconds())} ago)")
    else:
        print("Last successful poll : never")
    if last_ok:
        print(f"Session (5h)         : {_pct(last_ok['session_pct'])}  resets {_local(cfg, last_ok['session_resets_at'])} {_until(last_ok['session_resets_at'])}")
        print(f"Weekly (7d)          : {_pct(last_ok['weekly_pct'])}  resets {_local(cfg, last_ok['weekly_resets_at'])} {_until(last_ok['weekly_resets_at'])}")
        for b in conn.execute("SELECT * FROM poll_buckets WHERE poll_id = ? ORDER BY bucket_name", (last_ok["id"],)):
            print(f"  {b['bucket_name']:<19}: {_pct(b['pct'])}")
        try:
            raw = json.loads(last_ok["raw_json"] or "{}")
        except ValueError:
            raw = {}
        plan = detect_plan(raw)
        extra = raw.get("extra_usage") if isinstance(raw, dict) and isinstance(raw.get("extra_usage"), dict) else {}
        if last_ok["credits_spent_month"] is not None:
            bal = f", ${last_ok['credits_balance']:.2f} remaining" if last_ok["credits_balance"] is not None else ""
            off = (f"  (extra usage disabled: {extra.get('disabled_reason') or 'off'})"
                   if extra.get("is_enabled") is False else "")
            print(f"Usage credits        : ${last_ok['credits_spent_month']:.2f} spent this month{bal}{off}")
        if plan:
            print(f"Plan (heuristic)     : {plan}")
    if st.get("interval_s") is not None:
        ran = parse_iso(st.get("last_poll_at"))
        age = (now - ran).total_seconds() if ran else None
        stale = "  ** poller looks stopped **" if age is not None and age > max(3 * st["interval_s"], 600) else ""
        print(f"Poll interval        : {int(st['interval_s'])}s (last attempt {fmt_age(age) if age is not None else '-'} ago){stale}")
    print(f"Consecutive failures : {st.get('consecutive_failures', 0)}"
          + (f"  (last error: {st['last_error']})" if st.get("last_error") else ""))
    if last_fail:
        days = (now - parse_iso(last_fail["polled_at"])).total_seconds() / 86400
        print(f"Days since failure   : {days:.1f}  ({_local(cfg, last_fail['polled_at'])}: {last_fail['error']})")
    else:
        print("Days since failure   : no failures recorded")
    alerts = conn.execute("SELECT key, detail, last_alert_at FROM alert_state WHERE active = 1").fetchall()
    print("Open alerts          : " + (", ".join(f"{a['key']} ({a['detail']})" for a in alerts) if alerts else "none"))
    recent = conn.execute("SELECT polled_at, interval_s FROM polls ORDER BY id DESC LIMIT 8").fetchall()
    if recent:
        print("Recent intervals     : " + "  ".join(
            f"{_local(cfg, r['polled_at'], '%H:%M')}→{int(r['interval_s'] or 0)}s" for r in reversed(recent)))
    synced = conn.execute("SELECT MAX(synced_at) AS s, SUM(needs_sync) AS pending FROM daily").fetchone()
    print(f"Last Notion sync     : {_local(cfg, synced['s'])}  ({synced['pending'] or 0} day(s) pending)")
    return 0


def _pct(v) -> str:
    return f"{v:5.1f}%" if v is not None else "    -"


def cmd_rollup(cfg: Config, conn, args) -> int:
    st = db.get_state(conn, "poller", {}) or {}
    last = parse_iso(st.get("last_poll_at"))
    if last is None or (utcnow() - last).total_seconds() > STALE_POLLER_S:
        Alerter(conn, cfg).send(f"Claude usage tracker: the poller has not run since {last or 'ever'}. "
                                "Check `systemctl status claude-usage-poller`.")
    days = args.day or None
    if args.all:
        days = _local_days_with_polls(cfg, conn)
    changed = rollup(conn, cfg, days)
    print(f"rollup: {len(changed)} day(s) changed: {', '.join(changed) or '-'}")
    return 0


def _local_days_with_polls(cfg: Config, conn) -> list[str]:
    first = conn.execute("SELECT MIN(polled_at) FROM polls").fetchone()[0]
    if not first:
        return []
    start = parse_iso(first).astimezone(cfg.tz).date()
    yesterday = utcnow().astimezone(cfg.tz).date() - timedelta(days=1)
    return [(start + timedelta(days=i)).isoformat() for i in range((yesterday - start).days + 1)]


def cmd_sync(cfg: Config, conn, args) -> int:
    notion = Notion(cfg.notion_token, cfg.notion_database_id, data_source_id=cfg.notion_data_source_id)
    if args.check:
        problems = notion.verify_schema()
        if problems:
            print("Notion database problems:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            return 2
        print("Notion database OK")
        return 0
    n = notion_sync(conn, notion, force=args.force)
    print(f"sync: {n} day(s) upserted to Notion")
    return 0


def cmd_cc_tokens(cfg: Config, conn, args) -> int:
    if cfg.cc_log_dir is None:
        log.info("CLAUDE_CODE_LOG_DIR not set; skipping Claude Code token scan")
        return 0
    n = cctokens.run(conn, cfg)
    print(f"cc-tokens: {n} day(s) updated")
    return 0


def cmd_backfill_parse(cfg: Config, conn, args) -> int:
    n = warned = 0
    for row in conn.execute("SELECT id, raw_json FROM polls WHERE ok = 1 AND raw_json IS NOT NULL ORDER BY id").fetchall():
        try:
            data = json.loads(row["raw_json"])
        except ValueError:
            log.warning("poll %s: raw_json is not valid JSON; skipped", row["id"])
            continue
        usage = parse_usage(data)
        write_usage_columns(conn, row["id"], usage)
        n += 1
        warned += bool(usage.warnings)
    conn.commit()
    print(f"backfill-parse: re-parsed {n} poll(s), {warned} with warnings. "
          "Now run `rollup --all` then `sync` to refresh daily rows and Notion.")
    return 0


def cmd_test_alert(cfg: Config, conn, args) -> int:
    ok = Alerter(conn, cfg).send("Claude usage tracker: test alert. Slack webhook is working.")
    print("sent" if ok else "failed (see log)")
    return 0 if ok else 1


def cmd_notify_unit_failure(cfg: Config, conn, args) -> int:
    Alerter(conn, cfg).send(f"Claude usage tracker: systemd unit `{args.unit}` failed. "
                            f"Check `journalctl -u {args.unit} -n 50`.")
    return 0


COMMANDS = {
    "poll": cmd_poll, "status": cmd_status, "rollup": cmd_rollup, "sync": cmd_sync,
    "cc-tokens": cmd_cc_tokens, "backfill-parse": cmd_backfill_parse,
    "test-alert": cmd_test_alert, "notify-unit-failure": cmd_notify_unit_failure,
}
# Nightly jobs alert on Slack when they raise, so a broken job is never silent.
ALERT_ON_ERROR = {"rollup", "sync", "cc-tokens", "backfill-parse"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude_usage.py", description="Track Claude plan usage.")
    p.add_argument("--env-file", help="path to .env (default /opt/claude-usage/.env)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("poll", help="poll usage once, or run the adaptive poller with --daemon")
    sp.add_argument("--daemon", action="store_true")
    sp.add_argument("--print-raw", action="store_true", help="print the raw JSON response (one-shot only)")
    sp.add_argument("--no-alert", action="store_true", help="don't send Slack alerts")

    sub.add_parser("status", help="show the latest readings and poller health")

    sp = sub.add_parser("rollup", help="compute daily rows")
    sp.add_argument("--day", action="append", help="YYYY-MM-DD; repeatable. Default: yesterday + changed days")
    sp.add_argument("--all", action="store_true", help="recompute every day since the first poll")

    sp = sub.add_parser("sync", help="upsert daily rows to Notion")
    sp.add_argument("--check", action="store_true", help="only verify the database properties")
    sp.add_argument("--force", action="store_true", help="re-send every day, not just changed ones")

    sub.add_parser("cc-tokens", help="sum Claude Code tokens per day (needs CLAUDE_CODE_LOG_DIR)")
    sub.add_parser("backfill-parse", help="re-parse raw_json of all polls after a parser fix")
    sub.add_parser("test-alert", help="send a test Slack message")
    sp = sub.add_parser("notify-unit-failure", help=argparse.SUPPRESS)
    sp.add_argument("unit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = config_mod.load(args.env_file)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    try:
        for d in getattr(args, "day", None) or []:
            date.fromisoformat(d)
    except ValueError:
        log.error("--day must be YYYY-MM-DD")
        return 2
    conn = db.connect(cfg.db_path)
    try:
        return COMMANDS[args.command](cfg, conn, args)
    except NotionError as exc:
        log.error("%s", exc)
        if args.command in ALERT_ON_ERROR:
            Alerter(conn, cfg).job_failed(args.command, str(exc).splitlines()[0])
        return 1
    except Exception as exc:
        log.exception("%s failed", args.command)
        if args.command in ALERT_ON_ERROR:
            Alerter(conn, cfg).job_failed(args.command, f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()
