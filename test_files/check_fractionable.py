"""Ad-hoc probe: how many of our labelled instruments are NOT fractionable on Alpaca?

READ-ONLY. Opens the live DB with `mode=ro` and makes one read-only Alpaca call
(`get_all_assets`). Submits nothing, writes nothing, prints no credentials.

Run:  venv/bin/python test_files/check_fractionable.py
"""
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from ba2_trade_platform.core.seam_wiring import wire_all_seams
from ba2_trade_platform.core.utils import get_account_instance_from_id

DB = os.path.expanduser("~/Documents/ba2/trade/db.sqlite")
ACCOUNT_ID = int(os.environ.get("ACCOUNT_ID", "3"))


def labelled_symbols():
    """Every instrument name we track, read-only."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT name, instrument_type FROM instrument WHERE name IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    return {name.strip().upper(): itype for name, itype in rows if name and name.strip()}


def main():
    wire_all_seams()

    ours = labelled_symbols()
    print(f"Instruments in DB: {len(ours)}")

    account = get_account_instance_from_id(ACCOUNT_ID)
    client = account.client

    assets = client.get_all_assets(
        GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    )
    by_symbol = {a.symbol.upper(): a for a in assets}
    print(f"Active US equities at Alpaca: {len(by_symbol)}")

    frac, nofrac, missing, not_tradable = [], [], [], []
    for sym in sorted(ours):
        a = by_symbol.get(sym)
        if a is None:
            missing.append(sym)
        elif not a.tradable:
            not_tradable.append(sym)
        elif getattr(a, "fractionable", False):
            frac.append(sym)
        else:
            nofrac.append(sym)

    matched = len(frac) + len(nofrac)
    print("\n" + "=" * 60)
    print(f"  fractionable      {len(frac):5d}"
          + (f"  ({100 * len(frac) / matched:.1f}% of matched)" if matched else ""))
    print(f"  NOT fractionable  {len(nofrac):5d}"
          + (f"  ({100 * len(nofrac) / matched:.1f}% of matched)" if matched else ""))
    print(f"  not tradable      {len(not_tradable):5d}")
    print(f"  unknown to Alpaca {len(missing):5d}")
    print("=" * 60)

    if nofrac:
        print(f"\nNOT fractionable ({len(nofrac)}):")
        for i in range(0, len(nofrac), 10):
            print("   " + "  ".join(f"{s:<6}" for s in nofrac[i:i + 10]))
        print("\n  by instrument_type:",
              dict(Counter(ours[s] for s in nofrac)))

    if missing:
        print(f"\nUnknown to Alpaca ({len(missing)}) — first 40:")
        for i in range(0, min(len(missing), 40), 10):
            print("   " + "  ".join(f"{s:<6}" for s in missing[i:i + 10]))

    if not_tradable:
        print(f"\nNot tradable ({len(not_tradable)}):")
        for i in range(0, len(not_tradable), 10):
            print("   " + "  ".join(f"{s:<6}" for s in not_tradable[i:i + 10]))


if __name__ == "__main__":
    main()
