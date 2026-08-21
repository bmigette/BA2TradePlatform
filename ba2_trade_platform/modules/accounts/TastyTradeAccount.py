import asyncio
import threading
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
from concurrent.futures import TimeoutError as FutureTimeoutError

from tastytrade.order import InstrumentType as TTInstrumentType
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce
from tastytrade.order import OrderStatus as TTOrderStatus
from tastytrade.order import OrderType as TTOrderType

from sqlmodel import select

from ...logger import logger
from ...core.db import add_instance, get_db, get_instance, update_instance, InstanceNotFound
from ...core.models import Position, TradingOrder
from ...core.types import OrderDirection, OrderStatus
from ...core.types import OrderType as CoreOrderType
from ...core.account_types import (
    AccountSnapshot, CashTransfer, MarginInfo, OrderImpact,
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL,
    MARGIN_SOURCE_DEFAULT, MARGIN_SOURCE_POSITION,
)
from ...core.interfaces import AccountInterface
from ...core.models import Transaction


class TastyTradeAccount(AccountInterface):
    """
    Trading account interface for TastyTrade brokerage.

    Uses the tastytrade Python SDK 12.x (async) driven from a persistent
    background event loop, so the httpx client's connections are never invalidated
    by a closed loop.

    ``supports_trading`` is deliberately NOT pinned here -- it is inherited as True
    from AccountInterface. It is read from the CLASS at ui/pages/settings.py:1435 and
    from the INSTANCE at core/TradeManager.py:921 and :1223; a local pin is exactly
    what made those reads disagree.

    Supported: equity market/limit/stop/stop-limit submission, cancellation, order and
    position refresh, order preview (dry run), account snapshot, cash transfers and
    per-symbol margin metadata.

    Out of scope (explicitly unsupported below, never silently half-working):
    ``modify_order``, TP/SL adjustment, complex orders and OptionsAccountInterface.

    TWO SDK TRAPS:
      * ``Account.place_order``'s ``dry_run`` DEFAULTS TO True (account.py:877) --
        every real submission passes ``dry_run=False`` explicitly.
      * ``NewOrder.price_effect`` is a computed field derived from the SIGN of
        ``price`` (order.py:264-276) -- never set it by hand.
    """

    #: TastyTrade protective-leg management is out of scope here, so the adjust_*
    #: methods raise NotImplementedError. Declaring it makes
    #: ``AccountInterface.submit_order`` refuse a ``tp_price``/``sl_price`` request
    #: BEFORE it opens anything, instead of filling the entry and then logging that
    #: the stop could not be placed -- which handed the caller a live, unprotected
    #: position as the success value.
    supports_protective_legs = False

    def __init__(self, id: int):
        super().__init__(id)
        self._session = None
        self._account = None
        self._authentication_error = None
        # Persistent event loop for this account instance so the httpx
        # async client's connections are never invalidated by a closed loop.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

        try:
            required_settings = ["client_secret", "refresh_token", "account_id"]
            missing = [k for k in required_settings if k not in self.settings or self.settings[k] is None]
            if missing:
                error_msg = f"Missing required settings: {', '.join(missing)}"
                self._authentication_error = error_msg
                logger.error(f"TastyTradeAccount {id}: {error_msg}")
                raise ValueError(error_msg)

            self._connect()
            logger.info(f"TastyTrade session initialized for account {id}.")
        except Exception as e:
            self._authentication_error = str(e)
            logger.error(f"Failed to initialize TastyTrade session for account {id}: {e}", exc_info=True)
            raise

    #: Wall-clock budget for ONE SDK call. The old hardcoded 30s was routinely
    #: exceeded by paginated calls (a full order history, a year of transactions),
    #: and because every caller wraps _run_async in `except Exception: return []`,
    #: a timeout surfaced as "the broker has no data" instead of as a failure.
    _ASYNC_TIMEOUT_SECONDS = 180

    def _run_async(self, coro, timeout: Optional[float] = None):
        """Run an async coroutine on this account's persistent event loop.

        Args:
            coro: the coroutine to drive.
            timeout: seconds to wait; ``None`` uses ``_ASYNC_TIMEOUT_SECONDS``.

        Raises:
            TimeoutError: naming the account and the budget, so the caller's
                ``logger.error`` says WHY it produced nothing. The pending future is
                cancelled first so the loop does not keep the request alive.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        limit = self._ASYNC_TIMEOUT_SECONDS if timeout is None else timeout
        try:
            return future.result(timeout=limit)
        except FutureTimeoutError as e:
            future.cancel()
            raise TimeoutError(
                f"[Account {self.id}] TastyTrade call timed out after {limit}s"
            ) from e

    def _is_sandbox(self) -> bool:
        """Whether this account targets TastyTrade's sandbox (certification) API.

        Read through ``get_setting_with_interface_default`` rather than
        ``self.settings.get('is_test', False)``. The settings property seeds every
        DECLARED key to ``None``, so ``.get(key, default)`` never returns the default
        for a never-saved key; and a legacy row holding the literal string ``"None"``
        (the str(None) write bug) coerces to ``True`` under ``bool()``, which would
        silently point a PRODUCTION account at the sandbox.
        ``get_setting_with_interface_default`` treats ``"None"`` as unset.
        """
        return bool(self.get_setting_with_interface_default("is_test", log_warning=False))

    def _connect(self):
        """Establish connection to TastyTrade API."""
        from tastytrade.session import Session as TastySession
        from tastytrade.account import Account as TastyAccount

        is_test = self._is_sandbox()

        # Create session on the persistent loop so httpx client binds to it
        self._session = self._run_async(self._create_session_async(
            provider_secret=self.settings["client_secret"],
            refresh_token=self.settings["refresh_token"],
            is_test=is_test,
        ))

        # Fetch the specific account by account number
        target_id = self.settings["account_id"]
        self._account = self._run_async(TastyAccount.get(self._session, account_number=target_id))

        if self._account is None:
            raise ValueError(f"Account {target_id} not found on TastyTrade.")

    @staticmethod
    async def _create_session_async(provider_secret, refresh_token, is_test):
        """Create TastyTrade session on the async loop so httpx client binds correctly."""
        from tastytrade.session import Session as TastySession
        return TastySession(
            provider_secret=provider_secret,
            refresh_token=refresh_token,
            is_test=is_test,
        )

    def _check_authentication(self) -> bool:
        if self._session is None or self._account is None:
            logger.error(f"TastyTradeAccount {self.id}: Not authenticated - {self._authentication_error}")
            return False
        return True

    @staticmethod
    def get_settings_definitions() -> Dict[str, Any]:
        return {
            "client_secret": {
                "type": "str",
                "required": True,
                "description": "OAuth provider secret (mapped to provider_secret in SDK)"
            },
            "refresh_token": {
                "type": "str",
                "required": True,
                "description": "OAuth refresh token"
            },
            "account_id": {
                "type": "str",
                "required": True,
                "description": "TastyTrade account number"
            },
            "is_test": {
                "type": "bool",
                "required": False,
                "default": False,
                "description": "Use sandbox/test environment"
            }
        }

    def get_balance(self) -> Optional[float]:
        if not self._check_authentication():
            return None
        try:
            balances = self._run_async(self._account.get_balances(self._session))
            return float(balances.net_liquidating_value)
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting balance: {e}", exc_info=True)
            return None

    def get_account_info(self) -> Dict[str, Any]:
        if not self._check_authentication():
            return {}
        try:
            balances = self._run_async(self._account.get_balances(self._session))
            return {
                "account_number": self._account.account_number,
                "account_type": self._account.account_type_name,
                # `buying_power` MUST be present and MUST come first among the
                # spendable-balance keys: MarketExpertInterface._get_actual_available_balance
                # probes buying_power -> cash -> cash_balance -> equity_buying_power and
                # takes the FIRST hit. Without it the probe fell through to cash_balance
                # and margin buying power was silently ignored.
                "buying_power": float(balances.equity_buying_power),
                "net_liquidating_value": float(balances.net_liquidating_value),
                "cash_balance": float(balances.cash_balance),
                "equity_buying_power": float(balances.equity_buying_power),
                "derivative_buying_power": float(balances.derivative_buying_power),
                "long_equity_value": float(balances.long_equity_value),
                "short_equity_value": float(balances.short_equity_value),
                "margin_equity": float(balances.margin_equity),
                "maintenance_requirement": float(balances.maintenance_requirement),
                "pending_cash": float(balances.pending_cash),
                "cash_available_to_withdraw": float(balances.cash_available_to_withdraw),
                "supports_trading": self.supports_trading,
            }
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting account info: {e}", exc_info=True)
            return {}

    def _is_margin_account(self) -> bool:
        """Whether this is a Reg-T margin account (``Account.margin_or_cash``)."""
        return str(getattr(self._account, "margin_or_cash", "") or "").strip().lower() == "margin"

    def get_account_snapshot(self) -> AccountSnapshot:
        """Broker-agnostic cash / equity / buying-power view of this account.

        Overrides the base tolerant probe: TastyTrade returns a typed AccountBalance,
        so every field is read directly.

        Never fabricates a number -- a field TastyTrade did not supply stays ``None``,
        and a failed fetch returns an ALL-NONE snapshot, which is a legitimate "the
        broker told us nothing" result the caller must refuse to plan on. Zeros would
        be indistinguishable from a real flat account.

        ``margin_multiplier`` is the Reg-T leverage the allocation engine uses as its
        conservative ``default_bp_factor``: 2.0 for a margin account, 1.0 for cash.
        """
        if not self._check_authentication():
            return AccountSnapshot()
        try:
            balances = self._run_async(self._account.get_balances(self._session))
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting account snapshot: {e}", exc_info=True)
            return AccountSnapshot()

        def _num(name):
            value = getattr(balances, name, None)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        is_margin = self._is_margin_account()
        net_liquidation = _num("net_liquidating_value")
        return AccountSnapshot(
            cash=_num("cash_balance"),
            equity=net_liquidation,
            net_liquidation=net_liquidation,
            buying_power=_num("equity_buying_power"),
            non_marginable_buying_power=_num("cash_available_to_withdraw"),
            margin_multiplier=2.0 if is_margin else 1.0,
            is_margin_account=is_margin,
            long_market_value=_num("long_equity_value"),
            # NEGATED ON PURPOSE. AccountSnapshot pins short_market_value as NEGATIVE
            # while shorts are held (the Alpaca convention), but TastyTrade's
            # short-equity-value is a POSITIVE MAGNITUDE. Passing it through unchanged
            # makes gross exposure broker-dependent: long + abs(short) and long - short
            # disagree, and no fixture with a zero short can tell the difference.
            short_market_value=(
                -_num("short_equity_value")
                if _num("short_equity_value") is not None
                else None
            ),
            # TastyTrade's pending_cash is SIGNED (positive = incoming); it is reported
            # as-is rather than clamped, so the caller sees what the broker said.
            pending_transfer_in=_num("pending_cash"),
            supports_fractional=True,
            raw={
                "margin_equity": _num("margin_equity"),
                "maintenance_requirement": _num("maintenance_requirement"),
                "derivative_buying_power": _num("derivative_buying_power"),
                "margin_or_cash": getattr(self._account, "margin_or_cash", None),
            },
        )

    def get_positions(self) -> Optional[List[Position]]:
        """Current EQUITY positions.

        Returns:
            Optional[List[Position]]: the equity book, ``[]`` when the account is
            genuinely flat, and ``None`` when the FETCH ITSELF FAILED. That
            distinction is load-bearing: ``reconcile_externally_closed_transactions``
            and the overview position comparison treat an empty list as "the broker
            confirmed it holds nothing", and a transient outage swallowed to ``[]``
            once mass-closed 8 real open transactions (2026-07-03).

        EQUITY_OPTION rows are excluded: their market value is multiplier-scaled
        (x100), so including them would fold option notionals into equity weights.
        Option exposure is read through OptionsAccountInterface, not here.
        """
        if not self._check_authentication():
            return None
        try:
            tt_positions = self._run_async(
                self._account.get_positions(self._session, include_marks=True))
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting positions: {e}", exc_info=True)
            return None

        positions = []
        skipped_non_equity = 0
        for pos in tt_positions:
            if pos.instrument_type != TTInstrumentType.EQUITY:
                skipped_non_equity += 1
                continue

            qty = float(pos.quantity)
            if qty == 0:
                continue

            multiplier = int(pos.multiplier) if pos.multiplier else 1
            avg_price = float(pos.average_open_price)
            close_price = float(pos.close_price) if pos.close_price is not None else None
            # mark_price is per-share (`mark` is the whole position); fall back to the
            # previous close, then to the entry price. Never a fabricated constant.
            if pos.mark_price is not None:
                current = float(pos.mark_price)
            elif close_price is not None:
                current = close_price
            else:
                current = avg_price

            abs_qty = abs(qty)
            cost_basis = avg_price * abs_qty * multiplier
            market_val = current * abs_qty * multiplier
            unrealized_pl = market_val - cost_basis
            unrealized_plpc = (unrealized_pl / cost_basis) if cost_basis else 0.0

            # INTRADAY = move since the previous close, on the position still OPEN.
            # (`realized_day_gain` is CLOSED-out P&L for the day -- a different number,
            # and what this used to report.)
            lastday_price = close_price if close_price is not None else current
            change_today = ((current - lastday_price) / lastday_price) if lastday_price else 0.0
            intraday_pl = (current - lastday_price) * abs_qty * multiplier
            lastday_value = lastday_price * abs_qty * multiplier
            intraday_plpc = (intraday_pl / lastday_value) if lastday_value else 0.0

            side = OrderDirection.BUY if pos.quantity_direction == "Long" else OrderDirection.SELL

            positions.append(Position(
                asset_class="Equity",
                avg_entry_price=avg_price,
                avg_entry_swap_rate=None,
                change_today=change_today,
                cost_basis=cost_basis,
                current_price=current,
                exchange="",
                lastday_price=lastday_price,
                market_value=market_val,
                qty=abs_qty,
                qty_available=abs_qty,
                side=side,
                swap_rate=None,
                symbol=pos.symbol,
                unrealized_intraday_pl=intraday_pl,
                unrealized_intraday_plpc=intraday_plpc,
                unrealized_pl=unrealized_pl,
                unrealized_plpc=unrealized_plpc,
            ))

        logger.debug(
            f"[Account {self.id}] Retrieved {len(positions)} equity positions from "
            f"TastyTrade ({skipped_non_equity} non-equity rows skipped)")
        return positions

    #: TastyTrade order status -> platform OrderStatus. TastyTrade's enum lives in
    #: tastytrade.order (imported here as TTOrderStatus); the platform's is
    #: ba2_common.core.types.OrderStatus. Keep this the ONE place they meet.
    _TT_STATUS_MAP = {
        TTOrderStatus.RECEIVED: OrderStatus.NEW,
        TTOrderStatus.ROUTED: OrderStatus.NEW,
        TTOrderStatus.IN_FLIGHT: OrderStatus.PENDING_NEW,
        TTOrderStatus.LIVE: OrderStatus.ACCEPTED,
        TTOrderStatus.CONTINGENT: OrderStatus.WAITING_TRIGGER,
        TTOrderStatus.FILLED: OrderStatus.FILLED,
        TTOrderStatus.CANCELLED: OrderStatus.CANCELED,
        TTOrderStatus.CANCEL_REQUESTED: OrderStatus.PENDING_CANCEL,
        TTOrderStatus.REPLACE_REQUESTED: OrderStatus.PENDING_REPLACE,
        TTOrderStatus.EXPIRED: OrderStatus.EXPIRED,
        TTOrderStatus.REJECTED: OrderStatus.REJECTED,
        TTOrderStatus.REMOVED: OrderStatus.CANCELED,
        TTOrderStatus.PARTIALLY_REMOVED: OrderStatus.CANCELED,
    }

    #: TastyTrade statuses that mean "still working at the broker".
    #: CANCEL_REQUESTED and REPLACE_REQUESTED belong here: the broker has ACCEPTED the
    #: request but the order is still live and its quantity is not yet released, so a
    #: "what is still working?" query that omitted them missed exactly the orders a
    #: dependent replacement is waiting on.
    _TT_OPEN_STATUSES = (TTOrderStatus.RECEIVED, TTOrderStatus.ROUTED,
                         TTOrderStatus.IN_FLIGHT, TTOrderStatus.LIVE,
                         TTOrderStatus.CONTINGENT, TTOrderStatus.CANCEL_REQUESTED,
                         TTOrderStatus.REPLACE_REQUESTED)

    #: TastyTrade statuses that mean "done, one way or another".
    #: PARTIALLY_REMOVED is terminal (it maps to CANCELED, like REMOVED).
    _TT_CLOSED_STATUSES = (TTOrderStatus.FILLED, TTOrderStatus.CANCELLED,
                           TTOrderStatus.EXPIRED, TTOrderStatus.REJECTED,
                           TTOrderStatus.REMOVED, TTOrderStatus.PARTIALLY_REMOVED)

    @classmethod
    def _tt_statuses_for(cls, status) -> Optional[List["TTOrderStatus"]]:
        """Translate a platform status filter into the SDK's ``statuses=[...]`` list.

        Returns ``None`` for "no filter" -- i.e. ``status`` is ``None`` or
        ``OrderStatus.ALL``. The SDK filters server-side via the ``status[]`` query
        param, so the argument must never be silently dropped (which is what
        ``get_orders`` used to do, returning every order ever placed).

        Raises:
            ValueError: when ``status`` has no TastyTrade equivalent. Returning the
                "no filter" ``None`` for an unmapped status was strictly worse than
                failing: combined with ``page_offset=None`` it walked EVERY order ever
                placed and handed the caller the lot as though it were the filtered
                answer. A filter this adapter cannot express is a caller bug.
        """
        if status is None or status == OrderStatus.ALL:
            return None
        if status == OrderStatus.OPEN:
            return list(cls._TT_OPEN_STATUSES)
        if status == OrderStatus.CLOSED:
            return list(cls._TT_CLOSED_STATUSES)
        matches = [tt for tt, core in cls._TT_STATUS_MAP.items() if core == status]
        if not matches:
            raise ValueError(
                f"No TastyTrade order status maps to {status.value!r}; refusing to "
                f"fetch unfiltered (that would return every order ever placed)")
        return matches

    @classmethod
    def _map_order_status(cls, tt_status) -> OrderStatus:
        """TastyTrade order status -> platform OrderStatus. UNKNOWN for anything unmapped."""
        if tt_status is None:
            return OrderStatus.UNKNOWN
        try:
            return cls._TT_STATUS_MAP[TTOrderStatus(tt_status)]
        except (ValueError, KeyError):
            logger.warning(f"Unmapped TastyTrade order status {tt_status!r}; recording UNKNOWN")
            return OrderStatus.UNKNOWN

    @staticmethod
    def _map_order_type(tt_type, side: OrderDirection) -> CoreOrderType:
        """Map a TastyTrade order type onto our DIRECTIONAL OrderType.

        TastyTrade's ``order_type`` is non-directional (Market / Limit / Stop /
        Stop Limit / Marketable Limit / Notional Market); ours is directional for the
        limit and stop variants, so it must be combined with the side. Unknown types
        fall back to MARKET, exactly as ``AlpacaAccount._map_order_type`` does.
        """
        if tt_type is None:
            return CoreOrderType.MARKET
        value = str(getattr(tt_type, "value", tt_type))
        is_buy = side == OrderDirection.BUY
        if value in ("Market", "Notional Market"):
            return CoreOrderType.MARKET
        if value in ("Limit", "Marketable Limit"):
            return CoreOrderType.BUY_LIMIT if is_buy else CoreOrderType.SELL_LIMIT
        if value == "Stop":
            return CoreOrderType.BUY_STOP if is_buy else CoreOrderType.SELL_STOP
        if value == "Stop Limit":
            return CoreOrderType.BUY_STOP_LIMIT if is_buy else CoreOrderType.SELL_STOP_LIMIT
        logger.warning(f"Unmapped TastyTrade order type {value!r}; recording MARKET")
        return CoreOrderType.MARKET

    @staticmethod
    def _side_from_legs(legs) -> Optional[OrderDirection]:
        """Derive the order side from its legs.

        A ``PlacedOrder`` has NO top-level side: it lives on each leg's
        ``OrderAction`` ('Buy to Open', 'Sell to Close', ...). Equity orders here are
        always single-leg, so the first leg decides. Returns ``None`` when no leg
        yields a side -- the caller must skip the order rather than guess, because a
        fabricated side puts the row on the wrong side of the book.
        """
        for leg in legs or []:
            raw = getattr(leg, "action", None)
            action = str(getattr(raw, "value", raw) or "")
            if action.startswith("Buy"):
                return OrderDirection.BUY
            if action.startswith("Sell"):
                return OrderDirection.SELL
        return None

    @staticmethod
    def _fills_summary(legs):
        """(total filled quantity, quantity-weighted average fill price) across legs.

        Returns ``(0.0, None)`` when nothing has filled -- never a fabricated price.
        """
        total_qty = 0.0
        total_notional = 0.0
        for leg in legs or []:
            for fill in (getattr(leg, "fills", None) or []):
                quantity = float(fill.quantity)
                total_qty += quantity
                total_notional += quantity * float(fill.fill_price)
        if total_qty <= 0:
            return 0.0, None
        return total_qty, total_notional / total_qty

    def tastytrade_order_to_tradingorder(self, order) -> Optional[TradingOrder]:
        """Convert a tastytrade ``PlacedOrder`` into an UNSAVED TradingOrder.

        Returns ``None`` when the side cannot be determined from the legs.
        ``PlacedOrder.price`` is SIGNED (negative = debit) but ``limit_price`` is a
        plain price, so it is stored as an absolute value. A dry-run order has
        ``id == -1``, which is not a broker id and is stored as ``None``.
        """
        side = self._side_from_legs(getattr(order, "legs", None))
        if side is None:
            logger.error(
                f"[Account {self.id}] Cannot determine side for TastyTrade order "
                f"{getattr(order, 'id', None)} -- skipping")
            return None

        filled_qty, avg_fill_price = self._fills_summary(getattr(order, "legs", None))
        raw_id = getattr(order, "id", None)
        size = getattr(order, "size", None)
        price = getattr(order, "price", None)
        stop_trigger = getattr(order, "stop_trigger", None)
        tif = getattr(order, "time_in_force", None)

        return TradingOrder(
            broker_order_id=str(raw_id) if raw_id not in (None, -1) else None,
            symbol=getattr(order, "underlying_symbol", None),
            quantity=float(size) if size is not None else filled_qty,
            side=side,
            order_type=self._map_order_type(getattr(order, "order_type", None), side),
            good_for=(str(getattr(tif, "value", tif)) if tif is not None else None),
            limit_price=abs(float(price)) if price is not None else None,
            stop_price=float(stop_trigger) if stop_trigger is not None else None,
            status=self._map_order_status(getattr(order, "status", None)),
            filled_qty=filled_qty,
            open_price=avg_fill_price,
            comment=None,
            created_at=getattr(order, "received_at", None) or getattr(order, "updated_at", None),
        )

    def get_orders(self, status=None) -> Any:
        """All orders for this account, optionally filtered by platform OrderStatus.

        Args:
            status: a ``ba2_common.core.types.OrderStatus``. ``None`` and ``ALL``
                mean unfiltered; ``OPEN``/``CLOSED`` expand to the matching
                TastyTrade statuses.
        """
        # OUTSIDE the try: an unexpressible filter is a caller bug, and swallowing the
        # ValueError into the `return []` below would report "no such orders".
        statuses = self._tt_statuses_for(status)
        if not self._check_authentication():
            return []
        try:
            # page_offset=None is the SDK's "walk every page" sentinel
            # (session.py:389-419). Without it only the first 50 rows come back.
            kwargs = {"page_offset": None}
            if statuses:
                kwargs["statuses"] = statuses
            raw_orders = self._run_async(
                self._account.get_order_history(self._session, **kwargs))
            orders = [o for o in (self.tastytrade_order_to_tradingorder(r) for r in raw_orders)
                      if o is not None]
            logger.debug(f"[Account {self.id}] Retrieved {len(orders)} orders from TastyTrade")
            return orders
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting orders: {e}", exc_info=True)
            return []

    def get_order(self, order_id: str) -> Any:
        """Fetch one order by its BROKER id.

        TastyTrade order ids are integers. A non-numeric id -- an Alpaca UUID left on
        a migrated row, or a caller handing over something else entirely -- is
        rejected up front and logged as such, instead of raising ValueError out of a
        bare ``int()`` and being reported as a broker failure.
        """
        if not self._check_authentication():
            return None
        try:
            broker_id = int(str(order_id).strip())
        except (TypeError, ValueError):
            logger.error(
                f"[Account {self.id}] '{order_id}' is not a TastyTrade order id "
                f"(broker ids are numeric)")
            return None
        try:
            raw = self._run_async(self._account.get_order(self._session, broker_id))
            return self.tastytrade_order_to_tradingorder(raw)
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting order {order_id}: {e}", exc_info=True)
            return None

    def symbols_exist(self, symbols: List[str]) -> Dict[str, bool]:
        if not self._check_authentication():
            return {s: False for s in symbols}
        try:
            from tastytrade.instruments import Equity
            result = {}
            try:
                # page_offset=None -> all pages; the default of 0 caps the lookup at 250.
                equities = self._run_async(
                    Equity.get(self._session, symbols, page_offset=None))
                if isinstance(equities, list):
                    found_symbols = {e.symbol for e in equities}
                else:
                    found_symbols = {equities.symbol}
            except Exception as e:
                logger.warning(f"[Account {self.id}] Symbol lookup failed: {e}")
                found_symbols = set()

            for s in symbols:
                result[s] = s in found_symbols
            return result
        except Exception as e:
            logger.error(f"[Account {self.id}] Error checking symbols: {e}", exc_info=True)
            return {s: False for s in symbols}

    #: get_market_data_by_type's COMBINED limit across ALL instrument types is 100 per
    #: call (tastytrade/market_data.py:132), so symbol lists are chunked.
    _MARKET_DATA_CHUNK = 100

    @staticmethod
    def _pick_price(data, price_type: str) -> Optional[float]:
        """Resolve one MarketData row to a price, falling back down the ladder.

        Returns ``None`` when the row carries no usable price -- never a fabricated
        number (platform rule: no fallback values for live data).
        """
        if price_type == 'bid' and data.bid:
            return float(data.bid)
        if price_type == 'ask' and data.ask:
            return float(data.ask)
        if price_type == 'mid' and data.mid:
            return float(data.mid)
        if data.last:
            return float(data.last)
        if data.close:
            return float(data.close)
        return None

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        """Fetch a single price or a bulk price map.

        The list branch uses ``get_market_data_by_type``, which returns a whole batch
        in ONE round trip -- chunked at ``_MARKET_DATA_CHUNK`` because the SDK's
        combined limit is 100 symbols per call. A failing chunk leaves its symbols at
        ``None`` rather than aborting the whole fetch.
        """
        if not self._check_authentication():
            if isinstance(symbol_or_symbols, list):
                return {s: None for s in symbol_or_symbols}
            return None

        from tastytrade.market_data import get_market_data, get_market_data_by_type
        from tastytrade.order import InstrumentType

        if isinstance(symbol_or_symbols, str):
            try:
                data = self._run_async(
                    get_market_data(self._session, symbol_or_symbols, InstrumentType.EQUITY))
                return self._pick_price(data, price_type)
            except Exception as e:
                logger.error(
                    f"[Account {self.id}] Error getting price for {symbol_or_symbols}: {e}",
                    exc_info=True)
                return None

        symbols = list(symbol_or_symbols)
        result = {s: None for s in symbols}
        for start in range(0, len(symbols), self._MARKET_DATA_CHUNK):
            chunk = symbols[start:start + self._MARKET_DATA_CHUNK]
            try:
                rows = self._run_async(
                    get_market_data_by_type(self._session, equities=chunk))
            except Exception as e:
                logger.warning(
                    f"[Account {self.id}] Bulk quote fetch failed for {len(chunk)} symbols "
                    f"starting at {chunk[0]}: {e}")
                continue
            for row in rows:
                if row.symbol in result:
                    result[row.symbol] = self._pick_price(row, price_type)
        return result

    #: platform TradingOrder.good_for -> TastyTrade TIF. An absent/unknown value falls
    #: back to GTC, matching AlpacaAccount._submit_order_impl's tif_map default.
    _TT_TIF_MAP = {
        "day": OrderTimeInForce.DAY,
        "gtc": OrderTimeInForce.GTC,
        "gtd": OrderTimeInForce.GTD,
        "ext": OrderTimeInForce.EXT,
        "gtc_ext": OrderTimeInForce.GTC_EXT,
        "ioc": OrderTimeInForce.IOC,
    }

    @staticmethod
    def _tt_action(side: OrderDirection, is_closing_order: bool) -> OrderAction:
        """The equity ``OrderAction`` for a side plus an open/close intent."""
        if side == OrderDirection.BUY:
            return OrderAction.BUY_TO_CLOSE if is_closing_order else OrderAction.BUY_TO_OPEN
        return OrderAction.SELL_TO_CLOSE if is_closing_order else OrderAction.SELL_TO_OPEN

    @staticmethod
    def _signed_price(price: float, side: OrderDirection) -> Decimal:
        """TastyTrade encodes the direction of cash flow in the SIGN of ``NewOrder.price``.

        Negative = debit (you pay: a BUY), positive = credit (a SELL). The
        ``price_effect`` field is COMPUTED from this sign (order.py:264-276) with
        ``abs()`` applied on serialisation, so it must never be set by hand.
        """
        magnitude = abs(Decimal(str(price)))
        return -magnitude if side == OrderDirection.BUY else magnitude

    def _build_new_order(self, trading_order: TradingOrder,
                         is_closing_order: bool = False) -> NewOrder:
        """Build the SDK ``NewOrder`` for a TradingOrder.

        Shared by the live submit and by ``preview_order_impact``'s dry run, so a
        preview always prices exactly the order that would be sent.

        ``external_identifier`` carries our own row id -- TastyTrade's equivalent of
        Alpaca's ``client_order_id`` -- which is how ``refresh_orders`` matches broker
        orders back to database rows.

        Raises:
            ValueError: when a required price is missing, or the order type is not one
                TastyTrade equity submission supports here (OCO/OTO are out of scope).
        """
        from tastytrade.instruments import Equity

        equity = self._run_async(Equity.get(self._session, trading_order.symbol))
        action = self._tt_action(trading_order.side, is_closing_order)
        leg = equity.build_leg(Decimal(str(trading_order.quantity)), action)

        kwargs = {
            "time_in_force": self._TT_TIF_MAP.get(
                (trading_order.good_for or "").lower(), OrderTimeInForce.GTC),
            "legs": [leg],
            "external_identifier": str(trading_order.id) if trading_order.id else None,
        }

        core_type = trading_order.order_type
        if core_type == CoreOrderType.MARKET:
            kwargs["order_type"] = TTOrderType.MARKET
        elif core_type in (CoreOrderType.BUY_LIMIT, CoreOrderType.SELL_LIMIT):
            if trading_order.limit_price is None:
                raise ValueError(f"Limit price is required for {core_type.value} orders")
            kwargs["order_type"] = TTOrderType.LIMIT
            kwargs["price"] = self._signed_price(trading_order.limit_price, trading_order.side)
        elif core_type in (CoreOrderType.BUY_STOP, CoreOrderType.SELL_STOP):
            if trading_order.stop_price is None:
                raise ValueError(f"Stop price is required for {core_type.value} orders")
            kwargs["order_type"] = TTOrderType.STOP
            kwargs["stop_trigger"] = Decimal(str(trading_order.stop_price))
        elif core_type in (CoreOrderType.BUY_STOP_LIMIT, CoreOrderType.SELL_STOP_LIMIT):
            if trading_order.stop_price is None or trading_order.limit_price is None:
                raise ValueError("Stop and limit prices are both required for stop-limit orders")
            kwargs["order_type"] = TTOrderType.STOP_LIMIT
            kwargs["stop_trigger"] = Decimal(str(trading_order.stop_price))
            kwargs["price"] = self._signed_price(trading_order.limit_price, trading_order.side)
        else:
            raise ValueError(
                f"TastyTrade equity submission does not support order type {core_type}")

        return NewOrder(**kwargs)

    def _submit_order_impl(self, trading_order: TradingOrder, tp_price: Optional[float] = None,
                           sl_price: Optional[float] = None, is_closing_order: bool = False,
                           use_complex_order: bool = False) -> Optional[TradingOrder]:
        """Send ONE equity order to TastyTrade.

        NEVER override ``submit_order``: the template method on AccountInterface owns
        validation, transaction creation, the wash-trade gate and protective legs.
        Overriding it is exactly what disabled IBKRAccount (IBKRAccount.py:27-40).

        ``tp_price`` / ``sl_price`` / ``use_complex_order`` are accepted for interface
        compatibility but CANNOT be honoured: TastyTrade complex orders are out of scope
        here, and protective-leg management is unimplemented
        (``supports_protective_legs = False``). They used to be swallowed with a
        ``logger.warning``, which sent a bare entry to the broker and left the caller
        holding an unprotected position it believed was covered:

          * ``use_complex_order=True`` is set by ``submit_order``'s wash-trade gate when
            it has a TP/SL to attach. Ignoring it produced NEITHER complex legs (this
            method sent a plain order) NOR separate legs (``submit_order`` skips the
            adjust block entirely for a complex submission).
          * a bare ``tp_price``/``sl_price`` reaching here means the capability gate in
            ``submit_order`` was bypassed.

        Both are broken invariants, so both raise. ``submit_order`` refuses such
        requests up front, so neither should ever be reachable through it.

        Raises:
            NotImplementedError: when a protective price or a complex order is requested.

        Returns:
            Optional[TradingOrder]: the refreshed database row on success, ``None`` on
            failure.
        """
        # Raised BEFORE the auth check and before any DB write: nothing may be sent.
        if use_complex_order:
            raise NotImplementedError(
                f"[Account {self.id}] TastyTrade does not support native complex "
                f"(bracket/OTO) orders here, but submit_order requested one for "
                f"{trading_order.symbol} (tp={tp_price}, sl={sl_price}). Silently sending "
                f"a plain order would leave the position with no protective legs at all."
            )
        if tp_price is not None or sl_price is not None:
            raise NotImplementedError(
                f"[Account {self.id}] TastyTrade cannot attach TP/SL legs "
                f"(tp={tp_price}, sl={sl_price}) for {trading_order.symbol}, and cannot "
                f"place them separately either (supports_protective_legs=False). "
                f"Submitting the bare entry would open an UNPROTECTED position."
            )

        if not self._check_authentication():
            return None

        # Idempotency guard: an order that already carries a broker_order_id was
        # already sent. Never re-submit it.
        if trading_order.broker_order_id:
            logger.warning(
                f"Order {trading_order.id} already has broker_order_id "
                f"{trading_order.broker_order_id} -- skipping re-submission")
            return trading_order

        try:
            if trading_order.id is None:
                trading_order.status = OrderStatus.PENDING
                trading_order.id = add_instance(trading_order, expunge_after_flush=True)
                logger.info(
                    f"Created new order {trading_order.id} in database with status PENDING")

            new_order = self._build_new_order(trading_order, is_closing_order=is_closing_order)

            # dry_run DEFAULTS TO True in the SDK (tastytrade/account.py:877-879).
            # Pass it explicitly so a signature change can never turn a live order
            # into a silent no-op.
            response = self._run_async(
                self._account.place_order(self._session, new_order, dry_run=False))

            fresh_order = get_instance(TradingOrder, trading_order.id)
            fresh_order.broker_order_id = str(response.order.id)
            fresh_order.status = self._map_order_status(response.order.status)
            update_instance(fresh_order)
            logger.info(
                f"Submitted TastyTrade order {fresh_order.id}: "
                f"broker_order_id={fresh_order.broker_order_id}, status={fresh_order.status}")
            return fresh_order
        except Exception as e:
            logger.error(
                f"Error submitting order {trading_order.id} to TastyTrade: {e}", exc_info=True)
            # Broker-agnostic failure handling: classify the error, retry ONCE as a
            # MARKET order when a stop was already through the market, and otherwise
            # mark the row ERROR with the typed reason + broker message in `comment`
            # (so it is visible in the Pending Orders UI, not just the log). Returns
            # the resubmitted order on a successful retry, else None.
            if trading_order.id:
                return self._handle_order_submit_error(trading_order, e)
            logger.warning("Cannot mark order as ERROR - order has no ID")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order. ``order_id`` may be our DATABASE id or the BROKER id.

        AlpacaAccount.cancel_order tells the two apart by looking for a '-' (its broker
        ids are UUIDs). TastyTrade broker ids are integers, exactly like our database
        ids, so resolution is by LOOKUP instead: our own row first (scoped to this
        account), then by ``broker_order_id``.

        Returns:
            bool: True when the cancel was accepted by the broker.
        """
        if not self._check_authentication():
            return False

        db_order = None
        try:
            candidate = get_instance(TradingOrder, int(str(order_id).strip()))
            if candidate.account_id == self.id:
                db_order = candidate
        except (InstanceNotFound, TypeError, ValueError):
            db_order = None

        if db_order is None:
            with get_db() as session:
                found = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.broker_order_id == str(order_id),
                        TradingOrder.account_id == self.id,
                    )
                ).first()
                found_id = found.id if found else None
            db_order = get_instance(TradingOrder, found_id) if found_id else None

        if db_order is None:
            logger.error(f"[Account {self.id}] Order {order_id} not found in database")
            return False
        if not db_order.broker_order_id:
            logger.error(
                f"[Account {self.id}] Order {db_order.id} has no broker_order_id "
                f"-- it was never sent to TastyTrade")
            return False

        try:
            self._run_async(
                self._account.delete_order(self._session, int(db_order.broker_order_id)))
        except Exception as e:
            logger.error(
                f"[Account {self.id}] Error cancelling TastyTrade order {order_id}: {e}",
                exc_info=True)
            return False

        # PENDING_CANCEL, not CANCELED: the cancel has only been REQUESTED.
        # refresh_orders promotes it once the broker confirms and the qty is actually
        # released -- same rule as AlpacaAccount.cancel_order.
        fresh_order = get_instance(TradingOrder, db_order.id)
        fresh_order.status = OrderStatus.PENDING_CANCEL
        update_instance(fresh_order)
        logger.info(
            f"[Account {self.id}] Requested cancel of TastyTrade order "
            f"broker_order_id={db_order.broker_order_id} (db id={db_order.id})")
        return True

    def preview_order_impact(self, trading_order: TradingOrder,
                             is_closing_order: bool = False) -> Optional[OrderImpact]:
        """Broker-side dry run of ONE order: what it would cost in buying power.

        MUST NOT send a live order. ``place_order``'s ``dry_run`` parameter DEFAULTS
        TO True (tastytrade/account.py:877-879) -- it is passed explicitly here anyway,
        and must always be passed explicitly at real submission sites.

        The order is built with the SAME ``_build_new_order`` the live submit uses, so
        a preview prices exactly what would be sent. ``trading_order`` is neither
        mutated nor persisted, and no ``broker_order_id`` is written.

        ``is_closing_order`` MUST be forwarded, exactly as the live submit at
        ``_submit_order_impl`` forwards it. TastyTrade encodes open/close in the LEG
        ACTION, so the flag is the difference between SELL_TO_CLOSE and SELL_TO_OPEN --
        i.e. between freeing buying power and opening a SHORT that consumes margin and
        needs short approval. It used to default here while the live submit passed the
        caller's value, so every closing preview priced a short: on a cash or
        short-unapproved account the dry run returned errors, ``accepted=False``, and
        the allocation engine skipped a legitimate sell.

        Returns:
            Optional[OrderImpact]: ``None`` when the preview call failed. ``None``
            means "no precheck", NOT "the order is free" -- a zero impact is never
            fabricated.
        """
        if not self._check_authentication():
            return None
        try:
            new_order = self._build_new_order(trading_order,
                                              is_closing_order=is_closing_order)
            response = self._run_async(
                self._account.place_order(self._session, new_order, dry_run=True))
        except Exception as e:
            logger.error(
                f"[Account {self.id}] Order preview failed for {trading_order.symbol}: {e}",
                exc_info=True)
            return None

        effect = response.buying_power_effect
        fees = getattr(response, "fee_calculation", None)
        warnings = [str(w) for w in (response.warnings or [])]
        errors = [str(err) for err in (response.errors or [])]
        return OrderImpact(
            symbol=trading_order.symbol,
            # SIGNED: negative for a buy. Consume OrderImpact.bp_cost, never this.
            change_in_buying_power=float(effect.change_in_buying_power),
            # abs() ON BOTH -- LOAD-BEARING, NOT DEFENSIVE.
            #
            # `isolated_order_margin_requirement` and `total_fees` are both run through
            # `tastytrade.utils.set_sign_for` by a `model_validator(mode="before")`
            # (order.py:381-393 and :407-419). That helper does not NORMALISE a sign, it
            # CREATES a negative one: `if data["<key>-effect"] == DEBIT:
            # data[key] = -abs(...)`. A margin requirement and a fee are both debits, so
            # in production these arrive as -1500 and -0.03.
            #
            # Unlike change_in_buying_power, neither has a re-signing property on
            # OrderImpact: `margin_requirement` is a REQUIREMENT (capital tied up) and
            # `estimated_fees` is a COST, and both are consumed directly. A negative
            # requirement understates committed capital; a negative fee reads as a
            # rebate. Take the magnitude.
            margin_requirement=abs(float(effect.isolated_order_margin_requirement)),
            estimated_fees=abs(float(fees.total_fees)) if fees is not None else None,
            accepted=not errors,
            warnings=warnings,
            errors=errors,
            raw={
                "current_buying_power": float(effect.current_buying_power),
                "new_buying_power": float(effect.new_buying_power),
                "change_in_margin_requirement": float(effect.change_in_margin_requirement),
            },
        )

    # ------------------------------------------------------------------
    # Out of scope for TastyTrade (see class docstring). These are declared
    # @abstractmethod on AccountInterface, so they must exist for the class to be
    # instantiable -- but they fail LOUDLY rather than half-working.
    # ------------------------------------------------------------------

    def modify_order(self, order_id: str, trading_order: Optional[TradingOrder] = None):
        """NOT SUPPORTED on TastyTrade.

        The SDK exposes ``Account.replace_order``, but using it would need the whole
        cancel/replace + dependent-order bookkeeping AlpacaAccount carries. Cancel the
        order and submit a new one instead.
        """
        logger.error(
            f"[Account {self.id}] modify_order is not supported for TastyTrade "
            f"(order {order_id}); cancel and resubmit instead")
        return None

    # NotImplementedError, NOT `return False`. `False` means "I tried and the broker
    # refused"; NotImplementedError means "this broker cannot do this at all", and it is
    # the signal AccountInterface.submit_order documents and catches
    # (AccountInterface.py, the adjust_* bracket block). Returning False made that guard
    # DEAD CODE: submit_order swallowed the False, returned the successful entry order,
    # and the caller that asked for a protective stop got a live NAKED position reported
    # as success. Callers that want a boolean must catch it explicitly.

    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        raise NotImplementedError(
            f"[Account {self.id}] adjust_tp is not supported for TastyTrade "
            f"(transaction {transaction.id}, requested {new_tp_price}): this broker "
            f"cannot place a protective take-profit leg.")

    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        raise NotImplementedError(
            f"[Account {self.id}] adjust_sl is not supported for TastyTrade "
            f"(transaction {transaction.id}, requested {new_sl_price}): this broker "
            f"cannot place a protective stop leg.")

    def adjust_tp_sl(self, transaction: Transaction, new_tp_price: Optional[float] = None,
                     new_sl_price: Optional[float] = None, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        raise NotImplementedError(
            f"[Account {self.id}] adjust_tp_sl is not supported for TastyTrade "
            f"(transaction {transaction.id}, tp={new_tp_price}, sl={new_sl_price}): this "
            f"broker cannot place protective legs.")

    def refresh_positions(self) -> bool:
        """Confirm the broker's equity book is readable.

        TastyTrade positions are always fetched live, so there is no cache to refresh
        -- but the return value is a real signal that the broker answered. ``None``
        from ``get_positions`` means the FETCH FAILED (not a flat account), so it maps
        to False here, exactly as AlpacaAccount.refresh_positions does.
        """
        positions = self.get_positions()
        if positions is None:
            logger.error(f"[Account {self.id}] Error refreshing positions from TastyTrade: fetch failed")
            return False
        logger.info(
            f"[Account {self.id}] Successfully refreshed {len(positions)} positions from TastyTrade")
        return True

    def refresh_orders(self, **kwargs) -> bool:
        """Sync our TradingOrder rows with TastyTrade's order book.

        Without this, ``refresh_transactions``
        (ReadOnlyAccountInterface.refresh_transactions) has nothing to derive from and
        every transaction stays WAITING forever.

        Matching is on ``external_identifier`` first (we set it to our own row id at
        submission -- TastyTrade's equivalent of Alpaca's ``client_order_id``), then on
        ``broker_order_id``. Unlike AlpacaAccount.refresh_orders this does NOT cancel
        rows that are missing from the response: TastyTrade's order history is
        paginated and date-windowed, so absence is not evidence of cancellation.

        Args:
            **kwargs: absorbs the Alpaca-specific ``heuristic_mapping`` / ``fetch_all``
                that ui/pages/overview.py and core/TradeManager.py pass by name.

        Returns:
            bool: False only when the broker fetch itself failed.
        """
        if not self._check_authentication():
            return False
        try:
            raw_orders = self._run_async(
                self._account.get_order_history(self._session, page_offset=None))
        except Exception as e:
            logger.error(
                f"[Account {self.id}] Error refreshing orders from TastyTrade: {e}",
                exc_info=True)
            return False

        updated_count = 0
        mapped_count = 0
        for raw in raw_orders:
            raw_id = getattr(raw, "id", None)
            if raw_id in (None, -1):
                continue
            broker_order_id = str(raw_id)

            broker_state = self.tastytrade_order_to_tradingorder(raw)
            if broker_state is None:
                continue

            db_order = None
            external_identifier = getattr(raw, "external_identifier", None)
            if external_identifier:
                try:
                    candidate = get_instance(TradingOrder, int(external_identifier))
                except (InstanceNotFound, TypeError, ValueError):
                    candidate = None
                if candidate is not None and candidate.account_id == self.id:
                    db_order = candidate
                    if not db_order.broker_order_id:
                        mapped_count += 1

            if db_order is None:
                with get_db() as session:
                    found = session.exec(
                        select(TradingOrder).where(
                            TradingOrder.broker_order_id == broker_order_id,
                            TradingOrder.account_id == self.id,
                        )
                    ).first()
                    found_id = found.id if found else None
                db_order = get_instance(TradingOrder, found_id) if found_id else None
            if db_order is None:
                continue

            has_changes = False

            # PENDING_CANCEL only advances once the broker reaches a FINAL state --
            # a dependent replacement must not fire before the qty is released.
            if db_order.status == OrderStatus.PENDING_CANCEL:
                resolved = OrderStatus.resolve_pending_cancel(broker_state.status)
                if resolved is not None and resolved != db_order.status:
                    logger.info(
                        f"Order {db_order.id} PENDING_CANCEL -> {resolved.value} "
                        f"(broker reported {broker_state.status})")
                    db_order.status = resolved
                    has_changes = True
            elif db_order.status != broker_state.status:
                logger.debug(
                    f"Order {db_order.id} status changed: {db_order.status} -> {broker_state.status}")
                db_order.status = broker_state.status
                has_changes = True

            if float(db_order.filled_qty or 0.0) != float(broker_state.filled_qty or 0.0):
                db_order.filled_qty = broker_state.filled_qty
                has_changes = True

            if broker_state.open_price is not None and db_order.open_price != broker_state.open_price:
                db_order.open_price = broker_state.open_price
                has_changes = True

            if not db_order.broker_order_id:
                db_order.broker_order_id = broker_order_id
                has_changes = True

            if has_changes:
                update_instance(db_order)
                updated_count += 1

        logger.info(
            f"[Account {self.id}] Refreshed TastyTrade orders: {updated_count} updated, "
            f"{mapped_count} mapped via external_identifier")
        return True

    @staticmethod
    def _net_dividend(gross_total: float, tax_total: float) -> float:
        """The NET dividend kept for ONE ``(symbol, transaction_date)``.

        THE single definition of a net dividend for this adapter: both
        ``get_dividends`` and ``get_cash_transfers`` derive from it, so the two seams
        can never disagree about the same broker history (they did: a multi-leg
        dividend came out of the ledger seam short by the whole withholding).

        FLOORED AT ZERO. ``CashTransfer.amount`` is documented POSITIVE for a dividend
        and ``is_income`` gates the ledger on ``amount > 0``; withholding larger than
        the gross it belongs to is a CORRECTION, not negative income.
        """
        gross = round(float(gross_total), 2)
        tax = round(abs(float(tax_total)), 2)
        if tax > gross:
            logger.warning(
                f"Dividend withholding {tax:.2f} exceeds the {gross:.2f} gross it "
                f"belongs to; reporting 0.00 income rather than a negative dividend")
        net = round(gross - tax, 2)
        return net if net > 0 else 0.0

    @classmethod
    def _net_dividend_legs(cls, gross_values, tax_total) -> List[float]:
        """Split ONE ``(symbol, date)``'s net dividend back across its GROSS legs.

        TastyTrade posts a dividend as one or MORE positive ``Money Movement`` /
        ``Dividend`` legs (a regular dividend, a special dividend, a correction) plus
        at most one negative withholding leg, all sharing ``(symbol,
        transaction_date)``.

        The tax is allocated PRO RATA across that key's gross legs. The two
        alternatives were both worse:
          * subtracting the key's whole tax from EVERY gross leg -- what this used to
            do -- charged the tax once per leg (1.00 + 0.57 with 0.24 tax posted 1.09
            instead of 1.33) and could drive a small leg NEGATIVE; and
          * collapsing the key to a single row would drop the other legs' broker ids,
            and ``(account_id, external_id)`` is the ``portfolio_income_event``
            idempotency key -- a previously synced leg would be stranded at its old,
            wrong amount forever instead of being upserted.

        Returns:
            List[float]: one net amount per gross leg, in the given order, each
            rounded to cents, each ``>= 0``, summing EXACTLY to
            ``_net_dividend(sum(gross), tax)``.
        """
        gross = [round(float(g), 2) for g in gross_values]
        if not gross:
            return []
        total_gross = round(sum(gross), 2)
        target = cls._net_dividend(total_gross, tax_total)
        if total_gross <= 0 or target <= 0:
            return [0.0] * len(gross)
        nets = [round(g * target / total_gross, 2) for g in gross]
        # Largest-remainder: park the rounding residual on the BIGGEST leg so the
        # allocation sums to the key's net to the cent.
        residual = round(target - round(sum(nets), 2), 2)
        if residual:
            biggest = max(range(len(gross)), key=lambda i: gross[i])
            nets[biggest] = round(nets[biggest] + residual, 2)
        return [n if n > 0 else 0.0 for n in nets]

    def get_dividends(self, symbol=None, start_date=None, end_date=None) -> List[Dict]:
        """Return one record per dividend the account received.

        Dividends are driven by the CASH event, not the reinvestment. TastyTrade
        posts each dividend as a group of ``Money Movement`` transactions with
        ``transaction_sub_type == "Dividend"`` per (symbol, date):
            +1.57  "TIDAL TRUST II"   gross dividend (positive)
            -0.24  "TIDAL TRUST II"   tax withheld   (negative)
        When DRIP is enabled there is additionally:
            - a ``Money Movement`` / ``Withdrawal`` "Cash dividend reinvested
              into X" leg (the net cash spent buying shares), and
            - a ``Receive Deliver`` / ``Dividend`` leg crediting the shares.
        We anchor on the ``Money Movement`` / ``Dividend`` cash so a dividend
        shows up whether or not it was reinvested (with DRIP off there is no
        ``Receive Deliver`` leg at all), and we ENRICH with the share-receipt
        leg only when present.

        ``amount`` is the NET dividend (gross - tax) — the income actually kept.
        ``gross_amount`` / ``tax_withheld`` carry the breakdown.
        ``drip_quantity`` / ``drip_price`` are None for non-reinvested (cash)
        dividends.
        """
        if not self._check_authentication():
            return []
        try:
            def _range(p):
                if start_date:
                    p["start_date"] = start_date.date() if isinstance(start_date, datetime) else start_date
                if end_date:
                    p["end_date"] = end_date.date() if isinstance(end_date, datetime) else end_date
                if symbol:
                    p["symbol"] = symbol
                return p

            # DRIP share-receipt legs (enrichment only): Receive Deliver / Dividend.
            drip_map = {}  # (symbol, date) -> (qty, price)
            try:
                rd_txns = self._run_async(self._account.get_history(
                    self._session, **_range({"types": ["Receive Deliver"],
                                             "sub_types": ["Dividend"], "sort": "Asc",
                                             "page_offset": None})))
                for txn in rd_txns:
                    sym = getattr(txn, 'underlying_symbol', None) or getattr(txn, 'symbol', None)
                    d = getattr(txn, 'transaction_date', None)
                    qty = float(getattr(txn, 'quantity', 0) or 0)
                    if sym and d and qty > 0:
                        drip_map[(sym, d)] = (qty, float(getattr(txn, 'price', 0) or 0))
            except Exception as e:
                logger.warning(f"[Account {self.id}] DRIP share-receipt fetch failed: {e}")

            # Cash dividends + tax: Money Movement transactions, sub_type "Dividend"
            # only (excludes the "Withdrawal" reinvest leg, ACH deposits, fee
            # adjustments, etc.). Positive net = gross, negative net = tax.
            gross_map = {}  # (symbol, date) -> gross dividend
            tax_map = {}    # (symbol, date) -> tax withheld (positive)
            mm_txns = self._run_async(self._account.get_history(
                self._session, **_range({"types": ["Money Movement"], "sort": "Asc",
                                         "page_offset": None})))
            for mm in mm_txns:
                if getattr(mm, 'transaction_sub_type', None) != 'Dividend':
                    continue
                sym = getattr(mm, 'underlying_symbol', None) or getattr(mm, 'symbol', None)
                d = getattr(mm, 'transaction_date', None)
                if not sym or not d:
                    continue
                net_val = float(getattr(mm, 'net_value', 0) or 0)
                key = (sym, d)
                if net_val > 0:
                    gross_map[key] = gross_map.get(key, 0.0) + net_val
                elif net_val < 0:
                    tax_map[key] = tax_map.get(key, 0.0) + abs(net_val)

            # Anchor on the GROSS dividend keys (one positive cash dividend per
            # symbol+date). Tax is linked back by the SAME (symbol, date) key, so
            # withholding only ever reduces its own symbol's dividend -- an
            # unrelated/orphan tax line (no matching gross) is never emitted as a
            # phantom negative dividend.
            dividends = []
            for key in sorted(gross_map, key=lambda k: (str(k[1]), str(k[0]))):
                sym, d = key
                gross = round(gross_map.get(key, 0.0), 2)
                tax = round(tax_map.get(key, 0.0), 2)
                drip_qty, drip_price = drip_map.get(key, (None, None))
                dividends.append({
                    'symbol': sym,
                    # NET dividend (income kept). Same helper get_cash_transfers uses,
                    # so the two seams cannot disagree about the same history.
                    'amount': self._net_dividend(gross, tax),
                    'gross_amount': gross,
                    'tax_withheld': tax,
                    'date': d,
                    'drip_quantity': drip_qty,
                    'drip_price': drip_price,
                })

            logger.debug(f"[Account {self.id}] Retrieved {len(dividends)} dividend records")
            return dividends
        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching dividends: {e}", exc_info=True)
            return []

    #: Money Movement sub-types that ADD cash to the account.
    _TT_DEPOSIT_SUB_TYPES = ("Deposit", "Transfer")
    #: Money Movement sub-types that REMOVE cash from the account.
    _TT_WITHDRAWAL_SUB_TYPES = ("Withdrawal", "Transfer")

    def get_cash_transfers(self, start_date=None, end_date=None) -> List[CashTransfer]:
        """Deposits, withdrawals and dividends over a date window.

        ``page_offset=None`` is the SDK "all pages" sentinel. ``external_id`` is the
        broker transaction id -- the ``(account_id, external_id)`` idempotency key of
        ``portfolio_income_event`` -- so re-syncing a window upserts instead of
        duplicating.

        A dividend arrives as one or MORE positive gross legs plus (optionally) a
        NEGATIVE tax leg, all sharing the same ``(symbol, transaction_date)``. ONE
        CashTransfer is emitted per GROSS leg, keeping that leg's own id, and the key's
        withholding is netted ONCE -- allocated pro rata across its legs by
        ``_net_dividend_legs`` -- so the ledger records the income actually KEPT and the
        id stays 1:1. A DIVIDEND amount is never negative.

        Returns:
            List[CashTransfer]: ``[]`` on failure as well as on genuine emptiness (this
            seam does not distinguish the two); the failure is logged.
        """
        if not self._check_authentication():
            return []

        params = {"types": ["Money Movement"], "sort": "Asc", "page_offset": None}
        if start_date is not None:
            params["start_date"] = start_date.date() if isinstance(start_date, datetime) else start_date
        if end_date is not None:
            params["end_date"] = end_date.date() if isinstance(end_date, datetime) else end_date

        try:
            transactions = self._run_async(self._account.get_history(self._session, **params))
        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching cash transfers: {e}", exc_info=True)
            return []

        # Pass 1: GROUP the dividend legs by (symbol, date). Withholding belongs to the
        # whole key, so it must be netted ONCE across that key's gross legs -- taking
        # the key's whole tax off EVERY gross leg charged it once per leg.
        gross_legs = {}   # (symbol, date) -> [(row index, gross amount), ...]
        tax_by_key = {}   # (symbol, date) -> withholding, as a positive magnitude
        for index, txn in enumerate(transactions):
            if getattr(txn, "transaction_sub_type", None) != "Dividend":
                continue
            net_value = float(getattr(txn, "net_value", 0) or 0)
            key = (getattr(txn, "underlying_symbol", None) or getattr(txn, "symbol", None),
                   getattr(txn, "transaction_date", None))
            if net_value > 0:
                gross_legs.setdefault(key, []).append((index, net_value))
            elif net_value < 0:
                tax_by_key[key] = tax_by_key.get(key, 0.0) + abs(net_value)

        # Pass 2: allocate each key's tax across its OWN gross legs, pro rata. An
        # orphan tax line (no matching gross) is simply never emitted -- it is not a
        # phantom negative dividend.
        net_by_index = {}
        for key, legs in gross_legs.items():
            nets = self._net_dividend_legs([gross for _, gross in legs],
                                           tax_by_key.get(key, 0.0))
            for (index, _), net in zip(legs, nets):
                net_by_index[index] = net

        transfers = []
        for index, txn in enumerate(transactions):
            external_id = str(getattr(txn, "id", "") or "")
            event_date = getattr(txn, "transaction_date", None)
            if not external_id or event_date is None:
                continue
            sub_type = getattr(txn, "transaction_sub_type", None)
            net_value = float(getattr(txn, "net_value", 0) or 0)
            description = getattr(txn, "description", None)

            if sub_type == "Dividend":
                if net_value <= 0:
                    continue  # a tax leg -- already netted across its key's gross rows
                symbol = getattr(txn, "underlying_symbol", None) or getattr(txn, "symbol", None)
                amount = net_by_index[index]
                transfers.append(CashTransfer(
                    external_id=external_id, event_date=event_date,
                    event_type=CASH_TRANSFER_DIVIDEND, amount=amount,
                    symbol=symbol, description=description))
            elif sub_type in self._TT_DEPOSIT_SUB_TYPES and net_value > 0:
                transfers.append(CashTransfer(
                    external_id=external_id, event_date=event_date,
                    event_type=CASH_TRANSFER_DEPOSIT, amount=net_value,
                    description=description))
            elif sub_type in self._TT_WITHDRAWAL_SUB_TYPES and net_value < 0:
                # A DRIP leg is a "Withdrawal" that never left the account: it bought
                # shares with the dividend already recorded above. Emitting it would
                # double-count the cash going out.
                if "reinvest" in (description or "").lower():
                    continue
                transfers.append(CashTransfer(
                    external_id=external_id, event_date=event_date,
                    event_type=CASH_TRANSFER_WITHDRAWAL, amount=net_value,
                    description=description))

        logger.debug(f"[Account {self.id}] Retrieved {len(transfers)} cash transfers")
        return transfers

    def get_symbol_margin_info(self, symbols: List[str]) -> Dict[str, MarginInfo]:
        """Per-symbol margin / fractionability metadata, for buying-power sizing.

        Three SDK inputs are combined:
          * ``Account.get_margin_requirements()`` -> per-underlying
            ``initial_requirement``. Divided by the position's own notional (from
            ``get_positions``) that is the REAL initial margin rate for a HELD symbol
            -- the data behind TastyTrade's Cap Req screen.
          * ``Equity.is_fractional_quantity_eligible`` -> ``fractionable``.
          * ``get_quantity_decimal_precisions()`` -> the equity trade increment.

        A symbol with NO Equity record is OMITTED, never defaulted here -- the caller
        must know it fell back. A symbol that has an Equity record but is not held gets
        ``bp_factor = account multiplier``, which is EXACTLY the caller's own
        conservative fallback (assume no leverage), so reporting it over-commits
        nothing while still supplying real fractionability data.

        Args:
            symbols: symbols to describe; normalised here to ``.strip().upper()``.

        Returns:
            Dict[str, MarginInfo]: keyed by the normalised symbol.
        """
        from tastytrade.instruments import Equity, get_quantity_decimal_precisions

        wanted = [s.strip().upper() for s in (symbols or []) if s and s.strip()]
        if not wanted or not self._check_authentication():
            return {}

        is_margin = self._is_margin_account()
        multiplier = 2.0 if is_margin else 1.0

        equities = {}
        try:
            found = self._run_async(Equity.get(self._session, wanted, page_offset=None))
            equities = {e.symbol.strip().upper(): e for e in found}
        except Exception as e:
            logger.warning(f"[Account {self.id}] Equity metadata fetch failed: {e}")

        increment = None
        try:
            for precision in self._run_async(get_quantity_decimal_precisions(self._session)):
                # The generic EQUITY row (symbol is None) is the one that applies to
                # every equity; per-symbol overrides are not needed for sizing.
                if precision.instrument_type == TTInstrumentType.EQUITY and precision.symbol is None:
                    increment = float(10 ** -int(precision.minimum_increment_precision))
                    break
        except Exception as e:
            logger.warning(f"[Account {self.id}] Quantity precision fetch failed: {e}")

        # get_positions() returns None when the FETCH FAILED and [] when the account is
        # genuinely flat -- the distinction this file documents at get_positions. `or []`
        # collapsed the two: with the fetch failing, a HELD symbol carrying a real
        # initial_requirement was reported bp_factor=multiplier / initial_margin_rate=None
        # / source='default', byte-identical to an UNHELD one. Conservative in direction,
        # but it silently discards the only real per-symbol margin data TastyTrade
        # publishes and leaves the caller no way to know it fell back. Report NOTHING
        # instead: an omitted symbol is this method's documented "you must fall back".
        held = self.get_positions()
        if held is None:
            logger.error(
                f"[Account {self.id}] position fetch failed; cannot derive per-symbol "
                f"margin rates, returning no margin info for {len(wanted)} symbol(s) "
                f"rather than reporting them as unheld")
            return {}

        notional = {}
        for position in held:
            if position.market_value:
                notional[position.symbol.strip().upper()] = abs(float(position.market_value))

        requirement = {}
        try:
            report = self._run_async(self._account.get_margin_requirements(self._session))
            for group in (getattr(report, "groups", None) or []):
                # `groups` is list[MarginReportEntry | EmptyDict]; the EmptyDict
                # placeholders carry no attributes, hence getattr with defaults.
                symbol = getattr(group, "underlying_symbol", None)
                initial = getattr(group, "initial_requirement", None)
                if symbol and initial is not None:
                    requirement[symbol.strip().upper()] = abs(float(initial))
        except Exception as e:
            logger.warning(f"[Account {self.id}] Margin requirement fetch failed: {e}")

        result = {}
        for symbol in wanted:
            equity = equities.get(symbol)
            if equity is None:
                continue
            rate = None
            source = MARGIN_SOURCE_DEFAULT
            if symbol in requirement and notional.get(symbol):
                rate = min(1.0, requirement[symbol] / notional[symbol])
                source = MARGIN_SOURCE_POSITION
            # `is_fractional_quantity_eligible` is Optional in the SDK; None means the
            # broker did not say, which must read as "whole shares only". Assuming
            # fractional gets the order rejected at submission.
            fractionable = bool(getattr(equity, "is_fractional_quantity_eligible", False))
            result[symbol] = MarginInfo(
                symbol=symbol,
                bp_factor=(rate * multiplier) if rate is not None else multiplier,
                # TastyTrade publishes no PER-SYMBOL marginability flag, so this
                # reports whether the ACCOUNT is a margin account.
                marginable=is_margin,
                fractionable=fractionable,
                min_order_size=None,
                # MarginInfo.min_trade_increment = the smallest QUANTITY step the
                # broker accepts for this symbol, None when it did not publish one.
                # A whole-share-only symbol steps by 1.0 BY DEFINITION -- that is a
                # fact about the symbol, not a reading, so it survives the precision
                # table being unreachable. A fractionable one steps by the equity
                # precision, and stays None when that fetch failed rather than
                # inventing a step.
                min_trade_increment=increment if fractionable else 1.0,
                initial_margin_rate=rate,
                maintenance_margin_rate=None,
                source=source,
            )
        return result

    def get_balance_history(self, start_date=None, end_date=None) -> List[Dict]:
        if not self._check_authentication():
            return []
        try:
            # TastyTrade requires start_date to return daily snapshots;
            # without it, only a few snapshots are returned.
            if start_date:
                sd = start_date.date() if isinstance(start_date, datetime) else start_date
            else:
                sd = date.today() - timedelta(days=365)

            params = {"start_date": sd}
            if end_date:
                params["end_date"] = end_date.date() if isinstance(end_date, datetime) else end_date

            snapshots = self._run_async(self._account.get_balance_snapshots(
                self._session,
                page_offset=None,  # Get all pages
                **params
            ))

            result = []
            for snap in snapshots:
                cash = float(snap.cash_balance) if snap.cash_balance else 0.0
                nlv = float(snap.net_liquidating_value) if snap.net_liquidating_value else 0.0
                equity = nlv - cash

                result.append({
                    'date': snap.snapshot_date if hasattr(snap, 'snapshot_date') else None,
                    'net_liquidating_value': nlv,
                    'cash_balance': cash,
                    'equity_value': equity,
                })

            logger.debug(f"[Account {self.id}] Retrieved {len(result)} balance history snapshots")
            return result
        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching balance history: {e}", exc_info=True)
            return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None) -> List[Dict]:
        """Get filled trade history from TastyTrade transaction history."""
        if not self._check_authentication():
            return []

        try:
            params = {
                "types": ["Trade"],
                "sort": "Asc",
                # page_offset=None -> all pages; the default of 0 caps history at 250 rows.
                "page_offset": None,
            }
            if symbol:
                params["symbol"] = symbol
            if start_date:
                params["start_date"] = start_date.date() if isinstance(start_date, datetime) else start_date
            if end_date:
                params["end_date"] = end_date.date() if isinstance(end_date, datetime) else end_date

            transactions = self._run_async(self._account.get_history(self._session, **params))

            trades = []
            for txn in transactions:
                txn_symbol = getattr(txn, 'underlying_symbol', None) or getattr(txn, 'symbol', None)
                txn_qty = float(getattr(txn, 'quantity', 0) or 0)
                if txn_qty <= 0:
                    continue

                txn_date = getattr(txn, 'transaction_date', None) or getattr(txn, 'executed_at', None)
                txn_price = float(getattr(txn, 'price', 0) or 0)

                # Determine side from action field
                action = str(getattr(txn, 'action', '') or '').lower()
                if 'buy' in action:
                    side = 'BUY'
                elif 'sell' in action:
                    side = 'SELL'
                else:
                    continue

                trades.append({
                    'symbol': txn_symbol,
                    'qty': txn_qty,
                    'side': side,
                    'date': txn_date,
                    'price': txn_price,
                })

            logger.debug(f"[Account {self.id}] Retrieved {len(trades)} filled trades")
            return trades

        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching filled trades: {e}", exc_info=True)
            return []
