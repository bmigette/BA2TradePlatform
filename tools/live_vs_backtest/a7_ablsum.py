import json, collections
import pandas as pd, numpy as np
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
rows=json.load(open(L+'a6_ablation.json'))
def act(x):
    if not x or 'err' in x: return 'ERR'
    return 'SKIP' if x.get('skip') else x['sig']
recs=[]
for r in rows:
    d=dict(cls=r['cls'],inst=r['inst'],sym=r['sym'],D=r['D'],uc=r['uc'],final=r.get('same_final_ok'),
           live=act(r['live']),prior=act(r.get('prior')) if r.get('prior') else None,same=act(r.get('same')) if r.get('same') else None,
           lc=r['live'].get('conf'),pc=(r.get('prior') or {}).get('conf'),sc=(r.get('same') or {}).get('conf'),
           lp=r.get('live_price'),pp=r.get('prior_close_cache'),sp=r.get('same_close_cache'),
           l_ep=r['live'].get('ep'),p_ep=(r.get('prior') or {}).get('ep'),s_ep=(r.get('same') or {}).get('ep'))
    recs.append(d)
df=pd.DataFrame(recs); df.to_csv(L+'a7_ablation_pairs.csv',index=False)
print(len(df))
def agree(sub,col):
    s=sub[sub[col].notna()]
    return len(s), (s[col]==s['live']).mean() if len(s) else float('nan')
print('--- signal agreement with LIVE (live bundle, only the decision price/bar changed) ---')
for (c,i),g in df.groupby(['cls','inst']):
    n_p,a_p=agree(g,'prior'); n_s,a_s=agree(g,'same')
    gf=g[g.final==True]; nf,af=agree(gf,'same'); npf,apf=agree(gf,'prior')
    print(f'{c[:12]:12s} inst{i:3d} n={len(g):4d} | prior n={n_p} agree={a_p:.3f} | same(all cache D bar) n={n_s} agree={a_s:.3f} | FINAL-bar subset n={nf} same={af:.3f} prior={apf:.3f}')
print('--- conf change |same-live| and |prior-live| where signal equal (median, p90, max) ---')
for (c,i),g in df.groupby(['cls','inst']):
    for col,cc in (('prior','pc'),('same','sc')):
        s=g[(g[col]==g['live'])&g[cc].notna()&g.lc.notna()]
        d=(s[cc]-s.lc).abs()
        if len(d): print(f'{c[:12]:12s} inst{i:3d} {col:5s} n={len(d)} median={d.median():.2f} p90={d.quantile(.9):.2f} max={d.max():.2f} nonzero={(d>1e-6).mean():.2f}')
print('--- price: live price vs prior close vs same-day close (cache) ---')
for (c,i),g in df.groupby(['cls','inst']):
    s=g.dropna(subset=['lp','pp'])
    e1=((s.lp/s.pp)-1).abs()
    s2=g[g.final==True].dropna(subset=['lp','sp','pp'])
    e2=((s2.lp/s2.sp)-1).abs(); e3=((s2.lp/s2.pp)-1).abs()
    print(f'{c[:12]:12s} inst{i:3d} n_prior={len(s)} |live/prior-1| med={e1.median()*100:.2f}% p90={e1.quantile(.9)*100:.2f}% | final-bar n={len(s2)}: |live/sameclose-1| med={e2.median()*100:.2f}% p90={e2.quantile(.9)*100:.2f}%  vs |live/prior-1| med={e3.median()*100:.2f}% p90={e3.quantile(.9)*100:.2f}%')
print('--- flips ---')
fl=df[(df.same.notna())&(df.same!=df.live)]
print(fl.groupby(['cls','inst']).size())
print(fl.head(15)[['cls','inst','sym','D','live','prior','same','lc','pc','sc','lp','pp','sp']].to_string())
fl=df[(df.prior.notna())&(df.prior!=df.live)]
print('prior flips'); print(fl.groupby(['cls','inst']).size())
print(fl.head(15)[['cls','inst','sym','D','live','prior','same','lc','pc','sc','lp','pp','sp']].to_string())
