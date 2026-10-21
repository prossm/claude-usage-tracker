import json
import unittest
from pathlib import Path

from cusage.client import AUTH, NETWORK, OK, OTHER, SERVER, CookieAuth, HttpResponse, classify, get_json
from cusage.parse import detect_plan, parse_usage
from cusage.util import register_secret, scrub
from tests.helpers import make_cfg, usage_json


class ParseTests(unittest.TestCase):
    def test_full_response(self):
        u = parse_usage(usage_json(session=42.5, weekly=61, used_cents=250, opus=3))
        self.assertEqual(u.session_pct, 42.5)
        self.assertEqual(u.weekly_pct, 61.0)
        self.assertEqual(u.session_resets_at, "2026-10-20T20:00:00Z")
        self.assertEqual(u.credits_spent_month, 2.5)
        self.assertEqual(u.credits_balance, 97.5)
        self.assertEqual([(b.name, b.pct) for b in u.buckets], [("seven_day_opus", 3.0)])
        self.assertEqual(u.warnings, [])

    def test_missing_expected_key_warns_and_stores_null(self):
        data = usage_json()
        del data["seven_day"]
        u = parse_usage(data)
        self.assertIsNone(u.weekly_pct)
        self.assertEqual(u.session_pct, 10.0)
        self.assertEqual(u.warnings, ["missing key 'seven_day'"])

    def test_null_window_is_not_schema_change(self):
        data = usage_json()
        data["five_hour"] = None
        u = parse_usage(data)
        self.assertIsNone(u.session_pct)
        self.assertEqual(u.warnings, [])

    def test_renamed_field_warns(self):
        data = usage_json()
        data["five_hour"] = {"used": 5}
        self.assertIn("'five_hour' has no utilization field", parse_usage(data).warnings)

    def test_legacy_utilization_pct_accepted(self):
        data = usage_json()
        data["five_hour"] = {"utilization_pct": 7}
        self.assertEqual(parse_usage(data).session_pct, 7.0)

    def test_oauth_spend_variant(self):
        data = usage_json()
        data["spend"] = {"used": {"amount_minor": 1234, "exponent": 2}}
        self.assertEqual(parse_usage(data).credits_spent_month, 12.34)

    def test_garbage(self):
        self.assertEqual(parse_usage([1, 2]).warnings, ["response is not a JSON object"])

    def test_plan_hint(self):
        self.assertEqual(detect_plan(usage_json(used_cents=0, limit_cents=20000)), "Max 20x")
        self.assertIsNone(detect_plan(usage_json()))

    def test_resets_at_jitter_does_not_change_key(self):
        a = parse_usage(usage_json(session_reset="2026-10-20T20:00:00.1+00:00")).change_key()
        b = parse_usage(usage_json(session_reset="2026-10-20T20:00:00.9+00:00")).change_key()
        self.assertEqual(a, b)


class LiveFixtureTests(unittest.TestCase):
    """The real claude.ai response captured on the Pi on 2026-10-20."""

    def setUp(self):
        self.data = json.loads((Path(__file__).parent / "fixtures" / "usage_2026-10-20.json").read_text())

    def test_live_response(self):
        u = parse_usage(self.data)
        self.assertEqual((u.session_pct, u.weekly_pct), (6.0, 33.0))
        self.assertEqual((u.session_resets_at, u.weekly_resets_at), ("2026-10-21T06:00:00Z", "2026-10-22T18:00:00Z"))
        self.assertEqual([(b.name, b.pct, b.resets_at) for b in u.buckets],
                         [("seven_day_fable", 0.0, "2026-10-22T18:00:00Z")])
        self.assertEqual(u.credits_spent_month, 0.0)
        self.assertEqual(u.warnings, [])
        self.assertIsNone(detect_plan(self.data))  # monthly_limit is a user cap, not a plan

    def test_top_level_model_key_wins_over_limits_entry(self):
        self.data["seven_day_fable"] = {"utilization": 12.0, "resets_at": "2026-10-22T18:00:00+00:00"}
        buckets = parse_usage(self.data).buckets
        self.assertEqual([(b.name, b.pct) for b in buckets], [("seven_day_fable", 12.0)])

    def test_explicit_balance_preferred(self):
        self.data["spend"]["balance"] = {"amount_minor": 125, "exponent": 2}
        self.assertEqual(parse_usage(self.data).credits_balance, 1.25)


