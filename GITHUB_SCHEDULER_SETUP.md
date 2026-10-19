# GitHub Actions cloud scheduler

This workflow triggers the **existing** authenticated PythonAnywhere Flask endpoint; it does not execute `scanner.py` on GitHub and does not place orders.

## Setup

1. GitHub repository → Settings → Secrets and variables → Actions.
2. Add **repository secret** `SCANNER_TRIGGER_SECRET` with the exact value already configured in PythonAnywhere's `.env`. Do not put this value in chat, source code or a repository variable.
3. Add **repository variable** `SCANNER_BASE_URL` = `https://Tarunreddy2705.pythonanywhere.com`.
4. Review and merge the scheduler pull request into the default branch. Scheduled workflows do not run from unmerged feature branches.
5. Actions → **NIFTY 500 cloud scan trigger** → Run workflow → select **test**. The expected result is `ACCEPTED`, not a completed scan.
6. Separately inspect the PythonAnywhere `cloud_logs/` and `reports/` for the test run; confirm its outcome and timestamps. Do not treat diagnostic reports as live trades.

## Schedule

- Monday–Friday **09:25 IST** (03:55 UTC): morning
- Monday–Friday **12:35 IST** (07:05 UTC): midday

GitHub-hosted cron can be delayed or skipped, especially under load. These times are requested start times, not guaranteed execution deadlines. The workflow rejects requests that start outside the configured IST window, marks the job failed and attempts a Telegram SCAN SKIPPED notification. It does not widen the scan window or send an old report as a current signal. PythonAnywhere's own `session_gate` checks the market status; public holidays are not hardcoded into GitHub.

The cloud workflow is the only automatic GitHub schedule. The legacy `scanner.yml` remains available for manual diagnostics, without scheduled runs or Telegram delivery. Each cloud run title identifies MORNING or MIDDAY.

## Exact-time requirement and remaining deployment work

This patch improves failure visibility; it cannot guarantee exact-time scans. GitHub documents delayed and dropped scheduled events: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule.

To meet a tighter timing requirement, deploy the scanner under a persistent worker with an independent clock-based scheduler and durable job state, plus an independent deadline monitor. Use one job identity per IST date and mode, prevent duplicate execution, record completion and Telegram acknowledgement separately, and alert on missed completion. A blind retry of an ambiguous POST can start duplicate scans. GitHub Actions alone cannot monitor a run that never starts. This infrastructure is not provisioned by this pull request; current costs and available hosting must be checked before choosing a provider.

## Limits

- HTTP 202 only means the Flask app accepted the request. Its daemon thread may be killed by a web-worker restart, and the lock is per-process. This is **not** guaranteed background execution.
- A failed/timeout request is not automatically retried because the scan may already have started.
- Telegram delivery polls the authenticated report endpoint after a confirmed trigger. Trigger failures and late-window skips attempt an operational alert and leave the job failed. Delivery failure also fails the job. A dropped GitHub event, failed checkout or runner outage may still prevent any alert; an independent monitor is required.
- The free PythonAnywhere website may require periodic renewal.
- This change does not modify the Flask app, scanner or report reader. Workflow changes become active when merged into the default branch. Do not assume trading-session validation has passed until a real scheduled run completes and its Telegram message is received.

