"""Persist scanner claims and deliver saved reports from separate HTTP checks.

The existing PythonAnywhere runner remains best effort. This module detects a
lost runner; it does not make background threads durable or place orders.
"""
import hmac
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import jsonify, request

IST = ZoneInfo("Asia/Kolkata")
SLOTS = {"morning": ((9, 35), (9, 35), (10, 15)),
         "midday": ((12, 35), (12, 30), (13, 15))}


class Control:
    def __init__(self, root, env, now=None, sender=None):
        self.root = Path(root)
        self.env = env
        self.now = now or (lambda: datetime.now(IST))
        self.sender = sender or self.telegram
        self.db = self.root / "automation_state.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (day TEXT, mode TEXT, started TEXT, status TEXT, PRIMARY KEY(day,mode))")
            db.execute("CREATE TABLE IF NOT EXISTS notices (day TEXT, mode TEXT, kind TEXT, status TEXT, claimed TEXT, message_id INTEGER, PRIMARY KEY(day,mode,kind))")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def authorized(self):
        secret = self.env.get("SCANNER_TRIGGER_SECRET") or ""
        supplied = request.headers.get("X-Scanner-Secret", "")
        return bool(secret and supplied) and hmac.compare_digest(secret.encode(), supplied.encode())

    def claim(self, mode):
        now = self.now().astimezone(IST)
        day = now.date().isoformat()
        if mode not in ("morning", "midday", "test"):
            return {"error": "invalid mode"}, 400
        if mode != "test":
            _, start, end = SLOTS[mode]
            clock = (now.hour, now.minute)
            if now.weekday() >= 5 or not start <= clock <= end:
                return {"status": "skipped", "mode": mode, "reason": "outside approved weekday scan window"}, 409
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT status FROM runs WHERE day=? AND mode=?", (day, mode)).fetchone()
            if existing:
                return {"status": "already_requested", "mode": mode, "run_status": existing["status"]}, 200
            active = db.execute("SELECT started FROM runs WHERE status='requested'").fetchall()
            if any((now - datetime.fromisoformat(r["started"])).total_seconds() < 900 for r in active):
                return {"status": "busy", "mode": mode}, 409
            db.execute("INSERT INTO runs VALUES (?,?,?,?)", (day, mode, now.isoformat(), "requested"))
        return None

    def set_status(self, mode, status):
        with self.connect() as db:
            db.execute("UPDATE runs SET status=? WHERE day=? AND mode=?", (status, self.now().astimezone(IST).date().isoformat(), mode))

    def telegram(self, message):
        token = self.env.get("TELEGRAM_BOT_TOKEN")
        chat = self.env.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            raise RuntimeError("telegram credentials missing")
        data = urllib.parse.urlencode({"chat_id": chat, "text": message[:3900]}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=data, method="POST")
        # Callers deliberately suppress transport exceptions: their URLs may contain a bot token.
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.load(response)
        if payload.get("ok") is not True or not isinstance(payload.get("result", {}).get("message_id"), int):
            raise RuntimeError("telegram did not acknowledge")
        return payload["result"]["message_id"]

    def notify(self, mode, kind, message):
        now = self.now().astimezone(IST)
        key = (now.date().isoformat(), mode, kind)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM notices WHERE day=? AND mode=? AND kind=?", key).fetchone()
            if previous and previous["status"] == "sent":
                return "sent"
            if previous and previous["status"] == "sending" and (now - datetime.fromisoformat(previous["claimed"])).total_seconds() < 90:
                return "pending"
            db.execute("INSERT OR REPLACE INTO notices VALUES (?,?,?,?,?,NULL)", (*key, "sending", now.isoformat()))
        # Never hold a SQLite write transaction while waiting on a network request.
        try:
            message_id = self.sender(message)
        except Exception:
            with self.connect() as db:
                db.execute("UPDATE notices SET status='retry' WHERE day=? AND mode=? AND kind=?", key)
            return "failed"
        with self.connect() as db:
            db.execute("UPDATE notices SET status='sent', message_id=? WHERE day=? AND mode=? AND kind=?", (message_id, *key))
        return "sent"

    def report(self, mode, started):
        root = (self.root / "reports").resolve()
        files = [p for p in root.glob(mode + "_*.json") if p.is_file() and not p.is_symlink() and p.resolve().parent == root]
        if not files:
            return None
        latest = max(files, key=lambda p: p.name)
        if latest.stat().st_size > 5_000_000:
            raise ValueError("oversized report")
        report = json.loads(latest.read_text())
        stamp = datetime.fromisoformat(report["timestamp"].replace("Z", "+00:00"))
        now = self.now().astimezone(IST)
        if stamp.tzinfo is None or report.get("mode") != mode or not isinstance(report.get("coverage"), dict):
            raise ValueError("invalid report")
        if stamp.astimezone(IST).date() != now.date() or stamp < started - timedelta(seconds=2):
            return None
        if (stamp - now).total_seconds() > 60:
            raise ValueError("future report")
        if mode != "test" and report.get("test_data") is True:
            raise ValueError("test report in live slot")
        return report, stamp

    def check(self, mode):
        now = self.now().astimezone(IST)
        day = now.date().isoformat()
        if mode not in ("morning", "midday", "test"):
            return {"error": "invalid mode"}, 400
        if mode != "test":
            slot, _, end = SLOTS[mode]
            scheduled = now.replace(hour=slot[0], minute=slot[1], second=0, microsecond=0)
            if now.weekday() >= 5 or now < scheduled or (now.hour, now.minute) > (end[0], end[1] + 2):
                return {"mode": mode, "status": "idle"}, 200
        else:
            scheduled = now
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE day=? AND mode=?", (day, mode)).fetchone()
        report = None
        reason = None
        if row:
            try:
                report = self.report(mode, datetime.fromisoformat(row["started"]))
            except (OSError, ValueError, TypeError, KeyError):
                reason = "latest report is invalid or incomplete"
        if report:
            data, stamp = report
            age = (now - stamp).total_seconds()
            delayed = age > 1200
            outcome = str(data.get("outcome") or "UNVERIFIED").upper()
            coverage = data["coverage"]
            watches = data.get("watches")
            count = len(watches) if isinstance(watches, list) else "unknown"
            heading = "DIAGNOSTIC TEST — NOT A LIVE SIGNAL" if mode == "test" else "DELAYED REPORT — NOT A CURRENT SIGNAL" if delayed else "SCANNER REPORT — VERIFY INDEPENDENTLY"
            reason_text = str(data.get("reason") or "Scanner supplied no reason; strategy status unverified")[:500]
            for value in self.env.values():
                if isinstance(value, str) and len(value) > 12:
                    reason_text = reason_text.replace(value, "[redacted]")
            message = (f"NIFTY 500 {mode.upper()}\n{heading}\nScanner outcome: {outcome}\n"
                       f"Report: {stamp.isoformat()}\nReason: {reason_text}\n"
                       f"Fresh quotes: {coverage.get('fresh_quotes', 'unknown')}\n"
                       f"Deep evaluated: {coverage.get('deep_evaluated', 'unknown')}\nWATCH candidates: {count}\n"
                       "Delivery checks report-file timestamps only; strategy and market-data validation remain separate.\nNo orders placed.")
            details=[]
            if isinstance(data.get('test_data'),bool):
                details.append('Report mode: '+('DIAGNOSTIC' if data['test_data'] else 'LIVE DATA (scanner-reported)'))
            if data.get('setup_type')=='OPENING_RANGE_ONLY':
                details.append('Setup: opening range only; separate midday strategy unavailable')
            if data.get('policy_note'):
                details.append('Policy: '+str(data['policy_note'])[:220])
            summary=data.get('rejection_summary')
            if isinstance(summary,list):
                for item in summary[:4]:
                    if isinstance(item,dict):
                        details.append(f"Rejected {item.get('count','?')}: {str(item.get('reason','unknown'))[:180]}")
            if details:
                extra='\n'.join(details)
                for value in self.env.values():
                    if isinstance(value,str) and len(value)>12:
                        extra=extra.replace(value,'[redacted]')
                message=message.replace('\nDelivery checks', '\n'+extra+'\nDelivery checks')
            sent = self.notify(mode, "report", message)
            self.set_status(mode, "completed")
            return {"mode": mode, "status": "completed", "telegram": sent, "report_timestamp": stamp.isoformat(), "delayed": delayed}, 200 if sent == "sent" else 503
        deadline = scheduled + timedelta(minutes=10)
        if row:
            deadline = max(deadline, datetime.fromisoformat(row["started"]) + timedelta(minutes=5))
        if mode != "test" and now < deadline:
            return {"mode": mode, "status": "waiting"}, 200
        reason = reason or ("no trigger recorded" if not row else "no matching completed report; runner may have failed")
        sent = self.notify(mode, "missing", f"NIFTY 500 {mode.upper()}\nSCAN MISSING / UNVERIFIED — NO ACTIONABLE TRADE\nTime IST: {now.isoformat()}\nReason: {reason}\nNo automatic rescan; check scanner logs.")
        # Acknowledged operational alert is a completed monitor check, not a
        # completed scan. Unreachable app / Telegram failure still fails cron.
        return {"mode": mode, "status": "missing", "telegram": sent}, 200 if sent == "sent" else 503


def install_automation(app, root, env):
    control = Control(root, env)
    original = app.view_functions["trigger_scan"]

    def guarded_scan(mode):
        if not control.authorized():
            return jsonify(error="unauthorized"), 401
        result = control.claim(mode)
        if result:
            body, status = result
            return jsonify(body), status
        try:
            response = app.make_response(original(mode))
        except Exception:
            control.set_status(mode, "trigger_failed")
            return jsonify(status="trigger_failed", mode=mode), 503
        if response.status_code != 202:
            control.set_status(mode, "trigger_failed")
        return response

    app.view_functions["trigger_scan"] = guarded_scan

    @app.post("/automation/check/<mode>")
    def automation_check(mode):
        if not control.authorized():
            return jsonify(error="unauthorized"), 401
        body, status = control.check(mode)
        response = jsonify(body)
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/automation/health")
    def automation_health():
        if not control.authorized():
            return jsonify(error="unauthorized"), 401
        return jsonify(status="installed", ledger=True, telegram_configured=bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")), runner="existing best-effort PythonAnywhere background runner")

    return control

