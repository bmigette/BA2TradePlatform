"""PROBE: fetch the broker option-chain snapshot NOW for a few symbols and print ONLY its validity facts.

For the operator to learn, on weekdays, when the broker's snapshot rolls over from the prior session
(e.g. run at 20:00 ET, 00:30, 04:00, 08:00 and 09:25 ET). Prints, per symbol: the summary-date
distribution, the updated-at range, null and zero-bid counts, the near-money counts and the verdict of
the data validity rule for the session (default: the most recent closed session). Never prints secrets.

    python tools/probe_broker_iv_snapshot.py [--db PATH] [--account-id N] [--symbols MSFT,KO,AAPL]
                                            [--session YYYY-MM-DD] [--json out.jsonl]

READ-ONLY: the DB is opened with mode=ro; only market-data endpoints are called (see broker_iv_tools).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker_iv_tools as T  # noqa: E402


def facts_line(sym: str, verdict) -> str:
    f = verdict.facts
    return (f"{sym}: verdict={'VALID' if verdict.ok else 'REFUSED (' + verdict.kind + ')'} "
            f"contracts={f.get('contracts')}/{f.get('requested')} summary_dates={f.get('summary_dates')} "
            f"updated_at=[{f.get('updated_at_min')} .. {f.get('updated_at_max')}] null_bid_or_ask={f.get('null_bid_or_ask')} "
            f"zero_bids={f.get('zero_bids')} near_money={f.get('near_money_of_session')}/{f.get('near_money')} "
            f"spot={f.get('spot')} ({f.get('spot_summary_date')})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=T.DEFAULT_DB)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--symbols", default="MSFT,KO,AAPL")
    ap.add_argument("--session", type=date.fromisoformat)
    ap.add_argument("--json", help="append one JSON line per symbol (facts only) to this file")
    ns = ap.parse_args(argv)
    from ba2_common.core.option_chain_quotes import validate_prior_session_snapshot

    account_id = T.quote_source_account_id(ns.db, ns.account_id)
    session = ns.session or T.last_closed_session()
    get_json, close = T.tastytrade_get_json(ns.db, account_id)
    print(f"probe at {datetime.now(timezone.utc).isoformat()} -- account {account_id}, session {session}")
    try:
        for sym in [s.strip().upper() for s in ns.symbols.split(",") if s.strip()]:
            t0 = time.monotonic()
            snap = T.snapshot(get_json, sym, session, account_id)
            verdict = validate_prior_session_snapshot(snap, session)
            print(facts_line(sym, verdict) + f" requests={snap.requests} {time.monotonic() - t0:.1f}s")
            if not verdict.ok:
                print(f"    {verdict.reason}")
            if ns.json:
                with open(ns.json, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"symbol": sym, "ok": verdict.ok, "kind": verdict.kind,
                                         "facts": verdict.facts}, default=str) + "\n")
    finally:
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
