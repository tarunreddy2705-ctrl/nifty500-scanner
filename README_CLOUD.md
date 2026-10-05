# NIFTY 500 Scanner — Cloud Deployment

This package is designed for two Render cron jobs.

- Morning: 09:25 IST (03:55 UTC), Monday-Friday
- Midday: 12:35 IST (07:05 UTC), Monday-Friday
- The scanner itself enforces the configured IST windows.
- It also fails closed if current-session quote timestamps are not fresh, protecting against weekends, holidays, closures, or stale feeds.
- `UPSTOX_ACCESS_TOKEN` must be entered directly in Render as a secret environment variable. Never commit `.env`.

## Local validation

```bash
pip install -r requirements.txt
python scanner.py --mode test
```

## Render deployment

1. Put the contents of this folder in a private GitHub repository. Do not upload `.env`.
2. In Render, create a Blueprint from that repository. `render.yaml` creates both cron jobs.
3. For each cron service, enter `UPSTOX_ACCESS_TOKEN` in the Render dashboard when prompted / under Environment.
4. Deploy.
5. Use each cron job's Runs page to inspect run history and logs.

Render cron schedules use UTC; the supplied schedules correspond to 09:25 and 12:35 IST.
