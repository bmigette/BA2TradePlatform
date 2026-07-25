"""One-off driver: extend the options cache with INDEX/ETF underlyings (SPY/QQQ/IWM).

WHY A DRIVER RATHER THAN THE CLI: ``ba2-test fetch-options`` resolves credentials via
``get_app_setting("alpaca_market_api_key")``, and that appsetting row is STALE in both the
test and trade DBs (a 20-char ``PK...`` key that now returns HTTP 401). Because the launcher
does ``get_app_setting(...) or os.getenv(...)``, the stale value is truthy and wins, so the
env fallback never fires and the CLI cannot be fixed by exporting variables. ``build_cache``
accepts explicit creds, so this passes a WORKING pair directly and mutates no DB.

WHICH CREDENTIALS: the per-account rows in ``accountsetting`` (not ``appsetting``) are current.
Verified 2026-07-25 against the options-bars endpoint: prod acct1 "Alcapa Live" (AK..., live)
and all three trade paper accounts (PK...) return 200; both appsetting pairs return 401.
This uses a PAPER key deliberately -- it serves identical historical data, and running a long
bulk fetch on the live trading credential risks rate-limit contention with the production
platform if it is trading.

FEED: ``indicative``, matching how the existing 13.7M-bar cache was built. Mixing feeds inside
one cache would make bars from different sources silently incomparable.

Run:  .venv/Scripts/python.exe test_files/fetch_index_options.py
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
_TESTPLATFORM = os.path.join(HERE, "..", "testplatform")
sys.path.insert(0, _TESTPLATFORM)

# Reuse the launcher's OWN bootstrap rather than reimplementing it. It chdirs into backend/,
# puts it on the path, and -- the part that matters -- calls ba2_common.core.db.configure_db()
# with app.models.database.DATABASE_URL so get_app_setting() resolves against the TEST DB.
# Without it a fresh process reads ba2_common's neutral default (BA2_HOME/db.sqlite), which
# has no appsetting table at all, and every symbol dies with "FMP API key not configured".
from ba2test_launcher import _enter_backend  # noqa: E402

_enter_backend()

TRADE_DB = r"C:\Users\basti\Documents\ba2\trade\db.sqlite"
PROD_DB = r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite"
DEFAULT_CACHE = os.path.expanduser(
    r"~\Documents\ba2\common\cache\options\options_history.sqlite")


def creds(db: str, account_id: int):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    d = {k: v for k, v in con.execute(
        "select key,value_str from accountsetting where account_id=?", (account_id,))}
    return d.get("api_key"), d.get("api_secret")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlyings", default="SPY,QQQ,IWM")
    ap.add_argument("--start", default="2024-02-01")   # matches the existing cache window
    ap.add_argument("--end", default="2026-07-07")
    ap.add_argument("--cache-db", default=DEFAULT_CACHE)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--feed", default="indicative")
    ap.add_argument("--use-live-creds", action="store_true",
                    help="Use the prod LIVE account key instead of a paper key. Only if the "
                         "paper accounts lose options-data entitlement; risks rate-limit "
                         "contention with the live platform.")
    a = ap.parse_args()

    # Quiet the per-request DEBUG spam so progress stays readable in a long run.
    logging.getLogger("ba2_common").setLevel(logging.WARNING)

    # The launcher's _bootstrap mirrors these from app settings into the environment; this
    # driver skips _bootstrap (that is the point -- it is what pulls in the stale Alpaca
    # appsetting), so mirror them here. FMP supplies the UNDERLYING closes that
    # option_greeks.py needs to invert Black-Scholes; without it every symbol fails with
    # "FMP API key not configured". FRED supplies the risk-free rate (minor: only rho).
    con = sqlite3.connect(f"file:{TRADE_DB}?mode=ro", uri=True)
    for env_name, keys in (("FMP_API_KEY", ("FMP_API_KEY", "fmp_api_key")),
                           ("FRED_API_KEY", ("fred_api_key", "FRED_API_KEY"))):
        if os.environ.get(env_name):
            continue
        for k in keys:
            row = con.execute(
                "select value_str from appsetting where key=?", (k,)).fetchone()
            if row and row[0]:
                os.environ[env_name] = row[0]
                break
    print(f"env   : FMP_API_KEY={'set' if os.environ.get('FMP_API_KEY') else 'MISSING'}, "
          f"FRED_API_KEY={'set' if os.environ.get('FRED_API_KEY') else 'missing (flat rf rate)'}")

    if a.use_live_creds:
        key, sec = creds(PROD_DB, 1)
        paper, which = False, "PROD acct1 (Alcapa LIVE)"
    else:
        key, sec = creds(TRADE_DB, 1)
        paper, which = True, "TRADE acct1 (paper)"
    if not (key and sec):
        raise SystemExit(f"no credentials found for {which}")

    from app.services.backtest import fetch_options

    unders = [u.strip().upper() for u in a.underlyings.split(",") if u.strip()]
    print(f"creds : {which} key {key[:3]}...{key[-3:]} (len {len(key)}), paper={paper}")
    print(f"fetch : {unders}  {a.start} -> {a.end}  feed={a.feed} workers={a.workers}")
    print(f"cache : {a.cache_db}", flush=True)

    stats = fetch_options.build_cache(
        a.cache_db, unders, date.fromisoformat(a.start), date.fromisoformat(a.end),
        a.feed, api_key=key, api_secret=sec,
        max_workers=a.workers, resume=True, paper=paper)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
