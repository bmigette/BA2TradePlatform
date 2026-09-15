"""Read-only review of capped FMPRating and existing goal2020 candidates."""
import json
import hashlib
import sqlite3
from pathlib import Path
import pandas as pd

OUT=Path(__file__).parent
c=sqlite3.connect('file:C:/Users/basti/Documents/ba2/test/dl_forecasting.db?mode=ro',uri=True)
c.row_factory=sqlite3.Row
c.execute('PRAGMA query_only=ON')
c.execute('BEGIN')
rows=[]
for raw in c.execute('select id,name,status,start_date,end_date,initial_capital,annualized_return,total_return,max_drawdown,profit_factor,win_rate,total_trades,ga_fitness,strategy_params,trades,results from backtests where expert_name=?',('FMPRating',)):
    r=dict(raw)
    p=json.loads(r['strategy_params'] or '{}')
    if 'goal2020' not in r['name'] and p.get('equityCap')!=1000: continue
    if r['status']!='completed': continue
    tr=json.loads(r.pop('trades') or '[]')
    r.pop('results')
    r.pop('strategy_params')
    r['cap']=p.get('equityCap')
    r['settings']={k:v for k,v in p.items() if k.startswith(('model:','screener:','schedule:')) or k=='expertFixedSettings'}
    r['rules']={k:p.get(k) for k in ('entryRules','exitRules')}
    t=pd.DataFrame(tr)
    net=t.pnl.sum(); wins=t[t.pnl>0]
    gp=wins.pnl.sum()
    r['net_trade_pnl']=float(net)
    r['top1_net_pct']=float(t.pnl.max()/net*100) if net>0 else None
    r['top5_net_pct']=float(t.pnl.nlargest(5).sum()/net*100) if net>0 else None
    r['top5_gross_pct']=float(t.pnl.nlargest(5).sum()/gp*100) if gp>0 else None
    r['net_without_top5']=float(net-t.pnl.nlargest(5).sum())
    r['top_issuers']=t.groupby('symbol').pnl.sum().nlargest(5).round(2).to_dict()
    r['trade_hash']=hashlib.sha256(json.dumps(tr,sort_keys=True).encode()).hexdigest()
    entry=pd.to_datetime(t.entry_time,utc=True); end=pd.to_datetime(t.exit_time,utc=True)
    held=(end-entry).dt.total_seconds()/86400
    r['mean_holding_days']=float(held.mean())
    r['median_holding_days']=float(held.median())
    r['yearly_closed_pnl']=t.pnl.groupby(end.dt.year).sum().round(2).to_dict()
    instcap=p.get('model:max_virtual_equity_per_instrument_percent')
    r['per_instrument_pct']=instcap
    if instcap is not None:
        ceiling=1000*instcap/100
        r['full_balance_1000_notional_ceiling']=ceiling
        r['historical_entries_price_fits_one_share_pct']=float((t.entry_price<=ceiling).mean()*100)
    r['mean_entry_cost_proxy']=float((t.entry_price*t['size']*held).sum()/((pd.Timestamp(r['end_date'])-pd.Timestamp(r['start_date'])).total_seconds()/86400))
    rows.append(r)

core_ids=[1407,1420,1423,1409]
cap_ids=[r['id'] for r in rows if r['cap']==1000]
series={}
for bid in core_ids+cap_ids:
    raw=c.execute('select equity_curve,max_drawdown from backtests where id=?',(bid,)).fetchone()
    df=pd.DataFrame(json.loads(raw['equity_curve']))
    s=pd.Series(df.equity.to_numpy(float),index=pd.to_datetime(df.date,utc=True)).sort_index()
    assert s.index.is_unique and (s>0).all()
    curve=1000+(s.div(s.shift()).sub(1).fillna(0)*1000).cumsum()
    assert abs(float(((curve-curve.cummax())/10).min())-raw['max_drawdown'])<.011
    series[bid]=curve
c.close()
start=max(s.index[0] for s in series.values())
end=min(s.index[-1] for s in series.values())
aligned=pd.concat(series,axis=1,sort=True).sort_index().ffill().loc[start:end]
assert aligned.notna().all().all()
aligned=aligned-aligned.iloc[0]
daily=aligned.groupby(aligned.index.normalize()).last().diff().fillna(0)
core=aligned[core_ids].sum(axis=1)
combo={}
for bid in [None]+cap_ids:
    s=core+(0 if bid is None else aligned[bid])
    item={'pnl':float(s.iloc[-1]),'drawdown_pct_5000_cap':float(((s-s.cummax())/50).min())}
    if bid:
        item['correlation_core']=float(daily[bid].corr(daily[core_ids].sum(axis=1)))
        item['pnl_rating']=float(aligned[bid].iloc[-1])
    combo['cash' if bid is None else bid]=item
out={'rating_rows':rows,'capped_ids':cap_ids,'common_start':str(start),'common_end':str(end),'combined_fifth_sleeve':combo}
(OUT/'fmprating_1000_review.json').write_text(json.dumps(out,indent=2,default=str),encoding='utf-8')
print('CAPPED',json.dumps([{k:v for k,v in r.items() if k not in ('settings','rules','trade_hash')} for r in rows if r['cap']==1000],indent=2))
print('COMBO',json.dumps(combo,indent=2))
print('UNCAPPED CANDIDATES')
for r in rows:
    if r['cap'] is None and r['annualized_return']>=10 and abs(r['max_drawdown'])<=15:
        print(json.dumps({k:r[k] for k in ('id','name','annualized_return','max_drawdown','profit_factor','total_trades','top1_net_pct','top5_gross_pct','top5_net_pct','net_without_top5','mean_holding_days','per_instrument_pct','historical_entries_price_fits_one_share_pct')}))
