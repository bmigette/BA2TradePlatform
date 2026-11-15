"""PROBE: fetch the TastyTrade option-chain snapshot NOW for a few symbols and print ONLY its validity facts.

For the operator to learn, on weekdays, when the snapshot rolls over from the prior session (e.g. run at
20:00 ET, 00:30, 04:00, 08:00 and 09:25 ET). Prints, per symbol: the summary-date distribution, the
updated-at range, null and zero-bid counts, the near-money counts and the verdict of the data validity rule
for the session (default: the most recent closed session). Never prints secrets.

    python tools/probe_broker_iv_snapshot.py [--db-file PATH] [--use-account-id N] [--symbols MSFT,KO,AAPL]
                                            [--session YYYY-MM-DD] [--json out.jsonl]

Credentials: the app-level TastyTrade market-data credentials of --db-file, or (--use-account-id) those of
a TastyTrade account definition. READ-ONLY: the DB is opened with mode=ro; only market-data endpoints can
be called (see broker_iv_tools). Output: stdout, plus one JSON line per symbol appended to
reports/iv_broker_probe.jsonl (``--json`` to change, ``--json ''`` for stdout only).
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

DEFAULT_OUT = os.path.join(T._ROOT, "reports", "iv_broker_probe.jsonl")


def facts_line(sym: str, verdict) -> str:
    f = verdict.facts
    return (f"{sym}: verdict={'VALID' if verdict.ok else 'REFUSED (' + verdict.kind + ')'} "
            f"contracts={f.get('contracts')}/{f.get('requested')} summary_dates={f.get('summary_dates')} "
            f"updated_at=[{f.get('updated_at_min')} .. {f.get('updated_at_max')}] null_bid_or_ask={f.get('null_bid_or_ask')} "
            f"zero_bids={f.get('zero_bids')} near_money={f.get('near_money_of_session')}/{f.get('near_money')} "
            f"spot={f.get('spot')} ({f.get('spot_summary_date')})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    T.add_common_args(ap)
    ap.add_argument("--symbols", default="MSFT,KO,AAPL")
    ap.add_argument("--session", type=date.fromisoformat)
    ap.add_argument("--json", default=DEFAULT_OUT, help="append one JSON line per symbol (facts only); '' = none")
    ns = ap.parse_args(argv)
    from ba2_common.core.option_chain_quotes import validate_prior_session_snapshot

    session = ns.session or T.last_closed_session()
    client = T.market_data_client(ns.db_file, ns.use_account_id)
    at = datetime.now(timezone.utc).isoformat()
    creds = f"account {ns.use_account_id}" if ns.use_account_id else "app market-data credentials"
    print(f"probe at {at} -- {creds}, session {session}")
    if ns.json:
        os.makedirs(os.path.dirname(os.path.abspath(ns.json)), exist_ok=True)
    for sym in [s.strip().upper() for s in ns.symbols.split(",") if s.strip()]:
        t0 = time.monotonic()
        snap = T.snapshot(client, sym, session)
        verdict = validate_prior_session_snapshot(snap, session)
        print(facts_line(sym, verdict) + f" requests={snap.requests} {time.monotonic() - t0:.1f}s")
        if not verdict.ok:
            print(f"    {verdict.reason}")
        if ns.json:
            with open(ns.json, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"probe_at": at, "symbol": sym, "ok": verdict.ok, "kind": verdict.kind,
                                     "facts": verdict.facts}, default=str) + "\n")
    if ns.json:
        print(f"appended to {ns.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
