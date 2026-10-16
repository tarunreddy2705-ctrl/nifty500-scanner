"""Fail-closed NSE session and market-data validation. No network calls or orders."""
from __future__ import annotations
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
import math

IST = ZoneInfo("Asia/Kolkata")
OPEN = time(9, 15)
CLOSE = time(15, 30)

def ist_now():
    return datetime.now(IST)

def session_check(now=None, holidays=None, official_calendar_verified=False):
    now = (now or ist_now()).astimezone(IST)
    if now.weekday() >= 5:
        return False, "WEEKEND"
    if not official_calendar_verified or holidays is None:
        return False, "NSE HOLIDAY CALENDAR UNVERIFIED"
    if now.date() in holidays:
        return False, "NSE HOLIDAY"
    if not OPEN <= now.time().replace(tzinfo=None) <= CLOSE:
        return False, "OUTSIDE NSE CASH SESSION"
    return True, "NSE NORMAL SESSION (SPECIAL CLOSURES NOT VERIFIED)"

def parse_timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            n = float(value)
            if n > 1e11:
                n /= 1000
            dt = datetime.fromtimestamp(n, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(IST)
    except (ValueError, TypeError, OverflowError, OSError):
        return None

def validate_quote(q, now=None, max_age_seconds=120):
    now = (now or ist_now()).astimezone(IST)
    if not isinstance(q, dict):
        return False, "QUOTE MISSING"
    stamp = None
    for key in ("last_trade_time", "timestamp", "ts"):
        stamp = parse_timestamp(q.get(key))
        if stamp is not None:
            break
    if stamp is None:
        return False, "QUOTE TIMESTAMP MISSING"
    if stamp.date() != now.date():
        return False, "QUOTE NOT FROM CURRENT IST DATE"
    age = (now - stamp).total_seconds()
    if age < -60 or age > max_age_seconds:
        return False, f"QUOTE STALE/FUTURE: {age:.0f}s"
    try:
        if not math.isfinite(float(q.get("last_price"))) or float(q["last_price"]) <= 0:
            return False, "INVALID PRICE"
    except (ValueError, TypeError, KeyError):
        return False, "INVALID PRICE"
    return True, "FRESH"

def validate_candles(df, now=None, interval_minutes=5, min_bars=50):
    now = (now or ist_now()).astimezone(IST)
    if df is None or len(df) < min_bars:
        return False, "INSUFFICIENT INDICATOR HISTORY"
    if "timestamp" not in df:
        return False, "CANDLE TIMESTAMPS MISSING"
    try:
        stamps = [parse_timestamp(v.to_pydatetime() if hasattr(v, "to_pydatetime") else v) for v in df["timestamp"]]
        if any(t is None for t in stamps):
            return False, "NAIVE/INVALID CANDLE TIMESTAMP"
        if any(t.date() != now.date() for t in stamps):
            return False, "MIXED SESSION CANDLES"
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            return False, "DUPLICATE/UNSORTED CANDLES"
        if stamps[0].time() < OPEN:
            return False, "CANDLES BEFORE MARKET OPEN"
        # Candle timestamps are treated as start times. Reject unfinished bars.
        if stamps[-1] + timedelta(minutes=interval_minutes) > now:
            return False, "LAST CANDLE NOT CLOSED"
        if now - (stamps[-1] + timedelta(minutes=interval_minutes)) > timedelta(minutes=interval_minutes * 2):
            return False, "CANDLES STALE"
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df:
                return False, f"MISSING {col.upper()}"
            vals = [float(v) for v in df[col]]
            if not all(math.isfinite(v) for v in vals):
                return False, f"INVALID {col.upper()}"
        if any(float(row["high"]) < max(float(row["open"]), float(row["close"])) or float(row["low"]) > min(float(row["open"]), float(row["close"])) or float(row["low"]) <= 0 or float(row["volume"]) < 0 for _, row in df.iterrows()):
            return False, "INVALID OHLCV"
    except (ValueError, TypeError, KeyError, OverflowError):
        return False, "INVALID CANDLE DATA"
    return True, "CLOSED CURRENT-SESSION CANDLES"

def structural_rr(direction, entry, stop, barrier):
    """Use externally identified structural barrier, never a synthetic 2R target."""
    try:
        entry, stop, barrier = map(float, (entry, stop, barrier))
        if not all(map(math.isfinite, (entry, stop, barrier))):
            return None
        if direction == "LONG" and stop < entry < barrier:
            return (barrier-entry)/(entry-stop)
        if direction == "SHORT" and barrier < entry < stop:
            return (entry-barrier)/(stop-entry)
    except (TypeError, ValueError):
        pass
    return None
