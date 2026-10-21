import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from cusage import cctokens, dashboard, db, estimate, events
from cusage.poller import insert_poll
from cusage.parse import parse_usage
from tests.helpers import make_cfg, make_conn, ok_result, t

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "usage_2026-10-20.json").read_text())


def add_poll(conn, when, data):
    insert_poll(conn, when, ok_result(data), parse_usage(data), 30, "cookie")


def variant(weekly=None, reset=None, allowance=None, cap=None, drop_fable=False, extra_key=None, cc_share=None):
    d = copy.deepcopy(FIXTURE)
    if weekly is not None:
        d["seven_day"]["utilization"] = weekly
    if reset is not None:
        d["seven_day"]["resets_at"] = reset
    if allowance is not None:
        d["weekly_scoped_shares"][0]["allowance_percent_of_weekly"] = allowance
    if cap is not None:
        d["extra_usage"]["monthly_limit"] = cap
    if drop_fable:
        d["limits"] = d["limits"][:2]
        d["weekly_scoped_shares"] = []
    if extra_key:
        d[extra_key] = None
    if cc_share is not None:
        d["seven_day_breakdown"]["rows"][0]["percent"] = cc_share
    return d


class EventTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.cfg = make_conn(), make_cfg()
        self.t0 = t("2026-10-20T12:00:00")

    def run_events(self):
        new = events.update(self.conn, self.cfg)
        self.conn.commit()
        return [(e["kind"], e["severity"]) for e in new]

    def test_baseline_then_changes(self):
        add_poll(self.conn, self.t0, FIXTURE)
        self.assertEqual(self.run_events(), [("tracking_started", "info")])
        add_poll(self.conn, self.t0 + timedelta(hours=1), variant(allowance=30, cap=2000, extra_key="new_codename"))
        got = self.run_events()
        self.assertIn(("allowance_changed", "limit"), got)
        self.assertIn(("extra_cap_changed", "info"), got)
        self.assertIn(("api_fields_changed", "info"), got)
        self.assertEqual(self.run_events(), [])  # idempotent

    def test_removed_limit_debounced_24h(self):
        add_poll(self.conn, self.t0, FIXTURE)
        add_poll(self.conn, self.t0 + timedelta(hours=1), variant(drop_fable=True))
        add_poll(self.conn, self.t0 + timedelta(hours=2), FIXTURE)          # flap back: no event
        add_poll(self.conn, self.t0 + timedelta(hours=3), variant(drop_fable=True))
        add_poll(self.conn, self.t0 + timedelta(hours=28), variant(drop_fable=True))
        kinds = [k for k, _ in self.run_events()]
        self.assertEqual(kinds.count("limit_removed"), 1)
        self.assertNotIn("limit_added", kinds)
        add_poll(self.conn, self.t0 + timedelta(hours=29), FIXTURE)
        self.assertEqual(self.run_events(), [("limit_added", "limit")])

    def test_weekly_reset(self):
        add_poll(self.conn, self.t0, FIXTURE)                                # resets 2026-10-22T18:00
        add_poll(self.conn, t("2026-10-22T18:05:00"), variant(reset="2026-10-29T18:00:00+00:00"))  # normal rollover
        self.run_events()
        add_poll(self.conn, t("2026-10-23T09:00:00"), variant(reset="2026-10-30T09:00:00+00:00"))  # moved mid-window
        self.assertEqual(self.run_events(), [("weekly_reset_moved", "limit")])

    def test_rebuild(self):
        add_poll(self.conn, self.t0, FIXTURE)
        add_poll(self.conn, self.t0 + timedelta(hours=1), variant(allowance=40))
        self.run_events()
        self.assertEqual(events.rebuild(self.conn, self.cfg), 2)


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.cfg = make_conn(), make_cfg()

    def add_tokens(self, start, hours, per_hour_out, model="claude-fable-5"):
        rows = []
        for i in range(hours):
            h = cctokens.hour_of((start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            rows.append({"hour": h, "model": model, "input_tokens": 0, "output_tokens": per_hour_out,
                         "cache_read_tokens": 1000, "cache_write_tokens": 0, "messages": 1})
        cctokens.store_hourly(self.conn, self.cfg, "mac", {r["hour"] for r in rows}, rows)

    def week(self, reset, weekly, share, tokens_per_hour):
        end = t(reset)
        start = end - timedelta(days=7)
        add_poll(self.conn, start + timedelta(minutes=5), variant(weekly=1, reset=reset + "+00:00", cc_share=share))
        add_poll(self.conn, end - timedelta(minutes=5), variant(weekly=weekly, reset=reset + "+00:00", cc_share=share))
        self.add_tokens(start, 24 * 7, tokens_per_hour)

    def test_implied_limit_and_shift_flag(self):
        # 168h x 1000 output tokens = 168k CC tokens; 40% weekly x 50% CC share = 20% -> 840k at 100%
        self.week("2026-09-24T18:00:00", 40, 50, 1000)
        self.week("2026-10-01T18:00:00", 40, 50, 1000)
        self.week("2026-10-08T18:00:00", 40, 50, 1000)
        self.week("2026-10-15T18:00:00", 80, 50, 1000)  # same tokens, double the % -> limit halved
        res = estimate.windows(self.conn, now=t("2026-10-20T12:00:00"))
        ws = res["windows"]
        self.assertEqual(ws[0]["implied"]["output"], 840000)
        self.assertIsNone(ws[2]["flag"])
        self.assertIsNotNone(ws[3]["flag"])
        self.assertIn("down 50%", ws[3]["flag"]["message"])

    def test_pace(self):
        add_poll(self.conn, t("2026-10-19T18:00:00"), variant(weekly=33))  # window 10-15..10-22, 4 of 7 days gone
        res = estimate.windows(self.conn, now=t("2026-10-19T18:00:00"))
        self.assertAlmostEqual(res["pace"]["projected_pct"], 57.8, places=0)

    def test_family(self):
        self.assertEqual([estimate.family(m) for m in ("claude-opus-4-7", "claude-fable-5-1", "claude-haiku-5", "x", None)],
                         ["opus", "fable", "haiku", "other", "other"])


class PushIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name) / "projects"
        self.logs.mkdir()
        (self.logs / "a.jsonl").write_text("\n".join(json.dumps({
            "type": "assistant", "uuid": f"u{i}", "sessionId": "s", "timestamp": f"2026-10-20T15:{i:02d}:00Z",
            "message": {"id": f"m{i}", "model": "claude-fable-5", "content": [{"type": "text", "text": "SECRET CODE"}],
                        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                                  "cache_creation_input_tokens": 1}}}) for i in range(3)) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_push_sends_counts_only_and_pi_ingests(self):
        mac_conn = make_conn()
        mac_cfg = make_cfg(cc_log_dir=self.logs, cc_push_url="http://pi/api/cc-hourly", cc_push_token="tok", cc_source_name="mac")
        sent = []
        cctokens.push(mac_conn, mac_cfg, everything=True, send=lambda url, tok, payload: sent.append(payload))
        self.assertEqual(len(sent), 1)
        self.assertNotIn("SECRET", json.dumps(sent[0]))
        pi_conn, pi_cfg = make_conn(), make_cfg()
        for _ in range(2):  # re-sending the same hours replaces, never double counts
            cctokens.ingest(pi_conn, pi_cfg, sent[0])
        row = pi_conn.execute("SELECT * FROM daily WHERE day = '2026-10-20'").fetchone()
        self.assertEqual((row["cc_input_tokens"], row["cc_output_tokens"], row["cc_cache_read_tokens"]), (30, 15, 300))

    def test_ingest_validation(self):
        conn, cfg = make_conn(), make_cfg()
        good = {"source": "mac", "hours": ["2026-10-20T15:00:00Z"], "rows": [
            {"hour": "2026-10-20T15:00:00Z", "model": "m", "input_tokens": 1, "output_tokens": 1,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "messages": 1}]}
        for bad in ({**good, "source": "local"}, {**good, "hours": ["2026-10-20T15:30:00Z"]},
                    {**good, "rows": [{**good["rows"][0], "input_tokens": -5}]},
                    {**good, "rows": [{**good["rows"][0], "hour": "2026-10-21T00:00:00Z"}]}, []):
            with self.assertRaises(ValueError):
                cctokens.ingest(conn, cfg, bad)
        self.assertEqual(cctokens.ingest(conn, cfg, good), 1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = make_cfg(db_path=Path(cls.tmp.name) / "u.db", cc_ingest_token="s3cret-token-value")
        conn = db.connect(cls.cfg.db_path)
        add_poll(conn, t("2026-10-20T12:00:00"), FIXTURE)
        events.update(conn, cls.cfg)
        conn.commit()
        conn.close()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(cls.cfg))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.headers, r.read()

    def post(self, body, token):
        req = urllib.request.Request(self.base + "/api/cc-hourly", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_pages_and_apis(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        for path in ("/dashboard.js", "/dashboard.css", "/assets/quota-mark.svg", "/favicon.ico"):
            self.assertEqual(self.get(path)[0], 200, path)
        summary = json.loads(self.get("/api/summary")[2])
        self.assertEqual(summary["latest"]["weekly_pct"], 33.0)
        self.assertEqual(summary["latest"]["buckets"][0]["label"], "Fable weekly limit")
        series = json.loads(self.get("/api/series?range=all")[2])
        self.assertEqual(series["weekly"][0][1], 33.0)
        self.assertEqual(json.loads(self.get("/api/events")[2])["events"][0]["kind"], "tracking_started")
        for path in ("/api/daily?range=7d", "/api/estimate"):
            self.assertEqual(self.get(path)[0], 200)

    def test_no_path_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/../cusage/config.py")
        self.assertEqual(cm.exception.code, 404)

    def test_ingest_requires_token(self):
        body = {"source": "mac", "hours": [], "rows": []}
        self.assertEqual(self.post(body, "wrong"), 401)
        self.assertEqual(self.post(body, "s3cret-token-value"), 200)


if __name__ == "__main__":
    unittest.main()
