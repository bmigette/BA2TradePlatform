"""Pure Interactive Brokers (IBKR) mapping rules. NO ``ib_async`` import, no network, no DB.

Everything here is a total, table-driven function over plain strings and numbers, so it can be
tested without a Gateway and lives in the shared package (``packages/common`` is what CI
installs; ``ib_async`` is not). ``ba2_trade_platform/modules/accounts/IBKRAccount.py`` is the only
caller and owns every actual ``ib_async`` object.

What lives here and why it is separate from the account class:

* the IB order-status -> platform ``OrderStatus`` table, **total** over every status string ib_async
  can produce, and loud (``UnknownIBOrderStatus``) on anything else -- a silently-defaulted status
  is how an order ends up "pending" for ever;
* the IB error-code -> ``BrokerOrderErrorReason`` table, and the split between codes that are
  merely informational (farm-connection chatter) and codes that fail an order;
* OCC option-symbol <-> (root, expiry, right, strike) and the OPT-L7 "standard deliverable" test;
* the ``orderRef`` / ``broker_order_id`` encodings that correlate our rows with IB's orders;
* price-increment rounding from IB market rules (``reqMarketRule``);
* the account-summary tag selection and the ``AccountSnapshot`` derivation (what feeds buying power);
* the fractional-eligibility and shortability readings.

Design reference: ``docs/plans/2026-10-03-ibkr-support-design.md``.
"""
from __future__ import annotations

import math
import re
import secrets
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.types import BrokerOrderErrorReason, OptionRight, OrderStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: IB's "unset" sentinel for a double (``sys.float_info.max``): ib_async leaves it in fields the
#: broker never filled. Treated exactly like NaN: no value.
IB_UNSET_DOUBLE = 1.7976931348623157e308

#: Paper-trading account ids start with this; live ids do not.
PAPER_ACCOUNT_PREFIX = "DU"

#: Default API ports, for messages and the smoke script (never used to guess a setting).
IB_PORTS = {7496: "TWS live", 7497: "TWS paper", 4001: "IB Gateway live", 4002: "IB Gateway paper"}

#: Standard US equity option deliverable.
STANDARD_OPTION_MULTIPLIER = 100

#: Reg-T leverage published for a margin account, and the leverage heuristic's threshold: IB's
#: ``BuyingPower`` is ~4x ``AvailableFunds`` on a margin account and ~1x on a cash account.
REGT_MARGIN_MULTIPLIER = 2.0
MARGIN_DETECTION_RATIO = 1.9

#: ``Ticker.shortableShares`` scale (IB tick 46, "shortable"): > 2.5 means at least 1000 shares can
#: be located (the "easy to borrow" analogue), 1.5-2.5 limited, <= 1.5 not available.
EASY_TO_BORROW_THRESHOLD = 2.5


class UnknownIBOrderStatus(ValueError):
    """IB reported an order status this table does not know. Never defaulted."""


class IBKRUnsupportedCurrency(ValueError):
    """The account's base currency is not USD; the platform is USD-only."""


