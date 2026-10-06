import json, collections, os, re
import pandas as pd
from pathlib import Path
from datetime import datetime
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
rows=json.load(open(L+'a1_live_rows.json'))
syms=collections.defaultdict(set)
for r in rows: syms[r['inst']].add(r['symbol'])
C=Path('C:/Users/basti/Documents/ba2/common/cache')
# OHLCV last bar per live symbol
out=collections.defaultdict(collections.Counter)
for inst,ss in sorted(syms.items()):
    for s in ss:
        f=C/'FMPOHLCVProvider'/f'{s}_1d.parquet'
        if not f.exists(): out[inst]['no_file']+=1; continue
        d=pd.read_parquet(f,columns=['Date']).Date
        d=pd.to_datetime(d); last=(d.dt.tz_convert('UTC').dt.tz_localize(None) if d.dt.tz is not None else d).max()
        k='>=2026-10-05' if last>=pd.Timestamp('2026-10-05') else '2026-09-14..10-02' if last>=pd.Timestamp('2026-09-14') else '2026-07-01..09-13' if last>=pd.Timestamp('2026-07-01') else '<=2026-06-30'
        out[inst][k]+=1
print('OHLCV cache (test) last-bar by live-analysed symbol, per instance');
for i,c in out.items(): print(i,dict(c),'symbols',len(syms[i]))
# fmp_history: kinds and freshness for live symbols
H=C/'fmp_history'
kinds=collections.defaultdict(list)
for f in os.listdir(H):
    m=re.match(r'(.+?)__(.+)\.json$',f)
    if m: kinds[m.group(1)].append((m.group(2),os.path.getmtime(H/f)))
for k,v in kinds.items():
    mt=[x[1] for x in v]
    print(k,len(v),'oldest',datetime.fromtimestamp(min(mt)).date(),'newest',datetime.fromtimestamp(max(mt)).date())
allsyms=set().union(*syms.values())
for k,v in kinds.items():
    d=dict(v); have=[s for s in allsyms if s in d]
    stale=[s for s in have if datetime.fromtimestamp(d[s])<datetime(2026,9,11)]
    print(k,'live symbols with file',len(have),'/',len(allsyms),'| file fetched before 2026-09-11:',len(stale))
# metric store
ms=C/'screener'/'metric_store'
print('metric store months', sorted(p.name for p in ms.iterdir() if p.name.startswith('ym='))[-3:])
