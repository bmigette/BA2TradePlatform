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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from ib_async import IB, Order, Stock
from sqlmodel import select

from ba2_common.core import ibkr_flex as flex
from ba2_common.core import ibkr_mapping as M
from ba2_common.core.ibkr_flex import FlexClient, FlexStatement, default_http_get
from ba2_common.core.protective_legs import ProtectiveLegsMixin

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
from .AlpacaAccount import OCO_STOP_LIMIT_CUSHION
from .ibkr_options import IBKROptionsMixin
from .ibkr_runtime import (
    IBKRConnectionError, IBKRContractError, IBKRError, IBKROrderRejected, IBKRReadOnlyError,
    IBKRRuntime, get_runtime, shutdown_runtime)


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
        return cls(
            order_id=int(order.orderId or 0),
            perm_id=int(getattr(status, "permId", 0) or getattr(order, "permId", 0) or 0),
            order_ref=str(order.orderRef or ""),
            account=str(order.account or getattr(status, "account", "") or ""),
            status=str(status.status or ""),
            filled=float(M.ib_number(status.filled) or 0.0),
            remaining=float(M.ib_number(status.remaining) or 0.0),
            avg_fill_price=M.ib_number(status.avgFillPrice),
            action=str(order.action or ""),
            order_type=str(order.orderType or ""),
            total_quantity=float(M.ib_number(order.totalQuantity) or 0.0),
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

    @property
    def complete(self) -> bool:
        return self.open_ok and self.completed_ok and self.executions_ok


class IBKRAccount(ProtectiveLegsMixin, IBKROptionsMixin, AccountInterface, OptionsAccountInterface):
    """Interactive Brokers via TWS / IB Gateway (see the module docstring)."""

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

    def _runtime(self) -> IBKRRuntime:
        """This account's shared runtime (one TWS session per account definition, however many
        ``IBKRAccount`` objects exist for it)."""
        rt = getattr(self, "_rt", None)
        if rt is not None:
            return rt
        from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool
        get = lambda k: self.get_setting_with_interface_default(k, log_warning=False)  # noqa: E731
        host, port, client_id = str(get("host")), int(get("port")), int(get("client_id"))
        account_id = str(get("account_id")).strip()
        paper, read_only = coerce_bool(get("paper_account")), coerce_bool(get("read_only"))
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
            "flex_token": {"type": "str", "required": False,
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

    # ------------------------------------------------------------------ contracts (loop thread)
    async def _resolve_stock(self, ib: Any, symbol: str) -> _Resolved:
        key = (symbol or "").strip().upper()
        cached = self._runtime().state.contracts.get(key)
        now = time.monotonic()
        if cached is not None and now - cached.fetched_at < self._CONTRACT_TTL:
            return cached
        probe = Stock(M.to_ib_symbol(key), "SMART", "USD")
        details = await asyncio.wait_for(ib.reqContractDetailsAsync(probe), self._READ_TIMEOUT)
        stocks = [d for d in (details or [])
                  if getattr(d.contract, "secType", "") == "STK"
                  and getattr(d.contract, "currency", "USD") == "USD"]
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
        rows = await asyncio.wait_for(ib.reqMarketRuleAsync(int(rule_id)), self._READ_TIMEOUT)
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
        ladder = [price_type] + [n for n in ("mid", "last", "close") if n != price_type]
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
                tickers = await asyncio.wait_for(
                    ib.reqTickersAsync(*[r.contract for _, r in chunk]), self._READ_TIMEOUT)
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
        rows = await asyncio.wait_for(ib.accountSummaryAsync(self._account_id), self._READ_TIMEOUT)
        return M.select_account_values(rows, self._account_id)

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
            return {}
        snap = M.snapshot_from_account_values(numbers, texts, None, None)
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
    async def _equity_positions_payload(self, ib: Any) -> List[Dict[str, Any]]:
        account = self._account_id
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
                for t in await asyncio.wait_for(ib.reqTickersAsync(*need_snapshot), self._READ_TIMEOUT):
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
            open_trades = list(await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), self._READ_TIMEOUT))
        except Exception as e:  # noqa: BLE001
            open_ok = False
            logger.error(f"[Account {self.id}] could not read IBKR open orders: {e}", exc_info=True)
        try:
            completed = list(await asyncio.wait_for(ib.reqCompletedOrdersAsync(False), self._READ_TIMEOUT))
        except Exception as e:  # noqa: BLE001
            completed_ok = False
            logger.error(f"[Account {self.id}] could not read IBKR completed orders: {e}",
                         exc_info=True)
        views: Dict[Tuple, BrokerOrderView] = {}
        for trade in list(ib.trades()) + open_trades + completed:
            view = BrokerOrderView.from_trade(trade)
            views.setdefault(view.key, view)
        book = OrderBook(views=list(views.values()), open_ok=open_ok, completed_ok=completed_ok)
        try:
            fills = await asyncio.wait_for(ib.reqExecutionsAsync(), self._READ_TIMEOUT)
            self._aggregate_executions(book, fills)
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
            if fill.contract.secType == "OPT" and ex.permId:
                try:
                    buckets.append((book.executions_by_perm_occ,
                                    (int(ex.permId), cls._occ_of(fill.contract))))
                except ValueError:
                    logger.warning(f"execution {ex.execId}: cannot derive an OCC symbol from "
                                   f"{fill.contract.localSymbol!r}; leg price not attributed")
            for bucket, key in buckets:
                if not key:
                    continue
                agg = bucket.setdefault(key, {"shares": 0.0, "notional": 0.0, "when": None})
                agg["shares"] += shares
                agg["notional"] += shares * price
                agg["when"] = ex.time
        for bucket in (book.executions_by_ref, book.executions_by_perm, book.executions_by_perm_occ):
            for agg in bucket.values():
                agg["price"] = agg["notional"] / agg["shares"] if agg["shares"] else None

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
    async def _wait_ack(self, ib: Any, trade: Any, timeout: float) -> BrokerOrderView:
        """Wait for IB to acknowledge or reject a placed order (bounded; never resends).

        Raises ``IBKROrderRejected`` on a rejection. Returns the view as it stands at the deadline
        otherwise (``PendingSubmit`` means "no answer yet", which the caller records and
        ``refresh_orders`` resolves).
        """
        order_id = int(trade.order.orderId)
        deadline = time.monotonic() + timeout
        rt = self._runtime()
        while True:
            status = str(trade.orderStatus.status or "")
            errors = rt.order_errors(order_id)
            if status in ("Cancelled", "Inactive", "ApiCancelled", "ValidationError"):
                code, text = (errors[-1] if errors else (None, trade.log[-1].message if trade.log
                                                         else status))
                raise IBKROrderRejected(f"IB error {code}: {text}" if code else
                                        f"IBKR {status}: {text}", code)
            if status in ("Submitted", "PreSubmitted", "Filled", "ApiUpdate"):
                return BrokerOrderView.from_trade(trade)
            if errors and time.monotonic() > deadline - timeout + 0.2:
                code, text = errors[-1]
                raise IBKROrderRejected(f"IB error {code}: {text}", code)
            if time.monotonic() >= deadline:
                return BrokerOrderView.from_trade(trade)
            await asyncio.sleep(0.02)

    async def _short_check(self, ib: Any, contract: Any, symbol: str) -> None:
        """Refuse to open a short unless IB reports the stock shortable AND easy to borrow."""
        ticker = ib.reqMktData(contract, "236", False, False)
        try:
            deadline = time.monotonic() + 3.0
            while M.ib_number(getattr(ticker, "shortableShares", None)) is None \
                    and time.monotonic() < deadline:
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

    async def _place_single(self, ib: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
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
        trade = ib.placeOrder(contract, order)
        view = await self._wait_ack(ib, trade, self._ORDER_ACK_TIMEOUT)
        return {"view": view, "quantity": qty, "tif": spec["tif"]}

    async def _place_oco(self, ib: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
        resolved = await self._resolve_stock(ib, spec["symbol"])
        rules = await self._market_rules(ib, resolved.details)
        qty = float(spec["quantity"])
        self._refuse_fractional_priced_order(spec["symbol"], qty, CoreOrderType.OCO)
        tp = M.round_to_increment(spec["tp"], rules)
        sl_stop = M.round_to_increment(spec["sl"], rules)
        cushion = (1.0 - OCO_STOP_LIMIT_CUSHION) if spec["action"] == "SELL" else (1.0 + OCO_STOP_LIMIT_CUSHION)
        sl_limit = M.round_to_increment(sl_stop * cushion, rules)
        group = spec["oca_group"]
        tp_order = self._new_ib_order(action=spec["action"], order_type="LMT", qty=qty,
                                      tif=spec["tif"], ref=spec["tp_ref"], limit=tp, rules=rules)
        sl_order = self._new_ib_order(action=spec["action"], order_type="STP LMT", qty=qty,
                                      tif=spec["tif"], ref=spec["sl_ref"], limit=sl_limit,
                                      stop=sl_stop, rules=rules)
        for leg in (tp_order, sl_order):
            leg.ocaGroup, leg.ocaType = group, 2   # 2 = reduce remaining proportionally, with block
        # TP goes untransmitted, SL transmits both: TWS releases the pair or neither.
        tp_order.transmit, sl_order.transmit = False, True
        tp_trade = ib.placeOrder(resolved.contract, tp_order)
        try:
            sl_trade = ib.placeOrder(resolved.contract, sl_order)
        except Exception:
            ib.cancelOrder(tp_trade.order)
            raise
        try:
            tp_view = await self._wait_ack(ib, tp_trade, self._ORDER_ACK_TIMEOUT)
            sl_view = await self._wait_ack(ib, sl_trade, self._ORDER_ACK_TIMEOUT)
        except IBKROrderRejected:
            # One leg refused: never leave the other working alone.
            for trade in (tp_trade, sl_trade):
                if str(trade.orderStatus.status) not in ("Cancelled", "ApiCancelled", "Inactive"):
                    ib.cancelOrder(trade.order)
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
        if not fresh.broker_order_id:
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
        return fresh

    def _submit_order_impl(self, trading_order: TradingOrder, tp_price: Optional[float] = None,
                           sl_price: Optional[float] = None, is_closing_order: bool = False,
                           use_complex_order: bool = False) -> Optional[TradingOrder]:
        """Send ONE equity order (or one OCO pair) to IBKR.

        ``tp_price``/``sl_price`` are accepted for the interface and deliberately not acted on
        here: ``AccountInterface.submit_order`` calls ``adjust_tp_sl`` after this returns, which
        builds the protective exit. ``use_complex_order`` cannot occur (no wash-trade lock) and
        raises rather than being ignored.
        """
        if use_complex_order:
            raise NotImplementedError(
                f"[Account {self.id}] IBKR sets no wash-trade lock, so a complex-order submission "
                f"for {trading_order.symbol} is a broken invariant; refusing to ignore it")
        if trading_order.broker_order_id:
            logger.warning(f"Order {trading_order.id} already has broker_order_id "
                           f"{trading_order.broker_order_id} -- skipping re-submission")
            return trading_order
        try:
            if trading_order.id is None:
                trading_order.status = OrderStatus.PENDING
                trading_order.id = add_instance(trading_order, expunge_after_flush=True)
            self._refuse_if_read_only("submit orders")
            if trading_order.asset_class == CoreAssetClass.OPTION:
                raise ValueError("option orders go through submit_option_order, not submit_order")
            order_type = trading_order.order_type
            action = "BUY" if trading_order.side == OrderDirection.BUY else "SELL"
            ref = M.make_order_ref(self.id, trading_order.id)
            if order_type == CoreOrderType.OCO:
                return self._submit_oco(trading_order, action)
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
            if warning:
                logger.warning(f"Order {trading_order.id}: {warning}")
            spec = {
                "symbol": trading_order.symbol, "quantity": float(trading_order.quantity),
                "order_type": order_type, "ib_type": ib_type, "action": action, "tif": tif,
                "limit": trading_order.limit_price, "stop": trading_order.stop_price,
                "order_ref": ref,
                "opens_short": self._order_opens_short(trading_order, is_closing_order),
            }
            try:
                placed = self._call(lambda ib: self._place_single(ib, spec),
                                    op=f"place order {trading_order.id}",
                                    timeout=self._ORDER_ACK_TIMEOUT + self._READ_TIMEOUT)
            except _ZeroQuantityAfterRounding as e:
                logger.warning(f"Order {trading_order.id} ({trading_order.symbol}) skipped: {e}")
                self._record_skip(trading_order, f"skipped: {e}")
                return None
            fresh = self._persist_submission(trading_order.id, placed["view"], placed["tif"],
                                             quantity=placed["quantity"])
            logger.info(f"Submitted IBKR order {fresh.id}: broker_order_id={fresh.broker_order_id}, "
                        f"status={fresh.status}")
            return fresh
        except Exception as e:  # noqa: BLE001 -- recorded on the row, never swallowed
            logger.error(f"Error submitting order {trading_order.id} to IBKR: {e}", exc_info=True)
            if trading_order.id:
                return self._handle_order_submit_error(trading_order, e)
            logger.warning("Cannot mark order as ERROR - order has no ID")
            return None

    def _submit_oco(self, parent: TradingOrder, action: str) -> TradingOrder:
        if not parent.limit_price or parent.limit_price <= 0:
            raise ValueError("Limit price (take profit) is required for OCO orders")
        if not parent.stop_price or parent.stop_price <= 0:
            raise ValueError("Stop price (stop loss) is required for OCO orders")
        tif, _ = M.ib_time_in_force(parent.good_for, is_market=False)
        spec = {
            "symbol": parent.symbol, "quantity": float(parent.quantity), "action": action,
            "tp": float(parent.limit_price), "sl": float(parent.stop_price), "tif": tif,
            "oca_group": f"ba2-oca-{self.id}-{parent.id}",
            "tp_ref": M.make_order_ref(self.id, parent.id),
            "sl_ref": M.make_order_ref(self.id, parent.id, "SL"),
        }
        placed = self._call(lambda ib: self._place_oco(ib, spec), op=f"place OCO {parent.id}",
                            timeout=2 * self._ORDER_ACK_TIMEOUT + self._READ_TIMEOUT)
        tp_view, sl_view = placed["tp"], placed["sl"]
        fresh = self._persist_submission(parent.id, tp_view, tif)
        fresh.legs_broker_ids = [tp_view.broker_order_id, sl_view.broker_order_id]
        fresh.data = {**(fresh.data or {}), "ibkr": {
            "oca_group": spec["oca_group"], "tp_order_id": tp_view.order_id,
            "sl_order_id": sl_view.order_id, "sl_limit": placed["sl_limit"]}}
        update_instance(fresh)
        child = TradingOrder(
            account_id=self.id, symbol=parent.symbol, quantity=parent.quantity, side=parent.side,
            order_type=(CoreOrderType.SELL_STOP_LIMIT if action == "SELL"
                        else CoreOrderType.BUY_STOP_LIMIT),
            broker_order_id=sl_view.broker_order_id, limit_price=placed["sl_limit"],
            stop_price=placed["sl_stop"], good_for=tif.lower(),
            status=M.map_ib_status(sl_view.status, sl_view.filled, sl_view.remaining),
            filled_qty=sl_view.filled or None,
            comment=f"{int(self._utcnow().timestamp())}-OCO-SL-[PARENT:{parent.id}/BROKER:"
                    f"{tp_view.broker_order_id}]",
            transaction_id=parent.transaction_id, parent_order_id=parent.id,
            created_at=self._utcnow())
        add_instance(child)
        logger.info(f"Submitted IBKR OCO {parent.id}: TP leg {tp_view.broker_order_id}, "
                    f"SL leg {sl_view.broker_order_id}")
        return fresh

    # ------------------------------------------------------------------ cancel / modify
    @staticmethod
    def _order_ref_for_row(row: TradingOrder, account_id: int) -> str:
        """The ``orderRef`` this row's IB order carries (an OCO stop leg is ``<parent>:SL``)."""
        if row.parent_order_id and row.order_type in (CoreOrderType.SELL_STOP_LIMIT,
                                                      CoreOrderType.BUY_STOP_LIMIT):
            return M.make_order_ref(account_id, row.parent_order_id, "SL")
        return M.make_order_ref(account_id, row.id)

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
                await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), self._READ_TIMEOUT)
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
            if current == "Filled":
                logger.error(f"[Account {self.id}] cannot cancel {row['broker_order_id']}: it has "
                             f"already FILLED at IBKR")
                results[row["broker_order_id"]] = False
                continue
            if current in ("Cancelled", "ApiCancelled", "Inactive"):
                # Already not working: the goal is met; refresh_orders records the final status.
                results[row["broker_order_id"]] = True
                continue
            ib.cancelOrder(trade.order)
            deadline = time.monotonic() + self._CANCEL_ACK_TIMEOUT
            refused = None
            while time.monotonic() < deadline:
                if str(trade.orderStatus.status) in ("PendingCancel", "Cancelled", "ApiCancelled"):
                    break
                errors = [(c, m) for c, m in self._runtime().errors_by_req.get(order_id, [])
                          if c in M.ORDER_STATE_CODES]
                if errors:
                    refused = errors[-1]
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

    async def _modify_order_object(self, ib: Any, row: Dict[str, Any], *, qty: Optional[float],
                                   limit: Optional[float], stop: Optional[float],
                                   tif: Optional[str], symbol: str) -> Dict[str, Any]:
        trade = await self._find_trade(ib, row)
        if trade is None:
            raise IBKRContractError(
                f"order {row['broker_order_id']} is not known to this IBKR session; only orders "
                f"placed by this client id can be modified in place")
        resolved = await self._resolve_stock(ib, symbol)
        rules = await self._market_rules(ib, resolved.details)
        order = trade.order
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
        new_trade = ib.placeOrder(resolved.contract, order)   # same orderId => in-place modify
        view = await self._wait_ack(ib, new_trade, self._ORDER_ACK_TIMEOUT)
        return {"view": view, **applied}

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
                op=f"modify {db_order.id}", timeout=self._READ_TIMEOUT + self._ORDER_ACK_TIMEOUT)
            view = result["view"]
            fresh = get_instance(TradingOrder, db_order.id)
            # Persist what IB was actually sent (rounded to its tick), not what was asked for.
            if result["limit"] is not None:
                fresh.limit_price = result["limit"]
            if result["stop"] is not None:
                fresh.stop_price = result["stop"]
            if qty is not None:
                fresh.quantity = qty
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
        ref = M.parse_order_ref(view.order_ref)
        if ref is not None and ref[0] == self.id:
            _, order_id, suffix = ref
            if suffix is None:
                try:
                    row = get_instance(TradingOrder, order_id)
                except InstanceNotFound:
                    row = None
                if row is not None and row.account_id == self.id:
                    return row
            elif suffix == "SL":
                with get_db() as session:
                    child = session.exec(select(TradingOrder).where(
                        TradingOrder.parent_order_id == order_id,
                        TradingOrder.account_id == self.id)).first()
                    child_id = child.id if child else None
                if child_id:
                    return get_instance(TradingOrder, child_id)
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

    def _apply_view(self, row: TradingOrder, view: BrokerOrderView) -> bool:
        """Bring one row in line with IB's view of it. True when anything changed."""
        broker_status = M.map_ib_status(view.status, view.filled, view.remaining)
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
        if row.filled_qty is None or float(row.filled_qty) != view.filled:
            row.filled_qty, changed = view.filled, True
        if view.avg_fill_price and view.avg_fill_price > 0 and row.open_price != view.avg_fill_price:
            row.open_price, changed = view.avg_fill_price, True
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

    def _settle_absent_rows(self, book: OrderBook, matched_ids: set) -> int:
        """Rows with a broker id that IB lists nowhere. Absence from the open list is authoritative
        for "not working", but FILLED is only knowable from fills (kept for ~7 days)."""
        settled = 0
        terminal = OrderStatus.get_terminal_statuses() | {OrderStatus.FILLED}
        with get_db() as session:
            rows = session.exec(select(TradingOrder).where(
                TradingOrder.account_id == self.id, TradingOrder.broker_order_id.is_not(None),
                TradingOrder.status.not_in(list(terminal)))).all()
            candidate_ids = [r.id for r in rows if r.id not in matched_ids]
        now = self._utcnow()
        for row_id in candidate_ids:
            row = get_instance(TradingOrder, row_id)
            created = row.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (now - created) if created else None
            if age is not None and age < timedelta(minutes=self._ABSENT_GRACE_MINUTES):
                continue
            perm = int(row.broker_order_id) if str(row.broker_order_id).isdigit() else 0
            ex = book.executions_by_ref.get(M.make_order_ref(self.id, row.id)) \
                or (book.executions_by_perm.get(perm) if perm else None)
            if ex:
                row.status = (OrderStatus.FILLED if ex["shares"] + 1e-9 >= float(row.quantity)
                              else OrderStatus.PARTIALLY_FILLED)
                row.filled_qty, row.open_price = ex["shares"], ex["price"]
                update_instance(row)
                settled += 1
                continue
            if age is not None and age > timedelta(days=self._EXECUTION_WINDOW_DAYS):
                logger.warning(f"Order {row.id} (broker_order_id={row.broker_order_id}) is on no "
                               f"IBKR list and is older than the {self._EXECUTION_WINDOW_DAYS}-day "
                               f"execution window: cannot verify whether it filled; left unchanged")
                continue
            logger.warning(f"Order {row.id} (broker_order_id={row.broker_order_id}) is not working "
                           f"at IBKR and has no execution: marking CANCELED")
            row.status = OrderStatus.CANCELED
            update_instance(row)
            settled += 1
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
        matched: set = set()
        for view in book.views:
            if view.account and view.account != self._account_id:
                continue
            try:
                row = self._row_for_view(view)
                if row is None:
                    continue
                matched.add(row.id)
                if self._apply_view(row, view):
                    updated += 1
                if row.asset_class == CoreAssetClass.OPTION and not row.contract_symbol:
                    self._reconcile_option_children(row, view, book)
            except M.UnknownIBOrderStatus as e:
                logger.error(f"[Account {self.id}] {e}")
        absent = self._settle_absent_rows(book, matched) if book.complete else 0
        triggered = self._check_and_submit_dependent_orders()
        logger.info(f"[Account {self.id}] refreshed IBKR orders: {updated} updated, {absent} "
                    f"settled as absent, {triggered} dependent orders triggered")
        return True

    # ------------------------------------------------------------------ order preview
    async def _what_if(self, ib: Any, spec: Dict[str, Any]) -> Any:
        resolved = await self._resolve_stock(ib, spec["symbol"])
        rules = await self._market_rules(ib, resolved.details)
        order = self._new_ib_order(action=spec["action"], order_type=spec["ib_type"],
                                   qty=spec["quantity"], tif=spec["tif"], ref="ba2:preview",
                                   limit=spec["limit"], stop=spec["stop"], rules=rules)
        return await asyncio.wait_for(ib.whatIfOrderAsync(resolved.contract, order),
                                      self._READ_TIMEOUT)

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
    def _warn_once(self, key: str, message: str) -> None:
        warned = self._runtime().state.warned
        if key not in warned:
            warned.add(key)
            logger.warning(f"[Account {self.id}] {message}")

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
        return list(await asyncio.wait_for(ib.reqExecutionsAsync(), self._READ_TIMEOUT))

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
        cached = getattr(state, "flex", None)
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