def ib_number(value: Any) -> Optional[float]:
    """A float from an ib_async numeric field, or ``None`` when it is IB's "no value".

    ib_async fills unpublished doubles with NaN (ticks) or ``sys.float_info.max`` (order/contract
    fields). Both mean "absent" and must never be read as a price or quantity.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number >= IB_UNSET_DOUBLE:
        return None
    return number


# ---------------------------------------------------------------------------
# Order status
# ---------------------------------------------------------------------------

#: Every status string ib_async's ``OrderStatus`` constants, ``ActiveStates`` and ``DoneStates``
#: can carry, plus the two the TWS API documents but ib_async does not name as constants
#: (``PendingCancel``, ``ValidationError`` / ``ApiUpdate`` appear in ``ActiveStates``).
#: ``Submitted`` is refined by ``map_ib_status`` when part of the order has filled.
_STATUS_TABLE: Dict[str, OrderStatus] = {
    "ApiPending": OrderStatus.PENDING_NEW,
    "PendingSubmit": OrderStatus.PENDING_NEW,
    "PendingCancel": OrderStatus.PENDING_CANCEL,
    # PreSubmitted: accepted by IB but not yet working (e.g. a stop waiting on its trigger, or
    # an order queued for the open). It is a resting order, which is what ACCEPTED means here.
    "PreSubmitted": OrderStatus.ACCEPTED,
    "Submitted": OrderStatus.ACCEPTED,
    "ApiUpdate": OrderStatus.ACCEPTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELED,
    "ApiCancelled": OrderStatus.CANCELED,
    # Inactive: the order is not working. IB uses it for a rejection (and TWS for an order a
    # user deactivated). Terminal for our purposes; refresh_orders can still revive the row if
    # IB later reports it working, because a refresh applies whatever IB currently says.
    "Inactive": OrderStatus.REJECTED,
    # ValidationError: ib_async sets it when IB sends a WARNING (399 "held until the open", 404 "held
    # while shares are located", 10349, 2100-2199...) while the order stays LIVE and goes on to
    # PreSubmitted/Submitted. It is "working with a warning", never a rejection (review item 1).
    "ValidationError": OrderStatus.PENDING_NEW,
}

IB_STATUS_STRINGS = frozenset(_STATUS_TABLE)

#: IB statuses after which the order can no longer change.
IB_FINAL_STATUSES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})

#: IB statuses that count as the broker having ACKNOWLEDGED a submission (accepted or done).
IB_ACK_STATUSES = frozenset({"PreSubmitted", "Submitted", "Filled", "Cancelled", "ApiCancelled",
                             "Inactive", "ApiUpdate"})


def map_ib_status(status: Optional[str], filled: Optional[float] = None,
                  remaining: Optional[float] = None) -> OrderStatus:
    """IB order status string -> platform ``OrderStatus``. Raises on an unknown string.

    ``Submitted`` is refined: filled > 0 and remaining > 0 is ``PARTIALLY_FILLED``; remaining == 0
    with filled > 0 is ``FILLED`` (IB's final ``Filled`` line can arrive after the last
    ``Submitted`` update, and the quantities are the authority). A cancelled order that traded is
    still ``CANCELED`` -- the caller folds the partial fill back in.

    Raises:
        UnknownIBOrderStatus: for ``None``, an empty string or an unlisted spelling.
    """
    if not status or status not in _STATUS_TABLE:
        raise UnknownIBOrderStatus(
            f"IB reported the order status {status!r}, which is not in the mapping table "
            f"{sorted(_STATUS_TABLE)}; refusing to guess a platform status for it")
    mapped = _STATUS_TABLE[status]
    if status in ("Submitted", "PreSubmitted", "ApiUpdate"):
        done = ib_number(filled) or 0.0
        left = ib_number(remaining)
        if done > 0 and left is not None:
            if left <= 1e-9:
                return OrderStatus.FILLED
            return OrderStatus.PARTIALLY_FILLED
    return mapped


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

#: Codes that are connection-farm chatter / warnings attached to an order. Never an order failure.
INFO_CODES = frozenset(set(range(2100, 2111)) | {2119, 2137, 2150, 2157, 2158})

#: Codes ib_async (wrapper.py ``warningCodes``) treats as WARNINGS: the order stays LIVE and its status
#: becomes ``ValidationError``. Plus EVERY 2100-2199 code (those are warnings too), plus 10349
#: (TIF adjusted by the order preset), which ib_async treats as an error although IB keeps the order.
#: A warning is logged and NEVER fails an order; whether an order failed is decided from its STATUS
#: (see ``IB_REJECTION_STATUSES``), exactly as ib_async decides it.
ORDER_WARNING_CODES = frozenset({105, 110, 165, 321, 329, 399, 404, 434, 492, 10349})
IB_ASYNC_WARNING_RANGE = range(2100, 2200)

#: The statuses that mean an order is dead without having traded (ib_async sets ``Cancelled`` when a
#: non-warning error arrives for a live trade; TWS itself sends ``Inactive`` / ``ApiCancelled``).
IB_REJECTION_STATUSES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})

#: Connectivity state changes.
CONNECTION_LOST_CODES = frozenset({1100, 504, 502, 1300})
CONNECTION_RESTORED_CODES = frozenset({1101, 1102})

#: Market-data availability: "no price", never a stale one.
NO_MARKET_DATA_CODES = frozenset({354, 10089, 10090, 10167, 10168, 10197, 10285})

#: The client id is already in use by another API session.
CLIENT_ID_IN_USE_CODE = 326

#: "Order cancelled" is a status event delivered as an error code.
ORDER_CANCELLED_CODE = 202

#: Cannot find / cannot cancel / cannot modify an order.
ORDER_STATE_CODES = frozenset({103, 104, 105, 135, 136, 161, 10147, 10148})


def error_severity(code: int) -> str:
    """``"info" | "order_warning" | "connection_lost" | "connection_restored" | "market_data" | "cancelled" | "order"``.

    ``"order"`` is the bucket that fails an order when its ``reqId`` is an order id; everything
    else is handled by the connection or data paths.
    """
    if code in INFO_CODES:
        return "info"
    if code in ORDER_WARNING_CODES or code in IB_ASYNC_WARNING_RANGE:
        return "order_warning"
    if code in CONNECTION_LOST_CODES:
        return "connection_lost"
    if code in CONNECTION_RESTORED_CODES:
        return "connection_restored"
    if code in NO_MARKET_DATA_CODES:
        return "market_data"
    if code == ORDER_CANCELLED_CODE:
        return "cancelled"
    return "order"


def classify_ib_error(code: Optional[int], message: Optional[str]) -> BrokerOrderErrorReason:
    """IB error code + text -> the shared ``BrokerOrderErrorReason``.

    A code alone is ambiguous (201 covers margin, shorting and self-cross rejections), so the text
    is inspected exactly as ``AlpacaAccount._classify_order_error`` does. Anything not recognised
    is ``UNKNOWN``; the broker's own words are kept verbatim in the order comment by the caller.
    The texts marked UNVERIFIED in the design doc are matched on distinctive substrings only.
    """
    text = (message or "").lower()
    if code == 200:
        return BrokerOrderErrorReason.INVALID_SYMBOL
    if code == 321 and "read-only" in text:
        return BrokerOrderErrorReason.UNAUTHORIZED
    if code in (502, 504, CLIENT_ID_IN_USE_CODE):
        return BrokerOrderErrorReason.UNKNOWN
    if code in (201, 203, 10148) or code is None:
        if any(word in text for word in ("insufficient", "margin", "buying power", "funds",
                                         "equity with loan")):
            return BrokerOrderErrorReason.INSUFFICIENT_FUNDS
        if "short" in text and any(word in text for word in ("shares", "locate", "borrow",
                                                              "available", "sell")):
            return BrokerOrderErrorReason.INSUFFICIENT_QTY
        if "cross" in text:
            return BrokerOrderErrorReason.WASH_TRADE
        if "stop price" in text and ("market" in text or "current" in text or "last" in text):
            return BrokerOrderErrorReason.STOP_THROUGH_MARKET
    if "not enough" in text and ("shares" in text or "position" in text):
        return BrokerOrderErrorReason.INSUFFICIENT_QTY
    return BrokerOrderErrorReason.UNKNOWN


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

def to_ib_symbol(symbol: str) -> str:
    """Platform ticker -> IB symbol: share classes use a space (``BRK.B`` -> ``BRK B``)."""
    return (symbol or "").strip().upper().replace(".", " ")


def from_ib_symbol(symbol: str) -> str:
    """IB symbol -> platform ticker (``BRK B`` -> ``BRK.B``)."""
    return (symbol or "").strip().upper().replace(" ", ".")


_OCC_RE = re.compile(r"^(?P<root>[A-Z0-9]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
                     r"(?P<right>[CP])(?P<strike>\d{8})$")
_STANDARD_OCC_ROOT_RE = re.compile(r"^[A-Z]{1,6}$")


def parse_occ(occ: str) -> Tuple[str, date, OptionRight, float]:
    """``AAPL260116C00150000`` -> ``("AAPL", date(2026, 1, 16), CALL, 150.0)``. Raises ValueError."""
    match = _OCC_RE.match((occ or "").strip().upper())
    if not match:
        raise ValueError(f"not an OCC option symbol: {occ!r}")
    right = OptionRight.CALL if match["right"] == "C" else OptionRight.PUT
    expiry = date(2000 + int(match["yy"]), int(match["mm"]), int(match["dd"]))
    return match["root"], expiry, right, int(match["strike"]) / 1000.0


def build_occ(root: str, expiry: date, right: OptionRight, strike: float) -> str:
    """Inverse of :func:`parse_occ` (root is not padded, as Alpaca writes it)."""
    char = "C" if right == OptionRight.CALL else "P"
    mills = int(round(float(strike) * 1000))
    return f"{root.strip().upper()}{expiry:%y%m%d}{char}{mills:08d}"


def is_standard_occ_root(root: str, underlying: Optional[str] = None) -> bool:
    """OPT-L7: an adjusted (corporate-action) contract carries a digit or a changed root and does
    not deliver 100 ordinary shares. Same rule the Alpaca/Tasty adapters apply."""
    if not _STANDARD_OCC_ROOT_RE.match(root or ""):
        return False
    return underlying is None or root.upper() == underlying.strip().upper().replace(" ", "")


def occ_from_contract_fields(local_symbol: str, trading_class: str, symbol: str,
                             last_trade: str, right: str, strike: float) -> str:
    """The OCC symbol (unpadded root) of an IB option contract.

    IB's ``localSymbol`` for an option IS the OCC symbol with the root space-padded to six
    characters (``"AAPL  260116C00150000"``), so removing the spaces yields ours. When it is absent
    the fields are used, with ``tradingClass`` as the root (the OCC root, which differs from the
    underlying for e.g. BRK B -> BRKB). Raises ValueError when neither can produce one.
    """
    compact = (local_symbol or "").replace(" ", "").upper()
    if _OCC_RE.match(compact):
        return compact
    root = (trading_class or symbol or "").replace(" ", "").upper()
    expiry = date(int(last_trade[0:4]), int(last_trade[4:6]), int(last_trade[6:8]))
    return build_occ(root, expiry, OptionRight.CALL if right.upper().startswith("C")
                     else OptionRight.PUT, strike)


def ib_expiry_string(expiry: date) -> str:
    """``lastTradeDateOrContractMonth`` for an option: ``YYYYMMDD``."""
    return f"{expiry:%Y%m%d}"


def ib_right(right: OptionRight) -> str:
    return "C" if right == OptionRight.CALL else "P"


# ---------------------------------------------------------------------------
# Order ids
# ---------------------------------------------------------------------------

class OrderRef(NamedTuple):
    account: int
    order: int
    nonce: Optional[str]
    suffix: Optional[str]


_ORDER_REF_RE = re.compile(r"^ba2:(?P<account>\d+):(?P<order>\d+)(?::(?P<nonce>[0-9a-f]{8}))?"
                           r"(?::(?P<suffix>[A-Z]+))?$")


def new_nonce() -> str:
    """A per-row random token. ``tradingorder`` ids are recycled by SQLite and repeat across
    instances, so the id alone must never identify an IB order or execution (review item 7)."""
    return secrets.token_hex(4)


def make_order_ref(account_id: int, order_id: int, suffix: Optional[str] = None,
                   nonce: Optional[str] = None) -> str:
    """Our correlation key on every IB order: ``ba2:<account def>:<order id>:<nonce>[:SL]``."""
    ref = f"ba2:{int(account_id)}:{int(order_id)}"
    if nonce:
        ref += f":{nonce}"
    return f"{ref}:{suffix}" if suffix else ref


def parse_order_ref(ref: Optional[str]) -> Optional[OrderRef]:
    """``OrderRef(account, order, nonce, suffix)`` or ``None`` for a ref that is not ours."""
    match = _ORDER_REF_RE.match(ref or "")
    if not match:
        return None
    return OrderRef(int(match["account"]), int(match["order"]), match["nonce"], match["suffix"])


def format_broker_order_id(perm_id: Optional[int], order_id: Optional[int]) -> str:
    """``str(permId)`` when TWS has assigned one, else ``o<orderId>`` (upgraded on refresh)."""
    if perm_id:
        return str(int(perm_id))
    if order_id is None:
        raise ValueError("an IB order has neither a permId nor an orderId")
    return f"o{int(order_id)}"


def broker_id_matches(broker_order_id: Optional[str], perm_id: Optional[int],
                      order_id: Optional[int]) -> bool:
    """Does a stored ``broker_order_id`` name the order with this permId / orderId?"""
    if not broker_order_id:
        return False
    if perm_id and broker_order_id == str(int(perm_id)):
        return True
    return bool(order_id is not None and broker_order_id == f"o{int(order_id)}")


# ---------------------------------------------------------------------------
# Time in force
# ---------------------------------------------------------------------------

_TIF_TABLE = {"day": "DAY", "gtc": "GTC", "ioc": "IOC", "fok": "FOK", "opg": "OPG", "gtd": "GTD"}
#: TIFs a MARKET order may carry. Anything resting (GTC/GTD) is sent DAY: a market order fills at
#: once or not at all, and the 2026-10-02 TastyTrade incident was a GTC market order.
_MARKET_TIFS = frozenset({"DAY", "IOC", "OPG", "FOK"})


def ib_time_in_force(good_for: Optional[str], *, is_market: bool) -> Tuple[str, Optional[str]]:
    """``(tif, warning)`` for a ``TradingOrder.good_for``.

    * MARKET: DAY unless the row names IOC/FOK/OPG; a resting TIF (GTC/GTD) is replaced by DAY
      with a warning (a market order never rests).
    * everything else with no ``good_for``: **GTC**, the Alpaca default. A protective stop must
      survive the close; a DAY stop would leave the position naked overnight.
    * an unrecognised value raises ``ValueError``: no silent fallback either way.
    """
    key = str(good_for or "").strip().lower()
    warning = None
    if not key:
        return ("DAY" if is_market else "GTC"), None
    if key not in _TIF_TABLE:
        raise ValueError(f"unrecognised time in force {good_for!r}; expected one of "
                         f"{sorted(_TIF_TABLE)}")
    tif = _TIF_TABLE[key]
    if is_market and tif not in _MARKET_TIFS:
        warning = (f"IBKR market order with good_for={good_for!r}: sending DAY "
                   f"(a market order never rests)")
        tif = "DAY"
    return tif, warning


# ---------------------------------------------------------------------------
# Price increments
# ---------------------------------------------------------------------------

def price_increment(rules: Sequence[Tuple[float, float]], price: float) -> float:
    """Increment for ``price`` from IB market-rule rows ``(lowEdge, increment)``.

    The applicable row is the one with the greatest ``lowEdge <= price``. Raises when there are no
    rules: rounding on a guessed tick is how an order trips error 110.
    """
    if not rules:
        raise ValueError("no price-increment rules published for this contract")
    ordered = sorted(rules, key=lambda row: row[0])
    increment = None
    for low, inc in ordered:
        if price >= low:
            increment = inc
    if increment is None:
        increment = ordered[0][1]
    if increment <= 0:
        raise ValueError(f"non-positive price increment {increment!r} in market rule")
    return float(increment)


def round_to_increment(price: float, rules: Sequence[Tuple[float, float]],
                       mode: str = "nearest") -> float:
    """Round ``price`` to IB's tick for that price level (``nearest`` / ``down`` / ``up``)."""
    increment = Decimal(str(price_increment(rules, price)))
    value = Decimal(str(price))
    rounding = {"nearest": ROUND_HALF_UP, "down": ROUND_DOWN, "up": ROUND_UP}[mode]
    steps = (value / increment).to_integral_value(rounding=rounding)
    result = steps * increment
    # Rounding can cross into the next price band whose tick differs: re-check once.
    again = Decimal(str(price_increment(rules, float(result))))
    if again != increment:
        steps = (result / again).to_integral_value(rounding=rounding)
        result = steps * again
    return float(result)


