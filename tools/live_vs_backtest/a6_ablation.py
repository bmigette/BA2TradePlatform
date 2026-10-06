"""Decision-timing ablation on the RECORDED live bundles: same live inputs, only the price/bar the decision sees changes.
  live  : as recorded (deterministic replay baseline)
  prior : decision sees only sessions BEFORE the decision day (BA2_EXP_PRIOR_CLOSE=1 semantics)
  same  : decision sees the decision day's own FINAL bar (backtest as it is today)
"""
import json, logging, copy
from pathlib import Path
import pandas as pd
from ba2_common.core.replay import ReplayStore, use_capture_context
from ba2_common.core.replay.service import SessionBundle
from app.services.replay.expert_replay import build_replay_expert, replay_context
from app.services.replay.isolation import replay_isolation
from app.services.replay.historical import evaluation_time
from ba2_common.core.replay import ReplayStatus

L = Path('C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp')
CACHE = Path('C:/Users/basti/Documents/ba2/common/cache/FMPOHLCVProvider')
logging.disable(logging.CRITICAL)
st = ReplayStore(L / 'store')
_c = {}


def cache_bars(sym):
    if sym not in _c:
        f = CACHE / f'{sym}_1d.parquet'
        if not f.exists():
            _c[sym] = None
        else:
            c = pd.read_parquet(f)
            d = pd.to_datetime(c['Date'])
            c['d'] = (d.dt.tz_convert('UTC').dt.tz_localize(None) if d.dt.tz is not None else d).dt.normalize()
            _c[sym] = c.sort_values('d').reset_index(drop=True)
    return _c[sym]


def run(expert, a, bundle, settings):
    with use_capture_context(replay_context(a, ReplayStatus.PHASE_PROCESS)):
        try:
            rec = expert._process(bundle, settings, as_of=None)
        except BaseException as e:
            return dict(err=f'{type(e).__name__}: {str(e)[:120]}')
    return dict(sig=str(rec.signal.value), conf=rec.confidence, price=rec.current_price, ep=rec.expected_profit_percent,
                skip=rec.skip, target=rec.target_price,
                stop=(rec.raw_outputs or {}).get('stop_price') if isinstance(rec.raw_outputs, dict) else None)


rows = []
with replay_isolation():
    for s in st.index.list_sessions():
        for a in st.index.analyses(s.session_id):
            if not a.bundle_object or a.outcome == 'error':
                continue
            settings = st.decode_object(a.settings_object)
            b = st.decode_object(a.bundle_object)
            D = pd.Timestamp(str(evaluation_time(a))[:10])
            sym = a.symbol
            cb = cache_bars(sym)
            r = dict(aid=a.analysis_id, cls=a.expert_class, inst=a.expert_instance_id, sym=sym, uc=a.use_case, D=str(D.date()))
            expert = build_replay_expert(a.expert_class, a)
            r['live'] = run(expert, a, b, settings)
            live_price = b.get('current_price')
            r['live_price'] = live_price
            # prior / same references
            prior_close = same_close = None; final_ok = False; same_bar = None; prior_bar = None
            if cb is not None:
                pr = cb[cb.d < D]
                if len(pr) and (D - pr['d'].iloc[-1]).days <= 4:
                    prior_close = float(pr['Close'].iloc[-1]); prior_bar = pr.iloc[-1]
                sm = cb[cb.d == D]
                after = (cb.d > D).any()
                if len(sm):
                    same_bar = sm.iloc[0]; same_close = float(same_bar['Close'])
                    final_ok = bool(after)
            r['prior_close_cache'] = prior_close; r['same_close_cache'] = same_close; r['same_final_ok'] = final_ok
            if a.expert_class == 'DeterministicScorer':
                df = b['ohlcv'].copy()
                dd = pd.to_datetime(df['Date'])
                dd = (dd.dt.tz_convert('UTC').dt.tz_localize(None) if dd.dt.tz is not None else dd).dt.normalize()
                live_has_D = bool((dd == D).any())
                r['live_has_D'] = live_has_D
                if live_has_D:
                    lv = df[dd == D].iloc[0]
                    r['live_D_vol'] = float(lv['Volume'])
                    if same_bar is not None:
                        r['cache_D_vol'] = float(same_bar['Volume'])
                        # final only if the cache bar is NOT the live partial snapshot
                        r['same_final_ok'] = bool(final_ok and float(same_bar['Volume']) != float(lv['Volume']))
                # prior: drop D (and later) bars
                dfp = df[dd < D].reset_index(drop=True)
                bp = dict(b); bp['ohlcv'] = dfp
                bp['current_price'] = float(dfp['Close'].iloc[-1]) if len(dfp) else None
                r['prior'] = run(expert, a, bp, settings)
                # same: D bar replaced by cache final
                if same_bar is not None:
                    dfs = df[dd < D].copy()
                    newrow = {c: same_bar[c] for c in ('Open', 'High', 'Low', 'Close', 'Volume')}
                    ts = df['Date'].iloc[-1]
                    # keep the Date dtype/tz of the live frame
                    newrow['Date'] = pd.Timestamp(D.date()).tz_localize('UTC') if getattr(pd.Timestamp(ts), 'tzinfo', None) else pd.Timestamp(D.date())
                    dfs = pd.concat([dfs, pd.DataFrame([newrow])], ignore_index=True)
                    bs = dict(b); bs['ohlcv'] = dfs; bs['current_price'] = float(same_bar['Close'])
                    r['same'] = run(expert, a, bs, settings)
            else:
                if prior_close is not None:
                    bp = dict(b); bp['current_price'] = prior_close
                    r['prior'] = run(expert, a, bp, settings)
                if same_close is not None:
                    bs = dict(b); bs['current_price'] = same_close
                    r['same'] = run(expert, a, bs, settings)
            rows.append(r)
(L / 'a6_ablation.json').write_text(json.dumps(rows, default=str))
print(len(rows))
