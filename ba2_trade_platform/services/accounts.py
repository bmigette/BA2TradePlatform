"""Account reads for the management API: definition + masked settings, live snapshot, broker positions/orders.

Credentials are NEVER returned: a setting is masked when its definition says ``"secret": True`` / ``"sensitive":
True`` / ``"type": "password"`` OR its key looks like a credential (defence in depth for providers that forgot
the flag)."""
import time
from typing import Any, Callable, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import AccountDefinition, AccountSetting
from ..core.types import OrderStatus
from ..core.utils import get_account_instance_from_id
from ..logger import logger
from ..modules.accounts import get_account_class
from ._common import jsonable, page, parse_enum, require_row, row_dict, utcnow_iso
from .errors import BrokerRefused, BrokerUnreachable, NotFound, ServiceError, Unmeasurable

_SECRET_KEY_PARTS = ("key", "secret", "token", "password", "pass", "pin", "client_id")


def is_secret_setting(key: str, definition: Optional[Dict[str, Any]]) -> bool:
    if definition is not None:
        if ("secret" in definition and definition["secret"]) or \
                ("sensitive" in definition and definition["sensitive"]):
            return True
        if "type" in definition and definition["type"] == "password":
            return True
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def _stored_value(row: AccountSetting) -> Any:
    if row.value_str is not None:
        return row.value_str
    if row.value_float is not None:
        return row.value_float
    return row.value_json if row.value_json else None


def masked_settings(provider: str, rows) -> Dict[str, Any]:
    cls = get_account_class(provider)
    definitions = cls.get_merged_settings_definitions() if cls is not None else {}
    out: Dict[str, Any] = {}
    for row in rows:
        value = _stored_value(row)
        definition = definitions[row.key] if row.key in definitions else None
        if is_secret_setting(row.key, definition):
            out[row.key] = {"set": value is not None and value != ""}
        else:
            out[row.key] = value
    return out


def _definition_dict(session, account: AccountDefinition) -> Dict[str, Any]:
    rows = session.exec(select(AccountSetting).where(AccountSetting.account_id == account.id)).all()
    out = row_dict(account)
    out["settings"] = masked_settings(account.provider, rows)
    return out


def account_definition(account_id: int) -> Dict[str, Any]:
    with get_db() as session:
        return _definition_dict(session, require_row(session, AccountDefinition, account_id, "account"))


def _live_account(account_id: int):
    account = broker_read(account_id, "the account connection", get_account_instance_from_id, account_id)
    if account is None:
        raise NotFound(f"No account with id {account_id}", {"id": account_id})
    return account


#: Class names of the broker SDKs' API errors (alpaca-py APIError, tastytrade, ib_async/IBKR). Matched by NAME
#: along the MRO so this module imports none of the SDKs.
_SDK_ERROR_NAMES = frozenset({"APIError", "TastytradeError", "IBKRError", "RequestError"})
#: A cached balance older than this is not a live figure (Alpaca's get_balance serves its last value forever
#: when its fetches fail).
MAX_BALANCE_AGE_S = 300.0


def broker_read(account_id: int, what: str, fn: Callable, *args, **kwargs):
    """Run ONE broker read and map its failures onto the typed errors. Messages carry the exception CLASS NAME
    only (the text of a broker error can echo request data); the full error goes to the log.

    OSError family (ConnectionError, TimeoutError, DNS ...) -> BrokerUnreachable (503); a broker SDK API error ->
    BrokerRefused (502); ValueError (the platform's loud "unpublished / non-finite figure" refusals) ->
    Unmeasurable (503). Anything else is a defect and propagates."""
    try:
        return fn(*args, **kwargs)
    except ServiceError:
        raise
    except OSError as e:
        logger.warning(f"API: account {account_id} {what}: {type(e).__name__}: {e}")
        raise BrokerUnreachable(f"Account {account_id}: {what} failed: {type(e).__name__}",
                                {"account_id": account_id}) from e
    except ValueError as e:
        logger.warning(f"API: account {account_id} {what}: {type(e).__name__}: {e}")
        raise Unmeasurable(f"Account {account_id}: {what} cannot be measured: {type(e).__name__}",
                           {"account_id": account_id}) from e
    except Exception as e:
        if any(c.__name__ in _SDK_ERROR_NAMES for c in type(e).__mro__):
            logger.warning(f"API: account {account_id} {what}: {type(e).__name__}: {e}")
            raise BrokerRefused(f"Account {account_id}: the broker refused {what}: {type(e).__name__}",
                                {"account_id": account_id}) from e
        raise


