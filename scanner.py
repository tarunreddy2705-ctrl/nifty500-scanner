from __future__ import annotations
import argparse, gzip, json, math, os, sys, time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
from collections import Counter
from paper_tracking import update_paper_tracking
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

VERSION='2026-10-23-midday-and-paper-tracking'
IST=ZoneInfo('Asia/Kolkata')
def now_ist(): return datetime.now(IST)
ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'; REPORTS=ROOT/'reports'; LOGS=ROOT/'logs'
load_dotenv(ROOT/'.env')
TOKEN=os.getenv('UPSTOX_ACCESS_TOKEN','').strip()
UPSTOX_MASTER='https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'
NIFTY500='https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv'
QUOTE_URL='https://api.upstox.com/v3/market-quote/quotes'
INDEX_KEYS=['NSE_INDEX|Nifty 50','NSE_INDEX|Nifty Bank','NSE_INDEX|Nifty 500','NSE_INDEX|India VIX']
INTRADAY='https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/{interval}'
HEADERS={'Accept':'application/json','Authorization':f'Bearer {TOKEN}'}
UA={'User-Agent':'Mozilla/5.0','Accept':'text/csv,*/*'}

def cfg(): return json.loads((ROOT/'config.json').read_text())
def req(url, headers=None, params=None, timeout=30):
    for attempt in range(3):
        try:
            r=requests.get(url,headers=headers,params=params,timeout=timeout)
            if r.status_code==401: raise RuntimeError('Upstox authentication failed; refresh the access token')
            if r.status_code in (429,500,502,503,504) and attempt<2:
                time.sleep(1+attempt); continue
            if r.status_code>=400: raise RuntimeError(f'HTTP {r.status_code} from data provider')
            return r
        except (requests.Timeout, requests.ConnectionError):
            if attempt==2: raise RuntimeError('Data provider timeout/connection failure') from None
            time.sleep(1+attempt)
    raise RuntimeError('Data provider request failed')

def download_master():
    raw=gzip.decompress(req(UPSTOX_MASTER,timeout=60).content)
    arr=json.loads(raw.decode())
    (DATA/'nse_master.json').write_text(json.dumps(arr))
    return arr

def download_universe():
    local_file = Path(__file__).with_name("ind_nifty500list.csv")
    if not local_file.exists():
        raise FileNotFoundError(f"Missing local NIFTY 500 constituent file: {local_file}")
    return pd.read_csv(local_file)

def reconcile(master, df):
    required={'Symbol','ISIN Code','Industry','Company Name'}
    if not required.issubset(df.columns): raise ValueError('Constituent CSV missing required columns')
    if df['Symbol'].duplicated().any(): raise ValueError('Duplicate constituent symbols')
    # Primary match by ISIN; symbol validation/fallback. Keep NSE cash-market segment, not only type EQ,
    # because index methodologies can contain non-common-equity cash instruments.
    cash=[x for x in master if x.get('segment')=='NSE_EQ']
    by_isin={str(x.get('isin','')).strip().upper():x for x in cash if x.get('isin')}
    by_symbol={str(x.get('trading_symbol','')).strip().upper():x for x in cash if x.get('trading_symbol')}
    mapped={}; unmapped=[]; mismatches=[]
    for _,row in df.iterrows():
        sym=str(row.get('Symbol','')).strip().upper(); isin=str(row.get('ISIN Code','')).strip().upper()
        item=by_isin.get(isin) or by_symbol.get(sym)
        if item and str(item.get('isin','')).strip().upper()!=isin: item=None
        if not item: unmapped.append({'symbol':sym,'isin':isin,'series':str(row.get('Series',''))}); continue
        up_sym=str(item.get('trading_symbol','')).strip().upper()
        if up_sym!=sym: mismatches.append({'official':sym,'upstox':up_sym,'isin':isin})
        mapped[sym]={'company':row.get('Company Name'),'industry':row.get('Industry'),'series':row.get('Series'),'isin':isin,
                     'instrument_key':item.get('instrument_key'),'instrument_type':item.get('instrument_type'),'upstox_symbol':up_sym}
    out={'generated_at':now_ist().isoformat(),'official_rows':len(df),'mapped_count':len(mapped),'unmapped':unmapped,'symbol_mismatches':mismatches,'instruments':mapped}
    (DATA/'nifty500_universe.json').write_text(json.dumps(out,indent=2,default=str)); return out

def chunks(xs,n=500):
    for i in range(0,len(xs),n): yield xs[i:i+n]

def bulk_quotes(universe):
    keys=[v['instrument_key'] for v in universe['instruments'].values() if v.get('instrument_key')]
    data={}; errors=[]
    for batch in chunks(keys,500):
        try:
            j=req(QUOTE_URL,headers=HEADERS,params={'instrument_key':','.join(batch)},timeout=30).json()
            if j.get('status')=='success': data.update(j.get('data',{}))
            else: errors.append(j)
        except Exception as e: errors.append(str(e))
    return data,errors


