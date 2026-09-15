"""One-off repair of live TP/SL on prod account 1, 2026-09-10.

Why: (1) Insider entries 203/204/205 lost their ruleset take-profit to a stale
transaction write (the TP-only action's post-hook overwrote the broker's TP with
NULL); (2) RAT entries 127/128/194/195 were submitted before the entry-stop
reconciliation fix and run a 10% safeguard stop where the ruleset says -4% from
the open price.

Runs through the platform's OWN adjust path (AlpacaAccount.adjust_tp / adjust_sl),
never raw SQL or raw broker calls. Must run with PROD STOPPED (single writer).
Source label is NOT "manual" so no manual-override lock is set: the open-positions
rules keep governing these positions afterwards.

    python tools/repair_live_tpsl_2026-09-10.py            # dry run (read-only)
    python tools/repair_live_tpsl_2026-09-10.py --apply    # place the adjustments
"""
import os
import sys

PROD_DB = r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite"
PROD_CACHE = r"C:\Users\basti\Documents\ba2_trade_platform-prod\cache"
REPO = r"C:\Users\basti\Documents\dev\BA2TradePlatform"
ACCOUNT_ID = 1
SOURCE = "repair-2026-09-10"

# Env BEFORE any ba2 import: config modules read these at import time.
os.environ["DB_FILE"] = PROD_DB
os.environ["CACHE_FOLDER"] = PROD_CACHE
for p in (REPO, os.path.join(REPO, "packages", "common"), os.path.join(REPO, "packages", "providers"),
          os.path.join(REPO, "packages", "experts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ba2_common.core import db as _ba2_db  # noqa: E402
_ba2_db.configure_db(PROD_DB)

from ba2_common.core.db import get_instance  # noqa: E402
from ba2_common.core.models import Transaction  # noqa: E402
from ba2_common.core.types import TransactionStatus  # noqa: E402
from ba2_trade_platform.core.seam_wiring import wire_all_seams  # noqa: E402

# Intended values. TPs are the exact limit prices of the cancelled TP legs 608/611/614
# (ruleset: expert target -6% / 0%). SLs are the RAT rule 80 stop: -4% from the open price.
TP_REPAIRS = {203: 31.283275199999995, 204: 57.88332, 205: 15.041026573999996}
SL_RULE_PCT = -4.0
SL_REPAIRS = [127, 128, 194, 195]


def main(apply: bool, force_sl=frozenset(), only=None) -> int:
    wire_all_seams()
    from ba2_trade_platform.core.utils import get_account_instance_from_id
    account = get_account_instance_from_id(ACCOUNT_ID)
    if account is None:
        print("account not found"); return 2

    plan = []
    for tid, tp in TP_REPAIRS.items():
        txn = get_instance(Transaction, tid)
        plan.append(("TP", txn, tp))
    for tid in SL_REPAIRS:
        txn = get_instance(Transaction, tid)
        plan.append(("SL", txn, round(float(txn.open_price) * (1 + SL_RULE_PCT / 100.0), 4)))

    if only:
        plan = [row for row in plan if row[1].id in only]
    symbols = sorted({t.symbol for _, t, _ in plan})
    prices = account.get_instrument_current_price(symbols)
    failures = 0
    for kind, txn, target in plan:
        px = prices.get(txn.symbol) if isinstance(prices, dict) else None
        state = (f"txn {txn.id} {txn.symbol} {txn.status} qty {txn.quantity} open {txn.open_price} "
                 f"TP {txn.take_profit} SL {txn.stop_loss} now {px}")
        if txn.status not in (TransactionStatus.OPENED,):
            print(f"SKIP  {state} -> not OPENED"); continue
        if kind == "SL" and px is not None and target >= float(px) and txn.id not in force_sl:
            print(f"SKIP  {state} -> new SL {target} is not below the current price"); failures += 1; continue
        if kind == "SL" and txn.id in force_sl:
            # Operator decision 2026-09-10 ("Stop at 4% it's fine"): the rule stop is placed
            # even though the price is already below it, so it executes at once.
            print(f"FORCE {state} -> SL {target} is at/above the current price {px}; it will trigger immediately")
        if kind == "TP" and px is not None and target <= float(px):
            print(f"SKIP  {state} -> new TP {target} is not above the current price"); failures += 1; continue
        print(f"{'APPLY' if apply else 'PLAN '} {kind} {target}  <- {state}")
        if not apply:
            continue
        try:
            ok = (account.adjust_tp(txn, target, source=SOURCE) if kind == "TP"
                  else account.adjust_sl(txn, target, source=SOURCE))
        except Exception as e:  # noqa: BLE001 - report every outcome, continue with the rest
            print(f"  FAILED {kind} txn {txn.id}: {e!r}"); failures += 1; continue
        after = get_instance(Transaction, txn.id)
        print(f"  -> {'ok' if ok else 'REFUSED'}; now TP {after.take_profit} SL {after.stop_loss} "
              f"tp_lock {after.tp_manual_override} sl_lock {after.sl_manual_override}")
        if not ok:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    # --force-sl 127,194,195 : place the rule stop even when the market is already below it.
    forced = frozenset()
    if "--force-sl" in sys.argv:
        forced = frozenset(int(x) for x in sys.argv[sys.argv.index("--force-sl") + 1].split(","))
    only = None
    if "--only" in sys.argv:
        only = frozenset(int(x) for x in sys.argv[sys.argv.index("--only") + 1].split(","))
    sys.exit(main(apply="--apply" in sys.argv, force_sl=forced, only=only))
