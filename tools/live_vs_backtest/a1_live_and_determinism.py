"""Part A step 1: dump every recorded live analysis (flat) + offline determinism replay of the recorded bundle."""
import json, logging, sys, collections
from pathlib import Path
import pandas as pd
from ba2_common.core.replay import ReplayStore
from ba2_common.core.replay.service import SessionBundle
from app.services.replay.expert_replay import replay_analysis
from app.services.replay.isolation import replay_isolation
from app.services.replay.historical import evaluation_time

OUT = Path('C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp')
st = ReplayStore(OUT / 'store')
logging.disable(logging.CRITICAL)
rows = []
with replay_isolation():
    for s in st.index.list_sessions():
        sid = s.session_id
        ans = st.index.analyses(sid)
        bundle = SessionBundle(root=st.root, session=s, analyses=tuple(ans),
                               observations=tuple(st.index.session_observations(sid)),
                               coverage=(), objects=st.objects)
        for a in ans:
            r = dict(session=sid, analysis_id=a.analysis_id, attempt=a.attempt_id, cls=a.expert_class,
                     inst=a.expert_instance_id, symbol=a.symbol, use_case=a.use_case,
                     outcome=a.outcome, skip_reason=a.skip_reason, error=a.error,
                     started=str(a.started_at), as_of=str(evaluation_time(a)))
            try:
                if a.recommendation_object:
                    rec = st.decode_object(a.recommendation_object)
                    r.update(sig=str(rec.signal.value), conf=rec.confidence, price=rec.current_price,
                             exp_profit=rec.expected_profit_percent, target=rec.target_price,
                             rskip=rec.skip, rskip_reason=rec.skip_reason)
                if a.bundle_object:
                    b = st.decode_object(a.bundle_object)
                    r['bundle_price'] = b.get('current_price') if isinstance(b, dict) else None
                    oh = b.get('ohlcv') if isinstance(b, dict) else None
                    if isinstance(oh, pd.DataFrame) and len(oh):
                        r['live_last_bar'] = str(pd.Timestamp(oh['Date'].iloc[-1]))[:10]
                        r['live_last_close'] = float(oh['Close'].iloc[-1])
                        r['live_n_bars'] = len(oh)
            except Exception as e:
                r['decode_err'] = repr(e)[:200]
            try:
                res = replay_analysis(bundle, a)
                r['det_status'] = res.status
                r['det_detail'] = (res.detail or '')[:300]
                r['det_diffs'] = [str(d)[:200] for d in list(res.field_diffs)[:5]]; r['det_all'] = [[d.field, d.recorded, d.produced] for d in res.field_diffs]
            except Exception as e:
                r['det_status'] = 'EXC'; r['det_detail'] = repr(e)[:300]
            rows.append(r)
    print(collections.Counter((r['cls'], r.get('det_status')) for r in rows))
(OUT / 'a1_live_rows.json').write_text(json.dumps(rows, default=str, indent=0))
print(len(rows))
