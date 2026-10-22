# Midday WATCH logic and paper evidence

This revision implements an experimental midday consolidation setup and automated
paper evidence collection. It does **not** make the strategy validated or enable
TRADE calls. No broker order endpoint is used.

## Setup specification

Morning retains the completed opening-range setup and 09:35 IST start. Midday
uses the six latest completed five-minute candles (30 minutes). It requires a
positive, finite range no wider than two intraday ATR, VWAP/EMA9/EMA20 direction
alignment, EMA20/EMA50 intraday trend alignment, the existing daily/industry/
market gates, RSI/MACD and same-time relative-volume confirmation. A pending
trigger must be within 1.5 ATR of current price. The existing two-ATR VWAP
extension filter remains in force.

The conditional trigger is beyond that recent range, and the stop is beyond its
opposite boundary, using the existing 0.05 price buffer. Instrument tick-size
handling remains unverified; these are analytical WATCH levels, not orders.
The target cannot skip a nearby observed barrier. Midday's target is capped at
2% within structural room and must still satisfy the existing minimum 1% target
and 2:1 reward/risk. These are specification choices, not evidence of an edge.
All six bars must exist and be completed; old reports without these fields cannot
be used to claim historical performance of this new setup.

## Paper evidence

`paper_tracking.sqlite3` stores each new live-mode WATCH once by report, symbol
and direction. Diagnostic, rejected and unvalidated candidates are excluded.
At each successful live scan, up to three unresolved earlier WATCHes are
reconciled from provider five-minute candles before new evidence is registered.
This limit bounds additional API work and rotates older unresolved records.
There is no extra scheduled job and no guaranteed same-day closing summary.
Midday watches can be reconciled on the next successful session using history.

The paper model is deliberately conservative:

- Entry is never assigned from candles before formation or an incomplete bar.
- A trigger touched in the candle overlapping formation is ambiguous.
- Gap-through entries are cancelled. Stop gaps use the worse observed open.
- Unknown ordering of entry/stop/target within one candle stays ambiguous.
- Missing, duplicate, malformed or off-grid candles cannot establish a win.
- Pending entries expire after 60 minutes. Open paper positions exit at 15:25
  using the completed 15:20 candle. Missing exit data stays unresolved.
- Illustrative net return subtracts 0.10% round-trip costs plus 0.05% slippage
  per side. These are assumptions, not actual brokerage/tax calculations.

Quote freshness is checked again after reconciliation; expired WATCH levels
are withheld. Reconciliation failures are explicitly reported and cannot turn
a failed scan into a trade. Telegram labels reports over two minutes old as
delayed, includes setup type and paper-recording status, and shows conditional
levels only while their individual quote timestamps are within two minutes.

## Deployment and limits

Install `paper_tracking.py` beside `scanner.py`, with this revision's scanner and
config. Replace the automation module beside the Flask app and reload the app.
Preserve `.env`, existing reports and both SQLite ledgers. The 09:35 and 12:35
IST jobs and completion-monitor schedules do not change.

Readiness fields explicitly identify missing independent news/events and spread/
execution checks, unverified local constituent-list currency and unvalidated
performance. These gaps block any claim that the complete original actionable
scanner is finished. The current access does not provide a verified automated
news/event source. Software unit tests and synthetic candle tests are not a
substitute for live observations, costs-aware outcome analysis or source audits.
