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

GitHub-hosted cron can be delayed or skipped, especially under load. The workflow rejects requests that start outside the configured IST window. PythonAnywhere's own `session_gate` checks the market status; public holidays are not hardcoded into GitHub.

## Limits

- HTTP 202 only means the Flask app accepted the request. Its daemon thread may be killed by a web-worker restart, and the lock is per-process. This is **not** guaranteed background execution.
- A failed/timeout request is not automatically retried because the scan may already have started.
- There is no Telegram delivery yet. Add it only after the validated report path and notification secrets are implemented.
- The free PythonAnywhere website may require periodic renewal.
- No changes are made to the existing Flask app, scanner, bridge, Make.com scenario, or live jobs by this workflow.
