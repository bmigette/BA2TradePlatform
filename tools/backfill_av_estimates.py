"""Backfill the Alpha Vantage point-in-time EPS consensus cache (``av_eps_consensus``) + split calendars.

Sequential on purpose: ``fetch_av_consensus_payload`` makes 2 AV calls per symbol with a 1.1 s gap, so one
symbol at a time stays under the 75 calls/minute premium plan (a parallel prewarm would not). Resumable: a
symbol already in the disk cache is not refetched. Symbols AV cannot serve are listed in the report, not
retried forever.

    python tools/backfill_av_estimates.py --symbols-file tools/screener_store_universe_dated.txt \\
        --db ~/Documents/ba2/test/dl_forecasting.db \
        [--symbols-file ~/Documents/ba2/senate_universe.csv] [--limit N] [--report out.json]

Run with the code of the commit whose provider you backtest with (the venv's editable install must point
at it). Needs the ``alpha_vantage_api_key`` AppSetting (premium) and ``FMP_API_KEY`` (split calendars).
Then ``ba2-test cache push`` so remote workers get the namespace.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import List

#: 2 AV calls per symbol at <= 70 calls/minute (the premium plan allows 75).
_SYMBOL_MIN_S = 2 * 60.0 / 70


def _read_symbols(paths: List[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        text = open(os.path.expanduser(p), encoding="utf-8").read()
        for tok in text.replace(",", "\n").split():
            tok = tok.strip().upper()
            if tok and tok not in out:
                out.append(tok)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols-file", action="append", required=True)
    ap.add_argument("--limit", type=int, default=None, help="first N symbols only (a smoke run)")
    ap.add_argument("--report", default=None, help="write a JSON report (ok / failed with reasons)")
    ap.add_argument("--db", required=True,
                    help="sqlite DB holding the API keys (AppSetting), e.g. ~/Documents/ba2/test/dl_forecasting.db")
    args = ap.parse_args(argv)

    logging.disable(logging.WARNING)
    from ba2_common.core import db as _db
    _db.configure_db(os.path.expanduser(args.db))
    from ba2_providers.fmp_common import frozen_ttl_cache
    from ba2_providers.fundamentals.estimates.AlphaVantageEstimatesProvider import (
        load_av_consensus_payload, load_split_calendar)

    symbols = _read_symbols(args.symbols_file)
    if args.limit:
        symbols = symbols[:args.limit]
    ok, empty, failed = [], [], {}
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        # A symbol AV has no estimates for fails on its first call, before the provider's inter-call gap, so
        # unpaced no-data symbols fire back to back and trip the per-minute limit. Pace every symbol as if it
        # made both calls.
        if i > 1:
            wait = _SYMBOL_MIN_S - (time.monotonic() - sym_t0)
            if wait > 0:
                time.sleep(wait)
        sym_t0 = time.monotonic()
        try:
            # The disk cache is written only under the TTL freeze (the prewarm/backtest layer); outside it
            # fmp_history_disk_cached is the live pass-through and persists nothing.
            with frozen_ttl_cache():
                payload = load_av_consensus_payload(sym)
                (ok if payload else empty).append(sym)
                load_split_calendar(sym)
        except Exception as e:  # noqa: BLE001 -- one symbol's gap is reported, never stops the backfill
            failed[sym] = f"{type(e).__name__}: {str(e)[:200]}"
        if i % 50 == 0 or i == len(symbols):
            rate = i / max(time.time() - t0, 1e-9) * 60
            print(f"{i}/{len(symbols)}  ok={len(ok)} empty={len(empty)} failed={len(failed)}  "
                  f"{rate:.1f} symbols/min", flush=True)
    report = {"symbols": len(symbols), "ok": len(ok), "empty": empty, "failed": failed}
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
    print(json.dumps({k: (v if not isinstance(v, (list, dict)) else len(v)) for k, v in report.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
