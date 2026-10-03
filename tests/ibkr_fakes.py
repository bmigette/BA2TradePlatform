"""A behavioural fake of the ``ib_async.IB`` surface ``IBKRAccount`` uses. NO network, ever.

It speaks the REAL ib_async value types (``Trade``, ``OrderStatus``, ``Position``, ``PortfolioItem``,
``AccountValue``, ``ContractDetails``, ``Ticker``, ``Fill``, ``Execution`` ...), so a misspelt field in
the adapter fails here exactly as it would against a Gateway. What it fakes is IB's behaviour: order
acknowledgement, rejection, partial fills, cancel handshakes, OCA groups, untransmitted orders,
disconnects, client-id collisions, error callbacks.

Everything that mutates state runs ON THE ADAPTER'S LOOP THREAD (the adapter calls the fake from
coroutines there); the ``simulate_*`` helpers are for the TEST thread and hop onto that loop.

Usage: ``fake = FakeIB(); monkeypatch.setattr(IBKRAccount, "_ib_factory", staticmethod(lambda: fake))``.
"""
from __future__ import annotations

import asyncio
import copy
import math
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ib_async import (
    AccountValue, ComboLeg, Contract, ContractDetails, Execution, Fill, Option, OptionChain,
    Order, OrderState, PortfolioItem, Position, PriceIncrement, Stock, Ticker, Trade,
    TradeLogEntry)
from ib_async import OrderStatus as IBOrderStatus
from ib_async.objects import CommissionReport, OptionComputation

NAN = float("nan")
FINAL = ("Filled", "Cancelled", "ApiCancelled", "Inactive")


class FakeEvent:
    """The ``ib.errorEvent += handler`` idiom."""

    def __init__(self) -> None:
        self.handlers: List[Callable[..., Any]] = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


def default_account_rows(account: str) -> List[AccountValue]:
    values = {
        "AccountType": "INDIVIDUAL", "NetLiquidation": "100000", "TotalCashValue": "50000",
        "SettledCash": "48000", "AvailableFunds": "80000", "BuyingPower": "320000",
        "ExcessLiquidity": "90000", "InitMarginReq": "20000", "MaintMarginReq": "15000",
        "EquityWithLoanValue": "100000", "GrossPositionValue": "50000", "Cushion": "0.9",
    }
    return [AccountValue(account, tag, value,
                         "" if tag in ("AccountType", "Cushion") else "USD", "")
            for tag, value in values.items()]


