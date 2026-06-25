#!/usr/bin/env python
"""READ-ONLY probe of prod TastyTrade dividend transactions.

Points the DB at the prod state folder, loads TastyTrade account (id 2), and
prints recent Money Movement + Receive Deliver transactions with their
type/sub_type/description/net_value so we can confirm how cash dividends and
tax are tagged. No writes of any kind.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point at PROD db/cache BEFORE importing anything that binds DB_FILE.
import ba2_trade_platform.config as config
config.DB_FILE = r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite"
config.CACHE_FOLDER = r"C:\Users\basti\Documents\ba2_trade_platform-prod\cache"

from ba2_trade_platform.core.utils import get_account_instance_from_id

ACCOUNT_ID = 2
START = date(2026, 5, 25)


def dump(label, txns):
    print(f"\n=== {label} ({len(txns)} rows) ===")
    for t in txns:
        sym = getattr(t, "underlying_symbol", None) or getattr(t, "symbol", None) or "-"
        print(f"  {getattr(t,'transaction_date',None)} | {sym:<6} | "
              f"type={getattr(t,'transaction_type',None)} | "
              f"sub={getattr(t,'transaction_sub_type',None)} | "
              f"net={getattr(t,'net_value',None)} | qty={getattr(t,'quantity',None)} | "
              f"desc={getattr(t,'description',None)!r}")


def main():
    acct = get_account_instance_from_id(ACCOUNT_ID)
    print("account:", type(acct).__name__, "id", acct.id, "name?")
    if not acct._check_authentication():
        print("AUTH FAILED")
        return

    from datetime import datetime
    divs = acct.get_dividends(start_date=datetime(2025, 11, 1))
    print(f"\n=== get_dividends() -> {len(divs)} records (since 2025-11-01) ===")
    n_bad = 0
    tot_gross = tot_tax = tot_net = 0.0
    for dv in divs:
        flag = ""
        if dv['amount'] <= 0:
            flag = "  <-- NET <= 0"
            n_bad += 1
        if dv['gross_amount'] == 0:
            flag += "  <-- GROSS=0 (tax-only key!)"
        tot_gross += dv['gross_amount']; tot_tax += dv['tax_withheld']; tot_net += dv['amount']
        print(f"  {dv['date']} | {(dv['symbol'] or '-'):<6} | "
              f"gross={dv['gross_amount']:>7} | tax={dv['tax_withheld']:>6} | "
              f"NET={dv['amount']:>7} | drip={dv['drip_quantity']}{flag}")
    print(f"\nTOTAL gross={tot_gross:.2f} tax={tot_tax:.2f} net={tot_net:.2f} | "
          f"{n_bad} record(s) with net<=0")


if __name__ == "__main__":
    main()
