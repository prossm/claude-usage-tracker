import unittest
from datetime import timedelta

from cusage.alerts import Alerter
from cusage.client import AUTH, NETWORK, FetchResult
from cusage.parse import parse_usage
from cusage.poller import AdaptiveInterval, Poller, should_persist
from tests.helpers import FakeAuth, Outbox, make_cfg, make_conn, ok_result, t, usage_json

NOW = t("2026-10-20T12:00:00")


class AdaptiveIntervalTests(unittest.TestCase):
    def test_backoff_and_snap_back(self):
        a = AdaptiveInterval(fast=30, ceiling=300, idle_threshold=3, burst_window=120)
        seq = [a.on_success(10, 20, [], NOW)]           # first poll counts as change
        seq += [a.on_success(10, 20, [], NOW) for _ in range(7)]
        self.assertEqual(seq, [30, 30, 30, 60, 120, 240, 300, 300])
        self.assertEqual(a.on_success(11, 20, [], NOW), 30)  # any change snaps back

    def test_weekly_change_counts(self):
        a = AdaptiveInterval(30, 300, 1, 120)
        a.on_success(10, 20, [], NOW)
        self.assertEqual(a.on_success(10, 20, [], NOW), 60)
        self.assertEqual(a.on_success(10, 21, [], NOW), 30)

    def test_reset_within_window_is_fast(self):
        a = AdaptiveInterval(30, 300, 1, 120)
        a.on_success(10, 20, [], NOW)
        a.on_success(10, 20, [], NOW)
        self.assertEqual(a.on_success(10, 20, [NOW + timedelta(seconds=90)], NOW), 30)
        self.assertEqual(a.on_success(10, 20, [NOW - timedelta(seconds=10)], NOW), 60)  # past resets don't count

    def test_sleep_lands_inside_burst_window(self):
        a = AdaptiveInterval(30, 300, 3, 120)
        a.interval = 300
        # reset in 200s: sleeping 300s would skip the 2-minute window entirely
        self.assertEqual(a.sleep_for([NOW + timedelta(seconds=200)], NOW), 140)
        self.assertEqual(a.sleep_for([NOW + timedelta(hours=3)], NOW), 300)

    def test_failure_backs_off(self):
        a = AdaptiveInterval(30, 300, 3, 120)
        self.assertEqual([a.on_failure() for _ in range(5)], [60, 120, 240, 300, 300])


