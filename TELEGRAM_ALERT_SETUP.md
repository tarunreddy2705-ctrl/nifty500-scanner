# Telegram report alerts — staged integration

The workflow now triggers PythonAnywhere, polls the authenticated read-only report endpoint for up to four minutes, and sends a Telegram message from GitHub Actions. No Telegram API calls are made from PythonAnywhere.

## Required GitHub Actions repository secrets
- `SCANNER_TRIGGER_SECRET`
- `SCANNER_REPORT_READ_SECRET`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Repository variable: `SCANNER_BASE_URL=https://Tarunreddy2705.pythonanywhere.com`.

## Safety limitations before merge
1. The **currently deployed** `report_routes.py` excludes scanner `outcome` and `reason`. The report reader must be updated to expose these verified fields before alerts can meaningfully distinguish WATCH / NO TRADE / SCAN FAILED. Without them the message fails closed as UNVERIFIED.
2. PythonAnywhere's daemon thread can be terminated unexpectedly. A successful trigger does not guarantee completion.
3. The current report reader returns HTTP 409 for stale reports and may return 503 for invalid files; this script sends a **SCAN UNVERIFIED** message after the timeout, never an invented trade.
4. The GitHub job timeout must be increased to accommodate report polling.
5. No direct Telegram credentials are committed. Bot tokens previously visible in screenshots must be revoked and replaced.
6. Test during a trading session before relying on automatic alerts; the diagnostic test mode is not a live signal.

This branch does not alter the PythonAnywhere server, the scanner, the report reader, or existing production scheduling until merged.
