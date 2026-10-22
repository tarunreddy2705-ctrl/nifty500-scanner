# cron-job.org completion monitoring

The production scheduler is cron-job.org, with PythonAnywhere retaining the
existing scanner and GitHub retaining source and manual diagnostic workflows.

## Jobs (Asia/Kolkata, weekdays)

| Purpose | Cron expression | Authenticated target |
| --- | --- | --- |
| Morning trigger | `35 9 * * 1-5` | `POST /scan/morning` |
| Midday trigger | `35 12 * * 1-5` | `POST /scan/midday` |
| Morning completion / Telegram | `*/2 9,10 * * 1-5` | `POST /automation/check/morning` |
| Midday completion / Telegram | `*/2 12,13 * * 1-5` | `POST /automation/check/midday` |

All four jobs use the existing `X-Scanner-Secret` header, held privately in
cron-job.org. Credentials must never appear in URLs, source, logs or screenshots.
Completion checks outside their slot's active window respond `idle` without
running the scanner or sending a message. The broad hour schedules simplify
configuration; they do not cause additional scans.

## 23 October timing and explanation repair

The configured 15-minute opening range ends at 09:30 IST. The previous 09:25
trigger necessarily rejected every candidate for an incomplete range. Schedule
the morning trigger at **09:35 IST** and install this revision's `config.json`,
`scanner.py`, and `deployment/automation_control.py` together. The deployed
`automation_control.py` lives beside the Flask app; reload that web app after
replacing it. Midday remains 12:35 IST. Completion-check schedules stay unchanged.
Changing this document or merging a PR does not update cron-job.org.

Perform the cutover outside the morning window: back up those three deployed
files, update the cron morning job to `35 9 * * 1-5` in Asia/Kolkata (preserving
its private header), copy the files, and reload the app. Verify that the saved
job and `SLOTS` both specify 09:35; an old 09:25 trigger now gets HTTP 409 before
claiming the day's run. Do not delete the SQLite ledger to force a second run.

The scanner now records structured blocker counts in saved reports and its
cloud summary, and Telegram includes the leading rejection reasons, live/test
label and WATCH-only policy. Reasons can overlap for the same candidate.
Both modes still use the opening-range setup. This revision labels that limit;
it does not implement or validate a new midday trading strategy. No risk,
momentum, target-room or no-chasing thresholds have been relaxed. TRADE remains
disabled. A complete report is operational evidence, not proof of data accuracy
or trading profitability.

After deployment, verify the first real 09:35 run has `mode: morning`,
`test_data: false`, the new scanner version, completed opening-range candles,
coverage, rejection counts and one acknowledged Telegram delivery. Then verify
the 12:35 midday run independently. Offline tests cannot establish those results.

## Install without replacing the live application

Place `automation_control.py` beside the deployed Flask app. Preserve a backup
of the original Flask app, and append once, after its existing routes:

```python
from automation_control import install_automation
automation_control = install_automation(app, PROJECT_DIR, env)
```

The existing app must define `trigger_scan`, `PROJECT_DIR` and its dotenv `env`.
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must already be configured privately
in that environment. Reload the web app, then verify authenticated
`GET /automation/health`. Missing credentials are a deployment blocker.

## Behavior

- A SQLite ledger in the scanner project records one claim per IST day/mode.
  Duplicate triggers cannot start another scan, including across worker reloads.
  Unknown/failed executions are not automatically retried; operators must first
  establish whether the original subprocess is still running.
- Completion checks require a same-day timezone-aware report matching the mode
  and generated after the recorded trigger, allowing two seconds of clock skew.
  Future, malformed and mismatched files cannot satisfy the run.
- A missing trigger/report is alerted after 10 minutes from the scheduled time,
  or 5 minutes from a later actual trigger, whichever is later.
- Each report or missing-run notice has a separate persisted delivery claim.
  Telegram acknowledgement and message ID are recorded; known failures retry on
  the next completion check without rerunning the scanner. A lost acknowledgement
  can still produce a duplicate Telegram message; exactly-once external delivery
  is not claimed.
- A report older than 20 minutes is labelled delayed, not a current signal.
  Delivery verification does not establish underlying market-data freshness or
  strategy validity. No orders are placed and no new trade calls are generated.
- A monitor returns 200 when it has successfully sent the missing-run warning;
  its JSON still says `status: missing`, not completed. Telegram failures return
  503. Enable cron failure, recovery and auto-disable notifications for both
  monitors so an unreachable app has an alert path outside PythonAnywhere.

## Cutover and validation

1. Keep the original cron triggers and GitHub schedule until authenticated
   health and real diagnostic scan/report/Telegram delivery are verified.
2. Clone existing cron jobs to preserve their private authentication header;
   change only targets/schedules/names, and enable notifications. Never log/export
   the raw request, which contains its credential.
3. Test the cloned job with `/automation/check/test` after a current `/scan/test`.
   Require HTTP 200, `status: completed` and `telegram: sent`; this is diagnostic
   evidence only. Repeated checks must not send another report message.
4. Save the monitor jobs with their live morning/midday targets, then remove the
   automatic schedule from `.github/workflows/nifty500-cloud-scheduler.yml` while
   retaining workflow_dispatch. Verify four enabled cron jobs and no GitHub cron.
5. Verify a real trading-session scheduled run before declaring live reliability
   established. Test mode never substitutes for a trading-session validation.

## Limits and recovery

This is a best-effort free-hosting integration, not a durable worker deployment.
PythonAnywhere's existing background thread/subprocess can be terminated by the
host. The monitor detects missing output but cannot guarantee completion. Both
cron-job.org and PythonAnywhere can fail or run late; no precise-time or
zero-failure guarantee is made. cron-job.org failure notifications protect against
app/network failure, but an outage affecting the scheduler itself requires an
independent monitoring provider to detect.

To roll back, disable the new monitor jobs, restore the backed-up Flask file,
reload the web app and deliberately restore the old GitHub schedule if needed.
Do not run both schedulers automatically after rollback without duplicate-run
protection. The ledger is operational evidence; do not delete it to force a retry.

Offline checks: `python -m unittest discover -s tests -v` (requires Flask).

