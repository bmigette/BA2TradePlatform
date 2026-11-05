#!/usr/bin/env python
"""Cache the FMP dividend calendar of every universe symbol for the option basis guard.

The guard's carry separation (``app.services.backtest.option_basis_guard``, 2026-09-26) adds a
cash dividend back to the put-call-parity spot of every expiry after its ex-date, so a dividend
is not mistaken for a split-basis error. It reads
CACHE_FOLDER/fmp_history/mc_stock_dividend__<SYM>.json; a backtest never fetches. Without the
file the guard still runs, but it cannot add dividends back and says so in any refusal.

    python tools/warm_dividend_calendars.py --universe-file tools/options_universe_top100.txt

One FMP request per symbol (``historical-price-full/stock_dividend``), disk-cached for a day.
Exit codes: 0 every symbol cached; 1 at least one failed (named in the report); 2 a
configuration error. Prints a JSON report (requests and bytes from the FMP request meter).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2


def make_source(cache_root: str):
    """The FMP source the calendars are fetched through (seam: tests replace this)."""
    from ba2_providers.market_conditions.fmp_source import FMPWarmupSource
    return FMPWarmupSource(cache_root)


def _universe(path: str):
    with open(path, "r", encoding="utf-8") as f:
        syms = [line.split("#", 1)[0].strip().upper() for line in f]
    return list(dict.fromkeys(s for s in syms if s))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe-file", required=True)
    ap.add_argument("--cache-root", default=None)
    args = ap.parse_args(argv)
    if not os.path.isfile(args.universe_file):
        print(f"universe file not found: {args.universe_file}", file=sys.stderr)
        return EXIT_CONFIG
    if args.cache_root:
        root = os.path.abspath(args.cache_root)
    else:
        from ba2_common import config
        root = config.CACHE_FOLDER
    source = make_source(root)
    calls0, bytes0 = source.calls, source.bytes
    failed, rows = {}, 0
    symbols = _universe(args.universe_file)
    for sym in symbols:
        try:
            rows += len(source.dividend_calendar(sym))
        except Exception as e:  # noqa: BLE001 -- recorded per symbol and reported
            failed[sym] = f"{type(e).__name__}: {e}"
    print(json.dumps({"symbols": len(symbols), "rows": rows, "failed": failed,
                      "provider_calls": int(source.calls - calls0),
                      "provider_bytes": int(source.bytes - bytes0)}, indent=1, sort_keys=True))
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
