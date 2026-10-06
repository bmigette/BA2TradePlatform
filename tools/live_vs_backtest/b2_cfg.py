import sys, json
from pathlib import Path
ROOT = Path('C:/Users/basti/Documents/dev/BA2TradePlatform/.claude/worktrees/agent-a072d611f02ac2b20')
sys.path.insert(0, str(ROOT / "testplatform"))
from ba2test_launcher import _enter_backend
_enter_backend()
from app.models.backtest import Backtest
from app.models.database import SessionLocal
from app.services.backtest.rerun_handler import rebuild_config_for_backtest
db = SessionLocal()
out = {}
for bid in (1107, 1298, 1363, 1173, 1088, 1330, 1645):
    bt = db.query(Backtest).filter(Backtest.id == bid).first()
    cfg = rebuild_config_for_backtest(bt, db, window=['2026-09-11', '2026-10-05'])
    def trim(v):
        s = json.dumps(v, default=str)
        return s if len(s) < 600 else s[:600] + '...'
    out[bid] = {k: trim(v) for k, v in cfg.items()}
    print(bid, bt.name, bt.expert_type if hasattr(bt, 'expert_type') else '', bt.start_date, bt.end_date)
    for k, v in out[bid].items():
        print('   ', k, v)
    break
Path('C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/b2_cfg.json').write_text(json.dumps(out, indent=1))
