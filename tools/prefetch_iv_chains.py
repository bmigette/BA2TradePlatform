#!/usr/bin/env python
"""Prefetch the Alpha Vantage end-of-day chains that LIVE iv-v1 gates read, ahead of the decision.

The live scheduler runs the same prefetch every weekday at 06:30 ET for experts with an active iv gate
(``ba2_trade_platform/modules/dataproviders/options/iv_chain_prefetch.py``). This tool runs it by hand
for an explicit symbol list -- e.g. after a restart past 06:30, or to warm a new universe::

    python tools/prefetch_iv_chains.py --symbols-file U.txt --db ~/Documents/ba2/trade/db.sqlite
    python tools/prefetch_iv_chains.py --symbols AAPL,MSFT --session 2026-11-13 --db ...

``--session`` defaults to the session a live decision made NOW would read (``prior_session_v1``).
Resumable: chains already cached are skipped. Rate-limited through the shared
``<cache>/_av_rate_budget.json`` (12 calls/minute for this owner, 75 for the key overall). The API key
comes from the AppSetting ``alpha_vantage_api_key`` of ``--db`` and is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=None, help="comma-separated symbols")
    ap.add_argument("--symbols-file", default=None, help="one symbol per line, # comments allowed")
    ap.add_argument("--session", default=None, help="the chain date (ISO); default: the live prior session")
    ap.add_argument("--db", required=True, help="sqlite DB holding the AppSetting alpha_vantage_api_key")
    ap.add_argument("--cache-root", default=None)
    args = ap.parse_args(argv)
    import logging
    logging.disable(logging.WARNING)
    from ba2_common.core import db as _db
    _db.configure_db(os.path.expanduser(args.db))
    from ba2_common.core.market_calendar import decision_data_session, live_decision_label
    from ba2_providers.market_conditions.av_live_chain import prefetch_chains

    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    if args.symbols_file:
        with open(args.symbols_file, encoding="utf-8") as f:
            symbols += [ln.split("#", 1)[0].strip().upper() for ln in f if ln.split("#", 1)[0].strip()]
    if not symbols:
        print("no symbols given", file=sys.stderr)
        return 2
    session = (date.fromisoformat(args.session) if args.session
               else decision_data_session(live_decision_label(datetime.now(timezone.utc))))
    report = prefetch_chains(symbols, session, cache_root=args.cache_root, log=lambda m: print(m, flush=True))
    print(json.dumps(report, indent=1, sort_keys=True, default=str))
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
