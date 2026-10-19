"""Offline regression checks for missed-window and trigger-failure delivery."""
import importlib.util
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_alert():
    spec = importlib.util.spec_from_file_location("alert", ROOT / "scripts/telegram_report_alert.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeliveryTests(unittest.TestCase):
    def test_operational_failures_notify_without_reading_report(self):
        for status, expected in (("skipped", "SCAN SKIPPED"), ("trigger_failed", "STATUS UNKNOWN")):
            with self.subTest(status=status), patch.dict(os.environ, {"SCAN_MODE": "midday", "SCAN_OPERATION_STATUS": status}, clear=True):
                alert = load_alert()
                with patch.object(alert, "post_telegram") as send, patch.object(alert, "get_report") as read:
                    with self.assertRaises(SystemExit) as raised:
                        alert.main()
                    self.assertEqual(raised.exception.code, 1)
                    self.assertIn(expected, send.call_args.args[0])
                    read.assert_not_called()

    def test_telegram_failure_propagates(self):
        with patch.dict(os.environ, {"SCAN_MODE": "midday", "SCAN_OPERATION_STATUS": "skipped"}, clear=True):
            alert = load_alert()
            with patch.object(alert, "post_telegram", side_effect=RuntimeError("Telegram unavailable")):
                with self.assertRaises(RuntimeError):
                    alert.main()

    def test_trigger_time_is_preserved(self):
        with patch.dict(os.environ, {"SCAN_TRIGGER_TIME_UTC": "2026-10-19T07:05:00+00:00"}, clear=True):
            self.assertEqual(load_alert().STARTED, datetime(2026, 10, 19, 7, 5, tzinfo=timezone.utc))

    def test_empty_trigger_time_is_valid_for_operational_failure(self):
        with patch.dict(os.environ, {"SCAN_TRIGGER_TIME_UTC": ""}, clear=True):
            self.assertIsNotNone(load_alert().STARTED.tzinfo)

    def test_delayed_trigger_exits_failed_without_network(self):
        source = (ROOT / ".github/workflows/nifty500-cloud-scheduler.yml").read_text()
        code = textwrap.dedent(source.split("python3 - <<'PY'\n", 1)[1].split("          PY\n", 1)[0])

        class DelayedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 19, 13, 19, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            env = {"EVENT_NAME": "schedule", "SCHEDULE_CRON": "5 7 * * 1-5", "GITHUB_OUTPUT": str(output)}
            with patch.dict(os.environ, env, clear=True), patch("datetime.datetime", DelayedClock), patch("urllib.request.urlopen") as network:
                with self.assertRaises(SystemExit) as raised:
                    exec(compile(code, "trigger", "exec"), {})
                self.assertEqual(raised.exception.code, 1)
                self.assertIn("mode=midday", output.read_text())
                self.assertIn("skipped=true", output.read_text())
                network.assert_not_called()

    def test_no_duplicate_legacy_schedule(self):
        source = (ROOT / ".github/workflows/scanner.yml").read_text()
        self.assertNotIn("  schedule:", source)
        self.assertIn("  workflow_dispatch:", source)


if __name__ == "__main__":
    unittest.main()
