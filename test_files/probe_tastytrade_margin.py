"""READ-ONLY: reconcile the SDK margin report against TastyTrade's Cap Req screen.

The Cap Req table shows THREE numbers per symbol -- Requirement, Initial Req and
Maintenance -- and the account header's "BP Usage" matches the *Requirement* total,
not the Initial Req total. `get_symbol_margin_info` currently reads only
`initial_requirement`, so this establishes which SDK field is which.

Also dumps the quantity-precision fields, because the adapter reads
`minimum_increment_precision` (0 -> whole shares) while the account visibly holds
fractional positions.

Tripwired read-only. Run:
  PYTHONPATH=packages/common:packages/providers:packages/experts \
  venv/bin/python test_files/probe_tastytrade_margin.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_files.probe_tastytrade_live import (build_account, install_tripwire,  # noqa: E402
                                              load_settings)

# What the user's Cap Req screen showed at 2026-08-21 15:04 UTC.
SCREEN = {
    # symbol: (Requirement, BP Usage %, Initial Req, Maintenance)
    "AAOI": (164.68, 6.4, 329.35, 164.68),
    "BLOX": (195.41, 7.6, 195.41, 195.41),
    "DIVO": (0.62, 0.0, 1.24, 0.62),
    "LAZR": (450.70, 17.4, 450.70, 450.70),
    "MAIN": (59.40, 2.3, 118.80, 59.40),
    "MRVL": (126.90, 4.9, 253.80, 126.90),
    "SCHD": (0.50, 0.0, 1.00, 0.50),
    "VYMI": (0.51, 0.0, 1.02, 0.51),
    "IDVO": (22.07, 0.9, 44.14, 22.07),
}
SCREEN_TOTAL = (2404.22, 93.0, 3117.07, 2404.22)
SCREEN_STOCK_BP = 437.89
SCREEN_NET_LIQ = 2585.81


def main():
    install_tripwire()
    acct = build_account(load_settings())
    print(f"connected: {acct._account.account_number}\n")

    report = acct._run_async(acct._account.get_margin_requirements(acct._session))

    print("REPORT-LEVEL FIELDS")
    for f in sorted(type(report).model_fields):
        if f == "groups":
            continue
        print(f"    {f:<34} {getattr(report, f, None)!r}")

    print("\nPER-SYMBOL, SDK vs SCREEN  (screen values from the Cap Req tab)")
    hdr = (f"  {'sym':<6} {'margin_req':>12} {'initial_req':>12} {'maint_req':>12} "
           f"{'buying_power':>13} | {'scr Req':>9} {'scr Init':>9} {'scr Maint':>9}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    matches = {"margin_requirement": 0, "initial_requirement": 0,
               "maintenance_requirement": 0, "buying_power": 0}
    checked = 0

    for g in (report.groups or []):
        sym = getattr(g, "description", None) or getattr(g, "code", "?")
        if sym not in SCREEN:
            continue
        checked += 1
        req, _pct, init, maint = SCREEN[sym]
        vals = {f: getattr(g, f, None) for f in matches}
        print(f"  {sym:<6} "
              f"{str(vals['margin_requirement']):>12} "
              f"{str(vals['initial_requirement']):>12} "
              f"{str(vals['maintenance_requirement']):>12} "
              f"{str(vals['buying_power']):>13} | "
              f"{req:>9.2f} {init:>9.2f} {maint:>9.2f}")
        for f, v in vals.items():
            if v is not None and abs(abs(float(v)) - req) < 0.02:
                matches[f] += 1

    print(f"\n  -> which SDK field equals the screen's 'Requirement' column? "
          f"(of {checked} symbols)")
    for f, n in sorted(matches.items(), key=lambda kv: -kv[1]):
        print(f"       {f:<26} {n}/{checked}")

    # Totals
    tot = {}
    for f in ("margin_requirement", "initial_requirement", "maintenance_requirement"):
        s = sum(abs(float(getattr(g, f))) for g in (report.groups or [])
                if getattr(g, f, None) is not None)
        tot[f] = s
    print(f"\n  TOTALS      sdk margin_req={tot['margin_requirement']:.2f}  "
          f"initial={tot['initial_requirement']:.2f}  "
          f"maint={tot['maintenance_requirement']:.2f}")
    print(f"  SCREEN      Requirement={SCREEN_TOTAL[0]:.2f}  "
          f"Initial={SCREEN_TOTAL[2]:.2f}  Maintenance={SCREEN_TOTAL[3]:.2f}")
    print(f"  screen header: BP Usage={SCREEN_TOTAL[0]:.2f}  "
          f"Stock BP={SCREEN_STOCK_BP:.2f}  Net Liq={SCREEN_NET_LIQ:.2f}")

    snap = acct.get_account_snapshot()
    print(f"  our snapshot : buying_power={snap.buying_power}  "
          f"equity={snap.equity}  multiplier={snap.margin_multiplier}")

    # What rate does each candidate field imply, against real notional?
    print("\nIMPLIED MARGIN RATE per candidate field (requirement / position notional)")
    pos = {p.symbol: p for p in (acct.get_positions() or [])}
    print(f"  {'sym':<6} {'qty':>10} {'price':>9} {'notional':>10} "
          f"{'r(margin)':>10} {'r(initial)':>11} {'r(maint)':>10}")
    for sym in ("SCHD", "AAOI", "LAZR", "MAIN"):
        p = pos.get(sym)
        g = next((x for x in (report.groups or [])
                  if (getattr(x, "description", None) or getattr(x, "code", None)) == sym), None)
        if not p or not g:
            continue
        price = float(getattr(p, "current_price", 0) or 0) or float(p.market_value) / float(p.qty)
        notional = abs(float(p.qty) * price)
        def r(f):
            v = getattr(g, f, None)
            return f"{abs(float(v)) / notional:.4f}" if v is not None and notional else "-"
        print(f"  {sym:<6} {float(p.qty):>10.5f} {price:>9.2f} {notional:>10.2f} "
              f"{r('margin_requirement'):>10} {r('initial_requirement'):>11} "
              f"{r('maintenance_requirement'):>10}")

    print("\ndone - read-only, tripwire armed.")


if __name__ == "__main__":
    main()
