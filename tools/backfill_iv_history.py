"""Pre-warm / repair the derived ATM-IV history cache (``<CACHE_FOLDER>/AtmIvHistory``).

OPTIONAL: the live path fills this cache by itself when ``get_iv_rank`` asks for a series (the
foreground tail extension + one background worker). This tool calls the SAME provider method, in
this process, with a large budget -- useful to pre-warm before the first Monday or to repair after
a ``METHOD_VERSION`` bump.

SAFE BY DEFAULT
  * dry-run unless ``--apply``: prints per-symbol coverage from the store (ZERO API calls);
  * ``--apply`` against the live cache folder additionally needs ``--i-know-the-apps-are-stopped``
    (two writers on one parquet are serialised only inside ONE process). Point ``--cache-dir`` at a
    copy to avoid the flag.

Credentials: ``ALPACA_MARKET_API_KEY`` / ``ALPACA_MARKET_API_SECRET`` (or the generic names
``AlpacaOptionsProvider`` accepts). Never printed.

    python tools/backfill_iv_history.py --symbols AAPL MSFT                 # dry run
    python tools/backfill_iv_history.py --symbols AAPL --apply --cache-dir D:\\tmp\\ivcopy
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--lookback-days", type=int, default=365)
    ap.add_argument("--cache-dir", help="AtmIvHistory folder (default: <CACHE_FOLDER>/AtmIvHistory)")
    ap.add_argument("--paper", action="store_true", help="paper host for contract discovery")
    ap.add_argument("--apply", action="store_true", help="actually fetch and write (default: dry run)")
    ap.add_argument("--i-know-the-apps-are-stopped", action="store_true",
                    help="required with --apply on the LIVE cache folder")
    a = ap.parse_args(argv)

    from ba2_trade_platform import config
    from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H

    live = os.path.normcase(os.path.abspath(os.path.join(config.CACHE_FOLDER, "AtmIvHistory")))
    cache_dir = os.path.abspath(a.cache_dir) if a.cache_dir else live
    is_live = os.path.normcase(cache_dir) == live
    if a.apply and is_live and not a.i_know_the_apps_are_stopped:
        print(f"REFUSED: {cache_dir} is the live cache. Stop the apps and pass "
              f"--i-know-the-apps-are-stopped, or use --cache-dir on a copy.", file=sys.stderr)
        return 2

    from ba2_providers.options.alpaca import AlpacaOptionsProvider
    prov = H.AtmIvHistoryProvider(
        cache_dir=cache_dir, options_provider=AlpacaOptionsProvider(paper=a.paper),
        background_runner=lambda job: job())          # run the background fill inline, blocking
    end = prov.last_completed_session()
    print(f"cache: {cache_dir}  (live={is_live})  last completed session: {end}  "
          f"method: {H.METHOD_VERSION}  mode: {'APPLY' if a.apply else 'dry-run'}")
    rc = 0
    for sym in (s.upper() for s in a.symbols):
        if not a.apply:
            r = prov.peek(sym, end, a.lookback_days)
            print(f"{sym}: coverage {r.coverage}, {r.tombstones} tombstone(s), "
                  f"{r.unresolved} session(s) to derive")
            continue
        r = prov.get_atm_iv_series(sym, end, a.lookback_days, max_api_calls=10 ** 6, max_seconds=3600)
        if r.status == H.STATUS_FILLING:         # the inline background job has finished: re-read
            r = prov.peek(sym, end, a.lookback_days)
        print(f"{sym}: {r.status} coverage {r.coverage}, {r.tombstones} tombstone(s)"
              + (f" -- {r.reason}" if r.reason else ""))
        if r.status != H.STATUS_COMPLETE:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