class PersistTests(unittest.TestCase):
    def test_should_persist(self):
        k = parse_usage(usage_json()).change_key()
        self.assertTrue(should_persist(k, None, None, NOW, 900))
        self.assertFalse(should_persist(k, k, NOW - timedelta(minutes=5), NOW, 900))
        self.assertTrue(should_persist(k, k, NOW - timedelta(minutes=15), NOW, 900))
        k2 = parse_usage(usage_json(session=11)).change_key()
        self.assertTrue(should_persist(k2, k, NOW, NOW, 900))

    def test_poller_persists_changes_heartbeat_and_failures(self):
        conn, out = make_conn(), Outbox()
        cfg = make_cfg()
        same = ok_result(usage_json())
        auth = FakeAuth([same, same, same, ok_result(usage_json(session=12)),
                         FetchResult(NETWORK, None, "network error"), same])
        p = Poller(conn, cfg, auth, Alerter(conn, cfg, out))
        p.poll_once()
        p.poll_once()
        p.poll_once()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 1)
        p.poll_once()   # change
        p.poll_once()   # failure
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 3)
        p.last_persisted_at -= timedelta(minutes=20)
        p.last_key = parse_usage(usage_json()).change_key()
        p.poll_once()   # unchanged vs last persisted but heartbeat due
        rows = conn.execute("SELECT ok, session_pct, raw_json IS NOT NULL AS has_raw, interval_s FROM polls ORDER BY id").fetchall()
        self.assertEqual([(r["ok"], r["session_pct"], r["has_raw"]) for r in rows],
                         [(1, 10.0, 1), (1, 12.0, 1), (0, None, 0), (1, 10.0, 1)])
        self.assertTrue(all(r["interval_s"] for r in rows))
        tally = conn.execute("SELECT ok, failed FROM poll_tally").fetchone()
        self.assertEqual(tuple(tally), (5, 1))
        self.assertEqual(out.sent, [])

    def test_secret_never_stored(self):
        conn, cfg = make_conn(), make_cfg()
        auth = FakeAuth([FetchResult(AUTH, 403, "rejected sessionKey=sk-ant-sid01-TESTSECRET")])
        Poller(conn, cfg, auth, Alerter(conn, cfg, Outbox())).poll_once()
        dump = "\n".join(conn.iterdump())
        self.assertNotIn("TESTSECRET", dump)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.cfg, self.out = make_conn(), make_cfg(), Outbox()
        self.alerter = Alerter(self.conn, self.cfg, self.out)

    def fail(self, n, kind=AUTH, now=NOW):
        self.alerter.on_poll(FetchResult(kind, 403 if kind == AUTH else None, "x"), None, n, now)

    def ok(self, usage=None, now=NOW):
        self.alerter.on_poll(ok_result({}), usage or parse_usage(usage_json()), 0, now)

    def test_garbage_key_one_alert_then_one_recovery(self):
        for i in range(1, 30):
            self.fail(i, now=NOW + timedelta(minutes=5 * i))
        self.assertEqual(len(self.out.sent), 1)
        self.assertIn("session key has likely expired", self.out.sent[0])
        self.ok()
        self.ok()
        self.assertEqual(len(self.out.sent), 2)
        self.assertIn("recovered", self.out.sent[1])

    def test_auth_reminder_after_12h(self):
        self.fail(1)
        self.fail(2, now=NOW + timedelta(hours=11))
        self.fail(3, now=NOW + timedelta(hours=12, minutes=1))
        self.assertEqual(len(self.out.sent), 2)
        self.assertIn("still failing", self.out.sent[1])

    def test_other_failures_alert_on_third(self):
        self.fail(1, NETWORK)
        self.fail(2, NETWORK)
        self.assertEqual(self.out.sent, [])
        self.fail(3, NETWORK)
        self.assertEqual(len(self.out.sent), 1)

    def test_no_recovery_message_if_never_alerted(self):
        self.fail(1, NETWORK)
        self.ok()
        self.assertEqual(self.out.sent, [])
        self.assertIsNone(self.conn.execute("SELECT * FROM alert_state WHERE key='failure'").fetchone())

    def test_slack_down_retries_alert(self):
        self.out.ok = False
        self.fail(1)
        self.out.ok = True
        self.fail(2)
        self.fail(3)
        self.assertEqual(len(self.out.sent), 2)  # one failed attempt, one delivered, then quiet

    def test_schema_alert_once(self):
        data = usage_json()
        del data["five_hour"]
        for _ in range(3):
            self.ok(parse_usage(data))
        self.assertEqual(len(self.out.sent), 1)
        self.assertIn("changed shape", self.out.sent[0])

    def test_weekly_threshold_once_per_window(self):
        self.cfg.alert_weekly_pct = 80
        self.ok(parse_usage(usage_json(weekly=79)))
        self.ok(parse_usage(usage_json(weekly=81)))
        self.ok(parse_usage(usage_json(weekly=90)))
        self.assertEqual(len(self.out.sent), 1)
        self.ok(parse_usage(usage_json(weekly=85, weekly_reset="2026-10-31T13:00:00+00:00")))
        self.assertEqual(len(self.out.sent), 2)

    def test_state_survives_restart(self):
        self.fail(1)
        fresh = Alerter(self.conn, self.cfg, self.out)
        fresh.on_poll(FetchResult(AUTH, 403, "x"), None, 2, NOW + timedelta(hours=1))
        self.assertEqual(len(self.out.sent), 1)


if __name__ == "__main__":
    unittest.main()
