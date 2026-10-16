"""Read-only GitHub Actions report delivery. Never places orders or creates trade calls."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("SCANNER_BASE_URL", "").rstrip("/")
READ_SECRET = os.environ.get("SCANNER_REPORT_READ_SECRET", "")
BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
MODE = os.environ.get("SCAN_MODE", "test")
STARTED = datetime.fromisoformat(os.environ.get("SCAN_TRIGGER_TIME_UTC", datetime.now(timezone.utc).isoformat())).astimezone(timezone.utc)
WAIT_SECONDS = 240


def post_telegram(message):
    if not BOT or not CHAT:
        raise RuntimeError("Missing Telegram repository secrets")
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": message[:3900]}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot" + BOT + "/sendMessage",
        data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.load(resp)
            if not result.get("ok"):
                raise RuntimeError("Telegram rejected notification")
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Telegram HTTP " + str(exc.code)) from None


def get_report():
    req = urllib.request.Request(
        BASE + "/bridge/reports/" + MODE,
        headers={"X-Report-Secret": READ_SECRET, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except (ValueError, UnicodeDecodeError):
            return exc.code, {}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing report timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def render_report(payload):
    report = payload["report"]
    outcome = str(report.get("outcome") or "UNVERIFIED").upper()
    reason = str(report.get("reason") or "No reason supplied")
    if MODE == "test":
        heading = "DIAGNOSTIC TEST — NOT A LIVE TRADING SIGNAL"
    elif outcome in ("SCAN FAILED", "NO SCAN", "DIAGNOSTIC ONLY"):
        heading = "SCAN FAILED / UNVERIFIED — NO ACTIONABLE TRADE"
    elif outcome in ("NO TRADE", "NO ACTIONABLE TRADE"):
        heading = "NO TRADE"
    elif outcome == "WATCH ONLY":
        heading = "WATCH ONLY — NOT AN ACTIONABLE TRADE"
    else:
        heading = "UNVERIFIED OUTCOME — NO ACTIONABLE TRADE"
    coverage = report.get("coverage") or {}
    watches = report.get("watches") or []
    parts = [
        "NIFTY 500 " + MODE.upper(),
        heading,
        "Scanner outcome: " + outcome,
        "Report: " + str(payload.get("report_timestamp", "")),
        "Reason: " + reason[:500],
        "Fresh quotes: " + str(coverage.get("fresh_quotes", "unknown")),
        "Deep evaluated: " + str(coverage.get("deep_evaluated", "unknown")),
        "WATCH candidates: " + str(len(watches)),
        "No orders placed. Verify all signals independently.",
    ]
    if outcome == "WATCH ONLY" and MODE != "test":
        for item in watches[:3]:
            if isinstance(item, dict):
                symbol = item.get("symbol") or item.get("stock") or "unknown"
                parts.append("WATCH: " + str(symbol)[:50] + " (not a trade recommendation)")
    return "\n".join(parts)


def main():
    if MODE not in ("morning", "midday", "test"):
        raise RuntimeError("Invalid mode")
    parsed = urllib.parse.urlsplit(BASE)
    if parsed.scheme != "https" or parsed.hostname != "tarunreddy2705.pythonanywhere.com" or parsed.path not in ("", "/") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Unapproved scanner base URL")
    if not READ_SECRET:
        raise RuntimeError("Missing report-read secret")
    if os.environ.get("TELEGRAM_CONNECTION_TEST") == "true":
        post_telegram("NIFTY 500: GitHub Actions → Telegram test successful. No scan executed. No trading signal.")
        print("Telegram connection test sent")
        return
    last_error = "report unavailable"
    for _ in range(WAIT_SECONDS // 15):
        try:
            status, payload = get_report()
            if status == 200 and payload.get("report_fresh") is True:
                stamp = timestamp(payload.get("report_timestamp"))
                # Must be generated after the trigger was accepted. Allow 10 seconds clock skew.
                if stamp >= (STARTED - __import__("datetime").timedelta(seconds=10)) and isinstance(payload.get("report"), dict):
                    post_telegram(render_report(payload))
                    print("Validated report notification sent for", MODE)
                    return
                last_error = "latest report predates this notification run"
            elif status == 409 and isinstance(payload.get("report"), dict):
                stamp = timestamp(payload.get("report_timestamp"))
                if stamp >= (STARTED - __import__("datetime").timedelta(seconds=10)):
                    outcome = str(payload["report"].get("outcome") or "UNVERIFIED")
                    reason = str(payload["report"].get("reason") or payload.get("error") or "Report rejected")
                    post_telegram("NIFTY 500 " + MODE.upper() + "\nSCAN UNVERIFIED — NO ACTIONABLE TRADE\nScanner outcome: " + outcome[:100] + "\nReason: " + reason[:500] + "\nReport reader rejected this run (HTTP 409).")
                    print("Rejected current-run report notification sent")
                    return
                last_error = "rejected report predates this run"
            elif status in (404, 409, 503):
                last_error = "report not ready or rejected (HTTP " + str(status) + ")"
            else:
                last_error = "unexpected report response HTTP " + str(status)
                if status in (401, 403):
                    break
        except (ValueError, urllib.error.URLError, TimeoutError) as exc:
            last_error = "report retrieval or validation failed: " + type(exc).__name__
        time.sleep(15)
    post_telegram("NIFTY 500 " + MODE.upper() + "\nSCAN UNVERIFIED — NO ACTIONABLE TRADE\n" + last_error + "\nGitHub accepted a trigger, but no validated current-run report was retrieved.")
    print("Failure notification sent:", last_error)
    sys.exit(1)


if __name__ == "__main__":
    main()
