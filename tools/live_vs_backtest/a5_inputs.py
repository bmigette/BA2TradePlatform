import json, collections
import pandas as pd
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
res=json.load(open(L+'a2_full.json'))
out=collections.defaultdict(collections.Counter)
ex=collections.defaultdict(list)
for r in res:
    lb=r.get('live_bundle') or {}; bb=r['off']['bundle'] or {}
    c=r['cls']; i=r['inst']; k=(c,i)
    def cmp(name,a,b):
        if a==b: out[k][name+':same']+=1
        elif b in (None,{},[]) and a not in (None,{},[]): out[k][name+':BT_missing']+=1; ex[(k,name)].append((r['symbol'],r['as_of'][:10],a,b))
        elif a in (None,{},[]) and b not in (None,{},[]): out[k][name+':live_missing']+=1
        else: out[k][name+':differ']+=1; ex[(k,name)].append((r['symbol'],r['as_of'][:10],a,b))
    if c=='FMPEarningsDrift':
        cmp('latest_earnings',lb.get('latest_earnings'),bb.get('latest_earnings'))
        cmp('current_price',lb.get('current_price'),bb.get('current_price'))
    if c=='FMPRating':
        cmp('consensus',lb.get('consensus'),bb.get('consensus'))
        cmp('upgrade',lb.get('upgrade'),bb.get('upgrade'))
        cmp('current_price',lb.get('current_price'),bb.get('current_price'))
    if c=='FMPInsiderClusterBuy':
        cmp('insider',lb.get('insider'),bb.get('insider'))
        cmp('current_price',lb.get('current_price'),bb.get('current_price'))
    if c=='DeterministicScorer':
        for f in ('n_grades_rows','n_target_rows','n_earnings_rows','n_bars'):
            a=lb.get(f); b=bb.get(f)
            if a==b: out[k][f+':same']+=1
            elif b is None: out[k][f+':BT_missing']+=1
            else: out[k][f+':differ']+=1
        cmp('last_bar',lb.get('last_bar'),bb.get('last_bar'))
        cmp('last_close',lb.get('last_close'),bb.get('last_close'))
for k,v in out.items():
    print(k);
    for kk in sorted(v): print('   ',kk,v[kk])
for key,v in ex.items():
    print(key,len(v),v[:3])
