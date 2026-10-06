import json, collections, sys
import pandas as pd, numpy as np
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
live={r['analysis_id']:r for r in json.load(open(L+'a1_live_rows.json'))}
res=json.load(open(L+'a2_full.json'))
rows=[]
for r in res:
    lv=live[r['analysis_id']]
    d=dict(aid=r['analysis_id'],cls=r['cls'],inst=r['inst'],sym=r['symbol'],uc=r['use_case'],as_of=r['as_of'][:10],
           live_out=lv['outcome'],live_sig=lv.get('sig'),live_conf=lv.get('conf'),live_price=lv.get('price') if lv.get('price') is not None else lv.get('bundle_price'),
           live_skip=lv.get('rskip'),live_ep=lv.get('exp_profit'))
    for m in ('off','on'):
        x=r[m]
        d[m+'_ok']=x['ok']; d[m+'_err']=x.get('err'); d[m+'_sig']=x.get('sig'); d[m+'_conf']=x.get('conf'); d[m+'_price']=x.get('price'); d[m+'_skip']=x.get('skip')
        d[m+'_ep']=x.get('exp_profit'); d[m+'_miss']=len(x.get('hermetic_misses') or [])
        d[m+'_lastbar']=x['bundle'].get('last_bar')
    rows.append(d)
df=pd.DataFrame(rows)
def act(sig,skip):
    if skip is True: return 'SKIP'
    return sig
df['live_act']=[act(a,b) for a,b in zip(df.live_sig,df.live_skip)]
for m in ('off','on'): df[m+'_act']=[act(a,b) for a,b in zip(df[m+'_sig'],df[m+'_skip'])]
# live ERROR outcomes
df.loc[df.live_out=='error','live_act']='ERROR'
df.loc[(df.live_out=='skip')&df.live_act.isna(),'live_act']='SKIP'
# BT data availability: BT price present (DS also needs ohlcv) or skip/ok
for m in ('off','on'):
    df[m+'_hasdata']=df[m+'_ok']&df[m+'_price'].notna()
df.to_csv(L+'a4_pairs.csv',index=False)
print(df.groupby(['cls','inst']).size())
print('live action distribution'); print(pd.crosstab([df.cls,df.inst],df.live_act))
print('BT(off) act dist'); print(pd.crosstab([df.cls,df.inst],df.off_act.fillna('ERR')))
for m in ('off','on'):
    print(m,'errors',df[m+'_err'].dropna().str[:70].value_counts().head(5).to_dict(),'| hermetic-miss rows',(df[m+'_miss']>0).sum())