def _balance_age_s(account) -> Optional[float]:
    """Age of the account's cached balance, for adapters that keep one (Alpaca: ``_balance_cache`` /
    ``_balance_cache_time``); None when the adapter exposes none (age unknown, not zero)."""
    cached = getattr(account, "_balance_cache", None)
    stamp = getattr(account, "_balance_cache_time", None)
    if cached is None or stamp is None:
        return None
    return max(0.0, time.time() - stamp)


def account_snapshot(account_id: int) -> Dict[str, Any]:
    """Live balance / equity / cash / buying power / margin / exposure of one account.

    Built from ONE ``get_account_snapshot()`` and ONE ``describe_capital()``. A figure the broker did not publish
    raises ``Unmeasurable`` naming it; it is never reported as 0 or null. The margin-only terms of
    ``describe_capital`` are legitimately ``None`` with margin OFF (that method's contract); ``margin_enabled``
    says so. Option buying power and the long/short market values may stay null when the broker publishes none.
    ``balance_age_s`` is the age of the adapter's cached balance (null when it keeps none); older than
    MAX_BALANCE_AGE_S the balance is a stale cache served after failed fetches and the call is Unmeasurable."""
    account = _live_account(account_id)
    snap = broker_read(account_id, "the account snapshot", account.get_account_snapshot)
    capital = broker_read(account_id, "the capital description", account.describe_capital)
    required = {"equity": snap.equity, "cash": snap.cash, "buying_power": snap.buying_power,
                "balance": capital["balance"], "tradable_balance": capital["tradable_balance"]}
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise Unmeasurable(f"Account {account_id}: the broker published no value for {missing}",
                           {"account_id": account_id, "missing": missing})
    age = _balance_age_s(account)
    if age is not None and age > MAX_BALANCE_AGE_S:
        raise Unmeasurable(f"Account {account_id}: the balance is a stale cache ({age:.0f}s old, limit "
                           f"{MAX_BALANCE_AGE_S:.0f}s): the broker could not be read",
                           {"account_id": account_id, "balance_age_s": age})
    # pending_entries is also available with margin OFF straight from the order store; describe_capital
    # skips it there to keep backtests cheap. Private method on purpose -- no public equivalent exists.
    pending = capital["pending_entries"]
    if pending is None:
        pending = broker_read(account_id, "the pending-entry total", account._pending_stock_entry_notional)
    return {
        "as_of": utcnow_iso(),
        "balance_age_s": age,
        "balance": capital["balance"],
        "equity": snap.equity,
        "net_liquidation": snap.net_liquidation,
        "cash": snap.cash,
        "buying_power": snap.buying_power,
        "option_buying_power": snap.option_buying_power,
        "long_market_value": snap.long_market_value,
        "short_market_value": snap.short_market_value,
        "margin_enabled": capital["margin_enabled"],
        "margin_factor": capital["margin_factor"],
        "broker_multiplier": capital["broker_multiplier"],
        "effective_factor": capital["effective_factor"],
        "tradable_balance": capital["tradable_balance"],
        "gross_exposure": capital["gross_exposure"],
        "pending_entries": pending,
        "headroom": capital["headroom"],
    }


def account_detail(account_id: int) -> Dict[str, Any]:
    out = account_definition(account_id)
    out["snapshot"] = account_snapshot(account_id)
    return out