class FakeIB:
    """See the module docstring."""

    def __init__(self, account: str = "DU1234567", managed: Optional[List[str]] = None) -> None:
        self.errorEvent = FakeEvent()
        self.disconnectedEvent = FakeEvent()
        self.account = account
        self.managed = managed if managed is not None else [account]
        self._connected = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client_id: Optional[int] = None
        # connect scripting
        self.connect_calls: List[Dict[str, Any]] = []
        self.connect_failure: Optional[BaseException] = None
        self.client_id_in_use = False
        self.market_data_type: Optional[int] = None
        # account data
        self.account_rows: List[AccountValue] = default_account_rows(account)
        #: when set, accountSummaryAsync serves THESE rows instead of ``account_rows`` (to test staleness)
        self.summary_rows: Optional[List[AccountValue]] = None
        self.updates_available = True
        self._positions: List[Position] = []
        self._portfolio: List[PortfolioItem] = []
        # contracts
        self.details: Dict[str, List[ContractDetails]] = {}
        self.market_rules: Dict[int, List[PriceIncrement]] = {26: [PriceIncrement(0.0, 0.01)]}
        self.quotes: Dict[int, Ticker] = {}
        self.shortable_shares: Dict[int, float] = {}
        self.detail_calls = 0
        self.active_lines = 0
        self.max_active_lines = 0
        self.cancelled_lines = 0
        # orders
        self._trades: List[Trade] = []
        self.prior_trades: List[Trade] = []
        self.fills_list: List[Fill] = []
        self._next_order_id = 100
        self._next_perm = 1_000_000_000
        self.behaviors: List[Any] = []
        self.default_behavior: Any = "accept"
        self.hold_cancels = False
        self.placed: List[Dict[str, Any]] = []
        self.cancel_requests: List[int] = []
        self.what_if = OrderState(status="PreSubmitted", initMarginChange=5000.0,
                                  maintMarginChange=4000.0, equityWithLoanChange=0.0,
                                  commission=1.0, warningText="")
        self.fail_calls: Dict[str, BaseException] = {}
        self.block_calls: Dict[str, float] = {}
        self.call_threads: Dict[str, int] = {}
        # options
        self.option_chains: Dict[str, List[OptionChain]] = {}
        self.option_quotes: Dict[int, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ plumbing
    def _note(self, name: str) -> None:
        import threading
        self.call_threads[name] = threading.get_ident()

    async def _maybe_fail(self, name: str) -> None:
        self._note(name)
        delay = self.block_calls.get(name)
        if delay:
            await asyncio.sleep(delay)
        exc = self.fail_calls.get(name)
        if exc is not None:
            raise exc

    def _con_id_for(self, symbol: str) -> int:
        return abs(hash(symbol)) % 900000 + 100000

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    # ------------------------------------------------------------------ connection
    def isConnected(self) -> bool:
        return self._connected

    async def connectAsync(self, host="127.0.0.1", port=7497, clientId=1, timeout=4, readonly=False,
                           account="", **kwargs) -> None:
        self.loop = asyncio.get_running_loop()
        self.connect_calls.append({"host": host, "port": port, "clientId": clientId,
                                   "readonly": readonly, "account": account})
        self._note("connectAsync")
        if self.connect_failure is not None:
            raise self.connect_failure
        if self.client_id_in_use:
            self.errorEvent.emit(-1, 326, "Unable to connect as the client id is already in use. "
                                          "Retry with a unique client id.", None)
            raise ConnectionRefusedError("client id in use")
        self._connected = True
        self.client_id = clientId

    def disconnect(self) -> None:
        was = self._connected
        self._connected = False
        if was:
            self.disconnectedEvent.emit()

    def simulate_disconnect(self) -> None:
        asyncio.run_coroutine_threadsafe(self._async(self.disconnect), self.loop).result(5)

    def simulate_error(self, req_id: int, code: int, text: str) -> None:
        asyncio.run_coroutine_threadsafe(
            self._async(lambda: self.errorEvent.emit(req_id, code, text, None)), self.loop).result(5)

    @staticmethod
    async def _async(fn: Callable[[], Any]) -> Any:
        return fn()

    def managedAccounts(self) -> List[str]:
        return list(self.managed)

    def reqMarketDataType(self, marketDataType: int) -> None:
        self.market_data_type = marketDataType

    async def reqCurrentTimeAsync(self) -> datetime:
        await self._maybe_fail("reqCurrentTimeAsync")
        return self.now()

    # ------------------------------------------------------------------ account
    async def accountSummaryAsync(self, account: str = "") -> List[AccountValue]:
        await self._maybe_fail("accountSummaryAsync")
        return [r for r in self.account_summary_rows() if not account or r.account == account]

    def accountValues(self, account: str = "") -> List[AccountValue]:
        """The account-updates stream (what ib_async keeps live after connect). Empty when
        ``updates_available`` is False, which forces the adapter onto the summary fallback."""
        self._note("accountValues")
        if "accountValues" in self.fail_calls:
            raise self.fail_calls["accountValues"]
        if not self.updates_available:
            return []
        return [r for r in self.account_rows if not account or r.account == account]

    def account_summary_rows(self) -> List[AccountValue]:
        return self.summary_rows if self.summary_rows is not None else self.account_rows

    def fail_account_reads(self, exc: BaseException) -> None:
        """Make BOTH account-value sources fail (the stream raises, the summary raises)."""
        self.fail_calls["accountValues"] = exc
        self.fail_calls["accountSummaryAsync"] = exc

    def set_account_value(self, tag: str, value: str, currency: str = "USD") -> None:
        self.account_rows = [r for r in self.account_rows if r.tag != tag]
        self.account_rows.append(AccountValue(self.account, tag, value, currency, ""))

    def positions(self, account: str = "") -> List[Position]:
        self._note("positions")
        if "positions" in self.fail_calls:
            raise self.fail_calls["positions"]
        return [p for p in self._positions if not account or p.account == account]

    def portfolio(self, account: str = "") -> List[PortfolioItem]:
        self._note("portfolio")
        return [p for p in self._portfolio if not account or p.account == account]

    def add_position(self, contract: Contract, position: float, avg_cost: float,
                     mark: Optional[float] = None, account: Optional[str] = None,
                     with_portfolio: bool = True, unrealized: Optional[float] = None,
                     multiplier: float = 1.0) -> None:
        account = account or self.account
        self._positions.append(Position(account, contract, position, avg_cost))
        if with_portfolio:
            price = mark if mark is not None else NAN
            value = price * position * multiplier if mark is not None else NAN
            pnl = unrealized if unrealized is not None else (
                (mark * multiplier - avg_cost) * position if mark is not None else NAN)
            self._portfolio.append(PortfolioItem(contract, position, price, value, avg_cost, pnl,
                                                 0.0, account))

    # ------------------------------------------------------------------ contracts / data
    def add_stock(self, symbol: str, con_id: Optional[int] = None, primary: str = "NASDAQ",
                  fractional: bool = False, rule: str = "26", min_size: Optional[float] = None,
                  size_increment: Optional[float] = None) -> Contract:
        con_id = con_id or self._con_id_for(symbol)
        contract = Stock(symbol, "SMART", "USD", primaryExchange=primary, conId=con_id,
                         localSymbol=symbol)
        details = ContractDetails(contract=contract, marketRuleIds=f"{rule},{rule}",
                                  minSize=(min_size if min_size is not None
                                           else (0.0001 if fractional else 1.0)),
                                  sizeIncrement=(size_increment if size_increment is not None
                                                 else (0.0001 if fractional else 1.0)))
        self.details.setdefault(symbol, []).append(details)
        return contract

    def add_option(self, underlying: str, expiry: str, strike: float, right: str,
                   con_id: Optional[int] = None, bid=NAN, ask=NAN, last=NAN, iv: Optional[float] = None,
                   delta: Optional[float] = None, gamma: Optional[float] = None,
                   theta: Optional[float] = None, vega: Optional[float] = None,
                   oi: Optional[float] = None, trading_class: Optional[str] = None,
                   multiplier: str = "100", rule: str = "26", market_data_type: int = 1) -> Contract:
        """Register an option contract (+ its streaming quote) the way IB would describe it."""
        root = trading_class or underlying.replace(" ", "")
        con_id = con_id or self._con_id_for(f"{underlying}{expiry}{strike}{right}")
        local = f"{root:<6}{expiry[2:]}{right}{int(round(strike * 1000)):08d}"
        contract = Option(symbol=underlying, lastTradeDateOrContractMonth=expiry, strike=strike,
                          right=right, exchange="SMART", currency="USD", multiplier=multiplier,
                          tradingClass=root, conId=con_id, localSymbol=local)
        self.details.setdefault(underlying, []).append(
            ContractDetails(contract=contract, marketRuleIds=f"{rule},{rule}", minSize=1.0,
                            sizeIncrement=1.0))
        quote: Dict[str, Any] = {"bid": bid, "ask": ask, "last": last, "marketDataType": market_data_type}
        if any(v is not None for v in (iv, delta, gamma, theta, vega)):
            quote["modelGreeks"] = OptionComputation(
                0, iv if iv is not None else NAN, delta if delta is not None else NAN, NAN, NAN,
                gamma if gamma is not None else NAN, vega if vega is not None else NAN,
                theta if theta is not None else NAN, NAN)
        if oi is not None:
            quote["callOpenInterest" if right == "C" else "putOpenInterest"] = oi
        self.option_quotes[con_id] = quote
        return contract

    def add_option_chain(self, underlying: str, expirations: List[str], strikes: List[float],
                         trading_class: Optional[str] = None, multiplier: str = "100",
                         exchange: str = "SMART", under_con_id: int = 1) -> None:
        self.option_chains.setdefault(underlying, []).append(OptionChain(
            exchange, under_con_id, trading_class or underlying.replace(" ", ""), multiplier,
            expirations, strikes))

    def add_leg_fills(self, ref: str, legs: List[Any]) -> None:
        """Per-leg executions of a combo: ``legs`` = [(leg_contract, side 'BOT'/'SLD', shares, price)]."""
        trade = self.trade_by_ref(ref)
        for contract, side, shares, price in legs:
            execution = Execution(execId=f"L{len(self.fills_list) + 1}", time=self.now(),
                                  acctNumber=self.account, side=side, shares=shares, price=price,
                                  permId=trade.orderStatus.permId, orderId=trade.order.orderId,
                                  orderRef=trade.order.orderRef)
            self.fills_list.append(Fill(contract, execution,
                                        CommissionReport(execution.execId, 0.5, "USD", 0.0, 0.0, 0),
                                        self.now()))

    def set_quote(self, contract: Contract, bid=NAN, ask=NAN, last=NAN, close=NAN,
                  market_data_type: int = 1) -> None:
        # Ticker.__post_init__ resets every price field to NaN, so constructor kwargs are lost:
        # set the attributes after construction.
        ticker = Ticker(contract=contract)
        ticker.bid, ticker.ask, ticker.last, ticker.close = bid, ask, last, close
        ticker.marketDataType = market_data_type
        self.quotes[contract.conId] = ticker

    async def reqContractDetailsAsync(self, contract: Contract) -> List[ContractDetails]:
        await self._maybe_fail("reqContractDetailsAsync")
        self.detail_calls += 1
        found = list(self.details.get(contract.symbol, []))
        if contract.secType == "OPT":
            # a blank right / zero strike is a WILDCARD, as at IBKR (one call returns the ladder)
            found = [d for d in found if d.contract.secType == "OPT"
                     and d.contract.lastTradeDateOrContractMonth == contract.lastTradeDateOrContractMonth
                     and (not contract.strike or abs(d.contract.strike - contract.strike) < 1e-9)
                     and (not contract.right or d.contract.right == contract.right)
                     and (not contract.tradingClass or d.contract.tradingClass == contract.tradingClass)]
        else:
            found = [d for d in found if d.contract.secType == contract.secType]
        if not found:
            self.errorEvent.emit(-1, 200, "No security definition has been found for the request",
                                 contract)
        return found

    async def qualifyContractsAsync(self, *contracts, returnAll=False):
        out = []
        for c in contracts:
            found = await self.reqContractDetailsAsync(c)
            out.append(found[0].contract if len(found) == 1 else None)
        return out

    async def reqMarketRuleAsync(self, marketRuleId: int) -> List[PriceIncrement]:
        await self._maybe_fail("reqMarketRuleAsync")
        return list(self.market_rules[int(marketRuleId)])

    async def reqTickersAsync(self, *contracts, regulatorySnapshot=False) -> List[Ticker]:
        await self._maybe_fail("reqTickersAsync")
        out = []
        for c in contracts:
            ticker = self.quotes.get(c.conId)
            out.append(copy.copy(ticker) if ticker is not None else Ticker(contract=c))
        return out

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False,
                   mktDataOptions=None) -> Ticker:
        self._note("reqMktData")
        self.active_lines += 1
        self.max_active_lines = max(self.max_active_lines, self.active_lines)
        base = self.quotes.get(contract.conId)
        ticker = copy.copy(base) if base is not None else Ticker(contract=contract)
        if contract.conId in self.shortable_shares:
            ticker.shortableShares = self.shortable_shares[contract.conId]
        opt = self.option_quotes.get(contract.conId)
        if opt:
            for key, value in opt.items():
                setattr(ticker, key, value)
        return ticker

    def cancelMktData(self, contract) -> bool:
        self.active_lines -= 1
        self.cancelled_lines += 1
        return True

    async def reqSecDefOptParamsAsync(self, underlyingSymbol, futFopExchange, underlyingSecType,
                                      underlyingConId):
        await self._maybe_fail("reqSecDefOptParamsAsync")
        return list(self.option_chains.get(underlyingSymbol, []))

    # ------------------------------------------------------------------ orders
    def trades(self) -> List[Trade]:
        return list(self._trades)

    def openTrades(self) -> List[Trade]:
        return [t for t in self._trades if t.orderStatus.status not in FINAL]

    async def reqAllOpenOrdersAsync(self) -> List[Trade]:
        await self._maybe_fail("reqAllOpenOrdersAsync")
        return self.openTrades()

    async def reqCompletedOrdersAsync(self, apiOnly: bool) -> List[Trade]:
        await self._maybe_fail("reqCompletedOrdersAsync")
        return list(self.prior_trades) + [t for t in self._trades if t.orderStatus.status in FINAL]

    async def reqExecutionsAsync(self, execFilter=None) -> List[Fill]:
        await self._maybe_fail("reqExecutionsAsync")
        return list(self.fills_list)

    async def whatIfOrderAsync(self, contract, order) -> OrderState:
        await self._maybe_fail("whatIfOrderAsync")
        return self.what_if

    def _log(self, trade: Trade, status: str, message: str = "", code: int = 0) -> None:
        trade.log.append(TradeLogEntry(self.now(), status, message, code))

    def _find(self, order_id: int) -> Optional[Trade]:
        for trade in self._trades:
            if trade.order.orderId == order_id:
                return trade
        return None

    def placeOrder(self, contract: Contract, order: Order) -> Trade:
        self._note("placeOrder")
        if self._find(order.orderId) is not None and order.orderId:
            trade = self._find(order.orderId)
            self.placed.append(self._snapshot(contract, order, modification=True))
            self._log(trade, trade.orderStatus.status, "modified")
            return trade
        order.orderId = self._next_order_id
        self._next_order_id += 1
        status = IBOrderStatus(orderId=order.orderId, status="PendingSubmit",
                               remaining=order.totalQuantity, clientId=self.client_id or 0)
        trade = Trade(contract, order, status, [], [])
        self._log(trade, "PendingSubmit")
        self._trades.append(trade)
        self.placed.append(self._snapshot(contract, order, modification=False))
        behavior = self.behaviors.pop(0) if self.behaviors else self.default_behavior
        trade._behavior = behavior  # type: ignore[attr-defined]
        if order.transmit:
            self._release(trade)
            for other in self._trades:
                if (other is not trade and other.orderStatus.status == "PendingSubmit"
                        and getattr(other, "_untransmitted", False)
                        and other.order.ocaGroup == order.ocaGroup and order.ocaGroup):
                    other._untransmitted = False  # type: ignore[attr-defined]
                    self._release(other)
        else:
            trade._untransmitted = True  # type: ignore[attr-defined]
        return trade

    @staticmethod
    def _snapshot(contract: Contract, order: Order, modification: bool) -> Dict[str, Any]:
        return {
            "modification": modification, "orderId": order.orderId, "sec_type": contract.secType,
            "symbol": contract.symbol, "con_id": contract.conId, "action": order.action,
            "orderType": order.orderType, "qty": order.totalQuantity, "tif": order.tif,
            "lmt": order.lmtPrice, "aux": order.auxPrice, "ref": order.orderRef,
            "oca": order.ocaGroup, "oca_type": order.ocaType, "transmit": order.transmit,
            "account": order.account, "outsideRth": order.outsideRth,
            "combo_legs": [(l.conId, l.ratio, l.action) for l in (contract.comboLegs or [])],
        }

    def _release(self, trade: Trade) -> None:
        loop = self.loop or asyncio.get_event_loop()
        loop.call_later(0.005, self._resolve, trade)

    def _resolve(self, trade: Trade) -> None:
        behavior = getattr(trade, "_behavior", "accept")
        order, status = trade.order, trade.orderStatus
        if behavior == "silent":
            return
        if isinstance(behavior, tuple) and behavior[0] == "reject":
            _, code, text = behavior
            self.errorEvent.emit(order.orderId, code, text, trade.contract)
            status.status = "Inactive"
            self._log(trade, "Inactive", text, code)
            return
        status.permId = self._next_perm
        self._next_perm += 1
        order.permId = status.permId
        status.status = "PreSubmitted" if order.orderType in ("STP", "STP LMT") else "Submitted"
        self._log(trade, status.status)
        if behavior == "fill" or (isinstance(behavior, tuple) and behavior[0] == "fill"):
            price = behavior[1] if isinstance(behavior, tuple) else (order.lmtPrice if order.lmtPrice
                                                                     < 1e300 else 100.0)
            self._fill(trade, order.totalQuantity, price)

    def _fill(self, trade: Trade, qty: float, price: float) -> None:
        status, order = trade.orderStatus, trade.order
        total_before = status.filled
        status.filled = min(order.totalQuantity, total_before + qty)
        status.remaining = order.totalQuantity - status.filled
        status.avgFillPrice = ((status.avgFillPrice * total_before + price * qty)
                               / status.filled) if status.filled else 0.0
        status.lastFillPrice = price
        status.status = "Filled" if status.remaining <= 1e-9 else "Submitted"
        self._log(trade, status.status, f"fill {qty}@{price}")
        execution = Execution(execId=f"E{len(self.fills_list) + 1}", time=self.now(),
                              acctNumber=self.account, side="BOT" if order.action == "BUY" else "SLD",
                              shares=qty, price=price, permId=status.permId, orderId=order.orderId,
                              orderRef=order.orderRef)
        fill = Fill(trade.contract, execution, CommissionReport(execution.execId, 1.0, "USD", 0.0,
                                                                0.0, 0), self.now())
        trade.fills.append(fill)
        self.fills_list.append(fill)
        if status.status == "Filled" and order.ocaGroup:
            for other in self._trades:
                if (other is not trade and other.order.ocaGroup == order.ocaGroup
                        and other.orderStatus.status not in FINAL):
                    other.orderStatus.status = "Cancelled"
                    self._log(other, "Cancelled", "OCA", 202)

    def cancelOrder(self, order: Order, manualCancelOrderTime: str = "") -> Optional[Trade]:
        self._note("cancelOrder")
        self.cancel_requests.append(order.orderId)
        trade = self._find(order.orderId)
        if trade is None:
            self.errorEvent.emit(order.orderId, 10147,
                                 f"OrderId {order.orderId} that needs to be cancelled is not found.",
                                 None)
            return None
        if trade.orderStatus.status in FINAL:
            self.errorEvent.emit(order.orderId, 10148,
                                 f"OrderId {order.orderId} that needs to be cancelled cannot be "
                                 f"cancelled, state: {trade.orderStatus.status}.", None)
            return trade
        if getattr(trade, "_untransmitted", False):
            trade.orderStatus.status = "Cancelled"
            self._log(trade, "Cancelled", "never transmitted")
            return trade
        trade.orderStatus.status = "PendingCancel"
        self._log(trade, "PendingCancel")
        if not self.hold_cancels:
            loop = self.loop or asyncio.get_event_loop()
            loop.call_later(0.005, self._finish_cancel, trade)
        return trade

    def _finish_cancel(self, trade: Trade) -> None:
        if trade.orderStatus.status == "PendingCancel":
            trade.orderStatus.status = "Cancelled"
            self._log(trade, "Cancelled", "Order Canceled - reason:", 202)
            self.errorEvent.emit(trade.order.orderId, 202, "Order Canceled - reason:", None)

    # ------------------------------------------------------------------ test-thread helpers
    def _on_loop(self, fn: Callable[[], Any]) -> Any:
        assert self.loop is not None, "the adapter has not connected yet"
        return asyncio.run_coroutine_threadsafe(self._async(fn), self.loop).result(5)

    def trade_by_ref(self, ref: str) -> Trade:
        for trade in self._trades:
            if trade.order.orderRef == ref:
                return trade
        raise KeyError(ref)

    def simulate_fill(self, ref: str, qty: Optional[float] = None, price: float = 100.0) -> None:
        def run() -> None:
            trade = self.trade_by_ref(ref)
            self._fill(trade, qty if qty is not None else trade.orderStatus.remaining, price)
        self._on_loop(run)

    def simulate_status(self, ref: str, status: str) -> None:
        def run() -> None:
            self.trade_by_ref(ref).orderStatus.status = status
        self._on_loop(run)

    def simulate_cancel_confirmed(self, ref: str) -> None:
        self._on_loop(lambda: self._finish_cancel(self.trade_by_ref(ref)))

    def add_prior_trade(self, contract: Contract, order: Order, status: str, filled: float = 0.0,
                        avg: float = 0.0, perm: int = 0) -> Trade:
        order.orderId = 0
        st = IBOrderStatus(orderId=0, status=status, filled=filled,
                           remaining=order.totalQuantity - filled, avgFillPrice=avg,
                           permId=perm or self._next_perm)
        order.permId = st.permId
        trade = Trade(contract, order, st, [], [TradeLogEntry(self.now(), status, "", 0)])
        self.prior_trades.append(trade)
        return trade

    def make_fill_record(self, contract: Contract, side: str, shares: float, price: float,
                         when: Optional[datetime] = None, order_ref: str = "", perm: int = 0,
                         order_id: int = 0) -> Fill:
        execution = Execution(execId=f"X{len(self.fills_list) + 1}", time=when or self.now(),
                              acctNumber=self.account, side=side, shares=float(shares), price=float(price),
                              permId=perm, orderId=order_id, orderRef=order_ref)
        fill = Fill(contract, execution, CommissionReport(execution.execId, 1.0, "USD", 0.0, 0.0, 0),
                    execution.time)
        self.fills_list.append(fill)
        return fill
