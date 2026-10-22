import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from flask import Flask, jsonify

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deployment"))
from automation_control import Control, IST, install_automation


class AutomationTests(unittest.TestCase):
    def test_github_has_no_automatic_schedules(self):
        workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
        for path in workflows.glob('*.yml'):
            self.assertNotIn('  schedule:', path.read_text())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "reports").mkdir()
        self.clock = datetime(2026, 10, 19, 9, 35, tzinfo=IST)
        self.send = Mock(return_value=101)
        self.control = Control(self.root, {"SCANNER_TRIGGER_SECRET": "dummy-test-secret"}, lambda: self.clock, self.send)

    def report(self, **updates):
        data = {"timestamp": self.clock.isoformat(), "mode": "morning", "outcome": "NO TRADE", "reason": "fixture only", "coverage": {"fresh_quotes": 498}, "watches": []}
        data.update(updates)
        (self.root / "reports/morning_20261019_092500.json").write_text(json.dumps(data))

    def test_persisted_daily_claim_blocks_duplicates_after_restart(self):
        self.assertIsNone(self.control.claim("morning"))
        other = Control(self.root, {}, lambda: self.clock, self.send)
        self.assertEqual(other.claim("morning")[0]["status"], "already_requested")

    def test_cross_process_claim_is_atomic(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.control.claim("morning"))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(x is None for x in results), 1)
        self.assertEqual(len(results), 8)

    def test_late_trigger_does_not_run(self):
        self.clock = self.clock.replace(hour=13, minute=19)
        self.assertEqual(self.control.claim("midday")[1], 409)

    def test_early_morning_trigger_does_not_consume_daily_claim(self):
        self.clock=self.clock.replace(minute=25)
        self.assertEqual(self.control.claim('morning')[1],409)
        self.assertEqual(self.control.check('morning')[0]['status'],'idle')
        self.send.assert_not_called()
        self.clock=self.clock.replace(minute=35)
        self.assertIsNone(self.control.claim('morning'))

    def test_report_explains_rejections_and_watch_only_policy(self):
        self.control.claim('morning')
        self.report(test_data=False,setup_type='OPENING_RANGE_ONLY',
                    policy_note='WATCH-only; TRADE disabled',
                    rejection_summary=[{'reason':'No identified target','count':17}])
        self.control.check('morning')
        message=self.send.call_args.args[0]
        self.assertIn('LIVE DATA (scanner-reported)',message)
        self.assertIn('Rejected 17: No identified target',message)
        self.assertIn('WATCH-only; TRADE disabled',message)
        self.assertIn('separate midday strategy unavailable',message)

    def test_rejection_detail_secrets_are_redacted(self):
        self.control.claim('morning')
        self.report(rejection_summary=[{'reason':'dummy-test-secret','count':1}])
        self.control.check('morning')
        message=self.send.call_args.args[0]
        self.assertNotIn('dummy-test-secret',message)
        self.assertIn('[redacted]',message)

    def test_weekend_no_scan_or_missing_alert(self):
        self.clock = self.clock.replace(day=24)
        self.assertEqual(self.control.claim("morning")[1], 409)
        self.assertEqual(self.control.check("morning")[0]["status"], "idle")
        self.send.assert_not_called()

    def test_report_sent_once_across_repeated_checks(self):
        self.control.claim("morning")
        self.report()
        self.assertEqual(self.control.check("morning")[0]["telegram"], "sent")
        self.assertEqual(self.control.check("morning")[0]["telegram"], "sent")
        self.send.assert_called_once()

    def test_missing_trigger_alert_after_deadline(self):
        self.clock += timedelta(minutes=11)
        body, status = self.control.check("morning")
        self.assertEqual(body["status"], "missing")
        self.assertEqual(status, 200)
        self.assertIn("no trigger recorded", self.send.call_args.args[0])

    def test_invalid_report_is_never_announced_as_no_trade(self):
        self.control.claim("morning")
        self.report(timestamp="2026-10-19T09:25:00")
        self.clock += timedelta(minutes=11)
        self.assertEqual(self.control.check("morning")[0]["status"], "missing")
        self.assertIn("invalid or incomplete", self.send.call_args.args[0])

    def test_old_report_does_not_satisfy_new_run(self):
        self.control.claim("morning")
        self.report(timestamp=(self.clock - timedelta(minutes=5)).isoformat())
        self.assertEqual(self.control.check("morning")[0]["status"], "waiting")
        self.send.assert_not_called()

    def test_future_report_is_rejected(self):
        self.control.claim("morning")
        self.report(timestamp=(self.clock + timedelta(minutes=5)).isoformat())
        self.clock += timedelta(minutes=11)
        # Now it is no longer future-dated; use a still-future stamp.
        self.report(timestamp=(self.clock + timedelta(minutes=5)).isoformat())
        self.assertEqual(self.control.check("morning")[0]["status"], "missing")

    def test_delivery_failure_is_retried_without_another_scan(self):
        self.control.claim("morning")
        self.report()
        self.send.side_effect = [RuntimeError("fixture"), 123]
        self.assertEqual(self.control.check("morning")[1], 503)
        self.assertEqual(self.control.check("morning")[1], 200)
        self.assertEqual(self.control.claim("morning")[0]["status"], "already_requested")

    def test_delayed_report_explicitly_not_current(self):
        self.control.claim("morning")
        self.report()
        self.clock += timedelta(minutes=25)
        self.assertTrue(self.control.check("morning")[0]["delayed"])
        self.assertIn("NOT A CURRENT SIGNAL", self.send.call_args.args[0])

    def test_report_recovery_after_missing_alert(self):
        self.control.claim("morning")
        self.clock += timedelta(minutes=11)
        self.control.check("morning")
        self.report()
        self.assertEqual(self.control.check("morning")[0]["status"], "completed")
        self.assertEqual(self.send.call_count, 2)

    def test_http_auth_and_duplicate_guard(self):
        app = Flask(__name__)
        runner = Mock(return_value=({"status": "accepted", "mode": "test"}, 202))
        app.add_url_rule("/scan/<mode>", "trigger_scan", lambda mode: runner(mode), methods=["POST"])
        control = install_automation(app, self.root, {"SCANNER_TRIGGER_SECRET": "dummy-test-secret"})
        control.now = lambda: self.clock
        client = app.test_client()
        self.assertEqual(client.post("/scan/test").status_code, 401)
        self.assertEqual(client.post("/automation/check/test").status_code, 401)
        headers = {"X-Scanner-Secret": "dummy-test-secret"}
        self.assertEqual(client.post("/scan/test", headers=headers).status_code, 202)
        self.assertEqual(client.post("/scan/test", headers=headers).status_code, 200)
        self.assertEqual(client.get("/automation/health", headers=headers).json["status"], "installed")
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()

