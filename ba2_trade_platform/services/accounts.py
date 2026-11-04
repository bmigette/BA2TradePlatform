"""Account reads for the management API: definition + masked settings, live snapshot, broker positions/orders.

Credentials are NEVER returned: a setting is masked when its definition says ``"secret": True`` / ``"sensitive":
True`` / ``"type": "password"`` OR its key looks like a credential (defence in depth for providers that forgot
the flag)."""
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import AccountDefinition, AccountSetting
from ..core.utils import get_account_instance_from_id
from ..modules.accounts import get_account_class
from ._common import jsonable, page, require_row, row_dict, utcnow_iso
from .errors import BrokerUnreachable, NotFound, ServiceError, Unmeasurable

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
    account = get_account_instance_from_id(account_id)
    if account is None:
        raise NotFound(f"No account with id {account_id}", {"id": account_id})
    return account


def account_snapshot(account_id: int) -> Dict[str, Any]:
    """Live balance / equity / cash / buying power / margin / exposure of one account.

    Built from ONE ``get_account_snapshot()`` and ONE ``describe_capital()``. A figure the broker did not publish
    raises ``Unmeasurable`` naming it; it is never reported as 0 or null. The margin-only terms of
    ``describe_capital`` are legitimately ``None`` with margin OFF (that method's contract); ``margin_enabled``
    says so. Option buying power and the long/short market values may stay null when the broker publishes none."""
    account = _live_account(account_id)
    try:
        snap = account.get_account_snapshot()
        capital = account.describe_capital()
    except ValueError as e:
        raise Unmeasurable(f"Account {account_id}: {e}", {"account_id": account_id}) from e
    required = {"equity": snap.equity, "cash": snap.cash, "buying_power": snap.buying_power,
                "balance": capital["balance"], "tradable_balance": capital["tradable_balance"]}
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise Unmeasurable(f"Account {account_id}: the broker published no value for {missing}",
                           {"account_id": account_id, "missing": missing})
    # pending_entries is also available with margin OFF straight from the order store; describe_capital
    # skips it there to keep backtests cheap. Private method on purpose -- no public equivalent exists.
    pending = capital["pending_entries"]
    if pending is None:
        pending = account._pending_stock_entry_notional()
    return {
        "as_of": utcnow_iso(),
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
    """Definitions plus a snapshot SUMMARY each. One account's broker failure does not fail the list: that row
    carries ``snapshot: null`` and a ``snapshot_error`` envelope (explicit, never a silent zero)."""
    with get_db() as session:
        result = page(session, select(AccountDefinition).order_by(AccountDefinition.id),
                      limit=limit, offset=offset, convert=lambda a: _definition_dict(session, a))
    for item in result["items"]:
        try:
            snap = account_snapshot(item["id"])
            item["snapshot"] = {k: snap[k] for k in ("as_of", "balance", "equity", "cash",
                                                     "buying_power", "tradable_balance", "headroom")}
            item["snapshot_error"] = None
        except (ServiceError, ValueError) as e:
            item["snapshot"] = None
            code = e.code if isinstance(e, ServiceError) else "unmeasurable"
            item["snapshot_error"] = {"error": code, "message": str(e)}
    return result


def broker_positions(account_id: int) -> Dict[str, Any]:
    """Broker-side positions. ``get_positions()`` is tri-state (``None`` = the fetch failed, ``[]`` = flat):
    a failed fetch is ``BrokerUnreachable``, never an empty list."""
    account = _live_account(account_id)
    positions = account.get_positions()
    if positions is None:
        raise BrokerUnreachable(f"Account {account_id}: the broker positions fetch failed",
                                {"account_id": account_id})
    return {"as_of": utcnow_iso(), "items": jsonable(list(positions))}


def broker_orders(account_id: int, status: Optional[str]) -> Dict[str, Any]:
    account = _live_account(account_id)
    orders = account.get_orders(status)
    if orders is None:
        raise BrokerUnreachable(f"Account {account_id}: the broker orders fetch failed",
                                {"account_id": account_id})
    return {"as_of": utcnow_iso(), "items": jsonable(list(orders))}
