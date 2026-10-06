"""Compare live-recorded OHLCV frames (DS bundles) to the test-platform cache bars, per date."""
import json, collections, os
import pandas as pd
from pathlib import Path
from ba2_common.core.replay import ReplayStore
L = Path('C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp')
CACHE = Path('C:/Users/basti/Documents/ba2/common/cache/FMPOHLCVProvider')
st = ReplayStore(L / 'store')
res = []
cov = collections.Counter()
for s in st.index.list_sessions():
    for a in st.index.analyses(s.session_id):
        if a.expert_class != 'DeterministicScorer' or not a.bundle_object:
            continue
        b = st.decode_object(a.bundle_object)
        oh = b['ohlcv'].copy()
        oh['d'] = pd.to_datetime(oh['Date']).dt.tz_localize(None).dt.normalize() if pd.to_datetime(oh['Date']).dt.tz is None else pd.to_datetime(oh['Date']).dt.tz_convert('UTC').dt.tz_localize(None).dt.normalize()
        f = CACHE / f'{a.symbol}_1d.parquet'
        D = pd.Timestamp(str(a.started_at)[:10])
        if not f.exists():
            cov['no_cache_file'] += 1; continue
        c = pd.read_parquet(f)
        dc = pd.to_datetime(c['Date'])
        c['d'] = (dc.dt.tz_convert('UTC').dt.tz_localize(None) if dc.dt.tz is not None else dc).dt.normalize()
        cl = c['d'].max()
        cov['cache_last_ge_D' if cl >= D else 'cache_last_lt_D'] += 1
        m = oh.merge(c, on='d', suffixes=('_l', '_c'))
        for tag, day in (('prior', None), ('same', D)):
            if tag == 'same':
                row = m[m['d'] == D]
            else:
                row = m[m['d'] < D].tail(1)
            if len(row):
                r = row.iloc[0]
                res.append(dict(sym=a.symbol, inst=a.expert_instance_id, D=str(D.date()), tag=tag, bar=str(r['d'].date()),
                                close_l=r['Close_l'], close_c=r['Close_c'], open_l=r['Open_l'], open_c=r['Open_c'],
                                high_l=r['High_l'], high_c=r['High_c'], low_l=r['Low_l'], low_c=r['Low_c'],
                                vol_l=r['Volume_l'], vol_c=r['Volume_c']))
        # does live frame hold a bar dated D?
        cov['live_has_D' if (oh['d'] == D).any() else 'live_no_D'] += 1
print(cov)
df = pd.DataFrame(res)
df.to_csv(L / 'a3_bars.csv', index=False)
for tag in ('prior', 'same'):
    x = df[df.tag == tag]
    eq = ((x.close_l - x.close_c).abs() < 1e-6)
    print(tag, len(x), 'close equal', eq.mean(), 'open equal', ((x.open_l - x.open_c).abs() < 1e-6).mean(),
          'high eq', ((x.high_l - x.high_c).abs() < 1e-6).mean(), 'vol eq', (x.vol_l == x.vol_c).mean())
x = df[df.tag == 'same']
print(x.head(12).to_string())
