"""Read-only comparison of saved capped Earnings Drift runs; no engine startup."""
import json
import sqlite3
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).parent
conn = sqlite3.connect('file:C:/Users/basti/Documents/ba2/test/dl_forecasting.db?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA query_only=ON')
conn.execute('BEGIN')
meta = [dict(r) for r in conn.execute('select id,name,expert_name,status,strategy_params from backtests')]
candidate_ids=[]
for r in meta:
    p=json.loads(r['strategy_params'] or '{}')
    if p.get('equityCap')==1000 and r['expert_name']=='FMPEarningsDrift':
        candidate_ids.append(r['id'])
core_ids=[1407,1420,1409]
rows={}
for bid in core_ids+candidate_ids:
    r=dict(conn.execute('select * from backtests where id=?',(bid,)).fetchone())
    for k in ('strategy_params','results','trades','equity_curve','drawdown_curve'):
        r[k]=json.loads(r[k] or '{}')
    rows[bid]=r
conn.close()

series={}
stats={}
trades={}
for bid,r in rows.items():
    assert r['status']=='completed'
    assert r['strategy_params']['equityCap']==1000
    frame=pd.DataFrame(r['equity_curve'])
    s=pd.Series(frame.equity.to_numpy(float), index=pd.to_datetime(frame.date,utc=True)).sort_index()
    assert s.index.is_unique
    assert (s>0).all()
    pnl=s.div(s.shift()).sub(1).fillna(0)*1000
    real=1000+pnl.cumsum()
    dd=float(((real-real.cummax())/10).min())
    assert abs(dd-r['max_drawdown'])<0.011
    tr=pd.DataFrame(r['trades'])
    tr['entry']=pd.to_datetime(tr.entry_time,utc=True)
    tr['exit']=pd.to_datetime(tr.exit_time,utc=True)
    tr['cost']=tr.entry_price*tr['size']
    tr['days']=(tr.exit-tr.entry).dt.total_seconds()/86400
    assert abs(real.iloc[-1]-1000-tr.pnl.sum())<0.01
    capital_days=float((tr.cost*tr.days).sum())
    calendar_days=(real.index[-1]-real.index[0]).total_seconds()/86400
    yearly=pnl.groupby(pnl.index.year).sum()
    series[bid]=real
    trades[bid]=tr
    stats[bid]={
        'id':bid,'name':r['name'],'start':r['start_date'],'end':r['end_date'],
        'pnl':float(pnl.sum()),'drawdown_pct':dd,
        'win_rate':r['win_rate'],'profit_factor':r['profit_factor'],'trade_count':len(tr),
        'holding_mean_days':float(tr.days.mean()),'holding_median_days':float(tr.days.median()),
        'top5_trade_net_pnl_pct':float(tr.pnl.nlargest(5).sum()/tr.pnl.sum()*100),
        'top5_issuer_net_pnl_pct':float(tr.groupby('symbol').pnl.sum().nlargest(5).sum()/tr.pnl.sum()*100),
        'yearly_pnl':yearly.round(2).to_dict(),
        'mean_entry_cost_exposure_proxy':capital_days/calendar_days,
        'pnl_per_exposure_dollar_year':float(tr.pnl.sum())/(capital_days/365.25),
        'open_position_count':len(r['results'].get('open_positions',[])),
        'stored_scoring_annualized_return':r['annualized_return'],
        'stored_scoring_total_return':r['total_return'],
    }
    if bid in candidate_ids:
        p=r['strategy_params']
        stats[bid]['settings']={k:v for k,v in p.items() if k.startswith(('model:','screener:','schedule:')) or k=='expertFixedSettings'}
        stats[bid]['rules']={k:p[k] for k in ('entryRules','exitRules')}

aligned=pd.concat(series,axis=1,sort=True).sort_index().ffill()
assert aligned.notna().all().all()
daily=aligned.groupby(aligned.index.normalize()).last().diff().fillna(0)
core=aligned[core_ids].sum(axis=1)
comparisons={}
for bid in [None]+candidate_ids:
    total=core+(1000 if bid is None else aligned[bid])
    daily_combined=total.groupby(total.index.normalize()).last().diff().fillna(0)
    dd=(total-total.cummax())/40
    yearly=daily_combined.groupby(daily_combined.index.year).sum()/40
    combo={'pnl':float(total.iloc[-1]-4000),'net_pnl_pct_cap':float((total.iloc[-1]-4000)/40),
        'drawdown_pct_cap':float(dd.min()),'worst_dd_at':str(dd.idxmin()),
        'worst_daily_pnl':float(daily_combined.min()),
        'daily_bottom5pct_mean_pnl':float(daily_combined[daily_combined<=daily_combined.quantile(.05)].mean()),
        'annual_pnl_pct_cap':yearly.round(2).to_dict(),
        'daily_pnl_std':float(daily_combined.std())}
    if bid is not None:
        combo['correlation_with_core']=float(daily[bid].corr(daily[core_ids].sum(axis=1)))
        combo['correlation_each_core']={b:float(daily[bid].corr(daily[b])) for b in core_ids}
        overlap={}
        for b in core_ids:
            left,right=trades[bid],trades[b]
            symbols=[]
            for sym in set(left.symbol)&set(right.symbol):
                for _,t in left[left.symbol==sym].iterrows():
                    rr=right[right.symbol==sym]
                    if ((rr.entry<t.exit)&(rr.exit>t.entry)).any():
                        symbols.append(sym);break
            overlap[b]=symbols
        combo['simultaneous_closed_trade_symbols_with_core']=overlap
    comparisons['cash' if bid is None else bid]=combo

# Retrospective attribution only: the scale uses this sample's measured P&L,
# so this is not an executable allocation rule or out-of-sample comparator.
ratio=stats[1406]['pnl']/stats[1423]['pnl']
scaled_total=core+1000+(aligned[1423]-1000)*ratio
matched={'current_small_scale':ratio,
         'combined_drawdown_pct_cap':float(((scaled_total-scaled_total.cummax())/40).min()),
         'mean_exposure_proxy':stats[1423]['mean_entry_cost_exposure_proxy']*ratio,
         'label':'Retrospective equal-profit scaling diagnostic; ignores lot/fill changes'}

out={'core_ids':core_ids,'candidate_ids':candidate_ids,'standalone':stats,'combined':comparisons,'matched_profit_diagnostic':matched}
(ROOT/'earnings_drift_1000_comparison.json').write_text(json.dumps(out,indent=2,default=str),encoding='utf-8')
print(json.dumps({'candidate_ids':candidate_ids,'standalone':{b:{k:v for k,v in stats[b].items() if k not in ('rules','settings')} for b in candidate_ids},'combined':comparisons,'matched_profit_diagnostic':matched},indent=2,default=str))
