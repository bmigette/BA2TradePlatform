"""One-shot emergency action: attach a protective stop-loss to the 7 naked open positions on
the prod Alcapa Live account (FMPPScreener-1), discovered to have zero broker-side stop
protection. Uses the account's own configured min_stop_loss_pct (7%) off ENTRY price as a
conservative, immediately-actionable floor (the RM's full ATR-based calc isn't recomputed here
for speed — this is a stop-the-bleeding action, not a precise backtest-parity replication).

Uses AlpacaAccount.adjust_tp_sl() — the platform's own production TP/SL mechanism — so the
resulting order/transaction records are created exactly as the live UI would."""
import os
os.environ["DB_FILE"] = r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite"

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

STOP_PCT = 0.07  # min_stop_loss_pct for this expert instance
TXN_IDS = [105, 106, 107, 108, 109, 110, 111]

acct = AlpacaAccount(1)

for tid in TXN_IDS:
    txn = get_instance(Transaction, tid)
    if txn is None or txn.status.value != "OPENED":
        print(f"txn {tid}: skip (not found or not OPENED)")
        continue
    sl_price = round(txn.open_price * (1 - STOP_PCT), 2)
    print(f"txn {tid} ({txn.symbol}): entry=${txn.open_price:.2f} -> SL=${sl_price:.2f}")
    ok = acct.adjust_tp_sl(txn, new_tp_price=None, new_sl_price=sl_price, source="manual")
    print(f"  adjust_tp_sl -> {ok}")
