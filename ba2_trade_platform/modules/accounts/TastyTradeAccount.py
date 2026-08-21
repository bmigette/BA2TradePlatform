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
from ...core.account_types import OrderImpact
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
    _TT_OPEN_STATUSES = (TTOrderStatus.RECEIVED, TTOrderStatus.ROUTED,
                         TTOrderStatus.IN_FLIGHT, TTOrderStatus.LIVE,
                         TTOrderStatus.CONTINGENT)

    #: TastyTrade statuses that mean "done, one way or another".
    _TT_CLOSED_STATUSES = (TTOrderStatus.FILLED, TTOrderStatus.CANCELLED,
                           TTOrderStatus.EXPIRED, TTOrderStatus.REJECTED,
                           TTOrderStatus.REMOVED)

    @classmethod
    def _tt_statuses_for(cls, status) -> Optional[List["TTOrderStatus"]]:
        """Translate a platform status filter into the SDK's ``statuses=[...]`` list.

        Returns ``None`` for "no filter" -- i.e. ``status`` is ``None`` or
        ``OrderStatus.ALL``. The SDK filters server-side via the ``status[]`` query
        param, so the argument must never be silently dropped (which is what
        ``get_orders`` used to do, returning every order ever placed).
        """
        if status is None or status == OrderStatus.ALL:
            return None
        if status == OrderStatus.OPEN:
            return list(cls._TT_OPEN_STATUSES)
        if status == OrderStatus.CLOSED:
            return list(cls._TT_CLOSED_STATUSES)
        matches = [tt for tt, core in cls._TT_STATUS_MAP.items() if core == status]
        if not matches:
            logger.warning(
                f"No TastyTrade order status maps to {status!r}; fetching unfiltered")
            return None
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
        if not self._check_authentication():
            return []
        try:
            # page_offset=None is the SDK's "walk every page" sentinel
            # (session.py:389-419). Without it only the first 50 rows come back.
            kwargs = {"page_offset": None}
            statuses = self._tt_statuses_for(status)
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

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        if not self._check_authentication():
            if isinstance(symbol_or_symbols, list):
                return {s: None for s in symbol_or_symbols}
            return None

        try:
            from tastytrade.market_data import get_market_data
            from tastytrade.order import InstrumentType

            if isinstance(symbol_or_symbols, str):
                data = self._run_async(get_market_data(self._session, symbol_or_symbols, InstrumentType.EQUITY))
                if price_type == 'bid' and data.bid:
                    return float(data.bid)
                elif price_type == 'ask' and data.ask:
                    return float(data.ask)
                elif price_type == 'mid' and data.mid:
                    return float(data.mid)
                elif data.last:
                    return float(data.last)
                elif data.close:
                    return float(data.close)
                return None
            else:
                result = {}
                for symbol in symbol_or_symbols:
                    try:
                        data = self._run_async(get_market_data(self._session, symbol, InstrumentType.EQUITY))
                        if price_type == 'bid' and data.bid:
                            result[symbol] = float(data.bid)
                        elif price_type == 'ask' and data.ask:
                            result[symbol] = float(data.ask)
                        elif price_type == 'mid' and data.mid:
                            result[symbol] = float(data.mid)
                        elif data.last:
                            result[symbol] = float(data.last)
                        elif data.close:
                            result[symbol] = float(data.close)
                        else:
                            result[symbol] = None
                    except Exception as e:
                        logger.warning(f"[Account {self.id}] Error fetching price for {symbol}: {e}")
                        result[symbol] = None
                return result
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting price: {e}", exc_info=True)
            if isinstance(symbol_or_symbols, list):
                return {s: None for s in symbol_or_symbols}
            return None

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
        compatibility and IGNORED: TastyTrade complex orders are out of scope here, so
        a protective leg has to be placed as its own order.

        Returns:
            Optional[TradingOrder]: the refreshed database row on success, ``None`` on
            failure.
        """
        if not self._check_authentication():
            return None

        # Idempotency guard: an order that already carries a broker_order_id was
        # already sent. Never re-submit it.
        if trading_order.broker_order_id:
            logger.warning(
                f"Order {trading_order.id} already has broker_order_id "
                f"{trading_order.broker_order_id} -- skipping re-submission")
            return trading_order

        if tp_price is not None or sl_price is not None:
            logger.warning(
                f"Order {trading_order.id}: TastyTrade does not attach TP/SL legs at "
                f"submission (tp={tp_price}, sl={sl_price} ignored); place them separately")

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

    def preview_order_impact(self, trading_order: TradingOrder) -> Optional[OrderImpact]:
        """Broker-side dry run of ONE order: what it would cost in buying power.

        MUST NOT send a live order. ``place_order``'s ``dry_run`` parameter DEFAULTS
        TO True (tastytrade/account.py:877-879) -- it is passed explicitly here anyway,
        and must always be passed explicitly at real submission sites.

        The order is built with the SAME ``_build_new_order`` the live submit uses, so
        a preview prices exactly what would be sent. ``trading_order`` is neither
        mutated nor persisted, and no ``broker_order_id`` is written.

        Returns:
            Optional[OrderImpact]: ``None`` when the preview call failed. ``None``
            means "no precheck", NOT "the order is free" -- a zero impact is never
            fabricated.
        """
        if not self._check_authentication():
            return None
        try:
            new_order = self._build_new_order(trading_order)
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
            margin_requirement=float(effect.isolated_order_margin_requirement),
            estimated_fees=float(fees.total_fees) if fees is not None else None,
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

    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        logger.error(
            f"[Account {self.id}] adjust_tp is not supported for TastyTrade "
            f"(transaction {transaction.id}, requested {new_tp_price})")
        return False

    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        logger.error(
            f"[Account {self.id}] adjust_sl is not supported for TastyTrade "
            f"(transaction {transaction.id}, requested {new_sl_price})")
        return False

    def adjust_tp_sl(self, transaction: Transaction, new_tp_price: Optional[float] = None,
                     new_sl_price: Optional[float] = None, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        logger.error(
            f"[Account {self.id}] adjust_tp_sl is not supported for TastyTrade "
            f"(transaction {transaction.id}, tp={new_tp_price}, sl={new_sl_price})")
        return False

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
                    'amount': round(gross - tax, 2),   # NET dividend (income kept)
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
