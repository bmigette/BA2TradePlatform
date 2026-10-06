import json, pandas as pd
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
live={r['analysis_id']:r for r in json.load(open(L+'a1_live_rows.json'))}
res=json.load(open(L+'a2_full.json'))
def act(x): return 'ERR' if not x['ok'] else ('SKIP' if x.get('skip') else x['sig'])
out=[]
for r in res:
    lv=live[r['analysis_id']]
    la='SKIP' if lv['outcome']=='skip' else lv.get('sig')
    if lv['outcome']=='error': la='ERROR'
    o=act(r['off'])
    if la!=o:
        lb=r.get('live_bundle') or {}; ob=r['off']['bundle'] or {}
        out.append((r['cls'][:10],r['inst'],r['symbol'],r['as_of'][:10],la,o,lv.get('conf'),r['off'].get('conf'),
                    {k:(lb.get(k),ob.get(k)) for k in ('current_price','last_bar','last_close','n_bars','n_grades_rows','n_target_rows','latest_earnings','consensus','insider') if (lb.get(k) is not None or ob.get(k) is not None)}))
seen={}
for o in out:
    k=(o[1],o[4],o[5])
    seen.setdefault(k,[]).append(o)
for k,v in sorted(seen.items(), key=lambda kv:-len(kv[1]))[:14]:
    print(k,len(v))
    for o in v[:2]: print('    ',o[2],o[3],'conf live/BT',o[6],o[7],'live|BT inputs:',o[8])
