"""Per-account live OHLCV history source (plan 2026-10-24-history-source-task11, Tasks 6-8).

The ONE place that turns "which account" into "which history provider", and the host side of the corporate-actions
refresh of the Alpaca history cache. Pure naming / validation lives in ``ba2_common.core.ohlcv_history``; the cache logic
in ``ba2_providers.ohlcv.alpaca_history``. Nothing here falls back to another source: a failed Alpaca read is a named
error, never a silent FMP read.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlmodel import select

from ba2_common.core.interfaces.ExtendableSettingsInterface import decode_setting_rows
from ba2_common.core.ohlcv_history import ACCOUNT_HISTORY_SOURCE_KEY, account_history_source

from ..logger import logger
from .db import InstanceNotFound, get_all_instances, get_db, get_instance
from .models import AccountDefinition, AccountSetting, ExpertInstance, TradingOrder, Transaction

_PLAIN_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z])?$")    # an equity ticker (not an OCC contract string)


class HistorySourceNeedsAlpacaKeys(ValueError):
    """``alpaca`` was chosen for an account that holds no Alpaca api_key / api_secret."""


def account_settings(account_id: int) -> Dict[str, Any]:
    """The typed settings dict of an account, read from its rows WITHOUT instantiating the account (an instance of a
    broker class may open a session); every declared key is present (None when unset)."""
    from ..modules.accounts import get_account_class
    try:
        definition = get_instance(AccountDefinition, account_id)
    except InstanceNotFound as e:
        raise ValueError(f"account {account_id} does not exist") from e
    account_cls = get_account_class(definition.provider)
    if account_cls is None:
        raise ValueError(f"account {account_id}: unknown provider {definition.provider!r}")
    with get_db() as session:
        rows = session.exec(select(AccountSetting).where(AccountSetting.account_id == account_id)).all()
    return decode_setting_rows(rows, account_cls.get_merged_settings_definitions())


def alpaca_keys(settings: Dict[str, Any]) -> Optional[Dict[str, str]]:
    key, secret = settings["api_key"] if "api_key" in settings else None, settings["api_secret"] if "api_secret" in settings else None
    return {"api_key": key, "api_secret": secret} if key and secret else None


def validate_account_history_source(settings: Dict[str, Any]) -> str:
    """The source ``settings`` selects, or raise: an unknown value (``UnknownHistorySource``), or ``alpaca`` on an account
    without Alpaca keys (TastyTrade, IBKR, an Alpaca account missing its keys: ``HistorySourceNeedsAlpacaKeys``)."""
    source = account_history_source(settings)
    if source == "alpaca" and alpaca_keys(settings) is None:
        raise HistorySourceNeedsAlpacaKeys(
            "ohlcv_history_source 'alpaca' needs Alpaca market-data keys (api_key and api_secret); this account has none")
    return source


def history_source_for_account(account_id: int) -> str:
    """``fmp`` | ``alpaca`` for an account (declared default ``fmp`` when unset). Raises for an unknown account, an
    unknown stored value, or ``alpaca`` without Alpaca keys: there is no fallback to another source."""
    return validate_account_history_source(account_settings(account_id))


def history_spec_for_account(account_id: int):
    """``(registry name, constructor kwargs)`` of the OHLCV history provider of ``account_id``."""
    settings = account_settings(account_id)
    if validate_account_history_source(settings) == "fmp":
        return "fmp", {}
    return "alpaca_hist", {**alpaca_keys(settings), "corporate_actions_refresher": corporate_actions_refresher()}


def history_provider_for_account(account_id: int):
    from ..modules.dataproviders import get_provider
    name, kwargs = history_spec_for_account(account_id)
    return get_provider("ohlcv", name, **kwargs)


def _expert_account_id(expert_id: int) -> int:
    try:
        return get_instance(ExpertInstance, expert_id).account_id
    except InstanceNotFound as e:
        raise ValueError(f"expert instance {expert_id} does not exist; its history source cannot be resolved") from e


def history_spec_for_expert(expert_id: int):
    return history_spec_for_account(_expert_account_id(expert_id))


def history_provider_for_expert(expert_id: int):
    return history_provider_for_account(_expert_account_id(expert_id))


def _transaction_account_id(transaction_id: int) -> int:
    with get_db() as session:
        order = session.exec(select(TradingOrder).where(TradingOrder.transaction_id == transaction_id).limit(1)).first()
    if order is None:
        raise ValueError(f"transaction {transaction_id} has no order, so no account; its history source cannot be resolved")
    return order.account_id


def history_spec_for_transaction(transaction_id: int):
    return history_spec_for_account(_transaction_account_id(transaction_id))


def history_provider_for_transaction(transaction_id: int):
    return history_provider_for_account(_transaction_account_id(transaction_id))


def indicator_provider_for_expert(expert_id: int):
    """The ATR indicator provider of an expert's account: built on the account's Alpaca history for an ``alpaca`` account;
    ``None`` for an ``fmp`` account, which keeps the default (yfinance) stack exactly as before (owner 2026-10-24)."""
    return indicator_provider_for_account(_expert_account_id(expert_id))


def indicator_provider_for_account(account_id: int):
    from ..modules.dataproviders import get_provider
    name, kwargs = history_spec_for_account(account_id)
    if name == "fmp":
        return None
    return get_provider("indicators", "pandas", ohlcv_provider=get_provider("ohlcv", name, **kwargs))


def alpaca_history_accounts() -> List[Dict[str, Any]]:
    """``[{"id", "api_key", "api_secret"}]`` of every account whose history source is ``alpaca``. An account whose stored
    setting is unusable is logged at ERROR and left out (the job must not die for one bad row)."""
    out = []
    for definition in get_all_instances(AccountDefinition):
        try:
            settings = account_settings(definition.id)
            if validate_account_history_source(settings) == "alpaca":
                out.append({"id": definition.id, **alpaca_keys(settings)})
        except (ValueError, KeyError) as e:
            logger.error(f"account {definition.id}: unusable {ACCOUNT_HISTORY_SOURCE_KEY} setting: {e}", exc_info=True)
    return out


def held_or_traded_symbols(account_ids: List[int]) -> List[str]:
    """Equity tickers ordered on, or with a live transaction of an expert of, the given accounts."""
    if not account_ids:
        return []
    from .types import TransactionStatus
    live = (TransactionStatus.WAITING, TransactionStatus.OPENED, TransactionStatus.CLOSING)
    symbols = set()
    with get_db() as session:
        symbols.update(session.exec(select(TradingOrder.symbol).where(TradingOrder.account_id.in_(account_ids))).all())
        expert_ids = session.exec(select(ExpertInstance.id).where(ExpertInstance.account_id.in_(account_ids))).all()
        if expert_ids:
            symbols.update(session.exec(select(Transaction.symbol).where(
                Transaction.expert_id.in_(expert_ids), Transaction.status.in_(live))).all())
    return sorted(s.upper() for s in symbols if s and _PLAIN_TICKER.match(s.upper()))


def _fmp_rows_getter():
    """FMP split-calendar rows for a date range (30-day windows), or None when no FMP key is stored."""
    from ..config import get_app_setting
    from ba2_providers.ohlcv.alpaca_history.split_build import FMP_ROW_CAP, FMP_WINDOW_DAYS, make_fmp_getter
    key = get_app_setting("FMP_API_KEY")
    if not key:
        return None
    get = make_fmp_getter(key)

    def rows(start: date, end: date) -> List[dict]:
        out, a = [], start
        while a <= end:
            b = min(a + timedelta(days=FMP_WINDOW_DAYS - 1), end)
            part = get(a, b)
            if len(part) >= FMP_ROW_CAP:
                raise RuntimeError(f"FMP split calendar {a}..{b} returned {len(part)} rows (cap {FMP_ROW_CAP}): may be truncated")
            out += part
            a = b + timedelta(days=1)
        return out
    return rows


def _refresh_clients(accounts: List[Dict[str, Any]]):
    from ba2_providers.ohlcv.alpaca_history.corporate_actions import AlpacaCorporateActions
    from ba2_providers.ohlcv.alpaca_history.live_fetch import AlpacaLiveBars
    first = accounts[0]
    return (AlpacaCorporateActions(first["api_key"], first["api_secret"]),
            AlpacaLiveBars(first["api_key"], first["api_secret"]).daily)


def run_corporate_actions_refresh(now: Optional[datetime] = None) -> Optional[dict]:
    """The daily job body: refresh the corporate actions of the symbols held / traded by ``alpaca`` history accounts.
    ``None`` when no account uses ``alpaca``."""
    from ba2_providers.ohlcv.alpaca_history import corporate_actions
    from zoneinfo import ZoneInfo
    accounts = alpaca_history_accounts()
    if not accounts:
        return None
    now = now or datetime.now(timezone.utc)
    ca_client, raw_fetcher = _refresh_clients(accounts)
    symbols = held_or_traded_symbols([a["id"] for a in accounts])
    return corporate_actions.refresh(symbols, today=now.astimezone(ZoneInfo("America/New_York")).date(), now=now,
                                     ca_client=ca_client, raw_fetcher=raw_fetcher, fmp_rows=_fmp_rows_getter())


def corporate_actions_refresher():
    """``refresher(symbol)`` for ``AlpacaHistoryOHLCVProvider``: on-demand refresh of one symbol (a top-up met a jump)."""
    from ba2_providers.ohlcv.alpaca_history import corporate_actions

    def refresher(symbol: str) -> None:
        accounts = alpaca_history_accounts()
        if not accounts:
            raise RuntimeError(f"{symbol}: no account uses the alpaca history source; cannot refresh corporate actions")
        ca_client, raw_fetcher = _refresh_clients(accounts)
        corporate_actions.refresh_symbol(symbol, ca_client=ca_client, raw_fetcher=raw_fetcher, fmp_rows=_fmp_rows_getter())
    return refresher


def history_job_is_fresh(now: Optional[datetime] = None) -> bool:
    """True when the corporate-actions status would pass the top-up staleness gate right now."""
    from ba2_providers.ohlcv.alpaca_history import corporate_actions
    try:
        corporate_actions.assert_fresh(now or datetime.now(timezone.utc))
        return True
    except corporate_actions.CorporateActionsStale:
        return False
