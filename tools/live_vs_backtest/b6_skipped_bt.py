"""Part A step 2: re-derive each live analysis through the BACKTEST data path (analyze_as_of over the
cache as of the decision instant), twice: switch OFF (as today) and ON (prior-session clamp).

usage: a2_bt_signals.py OUT.json [LIMIT]
"""
import json, logging, os, sys, time, collections
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

LIMIT=None
OUTF = Path(sys.argv[1])
L = Path('C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp')

from ba2_common.core.replay import ReplayStore
from ba2_common.core.replay.service import SessionBundle
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_providers import get_provider
from ba2_providers.fmp_common import frozen_ttl_cache, hermetic_miss_symbols, reset_hermetic_misses
from app.services.replay.isolation import replay_isolation
from app.services.replay import historical as H

logging.disable(logging.CRITICAL)
st = ReplayStore(L / 'store')


def clamp_cut(as_of):
    a = pd.Timestamp(as_of)
    a = a.tz_convert('UTC') if a.tzinfo else a.tz_localize('UTC')
    return datetime(a.year, a.month, a.day, tzinfo=timezone.utc) - timedelta(microseconds=1)


class Clamp:
    def __init__(self, inner, cut):
        self._i = inner; self._cut = cut

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval='1d', **kw):
        if interval == '1d':
            end_date = self._cut if end_date is None else min(pd.Timestamp(end_date).tz_convert('UTC') if pd.Timestamp(end_date).tzinfo else pd.Timestamp(end_date).tz_localize('UTC'), pd.Timestamp(self._cut))
        return self._i.get_ohlcv_data(symbol, start_date=start_date, end_date=end_date, interval=interval, **kw)

    def __getattr__(self, k):
        return getattr(self._i, k)


class ClampBundle(LiveProviderBundle):
    def __init__(self, get, cut):
        super().__init__(get); self._cut = cut

    def ohlcv(self):
        return Clamp(super().ohlcv(), self._cut)


from ba2_experts.DeterministicScorer import data as dsd
_orig_dsf = dsd.fetch_ohlcv
MODE = {'m': 'off', 'cut': None}


def _dsf(providers, symbol, as_of, *a, **kw):
    out = _orig_dsf(providers, symbol, as_of, *a, **kw)
    if MODE['m'] == 'on' and out is not None and len(out):
        d = pd.to_datetime(out['Date'])
        d = d.dt.tz_convert('UTC').dt.tz_localize(None) if d.dt.tz is not None else d
        cut = pd.Timestamp(MODE['cut']).tz_convert('UTC').tz_localize(None)
        out = out[d <= cut].reset_index(drop=True)
        if not len(out):
            out = None
    return out


dsd.fetch_ohlcv = _dsf


def summ(b):
    if not isinstance(b, dict):
        return {}
    o = {'current_price': b.get('current_price')}
    oh = b.get('ohlcv')
    if isinstance(oh, pd.DataFrame) and len(oh):
        o['last_bar'] = str(pd.Timestamp(oh['Date'].iloc[-1]))[:10]
        o['last_close'] = float(oh['Close'].iloc[-1]); o['n_bars'] = len(oh)
    for k in ('grades_rows', 'target_rows', 'earnings_rows'):
        if isinstance(b.get(k), list):
            o['n_' + k] = len(b[k])
            if b[k]:
                o['first_' + k] = {kk: vv for kk, vv in list(b[k][0].items())[:4]}
    le = b.get('latest_earnings')
    if isinstance(le, dict):
        o['latest_earnings'] = {k: le.get(k) for k in ('report_date', 'reported_eps', 'estimated_eps')}
    elif 'latest_earnings' in b:
        o['latest_earnings'] = None
    cd = b.get('consensus_data')
    if isinstance(cd, dict):
        o['consensus'] = {k: cd.get(k) for k in ('targetMedian', 'targetConsensus', 'targetCount')}
    ud = b.get('upgrade_data')
    if isinstance(ud, list) and ud:
        o['upgrade'] = {k: ud[0].get(k) for k in ('strongBuy', 'buy', 'hold', 'sell', 'strongSell')}
    idt = b.get('insider_data')
    if isinstance(idt, dict):
        o['insider'] = {k: idt.get(k) for k in ('transaction_count', 'total_purchase_value')}
    return o


def run_mode(expert_cls, entry, settings, mode, bundle_hold):
    expert = H._build_historical_expert(entry)
    as_of = datetime.fromisoformat(entry['as_of'])
    orig = expert._gather

    def g(*a, **k):
        p = orig(*a, **k)
        bundle_hold['b'] = p
        return p
    expert._gather = g
    get = get_provider
    prov = ClampBundle(get, clamp_cut(as_of)) if mode == 'on' else LiveProviderBundle(get)
    reset_hermetic_misses()
    MODE['m'] = mode; MODE['cut'] = clamp_cut(as_of)
    try:
        rec = expert.analyze_as_of(as_of, BacktestContext(providers=prov, settings=settings, as_of=as_of,
                                                          extra={'symbol': entry['symbol']}))
        out = dict(ok=True)
        if rec is not None:
            out.update(sig=str(rec.signal.value), conf=rec.confidence, price=rec.current_price,
                       exp_profit=rec.expected_profit_percent, target=rec.target_price,
                       skip=rec.skip, skip_reason=rec.skip_reason)
    except BaseException as e:
        out = dict(ok=False, err=f'{type(e).__name__}: {str(e)[:300]}')
    out['hermetic_misses'] = sorted(hermetic_miss_symbols())[:5]
    out['bundle'] = summ(bundle_hold.get('b'))
    return out



import sqlite3
con = sqlite3.connect('file:C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite?mode=ro', uri=True)
sk = pd.read_sql("select id,symbol,expert_instance_id,created_at,state from marketanalysis where status='SKIPPED' and created_at>='2026-09-10'", con)
import json as _j
sk['skip_type'] = sk.state.map(lambda x: _j.loads(x).get('skip_type'))
sk = sk[sk.skip_type.isin(['insufficient_balance', 'symbol_price_balance_check'])].copy()
print('balance-skipped analyses', len(sk), flush=True)
# latest recorded settings per instance + class
latest = {}
for s_ in st.index.list_sessions():
    for a in st.index.analyses(s_.session_id):
        if a.settings_object and (a.expert_instance_id not in latest or str(a.started_at) > str(latest[a.expert_instance_id][1].started_at)):
            latest[a.expert_instance_id] = (s_.session_id, a)
results = []
t0 = time.time()
with replay_isolation(), frozen_ttl_cache(), H.offline_credentials():
    for _, row in sk.iterrows():
        inst = int(row.expert_instance_id)
        if inst not in latest:
            results.append(dict(id=int(row.id), inst=inst, symbol=row.symbol, no_settings=True)); continue
        sid, a0 = latest[inst]
        settings = st.decode_object(a0.settings_object)
        as_of = datetime.fromisoformat(row.created_at.replace(' ', 'T')).replace(tzinfo=timezone.utc)
        entry = dict(analysis_id=str(row.id), expert_class=a0.expert_class, expert_instance_id=inst, symbol=row.symbol,
                     use_case='enter_market', branch_flags=a0.branch_flags, as_of=as_of.isoformat(), live_attempt_id='x')
        r = dict(id=int(row.id), inst=inst, cls=a0.expert_class, symbol=row.symbol, as_of=as_of.isoformat(), skip_type=row.skip_type)
        for mode in ('off', 'on'):
            r[mode] = run_mode(a0.expert_class, entry, settings, mode, {})
        results.append(r)
OUTF.write_text(json.dumps(results, default=str))
print('done', len(results), round(time.time() - t0))
