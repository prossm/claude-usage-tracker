import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from cusage import cctokens, cli
from cusage.notion import Notion, NotionError, REQUIRED_PROPERTIES, normalize_id, sync
from cusage.rollup import credits_spent, rollup
from tests.helpers import insert_poll_row, make_cfg, make_conn

TODAY = date(2026, 10, 21)  # yesterday = 2026-10-20 (EDT, UTC-4)


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.cfg = make_conn(), make_cfg()

    def test_credits_reset(self):
        self.assertEqual(credits_spent([5.0, 6.0, 6.0, 7.5]), 2.5)
        self.assertEqual(credits_spent([40.0, 41.0, 0.5, 2.0]), 3.0)  # reset mid-day
        self.assertIsNone(credits_spent([]))

    def test_day_boundaries_and_metrics(self):
        c = self.conn
        insert_poll_row(c, "2026-10-20T03:00:00Z", session=99, weekly=50, credits=4.0)   # previous local day (23:00 EDT)
        insert_poll_row(c, "2026-10-20T05:00:00Z", session=30, weekly=60, credits=4.0)
        insert_poll_row(c, "2026-10-20T15:00:00Z", session=100, weekly=70, credits=5.25)
        insert_poll_row(c, "2026-10-20T20:00:00Z", ok=0)
        insert_poll_row(c, "2026-10-21T03:59:00Z", session=5, weekly=71, credits=6.0)   # 23:59 EDT, still the 20th
        insert_poll_row(c, "2026-10-21T04:01:00Z", session=1, weekly=72, credits=9.0)   # the 21st
        rollup(c, self.cfg, today=TODAY)
        row = c.execute("SELECT * FROM daily WHERE day='2026-10-20'").fetchone()
        self.assertEqual(row["max_session_pct"], 100)
        self.assertEqual(row["max_weekly_pct"], 71)
        self.assertEqual((row["session_limit_hit"], row["weekly_limit_hit"]), (1, 0))
        self.assertEqual(row["credits_spent_day"], 2.0)   # 4.0 baseline -> 6.0
        self.assertEqual(row["overage"], 1)
        self.assertEqual(row["poll_count"], 3)            # no tally rows -> persisted ok polls
        self.assertEqual(row["needs_sync"], 1)

    def test_tally_used_for_poll_count(self):
        insert_poll_row(self.conn, "2026-10-20T15:00:00Z", session=1, weekly=1)
        self.conn.execute("INSERT INTO poll_tally (day, ok, failed) VALUES ('2026-10-20', 412, 2)")
        rollup(self.conn, self.cfg, today=TODAY)
        self.assertEqual(self.conn.execute("SELECT poll_count FROM daily").fetchone()[0], 412)

    def test_recompute_changed_old_day_and_keep_cc_columns(self):
        c = self.conn
        insert_poll_row(c, "2026-10-17T15:00:00Z", session=10, weekly=10)
        insert_poll_row(c, "2026-10-20T15:00:00Z", session=10, weekly=10)
        rollup(c, self.cfg, today=TODAY)
        c.execute("UPDATE daily SET needs_sync=0, cc_input_tokens=123, notion_page_id='p1'")
        rollup(c, self.cfg, today=TODAY)
        self.assertEqual(c.execute("SELECT SUM(needs_sync) FROM daily").fetchone()[0], 0)  # nothing changed
        insert_poll_row(c, "2026-10-17T16:00:00Z", session=55, weekly=10)  # e.g. backfill
        rollup(c, self.cfg, today=TODAY)
        row = c.execute("SELECT * FROM daily WHERE day='2026-10-17'").fetchone()
        self.assertEqual((row["max_session_pct"], row["needs_sync"], row["cc_input_tokens"], row["notion_page_id"]),
                         (55, 1, 123, "p1"))

    def test_no_polls_no_row(self):
        rollup(self.conn, self.cfg, today=TODAY)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0], 0)


class FakeNotion:
    """In-memory Notion API."""

    def __init__(self, props=None, throttle_first=0):
        self.pages = {}
        self.props = props or {k: {"type": v} for k, v in REQUIRED_PROPERTIES.items()}
        self.calls = []
        self.throttle = throttle_first

    def __call__(self, method, url, body, headers):
        path = url.split("/v1", 1)[1]
        self.calls.append((method, path))
        if self.throttle:
            self.throttle -= 1
            return 429, {"Retry-After": "1"}, {}
        if method == "GET" and path.startswith("/databases/"):
            return 200, {}, {"properties": self.props}
        if method == "POST" and path.endswith("/query"):
            day = body["filter"]["title"]["equals"]
            hits = [{"id": pid} for pid, p in self.pages.items() if p["Day"]["title"][0]["text"]["content"] == day]
            return 200, {}, {"results": hits}
        if method == "POST" and path == "/pages":
            pid = f"page-{len(self.pages) + 1}"
            self.pages[pid] = body["properties"]
            return 200, {}, {"id": pid}
        if method == "PATCH":
            pid = path.split("/")[-1]
            if pid not in self.pages:
                return 404, {}, {"message": "not found"}
            self.pages[pid] = body["properties"]
            return 200, {}, {"id": pid}
        return 400, {}, {"message": "unexpected"}


class NotionTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.cfg = make_conn(), make_cfg()
        insert_poll_row(self.conn, "2026-10-19T15:00:00Z", session=10, weekly=10)
        insert_poll_row(self.conn, "2026-10-20T15:00:00Z", session=20, weekly=30)
        self.sleeps = []

    def notion(self, fake):
        return Notion("secret_x", "a" * 32, request=fake, sleep=self.sleeps.append, min_gap=0)

    def test_rollup_and_sync_twice_no_duplicates(self):
        fake = FakeNotion()
        for _ in range(2):
            rollup(self.conn, self.cfg, today=TODAY)
            sync(self.conn, self.notion(fake))
        self.assertEqual(len(fake.pages), 2)
        self.assertEqual(sync(self.conn, self.notion(fake)), 0)  # nothing pending

    def test_lost_page_id_found_by_search_not_recreated(self):
        fake = FakeNotion()
        rollup(self.conn, self.cfg, today=TODAY)
        sync(self.conn, self.notion(fake))
        self.conn.execute("UPDATE daily SET notion_page_id = NULL, needs_sync = 1")
        sync(self.conn, self.notion(fake))
        self.assertEqual(len(fake.pages), 2)

    def test_deleted_page_recreated(self):
        fake = FakeNotion()
        rollup(self.conn, self.cfg, today=TODAY)
        sync(self.conn, self.notion(fake))
        fake.pages.clear()
        sync(self.conn, self.notion(fake), force=True)
        self.assertEqual(len(fake.pages), 2)

    def test_429_backoff(self):
        fake = FakeNotion(throttle_first=2)
        rollup(self.conn, self.cfg, today=TODAY)
        sync(self.conn, self.notion(fake))
        self.assertEqual(self.sleeps[:2], [1.0, 1.0])
        self.assertEqual(len(fake.pages), 2)

    def test_schema_check(self):
        props = {k: {"type": v} for k, v in REQUIRED_PROPERTIES.items()}
        del props["Overage"]
        props["Polls"] = {"type": "rich_text"}
        with self.assertRaises(NotionError) as cm:
            sync(self.conn, self.notion(FakeNotion(props)))
        self.assertIn("missing property 'Overage'", str(cm.exception))
        self.assertIn("'Polls' is rich_text", str(cm.exception))

    def test_normalize_id(self):
        url = "https://www.notion.so/me/1234567890abcdef1234567890abcdef?v=fedcba0987654321fedcba0987654321"
        self.assertEqual(normalize_id(url), "1234567890abcdef1234567890abcdef")
        self.assertEqual(normalize_id("12345678-90ab-cdef-1234-567890abcdef"), "1234567890abcdef1234567890abcdef")


def _assistant(mid, ts, inp, out, cr=0, cw=0, uuid="u"):
    return json.dumps({"type": "assistant", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
                       "message": {"id": mid, "model": "claude-x", "usage": {
                           "input_tokens": inp, "output_tokens": out,
                           "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}})


class CCTokensTests(unittest.TestCase):
    def test_dedupe_streaming_and_across_files_and_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d) / "proj"
            proj.mkdir()
            (proj / "a.jsonl").write_text("\n".join([
                _assistant("m1", "2026-10-20T15:00:00Z", 10, 1),
                _assistant("m1", "2026-10-20T15:00:01Z", 10, 50, cr=100, cw=7),  # final streamed tally wins
                _assistant("m2", "2026-10-21T03:30:00Z", 5, 5),                   # 23:30 EDT on the 20th
                "not json",
                json.dumps({"type": "user", "message": {"content": "hi"}}),
            ]) + "\n")
            (proj / "b.jsonl").write_text(_assistant("m1", "2026-10-20T15:00:01Z", 10, 50, cr=100, cw=7) + "\n")
            conn, cfg = make_conn(), make_cfg(cc_log_dir=Path(d))
            cctokens.run(conn, cfg)
            cctokens.run(conn, cfg)
            os.utime(proj / "a.jsonl", (1, 1))  # force rescan
            cctokens.run(conn, cfg)
            row = conn.execute("SELECT * FROM daily WHERE day='2026-10-20'").fetchone()
            self.assertEqual((row["cc_input_tokens"], row["cc_output_tokens"],
                              row["cc_cache_read_tokens"], row["cc_cache_write_tokens"]), (15, 55, 100, 7))

    def test_cli_skips_when_unset(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("CLAUDE_CODE_LOG_DIR=\n")
            os.environ["CLAUDE_USAGE_DB"] = str(Path(d) / "t.db")
            os.environ.pop("CLAUDE_CODE_LOG_DIR", None)
            try:
                self.assertEqual(cli.main(["--env-file", str(env), "cc-tokens"]), 0)
            finally:
                del os.environ["CLAUDE_USAGE_DB"]


if __name__ == "__main__":
    unittest.main()
