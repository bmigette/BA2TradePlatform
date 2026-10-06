"""Part A step 2: re-derive each live analysis through the BACKTEST data path (analyze_as_of over the
cache as of the decision instant), twice: switch OFF (as today) and ON (prior-session clamp).

usage: a2_bt_signals.py OUT.json [LIMIT]
"""
import json, logging, os, sys, time, collections
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else None
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


results = []
t0 = time.time()
with replay_isolation(), frozen_ttl_cache(), H.offline_credentials():
    # one entry per analysis_id: the LAST attempt that reached an outcome
    chosen = {}
    sessions = {s.session_id: s for s in st.index.list_sessions()}
    for sid, s in sessions.items():
        for a in st.index.analyses(sid):
            if a.settings_object is None:
                continue
            key = a.analysis_id
            if key not in chosen or str(a.started_at) > str(chosen[key][1].started_at):
                chosen[key] = (sid, a)
    items = sorted(chosen.values(), key=lambda x: str(x[1].started_at))
    if LIMIT:
        step = max(1, len(items) // LIMIT)
        items = items[::step][:LIMIT]
    print('analyses', len(items), flush=True)
    for n, (sid, a) in enumerate(items):
        entry = dict(analysis_id=a.analysis_id, expert_class=a.expert_class,
                     expert_instance_id=a.expert_instance_id, symbol=a.symbol, use_case=a.use_case,
                     branch_flags=a.branch_flags, as_of=H.evaluation_time(a).isoformat(),
                     live_attempt_id=a.attempt_id)
        settings = st.decode_object(a.settings_object)
        r = dict(analysis_id=a.analysis_id, cls=a.expert_class, inst=a.expert_instance_id, symbol=a.symbol,
                 use_case=a.use_case, as_of=entry['as_of'], live_outcome=a.outcome)
        if a.bundle_object:
            try:
                r['live_bundle'] = summ(st.decode_object(a.bundle_object))
            except Exception as e:
                r['live_bundle_err'] = repr(e)[:100]
        for mode in ('off', 'on'):
            r[mode] = run_mode(a.expert_class, entry, settings, mode, {})
        results.append(r)
        if n % 50 == 0:
            print(n, round(time.time() - t0), flush=True)
OUTF.write_text(json.dumps(results, default=str))
print('done', len(results), round(time.time() - t0))