class ClassifyTests(unittest.TestCase):
    def test_html_200_is_auth_failure_not_data(self):
        r = classify(HttpResponse(200, "text/html; charset=utf-8", "<!DOCTYPE html><html>login</html>"))
        self.assertEqual(r.kind, AUTH)
        self.assertIsNone(r.data)

    def test_html_without_content_type(self):
        self.assertEqual(classify(HttpResponse(200, "", "  <html>")).kind, AUTH)

    def test_cloudflare_challenge(self):
        r = classify(HttpResponse(403, "text/html", "<title>Just a moment...</title>", cf_mitigated=True))
        self.assertEqual(r.kind, AUTH)
        self.assertIn("Cloudflare", r.error)

    def test_status_mapping(self):
        self.assertEqual(classify(HttpResponse(401, "application/json", "{}")).kind, AUTH)
        self.assertEqual(classify(HttpResponse(502, "text/plain", "bad")).kind, SERVER)
        self.assertEqual(classify(HttpResponse(404, "application/json", "{}")).kind, OTHER)
        self.assertEqual(classify(HttpResponse(200, "application/json", "not json")).kind, OTHER)
        ok = classify(HttpResponse(200, "application/json", '{"a": 1}'))
        self.assertEqual((ok.kind, ok.data), (OK, {"a": 1}))


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        return False

    def test_no_retry_on_403(self):
        tr = FakeTransport([HttpResponse(403, "application/json", "{}")])
        self.assertEqual(get_json(tr, "u", {}, self.sleep).kind, AUTH)
        self.assertEqual((len(tr.calls), self.sleeps), (1, []))

    def test_one_retry_after_60s_on_5xx(self):
        tr = FakeTransport([HttpResponse(503, "", "x"), HttpResponse(503, "", "x")])
        self.assertEqual(get_json(tr, "u", {}, self.sleep).kind, SERVER)
        self.assertEqual((len(tr.calls), self.sleeps), (2, [60]))

    def test_network_error_then_success(self):
        tr = FakeTransport([OSError("timed out"), HttpResponse(200, "application/json", "{}")])
        self.assertEqual(get_json(tr, "u", {}, self.sleep).kind, OK)
        self.assertEqual(self.sleeps, [60])

    def test_network_error_kind(self):
        tr = FakeTransport([OSError("x"), OSError("y")])
        self.assertEqual(get_json(tr, "u", {}, self.sleep).kind, NETWORK)


class CookieAuthTests(unittest.TestCase):
    def test_discovers_chat_org_and_sends_cookie(self):
        orgs = [{"uuid": "api-org", "capabilities": ["api"]}, {"uuid": "chat-org", "capabilities": ["chat", "claude_pro"]}]
        tr = FakeTransport([
            HttpResponse(200, "application/json", json.dumps(orgs)),
            HttpResponse(200, "application/json", json.dumps(usage_json())),
            HttpResponse(200, "application/json", json.dumps(usage_json())),
        ])
        auth = CookieAuth(make_cfg(), tr)
        self.assertTrue(auth.fetch_usage(lambda s: False).ok)
        self.assertTrue(auth.fetch_usage(lambda s: False).ok)
        self.assertTrue(tr.calls[1][0].endswith("/api/organizations/chat-org/usage"))
        self.assertEqual(len(tr.calls), 3)  # org cached
        self.assertEqual(tr.calls[1][1]["Cookie"], "sessionKey=sk-ant-sid01-TESTSECRET")

    def test_missing_key_is_auth_failure(self):
        self.assertEqual(CookieAuth(make_cfg(session_key=""), FakeTransport([])).fetch_usage().kind, AUTH)


class ScrubTests(unittest.TestCase):
    def test_scrub(self):
        register_secret("my-very-secret-notion-token")
        s = scrub("Cookie: sessionKey=sk-ant-sid01-abc; Bearer xyz.123 my-very-secret-notion-token "
                  "https://hooks.slack.com/services/T0/B0/abc")
        for leaked in ("sk-ant-sid01-abc", "xyz.123", "my-very-secret", "T0/B0"):
            self.assertNotIn(leaked, s)


if __name__ == "__main__":
    unittest.main()
