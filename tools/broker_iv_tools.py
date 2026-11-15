"""Shared plumbing of the broker iv-v1 operator tools (``probe_broker_iv_snapshot.py``,
``certify_broker_iv_chain.py``). READ-ONLY everywhere:

* the platform DB is opened with SQLite ``mode=ro`` (nothing is ever written);
* the quote-source account comes from ``--account-id`` or the app setting ``iv_live_chain_source``;
* credentials stay in memory: never printed, logged or written (only TastyTrade is supported here --
  the only certified broker kind; its sandbox is refused, its quotes are fake);
* only market-data endpoints are called (chain listing, ``/market-data/by-type``).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "packages", "common"), os.path.join(_ROOT, "packages", "providers"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_DB = os.path.expanduser(os.path.join("~", "Documents", "ba2", "trade", "db.sqlite"))


def _ro(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{os.path.expanduser(db)}?mode=ro", uri=True)


def quote_source_account_id(db: str, account_id: Optional[int]) -> int:
    """``--account-id`` if given, else the app setting ``iv_live_chain_source`` (must be account:<id>)."""
    if account_id is not None:
        return int(account_id)
    from ba2_common.core.market_condition_iv_live import LIVE_CHAIN_SOURCE_SETTING, SOURCE_BROKER, parse_live_chain_source

    con = _ro(db)
    try:
        row = con.execute("SELECT value_str FROM appsetting WHERE key = ?", (LIVE_CHAIN_SOURCE_SETTING,)).fetchone()
    finally:
        con.close()
    choice = parse_live_chain_source(row[0] if row else None)
    if choice is None or choice.source != SOURCE_BROKER:
        raise SystemExit(f"{LIVE_CHAIN_SOURCE_SETTING} in {db} is not a quote-source account "
                         f"({row[0] if row else 'unset'!r}); pass --account-id")
    return int(choice.account_id)


def _provider(db: str, account_id: int) -> str:
    con = _ro(db)
    try:
        row = con.execute("SELECT provider FROM accountdefinition WHERE id = ?", (int(account_id),)).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit(f"account definition {account_id} not found in {db}")
    return str(row[0])


def tastytrade_get_json(db: str, account_id: int) -> Tuple[Callable[[str, Any], Any], Callable[[], None]]:
    """``(get_json, close)`` over a TastyTrade session built from the account's credentials, read-only."""
    provider = _provider(db, account_id)
    if "tasty" not in provider.lower():
        raise SystemExit(f"account {account_id} is a {provider} account; these tools support TastyTrade only "
                         f"(the only certified broker quote source)")
    from warm_options_history import _sandbox_from, load_credentials

    creds = load_credentials(db, int(account_id))
    if not creds.get("client_secret") or not creds.get("refresh_token"):
        raise SystemExit(f"account {account_id}: TastyTrade credentials incomplete")
    if _sandbox_from(creds):
        raise SystemExit(f"account {account_id} targets the TastyTrade sandbox, whose quotes are fake; refused")
    from tastytrade.session import Session

    loop = asyncio.new_event_loop()

    async def _make():
        return Session(provider_secret=creds["client_secret"], refresh_token=creds["refresh_token"], is_test=False)

    session = loop.run_until_complete(_make())
    del creds

    def get_json(path, params):
        return loop.run_until_complete(session._get(path, params=params))

    def close():
        try:
            loop.run_until_complete(session._client.aclose())
        finally:
            loop.close()

    return get_json, close


def last_closed_session(now: Optional[datetime] = None) -> date:
    from ba2_common.core.market_calendar import is_regular_session, regular_session_close_utc

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    from zoneinfo import ZoneInfo

    day = now.astimezone(ZoneInfo("America/New_York")).date()
    for _ in range(12):
        if is_regular_session(day) and regular_session_close_utc(day) <= now:
            return day
        day -= timedelta(days=1)
    raise SystemExit("no closed regular session in the last 12 days")


def snapshot(get_json, symbol: str, session: date, account_id: int):
    from ba2_common.core.option_chain_quotes import FETCH_STRIKE_BAND, fetch_expiry_window
    from ba2_providers.options.tastytrade_quotes import fetch_chain_quote_snapshot

    lo, hi = fetch_expiry_window(session)
    return fetch_chain_quote_snapshot(get_json, symbol, expiry_min=lo, expiry_max=hi, strike_band=FETCH_STRIKE_BAND,
                                      account_id=account_id)
