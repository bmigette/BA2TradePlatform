import json, collections
import pandas as pd, numpy as np
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
live={r['analysis_id']:r for r in json.load(open(L+'a1_live_rows.json'))}
res=json.load(open(L+'a2_full.json'))
abl={r['aid']:r for r in json.load(open(L+'a6_ablation.json'))}
def act(sig,skip,out=None):
    if out=='error': return 'ERROR'
    return 'SKIP' if skip else sig
rows=[]
for r in res:
    lv=live[r['analysis_id']]; D=pd.Timestamp(r['as_of'][:10])
    lb=r.get('live_bundle') or {}; ob=r['off']['bundle'] or {}; nb=r['on']['bundle'] or {}
    d=dict(cls=r['cls'],inst=r['inst'],sym=r['symbol'],D=r['as_of'][:10],uc=r['use_case'],
           live=act(lv.get('sig'),lv.get('rskip'),lv['outcome']) if lv['outcome']!='skip' else 'SKIP',
           lconf=lv.get('conf'),lprice=lv.get('price'),
           off=act(r['off'].get('sig'),r['off'].get('skip')) if r['off']['ok'] else 'ERR',
           on=act(r['on'].get('sig'),r['on'].get('skip')) if r['on']['ok'] else 'ERR',
           oconf=r['off'].get('conf'),nconf=r['on'].get('conf'),oprice=r['off'].get('price'),nprice=r['on'].get('price'))
    # BT-input completeness: how much of the live bundle did the BT cache reproduce
    c=r['cls']
    if c=='DeterministicScorer':
        comp=(r['on']['ok'] and ob.get('n_grades_rows')==lb.get('n_grades_rows') and ob.get('n_target_rows')==lb.get('n_target_rows')
              and ob.get('n_earnings_rows')==lb.get('n_earnings_rows') and nb.get('last_bar') is not None
              and (D-pd.Timestamp(nb['last_bar'])).days<=4)
    elif c=='FMPEarningsDrift': comp=ob.get('latest_earnings')==lb.get('latest_earnings')
    elif c=='FMPRating': comp=(ob.get('consensus')==lb.get('consensus') and ob.get('upgrade')==lb.get('upgrade'))
    else: comp=ob.get('insider')==lb.get('insider')
    d['complete']=bool(comp)
    d['final']=abl.get(r['analysis_id'],{}).get('same_final_ok')
    rows.append(d)
df=pd.DataFrame(rows); df.to_csv(L+'a8_direct_pairs.csv',index=False)
def rate(g,col):
    return f"{(g[col]==g.live).mean():.3f}"
print('=== direct as-of reconstruction vs live, action agreement (BUY/HOLD/SELL/SKIP/ERR) ===')
print('cls inst | n | off agree | on agree || complete-inputs subset: n, off, on || complete & final-D-bar subset n off on')
for (c,i),g in df.groupby(['cls','inst']):
    gc=g[g.complete]; gcf=gc[gc.final==True]
    print(f'{c[:14]:14s} {i:3d} | {len(g):4d} | {rate(g,"off")} {rate(g,"on")} || {len(gc):4d} {rate(gc,"off") if len(gc) else "-"} {rate(gc,"on") if len(gc) else "-"} || {len(gcf):4d} {rate(gcf,"off") if len(gcf) else "-"} {rate(gcf,"on") if len(gcf) else "-"}')
print()
print('=== confusion live(row) vs BT off (all) ===')
for (c,i),g in df.groupby(['cls','inst']):
    print(c,i); print(pd.crosstab(g.live,g.off))
print('=== complete-inputs only: confusion off ===')
for (c,i),g in df[df.complete].groupby(['cls','inst']):
    print(c,i); print(pd.crosstab(g.live,g.off));
    s=g.dropna(subset=['lconf','oconf']); s=s[s.live==s.off]
    if len(s): print(' conf diff off: median',(s.oconf-s.lconf).abs().median().round(2),'p90',(s.oconf-s.lconf).abs().quantile(.9).round(2),'n',len(s))
    s=g.dropna(subset=['lconf','nconf']); s=s[s.live==s.on]
    if len(s): print(' conf diff on : median',(s.nconf-s.lconf).abs().median().round(2),'p90',(s.nconf-s.lconf).abs().quantile(.9).round(2),'n',len(s))
print('=== price seen: live vs BT off / on (rows where BT has a price) ===')
for (c,i),g in df.groupby(['cls','inst']):
    for col in ('oprice','nprice'):
        s=g.dropna(subset=['lprice',col]); e=((s[col]/s.lprice)-1).abs()
        if len(e): print(f'{c[:14]:14s} {i:3d} {col} n={len(e)} median={e.median()*100:.2f}% p90={e.quantile(.9)*100:.2f}% frac>1%={(e>.01).mean():.2f}')
    print('    BT price missing: off',g.oprice.isna().sum(),'of',len(g))