def _epoch_to_dt(value):
    if value is None: return None
    try:
        if isinstance(value,str) and not value.replace('.','',1).isdigit():
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            return dt.astimezone(IST) if dt.tzinfo else None
        x=float(value)
        if x>10_000_000_000: x/=1000
        return datetime.fromtimestamp(x,IST)
    except (ValueError,TypeError,OverflowError,OSError): return None

def quote_time(q):
    # OHLC timestamp is a candle start, not quote freshness.
    for field in ('last_trade_time','timestamp','ts'):
        dt=_epoch_to_dt(q.get(field))
        if dt is not None: return dt
    return None

def fresh_quote(q, max_age_minutes=2, now=None):
    now=now or now_ist(); dt=quote_time(q)
    if dt is None or dt.date()!=now.date(): return False
    age=(now-dt).total_seconds()
    return -5<=age<=max_age_minutes*60

def clean_quotes(quotes, max_age_minutes=2, now=None):
    now=now or now_ist()
    valid={k:q for k,q in quotes.items() if fresh_quote(q,max_age_minutes,now)}
    return valid, {'received':len(quotes),'fresh':len(valid),'excluded':len(quotes)-len(valid),
                   'checked_at':now.isoformat(),'max_age_minutes':max_age_minutes}

def market_data_freshness(quotes, max_age_minutes=2):
    valid, details=clean_quotes(quotes,max_age_minutes)
    details['reason']='fresh quotes available' if valid else 'no fresh attributable current-session quotes'
    return bool(valid), details

def quote_by_symbol(q):
    out={}
    for _,v in q.items():
        sym=str(v.get('symbol','')).strip().upper()
        if sym: out[sym]=v
    return out

def stage1(universe, quotes, c):
    qm=quote_by_symbol(quotes); rows=[]
    for sym,meta in universe['instruments'].items():
        q=qm.get(meta.get('upstox_symbol') or sym)
        if not q: continue
        ltp=float(q.get('last_price') or 0); pc=float(q.get('prev_close_price') or 0)
        o=q.get('ohlc') or {}; vol=float(o.get('volume') or 0)
        if ltp<=0 or pc<=0: continue
        ch=(ltp/pc-1)*100; turnover=ltp*vol/1e7
        if ltp<c['min_price'] or turnover<c['min_turnover_crore'] or abs(ch)<c['min_abs_change_pct'] or abs(ch)>c['max_abs_change_pct']: continue
        # Stage 1 score ranks activity; it is NOT the final trade score.
        score=min(35,math.log10(max(turnover,1))*10)+min(35,abs(ch)*7)+min(30,math.log10(max(vol,1))*4)
        rows.append({'symbol':sym,'industry':meta.get('industry'),'ltp':ltp,'quote_timestamp':quote_time(q).isoformat() if quote_time(q) else None,'change_pct':ch,'volume':int(vol),'turnover_cr':turnover,'stage1_score':score,'year_high':q.get('year_high'),'year_low':q.get('year_low')})
    return sorted(rows,key=lambda x:x['stage1_score'],reverse=True)[:c['max_stage1_survivors']]

def candle_frame(candles):
    if not candles: return pd.DataFrame()
    df=pd.DataFrame([c[:7] for c in candles],columns=['timestamp','open','high','low','close','volume','oi'])
    df['timestamp']=pd.to_datetime(df['timestamp'],utc=True,errors='coerce').dt.tz_convert(IST)
    for col in ('open','high','low','close','volume'): df[col]=pd.to_numeric(df[col],errors='coerce')
    if df[['timestamp','open','high','low','close','volume']].isna().any().any():
        raise ValueError('Malformed candle values')
    if not np.isfinite(df[['open','high','low','close','volume']].to_numpy()).all():
        raise ValueError('Non-finite candle values')
    invalid=(df.low<=0)|(df.volume<0)|(df.high<df.low)|(df.high<df[['open','close']].max(axis=1))|(df.low>df[['open','close']].min(axis=1))
    if invalid.any(): raise ValueError('Impossible OHLC/volume values')
    if df.timestamp.duplicated().any(): raise ValueError('Duplicate candle timestamps')
    return df.sort_values('timestamp').reset_index(drop=True)

def candle_request(url):
    j=req(url,headers=HEADERS,timeout=30).json()
    if j.get('status')!='success': raise ValueError('Candle API did not return success')
    return candle_frame((j.get('data') or {}).get('candles') or [])

def intraday(key, interval):
    return candle_request(INTRADAY.format(key=quote(key,safe=''),interval=interval))

