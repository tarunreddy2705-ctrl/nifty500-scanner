# NIFTY 500 Scanner — Production Candidate

Read-only Upstox Analytics scanner. It does not place orders.

## Install over existing project
Preserve your existing `.env`.

```bash
cd ~/nifty500-scanner
source .venv/bin/activate
# Copy scanner.py, config.json, requirements.txt from this package into this folder
pip install -r requirements.txt
python scanner.py --mode test
```

## Modes
- `--mode test`: diagnostic run at any time; never implies a live-session trade.
- `--mode morning`: only runs 09:20–10:15 IST on weekdays.
- `--mode midday`: only runs 12:30–13:15 IST on weekdays.

The local engine reports current official-universe coverage, Upstox quote coverage, NIFTY/Bank/VIX snapshots when available, breadth, industry breadth, Stage-1 survivors and deep technical evaluations.

## Fail-closed policy
This package intentionally does **not** auto-promote a technical candidate to `TRADE`. Material company news, RBI/macro events, results/corporate actions and other event risk must be independently verified before an actionable call. Candidates can be `WATCH` or `REJECT` locally.

## Automation
Once a live-session validation passes, schedule `python scanner.py --mode morning` and `python scanner.py --mode midday` using macOS `launchd`, cron, n8n or a server. Do not schedule before live validation.
