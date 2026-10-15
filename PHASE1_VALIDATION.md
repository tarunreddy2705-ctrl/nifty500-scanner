# Phase 1 deployment and validation (NOT YET LIVE)

This branch contains **staged changes only**. The GitHub source may not match the currently deployed PythonAnywhere source. Do not copy this branch into production without a backup and a live-session verification.

## Before enabling live scans

1. Compare the deployed PythonAnywhere `scanner.py`, `config.json`, Flask report endpoint, and scheduled task commands with this branch. Preserve existing credentials; never paste tokens into GitHub or chat.
2. Verify Upstox v3 quote field semantics for `last_trade_time`, `timestamp`, `symbol`, `prev_close_price`, and volume, including whether the API timestamps are in seconds or milliseconds. Adjust parsing only against real sanitized payload samples.
3. Create `data/nse_holidays.json` locally using NSE's official current-year cash-market calendar **and special closure notices**, with the following schema. Do not set verified fields until independently confirmed:
   `{"year":2026,"source":"NSE_OFFICIAL","verified_at":"<timestamp with timezone>","special_closures_verified":true,"closed_dates":["YYYY-MM-DD"]}`
   This file is intentionally not populated with guessed dates.
4. Run `python -m unittest discover -s tests -v`, `python -m py_compile scanner.py data_integrity.py`, and `python scanner.py --mode test` with safe test credentials. Test mode is never live trade evidence.
5. In a normal NSE cash session, verify individual quote and completed-candle timestamps, all four index snapshots, 450+ fresh mapped quotes, reports, and execution duration.
6. Confirm PythonAnywhere scheduled triggers execute within the intended IST windows. Report readers must receive a newly generated, validated report within their 20-minute freshness SLA. Do not weaken this SLA.
7. Keep orders disabled. Event/news checks, spread, surveillance restrictions, and true structural target room are **not implemented** and therefore must remain external mandatory trade gates.

## Known limitations

- The NSE calendar is operator-verified, not automatically downloaded; without it the scanner refuses live runs.
- Intraday EMA50 requires 50 closed 5-minute bars, which means the morning window may have no eligible technical candidates. A separate previous-session historical warm-up is required before production morning signals can be evaluated.
- The original scanner still has a prototype technical score, not the requested final 100-point score. No candidate is promoted to TRADE.
- `report_fresh` written by the scanner is a creation-time flag, not proof of freshness at read time. The report bridge must calculate age independently.
- PythonAnywhere deployment, actual holiday list, API response semantics, and live-session outcomes have not been verified.

## Rollback

No deployment is performed by this branch. If deployed after validation, retain the previous `scanner.py` and `config.json` backups, and restore those exact files if runtime behavior differs from the tested version.
