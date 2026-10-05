from __future__ import annotations
import argparse, gzip, json, math, os, sys, time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'; REPORTS=ROOT/'reports'; LOGS=ROOT/'logs'
for p in (DATA,REPORTS,LOGS): p.mkdir(exist_ok=True)
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
    r=requests.get(url,headers=headers,params=params,timeout=timeout); r.raise_for_status(); return r

def download_master():
    raw=gzip.decompress(req(UPSTOX_MASTER,timeout=60).content)
    arr=json.loads(raw.decode())
    (DATA/'nse_master.json').write_text(json.dumps(arr))
    return arr

def download_universe():
    df=pd.read_csv(pd.io.common.BytesIO(req(NIFTY500,headers=UA,timeout=30).content))
    return df

def reconcile(master, df):
    # Primary match by ISIN; symbol validation/fallback. Keep NSE cash-market segment, not only type EQ,
    # because index methodologies can contain non-common-equity cash instruments.
    cash=[x for x in master if x.get('segment')=='NSE_EQ']
    by_isin={str(x.get('isin','')).strip().upper():x for x in cash if x.get('isin')}
    by_symbol={str(x.get('trading_symbol','')).strip().upper():x for x in cash if x.get('trading_symbol')}
    mapped={}; unmapped=[]; mismatches=[]
    for _,row in df.iterrows():
        sym=str(row.get('Symbol','')).strip().upper(); isin=str(row.get('ISIN Code','')).strip().upper()
        item=by_isin.get(isin) or by_symbol.get(sym)
        if not item: unmapped.append({'symbol':sym,'isin':isin,'series':str(row.get('Series',''))}); continue
        up_sym=str(item.get('trading_symbol','')).strip().upper()
        if up_sym!=sym: mismatches.append({'official':sym,'upstox':up_sym,'isin':isin})
        mapped[sym]={'company':row.get('Company Name'),'industry':row.get('Industry'),'series':row.get('Series'),'isin':isin,
                     'instrument_key':item.get('instrument_key'),'instrument_type':item.get('instrument_type'),'upstox_symbol':up_sym}
    out={'generated_at':datetime.now().astimezone().isoformat(),'official_rows':len(df),'mapped_count':len(mapped),'unmapped':unmapped,'symbol_mismatches':mismatches,'instruments':mapped}
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
    try:
        x=float(value)
        if x > 10_000_000_000: x/=1000.0
        return datetime.fromtimestamp(x).astimezone()
    except Exception:
        return None

def market_data_freshness(quotes, max_age_minutes=20):
    """Fail-closed freshness check for scheduled live scans.
    Uses Upstox last_trade_time/timestamp fields when available.
    """
    now=datetime.now().astimezone(); seen=[]
    for q in quotes.values():
        candidates=[q.get('last_trade_time'), q.get('timestamp'), q.get('ts')]
        o=q.get('ohlc') or {}
        candidates += [o.get('ts'), o.get('timestamp')]
        for v in candidates:
            dt=_epoch_to_dt(v)
            if dt:
                seen.append(dt); break
    if not seen:
        return False, {'reason':'no attributable quote timestamps available','timestamped_quotes':0}
    same_day=[d for d in seen if d.date()==now.date()]
    if not same_day:
        return False, {'reason':'no current-session quote timestamps','timestamped_quotes':len(seen),'latest':max(seen).isoformat()}
    latest=max(same_day); age=(now-latest).total_seconds()/60
    ok=age <= max_age_minutes
    return ok, {'reason':'fresh' if ok else f'latest quote is {age:.1f} minutes old', 'timestamped_quotes':len(seen), 'current_day_quotes':len(same_day), 'latest':latest.isoformat(), 'age_minutes':round(age,1)}

def quote_by_symbol(q):
    out={}
    for _,v in q.items():
        sym=str(v.get('symbol','')).strip().upper()
        if sym: out[sym]=v
    return out

