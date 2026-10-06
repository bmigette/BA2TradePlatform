import pandas as pd, numpy as np
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
df=pd.read_csv(L+'a7_ablation_pairs.csv')
ds=df[(df.cls=='DeterministicScorer')&(df.final==True)&df.same.notna()&df.prior.notna()]
print('DS rows with a FINAL D bar:',len(ds))
print('signal agree with live: same(i)=%.3f prior(ii)=%.3f'%((ds.same==ds.live).mean(),(ds.prior==ds.live).mean()))
ds=ds.assign(dS=(ds.sc-ds.lc).abs(),dP=(ds.pc-ds.lc).abs())
print('mean |conf - live|: same %.2f prior %.2f ; median %.2f / %.2f'%(ds.dS.mean(),ds.dP.mean(),ds.dS.median(),ds.dP.median()))
print('price: |same close/live-1| median %.2f%%  |prior close/live-1| median %.2f%%'%(((ds.sp/ds.lp-1).abs().median())*100,((ds.pp/ds.lp-1).abs().median())*100))
# non-DS pooled price proximity on final rows
nd=df[(df.cls!='DeterministicScorer')&(df.final==True)].dropna(subset=['lp','sp','pp'])
print('non-DS rows',len(nd),'|same/live-1| median %.2f%% |prior/live-1| median %.2f%%'%(((nd.sp/nd.lp-1).abs().median())*100,((nd.pp/nd.lp-1).abs().median())*100))
# expected profit diffs for non-DS
for cls,g in df.groupby('cls'):
    s=g.dropna(subset=['l_ep','s_ep','p_ep'])
    if len(s): print(cls,'exp_profit: |same-live| med %.2f |prior-live| med %.2f  (n=%d)'%((s.s_ep-s.l_ep).abs().median(),(s.p_ep-s.l_ep).abs().median(),len(s)))
