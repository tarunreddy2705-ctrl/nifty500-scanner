# Audit of the user-supplied 22 October reports

These observations come from uploaded saved reports and code, not independent
retrieval of exchange data. Both reports identify version
`2026-10-13-index-baseline-repair`, live mode (`test_data: false`), 499 fresh
quotes, 30 Stage-1 survivors, 20 deep evaluations and 20 technically valid
candidates. They report no technical-data failures. The local constituent CSV's
currency was not independently verified. The 501 reported rows include unmapped
`DUMMYHEG`; this is not proof of a current official 500-member universe.

## Morning, 09:25:40 IST

All 20 candidates lacked a completed 15-minute opening range. Their latest
completed candle started at 09:20. A 09:25 scheduled decision cannot pass the
implemented 09:15-09:30 opening-range gate. All 20 also failed structural
reward/risk or target identification; moving the scan time does not establish
that any would have qualified later.

## Midday, 12:35:32 IST

| Recorded blocker | Candidates |
| --- | ---: |
| Insufficient structural reward/risk or no identified target | 17 |
| Insufficient structural target room | 12 |
| Extended from VWAP; do not chase | 12 |
| Opening trigger already passed; require a new retest setup | 12 |
| Invalid structural entry/stop | 11 |
| Below WATCH score threshold | 9 |
| Historical same-time volume confirmation absent | 8 |
| MACD confirmation absent | 6 |
| RSI confirmation absent | 4 |
| No VWAP/EMA direction alignment | 2 |

Counts overlap. Midday reuses an opening trigger with a stop from recent bars;
after price moves, that combination can produce negative risk. Rejection is
appropriate; accepting such a setup or merely removing the blocker is not a fix.
The code requires a separate, explicitly specified continuation/retest setup
using raw completed candles, followed by validation. Saved indicator snapshots
alone cannot reconstruct the intervening price path or validate that strategy.

## Scope and validation of this repair

The repair fixes morning readiness/scheduling settings and rejection reporting.
It preserves the uploaded scanner's history, freshness and index-baseline repairs
that were missing from GitHub main. Both modes remain WATCH-only. It also records
market/industry/daily-trend blockers for already rejected candidates so reports
do not hide those additional failures.

Re-evaluating the 40 uploaded candidate snapshots with the old and repaired
scorers produced zero changes in status, score, direction, entry, stop, target,
reward/risk or expected move. This is a regression replay, not a backtest or
fresh live scan. Raw reports and credentials are not committed to the repository.

Remaining work includes a separately validated midday setup, independent
news/events and execution checks, instrument tick-size handling, universe
currency verification, and strategy outcome tracking with costs/slippage.
Some existing configuration keys (including `min_trade_score`,
`target_pct_cap` and `stop_atr_multiple`) are unused by the uploaded scorer;
this repair does not silently activate them or imply they are enforced.
