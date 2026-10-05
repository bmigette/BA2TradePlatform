"""Interactive Brokers account (``ib_async``): manual, automated and options trading.

Design reference: ``docs/plans/2026-10-03-ibkr-support-design.md`` (read it first: the IBKR facts,
the conservative readings and the UNVERIFIED items live there). Highlights:

* Connection/threading is ``ibkr_runtime.IBKRRuntime``: a private asyncio loop thread per account,
  every ib_async call is a coroutine on it, the public methods here are the bounded sync facade.
* Pure mapping rules (status table, error codes, OCC symbols, price ticks, snapshot maths) live in
  ``ba2_common.core.ibkr_mapping``; this module only talks to IB.
* ``submit_order`` is NEVER overridden (that was audit finding A1): ``_submit_order_impl`` sends ONE
  order, the template owns validation, transactions and the protective legs, which come from
  ``ba2_common.core.protective_legs.ProtectiveLegsMixin`` (an OCO row is two IB orders in one OCA group).
* TIF: market orders DAY, never GTC; resting orders default GTC (protective stops must survive the close).
* Nothing live is ever defaulted: a missing price/balance is ``None`` or a raise, a failed fetch is
  ``None``/``[]`` plus an ERROR log per the interface's tri-state contract.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from ib_async import IB, Order, Stock
from sqlmodel import or_, select

from ba2_common.core import ibkr_flex as flex
from ba2_common.core import ibkr_mapping as M
from ba2_common.core.ibkr_flex import FlexClient, FlexStatement, default_http_get
from ba2_common.core.protective_legs import OCO_STOP_LIMIT_CUSHION, ProtectiveLegsMixin

from ...core.account_types import (
    AccountSnapshot, MarginInfo, OrderImpact, MARGIN_SOURCE_DEFAULT)
from ...core.db import InstanceNotFound, add_instance, get_db, get_instance, update_instance
from ...core.interfaces import AccountInterface
from ...core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ...core.models import Position, TradingOrder, Transaction
from ...core.types import (
    AssetClass as CoreAssetClass, BrokerOrderErrorReason, OrderDirection, OrderOpenType,
    OrderStatus)
from ...core.types import OrderType as CoreOrderType
from ...logger import logger
from .ibkr_options import IBKROptionsMixin
from .ibkr_runtime import (
    IBKRConnectionError, IBKRContractError, IBKRError, IBKROrderRejected, IBKROrphanStop,
    IBKRReadOnlyError, IBKRRuntime, get_runtime, registry_signature, shutdown_runtime)


class _ZeroQuantityAfterRounding(Exception):
    """Rounding the quantity onto IB's grid left nothing to send: a SKIP, not a failure."""


class IBKRMissingMark(IBKRError):
    """A held position has no usable market price; the book cannot be reported truthfully."""


# ---------------------------------------------------------------------------
# Plain snapshots of ib_async objects (copied on the IB loop thread, read anywhere)
# ---------------------------------------------------------------------------

def _opt_float(value: Any) -> Optional[float]:
    return M.ib_number(value)


@dataclass(frozen=True)
class BrokerOrderView:
    """An IB order/trade copied into plain values on the loop thread.

    ib_async mutates its Trade objects from the loop thread; copying here means DB code on other
    threads never reads a half-updated object.
    """
    order_id: int
    perm_id: int
    order_ref: str
    account: str
    status: str
    filled: float
    remaining: float
    avg_fill_price: Optional[float]
    action: str
    order_type: str
    total_quantity: float
    limit_price: Optional[float]
    aux_price: Optional[float]
    tif: str
    oca_group: str
    sec_type: str
    con_id: int
    symbol: str
    local_symbol: str
    created_at: Optional[datetime]
    last_message: str = ""
    client_id: int = 0
    #: a BAG combo's legs as ``(conId, ratio, action)`` (empty for anything else)
    combo_legs: Tuple = ()

    @property
    def key(self) -> Tuple:
        return ("perm", self.perm_id) if self.perm_id else ("oid", self.order_id)

    @property
    def broker_order_id(self) -> str:
        return M.format_broker_order_id(self.perm_id, self.order_id)

    @classmethod
    def from_trade(cls, trade: Any) -> "BrokerOrderView":
        order, status, contract = trade.order, trade.orderStatus, trade.contract
        log = list(getattr(trade, "log", None) or [])
        created = log[0].time if log and getattr(log[0], "time", None) else None
        message = ""
        for entry in reversed(log):
            if getattr(entry, "message", ""):
                message = entry.message
                break
        total = float(M.ib_number(order.totalQuantity) or 0.0)
        filled = float(M.ib_number(status.filled) or 0.0)
        remaining = float(M.ib_number(status.remaining) or 0.0)
        if filled <= 0:
            # A COMPLETED order (reqCompletedOrders, e.g. after a restart) is built by ib_async with an
            # EMPTY status record (filled 0, avgFillPrice 0); the real figure rides on the order itself.
            traded = M.ib_number(getattr(order, "filledQuantity", None))
            if traded is not None and traded > 0:
                filled = traded
                remaining = 0.0 if str(status.status or "") == "Filled" else max(0.0, total - traded)
        return cls(
            order_id=int(order.orderId or 0),
            perm_id=int(getattr(status, "permId", 0) or getattr(order, "permId", 0) or 0),
            order_ref=str(order.orderRef or ""),
            account=str(order.account or getattr(status, "account", "") or ""),
            status=str(status.status or ""),
            filled=filled,
            remaining=remaining,
            avg_fill_price=M.ib_number(status.avgFillPrice),
            action=str(order.action or ""),
            order_type=str(order.orderType or ""),
            total_quantity=total,
            limit_price=M.ib_number(order.lmtPrice),
            aux_price=M.ib_number(order.auxPrice),
            tif=str(order.tif or ""),
            oca_group=str(order.ocaGroup or ""),
            sec_type=str(contract.secType or ""),
            con_id=int(contract.conId or 0),
            symbol=str(contract.symbol or ""),
            local_symbol=str(contract.localSymbol or ""),
            created_at=created,
            last_message=message,
            client_id=int(getattr(status, "clientId", 0) or getattr(order, "clientId", 0) or 0),
            combo_legs=tuple((int(leg.conId), int(leg.ratio), str(leg.action))
                             for leg in (getattr(contract, "comboLegs", None) or [])),
        )


@dataclass
class _Resolved:
    """A qualified stock contract plus the details we read once (24 h cache)."""
    contract: Any
    details: Any
    fetched_at: float


@dataclass
class OrderBook:
    views: List[BrokerOrderView]
    open_ok: bool
    completed_ok: bool
    #: execution aggregates keyed by orderRef and by permId: {"shares", "price", "when"}
    executions_by_ref: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    executions_by_perm: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    #: option-leg executions of a combo, keyed by (permId, OCC symbol)
    executions_by_perm_occ: Dict[Tuple[int, str], Dict[str, Any]] = field(default_factory=dict)
    executions_ok: bool = True
    #: per-LEG buckets (a combo's legs are separate executions sharing the combo's permId / orderRef):
    #: ``(permId, conId)``, ``(orderRef, conId)`` and ``(orderRef, OCC)``; each carries ``side``
    executions_by_perm_con: Dict[Tuple[int, int], Dict[str, Any]] = field(default_factory=dict)
    executions_by_ref_con: Dict[Tuple[str, int], Dict[str, Any]] = field(default_factory=dict)
    executions_by_ref_occ: Dict[Tuple[str, str], Dict[str, Any]] = field(default_factory=dict)
    #: view keys IB ITSELF listed (open / completed orders): a session-only trade is a LOCAL object
    open_keys: set = field(default_factory=set)
    completed_keys: set = field(default_factory=set)

    @property
    def complete(self) -> bool:
        return self.open_ok and self.completed_ok and self.executions_ok


class _SupportsTrading:
    """``True`` when read from the CLASS (the settings UI and registry read it there), but on an
    INSTANCE it is ``not read_only``: a read-only account must tell ``TradeManager`` it cannot trade,
    so orders are refused loudly BEFORE routing instead of ending as ERROR rows."""

    def __get__(self, obj, owner=None):
        if obj is None:
            return True
        try:
            return not obj._runtime().read_only
        except Exception:  # noqa: BLE001 -- unreadable settings: refuse, never assume writable
            return False