def history(key, unit='minutes', interval=5, lookback_days=14, now=None):
    now=now or now_ist(); end=now.date()-timedelta(days=1); start=end-timedelta(days=lookback_days-1)
    digest=hashlib.sha256(f'{key}|{unit}|{interval}|{start}|{end}'.encode()).hexdigest()[:24]
    path=DATA/f'history_{digest}.json'
    if path.exists():
        try: return candle_frame(json.loads(path.read_text()))
        except (ValueError,TypeError): path.unlink(missing_ok=True)
    url=f"https://api.upstox.com/v3/historical-candle/{quote(key,safe='')}/{unit}/{interval}/{end}/{start}"
    df=candle_request(url)
    if not df.empty:
        rows=[[r.timestamp.isoformat(),r.open,r.high,r.low,r.close,r.volume,r.oi] for r in df.itertuples()]
        atomic_json(path,rows)
    return df

def atomic_json(path, payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(sanitize(payload),indent=2,allow_nan=False)); temp.replace(path)

def sanitize(value):
    if isinstance(value,dict): return {str(k):sanitize(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [sanitize(v) for v in value]
    if isinstance(value,(float,np.floating)): return float(value) if math.isfinite(value) else None
    if isinstance(value,np.integer): return int(value)
    if isinstance(value,(datetime,pd.Timestamp)): return value.isoformat()
    return value

def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean()
    dn=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    result=100-(100/(1+up/dn.replace(0,np.nan)))
    result=result.mask((dn==0)&(up>0),100).mask((up==0)&(dn>0),0).mask((up==0)&(dn==0),50)
    return result

def atr(df,n=14):
    pc=df.close.shift(); tr=pd.concat([(df.high-df.low).abs(),(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1); return tr.ewm(alpha=1/n,adjust=False).mean()
def technical(df, opening_minutes=15, historical=None, interval=5, now=None):
    now=now or now_ist()
    if df.empty: raise ValueError('No current-session candles')
    today=df[df.timestamp.dt.date==now.date()].copy()
    today=today[(today.timestamp.dt.time>=dtime(9,15))&(today.timestamp.dt.time<dtime(15,30))]
    today=today[today.timestamp+pd.Timedelta(minutes=interval)<=now].reset_index(drop=True)
    if len(today)<2: raise ValueError('Fewer than two completed current-session candles')
    expected=pd.date_range(pd.Timestamp(datetime.combine(now.date(),dtime(9,15),IST)),today.timestamp.iloc[-1],freq=f'{interval}min')
    if list(today.timestamp)!=list(expected): raise ValueError('Missing or off-grid current-session candles')
    last=today.iloc[-1]
    if (now-(last.timestamp+pd.Timedelta(minutes=interval))).total_seconds()>interval*60+30:
        raise ValueError('Latest completed candle is stale')
    past=historical if historical is not None else pd.DataFrame()
    if past.empty: raise ValueError('Historical intraday warm-up unavailable')
    past=past[past.timestamp.dt.date<now.date()].copy()
    if len(past)<150: raise ValueError('Need at least 150 previous-session bars for indicator warm-up')
    previous_date=past.timestamp.dt.date.max()
    if (now.date()-previous_date).days>5: raise ValueError('Historical warm-up is too old')
    previous=past[past.timestamp.dt.date==previous_date]
    if len(previous)<70: raise ValueError('Previous-session candle coverage incomplete')
    allbars=pd.concat([past,today],ignore_index=True).sort_values('timestamp')
    ema9=allbars.close.ewm(span=9,adjust=False).mean(); ema20=allbars.close.ewm(span=20,adjust=False).mean()
    ema50=allbars.close.ewm(span=50,adjust=False).mean(); rs=rsi(allbars.close); at=atr(allbars)
    macd=allbars.close.ewm(span=12,adjust=False).mean()-allbars.close.ewm(span=26,adjust=False).mean()
    sig=macd.ewm(span=9,adjust=False).mean()
    # Session quantities use ONLY today's bars; historical volume never enters VWAP.
    tp=(today.high+today.low+today.close)/3; vwap=(tp*today.volume).sum()/today.volume.sum() if today.volume.sum()>0 else np.nan
    opening_end=datetime.combine(now.date(),dtime(9,15),IST)+timedelta(minutes=opening_minutes)
    opening=today[today.timestamp<opening_end]
    opening_complete=now>=opening_end and len(opening)==opening_minutes//interval
    same_time=past[past.timestamp.dt.time==last.timestamp.time()].tail(5)
    rvol=float(last.volume/same_time.volume.mean()) if len(same_time)>=3 and same_time.volume.mean()>0 else None
    return {'close':float(last.close),'vwap':float(vwap),'ema9':float(ema9.iloc[-1]),'ema20':float(ema20.iloc[-1]),
            'ema50':float(ema50.iloc[-1]),'rsi':float(rs.iloc[-1]),'atr':float(at.iloc[-1]),
            'macd':float(macd.iloc[-1]),'macd_signal':float(sig.iloc[-1]),'rvol_same_time':rvol,
            'opening_high':float(opening.high.max()),'opening_low':float(opening.low.min()),
            'opening_complete':opening_complete,'day_high':float(today.high.max()),'day_low':float(today.low.min()),
            'recent_low':float(today.tail(3).low.min()),'recent_high':float(today.tail(3).high.max()),
            'previous_high':float(previous.high.max()),'previous_low':float(previous.low.min()),
            'midday_range_high':float(today.tail(6).high.max()),
            'midday_range_low':float(today.tail(6).low.min()),
            'midday_range_bars':len(today.tail(6)),
            'midday_range_start':today.tail(6).timestamp.iloc[0].isoformat(),
            'midday_range_end':(last.timestamp+pd.Timedelta(minutes=interval)).isoformat(),
            'bars':len(today),'warmup_bars':len(past),'last_bar':last.timestamp.isoformat(),
            'bar_end':(last.timestamp+pd.Timedelta(minutes=interval)).isoformat(),'previous_session':str(previous_date)}

def score_candidate(row,t,cfgtech,mode='morning'):
    if not t: return {'status':'DATA_UNAVAILABLE','score':None,'reason':'Technical data unavailable'}
    mandatory=('close','vwap','ema9','ema20','ema50','rsi','atr','macd','macd_signal')
    if any(not math.isfinite(t[k]) for k in mandatory) or t['atr']<=0:
        return {'status':'DATA_UNAVAILABLE','score':None,'reason':'Invalid indicators'}
    p=t['close']; direction='LONG' if p>t['vwap'] and t['ema9']>t['ema20'] else ('SHORT' if p<t['vwap'] and t['ema9']<t['ema20'] else 'NONE')
    if direction=='NONE': return {'status':'REJECT','score':0,'direction':direction,'reason':'No VWAP/EMA direction alignment','blockers':['No VWAP/EMA direction alignment']}
    long=direction=='LONG'; score=25; reasons=['VWAP/EMA alignment']; blockers=[]
    if (long and 52<=t['rsi']<=72) or (not long and 28<=t['rsi']<=48): score+=15
    else: blockers.append('RSI confirmation absent')
    if (long and t['macd']>t['macd_signal']) or (not long and t['macd']<t['macd_signal']): score+=10
    else: blockers.append('MACD confirmation absent')
    rv=t.get('rvol_same_time')
    if rv is not None and rv>=1.2: score+=15
    else: blockers.append('Historical same-time volume confirmation absent')
    if abs(row['change_pct'])>=0.6: score+=10
    if row['turnover_cr']>=25: score+=10
    if not t['opening_complete']: blockers.append('15-minute opening range still developing')
    if abs(p-t['vwap'])>cfgtech.get('max_vwap_distance_atr',2)*t['atr']: blockers.append('Extended from VWAP; do not chase')
    # Midday uses a fresh 30-minute consolidation, not the opening trigger.
    setup='MIDDAY_CONSOLIDATION_BREAKOUT' if mode=='midday' else 'OPENING_RANGE_BREAKOUT'
    if mode=='midday':
        high=t.get('midday_range_high'); low=t.get('midday_range_low')
        if (t.get('midday_range_bars',0)<6 or high is None or low is None or
                not math.isfinite(high) or not math.isfinite(low) or low<=0 or high<=low):
            return {'status':'DATA_UNAVAILABLE','score':None,'direction':direction,
                    'reason':'Six completed midday consolidation bars unavailable','setup_type':setup}
        if high-low>cfgtech.get('midday_max_range_atr',2)*t['atr']:
            blockers.append('Midday range too wide; no clean consolidation')
        if not ((long and t['ema20']>=t['ema50']) or (not long and t['ema20']<=t['ema50'])):
            blockers.append('Midday EMA20/EMA50 trend confirmation absent')
        trigger=high+0.05 if long else low-0.05
        stop=low-0.05 if long else high+0.05
        live=float(row.get('ltp',p))
        if (long and live>=trigger) or (not long and live<=trigger):
            blockers.append('Midday trigger already passed in current quote; require new consolidation')
        if abs(trigger-p)>cfgtech.get('midday_max_trigger_distance_atr',1.5)*t['atr']:
            blockers.append('Midday trigger too far from current price')
    else:
        trigger=t['opening_high']+0.05 if long else t['opening_low']-0.05
        if (long and p>trigger+t['atr']*.25) or (not long and p<trigger-t['atr']*.25): blockers.append('Opening trigger already passed; require a new retest setup')
        stop=t['recent_low']-0.05 if long else t['recent_high']+0.05
    risk=trigger-stop if long else stop-trigger
    # Closest observed prior-session/daily candle barrier, never a manufactured 2R target.
    levels=t.get('resistance_levels' if long else 'support_levels',[])
    room=sorted([v for v in levels if v>trigger] if long else [v for v in levels if v<trigger],reverse=not long)
    structural_barrier=room[0] if room else None
    target=structural_barrier
    if mode=='midday' and target is not None:
        # Cap the paper target inside observed structural room, never beyond it.
        cap=cfgtech.get('target_pct_cap',2)/100
        target=min(target,trigger*(1+cap)) if long else max(target,trigger*(1-cap))
    reward=(target-trigger if long else trigger-target) if target is not None else None
    rr=reward/risk if reward is not None and risk>0 else None
    move=reward/trigger*100 if reward is not None and trigger>0 else None
    if rr is not None and rr>=cfgtech['min_reward_risk']: score+=15
    else: blockers.append('Insufficient structural reward/risk or no identified target')
    if move is None or move<cfgtech.get('min_target_pct',1): blockers.append('Insufficient structural target room')
    if score<cfgtech['min_watch_score']: blockers.append('Below WATCH score threshold')
    if trigger<=0 or stop<=0 or risk<=0: blockers.append('Invalid structural entry/stop')
    status='REJECT' if blockers else 'WATCH'
    return {'direction':direction,'score':score,'score_system':'prototype technical score; not win probability',
            'status':status,'trigger':round(trigger,2),'stop':round(stop,2),
            'setup_type':setup,'structural_barrier':structural_barrier,
            'target':round(target,2) if target else None,'rr':round(rr,2) if rr else None,
            'expected_move_pct':round(move,2) if move else None,'risk_per_share':round(risk,2),
            'confirmation_required':'Future trigger confirmation, current quotes, market/industry alignment and independent news/event and execution checks; WATCH is not permission to enter',
            'blockers':blockers,
            'reason':'; '.join(blockers or ['Technical WATCH only; news/event verification and execution checks remain unavailable'])}

def session_gate(mode, C):
    now=now_ist()
    if mode=='test': return True,'Diagnostic mode; no actionable signals'
    if now.weekday()>=5: return False,'weekend'
    opening_end=datetime.combine(now.date(),dtime(9,15),IST)+timedelta(minutes=C['technical']['opening_range_minutes'])
    if now<opening_end:
        return False,f'WAITING FOR OPENING RANGE until {opening_end:%H:%M} IST; no setup decision made'
    w=C.get('session_windows',{}).get(mode)
    if not w: return False,f'no configured {mode} window'
    sh,sm=map(int,w['start'].split(':')); eh,em=map(int,w['end'].split(':'))
    if not dtime(sh,sm)<=now.time().replace(tzinfo=None)<=dtime(eh,em): return False,f"outside {mode} window {w['start']}-{w['end']} IST"
    j=req('https://api.upstox.com/v2/market/status/NSE',headers=HEADERS).json()
    data=j.get('data') or {}
    if j.get('status')!='success' or data.get('exchange')!='NSE': raise ValueError('NSE status unavailable')
    if data.get('status')!='NORMAL_OPEN': return False,'NSE status '+str(data.get('status'))
    updated=_epoch_to_dt(data.get('last_updated'))
    if updated is None or updated.date()!=now.date() or updated>now+timedelta(seconds=5):
        raise ValueError('NSE status timestamp is not attributable to today')
    return True,'NSE NORMAL_OPEN verified by Upstox'

def index_snapshot():
    j=req(QUOTE_URL,headers=HEADERS,params={'instrument_key':','.join(INDEX_KEYS)},timeout=20).json()
    if j.get('status')!='success': raise ValueError('Index quote API failed')
    indexes=j.get('data',{})
    # Explicit previous-day OHLC endpoint; never assume today's OHLC close is yesterday's close.
    if any(not float(q.get('prev_close_price') or 0) for q in indexes.values()):
        try:
            old=req('https://api.upstox.com/v3/market-quote/ohlc',headers=HEADERS,
                    params={'instrument_key':','.join(INDEX_KEYS),'interval':'1d'},timeout=20).json()
            if old.get('status')=='success':
                for k,q in indexes.items():
                    prev=(old.get('data',{}).get(k,{}) or {}).get('prev_ohlc') or {}
                    dt=_epoch_to_dt(prev.get('ts'))
                    if dt and dt.date()<now_ist().date() and (now_ist().date()-dt.date()).days<=5 and float(prev.get('close') or 0)>0:
                        q['prev_close_price']=prev['close']; q['baseline_source']='Upstox OHLC prev_ohlc'
        except Exception as exc:
            for q in indexes.values(): q['ohlc_baseline_error']=str(exc)
    for k,q in indexes.items():
        if float(q.get('prev_close_price') or 0)>0: continue
        try:
            key=k.replace(':','|',1)
            if key not in INDEX_KEYS: raise ValueError('Unrecognized index response identity')
            daily=history(key,unit='days',interval=1,lookback_days=14)
            if daily.empty: raise ValueError('No index daily history returned')
            completed=daily[daily.timestamp.dt.date<now_ist().date()].sort_values('timestamp')
            if completed.empty: raise ValueError('No completed prior-session index candle')
            last=completed.iloc[-1]; age=(now_ist().date()-last.timestamp.date()).days
            if not 1<=age<=5 or not math.isfinite(float(last.close)) or last.close<=0:
                raise ValueError('Invalid or outdated index daily baseline')
            q['prev_close_price']=float(last.close)
            q['baseline_source']='Upstox V3 completed daily candle'
            q['baseline_date']=str(last.timestamp.date())
            q['baseline_timestamp']=last.timestamp.isoformat()
        except Exception as exc:
            q['baseline_error']=str(exc)  # Preserve diagnosis; never substitute zero as valid.
    return indexes

def breadth_snapshot(universe, quotes):
    qm=quote_by_symbol(quotes); adv=dec=flat=usable=0
    industries={}
    for sym,meta in universe['instruments'].items():
        q=qm.get(meta.get('upstox_symbol') or sym)
        if not q: continue
        l=float(q.get('last_price') or 0); pc=float(q.get('prev_close_price') or 0)
        if l<=0 or pc<=0: continue
        usable+=1; ch=(l/pc-1)*100
        if ch>0.05: adv+=1
        elif ch<-0.05: dec+=1
        else: flat+=1
        ind=str(meta.get('industry') or 'Unknown')
        d=industries.setdefault(ind,{'adv':0,'dec':0,'n':0,'sum_change':0.0})
        d['n']+=1; d['sum_change']+=ch
        if ch>0.05:d['adv']+=1
        elif ch<-0.05:d['dec']+=1
    for d in industries.values(): d['avg_change_pct']=round(d['sum_change']/d['n'],3) if d['n'] else None
    ranked=sorted(industries.items(),key=lambda kv:kv[1].get('avg_change_pct') if kv[1].get('avg_change_pct') is not None else -999,reverse=True)
    return {'usable':usable,'advancers':adv,'decliners':dec,'flat':flat,'advance_decline_ratio':round(adv/max(dec,1),2),'strongest_industries':ranked[:8],'weakest_industries':ranked[-8:],'industries':industries}

def regime(indexes,breadth):
    # Conservative local regime. Event/news risk remains an external verification gate.
    vals={}
    for k,q in indexes.items():
        l=float(q.get('last_price') or 0); pc=float(q.get('prev_close_price') or 0)
        vals[k]={'last_price':l,'prev_close':pc,'change_pct':round((l/pc-1)*100,3) if l>0 and pc>0 else None,'quote_timestamp':quote_time(q).isoformat() if quote_time(q) else None,'fresh':fresh_quote(q),'baseline_source':q.get('baseline_source','prev_close_price'),'baseline_date':q.get('baseline_date'),'baseline_timestamp':q.get('baseline_timestamp'),'baseline_error':q.get('baseline_error'),'ohlc_baseline_error':q.get('ohlc_baseline_error')}
    nifty=next((v for k,v in vals.items() if 'Nifty 50' in k),{}).get('change_pct')
    bank=next((v for k,v in vals.items() if 'Nifty Bank' in k),{}).get('change_pct')
    ratio=breadth.get('advance_decline_ratio',1)
    if len(vals)!=4 or any(v['change_pct'] is None or not v['fresh'] for v in vals.values()): label='UNVERIFIED'
    elif nifty>=0.6 and ratio>=1.4: label='STRONG BULLISH'
    elif nifty>=0.15 and ratio>=1.05: label='BULLISH'
    elif nifty<=-0.6 and ratio<=0.72: label='STRONG BEARISH'
    elif nifty<=-0.15 and ratio<=0.95: label='BEARISH'
    else: label='NEUTRAL-RANGE'
    return {'label':label,'indices':vals}

def rejection_summary(candidates):
    """Count each structured blocker at most once per candidate."""
    counts=Counter()
    for candidate in candidates:
        blockers=candidate.get('blockers')
        if not isinstance(blockers,list) or not blockers:
            # Older saved reports have only prose. Keep each full reason intact:
            # semicolons also appear *inside* individual blocker descriptions.
            blockers=[candidate.get('reason') or 'Unspecified rejection']
        counts.update(set(str(reason) for reason in blockers))
    return [{'reason':reason,'count':count} for reason,count in sorted(counts.items(),key=lambda item:(-item[1],item[0]))]

def save_report(report):
    report['rejection_summary']=rejection_summary(report.get('rejected',[]))
    report['data_issue_summary']=rejection_summary(report.get('data_unavailable',[]))
    if report['outcome']=='NO TRADE':
        top=report['rejection_summary'][:3]
        detail=' | '.join(f"{item['count']}: {item['reason']}" for item in top)
        report['reason']=('No candidate passed implemented technical gates. '+detail+'. Full trading strategy remains unvalidated')
    report=sanitize(report)
    stamp=now_ist().strftime('%Y%m%d_%H%M%S')
    path=REPORTS/f"{report['mode']}_{stamp}.json"
    atomic_json(path,report)
    print('RESULT='+report['outcome']); print(f'report={path}')
    print('CLOUD_SUMMARY_JSON='+json.dumps({
        'timestamp':report['timestamp'],'mode':report['mode'],'outcome':report['outcome'],
        'reason':report.get('reason'),'market_regime':report.get('market_regime',{}).get('label'),
        'coverage':report.get('coverage',{}),'watch_count':len(report['watches']),
        'trade_count':len(report['trades']),'top_watches':report['watches'][:5],
        'test_data':report.get('test_data'),'scanner_version':report.get('scanner_version'),
        'setup_type':report.get('setup_type'),'rejection_summary':report['rejection_summary']},allow_nan=False))
    return report

def run(mode):
    for p in (DATA,REPORTS,LOGS): p.mkdir(parents=True,exist_ok=True)
    report={'timestamp':now_ist().isoformat(),'mode':mode,'schema_version':2,'scanner_version':VERSION,
            'test_data':mode=='test','outcome':'SCAN FAILED','reason':None,'trades':[],'watches':[],
            'setup_type':'MIDDAY_CONSOLIDATION_BREAKOUT' if mode=='midday' else 'OPENING_RANGE_BREAKOUT',
            'strategy_limitations':['WATCH-only; actionable TRADE signals disabled',
                                    'Midday consolidation setup is experimental; performance unvalidated',
                                    'Independent news/events, spread and execution checks unavailable'],
            'readiness':{'signals':'WATCH_ONLY','news_events':'UNAVAILABLE',
                         'execution_spread':'UNAVAILABLE','strategy_performance':'UNVALIDATED',
                         'constituent_currency':'UNVERIFIED_LOCAL_CSV'},
            'rejected':[],'data_unavailable':[],'coverage':{},
            'policy_note':'WATCH-only repair. No automated TRADE: verified news/events, full execution checks and strategy validation are not implemented.'}
    try:
        C=cfg()
        if not TOKEN: raise RuntimeError('UPSTOX_ACCESS_TOKEN missing from .env')
        allowed,why=session_gate(mode,C); report['session_status']=why
        if not allowed:
            report['outcome']='NO SCAN'; report['reason']=why; return save_report(report)
        master=download_master(); official=download_universe(); universe=reconcile(master,official)
        raw,errors=bulk_quotes(universe)
        quotes,freshness=clean_quotes(raw,C['freshness']['max_quote_age_minutes'])
        report['data_freshness']=freshness
        report['coverage']={'official_rows':universe['official_rows'],'mapped':universe['mapped_count'],
            'unmapped':universe['unmapped'],'quotes_received':len(raw),'fresh_quotes':len(quotes),
            'quote_errors':errors,'stage1_survivors':0,'deep_evaluated':0,'technical_valid':0}
        if not quotes: raise ValueError('No fresh current-session quotes; diagnostic mode also refuses stale signals')
        breadth=breadth_snapshot(universe,quotes); indexes=index_snapshot(); market=regime(indexes,breadth)
        report.update(breadth=breadth,market_regime=market)
        survivors=stage1(universe,quotes,C['stage1']); report['coverage']['stage1_survivors']=len(survivors)
        deep=[]; valid=0
        for row in survivors[:C['technical']['max_deep_candidates']]:
            meta=universe['instruments'][row['symbol']]
            try:
                interval=C['technical']['intraday_interval_minutes']
                hist=history(meta['instrument_key'],interval=interval)
                daily=history(meta['instrument_key'],unit='days',interval=1,lookback_days=180)
                if len(daily)<50: raise ValueError('Insufficient daily trend history')
                current=intraday(meta['instrument_key'],interval)
                t=technical(current,C['technical']['opening_range_minutes'],hist,interval)
                if abs(row['ltp']-t['close'])>2*t['atr']: raise ValueError('Quote/candle divergence exceeds 2 ATR')
                t['daily_ema20']=float(daily.close.ewm(span=20,adjust=False).mean().iloc[-1])
                t['daily_ema50']=float(daily.close.ewm(span=50,adjust=False).mean().iloc[-1])
                t['resistance_levels']=sorted(set([t['previous_high']]+daily.tail(20).high.tolist()))
                t['support_levels']=sorted(set([t['previous_low']]+daily.tail(20).low.tolist()))
                s=score_candidate(row,t,C['technical'],mode=mode)
                if s['status']!='DATA_UNAVAILABLE': valid+=1
                avg=breadth['industries'].get(row.get('industry'),{}).get('avg_change_pct')
                direction=s.get('direction')
                aligned=avg is not None and ((direction=='LONG' and avg>=0) or (direction=='SHORT' and avg<=0))
                trend=(direction=='LONG' and t['daily_ema20']>=t['daily_ema50']) or (direction=='SHORT' and t['daily_ema20']<=t['daily_ema50'])
                blockers=[]
                if not aligned: blockers.append('Industry alignment unavailable/adverse')
                if not trend: blockers.append('Daily trend is adverse')
                if market['label']=='UNVERIFIED': blockers.append('Market regime unverified')
                if mode=='test': blockers.append('Diagnostic output only')
                if direction=='LONG' and market['label'] in ('BEARISH','STRONG BEARISH'): blockers.append('Market direction is adverse')
                if direction=='SHORT' and market['label'] in ('BULLISH','STRONG BULLISH'): blockers.append('Market direction is adverse')
                if blockers and s['status'] in ('WATCH','REJECT'):
                    s['status']='REJECT'
                    s['blockers']=list(dict.fromkeys(s.get('blockers',[])+blockers))
                    s['reason']='; '.join(s['blockers'])
                deep.append({**row,**t,**s,'industry_avg_change_pct':avg,'instrument_key':meta['instrument_key']})
            except Exception as exc:
                deep.append({**row,'status':'DATA_UNAVAILABLE','score':None,'reason':str(exc)})
            time.sleep(.05)
        report['coverage'].update(deep_evaluated=len(deep),technical_valid=valid,usable_quotes=breadth['usable'])
        market=regime(indexes,breadth)
        report['market_regime']=market  # Recheck index freshness after history requests.
        for candidate in deep:
            if candidate['status']=='WATCH':
                dt=_epoch_to_dt(candidate.get('quote_timestamp'))
                if dt is None or (now_ist()-dt).total_seconds()>C['freshness']['max_quote_age_minutes']*60:
                    candidate['status']='DATA_UNAVAILABLE'; candidate['reason']='Quote expired while history was fetched; rerun with cached history'
        report['watches']=[x for x in deep if x['status']=='WATCH']
        report['rejected']=[x for x in deep if x['status']=='REJECT']
        report['data_unavailable']=[x for x in deep if x['status']=='DATA_UNAVAILABLE']
        required=C['freshness'].get('min_usable_coverage',.95)
        if report['data_unavailable'] or market['label']=='UNVERIFIED' or breadth['usable']<required*max(universe['mapped_count'],1) or universe['mapped_count']<required*max(universe['official_rows'],1):
            report['outcome']='SCAN FAILED'; report['reason']='Essential data incomplete; see data_unavailable, market_regime and coverage'
            for candidate in report['watches']: candidate['status']='UNVALIDATED'
            report['unvalidated_candidates']=report['watches']; report['watches']=[]
        elif mode=='test':
            report['outcome']='DIAGNOSTIC ONLY'; report['reason']='Not a live trading decision'
        elif report['watches']:
            report['outcome']='WATCH ONLY'; report['reason']='Technical candidates require independent news/event and execution checks'
        else:
            report['outcome']='NO TRADE'; report['reason']='No candidate passed implemented technical gates; full trading strategy remains unvalidated'
    except Exception as exc:
        report['outcome']='SCAN FAILED'; report['reason']=str(exc)
    report['timestamp']=now_ist().isoformat()
    if mode!='test' and report['outcome'] in ('NO TRADE','WATCH ONLY'):
        # Paper reconciliation runs after live decision freshness checks. It
        # cannot promote a WATCH or change the scan's outcome to TRADE.
        try:
            report['paper_tracking']=update_paper_tracking(
                ROOT/'paper_tracking.sqlite3',report,
                lambda key,day: paper_candles(key,day),now_ist(),clock=now_ist)
        except Exception as exc:
            report['paper_tracking']={'status':'UNAVAILABLE','reason':type(exc).__name__}
        expired=[candidate for candidate in report['watches']
                 if (now_ist()-_epoch_to_dt(candidate['quote_timestamp'])).total_seconds()>C['freshness']['max_quote_age_minutes']*60]
        if expired:
            for candidate in expired:
                candidate['status']='DATA_UNAVAILABLE'; candidate['reason']='Quote expired during paper reconciliation'
            report['data_unavailable'].extend(expired)
            report['watches']=[]
            report['outcome']='SCAN FAILED'; report['reason']='Quote freshness expired before report delivery; no actionable signal'
    return save_report(report)

def paper_candles(key,day):
    """Fetch original five-minute bars; tracking never uses a price snapshot."""
    if day==now_ist().date():
        frame=intraday(key,5)
    else:
        frame=history(key,interval=5,lookback_days=(now_ist().date()-day).days+1)
    return [{'timestamp':r.timestamp.isoformat(),'open':float(r.open),'high':float(r.high),
             'low':float(r.low),'close':float(r.close)} for r in frame.itertuples()]

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--mode',choices=['morning','midday','test'],default='test'); args=ap.parse_args(); run(args.mode)