def list_accounts(*, limit: int, offset: int) -> Dict[str, Any]:
    """Definitions plus a snapshot SUMMARY each. One account's failure, whatever it is, does not fail the list:
    that row carries ``snapshot: null`` and a ``snapshot_error`` naming the error code (never a silent zero)."""
    with get_db() as session:
        result = page(session, select(AccountDefinition).order_by(AccountDefinition.id),
                      limit=limit, offset=offset, convert=lambda a: _definition_dict(session, a))
    for item in result["items"]:
        try:
            snap = account_snapshot(item["id"])
            item["snapshot"] = {k: snap[k] for k in ("as_of", "balance", "equity", "cash",
                                                     "buying_power", "tradable_balance", "headroom")}
            item["snapshot_error"] = None
        except Exception as e:  # noqa: BLE001 -- deliberate: one account's failure must not fail the list
            logger.error(f"API: account {item['id']} snapshot failed in the list: {type(e).__name__}", exc_info=True)
            item["snapshot"] = None
            code = e.code if isinstance(e, ServiceError) else "internal"
            item["snapshot_error"] = {"error": code, "message": type(e).__name__}
    return result


def broker_positions(account_id: int) -> Dict[str, Any]:
    """Broker-side positions. ``get_positions()`` is tri-state (``None`` = the fetch failed, ``[]`` = flat):
    a failed fetch is ``BrokerUnreachable``, never an empty list."""
    account = _live_account(account_id)
    positions = broker_read(account_id, "the positions fetch", account.get_positions)
    if positions is None:
        raise BrokerUnreachable(f"Account {account_id}: the broker positions fetch failed",
                                {"account_id": account_id})
    return {"as_of": utcnow_iso(), "items": jsonable(list(positions))}


_BROKER_SIDE_STATUSES = (OrderStatus.ALL, OrderStatus.OPEN, OrderStatus.CLOSED)


def _status_of(order) -> Optional[str]:
    raw = order["status"] if isinstance(order, dict) else getattr(order, "status", None)
    return None if raw is None else getattr(raw, "value", raw)


def broker_orders(account_id: int, status: Optional[str]) -> Dict[str, Any]:
    """Broker-side orders; for this endpoint BEST EFFORT per adapter.

    * Alpaca: read through ``_fetch_raw_alpaca_orders(full_history=True)``, which RAISES on a failed fetch
      (``get_orders`` would turn it into ``[]``): ``fetch_ok`` is true, an empty list is a verified empty book,
      and a failure is BrokerRefused / BrokerUnreachable.
    * Other adapters (IBKR, TastyTrade) only offer ``get_orders``, which swallows errors into ``[]``: an empty
      answer comes back with ``fetch_ok: false`` and a note, never as "no orders"; a non-empty answer proves the
      fetch worked.
    ``status`` is validated against OrderStatus; ALL / OPEN / CLOSED are asked of the broker, any other value is
    filtered here."""
    wanted = None if status is None else parse_enum(OrderStatus, status, "status")
    account = _live_account(account_id)
    broker_status = wanted if wanted in _BROKER_SIDE_STATUSES else OrderStatus.ALL
    if hasattr(account, "_fetch_raw_alpaca_orders") and hasattr(account, "alpaca_order_to_tradingorder"):
        if getattr(account, "client", True) is None:
            raise BrokerUnreachable(f"Account {account_id}: not authenticated with the broker",
                                    {"account_id": account_id})
        raw = broker_read(account_id, "the orders fetch", account._fetch_raw_alpaca_orders,
                          status=broker_status, fetch_all=False, full_history=True)
        orders = [account.alpaca_order_to_tradingorder(o) for o in raw]
        extra: Dict[str, Any] = {"fetch_ok": True, "best_effort": False}
    else:
        orders = broker_read(account_id, "the orders fetch", account.get_orders,
                             None if wanted is None else broker_status)
        if orders is None:
            raise BrokerUnreachable(f"Account {account_id}: the broker orders fetch failed",
                                    {"account_id": account_id})
        orders = list(orders)
        extra = {"fetch_ok": bool(orders), "best_effort": True}
        if not orders:
            extra["note"] = ("This broker adapter reports a failed fetch as an empty list, so an empty answer "
                             "cannot be told from 'no orders'.")
    if wanted is not None and wanted not in _BROKER_SIDE_STATUSES:
        orders = [o for o in orders if _status_of(o) == wanted.value]
    return {"as_of": utcnow_iso(), "items": jsonable(orders), **extra}
