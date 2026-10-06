"""Pre-warm / repair the derived ATM-IV history cache (``<CACHE_FOLDER>/AtmIvHistory``).

OPTIONAL: the live path fills this cache by itself inside each symbol's analysis task
(``atm_iv_task_hook``). This tool calls the SAME blocking ``ensure_filled``, in this process -- useful to pre-warm before the first Monday or to repair after
a ``METHOD_VERSION`` bump.

SAFE BY DEFAULT
  * dry-run unless ``--apply``: prints per-symbol coverage from the store (ZERO API calls);
  * ``--apply`` ALWAYS needs ``--i-know-the-apps-are-stopped`` (two writers on one parquet are
    serialised only inside ONE process), EXCEPT for an explicit scratch folder: ``--scratch`` plus
    a ``--cache-dir`` that is NOT any app cache (prod / opt / dev / common);
  * NOTE the provider reads FMP OHLCV, the split calendar and the FRED DGS3MO file from the
    process's ``CACHE_FOLDER`` (env ``CACHE_FOLDER``), not from ``--cache-dir``: that cache is
    only READ for them (``FmpSpotSource`` may top up FMP OHLCV through the provider).

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


def _is_scratch(path: str) -> bool:
    """False for anything that looks like an app cache (common/prod/opt/dev), or any cache folder."""
    low = os.path.normcase(os.path.abspath(path))
    from ba2_trade_platform import config
    app = os.path.normcase(os.path.abspath(config.CACHE_FOLDER))
    if low == app or low.startswith(app + os.sep):
        return False
    return not any(tok in low for tok in (os.sep + "ba2" + os.sep, "ba2_trade_platform", os.sep + "cache" + os.sep,
                                          os.sep + "prod", os.sep + "opt", os.sep + "dev"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--lookback-days", type=int, default=365)
    ap.add_argument("--cache-dir", help="AtmIvHistory folder (default: <CACHE_FOLDER>/AtmIvHistory)")
    ap.add_argument("--paper", action="store_true", help="paper host for contract discovery")
    ap.add_argument("--scratch", action="store_true",
                    help="--cache-dir is a throwaway folder (not an app cache): --apply needs no flag")
    ap.add_argument("--apply", action="store_true", help="actually fetch and write (default: dry run)")
    ap.add_argument("--i-know-the-apps-are-stopped", action="store_true",
                    help="required with --apply on the LIVE cache folder")
    a = ap.parse_args(argv)

    from ba2_trade_platform import config
    from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H

    live = os.path.normcase(os.path.abspath(os.path.join(config.CACHE_FOLDER, "AtmIvHistory")))
    cache_dir = os.path.abspath(a.cache_dir) if a.cache_dir else live
    is_live = os.path.normcase(cache_dir) == live
    if a.apply and not a.i_know_the_apps_are_stopped:
        if not (a.scratch and a.cache_dir and _is_scratch(cache_dir)):
            print(f"REFUSED: --apply on {cache_dir}. Stop every app and pass "
                  f"--i-know-the-apps-are-stopped; only an explicit --scratch --cache-dir that is "
                  f"not an app cache (prod/opt/dev/common) is exempt.", file=sys.stderr)
            return 2

    from ba2_providers.options.alpaca import AlpacaOptionsProvider
    prov = H.AtmIvHistoryProvider(
        cache_dir=cache_dir, options_provider=AlpacaOptionsProvider(paper=a.paper))
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
        r = prov.ensure_filled(sym, end, a.lookback_days, deadline_seconds=3600)
        print(f"{sym}: {r.status} coverage {r.coverage}, {r.tombstones} tombstone(s)"
              + (f" -- {r.reason}" if r.reason else ""))
        if r.status != H.STATUS_COMPLETE:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