def stage1(universe, quotes, c):
    qm=quote_by_symbol(quotes); rows=[]
    for sym,meta in universe['instruments'].items():
        q=qm.get(sym)
        if not q: continue
        ltp=float(q.get('last_price') or 0); pc=float(q.get('prev_close_price') or 0)
        o=q.get('ohlc') or {}; vol=float(o.get('volume') or 0)
        if ltp<=0 or pc<=0: continue
        ch=(ltp/pc-1)*100; turnover=ltp*vol/1e7
        if ltp<c['min_price'] or turnover<c['min_turnover_crore'] or abs(ch)<c['min_abs_change_pct'] or abs(ch)>c['max_abs_change_pct']: continue
        # Stage 1 score ranks activity; it is NOT the final trade score.
        score=min(35,math.log10(max(turnover,1))*10)+min(35,abs(ch)*7)+min(30,math.log10(max(vol,1))*4)
        rows.append({'symbol':sym,'industry':meta.get('industry'),'ltp':ltp,'change_pct':ch,'volume':int(vol),'turnover_cr':turnover,'stage1_score':score,'year_high':q.get('year_high'),'year_low':q.get('year_low')})
    return sorted(rows,key=lambda x:x['stage1_score'],reverse=True)[:c['max_stage1_survivors']]

def intraday(key, interval):
    url=INTRADAY.format(key=quote(key,safe=''),interval=interval)
    j=req(url,headers=HEADERS,timeout=20).json(); candles=(j.get('data') or {}).get('candles') or []
    if not candles: return pd.DataFrame()
    df=pd.DataFrame(candles,columns=['timestamp','open','high','low','close','volume','oi'])
    df['timestamp']=pd.to_datetime(df['timestamp']); df=df.sort_values('timestamp').reset_index(drop=True)
    for col in ['open','high','low','close','volume']: df[col]=pd.to_numeric(df[col],errors='coerce')
    return df.dropna(subset=['open','high','low','close'])