# ---------------------------------------------------------------------------
# Account values
# ---------------------------------------------------------------------------

_CURRENCY_PREFERENCE = ("USD", "BASE", "")


def select_account_values(rows: Iterable[Any], account: str,
                          currency: str = "USD") -> Tuple[Dict[str, float], Dict[str, str]]:
    """Reduce ib_async ``AccountValue`` rows for ONE account to ``(numbers, texts)`` by tag.

    Rows for other accounts are ignored (a multi-account login publishes all of them). A tag that
    exists in several currencies resolves USD, then BASE, then currency-less. Non-numeric values
    (``AccountType``) go to ``texts``.

    Raises:
        IBKRUnsupportedCurrency: when the account's ``NetLiquidation`` is published only in a
            non-USD currency.
    """
    by_tag: Dict[str, Dict[str, str]] = {}
    for row in rows:
        if getattr(row, "account", None) != account:
            continue
        tag = getattr(row, "tag", None)
        if not tag:
            continue
        by_tag.setdefault(tag, {})[getattr(row, "currency", "") or ""] = getattr(row, "value", None)

    numbers: Dict[str, float] = {}
    texts: Dict[str, str] = {}
    for tag, per_currency in by_tag.items():
        chosen = None
        for cur in (currency, "BASE", ""):
            if cur in per_currency:
                chosen = per_currency[cur]
                break
        if chosen is None:
            continue
        try:
            numbers[tag] = float(chosen)
        except (TypeError, ValueError):
            texts[tag] = str(chosen)
    net_liq_currencies = set(by_tag.get("NetLiquidation", {}))
    if net_liq_currencies and not (net_liq_currencies & {currency, "BASE", ""}):
        raise IBKRUnsupportedCurrency(
            f"IBKR account {account} reports NetLiquidation only in {sorted(net_liq_currencies)}; "
            f"the platform is USD-only")
    return numbers, texts


