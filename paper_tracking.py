"""Conservative candle-based paper evidence. Never submits orders.

No win-rate claim is made. Missing bars and unknowable intrabar ordering remain
unresolved. Costs below are explicit illustrative assumptions, not broker fees.
"""
import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST=ZoneInfo('Asia/Kolkata')
TERMINAL={'TARGET','STOP','SESSION_EXIT','EXPIRED','GAP_CANCELLED',
          'INVALIDATED','AMBIGUOUS','AMBIGUOUS_START','AMBIGUOUS_ENTRY_BAR'}


@contextmanager
def connect(path):
    db=sqlite3.connect(path,timeout=5)
    try:
        with db: yield db
    finally:
        db.close()


def stamp(value):
    dt=datetime.fromisoformat(value) if isinstance(value,str) else value
    if not isinstance(dt,datetime) or dt.tzinfo is None:
        raise ValueError('Timezone-aware timestamp required')
    return dt.astimezone(IST)


def evaluate(signal,bars,now):
    created=stamp(signal['created_at']); now=stamp(now)
    if now<created: raise ValueError('Evaluation precedes formation')
    entry=float(signal['trigger']); stop=float(signal['stop']); target=float(signal['target'])
    long=signal['direction']=='LONG'
    if signal['direction'] not in ('LONG','SHORT') or not all(math.isfinite(x) and x>0 for x in (entry,stop,target)):
        raise ValueError('Invalid paper levels/direction')
    if not (stop<entry<target if long else target<entry<stop):
        raise ValueError('Invalid paper risk geometry')
    expires=min(created+timedelta(minutes=60),datetime.combine(created.date(),time(15,25),IST))
    close=datetime.combine(created.date(),time(15,25),IST)
    if created>=close: return {'status':'EXPIRED'}
    start=created.replace(minute=created.minute-created.minute%5,second=0,microsecond=0)
    end=min(now,close)
    candles={}
    for raw in bars:
        dt=stamp(raw['timestamp'])
        if dt.date()!=created.date() or dt<start or dt+timedelta(minutes=5)>end: continue
        if dt in candles or dt.minute%5 or dt.second or dt.microsecond:
            raise ValueError('Duplicate/off-grid paper candles')
        b={k:float(raw[k]) for k in ('open','high','low','close')}
        if not all(math.isfinite(v) and v>0 for v in b.values()) or not b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']:
            raise ValueError('Malformed paper OHLC')
        candles[dt]=b
    expected=[]; cursor=start
    while cursor+timedelta(minutes=5)<=end:
        expected.append(cursor); cursor+=timedelta(minutes=5)
    if any(dt not in candles for dt in expected):
        return {'status':'DATA_INCOMPLETE','reason':'Missing post-formation completed candles'}
    fill=None
    for dt in expected:
        b=candles[dt]
        touch=b['high']>=entry if long else b['low']<=entry
        if dt<created:
            if touch: return {'status':'AMBIGUOUS_START','reason':'Trigger touched in candle overlapping report creation'}
            continue
        just_entered=False
        if fill is None:
            # A bar straddling expiry cannot establish when entry occurred.
            if dt>=expires: return {'status':'EXPIRED'}
            if dt+timedelta(minutes=5)>expires:
                return {'status':'AMBIGUOUS' if touch else 'EXPIRED','reason':'Entry expiry falls inside candle'}
            invalid=b['low']<=stop if long else b['high']>=stop
            if not touch:
                if invalid: return {'status':'INVALIDATED','reason':'Stop structure failed before trigger'}
                continue
            gap=b['open']>entry if long else b['open']<entry
            if gap:
                return {'status':'GAP_CANCELLED','reason':'Paper model refuses gap-through entries'}
            fill=entry; just_entered=True
        stop_hit=b['low']<=stop if long else b['high']>=stop
        target_hit=b['high']>=target if long else b['low']<=target
        if stop_hit and (target_hit or just_entered):
            return {'status':'AMBIGUOUS_ENTRY_BAR' if just_entered else 'AMBIGUOUS',
                    'reason':'Candle cannot establish entry/stop/target ordering'}
        if stop_hit or target_hit:
            if stop_hit:
                exit_price=min(stop,b['open']) if long else max(stop,b['open'])
            else: exit_price=target
            return outcome('STOP' if stop_hit else 'TARGET',fill,exit_price,long,dt)
    if now>=close:
        if fill is None: return {'status':'EXPIRED'}
        last=candles.get(close-timedelta(minutes=5))
        if last is None: return {'status':'DATA_INCOMPLETE','reason':'Session-exit candle unavailable'}
        return outcome('SESSION_EXIT',fill,last['close'],long,close)
    if fill is None and now>=expires: return {'status':'EXPIRED'}
    return {'status':'OPEN' if fill is not None else 'PENDING'}


