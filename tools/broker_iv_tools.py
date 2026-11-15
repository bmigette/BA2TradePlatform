"""Shared plumbing of the TastyTrade iv-v1 operator tools (``probe_broker_iv_snapshot.py``,
``certify_broker_iv_chain.py``). READ-ONLY everywhere:

* the platform DB (``--db-file``) is opened with SQLite ``mode=ro`` (nothing is ever written);
* the credentials are the app-level TastyTrade MARKET-DATA credentials of that DB (app settings
  ``tastytrade_market_client_secret`` / ``tastytrade_market_refresh_token``), or -- with
  ``--use-account-id N`` -- the TastyTrade credentials of account definition N (allowed for read-only
  tests until a dedicated market-data token exists). They stay in memory: never printed, logged or
  written; a sandbox account is refused (its quotes are fake);
* only market-data endpoints can be called (``TastyTradeMarketDataClient`` refuses every other path).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "packages", "common"), os.path.join(_ROOT, "packages", "providers"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_DB = os.path.expanduser(os.path.join("~", "Documents", "ba2", "trade", "db.sqlite"))
TT_SECRET_SETTING = "tastytrade_market_client_secret"
TT_REFRESH_SETTING = "tastytrade_market_refresh_token"


def _ro(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{os.path.expanduser(db)}?mode=ro", uri=True)


def add_common_args(ap) -> None:
    ap.add_argument("--db-file", default=DEFAULT_DB, help="platform DB to read the credentials from (read-only)")
    ap.add_argument("--use-account-id", type=int,
                    help="read the TastyTrade credentials of this account definition instead of the app-level "
                         "market-data credentials (read-only tests)")


def market_data_client(db: str, use_account_id: Optional[int] = None):
    """The market-data-only client over the DB's credentials (see the module docstring)."""
    from ba2_providers.options.tastytrade_quotes import TastyTradeMarketDataClient

    if use_account_id is None:
        con = _ro(db)
        try:
            rows = dict(con.execute("SELECT key, value_str FROM appsetting WHERE key IN (?, ?)",
                                    (TT_SECRET_SETTING, TT_REFRESH_SETTING)).fetchall())
        finally:
            con.close()
        client = TastyTradeMarketDataClient(rows.get(TT_SECRET_SETTING), rows.get(TT_REFRESH_SETTING))
        if not client.configured:
            raise SystemExit(f"{db}: the TastyTrade market-data credentials ({TT_SECRET_SETTING}, "
                             f"{TT_REFRESH_SETTING}) are not set; set them or pass --use-account-id")
        return client
    con = _ro(db)
    try:
        row = con.execute("SELECT provider FROM accountdefinition WHERE id = ?", (int(use_account_id),)).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit(f"account definition {use_account_id} not found in {db}")
    if "tasty" not in str(row[0]).lower():
        raise SystemExit(f"account {use_account_id} is a {row[0]} account; --use-account-id needs a TastyTrade account")
    from warm_options_history import _sandbox_from, load_credentials

    creds = load_credentials(db, int(use_account_id))
    if _sandbox_from(creds):
        raise SystemExit(f"account {use_account_id} targets the TastyTrade sandbox, whose quotes are fake; refused")
    client = TastyTradeMarketDataClient(creds.get("client_secret"), creds.get("refresh_token"))
    del creds
    if not client.configured:
        raise SystemExit(f"account {use_account_id}: TastyTrade credentials incomplete")
    return client


def last_closed_session(now: Optional[datetime] = None) -> date:
    from zoneinfo import ZoneInfo

    from ba2_common.core.market_calendar import is_regular_session, regular_session_close_utc

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    for _ in range(12):
        if is_regular_session(day) and regular_session_close_utc(day) <= now:
            return day
        day -= timedelta(days=1)
    raise SystemExit("no closed regular session in the last 12 days")


def snapshot(client, symbol: str, session: date):
    from ba2_common.core.option_chain_quotes import FETCH_STRIKE_BAND, fetch_expiry_window
    from ba2_providers.options.tastytrade_quotes import fetch_chain_quote_snapshot

    lo, hi = fetch_expiry_window(session)
    return fetch_chain_quote_snapshot(client.get_json, symbol, expiry_min=lo, expiry_max=hi,
                                      strike_band=FETCH_STRIKE_BAND)