def margin_multiplier_from(numbers: Dict[str, float]) -> float:
    """Reg-T overnight leverage: 2.0 when ``BuyingPower / AvailableFunds >= 1.9`` (a margin
    account), else 1.0. 1.0 whenever it cannot be told (no/zero ``AvailableFunds``): never guess
    leverage."""
    funds = numbers.get("AvailableFunds")
    power = numbers.get("BuyingPower")
    if not funds or funds <= 0 or power is None:
        return 1.0
    return REGT_MARGIN_MULTIPLIER if power / funds >= MARGIN_DETECTION_RATIO else 1.0


def buying_power_components(numbers: Dict[str, float], multiplier: float) -> Dict[str, float]:
    """The candidate Reg-T room figures, each in buying-power dollars (``x multiplier``).

    ``AvailableFunds x m`` can exceed the true overnight Reg-T room, so it is never used alone:
    ``SMA x m`` (the Reg-T special memorandum account, present on margin accounts) and
    ``ExcessLiquidity x m`` (the liquidation cushion) bound it from below. Only tags IBKR published
    appear in the result.
    """
    parts: Dict[str, float] = {}
    if numbers.get("AvailableFunds") is not None:
        parts["available_funds_x_mult"] = numbers["AvailableFunds"] * multiplier
    # SMA is a bound only when published AND positive: a cash account publishes 0 (and a margin one can
    # briefly publish a negative figure after a withdrawal), which would read as "no buying power" and
    # silently block all trading; AvailableFunds and ExcessLiquidity already carry the real limit.
    if numbers.get("SMA") is not None and numbers["SMA"] > 0:
        parts["sma_x_mult"] = numbers["SMA"] * multiplier
    if numbers.get("ExcessLiquidity") is not None:
        parts["excess_liquidity_x_mult"] = numbers["ExcessLiquidity"] * multiplier
    return parts