def outcome(status,entry,exit_price,long,dt):
    gross=(exit_price-entry)*(1 if long else -1)/entry*100
    return {'status':status,'entry_price':entry,'exit_price':exit_price,
            'exit_candle':dt.isoformat(),'gross_pct':round(gross,5),
            'illustrative_net_pct':round(gross-.20,5),
            'cost_assumption':'0.10% round-trip costs + 0.05% slippage each side; illustrative, not actual fees'}


def update_paper_tracking(path,report,fetch,now,clock=None):
    """Reconcile up to three older signals per scan, then register new WATCHes.

    SQLite gives atomic updates across processes. Network requests happen outside
    its write transaction. Old unresolved observations are rotated fairly by
    last check; failures never change a scanner decision.
    """
    now=stamp(now)
    if report.get('test_data') is not False or report.get('mode') not in ('morning','midday'):
        return {'status':'SKIPPED','reason':'Only live-mode paper evidence is registered'}
    with connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS paper_signals (id TEXT PRIMARY KEY, created TEXT, payload TEXT, status TEXT, checked TEXT, result TEXT)')
        rows=db.execute('SELECT id,payload FROM paper_signals WHERE status NOT IN ('+','.join('?' for _ in TERMINAL)+') ORDER BY checked,created LIMIT 3',tuple(sorted(TERMINAL))).fetchall()
    reconciled=[]
    for key,payload in rows:
        signal=json.loads(payload)
        try:
            result=evaluate(signal,fetch(signal['instrument_key'],stamp(signal['created_at']).date()),now)
        except Exception as exc:
            result={'status':'DATA_UNAVAILABLE','reason':type(exc).__name__}
        with connect(path) as db:
            db.execute('UPDATE paper_signals SET status=?,checked=?,result=? WHERE id=?',
                       (result['status'],now.isoformat(),json.dumps(result),key))
        reconciled.append({'symbol':signal['symbol'],**result})
    # Formation cannot predate reconciliation work or publication preparation.
    now=stamp(clock()) if clock else now
    registered=0
    with connect(path) as db:
        for candidate in report.get('watches',[]):
            if candidate.get('status')!='WATCH': continue
            quote_time=candidate.get('quote_timestamp')
            if quote_time is not None and (now-stamp(quote_time)).total_seconds()>120:
                continue
            fields=('symbol','instrument_key','direction','trigger','stop','target')
            signal={k:candidate[k] for k in fields}
            signal.update(created_at=max(stamp(report['timestamp']),now).isoformat(),mode=report['mode'],setup_type=candidate.get('setup_type'))
            evaluate(signal,[],stamp(signal['created_at']))  # Validate, never fill from past bars.
            key=hashlib.sha256((report['timestamp']+'|'+signal['symbol']+'|'+signal['direction']).encode()).hexdigest()
            cur=db.execute('INSERT OR IGNORE INTO paper_signals VALUES (?,?,?,?,?,?)',
                           (key,signal['created_at'],json.dumps(signal),'PENDING',now.isoformat(),'{}'))
            registered+=cur.rowcount
        counts=dict(db.execute('SELECT status,count(*) FROM paper_signals GROUP BY status').fetchall())
    return {'status':'RECORDING','registered':registered,'reconciled':reconciled,
            'counts':counts,'model':'Completed 5-minute OHLC simulation; ambiguous fills unresolved; not strategy validation'}