class IBKRAccount(ProtectiveLegsMixin, IBKROptionsMixin, AccountInterface, OptionsAccountInterface):
    """Interactive Brokers via TWS / IB Gateway (see the module docstring)."""

    supports_trading = _SupportsTrading()
    #: An expert may not size from cash / net liquidation when IBKR's buying power cannot be derived
    #: (``AvailableFunds`` is far below net liquidation on a margin account): sizing is refused instead.
    buying_power_is_mandatory = True

    #: Overridable in tests with a fake; the runtime builds the IB object ON its loop thread.
    _ib_factory = staticmethod(IB)
    _CONNECT_TIMEOUT = 15.0
    _CONNECT_COOLDOWN = 15.0

    #: Seconds. Reads, order acknowledgement, and the cancel acknowledgement.
    _READ_TIMEOUT = 20.0
    _ORDER_ACK_TIMEOUT = 10.0
    _CANCEL_ACK_TIMEOUT = 3.0
    #: A contract's details change rarely; prices never come from this cache.
    _CONTRACT_TTL = 24 * 60 * 60
    _PRICE_CHUNK = 80
    _RESOLVE_CONCURRENCY = 20
    #: A freshly submitted order may not be on IB's lists yet.
    _ABSENT_GRACE_MINUTES = 5.0
    #: A row IB never ACKNOWLEDGED (PENDING_NEW) is judged "never reached IBKR" only after this long, and
    #: only against a fully read order book (open + completed + executions).
    _UNACKNOWLEDGED_GRACE_MINUTES = 10.0
    #: A modification is confirmed by IB's own echo; if none arrives within this window (or a third of the
    #: acknowledgement timeout, whichever is shorter) the open order is re-read and its prices compared.
    _MODIFY_FAST_WINDOW = 1.0
    _MODIFY_REREAD_INTERVAL = 0.5
    #: Executions are only available for roughly a week (TWS Trade Log).
    _EXECUTION_WINDOW_DAYS = 7
    #: Primary exchanges accepted when a symbol resolves to several listings.
    _US_PRIMARY_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "IEX", "NYSE ARCA"})

    # ------------------------------------------------------------------ lifecycle
    def __init__(self, id: int):
        super().__init__(id)
        self._authentication_error: Optional[str] = None
        self._rt: Optional[IBKRRuntime] = None
        try:
            self._validate_settings()
        except Exception as e:
            self._authentication_error = str(e)
            logger.error(f"IBKRAccount {id}: {e}", exc_info=True)
            raise
        self._runtime()  # starts the loop thread; does NOT connect (first call connects)

    def _validate_settings(self) -> None:
        required = ["host", "port", "client_id", "account_id", "paper_account"]
        missing = []
        for key in required:
            try:
                value = self.get_setting_with_interface_default(key, log_warning=False)
            except ValueError:
                value = None
            if value is None or value == "":
                missing.append(key)
        if missing:
            raise ValueError(f"Missing required settings: {', '.join(missing)}")

    def _invalidate_settings_cache(self) -> None:
        """A settings edit: drop the cached settings AND this object's runtime handle, so the next
        call rebuilds the signature from the new settings and ``get_runtime`` replaces (and closes)
        the old session. Calls still waiting on the old one fail at once (see ``IBKRRuntime.close``)."""
        super()._invalidate_settings_cache()
        self._rt_prev = getattr(self, "_rt", None)
        self._rt = None

    def _runtime(self) -> IBKRRuntime:
        """This account's shared runtime (one TWS session per account definition, however many
        ``IBKRAccount`` objects exist for it)."""
        rt = getattr(self, "_rt", None)
        if threading.current_thread().name.startswith("ibkr-loop-"):
            # Called from INSIDE a coroutine: never rebuild here. A rebuild closes the old runtime, which
            # is the very thread this code runs on (it would join itself). A settings edit mid-call
            # leaves the call on the runtime it started on; the next facade call picks up the new one.
            rt = rt or getattr(self, "_rt_prev", None)
            if rt is None:
                raise IBKRConnectionError(
                    f"[Account {self.id}] the IBKR runtime was replaced (settings changed) while a call "
                    f"was in flight; retry")
            return rt
        if rt is not None and not rt.closed:
            return rt
        if rt is not None:
            # replaced elsewhere (settings edit / shutdown): this object's cached settings may be
            # stale, so re-read them before rebuilding rather than resurrecting the old session
            self._settings_cache = None
            self._rt = None
        from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool
        get = lambda k: self.get_setting_with_interface_default(k, log_warning=False)  # noqa: E731
        host, port, client_id = str(get("host")), int(get("port")), int(get("client_id"))
        account_id = str(get("account_id")).strip()
        paper, read_only = coerce_bool(get("paper_account")), coerce_bool(get("read_only"))
        self._warn_if_client_id_changes(client_id, account_id)
        factory = self._ib_factory
        # The factory OBJECT (not its id: an id can be reused after garbage collection and would
        # make a stale runtime look current).
        signature = (host, port, client_id, account_id, paper, read_only, factory,
                     self._CONNECT_TIMEOUT, self._CONNECT_COOLDOWN)
        self._rt = get_runtime(self.id, signature, lambda: IBKRRuntime(
            label=f"IBKR account {self.id}", ib_factory=factory, host=host, port=port,
            client_id=client_id, account_id=account_id, paper=paper, read_only=read_only,
            connect_timeout=self._CONNECT_TIMEOUT, cooldown=self._CONNECT_COOLDOWN))
        return self._rt

    def _warn_if_client_id_changes(self, client_id: int, account_id: str) -> None:
        """Orders belong to the API CLIENT that placed them: after a client-id change this session can
        no longer modify or cancel what the old one left working (protective stops included), and
        ``refresh_orders`` stops matching them. Say so loudly when any are still live."""
        previous = registry_signature(self.id)
        if previous is None or (previous[2] == client_id and previous[3] == account_id):
            return
        terminal = OrderStatus.get_terminal_statuses() | {OrderStatus.FILLED}
        with get_db() as session:
            live = session.exec(select(TradingOrder).where(
                TradingOrder.account_id == self.id, TradingOrder.broker_order_id.is_not(None),
                TradingOrder.status.not_in(list(terminal)))).all()
            live_ids = [r.id for r in live]
        if live_ids:
            logger.error(
                f"[Account {self.id}] IBKR client id/account changed ({previous[2]}/{previous[3]} -> "
                f"{client_id}/{account_id}) while {len(live_ids)} order(s) are still LIVE under the old "
                f"client id (rows {live_ids[:10]}). The new session CANNOT modify or cancel them "
                f"(protective stops included) and will not match them: cancel them in TWS, or switch the "
                f"client id back.")
        else:
            logger.warning(f"[Account {self.id}] IBKR client id/account changed "
                           f"({previous[2]}/{previous[3]} -> {client_id}/{account_id}); no live orders "
                           f"were tracked under the old one")

    def close(self) -> None:
        """Disconnect this account's shared runtime and stop its loop thread (app shutdown, tests).
        Dropping an account OBJECT never does this: other objects share the connection."""
        shutdown_runtime(self.id)
        self._rt = None

    @property
    def _account_id(self) -> str:
        return self._runtime().account_id

    def _call(self, fn, *, op: str, timeout: Optional[float] = None):
        return self._runtime().call(fn, timeout=timeout or self._READ_TIMEOUT, op=op)

    def _refuse_if_read_only(self, what: str) -> None:
        if self._runtime().read_only:
            raise IBKRReadOnlyError(
                f"[Account {self.id}] this IBKR account is configured read-only; cannot {what}. "
                f"Uncheck 'read_only' (and the Gateway's own Read-Only API option) to trade.")

    # ------------------------------------------------------------------ settings
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "host": {"type": "str", "required": True, "default": "127.0.0.1",
                     "description": "TWS / IB Gateway host"},
            "port": {"type": "int", "required": True, "default": 4002,
                     "description": "API port (Gateway paper 4002, Gateway live 4001, TWS paper "
                                    "7497, TWS live 7496)"},
            "client_id": {"type": "int", "required": True, "default": 1,
                          "description": "API client id; one session per id. Never changed "
                                         "automatically."},
            "account_id": {"type": "str", "required": True,
                           "description": "IBKR account id (paper accounts start with DU)"},
            "paper_account": {"type": "bool", "required": True, "default": True,
                              "description": "Paper-trading account",
                              "tooltip": "Safety rail: checked => the account id must start "
                                         "with DU; unchecked => it must not. A mismatch refuses "
                                         "to connect, so the default (paper) can never route a "
                                         "live account by accident."},
            "read_only": {"type": "bool", "required": False, "default": True,
                          "description": "Read-only (refuse every order)",
                          "tooltip": "Recommended for the first runs. Also set Read-Only API in "
                                     "the Gateway/TWS API settings."},
            "flex_token": {"type": "str", "required": False, "secret": True,
                           "description": "Flex Web Service token (optional)",
                           "tooltip": "Only needed for dividend, cash-transfer and balance "
                                      "history, which the IBKR API does not provide."},
            "flex_query_id": {"type": "str", "required": False,
                              "description": "Flex query id (optional)",
                              "tooltip": "An Activity Flex Query with the Cash Transactions and "
                                         "Equity Summary (base currency) sections."},
        }

    # ------------------------------------------------------------------ small readers
    def _ib_account(self) -> str:
        return self._account_id

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def _market_data_lock(self) -> asyncio.Lock:
        """The runtime-wide lock around any group of market-data requests (loop thread only)."""
        state = self._runtime().state
        if state.data_lock is None:
            state.data_lock = asyncio.Lock()
        return state.data_lock

    # ------------------------------------------------------------------ bounded, serialised requests
    async def _bounded(self, awaitable: Any, what: str, timeout: Optional[float] = None) -> Any:
        """``await`` with a deadline that SAYS WHICH wait expired (a bare ``wait_for`` timeout carries no
        text, and ``concurrent.futures.TimeoutError`` is the builtin one in 3.11)."""
        limit = float(timeout if timeout is not None else self._READ_TIMEOUT)
        try:
            return await asyncio.wait_for(awaitable, limit)
        except asyncio.TimeoutError:
            raise TimeoutError(f"{what} did not answer within {limit:g}s") from None

    async def _locked(self, name: str, factory: Any, what: str, timeout: Optional[float] = None) -> Any:
        """One request of type ``name`` at a time: ib_async keeps ONE pending future per request type,
        so a concurrent identical request would steal this one's answer (and time out)."""
        async with self._runtime().request_lock(name):
            return await self._bounded(factory(), what, timeout)

    async def _open_orders(self, ib: Any) -> List[Any]:
        return list(await self._locked("openOrders", ib.reqAllOpenOrdersAsync,
                                       "open orders (reqAllOpenOrders)"))

    async def _completed_orders(self, ib: Any) -> List[Any]:
        return list(await self._locked("completedOrders", lambda: ib.reqCompletedOrdersAsync(False),
                                       "completed orders (reqCompletedOrders)"))

    async def _executions(self, ib: Any) -> List[Any]:
        return list(await self._locked("executions", ib.reqExecutionsAsync,
                                       "executions (reqExecutions)"))

    # ------------------------------------------------------------------ contracts (loop thread)
    async def _resolve_stock(self, ib: Any, symbol: str) -> _Resolved:
        key = (symbol or "").strip().upper()
        cached = self._runtime().state.contracts.get(key)
        now = time.monotonic()
        if cached is not None and now - cached.fetched_at < self._CONTRACT_TTL:
            return cached
        probe = Stock(M.to_ib_symbol(key), "SMART", "USD")
        details = await self._bounded(ib.reqContractDetailsAsync(probe),
                                      "contract details (reqContractDetails)")
        stocks = [d for d in (details or [])
                  if getattr(d.contract, "secType", "") == "STK"
                  and d.contract.currency == "USD"]
        if not stocks:
            raise IBKRContractError(f"IBKR knows no US stock {symbol!r} (no security definition)")
        if len(stocks) > 1:
            us = [d for d in stocks
                  if (getattr(d.contract, "primaryExchange", "") or "") in self._US_PRIMARY_EXCHANGES]
            if len(us) != 1:
                names = [f"{d.contract.symbol}@{d.contract.primaryExchange}" for d in stocks]
                raise IBKRContractError(
                    f"IBKR contract for {symbol!r} is ambiguous ({names}); refusing to pick a listing")
            stocks = us
        resolved = _Resolved(contract=stocks[0].contract, details=stocks[0], fetched_at=now)
        self._runtime().state.contracts[key] = resolved
        return resolved

    async def _resolve_many(self, ib: Any, symbols: List[str]) -> Dict[str, Optional[_Resolved]]:
        gate = asyncio.Semaphore(self._RESOLVE_CONCURRENCY)
        out: Dict[str, Optional[_Resolved]] = {}

        async def one(sym: str) -> None:
            async with gate:
                try:
                    out[sym] = await self._resolve_stock(ib, sym)
                except IBKRContractError as e:
                    logger.debug(f"[Account {self.id}] {e}")
                    out[sym] = None
                except Exception as e:  # noqa: BLE001 -- a timeout is "unknown", logged
                    logger.warning(f"[Account {self.id}] contract lookup for {sym} failed: "
                                   f"{type(e).__name__}: {e}")
                    out[sym] = None

        await asyncio.gather(*(one(s) for s in symbols))
        return out

    async def _market_rules(self, ib: Any, details: Any) -> List[Tuple[float, float]]:
        """Price-increment rows for a contract (``reqMarketRule`` of its first rule id), cached."""
        ids = [s for s in str(getattr(details, "marketRuleIds", "") or "").split(",") if s.strip()]
        if not ids:
            raise IBKRContractError(
                f"IBKR published no market rule for {getattr(details.contract, 'symbol', '?')}; "
                f"cannot round its prices to a valid tick")
        rule_id = ids[0].strip()
        cached = self._runtime().state.rules.get(rule_id)
        if cached is not None:
            return cached
        rows = await self._bounded(ib.reqMarketRuleAsync(int(rule_id)), "market rule (reqMarketRule)")
        rules = [(float(r.lowEdge), float(r.increment)) for r in (rows or [])]
        if not rules:
            raise IBKRContractError(f"IBKR returned an empty market rule {rule_id}")
        self._runtime().state.rules[rule_id] = rules
        return rules

    # ------------------------------------------------------------------ prices
    @staticmethod
    def _pick_price(ticker: Any, price_type: str) -> Optional[float]:
        """One price from a ticker: the requested type, then mid, last, close. ``None`` when the
        ticker carries nothing usable or its data is DELAYED (never a stale price)."""
        if M.delayed_market_data(getattr(ticker, "marketDataType", None)):
            return None
        bid, ask = M.ib_number(ticker.bid), M.ib_number(ticker.ask)
        bid = bid if bid and bid > 0 else None
        ask = ask if ask and ask > 0 else None
        values = {"bid": bid, "ask": ask,
                  "mid": ((bid + ask) / 2.0) if bid and ask else None,
                  "last": M.ib_number(ticker.last), "close": M.ib_number(ticker.close)}
        # NEVER fall back to a previous close: it is yesterday's price, not a quote. 'close' is
        # returned only when it is asked for by name.
        ladder = [price_type] + [n for n in ("mid", "last") if n != price_type]
        for name in ladder:
            value = values.get(name)
            if value is not None and value > 0:
                return float(value)
        return None

    async def _prices(self, ib: Any, symbols: List[str], price_type: str) -> Dict[str, Optional[float]]:
        resolved = await self._resolve_many(ib, symbols)
        out: Dict[str, Optional[float]] = {s: None for s in symbols}
        live = [(s, r) for s, r in resolved.items() if r is not None]
        for start in range(0, len(live), self._PRICE_CHUNK):
            chunk = live[start:start + self._PRICE_CHUNK]
            try:
                async with self._market_data_lock():
                    tickers = await self._bounded(
                        ib.reqTickersAsync(*[r.contract for _, r in chunk]), "price snapshot (reqTickers)")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Account {self.id}] price snapshot for {len(chunk)} symbols "
                               f"starting at {chunk[0][0]} failed: {type(e).__name__}: {e}")
                continue
            by_con = {int(getattr(t.contract, "conId", 0) or 0): t for t in tickers or []}
            for sym, res in chunk:
                ticker = by_con.get(int(res.contract.conId or 0))
                if ticker is not None:
                    out[sym] = self._pick_price(ticker, price_type)
        return out

    _PRICE_TYPES = ("bid", "ask", "mid", "last", "close")

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type="bid"):
        if price_type not in self._PRICE_TYPES:
            raise ValueError(f"unknown price type {price_type!r}; expected one of {self._PRICE_TYPES}")
        if isinstance(symbol_or_symbols, str):
            symbol = symbol_or_symbols
            try:
                return self._call(lambda ib: self._prices(ib, [symbol], price_type),
                                  op=f"price {symbol}")[symbol]
            except Exception as e:  # noqa: BLE001
                logger.error(f"[Account {self.id}] error getting price for {symbol}: "
                             f"{type(e).__name__}: {e}", exc_info=True)
                return None
        symbols = list(symbol_or_symbols)
        result: Dict[str, Optional[float]] = {s: None for s in symbols}
        try:
            result.update(self._call(
                lambda ib: self._prices(ib, symbols, price_type),
                op=f"prices x{len(symbols)}", timeout=self._READ_TIMEOUT + 0.2 * len(symbols)))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] bulk price fetch failed: {type(e).__name__}: {e}",
                         exc_info=True)
        return result

    def symbols_exist(self, symbols: List[str]) -> Dict[str, bool]:
        wanted = list(symbols)
        try:
            resolved = self._call(lambda ib: self._resolve_many(ib, wanted),
                                  op=f"symbols_exist x{len(wanted)}",
                                  timeout=self._READ_TIMEOUT + 0.1 * len(wanted))
            return {s: resolved.get(s) is not None for s in wanted}
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error checking symbols: {type(e).__name__}: {e}",
                         exc_info=True)
            return {s: False for s in wanted}

    # ------------------------------------------------------------------ account state
    async def _account_numbers(self, ib: Any):
        """Account tags by name. PRIMARY source: the account-updates stream ib_async subscribes to at
        connect (``ib.accountValues``), which IBKR pushes on every change (fills move AvailableFunds at
        once). ``reqAccountSummary`` is only the FALLBACK (first reads before the stream has delivered):
        its subscription refreshes every ~3 minutes, long enough to over-state buying power after a fill."""
        account = self._account_id
        rows = ib.accountValues(account)
        if not any(r.tag == "NetLiquidation" for r in rows):
            rows = await self._locked("accountSummary", lambda: ib.accountSummaryAsync(account),
                                      "account summary (reqAccountSummary)")
        return M.select_account_values(rows, account)

    async def _snapshot_inputs(self, ib: Any):
        numbers, texts = await self._account_numbers(ib)
        long_mv = short_mv = 0.0
        known = True
        for item in ib.portfolio(self._account_id):
            mv = M.ib_number(getattr(item, "marketValue", None))
            if mv is None:
                known = False
                break
            if mv >= 0:
                long_mv += mv
            else:
                short_mv += mv
        return numbers, texts, (long_mv if known else None), (short_mv if known else None)

    def get_account_snapshot(self) -> AccountSnapshot:
        try:
            numbers, texts, long_mv, short_mv = self._call(self._snapshot_inputs, op="account snapshot")
            return M.snapshot_from_account_values(numbers, texts, long_mv, short_mv)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error getting account snapshot: "
                         f"{type(e).__name__}: {e}", exc_info=True)
            return AccountSnapshot()

    def get_balance(self) -> Optional[float]:
        snapshot = self.get_account_snapshot()
        return snapshot.net_liquidation

    def get_account_info(self) -> Dict[str, Any]:
        try:
            numbers, texts = self._call(self._account_numbers, op="account info")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error getting account info: "
                         f"{type(e).__name__}: {e}", exc_info=True)
            return {}   # a FAILED READ is {} (as every adapter); a missing tag below raises
        snap = M.snapshot_from_account_values(numbers, texts, None, None)
        if snap.buying_power is None:
            # No spendable-room figure can be derived (AvailableFunds missing, or a margin account without
            # ExcessLiquidity). Returning a dict WITHOUT it would let the shared expert clamp
            # (MarketExpertInterface._get_actual_available_balance) fall through to 'cash' / net
            # liquidation as if they were the buying power: refuse loudly instead.
            raise IBKRError(
                f"[Account {self.id}] IBKR published no usable buying power (tags present: "
                f"{sorted(numbers)}; needs AvailableFunds, and ExcessLiquidity on a margin account); "
                f"refusing to report the account without it so that no caller substitutes cash or "
                f"net liquidation for it")
        binding = snap.raw.get("bp_binding")
        state = self._runtime().state
        if state.bp_binding != binding:
            state.bp_binding = binding
            logger.info(f"[Account {self.id}] buying power {snap.buying_power:,.2f} is bound by "
                        f"{binding} (components {snap.raw.get('bp_components')})")
        info: Dict[str, Any] = {
            "account_number": self._account_id, "account_type": texts.get("AccountType"),
            "currency": "USD", "supports_trading": self.supports_trading,
            "paper_account": self._runtime().paper, "read_only": self._runtime().read_only,
        }
        # buying_power MUST come first among the spendable keys the experts probe
        # (MarketExpertInterface._get_actual_available_balance): it is AvailableFunds x Reg-T
        # multiplier, never IB's own 4x BuyingPower (kept as ib_buying_power).
        for key, value in (
                ("buying_power", snap.buying_power), ("equity", snap.equity),
                ("net_liquidation", snap.net_liquidation), ("cash", snap.cash),
                ("margin_multiplier", snap.margin_multiplier),
                ("available_funds", snap.raw.get("available_funds")),
                ("excess_liquidity", snap.raw.get("excess_liquidity")),
                ("init_margin_req", snap.raw.get("init_margin_req")),
                ("maint_margin_req", snap.raw.get("maint_margin_req")),
                ("ib_buying_power", snap.raw.get("ib_buying_power"))):
            if value is not None:
                info[key] = value
        return info

    # ------------------------------------------------------------------ positions
    async def _confirm_positions(self, ib: Any) -> None:
        """An unconfirmed position snapshot is NOT an empty book. ib_async reads ``positions()`` from
        a cache a startup sync fills; if that sync timed out the cache is empty and reads as "flat",
        which is what makes reconcilers close transactions and cancel protective orders. So every
        positions read first awaits an explicit ``reqPositions`` round trip; any failure raises and
        the caller reports the fetch as FAILED (``None``)."""
        await self._locked("positions", ib.reqPositionsAsync, "positions (reqPositions)")

    async def _equity_positions_payload(self, ib: Any) -> List[Dict[str, Any]]:
        account = self._account_id
        await self._confirm_positions(ib)
        held = [p for p in ib.positions(account)
                if p.account == account and p.contract.secType == "STK" and p.position]
        portfolio = {int(i.contract.conId): i for i in ib.portfolio(account)}
        today = self._utcnow().date().isoformat()
        need_snapshot = []
        for pos in held:
            item = portfolio.get(int(pos.contract.conId))
            mark = M.ib_number(getattr(item, "marketPrice", None)) if item else None
            prev = self._runtime().state.prev_close.get(M.from_ib_symbol(pos.contract.symbol))
            if mark is None or mark <= 0 or prev is None or prev[0] != today:
                need_snapshot.append(pos.contract)
        tickers: Dict[int, Any] = {}
        if need_snapshot:
            try:
                async with self._market_data_lock():
                    snapshot = await self._bounded(ib.reqTickersAsync(*need_snapshot),
                                                   "position price snapshot (reqTickers)")
                for t in snapshot:
                    tickers[int(t.contract.conId)] = t
            except Exception as e:  # noqa: BLE001 -- only matters if a mark is then missing
                logger.warning(f"[Account {self.id}] position price snapshot failed: {e}")
        out = []
        for pos in held:
            symbol = M.from_ib_symbol(pos.contract.symbol)
            con_id = int(pos.contract.conId)
            item, ticker = portfolio.get(con_id), tickers.get(con_id)
            mark = M.ib_number(getattr(item, "marketPrice", None)) if item else None
            if mark is None or mark <= 0:
                mark = self._pick_price(ticker, "last") if ticker is not None else None
            if mark is None:
                raise IBKRMissingMark(
                    f"no market price for held position {symbol} (portfolio and snapshot both "
                    f"empty); refusing to report the book with a fabricated mark")
            prev_close = None
            if ticker is not None:
                prev_close = M.ib_number(ticker.close)
                if prev_close and prev_close > 0:
                    self._runtime().state.prev_close[symbol] = (today, float(prev_close))
            cached_close = self._runtime().state.prev_close.get(symbol)
            if prev_close is None and cached_close is not None:
                prev_close = cached_close[1]
            qty = float(pos.position)
            market_value = M.ib_number(getattr(item, "marketValue", None)) if item else None
            unrealized = M.ib_number(getattr(item, "unrealizedPNL", None)) if item else None
            avg = float(pos.avgCost)
            out.append({
                "symbol": symbol, "qty": qty, "avg_cost": avg, "mark": float(mark),
                "market_value": market_value if market_value is not None else mark * qty,
                "unrealized": unrealized if unrealized is not None else (mark - avg) * qty,
                "prev_close": prev_close,
                "exchange": str(pos.contract.primaryExchange or pos.contract.exchange or ""),
            })
        return out

    def get_positions(self) -> Optional[List[Position]]:
        """Current EQUITY positions. TRI-STATE: a list; ``[]`` for a CONFIRMED flat account;
        ``None`` when the FETCH FAILED (Gateway down, timeout, a held position with no price).

        Option positions are excluded (their value is multiplier-scaled); read them through
        ``get_option_positions``. ``qty_available`` equals ``qty``: IB publishes no per-order hold.
        """
        try:
            rows = self._call(self._equity_positions_payload, op="positions")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error getting positions: {type(e).__name__}: {e} "
                         f"-- reporting the FETCH FAILURE as None (not a flat account)",
                         exc_info=True)
            return None
        positions = []
        for row in rows:
            qty = abs(row["qty"])
            cost_basis = row["avg_cost"] * qty
            market_value = abs(row["market_value"])
            last_day = row["prev_close"] if row["prev_close"] else row["mark"]
            intraday_pl = (row["mark"] - last_day) * qty * (1 if row["qty"] > 0 else -1)
            positions.append(Position(
                asset_class="Equity", avg_entry_price=row["avg_cost"], avg_entry_swap_rate=None,
                change_today=((row["mark"] - last_day) / last_day) if last_day else 0.0,
                cost_basis=cost_basis, current_price=row["mark"], exchange=row["exchange"],
                lastday_price=last_day, market_value=market_value, qty=qty, qty_available=qty,
                side=OrderDirection.BUY if row["qty"] > 0 else OrderDirection.SELL,
                swap_rate=None, symbol=row["symbol"],
                unrealized_intraday_pl=intraday_pl,
                unrealized_intraday_plpc=(intraday_pl / (last_day * qty)) if last_day and qty else 0.0,
                unrealized_pl=row["unrealized"],
                unrealized_plpc=(row["unrealized"] / cost_basis) if cost_basis else 0.0))
        return positions

    def refresh_positions(self) -> bool:
        positions = self.get_positions()
        if positions is None:
            logger.error(f"[Account {self.id}] error refreshing positions from IBKR: fetch failed")
            return False
        logger.info(f"[Account {self.id}] refreshed {len(positions)} IBKR positions")
        return True

    async def _floating_pl_payload(self, ib: Any) -> Optional[float]:
        account = self._account_id
        total = 0.0
        for item in ib.portfolio(account):
            if item.contract.secType != "STK":
                continue
            value = M.ib_number(getattr(item, "unrealizedPNL", None))
            if value is None:
                return None
            total += value
        return total

    def get_broker_floating_pl(self) -> Optional[float]:
        try:
            return self._call(self._floating_pl_payload, op="floating pl")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] could not read the broker's floating P/L: {e}",
                         exc_info=True)
            return None

    # ------------------------------------------------------------------ order translation
    @staticmethod
    def _map_order_type(ib_type: str, action: str) -> CoreOrderType:
        is_buy = action == "BUY"
        if ib_type in ("MKT", "MOC", "MOO"):
            return CoreOrderType.MARKET
        if ib_type == "LMT":
            return CoreOrderType.BUY_LIMIT if is_buy else CoreOrderType.SELL_LIMIT
        if ib_type == "STP":
            return CoreOrderType.BUY_STOP if is_buy else CoreOrderType.SELL_STOP
        if ib_type == "STP LMT":
            return CoreOrderType.BUY_STOP_LIMIT if is_buy else CoreOrderType.SELL_STOP_LIMIT
        if ib_type == "TRAIL":
            return CoreOrderType.TRAILING_STOP
        logger.warning(f"Unmapped IBKR order type {ib_type!r}; recording MARKET")
        return CoreOrderType.MARKET

    def _view_to_tradingorder(self, view: BrokerOrderView) -> TradingOrder:
        """An UNSAVED TradingOrder describing an IB order (the read-side listing shape)."""
        side = OrderDirection.BUY if view.action == "BUY" else OrderDirection.SELL
        if view.sec_type == "BAG" and view.limit_price is not None and view.limit_price < 0:
            side = OrderDirection.SELL          # a credit combo: the platform's parent side is the net side
        order_type = self._map_order_type(view.order_type,
                                          "BUY" if side == OrderDirection.BUY else "SELL")
        limit, stop = view.limit_price, None
        if view.order_type == "STP":
            stop, limit = view.aux_price, None
        elif view.order_type == "STP LMT":
            stop = view.aux_price
        symbol = M.from_ib_symbol(view.symbol)
        asset = CoreAssetClass.OPTION if view.sec_type in ("OPT", "BAG") else CoreAssetClass.EQUITY
        return TradingOrder(
            account_id=self.id, broker_order_id=view.broker_order_id,
            symbol=symbol, quantity=view.total_quantity, side=side, order_type=order_type,
            good_for=view.tif.lower() or None, limit_price=limit, stop_price=stop,
            status=M.map_ib_status(view.status, view.filled, view.remaining),
            filled_qty=view.filled,
            open_price=view.avg_fill_price if view.avg_fill_price and view.avg_fill_price > 0 else None,
            asset_class=asset,
            contract_symbol=((view.local_symbol.replace(" ", "") or None)
                             if view.sec_type == "OPT" else None),
            comment=None, created_at=view.created_at)

    async def _read_order_book(self, ib: Any) -> OrderBook:
        open_ok = completed_ok = executions_ok = True
        open_trades, completed = [], []
        try:
            open_trades = await self._open_orders(ib)
        except Exception as e:  # noqa: BLE001
            open_ok = False
            logger.error(f"[Account {self.id}] could not read IBKR open orders: {e}", exc_info=True)
        try:
            completed = await self._completed_orders(ib)
        except Exception as e:  # noqa: BLE001
            completed_ok = False
            logger.error(f"[Account {self.id}] could not read IBKR completed orders: {e}",
                         exc_info=True)
        views: Dict[Tuple, BrokerOrderView] = {}
        open_keys, completed_keys = set(), set()
        for trade in list(ib.trades()):
            view = BrokerOrderView.from_trade(trade)
            views.setdefault(view.key, view)
        for trade in open_trades:
            view = BrokerOrderView.from_trade(trade)
            open_keys.add(view.key)
            views.setdefault(view.key, view)
        for trade in completed:
            view = BrokerOrderView.from_trade(trade)
            completed_keys.add(view.key)
            views.setdefault(view.key, view)
        book = OrderBook(views=list(views.values()), open_ok=open_ok, completed_ok=completed_ok,
                         open_keys=open_keys, completed_keys=completed_keys)
        try:
            self._aggregate_executions(book, await self._executions(ib))
        except Exception as e:  # noqa: BLE001
            book.executions_ok = False
            logger.error(f"[Account {self.id}] could not read IBKR executions: {e}", exc_info=True)
        return book

    @classmethod
    def _aggregate_executions(cls, book: OrderBook, fills: List[Any]) -> None:
        for fill in fills or []:
            ex = fill.execution
            shares, price = float(ex.shares), float(ex.price)
            if shares <= 0:
                continue
            buckets = [(book.executions_by_ref, str(ex.orderRef or "")),
                       (book.executions_by_perm, int(ex.permId or 0))]
            if ex.permId and fill.contract.conId:
                buckets.append((book.executions_by_perm_con, (int(ex.permId), int(fill.contract.conId))))
            if ex.orderRef and fill.contract.conId:
                buckets.append((book.executions_by_ref_con, (str(ex.orderRef), int(fill.contract.conId))))
            if fill.contract.secType == "OPT" and ex.permId:
                try:
                    occ = cls._occ_of(fill.contract)
                    buckets.append((book.executions_by_perm_occ, (int(ex.permId), occ)))
                    if ex.orderRef:
                        buckets.append((book.executions_by_ref_occ, (str(ex.orderRef), occ)))
                except ValueError:
                    logger.warning(f"execution {ex.execId}: cannot derive an OCC symbol from "
                                   f"{fill.contract.localSymbol!r}; leg price not attributed")
            for bucket, key in buckets:
                if not key:
                    continue
                agg = bucket.setdefault(key, {"shares": 0.0, "notional": 0.0, "when": None,
                                              "side": str(ex.side)})
                agg["shares"] += shares
                agg["notional"] += shares * price
                agg["when"] = ex.time
        for bucket in (book.executions_by_ref, book.executions_by_perm, book.executions_by_perm_occ,
                       book.executions_by_perm_con, book.executions_by_ref_con, book.executions_by_ref_occ):
            for agg in bucket.values():
                agg["price"] = agg["notional"] / agg["shares"] if agg["shares"] else None

    @staticmethod
    def _combo_net_price(legs, book: OrderBook) -> Optional[float]:
        """The net price PER COMBO UNIT of a filled combo, from its legs' own executions: the sum over legs
        of ``side x leg average price x ratio`` (bought +, sold -; a debit is positive, a credit negative,
        the platform's convention). ``legs`` = ``[(agg, ratio), ...]``. NEVER an average of the legs: a debit
        spread bought at 5.00 and sold at 2.00 cost 3.00, not 3.50. ``None`` when any leg has no price."""
        total = 0.0
        for agg, ratio in legs:
            if not agg or not agg.get("price"):
                return None
            total += (1.0 if str(agg.get("side")) == "BOT" else -1.0) * float(agg["price"]) * ratio
        return total

    @classmethod
    def _combo_from_view(cls, view: BrokerOrderView, book: OrderBook) -> Optional[float]:
        if not view.combo_legs:
            return None
        legs = []
        for con_id, ratio, _action in view.combo_legs:
            agg = ((book.executions_by_perm_con.get((view.perm_id, con_id)) if view.perm_id else None)
                   or book.executions_by_ref_con.get((view.order_ref, con_id)))
            legs.append((agg, ratio))
        return cls._combo_net_price(legs, book)

    def _combo_from_children(self, row: TradingOrder, children: List[TradingOrder], ref: str, perm: int,
                             book: OrderBook) -> Optional[Tuple[float, Optional[float]]]:
        """``(filled combo units, net price per unit)`` of a combo PARENT row from its leg children's
        executions, or ``None`` when any leg has none (a half-executed combo is not settled from here).
        Units are the SMALLEST leg fill divided by that leg's ratio, never the sum of the legs' shares."""
        units, legs = None, []
        for child in children:
            if not child.contract_symbol or not row.quantity:
                return None
            agg = ((book.executions_by_perm_occ.get((perm, child.contract_symbol)) if perm else None)
                   or book.executions_by_ref_occ.get((ref, child.contract_symbol)))
            if not agg:
                return None
            ratio = float(child.quantity) / float(row.quantity)
            leg_units = agg["shares"] / ratio
            units = leg_units if units is None else min(units, leg_units)
            agg = dict(agg, side=("BOT" if child.side == OrderDirection.BUY else "SLD"))
            legs.append((agg, ratio))
        return (units, self._combo_net_price(legs, book)) if units is not None else None

    @staticmethod
    def _status_wanted(status: Any) -> Optional[set]:
        """Platform statuses a ``get_orders`` filter selects; ``None`` = everything."""
        if status is None:
            return None
        status = OrderStatus(status) if isinstance(status, str) else status
        if status == OrderStatus.ALL:
            return None
        if status == OrderStatus.OPEN:
            return {OrderStatus.PENDING_NEW, OrderStatus.PENDING_CANCEL, OrderStatus.ACCEPTED,
                    OrderStatus.PARTIALLY_FILLED}
        if status == OrderStatus.CLOSED:
            return {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED,
                    OrderStatus.EXPIRED}
        return {status}

    def get_orders(self, status: Optional[Any] = None) -> Any:
        wanted = self._status_wanted(status)  # outside the try: a bad filter is a caller bug
        try:
            book = self._call(self._read_order_book, op="orders", timeout=40.0)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error getting orders: {type(e).__name__}: {e}",
                         exc_info=True)
            return []
        orders = []
        for view in book.views:
            if view.account and view.account != self._account_id:
                continue
            order = self._view_to_tradingorder(view)
            if wanted is None or order.status in wanted:
                orders.append(order)
        return orders

    def get_order(self, order_id: str) -> Any:
        broker_id = str(order_id).strip()
        try:
            book = self._call(self._read_order_book, op=f"order {broker_id}", timeout=40.0)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error getting order {order_id}: {e}", exc_info=True)
            return None
        for view in book.views:
            if broker_id == view.broker_order_id or M.broker_id_matches(
                    broker_id, view.perm_id, view.order_id):
                return self._view_to_tradingorder(view)
        return None

    # ------------------------------------------------------------------ errors
    def _classify_order_error(self, exc: Exception) -> BrokerOrderErrorReason:
        if isinstance(exc, IBKRReadOnlyError):
            return BrokerOrderErrorReason.UNAUTHORIZED
        if isinstance(exc, IBKRContractError):
            return BrokerOrderErrorReason.INVALID_SYMBOL
        if isinstance(exc, IBKROrderRejected):
            return M.classify_ib_error(exc.code, str(exc))
        return BrokerOrderErrorReason.UNKNOWN

    # ------------------------------------------------------------------ order building
    @staticmethod
    def _is_market(order_type: CoreOrderType) -> bool:
        return order_type == CoreOrderType.MARKET

    def _tradable_quantity(self, symbol: str, details: Any, quantity: float) -> float:
        """The quantity IB will take: whole shares unless the symbol is positively fractional.

        Rounding is always DOWN (up would spend buying power nobody budgeted). Unknown
        eligibility floors to whole shares, exactly as TastyTrade does.
        """
        requested = Decimal(str(quantity))
        if requested <= 0:
            raise _ZeroQuantityAfterRounding(f"quantity {quantity} for {symbol} is not positive")
        fractional = M.fractionable_from_size(getattr(details, "minSize", None),
                                              getattr(details, "sizeIncrement", None))
        if fractional is True:
            step = M.ib_number(getattr(details, "sizeIncrement", None)) or \
                M.ib_number(getattr(details, "minSize", None))
            grid = Decimal(str(step))
            allowed = (requested / grid).to_integral_value(rounding=ROUND_DOWN) * grid
        else:
            allowed = requested.quantize(Decimal("1"), rounding=ROUND_DOWN)
        if allowed <= 0:
            raise _ZeroQuantityAfterRounding(
                f"qty {quantity} for {symbol} floors to 0 on IBKR's grid "
                f"(fractional eligibility={fractional!r}) -- nothing submitted")
        if allowed != requested:
            logger.warning(f"[Account {self.id}] {symbol}: requested qty {requested} is not "
                           f"tradable (fractional eligibility={fractional!r}); submitting {allowed}")
        return float(allowed)

    @staticmethod
    def _refuse_fractional_priced_order(symbol: str, qty: float, order_type: CoreOrderType) -> None:
        priced_not_allowed = {CoreOrderType.BUY_STOP, CoreOrderType.SELL_STOP,
                              CoreOrderType.BUY_STOP_LIMIT, CoreOrderType.SELL_STOP_LIMIT,
                              CoreOrderType.OCO}
        if order_type in priced_not_allowed and float(qty) != int(qty):
            raise ValueError(
                f"IBKR will not take the fractional quantity {qty} of {symbol} on a "
                f"{order_type.value} order (fractional orders: market or limit only). Send it as a "
                f"market/limit order or round to whole shares.")

    def _new_ib_order(self, *, action: str, order_type: str, qty: float, tif: str, ref: str,
                      limit: Optional[float] = None, stop: Optional[float] = None,
                      rules: Optional[List[Tuple[float, float]]] = None) -> Order:
        order = Order()
        order.action, order.orderType, order.totalQuantity = action, order_type, qty
        order.tif, order.orderRef, order.account = tif, ref, self._account_id
        order.outsideRth, order.transmit = False, True
        if order_type == "LMT":
            order.lmtPrice = M.round_to_increment(limit, rules)
        elif order_type == "STP":
            order.auxPrice = M.round_to_increment(stop, rules)
        elif order_type == "STP LMT":
            order.auxPrice = M.round_to_increment(stop, rules)
            order.lmtPrice = M.round_to_increment(limit, rules)
        elif order_type != "MKT":
            raise ValueError(f"unsupported IB order type {order_type!r}")
        return order

    # ------------------------------------------------------------------ placement (loop thread)
    async def _wait_ack(self, ib: Any, trade: Any, timeout: float, seq: int) -> BrokerOrderView:
        """Wait for IB to acknowledge or reject a placed order (bounded; never resends).

        Whether an order FAILED is decided from its STATUS, exactly as ib_async decides it: ib_async turns
        a live trade ``Cancelled`` on any non-warning error (and ``Inactive``/``ApiCancelled`` come from
        TWS itself); a WARNING (105 110 165 321 329 399 404 434 492 and every 21xx) only sets the status
        ``ValidationError`` and the order stays LIVE. So:

        * ``Cancelled``/``Inactive``/``ApiCancelled`` -> ``IBKROrderRejected`` carrying the error that
          explains it (errors are read by sequence: only those after ``seq``, for this order id) -- unless
          the order already TRADED (price protection can cancel after a partial fill): that is returned as
          the fact it is, never raised as an error;
        * ``Submitted``/``PreSubmitted``/``Filled`` -> the acknowledgement;
        * anything else (``PendingSubmit``/``ValidationError``) keeps waiting; warnings are logged; one
          exception: a 321 'read-only' warning means the Gateway refuses API orders, so the order was
          never placed (ib_async leaves the trade in limbo), which is a refusal, not a hang;
        * at the deadline the current view is returned (the caller records ``PENDING_NEW`` and
          ``refresh_orders`` resolves it).
        """
        order_id = int(trade.order.orderId)
        deadline = time.monotonic() + timeout
        rt = self._runtime()
        logged: set = set()
        while True:
            status = str(trade.orderStatus.status or "")
            if status in M.IB_REJECTION_STATUSES:
                view = BrokerOrderView.from_trade(trade)
                if view.filled > 0:
                    logger.warning(f"[Account {self.id}] IB order {order_id} ended {status} AFTER trading "
                                   f"{view.filled:g}; recording the fill, not an error")
                    return view
                code, text = self._rejection_reason(rt, order_id, seq, trade, status)
                raise IBKROrderRejected(f"IB error {code}: {text}" if code else f"IBKR {status}: {text}",
                                        code)
            if status in ("Submitted", "PreSubmitted", "Filled", "ApiUpdate"):
                return BrokerOrderView.from_trade(trade)
            for code, text in rt.order_warnings(order_id, seq):
                if (code, text) in logged:
                    continue
                logged.add((code, text))
                logger.warning(f"[Account {self.id}] IB warning {code} on order {order_id} "
                               f"(the order stays LIVE): {text}")
                if code == 321 and "read-only" in text.lower():
                    raise IBKROrderRejected(f"IB error 321: {text}", 321)
            if time.monotonic() >= deadline:
                return BrokerOrderView.from_trade(trade)
            await asyncio.sleep(0.02)

    @staticmethod
    def _rejection_reason(rt: IBKRRuntime, order_id: int, seq: int, trade: Any, status: str):
        """The (code, text) that explains a dead order: the last failing error after ``seq``, else the last
        message of any kind, else the trade log's own words."""
        failing = rt.order_errors(order_id, seq)
        anything = rt.order_errors(order_id, seq, kinds=None)
        for pool in (failing, anything):
            if pool:
                return pool[-1]
        text = next((e.message for e in reversed(list(trade.log or [])) if getattr(e, "message", "")), status)
        return None, text

    async def _short_check(self, ib: Any, contract: Any, symbol: str) -> None:
        """Refuse to open a short unless IB reports the stock shortable AND easy to borrow."""
        async with self._market_data_lock():
            ticker = ib.reqMktData(contract, "236", False, False)
            try:
                deadline = time.monotonic() + 3.0
                while (M.ib_number(getattr(ticker, "shortableShares", None)) is None
                       and time.monotonic() < deadline):
                    await asyncio.sleep(0.05)
                shares = getattr(ticker, "shortableShares", None)
            finally:
                ib.cancelMktData(contract)
        if M.ib_number(shares) is None:
            raise ValueError(
                f"Cannot open SHORT position for {symbol}: IBKR published no shortable-shares "
                f"figure, so shortability is unknown. Refusing rather than assuming.")
        if not M.is_easy_to_borrow(shares):
            raise ValueError(
                f"Cannot open SHORT position for {symbol}: IBKR reports shortable shares "
                f"{shares} (needs > {M.EASY_TO_BORROW_THRESHOLD}, i.e. shortable and easy to borrow).")

    def _submit_budget(self, acks: int, *, reads: int = 2, cancel_waits: int = 0,
                       short_check: bool = True) -> float:
        """The caller's total wait for one placement call, split EXPLICITLY: ``reads`` request/response
        reads (contract details, market rule), the short check (3 s), ``acks`` acknowledgement waits and
        ``cancel_waits`` cancel confirmations. The adopt-before-place lookup is its OWN call
        (``_lookup_budget``), made only for a row that already had a nonce."""
        return (reads * self._READ_TIMEOUT + (3.0 if short_check else 0.0)
                + acks * self._ORDER_ACK_TIMEOUT + cancel_waits * self._CANCEL_ACK_TIMEOUT)

    def _lookup_budget(self) -> float:
        """Budget of the prior-order lookup: the open and the completed list, each of which may first wait
        for another caller's request of the same type to finish (see ``_locked``)."""
        return 2 * 2 * self._READ_TIMEOUT

    async def _find_refs(self, ib: Any, refs: List[str]) -> Dict[str, List[BrokerOrderView]]:
        """Every IB order carrying one of ``refs`` (session trades, open orders, completed orders), per
        ref. A list that cannot be read RAISES: an unverifiable book is not an empty one."""
        account = self._account_id
        found: Dict[str, List[BrokerOrderView]] = {r: [] for r in refs}
        seen: set = set()

        def collect(trades) -> None:
            for trade in trades:
                if trade.order.orderRef in found:
                    view = BrokerOrderView.from_trade(trade)
                    if (view.account and view.account != account) or view.key in seen:
                        continue
                    seen.add(view.key)
                    found[trade.order.orderRef].append(view)

        collect(ib.trades())
        collect(await self._open_orders(ib))
        collect(await self._completed_orders(ib))
        return found

    @staticmethod
    def _quantity_matches(have: float, wanted: float) -> bool:
        """IB's quantity is the requested one, or that rounded DOWN to whole shares."""
        return abs(have - wanted) < 1e-6 or (wanted >= 1 and abs(have - math.floor(wanted)) < 1e-6)

    def _classify_prior(self, views: List[BrokerOrderView], *, action: str, ib_type: str,
                        qty: float) -> Tuple[str, Optional[BrokerOrderView]]:
        """What the orders already carrying a ref mean for a (re)submission:

        * ``adopt``    a still-working (or already traded) order that MATCHES side/type/quantity;
        * ``conflict`` such an order exists but differs: never adopted, never duplicated;
        * ``dead``     only orders that died without trading (rejected/cancelled): their ref is spent;
        * ``none``     nothing under this ref.
        """
        adoptable = [v for v in views if v.status not in M.IB_REJECTION_STATUSES or v.filled > 0]
        for view in adoptable:
            if (view.action == action and view.order_type == ib_type
                    and self._quantity_matches(view.total_quantity, qty)):
                return "adopt", view
        if adoptable:
            return "conflict", adoptable[0]
        return ("dead", None) if views else ("none", None)

    def _resolve_prior(self, legs: List[Dict[str, Any]], what: str
                       ) -> Dict[str, Tuple[str, Optional[BrokerOrderView]]]:
        """Look up the orders already carrying each leg's orderRef and classify them. Only called for a
        row that ALREADY HAD a nonce (a first attempt cannot have an order at IB). Raises when a live
        order under the ref differs from what the row wants."""
        refs = [leg["ref"] for leg in legs]
        found = self._call(lambda ib: self._find_refs(ib, refs), op=f"look up prior orders of {what}",
                           timeout=self._lookup_budget())
        out: Dict[str, Tuple[str, Optional[BrokerOrderView]]] = {}
        for leg in legs:
            kind, view = self._classify_prior(found[leg["ref"]], action=leg["action"],
                                              ib_type=leg["ib_type"], qty=leg["qty"])
            if kind == "conflict":
                raise IBKROrderRejected(
                    f"an IB order with this orderRef is live but differs ({view.action} "
                    f"{view.total_quantity:g} {view.order_type}, {view.status}; this row wants "
                    f"{leg['action']} {leg['qty']:g} {leg['ib_type']}); not adopted, and no second order "
                    f"is placed: {leg['ref']}")
            out[leg["name"]] = (kind, view)
        return out

    @staticmethod
    def _trade_by_ref(ib: Any, ref: str) -> Optional[Any]:
        for trade in ib.trades():
            if trade.order.orderRef == ref:
                return trade
        return None

    async def _listed_by_ib(self, ib: Any, trade: Any) -> bool:
        """Does IB itself list this order as open? (A fresh ``reqAllOpenOrders``: TWS answers with an
        ``openOrder`` + ``orderStatus`` per working order, which also restores a status that ib_async
        changed LOCALLY.)"""
        listed = await self._open_orders(ib)
        oid = int(trade.order.orderId or 0)
        perm = int(trade.orderStatus.permId or trade.order.permId or 0)
        own_client = self._runtime().client_id
        for t in listed:
            if t is trade:
                return True
            t_perm = int(t.orderStatus.permId or t.order.permId or 0)
            if perm and t_perm:
                if t_perm == perm:               # permId is global: the same order, whoever placed it
                    return True
                continue
            # no permId on one side: an orderId is only unique PER CLIENT (another API client on the same
            # Gateway can hold the same number), so it counts only together with our own clientId
            t_client = int(getattr(t.orderStatus, "clientId", 0) or getattr(t.order, "clientId", 0) or 0)
            if oid and int(t.order.orderId or 0) == oid and t_client == own_client:
                return True
        return False

    async def _cancel_trade_confirmed(self, ib: Any, trade: Any) -> str:
        """Cancel one order and WAIT for IB to say so: ``"cancelled"``, ``"filled"`` (it traded first) or
        ``"unconfirmed"`` (no answer within the cancel window: it may still be live).

        A LOCAL ``Cancelled`` is never trusted on its own: ib_async sets it when IB refuses a
        MODIFICATION of a live order with a non-warning error, although the order is still working. So a
        trade that looks final is first checked against IB's open-order list; one IB still lists gets a
        real cancel, and "cancelled" is only returned when IB stops listing it or its status moves."""
        status = str(trade.orderStatus.status or "")
        distrust = False
        if status in M.IB_REJECTION_STATUSES:
            distrust = await self._listed_by_ib(ib, trade)
            status = str(trade.orderStatus.status or "")
            if status in M.IB_REJECTION_STATUSES and not distrust:
                return "cancelled"
            if status not in M.IB_REJECTION_STATUSES:
                distrust = False
        if status == "Filled":
            return "filled"
        seq = self._runtime().mark()
        ib.cancelOrder(trade.order)
        deadline = time.monotonic() + self._CANCEL_ACK_TIMEOUT
        while time.monotonic() < deadline:
            status = str(trade.orderStatus.status or "")
            if any(c in M.ORDER_STATE_CODES for c, _ in self._runtime().order_errors(
                    int(trade.order.orderId), seq, kinds=("order", "cancelled"))):
                return "unconfirmed"             # IB REFUSED the cancel (e.g. 10148): it may still work
            if distrust:
                if status in M.IB_REJECTION_STATUSES:
                    if not await self._listed_by_ib(ib, trade):
                        return "cancelled"
                    await asyncio.sleep(0.3)
                    continue
                distrust = False
            if status in M.IB_REJECTION_STATUSES:
                return "cancelled"
            if status == "Filled":
                return "filled"
            await asyncio.sleep(0.02)
        return "unconfirmed"

    async def _cancel_views_confirmed(self, ib: Any, views: List[BrokerOrderView]) -> List[str]:
        """Confirmed-cancel each view's order (found by orderRef among this session's trades or the open
        list); the outcomes, in order."""
        outcomes: List[str] = []
        for view in views:
            trade = self._trade_by_ref(ib, view.order_ref)
            if trade is None:
                await self._open_orders(ib)
                trade = self._trade_by_ref(ib, view.order_ref)
            outcomes.append("unconfirmed" if trade is None else await self._cancel_trade_confirmed(ib, trade))
        return outcomes

    async def _place_single(self, ib: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
        progress = spec["progress"]
        resolved = await self._resolve_stock(ib, spec["symbol"])
        details, contract = resolved.details, resolved.contract
        rules = await self._market_rules(ib, details)
        qty = self._tradable_quantity(spec["symbol"], details, spec["quantity"])
        self._refuse_fractional_priced_order(spec["symbol"], qty, spec["order_type"])
        if float(qty) != int(qty) and (spec["tif"] != "DAY"):
            logger.info(f"[Account {self.id}] fractional {spec['symbol']}: forcing DAY "
                        f"(was {spec['tif']})")
            spec = dict(spec, tif="DAY")
        if spec["opens_short"]:
            await self._short_check(ib, contract, spec["symbol"])
        order = self._new_ib_order(
            action=spec["action"], order_type=spec["ib_type"], qty=qty, tif=spec["tif"],
            ref=spec["order_ref"], limit=spec["limit"], stop=spec["stop"], rules=rules)
        seq = self._runtime().mark()
        trade = ib.placeOrder(contract, order)
        progress["order_ids"].append(int(trade.order.orderId))     # from here the order may exist at IB
        view = await self._wait_ack(ib, trade, self._ORDER_ACK_TIMEOUT, seq)
        return {"view": view, "quantity": qty, "tif": spec["tif"]}

    async def _place_oco(self, ib: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Two orders in one OCA group, STOP FIRST then take-profit, both transmitted.

        ``transmit=False`` is a parent/child (bracket) device; it holds nothing back in an OCA group,
        so the pair is NOT atomic at the broker and the order matters: the protective stop must exist
        before the take-profit does. A leg the caller found already live (``adopted_*``) is not placed
        again. If the take-profit fails the stop is cancelled AND THE CANCEL CONFIRMED; when it cannot be
        confirmed (or the stop filled meanwhile) ``IBKROrphanStop`` carries the live order back so the
        caller records it: a live order without a row is never left behind.
        """
        progress = spec["progress"]
        sl_view, tp_view = spec.get("adopted_sl"), spec.get("adopted_tp")
        resolved = await self._resolve_stock(ib, spec["symbol"])
        rules = await self._market_rules(ib, resolved.details)
        qty = float(spec["quantity"])
        self._refuse_fractional_priced_order(spec["symbol"], qty, CoreOrderType.OCO)
        tp = M.round_to_increment(spec["tp"], rules)
        sl_stop = M.round_to_increment(spec["sl"], rules)
        cushion = (1.0 - OCO_STOP_LIMIT_CUSHION) if spec["action"] == "SELL" else (1.0 + OCO_STOP_LIMIT_CUSHION)
        sl_limit = M.round_to_increment(sl_stop * cushion, rules)
        group = spec["oca_group"]
        rt = self._runtime()
        sl_trade = None
        if sl_view is None:
            sl_order = self._new_ib_order(action=spec["action"], order_type="STP LMT", qty=qty,
                                          tif=spec["tif"], ref=spec["sl_ref"], limit=sl_limit,
                                          stop=sl_stop, rules=rules)
            sl_order.ocaGroup, sl_order.ocaType = group, 2     # 2 = reduce remaining, with block
            seq = rt.mark()
            sl_trade = ib.placeOrder(resolved.contract, sl_order)
            progress["order_ids"].append(int(sl_trade.order.orderId))
            progress["sl_id"] = int(sl_trade.order.orderId)
            sl_view = await self._wait_ack(ib, sl_trade, self._ORDER_ACK_TIMEOUT, seq)
        if tp_view is None:
            tp_order = self._new_ib_order(action=spec["action"], order_type="LMT", qty=qty,
                                          tif=spec["tif"], ref=spec["tp_ref"], limit=tp, rules=rules)
            tp_order.ocaGroup, tp_order.ocaType = group, 2
            seq = rt.mark()
            try:
                tp_trade = ib.placeOrder(resolved.contract, tp_order)
                progress["order_ids"].append(int(tp_trade.order.orderId))
                progress["tp_id"] = int(tp_trade.order.orderId)
                tp_view = await self._wait_ack(ib, tp_trade, self._ORDER_ACK_TIMEOUT, seq)
            except IBKROrderRejected as tp_error:
                # The take-profit failed: never leave the stop working alone behind a failed submit, and
                # never ASSUME it is gone -- wait for IB to say so.
                stop_trade = sl_trade or self._trade_by_ref(ib, spec["sl_ref"])
                outcome = ("cancelled" if stop_trade is None
                           else await self._cancel_trade_confirmed(ib, stop_trade))
                if outcome != "cancelled":
                    raise IBKROrphanStop(
                        f"OCO take-profit failed ({tp_error}) and the stop leg is NOT confirmed cancelled "
                        f"(outcome: {outcome}); it may still be working at IB",
                        tp_error.code, view=BrokerOrderView.from_trade(stop_trade), sl_stop=sl_stop,
                        sl_limit=sl_limit, outcome=outcome) from tp_error
                raise
        return {"tp": tp_view, "sl": sl_view, "tp_price": tp, "sl_stop": sl_stop, "sl_limit": sl_limit}

    # ------------------------------------------------------------------ submission
    @staticmethod
    def _order_opens_short(order: TradingOrder, is_closing_order: bool) -> bool:
        """True when this SELL would OPEN (or extend) an equity short: decided from the order and
        its TRANSACTION (every long's TP/SL leg is a SELL too), never from the side alone."""
        if is_closing_order or order.side != OrderDirection.SELL:
            return False
        if getattr(order, "asset_class", None) == CoreAssetClass.OPTION:
            return False
        if not order.transaction_id or getattr(order, "depends_on_order", None):
            return False
        try:
            transaction = get_instance(Transaction, order.transaction_id)
        except InstanceNotFound:
            logger.warning(f"Shortability gate skipped for order {order.id} ({order.symbol}): "
                           f"transaction {order.transaction_id} not found")
            return False
        return transaction.side == OrderDirection.SELL

    def _is_washtrade_lock_candidate(self, trading_order: TradingOrder) -> bool:
        """IBKR has no wash-trade rejection for ordinary orders (design doc 4.6): never lock."""
        return False

    def _record_skip(self, order: TradingOrder, reason: str) -> None:
        fresh = get_instance(TradingOrder, order.id)
        fresh.status = OrderStatus.CANCELED
        fresh.comment = (f"{fresh.comment} | {reason}" if fresh.comment else reason)[:500]
        update_instance(fresh)

    def _persist_submission(self, order_id: int, view: BrokerOrderView, tif: str,
                            quantity: Optional[float] = None) -> TradingOrder:
        fresh = get_instance(TradingOrder, order_id)
        if not fresh.broker_order_id or (view.perm_id and str(fresh.broker_order_id).startswith("o")):
            fresh.broker_order_id = view.broker_order_id
        fresh.status = M.map_ib_status(view.status, view.filled, view.remaining)
        fresh.good_for = tif.lower()
        if quantity is not None:
            fresh.quantity = quantity
        if view.filled:
            fresh.filled_qty = view.filled
        if view.avg_fill_price and view.avg_fill_price > 0:
            fresh.open_price = view.avg_fill_price
        update_instance(fresh)
        if fresh.status == OrderStatus.CANCELED and fresh.filled_qty and fresh.filled_qty > 0:
            # It traded and then died inside the acknowledgement window: the shares are real. Fold them
            # back exactly as refresh_orders does (never an ERROR row for shares that were bought).
            from ba2_common.core.TransactionHelper import TransactionHelper
            logger.warning(f"[Account {self.id}] order {order_id} was cancelled by IB after trading "
                           f"{fresh.filled_qty:g}; recorded as CANCELED with its fill")
            TransactionHelper.reconcile_canceled_partial_fill(fresh)
        return fresh

    # -- the per-row nonce ------------------------------------------------------------------------------
    @staticmethod
    def _row_nonce(row_id: int) -> Optional[str]:
        return (get_instance(TradingOrder, row_id).data or {}).get("ibkr_nonce")

    def _ensure_nonce(self, row_id: int) -> str:
        """The row's per-row random token (persisted in ``data["ibkr_nonce"]``), created once.
        It is part of every orderRef: ``tradingorder`` ids are recycled by SQLite and repeat across
        instances, so an id alone must never identify an IB order or execution."""
        row = get_instance(TradingOrder, row_id)
        data = dict(row.data or {})
        nonce = data.get("ibkr_nonce")
        if not nonce:
            nonce = M.new_nonce()
            data["ibkr_nonce"] = nonce
            row.data = data
            update_instance(row)
        return nonce

    def _rotate_nonce(self, row_id: int) -> str:
        """Give the row a NEW nonce: the old orderRef belongs to an order that died, and a ref may
        identify only one live order (``refresh_orders`` matches rows to orders by it)."""
        row = get_instance(TradingOrder, row_id)
        data = dict(row.data or {})
        old = data.get("ibkr_nonce")
        nonce = M.new_nonce()
        data["ibkr_nonce"] = nonce
        if old:
            data["ibkr_prior_nonces"] = [*data.get("ibkr_prior_nonces", []), old][-5:]
        row.data = data
        update_instance(row)
        return nonce

    def _stamp_placed(self, order: TradingOrder) -> None:
        """Record WHEN this row is handed to IB (``data["ibkr_placed_at"]``). The "never reached IBKR"
        grace ages from here, not from ``created_at``: a dependent exit, a staged replacement or a
        stop->MARKET retry row can be created hours before it is placed."""
        now = self._utcnow().isoformat()
        fresh = get_instance(TradingOrder, order.id)
        fresh.data = {**(fresh.data or {}), "ibkr_placed_at": now}
        update_instance(fresh)
        order.data = {**(order.data or {}), "ibkr_placed_at": now}   # the caller's object, too

    @staticmethod
    def _carry_nonce(order: TradingOrder, nonce: str) -> None:
        """Copy the nonce onto the CALLER's object too: it predates the nonce, and a later
        ``update_instance`` of it must not wipe the nonce out of the row."""
        order.data = {**(order.data or {}), "ibkr_nonce": nonce}

    def _record_unconfirmed_placement(self, row_id: int, progress: Dict[str, Any], tif: str,
                                      quantity: Optional[float], error: Exception) -> TradingOrder:
        """The call timed out (or the connection dropped) AFTER ``placeOrder`` ran: the order may be
        live at IB. NEVER mark the row ERROR (a retry would then create a duplicate): record it
        PENDING_NEW and let ``refresh_orders`` resolve it by orderRef.

        An OCO is two orders: the PARENT row is the take-profit leg and takes only the TAKE-PROFIT's id
        (none when only the stop got out); a stop that got out gets its own child row at once, so no
        live order exists without one."""
        ids = progress["order_ids"]
        is_oco = "sl_id" in progress or "tp_id" in progress
        main_id = progress.get("tp_id") if is_oco else ids[0]
        fresh = get_instance(TradingOrder, row_id)
        if main_id and not fresh.broker_order_id:
            fresh.broker_order_id = M.format_broker_order_id(0, main_id)
        fresh.status = OrderStatus.PENDING_NEW
        fresh.good_for = tif.lower()
        if quantity is not None:
            fresh.quantity = quantity
        fresh.comment = (f"{fresh.comment} | " if fresh.comment else "") + (
            f"placement unconfirmed ({type(error).__name__}: {error}); left PENDING for refresh")[:300]
        update_instance(fresh)
        if progress.get("sl_id"):
            self._create_oco_child(fresh, "SELL" if fresh.side == OrderDirection.SELL else "BUY", tif,
                                   None, None, None, M.format_broker_order_id(0, progress["sl_id"]),
                                   OrderStatus.PENDING_NEW, "")
        logger.error(f"[Account {self.id}] order {row_id}: {type(error).__name__} AFTER placeOrder "
                     f"(IB order ids {ids}); the order may be live. Left PENDING_NEW, not ERROR; "
                     f"refresh_orders resolves it by orderRef")
        return fresh

    def _record_orphan_stop(self, parent: TradingOrder, action: str, tif: str,
                            error: IBKROrphanStop) -> None:
        """A live (or filled) stop whose OCO failed: keep it as a PLAIN STOP EXIT of the transaction, so
        the platform sees the protection that exists instead of layering a second one on top of it."""
        view = error.view
        side = OrderDirection.SELL if action == "SELL" else OrderDirection.BUY
        row_id = add_instance(TradingOrder(
            account_id=self.id, symbol=parent.symbol, quantity=view.total_quantity, side=side,
            order_type=(CoreOrderType.SELL_STOP_LIMIT if action == "SELL" else CoreOrderType.BUY_STOP_LIMIT),
            broker_order_id=view.broker_order_id, limit_price=error.sl_limit, stop_price=error.sl_stop,
            good_for=tif.lower(), status=M.map_ib_status(view.status, view.filled, view.remaining),
            filled_qty=view.filled or None,
            comment=f"{int(self._utcnow().timestamp())}-ORPHAN-OCO-SL-[PARENT:{parent.id}] "
                    f"(take-profit failed; stop cancel {error.outcome})",
            transaction_id=parent.transaction_id, created_at=self._utcnow()))
        logger.error(f"[Account {self.id}] ORPHAN STOP: the OCO of order {parent.id} failed at the "
                     f"take-profit and its stop leg (IB order {view.broker_order_id}, {view.status}) could "
                     f"not be confirmed cancelled ({error.outcome}). It is recorded as row {row_id} (a "
                     f"plain stop exit of transaction {parent.transaction_id}); verify it in TWS.")

    def _submit_order_impl(self, trading_order: TradingOrder, tp_price: Optional[float] = None,
                           sl_price: Optional[float] = None, is_closing_order: bool = False,
                           use_complex_order: bool = False) -> Optional[TradingOrder]:
        """Send ONE equity order (or one OCO pair) to IBKR.

        ``tp_price``/``sl_price`` are accepted for the interface and deliberately not acted on
        here: ``AccountInterface.submit_order`` calls ``adjust_tp_sl`` after this returns, which
        builds the protective exit. ``use_complex_order`` cannot occur (no wash-trade lock) and
        raises rather than being ignored. A read-only account raises BEFORE anything is written.

        A row that ALREADY HAD a nonce is a re-submission (the shared stop-through-market retry, the UI's
        Retry button, a lost answer): the orders carrying its orderRef are looked up first. A still-live
        (or already traded) one that matches is ADOPTED; one that differs is refused; one that merely died
        is spent, and the row gets a NEW nonce and is placed again. A first attempt does no lookup.
        """
        if use_complex_order:
            raise NotImplementedError(
                f"[Account {self.id}] IBKR sets no wash-trade lock, so a complex-order submission "
                f"for {trading_order.symbol} is a broken invariant; refusing to ignore it")
        if trading_order.broker_order_id:
            logger.warning(f"Order {trading_order.id} already has broker_order_id "
                           f"{trading_order.broker_order_id} -- skipping re-submission")
            return trading_order
        self._refuse_if_read_only("submit orders")
        progress: Dict[str, Any] = {"order_ids": []}
        tif_used = "day"
        try:
            if trading_order.id is None:
                trading_order.status = OrderStatus.PENDING
                trading_order.id = add_instance(trading_order, expunge_after_flush=True)
            if trading_order.asset_class == CoreAssetClass.OPTION:
                raise ValueError("option orders go through submit_option_order, not submit_order")
            order_type = trading_order.order_type
            action = "BUY" if trading_order.side == OrderDirection.BUY else "SELL"
            had_nonce = bool(self._row_nonce(trading_order.id))
            nonce = self._ensure_nonce(trading_order.id)
            self._carry_nonce(trading_order, nonce)
            if order_type == CoreOrderType.OCO:
                return self._submit_oco(trading_order, action, nonce, progress, had_nonce)
            if order_type in (CoreOrderType.TRAILING_STOP, CoreOrderType.OTO):
                raise ValueError(f"IBKR submission does not support order type {order_type.value}")
            ib_type = {CoreOrderType.MARKET: "MKT", CoreOrderType.BUY_LIMIT: "LMT",
                       CoreOrderType.SELL_LIMIT: "LMT", CoreOrderType.BUY_STOP: "STP",
                       CoreOrderType.SELL_STOP: "STP", CoreOrderType.BUY_STOP_LIMIT: "STP LMT",
                       CoreOrderType.SELL_STOP_LIMIT: "STP LMT"}.get(order_type)
            if ib_type is None:
                raise ValueError(f"unsupported order type {order_type}")
            if ib_type in ("LMT", "STP LMT") and not trading_order.limit_price:
                raise ValueError(f"Limit price is required for {order_type.value} orders")
            if ib_type in ("STP", "STP LMT") and not trading_order.stop_price:
                raise ValueError(f"Stop price is required for {order_type.value} orders")
            tif, warning = M.ib_time_in_force(trading_order.good_for,
                                              is_market=self._is_market(order_type))
            tif_used = tif
            if warning:
                logger.warning(f"Order {trading_order.id}: {warning}")
            ref = M.make_order_ref(self.id, trading_order.id, nonce=nonce)
            if had_nonce:
                # NOTE (kept on purpose): a re-submission REFUSED here because a live order under this
                # ref differs leaves the row ERROR with the refusal in its comment -- but the very next
                # ``refresh_orders`` matches that live order to this row BY ITS REF (same nonce) and
                # re-binds it: the row then shows the order IB really has (its quantity/prices/status),
                # not what the row asked for. That is the safe direction: the live order is tracked,
                # never orphaned, and the comment keeps the explanation. Design doc section 15.
                kind, prior = self._resolve_prior(
                    [{"name": "main", "ref": ref, "action": action, "ib_type": ib_type,
                      "qty": float(trading_order.quantity)}], f"order {trading_order.id}")["main"]
                if kind == "adopt":
                    logger.warning(f"[Account {self.id}] order {trading_order.id}: IB already has this "
                                   f"order ({prior.broker_order_id}, {prior.status}); adopting it instead "
                                   f"of placing a duplicate")
                    return self._persist_submission(trading_order.id, prior, prior.tif or tif,
                                                    quantity=prior.total_quantity)
                if kind == "dead":
                    nonce = self._rotate_nonce(trading_order.id)
                    self._carry_nonce(trading_order, nonce)
                    ref = M.make_order_ref(self.id, trading_order.id, nonce=nonce)
                    logger.info(f"[Account {self.id}] order {trading_order.id}: the earlier IB order under "
                                f"its ref died without trading; placing it again under a new nonce")
            spec = {
                "symbol": trading_order.symbol, "quantity": float(trading_order.quantity),
                "order_type": order_type, "ib_type": ib_type, "action": action, "tif": tif,
                "limit": trading_order.limit_price, "stop": trading_order.stop_price,
                "order_ref": ref, "progress": progress,
                "opens_short": self._order_opens_short(trading_order, is_closing_order),
            }
            self._stamp_placed(trading_order)
            try:
                placed = self._call(lambda ib: self._place_single(ib, spec),
                                    op=f"place order {trading_order.id}",
                                    timeout=self._submit_budget(1))
            except _ZeroQuantityAfterRounding as e:
                logger.warning(f"Order {trading_order.id} ({trading_order.symbol}) skipped: {e}")
                self._record_skip(trading_order, f"skipped: {e}")
                return None
            except (TimeoutError, IBKRConnectionError) as e:
                if progress["order_ids"]:
                    return self._record_unconfirmed_placement(
                        trading_order.id, progress, tif_used, None, e)
                raise
            fresh = self._persist_submission(trading_order.id, placed["view"], placed["tif"],
                                             quantity=placed["quantity"])
            logger.info(f"Submitted IBKR order {fresh.id}: broker_order_id={fresh.broker_order_id}, "
                        f"status={fresh.status}")
            return fresh
        except Exception as e:  # noqa: BLE001 -- recorded on the row, never swallowed
            logger.error(f"Error submitting order {trading_order.id} to IBKR: {e}", exc_info=True)
            if progress["order_ids"] and not isinstance(e, IBKROrderRejected):
                # something went wrong AFTER IB had the order (e.g. writing the answer back):
                # never mark it ERROR, the order may be live
                return self._record_unconfirmed_placement(trading_order.id, progress, tif_used, None, e)
            if trading_order.id:
                return self._handle_order_submit_error(trading_order, e)
            logger.warning("Cannot mark order as ERROR - order has no ID")
            return None

    def _submit_oco(self, parent: TradingOrder, action: str, nonce: str, progress: Dict[str, Any],
                    had_nonce: bool = False) -> TradingOrder:
        if not parent.limit_price or parent.limit_price <= 0:
            raise ValueError("Limit price (take profit) is required for OCO orders")
        if not parent.stop_price or parent.stop_price <= 0:
            raise ValueError("Stop price (stop loss) is required for OCO orders")
        tif, _ = M.ib_time_in_force(parent.good_for, is_market=False)
        qty = float(parent.quantity)

        def refs(n: str) -> Tuple[str, str]:
            return (M.make_order_ref(self.id, parent.id, nonce=n),
                    M.make_order_ref(self.id, parent.id, "SL", nonce=n))
        tp_ref, sl_ref = refs(nonce)
        adopted: Dict[str, BrokerOrderView] = {}
        if had_nonce:
            prior = self._resolve_prior(
                [{"name": "tp", "ref": tp_ref, "action": action, "ib_type": "LMT", "qty": qty},
                 {"name": "sl", "ref": sl_ref, "action": action, "ib_type": "STP LMT", "qty": qty}],
                f"OCO {parent.id}")
            if any(kind == "dead" for kind, _ in prior.values()):
                # One leg died: the other must not be left working beside a new pair. Cancel it (and
                # wait for the confirmation), then start over under a new nonce.
                live = [v for kind, v in prior.values() if kind == "adopt"]
                if any(v.filled > 0 for v in live):
                    raise IBKROrderRejected(
                        f"an OCO leg of order {parent.id} has already traded at IB while the other died; "
                        f"refusing to re-place the pair. Reconcile it by hand.")
                if live:
                    outcomes = self._call(lambda ib: self._cancel_views_confirmed(ib, live),
                                          op=f"cancel the surviving OCO leg of {parent.id}",
                                          timeout=self._READ_TIMEOUT * 2 + self._CANCEL_ACK_TIMEOUT * len(live))
                    if any(o != "cancelled" for o in outcomes):
                        raise IBKROrderRejected(
                            f"OCO {parent.id}: one leg died and the surviving leg's cancel is not "
                            f"confirmed ({outcomes}); refusing to place a second pair beside a live leg")
                nonce = self._rotate_nonce(parent.id)
                self._carry_nonce(parent, nonce)
                tp_ref, sl_ref = refs(nonce)
            else:
                adopted = {name: view for name, (kind, view) in prior.items() if kind == "adopt"}
        spec = {
            "symbol": parent.symbol, "quantity": qty, "action": action,
            "tp": float(parent.limit_price), "sl": float(parent.stop_price), "tif": tif,
            "oca_group": f"ba2-oca-{self.id}-{parent.id}-{nonce}", "progress": progress,
            "tp_ref": tp_ref, "sl_ref": sl_ref,
            "adopted_tp": adopted.get("tp"), "adopted_sl": adopted.get("sl"),
        }
        self._stamp_placed(parent)
        try:
            placed = self._call(lambda ib: self._place_oco(ib, spec), op=f"place OCO {parent.id}",
                                timeout=self._submit_budget(2, cancel_waits=1))
        except IBKROrphanStop as e:
            self._record_orphan_stop(parent, action, tif, e)
            raise
        except (TimeoutError, IBKRConnectionError) as e:
            if progress["order_ids"]:
                return self._record_unconfirmed_placement(parent.id, progress, tif, None, e)
            raise
        tp_view, sl_view = placed["tp"], placed["sl"]
        fresh = self._persist_submission(parent.id, tp_view, tif)
        fresh.legs_broker_ids = [tp_view.broker_order_id, sl_view.broker_order_id]
        fresh.data = {**(fresh.data or {}), "ibkr": {
            "oca_group": spec["oca_group"], "tp_order_id": tp_view.order_id,
            "sl_order_id": sl_view.order_id, "sl_limit": placed["sl_limit"]}}
        update_instance(fresh)
        self._create_oco_child(parent, action, tif, placed["sl_limit"], placed["sl_stop"],
                               sl_view.filled or None, sl_view.broker_order_id,
                               M.map_ib_status(sl_view.status, sl_view.filled, sl_view.remaining),
                               tp_view.broker_order_id)
        logger.info(f"Submitted IBKR OCO {parent.id}: TP leg {tp_view.broker_order_id}, "
                    f"SL leg {sl_view.broker_order_id}")
        return fresh

    def _create_oco_child(self, parent: TradingOrder, action: str, tif: str,
                          sl_limit: Optional[float], sl_stop: Optional[float],
                          filled: Optional[float], broker_id: str, status: OrderStatus,
                          tp_broker_id: str = "") -> None:
        """The stop-leg child row of an OCO parent (skipped when one already exists: a retry)."""
        with get_db() as session:
            existing = session.exec(select(TradingOrder).where(
                TradingOrder.parent_order_id == parent.id,
                TradingOrder.account_id == self.id)).first()
        if existing is not None:
            return
        add_instance(TradingOrder(
            account_id=self.id, symbol=parent.symbol, quantity=parent.quantity, side=parent.side,
            order_type=(CoreOrderType.SELL_STOP_LIMIT if action == "SELL" else CoreOrderType.BUY_STOP_LIMIT),
            broker_order_id=broker_id, limit_price=sl_limit, stop_price=sl_stop,
            good_for=tif.lower(), status=status, filled_qty=filled,
            comment=f"{int(self._utcnow().timestamp())}-OCO-SL-[PARENT:{parent.id}/BROKER:"
                    f"{tp_broker_id}]",
            transaction_id=parent.transaction_id, parent_order_id=parent.id,
            created_at=self._utcnow()))

    # ------------------------------------------------------------------ cancel / modify
    def _order_ref_for_row(self, row: TradingOrder, account_id: int) -> str:
        """The ``orderRef`` this row's IB order carries (an OCO stop leg is ``<parent>:SL`` and uses
        the PARENT's nonce)."""
        if row.parent_order_id and row.order_type in (CoreOrderType.SELL_STOP_LIMIT,
                                                      CoreOrderType.BUY_STOP_LIMIT):
            parent = get_instance(TradingOrder, row.parent_order_id)
            return M.make_order_ref(account_id, row.parent_order_id, "SL",
                                    nonce=(parent.data or {}).get("ibkr_nonce"))
        return M.make_order_ref(account_id, row.id, nonce=(row.data or {}).get("ibkr_nonce"))

    def _row_for_order_id(self, order_id: Any) -> Optional[TradingOrder]:
        """Our row for a DB id OR a broker id (scoped to this account), else ``None``.

        Broker ids are a ``permId`` (9+ digits) or ``o<orderId>``; DB ids are short integers.
        """
        text = str(order_id).strip()
        if text.isdigit() and len(text) < 8:
            try:
                candidate = get_instance(TradingOrder, int(text))
                if candidate.account_id == self.id:
                    return candidate
            except InstanceNotFound:
                pass
        with get_db() as session:
            found = session.exec(select(TradingOrder).where(
                TradingOrder.broker_order_id == text, TradingOrder.account_id == self.id)).first()
            found_id = found.id if found else None
        return get_instance(TradingOrder, found_id) if found_id else None

    async def _find_trade(self, ib: Any, row: Dict[str, Any]) -> Optional[Any]:
        def search() -> Optional[Any]:
            for trade in ib.trades():
                if row["order_ref"] and trade.order.orderRef == row["order_ref"]:
                    return trade
                if M.broker_id_matches(row["broker_order_id"], int(trade.orderStatus.permId or 0),
                                       int(trade.order.orderId or 0)):
                    return trade
            return None

        found = search()
        if found is None:
            try:
                await self._open_orders(ib)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Account {self.id}] could not refresh open orders to find "
                               f"{row['broker_order_id']}: {e}")
            found = search()
        return found

    async def _cancel_trades(self, ib: Any, rows: List[Dict[str, Any]]) -> Dict[str, bool]:
        """Send a cancel for each row's IB order; True per broker id when IB did not refuse."""
        results: Dict[str, bool] = {}
        for row in rows:
            trade = await self._find_trade(ib, row)
            if trade is None:
                logger.error(f"[Account {self.id}] order {row['broker_order_id']} is not known to "
                             f"this IBKR session (placed by another client id, or already gone)")
                results[row["broker_order_id"]] = False
                continue
            order_id = int(trade.order.orderId)
            current = str(trade.orderStatus.status)
            distrust = False
            if current in M.IB_REJECTION_STATUSES:
                # A local Cancelled is not proof (a refused MODIFY leaves a live order marked Cancelled
                # by ib_async): ask IB before concluding the goal is already met.
                distrust = await self._listed_by_ib(ib, trade)
                current = str(trade.orderStatus.status)
                if current in M.IB_REJECTION_STATUSES and not distrust:
                    # Already not working: the goal is met; refresh_orders records the final status.
                    results[row["broker_order_id"]] = True
                    continue
                if current not in M.IB_REJECTION_STATUSES:
                    distrust = False
            if current == "Filled":
                logger.error(f"[Account {self.id}] cannot cancel {row['broker_order_id']}: it has "
                             f"already FILLED at IBKR")
                results[row["broker_order_id"]] = False
                continue
            seq = self._runtime().mark()
            ib.cancelOrder(trade.order)
            deadline = time.monotonic() + self._CANCEL_ACK_TIMEOUT
            refused = None
            while time.monotonic() < deadline:
                errors = [(c, m) for c, m in self._runtime().order_errors(order_id, seq,
                                                                           kinds=("order", "cancelled"))
                          if c in M.ORDER_STATE_CODES]
                if errors:                       # read BEFORE any status shortcut, in every branch
                    refused = errors[-1]
                    break
                if distrust and str(trade.orderStatus.status) in M.IB_REJECTION_STATUSES:
                    # still the stale local status: wait for IB's own word (it stops listing the order,
                    # or its status moves to PendingCancel)
                    if not await self._listed_by_ib(ib, trade):
                        break
                    await asyncio.sleep(0.3)
                    continue
                distrust = False
                if str(trade.orderStatus.status) in ("PendingCancel", "Cancelled", "ApiCancelled"):
                    break
                await asyncio.sleep(0.02)
            if refused:
                logger.error(f"[Account {self.id}] IBKR refused the cancel of "
                             f"{row['broker_order_id']}: error {refused[0]}: {refused[1]}")
            results[row["broker_order_id"]] = refused is None
        return results

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order given our DB id OR the broker id. Marks it ``PENDING_CANCEL`` (the
        cancel is REQUESTED; ``refresh_orders`` promotes it when IB confirms). An OCO parent
        cancels both legs."""
        try:
            self._refuse_if_read_only("cancel orders")
            db_order = self._row_for_order_id(order_id)
            if db_order is None:
                logger.error(f"[Account {self.id}] order {order_id} not found in database")
                return False
            if not db_order.broker_order_id:
                logger.error(f"[Account {self.id}] order {db_order.id} has no broker_order_id -- "
                             f"it was never sent to IBKR")
                return False
            targets = [db_order]
            if db_order.order_type == CoreOrderType.OCO:
                with get_db() as session:
                    kids = session.exec(select(TradingOrder).where(
                        TradingOrder.parent_order_id == db_order.id,
                        TradingOrder.account_id == self.id)).all()
                    kid_ids = [k.id for k in kids if k.broker_order_id]
                targets += [get_instance(TradingOrder, kid) for kid in kid_ids]
            payload = [{"order_ref": self._order_ref_for_row(t, self.id),
                        "broker_order_id": t.broker_order_id} for t in targets]
            results = self._call(lambda ib: self._cancel_trades(ib, payload),
                                 op=f"cancel {db_order.id}", timeout=self._READ_TIMEOUT * 2)
            ok = all(results.values())
            for target in targets:
                if results.get(target.broker_order_id):
                    fresh = get_instance(TradingOrder, target.id)
                    fresh.status = OrderStatus.PENDING_CANCEL
                    update_instance(fresh)
            if ok:
                logger.info(f"[Account {self.id}] requested cancel of IBKR order "
                            f"{db_order.broker_order_id} (db id={db_order.id})")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error cancelling IBKR order {order_id}: {e}",
                         exc_info=True)
            return False

    @staticmethod
    def _same_number(a: Any, b: Any) -> bool:
        return abs(float(a) - float(b)) < 1e-6

    @classmethod
    def _order_carries(cls, order: Any, sent: Dict[str, Any]) -> bool:
        """Does the order object carry what was SENT? Only the prices that were actually sent count: a
        LIMIT order sends no ``auxPrice`` (UNSET) and IB may echo it as 0.0 (or leave it unset), a STOP
        sends no ``lmtPrice``; comparing those would never confirm a modification."""
        for key, value in (("lmt", order.lmtPrice), ("aux", order.auxPrice)):
            if M.ib_number(sent[key]) is not None and not cls._same_number(value, sent[key]):
                return False
        return cls._same_number(order.totalQuantity, sent["qty"])

    async def _modification_echoed(self, ib: Any, trade: Any, sent: Dict[str, Any]) -> bool:
        """Re-read the open orders and compare THIS order's values with what was sent. ib_async copies
        IB's own values onto a known trade's order object when IB reports it, so after the read the object
        holds what IB HAS: equal to ``sent`` means applied; the order missing from the list means gone."""
        listed = await self._open_orders(ib)
        if not any(t is trade or t.order.orderId == trade.order.orderId for t in listed):
            return False
        return self._order_carries(trade.order, sent)

    async def _modify_order_object(self, ib: Any, row: Dict[str, Any], *, qty: Optional[float],
                                   limit: Optional[float], stop: Optional[float],
                                   tif: Optional[str], symbol: str) -> Dict[str, Any]:
        """Re-send an order with the same ``orderId`` and WAIT FOR IB TO CONFIRM it.

        ib_async keeps the order's status unchanged on a modification, so "no error" is not
        confirmation, and its own ``Modified`` log entry exists ONLY when the echoed status is exactly
        ``Submitted`` (a resting ``PreSubmitted`` stop, the typical state of a US stop leg outside the
        regular session, gets none although IB applied the change). Confirmed = any of:

        * ib_async logged ``Modified``;
        * after a short window, a re-read of the open orders shows this order carrying the SENT values.

        Refused = an order-kind error for this id, or the order reaching a final state. Anything else at
        the deadline is UNCONFIRMED. Refused and unconfirmed both roll the in-memory Order object (shared
        with ib_async's state) back to its old values and raise: the caller must not store the new prices
        as truth. A late application at IB heals itself: ``refresh_orders`` copies the live prices.
        """
        trade = await self._find_trade(ib, row)
        if trade is None:
            raise IBKRContractError(
                f"order {row['broker_order_id']} is not known to this IBKR session; only orders "
                f"placed by this client id can be modified in place")
        resolved = await self._resolve_stock(ib, symbol)
        rules = await self._market_rules(ib, resolved.details)
        order = trade.order
        old = (order.lmtPrice, order.auxPrice, order.totalQuantity, order.tif, order.transmit)
        applied: Dict[str, Optional[float]] = {"limit": None, "stop": None}
        if limit is not None:
            order.lmtPrice = applied["limit"] = M.round_to_increment(limit, rules)
        if stop is not None:
            order.auxPrice = applied["stop"] = M.round_to_increment(stop, rules)
        if qty is not None:
            order.totalQuantity = qty
        if tif:
            order.tif = tif
        order.transmit = True
        sent = {"lmt": order.lmtPrice, "aux": order.auxPrice, "qty": order.totalQuantity}
        rt = self._runtime()
        seq, log_len = rt.mark(), len(trade.log)
        order_id = int(order.orderId)
        window = min(self._MODIFY_FAST_WINDOW, self._ORDER_ACK_TIMEOUT / 3.0)

        def rollback() -> None:
            order.lmtPrice, order.auxPrice, order.totalQuantity, order.tif, order.transmit = old

        try:
            new_trade = ib.placeOrder(resolved.contract, order)   # same orderId => in-place modify
            start = time.monotonic()
            deadline = start + self._ORDER_ACK_TIMEOUT
            next_reread = start + window
            while True:
                # ``Modified`` alone proves nothing: our OWN re-read of the open orders makes TWS send an
                # unchanged orderStatus, which ib_async logs as 'Modified' even when IB kept the OLD
                # prices (and has just copied them onto the order). It counts only while the order
                # object still carries what was sent.
                if (any(e.message == "Modified" for e in list(new_trade.log)[log_len:])
                        and self._order_carries(new_trade.order, sent)):
                    break
                errors = rt.order_errors(order_id, seq)
                if errors:
                    raise IBKROrderRejected(f"IB error {errors[-1][0]}: {errors[-1][1]}", errors[-1][0])
                status = str(new_trade.orderStatus.status)
                if status in M.IB_REJECTION_STATUSES or status == "Filled":
                    raise IBKROrderRejected(
                        f"order {order_id} reached {status} while being modified")
                now = time.monotonic()
                if now >= next_reread or now >= deadline:
                    if await self._modification_echoed(ib, new_trade, sent):
                        break
                    warnings = rt.order_warnings(order_id, seq)
                    if warnings:
                        # IB answered the modification with a warning (105/321/329 ...) and the re-read
                        # shows it did NOT apply the prices: that answer is final, no need to wait on
                        raise IBKROrderRejected(
                            f"IB did not apply the modification of order {order_id}: warning "
                            f"{warnings[-1][0]}: {warnings[-1][1]}", warnings[-1][0])
                    if now >= deadline:
                        raise IBKROrderRejected(
                            f"IB did not confirm the modification of order {order_id} within "
                            f"{self._ORDER_ACK_TIMEOUT:g}s (no 'Modified' entry, and the open order "
                            f"does not carry the sent prices)")
                    next_reread = now + self._MODIFY_REREAD_INTERVAL
                await asyncio.sleep(0.02)
        except BaseException as modify_error:
            rollback()
            if isinstance(modify_error, Exception):
                # A refused modification makes ib_async mark the (still working) trade Cancelled
                # LOCALLY: let IB's own answer restore the truth before anyone reads that status.
                try:
                    await self._open_orders(ib)
                except Exception as e:  # noqa: BLE001 -- best effort; the cancel paths re-check anyway
                    logger.warning(f"[Account {self.id}] could not re-read open orders after a refused "
                                   f"modification of order {order_id}: {e}")
            raise
        return {"view": BrokerOrderView.from_trade(new_trade), **applied}

    def modify_order(self, order_id: str, trading_order: Optional[TradingOrder] = None):
        """TRUE in-place modification (same IB ``orderId``): prices, TIF, and whole-share size.
        Returns the updated row, or ``None``."""
        try:
            self._refuse_if_read_only("modify orders")
            db_order = self._row_for_order_id(order_id)
            if db_order is None or not db_order.broker_order_id:
                logger.error(f"[Account {self.id}] cannot modify {order_id}: no such sent order")
                return None
            src = trading_order or db_order
            qty = float(src.quantity) if src.quantity else None
            if qty is not None and qty != int(qty):
                logger.error(f"[Account {self.id}] refusing to resize order {order_id} to the "
                             f"fractional {qty}: cancel and resubmit")
                return None
            tif = None
            if src.good_for:
                tif, _ = M.ib_time_in_force(src.good_for,
                                            is_market=db_order.order_type == CoreOrderType.MARKET)
            payload = {"order_ref": self._order_ref_for_row(db_order, self.id),
                       "broker_order_id": db_order.broker_order_id}
            result = self._call(
                lambda ib: self._modify_order_object(
                    ib, payload, qty=qty, limit=src.limit_price, stop=src.stop_price, tif=tif,
                    symbol=db_order.symbol),
                op=f"modify {db_order.id}", timeout=2 * self._READ_TIMEOUT + self._ORDER_ACK_TIMEOUT)
            view = result["view"]
            fresh = get_instance(TradingOrder, db_order.id)
            # Persist what IB was actually sent (rounded to its tick), not what was asked for.
            if result["limit"] is not None:
                fresh.limit_price = result["limit"]
            if result["stop"] is not None:
                fresh.stop_price = result["stop"]
            if qty is not None:
                fresh.quantity = qty
            if view.status != "ValidationError":      # a warning is not a state change of a live order
                fresh.status = M.map_ib_status(view.status, view.filled, view.remaining)
            update_instance(fresh)
            return fresh
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error modifying IBKR order {order_id}: {e}",
                         exc_info=True)
            return None

    def _update_broker_tp_order(self, tp_order: TradingOrder, new_tp_price: float) -> None:
        tp_order.limit_price = new_tp_price
        if self.modify_order(str(tp_order.id), tp_order) is None:
            raise RuntimeError(f"IBKR could not move TP order {tp_order.id} to {new_tp_price}")

    def _update_broker_sl_order(self, sl_order: TradingOrder, new_sl_price: float) -> None:
        sl_order.stop_price = new_sl_price
        if self.modify_order(str(sl_order.id), sl_order) is None:
            raise RuntimeError(f"IBKR could not move SL order {sl_order.id} to {new_sl_price}")

    # ------------------------------------------------------------------ in-place exit modification
    def _modify_exit_in_place(self, session, transaction, entry_order, spec, live_broker_orders,
                              quantity) -> bool:
        """Move the standing exit to ``spec``'s prices WITHOUT a gap in protection (IB can modify).

        Only when the structure and size already match: one live OCO (parent + SL child) for an
        OCO spec, or one live limit/stop for a limit/stop spec. All-or-nothing: a failed second
        leg puts the first back and returns False so the caller takes the safe cancel path.
        """
        order_type, tp_price, sl_price, _label = spec
        live = [o for o in live_broker_orders if o.status not in OrderStatus.get_terminal_statuses()]
        if not live or any(o.quantity is None or abs(float(o.quantity) - float(quantity)) > 1e-9
                           for o in live):
            return False
        parents = [o for o in live if o.parent_order_id is None]
        if len(parents) != 1 or parents[0].order_type != order_type:
            return False
        parent = parents[0]
        kids = [o for o in live if o.parent_order_id == parent.id]
        try:
            if order_type == CoreOrderType.OCO:
                if len(kids) != 1:
                    return False
                child = kids[0]
                cushion = (1.0 - OCO_STOP_LIMIT_CUSHION) if parent.side == OrderDirection.SELL \
                    else (1.0 + OCO_STOP_LIMIT_CUSHION)
                sl_limit = sl_price * cushion
                old = (parent.limit_price, child.stop_price, child.limit_price)
                if self.modify_order(str(parent.id), _Patch(limit_price=tp_price,
                                                            quantity=parent.quantity)) is None:
                    return False
                if self.modify_order(str(child.id), _Patch(stop_price=sl_price, limit_price=sl_limit,
                                                           quantity=child.quantity)) is None:
                    self.modify_order(str(parent.id), _Patch(limit_price=old[0],
                                                             quantity=parent.quantity))
                    return False
                fresh_parent = get_instance(TradingOrder, parent.id)
                fresh_parent.stop_price = sl_price
                update_instance(fresh_parent)
            elif tp_price:
                if kids or self.modify_order(str(parent.id), _Patch(
                        limit_price=tp_price, quantity=parent.quantity)) is None:
                    return False
            else:
                if kids or self.modify_order(str(parent.id), _Patch(
                        stop_price=sl_price, quantity=parent.quantity)) is None:
                    return False
        except Exception as e:  # noqa: BLE001 -- the caller falls back to cancel-and-replace
            logger.error(f"[Account {self.id}] in-place exit modification failed: {e}", exc_info=True)
            return False
        logger.info(f"[Account {self.id}] moved the {order_type.value} exit of transaction "
                    f"{transaction.id} in place (TP={tp_price}, SL={sl_price})")
        return True

    # ------------------------------------------------------------------ refresh
    def _row_for_view(self, view: BrokerOrderView) -> Optional[TradingOrder]:
        """Our row for an IB order. By ``orderRef`` ONLY when its account, row id AND per-row nonce
        all match and the order is this client's: a recycled ``tradingorder`` id (SQLite reuses them,
        and instances share ids) must never claim someone else's order. Otherwise by the broker id."""
        ref = M.parse_order_ref(view.order_ref)
        own_client = self._runtime().client_id
        if view.client_id and view.client_id != own_client:
            return None                      # another API client's order: never ours
        if ref is not None and ref.account == self.id and ref.nonce:
            try:
                row = get_instance(TradingOrder, ref.order)
            except InstanceNotFound:
                row = None
            if row is not None and row.account_id == self.id \
                    and (row.data or {}).get("ibkr_nonce") == ref.nonce:
                if ref.suffix is None:
                    return row
                if ref.suffix == "SL":
                    with get_db() as session:
                        child = session.exec(select(TradingOrder).where(
                            TradingOrder.parent_order_id == ref.order,
                            TradingOrder.account_id == self.id)).first()
                        child_id = child.id if child else None
                    if child_id:
                        return get_instance(TradingOrder, child_id)
            elif row is not None and ref.nonce in (row.data or {}).get("ibkr_prior_nonces", []):
                # a RETIRED nonce (the row was placed again after this order died): known, expected, quiet
                logger.debug(f"[Account {self.id}] IB order {view.broker_order_id} carries the retired "
                             f"nonce of row {ref.order}; not matched by reference")
            elif row is not None:
                logger.warning(f"[Account {self.id}] IB order {view.broker_order_id} carries orderRef "
                               f"{view.order_ref} but row {ref.order}'s nonce does not match "
                               f"(a recycled id?); not matched by reference")
        wanted = [f"o{view.order_id}"] if view.order_id else []
        if view.perm_id:
            wanted.append(str(view.perm_id))
        if not wanted:
            return None
        with get_db() as session:
            found = session.exec(select(TradingOrder).where(
                TradingOrder.broker_order_id.in_(wanted),
                TradingOrder.account_id == self.id)).first()
            found_id = found.id if found else None
        return get_instance(TradingOrder, found_id) if found_id else None

    def _apply_view(self, row: TradingOrder, view: BrokerOrderView,
                    book: Optional[OrderBook] = None) -> bool:
        """Bring one row in line with IB's view of it. True when anything changed."""
        if (row.status == OrderStatus.ERROR and view.status in M.IB_REJECTION_STATUSES
                and not view.filled):
            return False                      # our own verdict stands: the dead IB order adds nothing
        broker_status = M.map_ib_status(view.status, view.filled, view.remaining)
        if view.status == "ValidationError" and row.status != OrderStatus.PENDING:
            broker_status = row.status        # a WARNING on an order must not regress or revive its row
        changed = False
        if row.status == OrderStatus.PENDING_CANCEL:
            resolved = OrderStatus.resolve_pending_cancel(broker_status)
            if resolved is not None and resolved != row.status:
                logger.info(f"Order {row.id} PENDING_CANCEL -> {resolved.value} "
                            f"(IBKR reported {view.status})")
                row.status, changed = resolved, True
        elif row.status != broker_status:
            logger.debug(f"Order {row.id} status changed: {row.status} -> {broker_status}")
            row.status, changed = broker_status, True
        # A fill only ever GROWS: a view may carry less than the row already recorded (a completed order's
        # empty status record, an execution window that rolled over), and that is never evidence of a
        # smaller fill. Likewise an average price of 0 never replaces a recorded one (guarded below).
        if row.filled_qty is None or float(row.filled_qty) < view.filled - 1e-9:
            row.filled_qty, changed = view.filled, True
        if view.avg_fill_price and view.avg_fill_price > 0 and row.open_price != view.avg_fill_price:
            row.open_price, changed = view.avg_fill_price, True
        elif not row.open_price and view.filled > 0 and book is not None:
            # A completed order carries no average price (its status record is empty): take it from the
            # executions under the row's own ref / the order's permId, never zero or invent one.
            if view.sec_type == "BAG":
                # a combo's executions are its LEGS': the price is the NET per combo unit, never the
                # average of the leg prices
                net = self._combo_from_view(view, book)
                if net is not None:
                    row.open_price, changed = net, True
            else:
                ex = (book.executions_by_ref.get(view.order_ref)
                      or (book.executions_by_perm.get(view.perm_id) if view.perm_id else None))
                if ex and ex.get("price"):
                    row.open_price, changed = ex["price"], True
        working = view.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive")
        if working:
            # What is WORKING at IB is the truth: an OCO leg's size shrinks when the other leg part-fills
            # (ocaType 2), and a price moved in TWS (or a modify IB applied late) must reach the row.
            if view.total_quantity > 0 and (row.quantity is None
                                            or abs(float(row.quantity) - view.total_quantity) > 1e-9):
                row.quantity, changed = view.total_quantity, True
            prices = {"limit_price": None, "stop_price": None}
            if view.order_type == "LMT":
                prices["limit_price"] = view.limit_price
            elif view.order_type == "STP":
                prices["stop_price"] = view.aux_price
            elif view.order_type == "STP LMT":
                prices["limit_price"], prices["stop_price"] = view.limit_price, view.aux_price
            if row.order_type == CoreOrderType.OCO:
                prices["stop_price"] = None           # the parent's stop is its child's: leave it
                if view.order_type != "LMT":
                    prices["limit_price"] = None      # only the TAKE-PROFIT leg's limit is the parent's
            for field_name, value in prices.items():
                current = getattr(row, field_name)
                if value is not None and (current is None or abs(float(current) - value) > 1e-6):
                    setattr(row, field_name, value)
                    changed = True
        wanted_id = view.broker_order_id
        if row.broker_order_id != wanted_id and (
                not row.broker_order_id or (view.perm_id and row.broker_order_id.startswith("o"))):
            row.broker_order_id, changed = wanted_id, True
        if changed:
            update_instance(row)
            if (row.status == OrderStatus.CANCELED and row.filled_qty is not None
                    and row.filled_qty > 0):
                from ba2_common.core.TransactionHelper import TransactionHelper
                TransactionHelper.reconcile_canceled_partial_fill(row)
        return changed

    def _activity(self, severity: str, kind: str, description: str, data: Dict[str, Any]) -> None:
        """A visible Activity Log entry (never raises: a logging failure must not stop a refresh)."""
        try:
            from ba2_common.core.db import log_activity
            from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
            log_activity(severity=ActivityLogSeverity[severity], activity_type=ActivityLogType[kind],
                         description=description, data=data, source_account_id=self.id)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] could not write the activity-log entry: {e}")

    def _settle_unconfirmed_rows(self, book: OrderBook, view_by_row: Dict[int, BrokerOrderView]) -> int:
        """Rows IB does not (confirmably) have. Called only with a COMPLETE order book.

        * An execution under the row's own orderRef (nonce included) or permId settles it as FILLED /
          PARTIALLY_FILLED.
        * A row IB NEVER ACKNOWLEDGED (``PENDING_NEW``) that is on none of IB's lists (a session-only
          ``PendingSubmit`` or ``ValidationError`` trade is a LOCAL object, not IB's: a warning such as 434
          on a new order leaves it in that limbo), has no execution, is older than
          ``_UNACKNOWLEDGED_GRACE_MINUTES`` and younger than the execution window, never reached IBKR:
          ERROR with a clear message and an Activity Log entry, so the platform can proceed (the pending
          quantity and the exit logic stop counting it as live).
        * Anything IB DID acknowledge and that is now on no list is NEVER marked CANCELED or ERROR on that
          evidence (the Gateway restart drops completed orders and executions; it may have filled):
          it stays as it is, with a loud once-per-row warning and an Activity Log entry to reconcile by
          hand or from a Flex statement.
        """
        settled = 0
        terminal = OrderStatus.get_terminal_statuses() | {OrderStatus.FILLED}
        with get_db() as session:
            rows = session.exec(select(TradingOrder).where(
                TradingOrder.account_id == self.id, TradingOrder.status.not_in(list(terminal)),
                or_(TradingOrder.broker_order_id.is_not(None),
                    TradingOrder.status == OrderStatus.PENDING_NEW))).all()
            # a row without a broker id is a candidate only if WE placed it (it carries our nonce): option
            # leg children and other bookkeeping rows legitimately have none
            candidate_ids = [r.id for r in rows
                             if r.broker_order_id or (r.data or {}).get("ibkr_nonce")]
        now = self._utcnow()
        for row_id in candidate_ids:
            view = view_by_row.get(row_id)
            if view is not None and (view.key in book.open_keys or view.key in book.completed_keys
                                     or view.status not in ("PendingSubmit", "ValidationError")):
                continue            # IB lists it, or this session saw IB acknowledge it
            row = get_instance(TradingOrder, row_id)
            created = row.created_at
            placed_at = (row.data or {}).get("ibkr_placed_at")
            if placed_at:
                created = datetime.fromisoformat(placed_at)     # when it went to IB, not when it was made
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (now - created) if created is not None else None
            unacknowledged = row.status == OrderStatus.PENDING_NEW
            grace = self._UNACKNOWLEDGED_GRACE_MINUTES if unacknowledged else self._ABSENT_GRACE_MINUTES
            if age is not None and age < timedelta(minutes=grace):
                continue
            perm = int(row.broker_order_id) if str(row.broker_order_id or "").isdigit() else 0
            ref = self._order_ref_for_row(row, self.id)
            children = []
            if row.asset_class == CoreAssetClass.OPTION and not row.contract_symbol:
                with get_db() as session:
                    children = list(session.exec(select(TradingOrder).where(
                        TradingOrder.parent_order_id == row.id, TradingOrder.account_id == self.id)).all())
            if children:
                # a combo: its executions are LEG fills. Units = smallest leg fill / ratio, price = the net.
                combo = self._combo_from_children(row, children, ref, perm, book)
                ex = None if combo is None else {"shares": combo[0], "price": combo[1]}
            else:
                ex = book.executions_by_ref.get(ref) or (book.executions_by_perm.get(perm) if perm else None)
            if ex:
                row.status = (OrderStatus.FILLED if ex["shares"] + 1e-9 >= float(row.quantity)
                              else OrderStatus.PARTIALLY_FILLED)
                row.filled_qty = ex["shares"]
                if ex["price"] is not None:
                    row.open_price = ex["price"]
                update_instance(row)
                settled += 1
                continue
            data = {"order_id": row.id, "symbol": row.symbol, "broker_order_id": row.broker_order_id,
                    "status": row.status.value}
            if unacknowledged and (age is None or age < timedelta(days=self._EXECUTION_WINDOW_DAYS)):
                msg = (f"never reached IBKR: order {row.id} ({row.symbol}) was never acknowledged by IBKR, "
                       f"is on none of its order lists and has no execution under its orderRef; marked "
                       f"ERROR so the platform can proceed")
                row.status = OrderStatus.ERROR
                row.comment = (f"{row.comment} | {msg}" if row.comment else msg)[:500]
                update_instance(row)
                settled += 1
                logger.error(f"[Account {self.id}] {msg}")
                self._activity("FAILURE", "ORDER_REJECTED", msg, data)
                continue
            msg = (f"UNRESOLVED: order {row.id} ({row.symbol}, broker_order_id={row.broker_order_id}, "
                   f"status {row.status.value}) is on none of IBKR's lists (open, completed, session) and "
                   f"has no execution. It is left UNCHANGED, not cancelled: whether it filled, was "
                   f"cancelled or never existed cannot be told from the API (the Gateway restart drops "
                   f"completed orders and executions). Reconcile it by hand or from a Flex statement.")
            if self._warn_once(f"absent-{row.id}", msg):
                self._activity("WARNING", "ORDER_SUBMITTED", msg, data)
        return settled

    def refresh_orders(self, **kwargs) -> bool:
        """Sync our rows with IBKR's order book. ``**kwargs`` absorbs the Alpaca-specific
        ``heuristic_mapping`` / ``fetch_all`` that the UI and TradeManager pass by name."""
        try:
            book = self._call(self._read_order_book, op="refresh orders", timeout=60.0)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error refreshing orders from IBKR: "
                         f"{type(e).__name__}: {e}", exc_info=True)
            return False
        updated = 0
        view_by_row: Dict[int, BrokerOrderView] = {}
        for view in book.views:
            if view.account and view.account != self._account_id:
                continue
            try:
                row = self._row_for_view(view)
                if row is None:
                    continue
                view_by_row.setdefault(row.id, view)
                if self._apply_view(row, view, book):
                    updated += 1
                if row.asset_class == CoreAssetClass.OPTION and not row.contract_symbol:
                    self._reconcile_option_children(row, view, book)
            except M.UnknownIBOrderStatus as e:
                logger.error(f"[Account {self.id}] {e}")
        absent = self._settle_unconfirmed_rows(book, view_by_row) if book.complete else 0
        triggered = self._check_and_submit_dependent_orders()
        logger.info(f"[Account {self.id}] refreshed IBKR orders: {updated} updated, {absent} "
                    f"settled as unconfirmed, {triggered} dependent orders triggered")
        return True

    # ------------------------------------------------------------------ order preview
    async def _what_if(self, ib: Any, spec: Dict[str, Any]) -> Any:
        resolved = await self._resolve_stock(ib, spec["symbol"])
        rules = await self._market_rules(ib, resolved.details)
        order = self._new_ib_order(action=spec["action"], order_type=spec["ib_type"],
                                   qty=spec["quantity"], tif=spec["tif"], ref="ba2:preview",
                                   limit=spec["limit"], stop=spec["stop"], rules=rules)
        return await self._bounded(ib.whatIfOrderAsync(resolved.contract, order),
                                   "what-if (whatIfOrder)")

    def preview_order_impact(self, trading_order: TradingOrder,
                             is_closing_order: bool = False) -> Optional[OrderImpact]:
        """Broker dry run (``whatIfOrder``): never sends a live order. ``None`` = no precheck."""
        order_type = trading_order.order_type
        ib_type = {CoreOrderType.MARKET: "MKT", CoreOrderType.BUY_LIMIT: "LMT",
                   CoreOrderType.SELL_LIMIT: "LMT", CoreOrderType.BUY_STOP: "STP",
                   CoreOrderType.SELL_STOP: "STP", CoreOrderType.BUY_STOP_LIMIT: "STP LMT",
                   CoreOrderType.SELL_STOP_LIMIT: "STP LMT"}.get(order_type)
        if ib_type is None:
            return None
        try:
            tif, _ = M.ib_time_in_force(trading_order.good_for, is_market=ib_type == "MKT")
            spec = {"symbol": trading_order.symbol, "quantity": float(trading_order.quantity),
                    "action": "BUY" if trading_order.side == OrderDirection.BUY else "SELL",
                    "ib_type": ib_type, "tif": tif, "limit": trading_order.limit_price,
                    "stop": trading_order.stop_price}
            state = self._call(lambda ib: self._what_if(ib, spec), op="what-if",
                               timeout=self._READ_TIMEOUT * 2)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] order preview failed for {trading_order.symbol}: {e}",
                         exc_info=True)
            return None
        init_change = M.ib_number(getattr(state, "initMarginChange", None))
        if init_change is None:
            logger.error(f"[Account {self.id}] IBKR what-if for {trading_order.symbol} published "
                         f"no initial-margin change; no precheck")
            return None
        numbers, _ = self._call(self._account_numbers, op="account for preview")
        multiplier = M.margin_multiplier_from(numbers)
        warning = str(getattr(state, "warningText", "") or "")
        commission = M.ib_number(getattr(state, "commission", None))
        return OrderImpact(
            symbol=trading_order.symbol,
            change_in_buying_power=-(init_change * multiplier),
            margin_requirement=abs(init_change),
            estimated_fees=abs(commission) if commission is not None else None,
            accepted=not warning.lower().startswith("error"),
            warnings=[warning] if warning else [], errors=[],
            raw={"init_margin_change": init_change,
                 "maint_margin_change": M.ib_number(getattr(state, "maintMarginChange", None)),
                 "equity_with_loan_change": M.ib_number(getattr(state, "equityWithLoanChange", None))})

    # ------------------------------------------------------------------ margin metadata
    def get_symbol_margin_info(self, symbols: List[str]) -> Dict[str, MarginInfo]:
        wanted = [s.strip().upper() for s in (symbols or []) if s and s.strip()]
        if not wanted:
            return {}
        try:
            resolved = self._call(lambda ib: self._resolve_many(ib, wanted),
                                  op=f"margin info x{len(wanted)}",
                                  timeout=self._READ_TIMEOUT + 0.1 * len(wanted))
            snapshot = self.get_account_snapshot()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Account {self.id}] margin metadata fetch failed: {e}")
            return {}
        is_margin = bool(snapshot.is_margin_account)
        out: Dict[str, MarginInfo] = {}
        for symbol in wanted:
            res = resolved.get(symbol)
            if res is None:
                continue
            fractional = M.fractionable_from_size(getattr(res.details, "minSize", None),
                                                  getattr(res.details, "sizeIncrement", None))
            step = M.ib_number(getattr(res.details, "sizeIncrement", None))
            out[symbol] = MarginInfo(
                symbol=symbol, bp_factor=1.0, marginable=is_margin, fractionable=fractional,
                tradable=True,
                min_order_size=M.ib_number(getattr(res.details, "minSize", None)),
                min_trade_increment=(step if fractional is True and step else
                                     (1.0 if fractional is False else None)),
                min_fractional_notional=None, initial_margin_rate=None,
                maintenance_margin_rate=None, source=MARGIN_SOURCE_DEFAULT)
        return out

    # ------------------------------------------------------------------ history seams
    def _warn_once(self, key: str, message: str) -> bool:
        """Log ``message`` the first time ``key`` is seen; True when it was the first time."""
        warned = self._runtime().state.warned
        if key in warned:
            return False
        warned.add(key)
        logger.warning(f"[Account {self.id}] {message}")
        return True

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        """Filled equity executions from ``reqExecutions`` (today, or up to ~7 days)."""
        try:
            fills = self._call(self._read_fills, op="executions", timeout=40.0)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Account {self.id}] error fetching filled trades: {e}", exc_info=True)
            return []
        if start_date is not None:
            floor = self._utcnow() - timedelta(days=self._EXECUTION_WINDOW_DAYS)
            start = start_date if start_date.tzinfo else start_date.replace(tzinfo=timezone.utc)
            if start < floor:
                self._warn_once("fills-window", (
                    f"get_filled_trades(start_date={start_date:%Y-%m-%d}) asks for more than "
                    f"{self._EXECUTION_WINDOW_DAYS} days: the IBKR API keeps only recent "
                    f"executions, so the result is PARTIAL (use a Flex query for full history)"))
        trades = []
        for fill in fills:
            ex, contract = fill.execution, fill.contract
            if contract.secType != "STK" or float(ex.shares or 0) <= 0:
                continue
            sym = M.from_ib_symbol(contract.symbol)
            if symbol and sym != symbol.upper():
                continue
            when = ex.time
            if start_date and when < (start_date if start_date.tzinfo
                                      else start_date.replace(tzinfo=timezone.utc)):
                continue
            if end_date and when > (end_date if end_date.tzinfo
                                    else end_date.replace(tzinfo=timezone.utc)):
                continue
            trades.append({"symbol": sym, "qty": float(ex.shares),
                           "side": "BUY" if ex.side == "BOT" else "SELL", "date": when,
                           "price": float(ex.price)})
        return trades

    async def _read_fills(self, ib: Any) -> List[Any]:
        return await self._executions(ib)

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return self._flex_dividends(symbol, start_date, end_date)

    def get_balance_history(self, start_date=None, end_date=None):
        return self._flex_balance_history(start_date, end_date)

    def get_cash_transfers(self, start_date=None, end_date=None):
        return self._flex_cash_transfers(start_date, end_date)

    # ------------------------------------------------------------------ Flex Web Service seams
    #: HTTP seam (tests inject a fake); the real one is urllib against IBKR's fixed Flex host.
    _flex_http_get = staticmethod(default_http_get)
    _FLEX_CACHE_SECONDS = 600.0

    def _flex_statements(self, what: str) -> Optional[List[FlexStatement]]:
        """The Flex statement(s) for this account, cached 10 minutes (IBKR rate-limits Flex), or
        ``None`` when Flex is not configured or the fetch failed (both logged)."""
        token, query_id = self.settings.get("flex_token"), self.settings.get("flex_query_id")
        if not token or not query_id:
            self._warn_once(f"flex-{what}", (
                f"IBKR provides no {what} through the TWS API. Set 'flex_token' and "
                f"'flex_query_id' (an Activity Flex Query) in the account settings to enable it; "
                f"returning []."))
            return None
        state = self._runtime().state
        cached = state.flex
        if cached is not None and time.monotonic() - cached[0] < self._FLEX_CACHE_SECONDS:
            return cached[1]
        try:
            statements = FlexClient(str(token), str(query_id), http_get=self._flex_http_get).fetch(
                self._account_id)
        except Exception as e:  # noqa: BLE001 -- a seam without tri-state: log + []
            logger.error(f"[Account {self.id}] IBKR Flex fetch for {what} failed: "
                         f"{type(e).__name__}: {e}", exc_info=True)
            return None
        if not statements:
            logger.error(f"[Account {self.id}] the Flex statement contains no data for account "
                         f"{self._account_id}; check the query's account selection")
        state.flex = (time.monotonic(), statements)
        return statements

    def _flex_dividends(self, symbol, start_date, end_date):
        statements = self._flex_statements("dividend history")
        return [] if statements is None else flex.dividends_from(statements, symbol, start_date,
                                                                   end_date)

    def _flex_balance_history(self, start_date, end_date):
        statements = self._flex_statements("balance history")
        return [] if statements is None else flex.balance_history_from(statements, start_date,
                                                                      end_date)

    def _flex_cash_transfers(self, start_date, end_date):
        statements = self._flex_statements("cash-transfer history")
        return [] if statements is None else flex.cash_transfers_from(statements, start_date,
                                                                     end_date)


class _Patch:
    """A minimal stand-in for the fields ``modify_order`` reads from a ``TradingOrder``."""

    def __init__(self, *, limit_price=None, stop_price=None, quantity=None, good_for=None):
        self.limit_price, self.stop_price = limit_price, stop_price
        self.quantity, self.good_for = quantity, good_for
