"""IBKR options: chains, quotes, positions, single-leg and BAG-combo orders.

A mixin for ``IBKRAccount`` (design doc s9). It uses ``self._runtime()`` / ``self._call`` and the
contract/rule helpers of ``IBKRAccount``; it adds no connection state of its own.

IBKR facts used (**UNVERIFIED** without a Gateway; ``tools/ibkr_paper_smoke.py`` prints each one so the
operator can confirm on a paper account):

* ``reqSecDefOptParams`` lists expirations/strikes/tradingClass per exchange; the standard class is the
  SMART entry with ``multiplier == "100"`` whose ``tradingClass`` is the underlying's own root.
* ``reqContractDetails`` on an ``Option`` with the strike and/or right left blank returns every
  matching contract in ONE call (the efficient way to get a strike ladder with conIds).
* ``localSymbol`` of an option is the OCC symbol with the root space-padded to six characters.
* ``Position.avgCost`` of an option is per CONTRACT (premium x multiplier);
  ``PortfolioItem.marketPrice`` is per share and ``marketValue`` the signed total.
* A combo is a ``BAG`` contract with ``ComboLeg``s; the order action is the combo's, the limit price
  signed (negative = credit), which is exactly the platform's ``+debit / -credit`` convention.
* Streaming ticks with generic tick 101 deliver open interest (``callOpenInterest`` /
  ``putOpenInterest``); ``modelGreeks`` carries IV and delta/gamma/theta/vega (IB publishes no rho).

Deliberately NOT done (design doc 7.3 / Q4 / Q5): ``OptionContract.volume`` stays ``None`` (IB streams
today's partial volume, not the prior completed session Alpaca reports), and there is no
``get_option_activities`` / ``reconcile_option_assignments`` (the API has no assignment feed): the
generic ``reconcile_externally_closed_option_transactions`` closes a transaction whose contract
vanished.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ib_async import ComboLeg, Contract, Option, Order

from ba2_common.core import ibkr_mapping as M
from ba2_common.core.option_types import OptionContract, OptionLeg, OptionPosition, OptionQuote

from ...core.db import get_instance, update_instance
from ...core.models import TradingOrder
from ...core.types import OptionRight, OrderDirection, OrderStatus
from ...logger import logger
from .ibkr_runtime import IBKRContractError, IBKROrderRejected, IBKRReadOnlyError


class IBKROptionsMixin:
    """The ``OptionsAccountInterface`` half of ``IBKRAccount``."""

    OPTION_GREEKS_SOURCE = "broker"

    #: Streaming market-data lines used at once (a basic account has 100; the rest serve prices).
    _QUOTE_BATCH = 90
    #: How long a batch waits for ticks before reading what arrived.
    _QUOTE_WAIT = 3.0
    #: Refuse (loudly) a chain request wider than this instead of silently truncating it.
    _MAX_CHAIN_CONTRACTS = 1500
    _CHAIN_TIMEOUT = 150.0
    #: Combos tick in cents.
    _COMBO_RULES = [(0.0, 0.01)]

    # ------------------------------------------------------------------ contracts (loop thread)
    async def _resolve_option(self, ib: Any, occ: str, underlying: Optional[str] = None):
        """Qualify one OCC symbol to exactly one standard-deliverable IB option contract."""
        from .IBKRAccount import _Resolved
        state = self._runtime().state
        key = "OPT:" + occ
        cached = state.contracts.get(key)
        if cached is not None and time.monotonic() - cached.fetched_at < self._CONTRACT_TTL:
            return cached
        root, expiry, right, strike = M.parse_occ(occ)
        if not M.is_standard_occ_root(root):
            raise IBKRContractError(
                f"{occ}: non-standard OCC root {root!r} (an adjusted contract, OPT-L7); it does not "
                f"deliver {M.STANDARD_OPTION_MULTIPLIER} ordinary shares and is refused")
        probe = Option(symbol=M.to_ib_symbol(underlying) if underlying else root,
                       lastTradeDateOrContractMonth=M.ib_expiry_string(expiry), strike=strike,
                       right=M.ib_right(right), exchange="SMART", currency="USD",
                       multiplier=str(M.STANDARD_OPTION_MULTIPLIER), tradingClass=root)
        details = await asyncio.wait_for(ib.reqContractDetailsAsync(probe), self._READ_TIMEOUT)
        options = [d for d in (details or []) if d.contract.secType == "OPT"]
        if len(options) != 1:
            raise IBKRContractError(
                f"IBKR resolved {occ} to {len(options)} contracts (need exactly one)")
        detail = options[0]
        multiplier = M.ib_number(detail.contract.multiplier)
        if multiplier is None or int(multiplier) != M.STANDARD_OPTION_MULTIPLIER:
            raise IBKRContractError(
                f"{occ} delivers {detail.contract.multiplier!r} shares per contract, not "
                f"{M.STANDARD_OPTION_MULTIPLIER}; refused (OPT-L7)")
        resolved = _Resolved(contract=detail.contract, details=detail, fetched_at=time.monotonic())
        state.contracts[key] = resolved
        return resolved

    @staticmethod
    def _occ_of(contract: Any) -> str:
        return M.occ_from_contract_fields(
            contract.localSymbol, contract.tradingClass, contract.symbol,
            contract.lastTradeDateOrContractMonth, contract.right, float(contract.strike))

    @staticmethod
    def _pick_chain(params: List[Any], underlying: str) -> Any:
        """The standard option chain: ``multiplier == 100`` with the underlying's own trading class,
        SMART exchange first."""
        root = M.to_ib_symbol(underlying).replace(" ", "")
        standard = [c for c in params or []
                    if str(c.multiplier) == str(M.STANDARD_OPTION_MULTIPLIER)
                    and str(c.tradingClass).upper() == root]
        if not standard:
            raise IBKRContractError(
                f"IBKR lists no standard (100-share, class {root}) option chain for {underlying}")
        standard.sort(key=lambda c: 0 if c.exchange == "SMART" else 1)
        return standard[0]

    # ------------------------------------------------------------------ streaming quotes (loop thread)
    @staticmethod
    def _has_data(ticker: Any) -> bool:
        return any(M.ib_number(getattr(ticker, f)) is not None for f in ("bid", "ask", "last")) \
            or getattr(ticker, "modelGreeks", None) is not None

    @staticmethod
    def _quote_values(contract: Any, ticker: Any) -> Dict[str, Any]:
        """Plain numbers copied off a ticker. ``None`` where IB published nothing; a negative bid/ask
        is IB's "no quote" and is dropped, a ZERO bid is a real (empty) bid and is kept."""
        def price(value):
            number = M.ib_number(value)
            return None if number is None or number < 0 else number

        greeks = getattr(ticker, "modelGreeks", None)
        is_call = str(contract.right).upper().startswith("C")
        oi = M.ib_number(getattr(ticker, "callOpenInterest" if is_call else "putOpenInterest", None))
        return {
            "bid": price(ticker.bid), "ask": price(ticker.ask), "last": price(ticker.last),
            "iv": M.ib_number(getattr(greeks, "impliedVol", None)) if greeks else None,
            "delta": M.ib_number(getattr(greeks, "delta", None)) if greeks else None,
            "gamma": M.ib_number(getattr(greeks, "gamma", None)) if greeks else None,
            "theta": M.ib_number(getattr(greeks, "theta", None)) if greeks else None,
            "vega": M.ib_number(getattr(greeks, "vega", None)) if greeks else None,
            "open_interest": int(oi) if oi is not None else None,
            "time": getattr(ticker, "time", None),
            "delayed": M.delayed_market_data(getattr(ticker, "marketDataType", None)),
        }

    async def _stream_quotes(self, ib: Any, contracts: List[Any]) -> Dict[int, Dict[str, Any]]:
        """Quote a list of contracts in batches that respect the market-data line budget."""
        out: Dict[int, Dict[str, Any]] = {}
        for start in range(0, len(contracts), self._QUOTE_BATCH):
            batch = contracts[start:start + self._QUOTE_BATCH]
            tickers = [(c, ib.reqMktData(c, "101", False, False)) for c in batch]
            try:
                deadline = time.monotonic() + self._QUOTE_WAIT
                while time.monotonic() < deadline and not all(self._has_data(t) for _, t in tickers):
                    await asyncio.sleep(0.05)
            finally:
                for contract, _ in tickers:
                    ib.cancelMktData(contract)
            for contract, ticker in tickers:
                out[int(contract.conId)] = self._quote_values(contract, ticker)
        return out

    # ------------------------------------------------------------------ chain
    async def _option_chain_payload(self, ib: Any, underlying: str, expiry_min: date,
                                    expiry_max: date, option_type: Optional[OptionRight],
                                    strike_min: Optional[float], strike_max: Optional[float]):
        stock = await self._resolve_stock(ib, underlying)
        params = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync(stock.contract.symbol, "", "STK", stock.contract.conId),
            self._READ_TIMEOUT)
        chain = self._pick_chain(params, underlying)
        expiries = []
        for text in chain.expirations or []:
            try:
                day = datetime.strptime(text, "%Y%m%d").date()
            except ValueError:
                continue
            if expiry_min <= day <= expiry_max:
                expiries.append(day)
        right = "" if option_type is None else M.ib_right(option_type)
        contracts: List[Any] = []
        for day in sorted(expiries):
            probe = Option(symbol=stock.contract.symbol,
                           lastTradeDateOrContractMonth=M.ib_expiry_string(day), right=right,
                           exchange="SMART", currency="USD",
                           multiplier=str(M.STANDARD_OPTION_MULTIPLIER),
                           tradingClass=chain.tradingClass)
            details = await asyncio.wait_for(ib.reqContractDetailsAsync(probe), self._READ_TIMEOUT)
            for d in details or []:
                c = d.contract
                if c.secType != "OPT" or str(c.multiplier) != str(M.STANDARD_OPTION_MULTIPLIER):
                    continue
                if strike_min is not None and c.strike < strike_min:
                    continue
                if strike_max is not None and c.strike > strike_max:
                    continue
                contracts.append(c)
        if len(contracts) > self._MAX_CHAIN_CONTRACTS:
            raise IBKRContractError(
                f"{underlying} chain request spans {len(contracts)} contracts (limit "
                f"{self._MAX_CHAIN_CONTRACTS}); narrow the expiry or strike window rather than "
                f"receive a silently truncated chain")
        quotes = await self._stream_quotes(ib, contracts)
        return contracts, quotes

    def get_option_chain(self, underlying: str, expiry_min: date, expiry_max: date,
                         option_type: Optional[OptionRight] = None, strike_min: Optional[float] = None,
                         strike_max: Optional[float] = None) -> List[OptionContract]:
        """Chain rows (quote + greeks + open interest) for the window. A contract IB could not
        qualify is absent; a contract with no two-sided quote keeps ``None`` bid/ask (never 0).
        Raises on failure (callers log and treat the chain as UNKNOWN, as with Alpaca)."""
        contracts, quotes = self._call(
            lambda ib: self._option_chain_payload(ib, underlying, expiry_min, expiry_max,
                                                  option_type, strike_min, strike_max),
            op=f"option chain {underlying}", timeout=self._CHAIN_TIMEOUT)
        rows: List[OptionContract] = []
        delayed = 0
        for contract in contracts:
            q = quotes.get(int(contract.conId))
            if q is None:
                continue
            if q["delayed"]:
                delayed += 1
                continue
            rows.append(OptionContract(
                symbol=self._occ_of(contract), underlying=M.from_ib_symbol(contract.symbol),
                option_type=(OptionRight.CALL if str(contract.right).upper().startswith("C")
                             else OptionRight.PUT),
                strike=float(contract.strike),
                expiry=datetime.strptime(contract.lastTradeDateOrContractMonth, "%Y%m%d").date(),
                bid=q["bid"], ask=q["ask"], last=q["last"], implied_volatility=q["iv"],
                delta=q["delta"], gamma=q["gamma"], theta=q["theta"], vega=q["vega"],
                open_interest=q["open_interest"], volume=None, rho=None, quote_time=q["time"],
                greeks_source=self.OPTION_GREEKS_SOURCE))
        if delayed:
            logger.error(f"[Account {self.id}] {underlying} option chain: {delayed} contract(s) were "
                         f"served DELAYED data and are excluded (delayed data is never a price)")
        return rows

    async def _option_quote_payload(self, ib: Any, occ: str, underlying: Optional[str]):
        resolved = await self._resolve_option(ib, occ, underlying)
        quotes = await self._stream_quotes(ib, [resolved.contract])
        return quotes[int(resolved.contract.conId)]

    def get_option_quote(self, contract_symbol: str) -> Optional[OptionQuote]:
        """Latest quote + greeks for one OCC contract, or ``None`` when IB published none (or the
        data is delayed). Raises when the contract cannot be qualified."""
        q = self._call(lambda ib: self._option_quote_payload(ib, contract_symbol, None),
                       op=f"option quote {contract_symbol}", timeout=self._READ_TIMEOUT * 2)
        if q["delayed"]:
            logger.error(f"[Account {self.id}] {contract_symbol} quote is DELAYED data; refused")
            return None
        if all(q[k] is None for k in ("bid", "ask", "last", "iv")):
            return None
        return OptionQuote(symbol=contract_symbol, bid=q["bid"], ask=q["ask"], last=q["last"],
                           implied_volatility=q["iv"], delta=q["delta"], gamma=q["gamma"],
                           theta=q["theta"], vega=q["vega"], timestamp=q["time"], rho=None,
                           volume=None)

    def get_atm_implied_volatility(self, underlying: str) -> Optional[float]:
        """Near-ATM IV (0-1): the contract nearest spot in the 20-45 DTE window (the Alpaca rule)."""
        spot = self.get_instrument_current_price(underlying, "mid")
        if spot is None:
            logger.warning(f"get_atm_implied_volatility: no spot price for {underlying}")
            return None
        today = datetime.now(timezone.utc).date()
        chain = self.get_option_chain(underlying, today + timedelta(days=20),
                                      today + timedelta(days=45),
                                      strike_min=spot * 0.9, strike_max=spot * 1.1) or []
        candidates = [c for c in chain if c.strike is not None and c.implied_volatility is not None]
        if not candidates:
            logger.warning(f"get_atm_implied_volatility: empty/IV-less chain for {underlying}")
            return None
        return min(candidates, key=lambda c: abs(c.strike - spot)).implied_volatility

    # ------------------------------------------------------------------ positions
    async def _option_positions_payload(self, ib: Any) -> List[Dict[str, Any]]:
        account = self._account_id
        held = [p for p in ib.positions(account)
                if p.account == account and p.contract.secType == "OPT" and p.position]
        portfolio = {int(i.contract.conId): i for i in ib.portfolio(account)}
        out = []
        for pos in held:
            item = portfolio.get(int(pos.contract.conId))
            multiplier = M.ib_number(pos.contract.multiplier) or float(M.STANDARD_OPTION_MULTIPLIER)
            out.append({
                "occ": self._occ_of(pos.contract), "underlying": M.from_ib_symbol(pos.contract.symbol),
                "right": str(pos.contract.right), "strike": float(pos.contract.strike),
                "expiry": pos.contract.lastTradeDateOrContractMonth, "qty": float(pos.position),
                "avg_cost": float(pos.avgCost), "multiplier": int(multiplier),
                "mark": M.ib_number(getattr(item, "marketPrice", None)) if item else None,
                "market_value": M.ib_number(getattr(item, "marketValue", None)) if item else None,
                "unrealized": M.ib_number(getattr(item, "unrealizedPNL", None)) if item else None,
            })
        return out

    def get_option_positions(self) -> Optional[List[OptionPosition]]:
        """Held option positions. TRI-STATE like ``get_positions``: ``[]`` is a CONFIRMED empty
        book, ``None`` means the fetch FAILED. ``avg_entry_price`` is the per-share premium
        (IB's per-contract ``avgCost`` / multiplier); a malformed row is skipped with a warning."""
        try:
            rows = self._call(self._option_positions_payload, op="option positions")
        except Exception as e:  # noqa: BLE001 -- a failed fetch is UNKNOWN, never "flat"
            logger.error(f"[Account {self.id}] get_option_positions: could not fetch positions "
                         f"({e}). Reporting None (UNKNOWN) -- callers must not read this as an "
                         f"empty option book.", exc_info=True)
            return None
        positions: List[OptionPosition] = []
        for row in rows:
            try:
                multiplier = row["multiplier"]
                positions.append(OptionPosition(
                    contract_symbol=row["occ"], underlying=row["underlying"],
                    option_type=(OptionRight.CALL if row["right"].upper().startswith("C")
                                 else OptionRight.PUT),
                    strike=row["strike"],
                    expiry=datetime.strptime(row["expiry"], "%Y%m%d").date(),
                    side=OrderDirection.BUY if row["qty"] > 0 else OrderDirection.SELL,
                    quantity=abs(row["qty"]), avg_entry_price=row["avg_cost"] / multiplier,
                    current_price=row["mark"], market_value=row["market_value"],
                    unrealized_pl=row["unrealized"], multiplier=multiplier))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Skipping option position {row.get('occ', '?')}: {e}")
        return positions

    # ------------------------------------------------------------------ orders
    @staticmethod
    def _signed_round(price: float, rules: List[Tuple[float, float]]) -> float:
        magnitude = M.round_to_increment(abs(price), rules)
        return -magnitude if price < 0 else magnitude

    async def _place_option(self, ib: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
        legs = spec["legs"]
        resolved = [await self._resolve_option(ib, leg["occ"], leg["underlying"]) for leg in legs]
        if len(legs) == 1:
            contract = resolved[0].contract
            rules = await self._market_rules(ib, resolved[0].details)
            action = legs[0]["action"]
        else:
            contract = Contract(
                secType="BAG", symbol=M.to_ib_symbol(spec["underlying"]), exchange="SMART",
                currency="USD",
                comboLegs=[ComboLeg(conId=int(r.contract.conId), ratio=int(leg["ratio"]),
                                    action=leg["action"], exchange="SMART")
                           for r, leg in zip(resolved, legs)])
            rules = self._COMBO_RULES
            action = "BUY"          # the combo's own action; the legs carry their sides, the price its sign
        order = Order()
        order.action, order.totalQuantity = action, float(spec["quantity"])
        order.tif, order.orderRef, order.account = "DAY", spec["order_ref"], self._account_id
        order.outsideRth, order.transmit = False, True
        if spec["ib_type"] == "MKT":
            order.orderType = "MKT"
        else:
            order.orderType = "LMT"
            order.lmtPrice = self._signed_round(spec["limit"], rules)
        trade = ib.placeOrder(contract, order)
        view = await self._wait_ack(ib, trade, self._ORDER_ACK_TIMEOUT)
        return {"view": view, "limit": order.lmtPrice if spec["ib_type"] == "LMT" else None}

    def _submit_option_order_impl(self, trading_order: TradingOrder, legs: List[OptionLeg],
                                  leg_orders: Optional[List[Any]] = None) -> TradingOrder:
        """Send one option order (a single contract, or a BAG combo for 2-4 legs) and write the
        broker's answer back onto the persisted parent and its leg children.

        Errors propagate: ``submit_option_order`` unwinds the rows (ERROR only if nothing reached
        the broker). The broker id is persisted before anything else can raise.
        """
        from ...core.types import OrderType as CoreOrderType
        if self._runtime().read_only:
            raise IBKRReadOnlyError(
                f"[Account {self.id}] this IBKR account is configured read-only; cannot submit "
                f"option orders")
        quantity = float(trading_order.quantity)
        if quantity != int(quantity) or quantity <= 0:
            raise ValueError(f"option order quantity must be a positive whole number of contracts, "
                             f"got {trading_order.quantity!r}")
        is_market = trading_order.order_type == CoreOrderType.MARKET
        if not is_market and trading_order.limit_price is None:
            raise ValueError("a limit option order needs a limit price")
        if len(legs) == 1 and not is_market and float(trading_order.limit_price) <= 0:
            raise ValueError(f"a single-leg option limit must be a positive premium, got "
                             f"{trading_order.limit_price!r}")
        spec = {
            "legs": [{"occ": leg.contract_symbol,
                      "underlying": leg.underlying,
                      "action": "BUY" if leg.side == OrderDirection.BUY else "SELL",
                      "ratio": leg.ratio_qty} for leg in legs],
            "underlying": legs[0].underlying or trading_order.underlying_symbol or trading_order.symbol,
            "quantity": int(quantity), "ib_type": "MKT" if is_market else "LMT",
            "limit": None if is_market else float(trading_order.limit_price),
            "order_ref": M.make_order_ref(self.id, trading_order.id),
        }
        placed = self._call(lambda ib: self._place_option(ib, spec),
                            op=f"place option order {trading_order.id}",
                            timeout=self._ORDER_ACK_TIMEOUT + self._READ_TIMEOUT * (1 + len(legs)))
        view = placed["view"]
        # The broker id FIRST: from here on the contracts may exist at IBKR whatever else fails.
        trading_order.broker_order_id = view.broker_order_id
        update_instance(trading_order)
        status = M.map_ib_status(view.status, view.filled, view.remaining)
        trading_order.status = status
        if view.filled:
            trading_order.filled_qty = view.filled
        if view.avg_fill_price and view.avg_fill_price > 0:
            trading_order.open_price = view.avg_fill_price
        update_instance(trading_order)
        for child in (leg_orders or []):
            child.status = status
            update_instance(child)
        logger.info(f"Submitted IBKR option order {trading_order.id}: broker_order_id="
                    f"{trading_order.broker_order_id}, legs={len(legs)}, status={status}")
        return trading_order

    def close_option_position(self, position: OptionPosition, order_type: str = "limit",
                              limit_price: Optional[float] = None,
                              transaction_id: Optional[int] = None):
        """Submit a closing order that RIDES the open position's transaction (broker-neutral; see
        ``AlpacaAccount.close_option_position`` for why the transaction link matters)."""
        close_side = OrderDirection.SELL if position.side == OrderDirection.BUY else OrderDirection.BUY
        intent = "sell_to_close" if position.side == OrderDirection.BUY else "buy_to_close"
        leg = OptionLeg(contract_symbol=position.contract_symbol, side=close_side,
                        position_intent=intent, option_type=position.option_type,
                        strike=position.strike, expiry=position.expiry,
                        underlying=position.underlying)
        if transaction_id is None:
            transaction_id = self.open_option_transaction_id_for_contract(position.contract_symbol)
        if transaction_id is None:
            logger.error(
                f"Closing {position.contract_symbol} but no OPEN transaction holding it could be "
                f"found -- the close will be booked on a NEW transaction and the original position "
                f"(if any) will not reach CLOSED. Submitting anyway: flattening the position at the "
                f"broker takes priority over the ledger link.")
        return self.submit_option_order([leg], int(position.quantity), order_type, limit_price,
                                        option_strategy="close", transaction_id=transaction_id)

    # ------------------------------------------------------------------ refresh hook
    def _reconcile_option_children(self, parent: TradingOrder, view, book) -> int:
        """Mirror a combo parent's state onto its leg children and give each leg its own fill.

        The children are not separate IB orders (the BAG is one), so they follow the parent's
        status; per-leg fill quantity is the parent's fill x the leg's ratio, and a leg's price is
        set only when IB reported an execution for that contract (UNVERIFIED shape): a leg with no
        execution keeps a NULL price -- unknown is never zero.
        """
        from ba2_common.core.trade_store import orders_where
        status = M.map_ib_status(view.status, view.filled, view.remaining)
        changed = 0
        for child in orders_where(parent_order_id=parent.id):
            row = get_instance(TradingOrder, child.id)
            has_changes = False
            if row.status != status:
                row.status, has_changes = status, True
            if view.total_quantity:
                leg_filled = float(row.quantity) * view.filled / view.total_quantity
                if row.filled_qty is None or abs(float(row.filled_qty) - leg_filled) > 1e-9:
                    row.filled_qty, has_changes = leg_filled, True
            ex = book.executions_by_perm_occ.get((view.perm_id, row.contract_symbol))
            if ex and ex.get("price") and row.open_price != ex["price"]:
                row.open_price, has_changes = ex["price"], True
            if has_changes:
                update_instance(row)
                changed += 1
        return changed