def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); dn=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean(); rs=up/dn.replace(0,np.nan); return 100-(100/(1+rs))
def atr(df,n=14):
    pc=df.close.shift(); tr=pd.concat([(df.high-df.low).abs(),(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1); return tr.ewm(alpha=1/n,adjust=False).mean()
def technical(df, opening_minutes=15):
    if len(df)<20: return None
    tp=(df.high+df.low+df.close)/3; cv=df.volume.cumsum().replace(0,np.nan); vwap=(tp*df.volume).cumsum()/cv
    ema9=df.close.ewm(span=9,adjust=False).mean(); ema20=df.close.ewm(span=20,adjust=False).mean(); ema50=df.close.ewm(span=50,adjust=False).mean()
    rs=rsi(df.close); at=atr(df); macd=df.close.ewm(span=12,adjust=False).mean()-df.close.ewm(span=26,adjust=False).mean(); sig=macd.ewm(span=9,adjust=False).mean()
    n=max(1,opening_minutes//max(1,int(round((df.timestamp.iloc[1]-df.timestamp.iloc[0]).total_seconds()/60)))) if len(df)>1 else 3
    opening=df.iloc[:n]; last=df.iloc[-1]
    rv=(df.volume.iloc[-1]/df.volume.iloc[-6:-1].mean()) if len(df)>=6 and df.volume.iloc[-6:-1].mean()>0 else np.nan
    return {'close':float(last.close),'vwap':float(vwap.iloc[-1]),'ema9':float(ema9.iloc[-1]),'ema20':float(ema20.iloc[-1]),'ema50':float(ema50.iloc[-1]),'rsi':float(rs.iloc[-1]),'atr':float(at.iloc[-1]),'macd':float(macd.iloc[-1]),'macd_signal':float(sig.iloc[-1]),'rvol_5bar':float(rv) if pd.notna(rv) else None,'opening_high':float(opening.high.max()),'opening_low':float(opening.low.min()),'day_high':float(df.high.max()),'day_low':float(df.low.min()),'bars':len(df),'last_bar':str(last.timestamp)}

def score_candidate(row,t,cfgtech):
    if not t or not t.get('atr') or t['atr']<=0: return {'status':'REJECT','score':0,'reason':'Insufficient intraday technical data'}
    p=t['close']; bullish=p>t['vwap'] and t['ema9']>=t['ema20']; bearish=p<t['vwap'] and t['ema9']<=t['ema20']
    direction='LONG' if bullish else ('SHORT' if bearish else 'NONE')
    score=0; reasons=[]
    if direction!='NONE': score+=25; reasons.append('VWAP/EMA alignment')
    if direction=='LONG' and 52<=t['rsi']<=72: score+=15; reasons.append('constructive RSI')
    if direction=='SHORT' and 28<=t['rsi']<=48: score+=15; reasons.append('constructive bearish RSI')
    if (direction=='LONG' and t['macd']>t['macd_signal']) or (direction=='SHORT' and t['macd']<t['macd_signal']): score+=10; reasons.append('MACD confirms')
    if t.get('rvol_5bar') and t['rvol_5bar']>=1.2: score+=15; reasons.append('recent volume expansion')
    if abs(row['change_pct'])>=0.6: score+=10; reasons.append('meaningful session move')
    if row['turnover_cr']>=25: score+=10; reasons.append('strong turnover')
    if direction=='LONG': trigger=max(t['opening_high'],p); stop=min(t['vwap'],p-cfgtech['stop_atr_multiple']*t['atr']); risk=trigger-stop
    elif direction=='SHORT': trigger=min(t['opening_low'],p); stop=max(t['vwap'],p+cfgtech['stop_atr_multiple']*t['atr']); risk=stop-trigger
    else: trigger=stop=risk=np.nan
    target=None; rr=None
    if direction!='NONE' and risk>0:
        desired=min(cfgtech['target_pct_cap']/100*trigger,2*risk); target=trigger+desired if direction=='LONG' else trigger-desired; rr=desired/risk
        if rr>=cfgtech['min_reward_risk']: score+=15; reasons.append('>=2R modeled path')
    # hard guards: no actionable trade if score/structure is inadequate. News/sector context is not yet verified here.
    status='REJECT'
    if direction!='NONE' and rr is not None and rr>=cfgtech['min_reward_risk']:
        status='WATCH' if score>=cfgtech['min_watch_score'] else 'REJECT'
        # Deliberately never auto-promote to TRADE without external market/sector/news confirmation.
    return {'direction':direction,'score':int(score),'status':status,'trigger':None if pd.isna(trigger) else round(float(trigger),2),'stop':None if pd.isna(stop) else round(float(stop),2),'target':None if target is None else round(float(target),2),'rr':None if rr is None else round(float(rr),2),'reason':'; '.join(reasons)}



def session_gate(mode, C):
    now=datetime.now().astimezone()
    if mode=='test': return True, 'test mode bypass'
    if now.weekday()>=5: return False, 'weekend'
    windows=C.get('session_windows',{})
    w=windows.get(mode)
    if not w: return False, f'no configured {mode} window'
    sh,sm=map(int,w['start'].split(':')); eh,em=map(int,w['end'].split(':'))
    cur=now.timetz().replace(tzinfo=None)
    if not (dtime(sh,sm) <= cur <= dtime(eh,em)):
        return False, f"outside {mode} window {w['start']}-{w['end']} IST"
    return True, 'inside configured session window'

def index_snapshot():
    try:
        j=req(QUOTE_URL,headers=HEADERS,params={'instrument_key':','.join(INDEX_KEYS)},timeout=20).json()
        return j.get('data',{}) if j.get('status')=='success' else {}
    except Exception:
        return {}

def breadth_snapshot(universe, quotes):
    qm=quote_by_symbol(quotes); adv=dec=flat=usable=0
    industries={}
    for sym,meta in universe['instruments'].items():
        q=qm.get(sym)
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
    ranked=sorted(industries.items(),key=lambda kv:kv[1].get('avg_change_pct') or -999,reverse=True)
    return {'usable':usable,'advancers':adv,'decliners':dec,'flat':flat,'advance_decline_ratio':round(adv/max(dec,1),2),'strongest_industries':ranked[:8],'weakest_industries':ranked[-8:]}

def regime(indexes,breadth):
    # Conservative local regime. Event/news risk remains an external verification gate.
    vals={}
    for k,q in indexes.items():
        l=float(q.get('last_price') or 0); pc=float(q.get('prev_close_price') or 0)
        vals[k]={'last_price':l,'prev_close':pc,'change_pct':round((l/pc-1)*100,3) if l>0 and pc>0 else None}
    nifty=next((v for k,v in vals.items() if 'Nifty 50' in k),{}).get('change_pct')
    bank=next((v for k,v in vals.items() if 'Nifty Bank' in k),{}).get('change_pct')
    ratio=breadth.get('advance_decline_ratio',1)
    if nifty is None: label='UNVERIFIED'
    elif nifty>=0.6 and ratio>=1.4: label='STRONG BULLISH'
    elif nifty>=0.15 and ratio>=1.05: label='BULLISH'
    elif nifty<=-0.6 and ratio<=0.72: label='STRONG BEARISH'
    elif nifty<=-0.15 and ratio<=0.95: label='BEARISH'
    else: label='NEUTRAL-RANGE'
    return {'label':label,'indices':vals}

def run(mode):
    if not TOKEN: sys.exit('ERROR: UPSTOX_ACCESS_TOKEN missing from .env')
    C=cfg(); print(f'[{datetime.now().astimezone().isoformat()}] {mode.upper()} scanner starting')
    allowed,why=session_gate(mode,C)
    if not allowed:
        print('\n=== RESULT ===')
        print(f'NO SCAN — MARKET CLOSED / {why.upper()}')
        return
    master=download_master(); official=download_universe(); universe=reconcile(master,official)
    print(f"Official rows={universe['official_rows']} mapped={universe['mapped_count']} unmapped={len(universe['unmapped'])}")
    quotes,errors=bulk_quotes(universe); print(f'Quote responses={len(quotes)} API errors={len(errors)}')
    freshness={'reason':'test mode bypass'}
    if mode!='test':
        fresh,freshness=market_data_freshness(quotes,C.get('freshness',{}).get('max_quote_age_minutes',20))
        print(f"Freshness={freshness}")
        if not fresh:
            print('\n=== RESULT ===')
            print(f"NO SCAN — CURRENT NSE SESSION DATA NOT VERIFIED: {freshness.get('reason')}")
            return
    indexes=index_snapshot(); breadth=breadth_snapshot(universe,quotes); market=regime(indexes,breadth)
    print(f"Market regime={market['label']} breadth={breadth['advancers']}/{breadth['decliners']} usable={breadth['usable']}")
    survivors=stage1(universe,quotes,C['stage1']); print(f'Stage-1 survivors={len(survivors)}')
    deep=[]
    for row in survivors[:C['technical']['max_deep_candidates']]:
        meta=universe['instruments'][row['symbol']]
        try:
            df=intraday(meta['instrument_key'],C['technical']['intraday_interval_minutes']); t=technical(df,C['technical']['opening_range_minutes']); s=score_candidate(row,t,C['technical']);
            # Industry alignment adjustment from verified constituent breadth.
            ind=dict(breadth.get('strongest_industries',[])+breadth.get('weakest_industries',[])).get(row.get('industry'),{})
            avg=ind.get('avg_change_pct') if ind else None
            if avg is not None and s.get('direction')=='LONG' and avg<0: s['score']=max(0,s['score']-10); s['reason']+='; industry misalignment'
            if avg is not None and s.get('direction')=='SHORT' and avg>0: s['score']=max(0,s['score']-10); s['reason']+='; industry misalignment'
            if mode=='morning' and t and t.get('bars',0)<3: s={'status':'REJECT','score':0,'reason':'insufficient opening confirmation'}
            deep.append({**row,**(t or {}),**s,'industry_avg_change_pct':avg})
        except Exception as e: deep.append({**row,'status':'REJECT','score':0,'reason':f'data error: {e}'})
        time.sleep(.05)
    deep=sorted(deep,key=lambda x:x.get('score',0),reverse=True)
    actionable=[x for x in deep if x.get('status')=='TRADE']; watches=[x for x in deep if x.get('status')=='WATCH']
    report={'timestamp':datetime.now().astimezone().isoformat(),'mode':mode,'data_freshness':freshness,'market_regime':market,'breadth':breadth,'coverage':{'official_rows':universe['official_rows'],'mapped':universe['mapped_count'],'unmapped':universe['unmapped'],'quotes_received':len(quotes),'quote_errors':errors,'stage1_survivors':len(survivors),'deep_evaluated':len(deep)},'policy_note':'Fail-closed policy: local technical candidates remain WATCH until material company/macro news and scheduled-event risk are independently verified. Market regime and industry breadth are included locally.','trades':actionable,'watches':watches,'rejected':deep}
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); path=REPORTS/f'{mode}_{stamp}.json'; path.write_text(json.dumps(report,indent=2,default=str))
    print('\n=== RESULT ==='); print('NO ACTIONABLE TRADE' if not actionable else f'{len(actionable)} ACTIONABLE TRADE(S)')
    print(f'WATCH={len(watches)} DEEP_EVALUATED={len(deep)} report={path}')
    for x in watches[:10]: print(f"WATCH {x['symbol']:12} {x.get('direction'):5} score={x.get('score')} LTP={x.get('ltp'):.2f} trigger={x.get('trigger')} stop={x.get('stop')} target={x.get('target')} RR={x.get('rr')}")
    print('CLOUD_SUMMARY_JSON=' + json.dumps({'timestamp':report['timestamp'],'mode':mode,'market_regime':market.get('label'),'coverage':report['coverage'],'watch_count':len(watches),'trade_count':len(actionable),'top_watches':[{k:x.get(k) for k in ['symbol','direction','score','ltp','trigger','stop','target','rr','reason']} for x in watches[:5]]},default=str))

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--mode',choices=['morning','midday','test'],default='test'); args=ap.parse_args(); run(args.mode)