def buying_power_binding(numbers: Dict[str, float], multiplier: float) -> Optional[str]:
    """Which component of ``conservative_buying_power`` is the smallest (the one that binds)."""
    parts = buying_power_components(numbers, multiplier)
    if not parts:
        return None
    return min(parts, key=lambda k: parts[k])


def conservative_buying_power(numbers: Dict[str, float], multiplier: float) -> Optional[float]:
    """``min(AvailableFunds x m, SMA x m [if published], ExcessLiquidity x m)``.

    ``None`` (never a default) when ``AvailableFunds`` is missing, or when leverage is applied
    (m > 1) and ``ExcessLiquidity`` is missing: the bound cannot be checked, so no figure is given.
    """
    parts = buying_power_components(numbers, multiplier)
    if "available_funds_x_mult" not in parts:
        return None
    if multiplier > 1.0 and "excess_liquidity_x_mult" not in parts:
        return None
    return min(parts.values())


def snapshot_from_account_values(numbers: Dict[str, float], texts: Dict[str, str],
                                 long_market_value: Optional[float],
                                 short_market_value: Optional[float]) -> AccountSnapshot:
    """The platform's broker-agnostic snapshot from IBKR tags. See design doc section 5.

    Nothing is defaulted: a missing tag is ``None``. ``buying_power`` is the conservative minimum of
    ``AvailableFunds``, ``SMA`` and ``ExcessLiquidity``, each times the Reg-T multiplier (never IB's
    own 4x ``BuyingPower``, kept in ``raw``). Short market value is forced negative.
    """
    multiplier = margin_multiplier_from(numbers)
    funds = numbers.get("AvailableFunds")
    net_liq = numbers.get("NetLiquidation")
    short_mv = short_market_value
    if short_mv is not None and short_mv > 0:
        short_mv = -short_mv
    return AccountSnapshot(
        cash=numbers.get("TotalCashValue"),
        equity=net_liq,
        net_liquidation=net_liq,
        buying_power=conservative_buying_power(numbers, multiplier),
        non_marginable_buying_power=numbers.get("SettledCash"),
        option_buying_power=funds,
        margin_multiplier=multiplier if funds is not None else None,
        is_margin_account=multiplier > 1.0,
        long_market_value=long_market_value,
        short_market_value=short_mv,
        pending_transfer_in=None,
        supports_fractional=False,
        raw={"ib_buying_power": numbers.get("BuyingPower"),
             "available_funds": funds,
             "sma": numbers.get("SMA"),
             "bp_components": buying_power_components(numbers, multiplier),
             "bp_binding": buying_power_binding(numbers, multiplier),
             "excess_liquidity": numbers.get("ExcessLiquidity"),
             "init_margin_req": numbers.get("InitMarginReq"),
             "maint_margin_req": numbers.get("MaintMarginReq"),
             "cushion": numbers.get("Cushion"),
             "equity_with_loan": numbers.get("EquityWithLoanValue"),
             "gross_position_value": numbers.get("GrossPositionValue"),
             "account_type": texts.get("AccountType")},
    )


# ---------------------------------------------------------------------------
# Fractional / shortable readings
# ---------------------------------------------------------------------------

def fractionable_from_size(min_size: Any, size_increment: Any) -> Optional[bool]:
    """Tri-state fractional eligibility from ``ContractDetails.minSize`` / ``sizeIncrement``.

    ``True``  -- IB publishes a minimum or increment below one share;
    ``False`` -- it publishes one or more whole shares;
    ``None``  -- neither field is published (or both are junk): the broker did not say.
    """
    values = [v for v in (ib_number(min_size), ib_number(size_increment)) if v is not None and v > 0]
    if not values:
        return None
    return True if min(values) < 1.0 else False


def is_easy_to_borrow(shortable_shares: Any) -> bool:
    """IB tick-46 reading: > 2.5 => shortable and easy to borrow. Unreadable => False."""
    value = ib_number(shortable_shares)
    return value is not None and value > EASY_TO_BORROW_THRESHOLD


def delayed_market_data(market_data_type: Any) -> bool:
    """IB ``marketDataType`` 3 (delayed) / 4 (delayed-frozen) must never reach a sizing decision."""
    try:
        return int(market_data_type) in (3, 4)
    except (TypeError, ValueError):
        return False
