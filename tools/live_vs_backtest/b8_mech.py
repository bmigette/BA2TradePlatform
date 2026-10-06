import json, collections
import pandas as pd, numpy as np
from pathlib import Path
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
CACHE=Path('C:/Users/basti/Documents/ba2/common/cache/FMPOHLCVProvider')
tx=pd.read_csv(L+'b1_live_tx.csv'); tx=tx[tx.expert_id.isin([7,8,9,10,11,12,13])].copy()
b=pd.read_csv(L+'b4_buy_recs.csv')
tx['D']=tx.open_date.str[:10]
recp=b.set_index(['instance_id','symbol','D']).price_at_date
def bars(sym):
    f=CACHE/f'{sym}_1d.parquet'
    if not f.exists(): return None
    c=pd.read_parquet(f); d=pd.to_datetime(c['Date'])
    c['d']=(d.dt.tz_convert('UTC').dt.tz_localize(None) if d.dt.tz is not None else d).dt.normalize()
    return c.sort_values('d').reset_index(drop=True)
rows=[]
for _,t in tx.iterrows():
    r=dict(inst=t.expert_id,sym=t.symbol,D=t.D,qty=t.quantity,fill=t.open_price,recprice=recp.get((t.expert_id,t.symbol,t.D)),reason=t.close_reason,closed=t.status=='CLOSED')
    cb=bars(t.symbol)
    D=pd.Timestamp(t.D)
    if cb is not None:
        sm=cb[cb.d==D]
        if len(sm): r['d_open']=float(sm.Open.iloc[0]); r['d_close']=float(sm.Close.iloc[0]); r['has_after']=bool((cb.d>D).any())
        # exit timing check
        if t.status=='CLOSED' and t.close_reason in('oco_leg_filled','tp_sl_filled'):
            E=pd.Timestamp(str(t.close_date)[:10])
            leg='TP' if abs(t.close_price-t.take_profit)<abs(t.close_price-t.stop_loss) else 'SL'
            r['leg']=leg
            w=cb[(cb.d>=D)]
            if len(w)==0 or w.d.max()<E: r['exit_check']='cache_ends_before_exit'
            else:
                hit=w[(w.High>=t.take_profit)|(w.Low<=t.stop_loss)]
                if len(hit)==0: r['exit_check']='no_breach_in_daily_bars'
                else:
                    fd=hit.d.iloc[0]
                    r['bt_first_breach']=str(fd.date())
                    r['exit_check']='same_day' if fd==E else ('BT_earlier' if fd<E else 'BT_later')
                    r['days_diff']=(fd-E).days
    r['exit_reason']=t.close_reason
    rows.append(r)
df=pd.DataFrame(rows)
df['fill_vs_rec']=(df.fill/df.recprice-1)
df['fill_vs_open']=(df.fill/df.d_open-1)
df['fill_vs_close']=(df.fill/df.d_close-1)
print('fill slippage vs price the rec saw / D daily open / D daily close (abs, %): by inst')
for i,g in df.groupby('inst'):
    f=lambda s: f"{(s.abs().median()*100):.2f}/{(s.abs().quantile(.9)*100):.2f}(n={s.notna().sum()})"
    print(i,len(g),'rec',f(g.fill_vs_rec),'open',f(g.fill_vs_open),'close',f(g.fill_vs_close))
print(pd.crosstab(df.inst,df.exit_check.fillna('n/a')))
print(df[df.exit_check.isin(['BT_earlier','BT_later','no_breach_in_daily_bars'])][['inst','sym','D','leg','bt_first_breach','days_diff','exit_check']].to_string())
# entries made while the instance already held positions (double-charge exposure)
tx['od']=pd.to_datetime(tx.open_date); tx['cd']=pd.to_datetime(tx.close_date)
exp=[]
for i,t in tx.iterrows():
    prior=tx[(tx.expert_id==t.expert_id)&(tx.od<t.od)&((tx.cd.isna())|(tx.cd>t.od))]
    exp.append(len(prior))
tx['open_before']=exp
print(tx.groupby('expert_id').apply(lambda g: pd.Series(dict(entries=len(g),with_open_before=(g.open_before>0).sum(),max_open=g.open_before.max()))))
df.to_csv(L+'b8_mech.csv',index=False)
print(pd.crosstab(df.inst,df.exit_reason.fillna('open')))
