"""CERTIFY a broker quote source against the Alpha Vantage chain the iv-v1 gates were trained on.

For a symbol list and a session S, compare the broker's prior-session snapshot (validated by the data rule)
with the Alpha Vantage chain of S, through the SAME mapping and ``compute_iv_v1``, and APPEND one JSON line
per symbol to the report: per-contract bid/ask equality (all common contracts / near-money), contracts
present in one source only, the four iv-v1 values of each source, their differences, and the number of
GA thresholds (the searched grid of each field) on which a gate would decide differently.

The Alpha Vantage side comes from:
  * ``--av-source store`` (default): the LOCAL Alpha Vantage option store -- needs NO Alpha Vantage
    subscription (the session must already be in the store);
  * ``--av-source api``: one HISTORICAL_OPTIONS call per symbol with the app setting's key, through the
    shared rate budget (``_av_rate_budget.json``).

A broker snapshot is the prior-session chain only between S's close and the next open: run it then
(``--session`` defaults to the most recent closed session). The pairing ``alphavantage -> tastytrade``
rests on this evidence (docs/plans/2026-11-15-option-iv-conditions.md 6.6): run it daily while an AV key or
the store covers the session, and keep the report.

    python tools/certify_broker_iv_chain.py --symbols MSFT,KO,AAPL [--session YYYY-MM-DD]
        [--av-source store|api] [--report reports/iv_broker_certification.jsonl] [--db-file PATH]
        [--use-account-id N]

Credentials as in probe_broker_iv_snapshot.py (app-level market-data credentials, or --use-account-id).

READ-ONLY (DB mode=ro, market-data endpoints only); never prints secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker_iv_tools as T  # noqa: E402

DEFAULT_REPORT = os.path.join(T._ROOT, "reports", "iv_broker_certification.jsonl")


def _keys(chain) -> Dict[tuple, tuple]:
    return {(int(e), bool(c), float(k)): (float(b), float(a))
            for e, c, k, b, a in zip(chain.expiry_days, chain.is_call, chain.strike, chain.bid, chain.ask)}


def _thresholds(field):
    n = int(round((field.value_max - field.value_min) / field.value_step))
    return [round(field.value_min + i * field.value_step, 10) for i in range(n + 1)]


def compare(symbol: str, session: date, broker_chain, av_chain, window, wstatus="valid", wreason="",
            spot: Optional[float] = None) -> Dict[str, Any]:
    """The comparison record of one symbol (pure: chains are ``implied_vol.ChainQuotes``)."""
    from ba2_common.core.market_conditions import CHAIN_COMPUTE_BY_PROFILE, IV_PROFILE, PROFILES

    compute = CHAIN_COMPUTE_BY_PROFILE[IV_PROFILE]
    rb = compute(session, broker_chain, window, wstatus, wreason)
    ra = compute(session, av_chain, window, wstatus, wreason)
    kb, ka = _keys(broker_chain), _keys(av_chain)
    common = sorted(set(kb) & set(ka))
    eq = [kb[k] == ka[k] for k in common]
    near = [k for k in common if spot and abs(k[2] / spot - 1) <= 0.05]
    eq_near = [kb[k] == ka[k] for k in near]
    fields = {}
    flips = {}
    for f in PROFILES[IV_PROFILE].fields:
        vb, va = rb.values[f.name].value, ra.values[f.name].value
        fields[f.name] = {"broker": vb, "alphavantage": va,
                          "diff": (vb - va) if vb is not None and va is not None else None}
        if f.searched and f.kind == "numeric" and vb is not None and va is not None:
            th = _thresholds(f)
            flips[f.name] = {"thresholds": len(th), "flipped": int(sum((vb > t) != (va > t) for t in th))}
    return {"symbol": symbol, "session": session.isoformat(), "common_contracts": len(common),
            "only_broker": len(set(kb) - set(ka)), "only_alphavantage": len(set(ka) - set(kb)),
            "identical_quotes_share": (sum(eq) / len(eq)) if eq else None,
            "near_money_contracts": len(near),
            "identical_quotes_share_near_money": (sum(eq_near) / len(eq_near)) if eq_near else None,
            "fields": fields, "gate_flips": flips,
            "identical_rows": dict(rb.by_field()) == dict(ra.by_field())}


def _av_chain_store(symbol: str, session: date, cache_root: str):
    from ba2_providers.market_conditions.option_chain_source import chain_source_for

    src = chain_source_for("alphavantage", cache_root=cache_root)
    return src.chains(symbol, session, session).get(session)


def _av_chain_api(symbol: str, session: date, db: str, cache_root: str):
    from ba2_providers.av_rate_budget import FileRateBudget
    from ba2_providers.market_conditions.av_live_chain import chain_quotes
    from ba2_providers.options.alphavantage import fetch_chain_payload, map_chain

    con = T._ro(db)
    try:
        row = con.execute("SELECT value_str FROM appsetting WHERE key='alpha_vantage_api_key'").fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise SystemExit("--av-source api needs alpha_vantage_api_key in the app settings")
    FileRateBudget(os.path.join(cache_root, "_av_rate_budget.json"), owner=f"iv_certify:{os.getpid()}",
                   per_minute=5, key_limit=75).acquire()
    frame, _ = map_chain(symbol, session, fetch_chain_payload(symbol, session, api_key=row[0], timeout=60))
    return chain_quotes(frame, symbol, session)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    T.add_common_args(ap)
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--session", type=date.fromisoformat)
    ap.add_argument("--av-source", choices=("store", "api"), default="store")
    ap.add_argument("--cache-root", help="cache folder holding the AV store and the daily bars (default: the platform's)")
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ns = ap.parse_args(argv)
    from ba2_common.core.option_chain_quotes import validate_prior_session_snapshot
    from ba2_providers.market_conditions.av_live_chain import AlphaVantageLiveChainReader, chain_quotes
    from ba2_providers.market_conditions.broker_live_chain import map_snapshot

    if ns.cache_root is None:
        from ba2_common.core import native_cache
        ns.cache_root = native_cache.CACHE_FOLDER
    session = ns.session or T.last_closed_session()
    windows = AlphaVantageLiveChainReader("iv-v1", cache_root=ns.cache_root)
    client = T.market_data_client(ns.db_file, ns.use_account_id)
    os.makedirs(os.path.dirname(os.path.abspath(ns.report)), exist_ok=True)
    for sym in [s.strip().upper() for s in ns.symbols.split(",") if s.strip()]:
        rec: Dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "serving_kind": "tastytrade",
                               "credentials": "account" if ns.use_account_id else "app",
                               "av_source": ns.av_source}
        snap = T.snapshot(client, sym, session)
        verdict = validate_prior_session_snapshot(snap, session)
        if not verdict.ok:
            rec.update(symbol=sym, session=session.isoformat(), refused=verdict.reason, facts=verdict.facts)
        else:
            frame, _stats = map_snapshot(sym, session, verdict.kept)
            broker_chain = chain_quotes(frame, sym, session)
            av_chain = (_av_chain_store(sym, session, ns.cache_root) if ns.av_source == "store"
                        else _av_chain_api(sym, session, ns.db_file, ns.cache_root))
            if av_chain is None:
                rec.update(symbol=sym, session=session.isoformat(),
                           refused=f"the Alpha Vantage {ns.av_source} has no chain for {sym} on {session}")
            else:
                win, ws, wr = windows._window(sym, session)
                rec.update(compare(sym, session, broker_chain, av_chain, win, ws, wr, snap.spot))
                rec["facts"] = verdict.facts
        with open(ns.report, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        short = (f"refused: {rec['refused']}" if "refused" in rec else
                 f"identical quotes {rec['identical_quotes_share']:.4f} (near money "
                 f"{rec['identical_quotes_share_near_money']}), identical rows {rec['identical_rows']}, "
                 f"flips {sum(v['flipped'] for v in rec['gate_flips'].values())}")
        print(f"{sym} {session}: {short}")
    print(f"appended to {ns.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
