import sys, json
from pathlib import Path
ROOT = Path('C:/Users/basti/Documents/dev/BA2TradePlatform/.claude/worktrees/agent-a072d611f02ac2b20')
sys.path.insert(0, str(ROOT / "testplatform"))
from ba2test_launcher import _enter_backend
_enter_backend()
import logging; logging.disable(logging.CRITICAL)
from app.models.backtest import Backtest
from app.models.database import SessionLocal
from app.services.backtest.rerun_handler import rebuild_config_for_backtest
from ba2_common.core.replay import ReplayStore
L = Path('C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp')
st = ReplayStore(L / 'store')
latest = {}
for s in st.index.list_sessions():
    for a in st.index.analyses(s.session_id):
        if a.settings_object and (a.expert_instance_id not in latest or str(a.started_at) > str(latest[a.expert_instance_id].started_at)):
            latest[a.expert_instance_id] = a
db = SessionLocal()
MAP = {7: 1107, 8: 1298, 9: 1363, 10: 1173, 11: 1088, 12: 1330}
for inst, bid in MAP.items():
    bt = db.query(Backtest).filter(Backtest.id == bid).first()
    cfg = rebuild_config_for_backtest(bt, db, window=['2026-09-11', '2026-10-05'])
    bs = cfg['experts'][0]['settings']
    ls = st.decode_object(latest[inst].settings_object)
    diffs = []
    for k in sorted(set(bs) | set(ls)):
        a = ls.get(k, '<absent>'); b = bs.get(k, '<absent>')
        try:
            same = abs(float(a) - float(b)) < 1e-9
        except Exception:
            same = (str(a) == str(b))
        if not same and a != '<absent>' and b != '<absent>':
            diffs.append((k, a, b))
    print(inst, bid, latest[inst].expert_class, 'live keys', len(ls), 'row keys', len(bs), 'DIFFS', len(diffs))
    for d in diffs[:25]: print('    live=%r row=%r key=%s' % (d[1], d[2], d[0]))
