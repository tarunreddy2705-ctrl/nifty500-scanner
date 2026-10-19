# Telegram completion alerts

Production Telegram delivery now runs from authenticated PythonAnywhere completion checks called by cron-job.org, independently of the trigger request.

The existing private PythonAnywhere `.env` must contain `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, in addition to `SCANNER_TRIGGER_SECRET`. Never commit or display their values. The report-read secret remains separate for the read-only bridge.

The completion controller records Telegram acknowledgement and message ID in SQLite, avoids resending acknowledged reports, retries failed deliveries on the next check, and warns about missing reports. A transport timeout after Telegram accepted a message can still cause a duplicate. The controller never places orders or promotes scanner candidates to trades.

See [deployment/CRON_DEPLOYMENT.md](deployment/CRON_DEPLOYMENT.md) for schedules, validation and limits. GitHub's Telegram scripts remain available only for manually requested diagnostics.
