"""Operator smoke test for the IBKR integration, against YOUR IB Gateway / TWS PAPER account.

READ-ONLY BY DEFAULT. The API session is ALWAYS opened with ``readonly=True`` first (IB itself then
refuses any order); only ``--place-test-order`` reconnects writable, and only after the account is
confirmed to be a paper account. Without that flag the script only reads: accounts, summary, positions, open
orders, contract resolution, prices, shortability, an option chain and a what-if margin preview.

It exists because the IBKR adapter (ba2_trade_platform/modules/accounts/IBKRAccount.py) was written
with NO live connection. Every fact it rests on that is not in the ib_async source is listed in
docs/plans/2026-10-03-ibkr-support-design.md as UNVERIFIED, and this script prints the observed value of
each so you can confirm it (or send the output back so the adapter can be corrected). Look for the
``[CHECK]`` lines and the final summary.

Usage (from the repo root, with the platform venv):

    python tools/ibkr_paper_smoke.py                       # read-only, defaults: 127.0.0.1:4002 clientId 98
    python tools/ibkr_paper_smoke.py --symbol SCHD --option
    python tools/ibkr_paper_smoke.py --dump-executions     # raw executions (shape of assignments/fills)
    python tools/ibkr_paper_smoke.py --flex-token T --flex-query Q   # fetch + parse your Flex statement
    python tools/ibkr_paper_smoke.py --place-test-order    # PAPER ONLY: 1-share far-from-market limit,
                                                           # placed then cancelled

``--place-test-order`` REFUSES (exit 2, nothing placed) unless the connected account id starts with
``DU`` (IBKR paper accounts). Use a client id that no running platform instance uses.

Exit codes: 0 ok, 1 an unexpected failure, 2 a safety refusal.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

# Run from anywhere: the repo root and the shared packages must be importable.
_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "packages" / "common"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ba2_common.core import ibkr_mapping as M  # noqa: E402

DEFAULT_CLIENT_ID = 98
TEST_ORDER_REF = "ba2-smoke-test"


class Report:
    """Collects output lines (printed as they happen) and the facts to confirm."""

    def __init__(self, out: Callable[[str], None] = print) -> None:
        self.out = out
        self.facts: List[str] = []

    def line(self, text: str = "") -> None:
        self.out(text)

    def section(self, title: str) -> None:
        self.out("")
        self.out(f"=== {title} ===")

    def ok(self, text: str) -> None:
        self.out(f"[ OK ] {text}")

    def info(self, text: str) -> None:
        self.out(f"[INFO] {text}")

    def warn(self, text: str) -> None:
        self.out(f"[WARN] {text}")

    def check(self, fact: str, observed: Any) -> None:
        """An UNVERIFIED design assumption and what this run observed for it."""
        self.out(f"[CHECK] {fact}: {observed}")
        self.facts.append(f"{fact}: {observed}")


class Refusal(Exception):
    """A safety refusal (exit code 2)."""


def _num(value: Any) -> Optional[float]:
    return M.ib_number(value)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002, help="Gateway paper 4002, TWS paper 7497 (default 4002)")
    ap.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    ap.add_argument("--account", default=None, help="account id (required if the login manages several)")
    ap.add_argument("--symbol", default="AAPL", help="stock used for the contract/price/short checks")
    ap.add_argument("--option", action="store_true", help="also resolve and quote a near-ATM option")
    ap.add_argument("--dump-executions", action="store_true", help="print every execution IB reports")
    ap.add_argument("--flex-token", default=None)
    ap.add_argument("--flex-query", default=None)
    ap.add_argument("--place-test-order", action="store_true",
                    help="PAPER ONLY: place then cancel a 1-share far-from-market limit order")
    ap.add_argument("--timeout", type=float, default=15.0)
    return ap.parse_args(argv)


# ----------------------------------------------------------------------------- sections
def adapter_connect_kwargs(args: argparse.Namespace, *, readonly: bool, account: str) -> dict:
    """The arguments the ADAPTER connects with (``ibkr_runtime.IBKRRuntime._connect``): only the
    account-updates feed is fetched at startup and a slow optional sync does not fail the connect. The
    smoke test must connect the same way, or it measures a different session."""
    from ib_async import StartupFetch
    return dict(clientId=args.client_id, timeout=args.timeout, readonly=readonly, account=account,
                raiseSyncErrors=False, fetchFields=StartupFetch.ACCOUNT_UPDATES)


async def connect(ib: Any, args: argparse.Namespace, rep: Report) -> str:
    """Connect READ-ONLY first, always. Only after the account is confirmed to be a paper account is a
    writable session opened (and only for ``--place-test-order``), so a live account never sees one."""
    rep.section("Connection")
    kind = M.IB_PORTS.get(args.port, "unknown port")
    await asyncio.wait_for(
        ib.connectAsync(args.host, args.port,
                        **adapter_connect_kwargs(args, readonly=True, account=args.account or "")),
        args.timeout + 5)
    accounts = list(ib.managedAccounts())
    rep.ok(f"connected to {args.host}:{args.port} ({kind}), clientId={args.client_id}, readonly=True")
    rep.info(f"managed accounts: {accounts}")
    if args.account:
        if args.account not in accounts:
            raise Refusal(f"account {args.account!r} is not managed by this login ({accounts})")
        account = args.account
    elif len(accounts) == 1:
        account = accounts[0]
    else:
        raise Refusal(f"this login manages several accounts {accounts}; pass --account")
    paper = account.startswith(M.PAPER_ACCOUNT_PREFIX)
    rep.check("paper accounts start with 'DU'", f"{account} -> {'paper' if paper else 'LIVE'}")
    if args.place_test_order:
        if not paper:
            raise Refusal(f"--place-test-order refused: {account} is not a paper account "
                          f"(paper ids start with {M.PAPER_ACCOUNT_PREFIX!r}); nothing was placed")
        ib.disconnect()
        await asyncio.wait_for(
            ib.connectAsync(args.host, args.port,
                            **adapter_connect_kwargs(args, readonly=False, account=account)),
            args.timeout + 5)
        rep.ok("reconnected WRITABLE (paper account confirmed) for the test order")
    ib.reqMarketDataType(2)
    try:
        first_id = ib.client.getReqId()
        second_id = ib.client.getReqId()
        rep_check_ids = f"two consecutive getReqId() calls: {first_id}, {second_id}"
    except Exception as e:  # noqa: BLE001 -- informational
        rep_check_ids = f"not readable ({type(e).__name__}: {e})"
    rep.check("request/order ids after (re)connect: ib_async's counter vs TWS nextValidId (the adapter "
              "clears its error history on every connect because ids can repeat across sessions)",
              rep_check_ids)
    return account


async def account_section(ib: Any, account: str, rep: Report) -> None:
    rep.section("Account values (what feeds get_account_info / get_account_snapshot)")
    stream_rows = list(ib.accountValues(account))
    summary_rows = await ib.accountSummaryAsync(account)
    stream, _ = M.select_account_values(stream_rows, account) if stream_rows else ({}, {})
    summary, texts = M.select_account_values(summary_rows, account)
    primary = stream if "NetLiquidation" in stream else summary
    rep.info(f"primary source: {'account-updates stream' if primary is stream else 'account summary (stream empty)'}")
    for tag in ("NetLiquidation", "TotalCashValue", "SettledCash", "AvailableFunds", "BuyingPower",
                "ExcessLiquidity", "InitMarginReq", "MaintMarginReq", "EquityWithLoanValue",
                "GrossPositionValue", "Cushion"):
        rep.line(f"    {tag:22s} stream={stream.get(tag, 'n/a')!s:>14}  summary={summary.get(tag, 'n/a')!s:>14}")
    rep.line(f"    {'AccountType':22s} {texts.get('AccountType', 'n/a')}")
    rep.check("the account-updates stream publishes the tags the adapter needs (NetLiquidation, "
              "AvailableFunds, BuyingPower, TotalCashValue)",
              {t: (t in stream) for t in ("NetLiquidation", "AvailableFunds", "BuyingPower", "TotalCashValue")})
    funds, power = primary.get("AvailableFunds"), primary.get("BuyingPower")
    ratio = (power / funds) if funds and power is not None else None
    multiplier = M.margin_multiplier_from(primary)
    rep.check("BuyingPower / AvailableFunds (margin account ~4, cash account ~1)",
              f"{ratio:.2f}" if ratio is not None else "n/a")
    rep.check(f"adapter's Reg-T multiplier (2.0 iff ratio >= {M.MARGIN_DETECTION_RATIO})", multiplier)
    snap = M.snapshot_from_account_values(primary, texts, None, None)
    rep.info(f"adapter would publish: buying_power={snap.buying_power}, equity={snap.equity}, "
             f"cash={snap.cash}, option_buying_power={snap.option_buying_power}")
    comps = M.buying_power_components(primary, multiplier)
    rep.check("Reg-T room: AvailableFunds x 2 vs SMA x 2 vs ExcessLiquidity x 2 (the adapter publishes the "
              "MINIMUM; if SMA x 2 is the smaller one, AvailableFunds x 2 would have overstated the "
              "overnight room)",
              f"{comps} -> published {snap.buying_power}; SMA tag present={'SMA' in primary}")


async def book_section(ib: Any, account: str, rep: Report) -> None:
    rep.section("Positions, portfolio and open orders")
    positions = [p for p in ib.positions(account)]
    stk = [p for p in positions if p.contract.secType == "STK"]
    opt = [p for p in positions if p.contract.secType == "OPT"]
    rep.info(f"{len(positions)} positions ({len(stk)} stock, {len(opt)} option)")
    portfolio = {int(i.contract.conId): i for i in ib.portfolio(account)}
    for p in positions[:10]:
        item = portfolio.get(int(p.contract.conId))
        rep.line(f"    {p.contract.secType} {p.contract.localSymbol or p.contract.symbol:22s} "
                 f"qty={p.position} avgCost={p.avgCost} "
                 f"mark={_num(getattr(item, 'marketPrice', None))} "
                 f"mv={_num(getattr(item, 'marketValue', None))}")
    for p in opt[:3]:
        rep.check("option avgCost is per CONTRACT (premium x multiplier); adapter divides by multiplier",
                  f"{p.contract.localSymbol}: avgCost={p.avgCost}, multiplier={p.contract.multiplier}")
    completed = await ib.reqCompletedOrdersAsync(False)
    times = [t.log[0].time for t in completed if t.log]
    rep.check("how long completed orders survive (run again AFTER the Gateway's nightly restart and compare)",
              f"{len(completed)} completed orders; oldest log time {min(times) if times else None}")
    rep.check("a COMPLETED order's orderStatus.filled vs order.filledQuantity, and the status strings "
              "(ib_async leaves the status record empty; the adapter reads order.filledQuantity and takes the "
              "price from the executions)",
              [f"{t.orderStatus.status}: status.filled={t.orderStatus.filled} "
               f"avgFillPrice={t.orderStatus.avgFillPrice} order.filledQuantity={t.order.filledQuantity} "
               f"totalQuantity={t.order.totalQuantity}" for t in completed[:6]] or "no completed orders")
    trades = await ib.reqAllOpenOrdersAsync()
    rep.check("an orderStatus is delivered PER ORDER on reqAllOpenOrders (non-empty status and permId on "
              "every open order)",
              f"{sum(1 for t in trades if not t.orderStatus.status)} of {len(trades)} open orders have an EMPTY "
              f"status; " + "; ".join(f"id={t.order.orderId} perm={t.orderStatus.permId} "
                                      f"status={t.orderStatus.status!r}" for t in trades[:6]))
    mine_ids = {t.order.orderId for t in trades if int(t.orderStatus.clientId or 0) == args.client_id}
    foreign = [(t.order.orderId, int(t.orderStatus.clientId or 0)) for t in trades
               if int(t.orderStatus.clientId or 0) not in (0, args.client_id)]
    rep.check("OTHER API clients' orders: their orderIds are only unique PER client; the adapter matches an "
              "order by permId, else (clientId, orderId). Does any foreign order carry the same orderId as one "
              "of ours?",
              f"foreign (orderId, clientId)={foreign[:8]}; same orderId as one of ours="
              f"{sorted({oid for oid, _ in foreign} & mine_ids)}")
    fills = await ib.reqExecutionsAsync()
    rep.check("executions carry the orderRef (the adapter settles a row from an execution under its ref)",
              f"{sum(1 for f in fills if f.execution.orderRef)} of {len(fills)} executions have an orderRef")
    rep.info(f"{len(trades)} open orders (all clients)")
    for t in trades[:10]:
        rep.line(f"    id={t.order.orderId} perm={t.orderStatus.permId} ref={t.order.orderRef!r} "
                 f"{t.order.action} {t.order.totalQuantity} {t.contract.symbol} "
                 f"{t.order.orderType} {t.orderStatus.status}")


async def reconnect_positions_check(ib: Any, args: argparse.Namespace, account: str,
                                    rep: Report) -> None:
    """Positions right after a reconnect: the cache read straight away vs after an explicit request.
    The adapter never trusts the cache alone (an empty cache after a timed-out sync reads as flat)."""
    rep.section("Positions right after a reconnect")
    ib.disconnect()
    await asyncio.wait_for(
        ib.connectAsync(args.host, args.port,
                        **adapter_connect_kwargs(args, readonly=not args.place_test_order, account=account)),
        args.timeout + 5)
    immediately = len(ib.positions(account))
    confirmed = len(await asyncio.wait_for(ib.reqPositionsAsync(), args.timeout))
    rep.check("positions cache immediately after the adapter-style connect (raiseSyncErrors=False, "
              "account updates only) vs after reqPositions",
              f"{immediately} vs {confirmed} (they must agree; the adapter always awaits reqPositions)")
    ib.reqMarketDataType(2)


async def equity_section(ib: Any, symbol: str, rep: Report, args: argparse.Namespace,
                         account: str) -> Any:
    from ib_async import Stock
    rep.section(f"Equity contract, price, shortability, margin preview: {symbol}")
    details = await ib.reqContractDetailsAsync(Stock(M.to_ib_symbol(symbol), "SMART", "USD"))
    stocks = [d for d in details if d.contract.secType == "STK" and d.contract.currency == "USD"]
    rep.info(f"{len(details)} contract(s) returned, {len(stocks)} US-dollar stock(s): "
             f"{[(d.contract.conId, d.contract.primaryExchange) for d in stocks]}")
    if not stocks:
        rep.warn(f"{symbol} did not resolve to a US stock")
        return None
    d = stocks[0]
    c = d.contract
    rep.ok(f"{symbol}: conId={c.conId} primaryExchange={c.primaryExchange} "
           f"marketRuleIds={d.marketRuleIds!r}")
    rep.check("fractional eligibility from ContractDetails.minSize / sizeIncrement",
              f"minSize={d.minSize} sizeIncrement={d.sizeIncrement} suggestedSizeIncrement="
              f"{getattr(d, 'suggestedSizeIncrement', None)} -> fractionable="
              f"{M.fractionable_from_size(d.minSize, d.sizeIncrement)}")
    rule_ids = [s for s in str(d.marketRuleIds or "").split(",") if s.strip()]
    if rule_ids:
        rows = await ib.reqMarketRuleAsync(int(rule_ids[0]))
        rep.info(f"market rule {rule_ids[0]}: {[(r.lowEdge, r.increment) for r in rows][:6]}")
    ticker = (await ib.reqTickersAsync(c))[0]
    rep.check("snapshot ticks (marketDataType 1/2 live-or-frozen is usable; 3/4 delayed is refused)",
              f"bid={_num(ticker.bid)} ask={_num(ticker.ask)} last={_num(ticker.last)} "
              f"close={_num(ticker.close)} marketDataType={ticker.marketDataType} "
              f"delayed={M.delayed_market_data(ticker.marketDataType)}")
    stream = ib.reqMktData(c, "236", False, False)
    deadline = time.monotonic() + 4.0
    while _num(getattr(stream, "shortableShares", None)) is None and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    shares = getattr(stream, "shortableShares", None)
    ib.cancelMktData(c)
    rep.check("shortableShares (adapter: > 2.5 = shortable and easy to borrow)",
              f"{shares} -> easy_to_borrow={M.is_easy_to_borrow(shares)}")
    last = _num(ticker.last) or _num(ticker.close) or _num(ticker.ask) or _num(ticker.bid)
    if last:
        from ib_async import Order
        far = max(round(last * 0.5, 2), 0.01)
        order = Order(action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=far, tif="DAY",
                      account=account)
        try:
            state = await asyncio.wait_for(ib.whatIfOrderAsync(c, order), args.timeout)
            rep.check("whatIfOrder (adapter: bp_cost = initMarginChange x multiplier)",
                      f"1 share LMT {far}: initMarginChange={_num(state.initMarginChange)} "
                      f"maintMarginChange={_num(state.maintMarginChange)} "
                      f"commission={_num(state.commission)} warning={state.warningText!r}")
        except Exception as e:  # noqa: BLE001 -- informational
            rep.warn(f"what-if failed: {type(e).__name__}: {e}")
    return d


async def option_section(ib: Any, symbol: str, stock: Any, rep: Report) -> None:
    from ib_async import Option
    rep.section(f"Option chain and quote: {symbol}")
    c = stock.contract
    params = await ib.reqSecDefOptParamsAsync(c.symbol, "", "STK", c.conId)
    rep.info(f"{len(params)} chain entries: "
             f"{[(p.exchange, p.tradingClass, p.multiplier, len(p.expirations)) for p in params][:8]}")
    standard = [p for p in params if str(p.multiplier) == "100"
                and str(p.tradingClass).upper() == M.to_ib_symbol(symbol).replace(' ', '')]
    if not standard:
        rep.warn("no standard (100-share, own trading class) chain")
        return
    chain = sorted(standard, key=lambda p: 0 if p.exchange == "SMART" else 1)[0]
    today = date.today()
    expiries = sorted(datetime.strptime(e, "%Y%m%d").date() for e in chain.expirations)
    wanted = next((e for e in expiries if e >= today + timedelta(days=20)), None)
    if wanted is None:
        rep.warn("no expiry 20+ days out")
        return
    probe = Option(symbol=c.symbol, lastTradeDateOrContractMonth=M.ib_expiry_string(wanted), right="",
                   exchange="SMART", currency="USD", multiplier="100", tradingClass=chain.tradingClass)
    details = await ib.reqContractDetailsAsync(probe)
    rep.check("one wildcard (blank right/strike) reqContractDetails returns the whole ladder",
              f"{len(details)} contracts for expiry {wanted}")
    calls = sorted((d.contract for d in details if d.contract.right == "C"), key=lambda k: k.strike)
    if not calls:
        rep.warn("no call contracts returned")
        return
    ticker = (await ib.reqTickersAsync(c))[0]
    spot = _num(ticker.last) or _num(ticker.close) or calls[len(calls) // 2].strike
    pick = min(calls, key=lambda k: abs(k.strike - spot))
    derived = M.occ_from_contract_fields(pick.localSymbol, pick.tradingClass, pick.symbol,
                                         pick.lastTradeDateOrContractMonth, pick.right, pick.strike)
    rebuilt = M.build_occ(pick.tradingClass or pick.symbol, wanted, M.OptionRight.CALL, pick.strike)
    rep.check("localSymbol with spaces removed IS the OCC symbol",
              f"localSymbol={pick.localSymbol!r} -> {derived!r}; rebuilt from fields {rebuilt!r}; "
              f"equal={derived == rebuilt}")
    stream = ib.reqMktData(pick, "101", False, False)
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and not (
            _num(stream.bid) is not None or _num(stream.ask) is not None
            or getattr(stream, "modelGreeks", None) is not None):
        await asyncio.sleep(0.1)
    g = getattr(stream, "modelGreeks", None)
    rep.check("option ticks (bid/ask -1 = no quote; modelGreeks carries iv/delta/gamma/theta/vega; "
              "callOpenInterest needs generic tick 101)",
              f"bid={_num(stream.bid)} ask={_num(stream.ask)} last={_num(stream.last)} "
              f"iv={_num(getattr(g, 'impliedVol', None))} delta={_num(getattr(g, 'delta', None))} "
              f"theta={_num(getattr(g, 'theta', None))} callOI={_num(stream.callOpenInterest)} "
              f"volume={_num(stream.volume)} marketDataType={stream.marketDataType}")
    ib.cancelMktData(pick)
    await combo_whatif(ib, calls, spot, rep)


async def combo_whatif(ib: Any, calls: List[Any], spot: float, rep: Report) -> None:
    """A 2-leg combo what-if with a NEGATIVE limit (credit) and the per-leg structure it implies.
    what-if is read-only: nothing is placed."""
    from ib_async import ComboLeg, Contract, Order
    if len(calls) < 2:
        return
    ordered = sorted(calls, key=lambda k: abs(k.strike - spot))
    short_leg, long_leg = ordered[0], ordered[1]
    bag = Contract(secType="BAG", symbol=short_leg.symbol, exchange="SMART", currency="USD",
                   comboLegs=[ComboLeg(conId=short_leg.conId, ratio=1, action="SELL", exchange="SMART"),
                              ComboLeg(conId=long_leg.conId, ratio=1, action="BUY", exchange="SMART")])
    order = Order(action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=-0.05, tif="DAY")
    try:
        state = await asyncio.wait_for(ib.whatIfOrderAsync(bag, order), 20)
        rep.check("BAG order with a NEGATIVE limit (credit) is accepted (adapter: action BUY + signed price)",
                  f"what-if ok: initMarginChange={_num(state.initMarginChange)} "
                  f"warning={state.warningText!r}")
    except Exception as e:  # noqa: BLE001 -- the answer is the point
        rep.check("BAG order with a NEGATIVE limit (credit) is accepted", f"REFUSED: {type(e).__name__}: {e}")
    rep.info("per-leg combo fill shape needs a real fill: after a paper fill of a combo run "
             "--dump-executions and look for one execution per leg (secType OPT) with the combo's permId "
             "(the adapter attributes leg prices by (permId, OCC))")


async def executions_section(ib: Any, account: str, rep: Report) -> None:
    rep.section("Executions (raw)")
    fills = await ib.reqExecutionsAsync()
    rep.info(f"{len(fills)} executions")
    for f in fills:
        ex, c = f.execution, f.contract
        flag = "  <-- orderId 0: candidate assignment/exercise" if not ex.orderId else ""
        rep.line(f"    {ex.time} acct={ex.acctNumber} {c.secType} {c.localSymbol or c.symbol} "
                 f"{ex.side} {ex.shares}@{ex.price} orderId={ex.orderId} permId={ex.permId} "
                 f"exchange={ex.exchange!r} liquidation={ex.liquidation} ref={ex.orderRef!r}{flag}")
    by_perm = {}
    for f in fills:
        by_perm.setdefault(f.execution.permId, []).append(f)
    combos = {perm: fs for perm, fs in by_perm.items() if perm and len({x.contract.conId for x in fs}) > 1}
    rep.check("COMBO executions: do the legs' executions carry the combo's orderRef, and is there any "
              "combo-level (BAG) execution? The adapter prices a combo from its LEG fills "
              "(side x leg average x ratio) found by (permId, conId) or (orderRef, conId)",
              [f"permId={perm}: legs={[(x.contract.secType, x.contract.conId, x.execution.side, x.execution.shares, x.execution.price, x.execution.orderRef) for x in fs]}"
               for perm, fs in list(combos.items())[:4]] or "no multi-leg execution in the window "
              f"(BAG-level executions seen: {sum(1 for f in fills if f.contract.secType == 'BAG')})")
    rep.check("executions older than ~7 days are not returned", f"oldest seen: "
              f"{min((f.execution.time for f in fills), default=None)}")


def flex_section(args: argparse.Namespace, account: str, rep: Report,
                 http_get: Optional[Callable[[str], str]] = None) -> None:
    from ba2_common.core import ibkr_flex as F
    rep.section("Flex Web Service")
    kwargs = {"http_get": http_get} if http_get else {}
    client = F.FlexClient(args.flex_token, args.flex_query, **kwargs)
    statements = client.fetch(account)
    rep.ok(f"{len(statements)} statement(s) for {account}")
    for st in statements:
        kinds: dict = {}
        for row in st.cash_transactions:
            kinds[row.get("type")] = kinds.get(row.get("type"), 0) + 1
        rep.check("CashTransaction types present", kinds)
        rep.info(f"equity-summary rows: {len(st.equity_summary)}")
    rep.info(f"dividends parsed: {len(F.dividends_from(statements))}, "
             f"cash transfers: {len(F.cash_transfers_from(statements))}, "
             f"NAV points: {len(F.balance_history_from(statements))}")


async def place_test_order(ib: Any, account: str, symbol: str, details: Any, rep: Report,
                           args: argparse.Namespace) -> None:
    from ib_async import Order
    rep.section("Test order (PAPER): place and cancel a far-from-market 1-share limit")
    if details is None:
        raise Refusal("no resolved contract to trade; nothing placed")
    if not account.startswith(M.PAPER_ACCOUNT_PREFIX):          # belt and braces: connect() checked too
        raise Refusal(f"{account} is not a paper account; nothing placed")
    contract = details.contract
    ticker = (await ib.reqTickersAsync(contract))[0]
    last = _num(ticker.last) or _num(ticker.close) or _num(ticker.bid) or _num(ticker.ask)
    if last is None:
        raise Refusal(f"no price for {symbol}; refusing to guess a limit")
    far = max(round(last * 0.5, 2), 0.01)
    order = Order(action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=far, tif="DAY",
                  outsideRth=False, orderRef=TEST_ORDER_REF, account=account, transmit=True)
    trade = ib.placeOrder(contract, order)
    deadline = time.monotonic() + args.timeout
    while trade.orderStatus.status in ("", "PendingSubmit", "ApiPending") and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    rep.ok(f"placed BUY 1 {symbol} LMT {far} DAY: orderId={trade.order.orderId} "
           f"permId={trade.orderStatus.permId} status={trade.orderStatus.status}")
    rep.check("a working order shows PreSubmitted/Submitted with a permId; orderRef round-trips",
              f"status={trade.orderStatus.status} permId={trade.orderStatus.permId} "
              f"orderRef={trade.order.orderRef!r}")
    ib.cancelOrder(trade.order)
    deadline = time.monotonic() + args.timeout
    while trade.orderStatus.status not in ("Cancelled", "ApiCancelled", "Filled", "Inactive") \
            and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    rep.check("cancel handshake PendingCancel -> Cancelled", f"final status={trade.orderStatus.status}")
    if trade.orderStatus.status == "Filled":
        rep.warn("the far-from-market order FILLED (price moved?); check and close the 1-share position")
    await modify_check(ib, contract, account, last, args, rep)
    await stop_modify_check(ib, contract, account, last, args, rep)
    await oca_check(ib, contract, account, last, args, rep)


async def _wait_status(trade: Any, wanted: tuple, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while trade.orderStatus.status not in wanted and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    return str(trade.orderStatus.status)


async def modify_check(ib: Any, contract: Any, account: str, last: float, args: argparse.Namespace,
                       rep: Report) -> None:
    """How IB confirms (and refuses) a modification. ib_async keeps the status unchanged, so the adapter
    waits for a 'Modified' log entry; a refusal must arrive as an error."""
    from ib_async import Order
    far = max(round(last * 0.5, 2), 0.02)
    order = Order(action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=far, tif="DAY",
                  orderRef=TEST_ORDER_REF + "-mod", account=account, transmit=True)
    trade = ib.placeOrder(contract, order)
    immediately = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), args.timeout)
    rep.check("does TWS list a NOT-YET-ACKNOWLEDGED order? (the 'never reached IBKR' rule assumes an order IB "
              "has not acknowledged is on none of its lists)",
              f"local status at the re-read={trade.orderStatus.status!r}; listed by IB: "
              f"{any(t.order.orderId == trade.order.orderId for t in immediately)}")
    await _wait_status(trade, ("Submitted", "PreSubmitted"), args.timeout)
    log_len = len(trade.log)
    trade.order.lmtPrice = round(far + 0.05, 2)
    ib.placeOrder(contract, trade.order)
    # an IMMEDIATE re-read (what the adapter does after ~1 s): does ib_async log 'Modified' although the
    # order still carries the OLD price? (an unchanged orderStatus is what 'Modified' is keyed on)
    reread = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), args.timeout)
    mine = [t for t in reread if t.order.orderId == trade.order.orderId]
    rep.check("auxPrice as echoed for a LIMIT order (0.0 or unset?): the adapter compares only the prices it "
              "SENT, so an echoed aux of either kind must not veto a confirmation",
              f"auxPrice after the re-read={mine[0].order.auxPrice if mine else 'not listed'} "
              f"(UNSET marker is {1.7976931348623157e308})")
    rep.check("modify + IMMEDIATE re-read: is 'Modified' logged while the open order still shows the OLD "
              "price? (the adapter accepts 'Modified' only when the order object carries the NEW price)",
              f"'Modified' logged={any(e.message == 'Modified' for e in trade.log[log_len:])}; open-order "
              f"lmtPrice after the re-read={mine[0].order.lmtPrice if mine else 'not listed'} "
              f"(sent {round(far + 0.05, 2)}, was {far})")
    await asyncio.sleep(min(args.timeout, 4.0))
    entries = [(e.status, e.message, e.errorCode) for e in trade.log[log_len:]]
    rep.check("a modification is acknowledged by a 'Modified' log entry while status stays the same",
              f"status={trade.orderStatus.status} new log entries={entries}")
    refused_at = len(trade.log)
    trade.order.lmtPrice = 0.0
    ib.placeOrder(contract, trade.order)
    await asyncio.sleep(min(args.timeout, 4.0))
    status_after_refusal = trade.orderStatus.status
    still_listed = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), args.timeout)
    rep.check("a REFUSED modification (limit 0.0) arrives as an error/warning, not as 'Modified'; and the "
              "LOCAL status after it (ib_async marks the trade Cancelled on a non-warning error although IB "
              "may still work the order) vs after a re-read of the open orders",
              f"status right after={status_after_refusal!r}; listed by IB after the re-read="
              f"{any(t.order.orderId == trade.order.orderId for t in still_listed)}; status after the "
              f"re-read={trade.orderStatus.status!r}; new log entries="
              f"{[(e.status, e.message, e.errorCode) for e in trade.log[refused_at:]]}")
    trade.order.lmtPrice = round(far + 0.05, 2)
    ib.cancelOrder(trade.order)
    await _wait_status(trade, ("Cancelled", "ApiCancelled", "Inactive"), args.timeout)
    # cancelling an ALREADY-CANCELLED order: which error does IB answer with (the adapter treats 103/104/135/
    # 136/161/10147/10148 as "the cancel was refused")?
    seen = []

    def on_error(req_id, code, msg, contract=None):
        if req_id == trade.order.orderId:
            seen.append((code, msg))
    ib.errorEvent += on_error
    ib.cancelOrder(trade.order)
    await asyncio.sleep(min(args.timeout, 3.0))
    ib.errorEvent -= on_error
    rep.check("error codes when CANCELLING AN ALREADY-CANCELLED order (adapter refusal set: "
              f"{sorted(M.ORDER_STATE_CODES)})", seen or "no error answered")


async def stop_modify_check(ib: Any, contract: Any, account: str, last: float, args: argparse.Namespace,
                            rep: Report) -> None:
    """A RESTING STOP leg is what the adapter modifies most. ib_async logs ``Modified`` only when the echoed
    status is exactly ``Submitted``; a stop that rests as ``PreSubmitted`` (typical outside the regular
    session, and for stops IB simulates) gets no log entry although IB applies the change. The adapter
    therefore also re-reads the open order and compares prices. This records which status the stop rests
    in, whether the 'Modified' entry appears, and whether the re-read shows the new price."""
    from ib_async import Order
    stop = round(last * 1.5, 2)
    order = Order(action="BUY", totalQuantity=1, orderType="STP LMT", auxPrice=stop,
                  lmtPrice=round(stop * 1.005, 2), tif="GTC", orderRef=TEST_ORDER_REF + "-stpmod",
                  account=account, transmit=True)
    trade = ib.placeOrder(contract, order)
    resting = await _wait_status(trade, ("Submitted", "PreSubmitted", "Cancelled", "Inactive"), args.timeout)
    log_len = len(trade.log)
    new_stop = round(stop + 0.05, 2)
    trade.order.auxPrice, trade.order.lmtPrice = new_stop, round(new_stop * 1.005, 2)
    ib.placeOrder(contract, trade.order)
    await asyncio.sleep(min(args.timeout, 4.0))
    modified_logged = any(e.message == "Modified" for e in trade.log[log_len:])
    listed = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), args.timeout)
    mine = [t for t in listed if t.order.orderId == trade.order.orderId]
    echoed = bool(mine) and abs(mine[0].order.auxPrice - new_stop) < 1e-6
    rep.check("a RESTING STOP's modification: resting status, 'Modified' log entry, and whether a re-read of "
              "the open orders shows the new stop (the adapter confirms on EITHER; PreSubmitted should give "
              "no log entry but a matching re-read)",
              f"resting status={resting}; 'Modified' logged={modified_logged}; open-order re-read shows the "
              f"new stop={echoed}; status now={trade.orderStatus.status}")
    ib.cancelOrder(trade.order)
    await _wait_status(trade, ("Cancelled", "ApiCancelled", "Inactive"), args.timeout)


async def oca_check(ib: Any, contract: Any, account: str, last: float, args: argparse.Namespace,
                    rep: Report) -> None:
    """Two BUY orders in one OCA group (ocaType 2), both transmit=True, stop first: statuses, and what
    cancelling ONE leg does to the other. (Partial/full-fill reduction needs a real fill: not tested.)"""
    from ib_async import Order
    stop = round(last * 1.5, 2)
    stop_lmt = round(stop * 1.005, 2)
    tp = max(round(last * 0.5, 2), 0.02)
    group = f"ba2-smoke-oca-{int(time.time())}"
    sl_order = Order(action="BUY", totalQuantity=1, orderType="STP LMT", auxPrice=stop, lmtPrice=stop_lmt,
                     tif="GTC", ocaGroup=group, ocaType=2, orderRef=TEST_ORDER_REF + "-sl",
                     account=account, transmit=True)
    tp_order = Order(action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=tp, tif="GTC",
                     ocaGroup=group, ocaType=2, orderRef=TEST_ORDER_REF + "-tp", account=account,
                     transmit=True)
    sl_trade = ib.placeOrder(contract, sl_order)
    sl_status = await _wait_status(sl_trade, ("Submitted", "PreSubmitted", "Cancelled", "Inactive"), args.timeout)
    tp_trade = ib.placeOrder(contract, tp_order)
    tp_status = await _wait_status(tp_trade, ("Submitted", "PreSubmitted", "Cancelled", "Inactive"), args.timeout)
    rep.check("OCA pair with BOTH legs transmit=True, stop first (transmit=False is only a parent/child "
              "device): each leg goes live on its own",
              f"stop status={sl_status}, take-profit status={tp_status}, ocaType=2")
    ib.cancelOrder(tp_trade.order)
    await _wait_status(tp_trade, ("Cancelled", "ApiCancelled"), args.timeout)
    await asyncio.sleep(1.0)
    rep.check("cancelling ONE OCA leg leaves the other working (the adapter cancels both explicitly)",
              f"cancelled take-profit -> stop status={sl_trade.orderStatus.status}")
    ib.cancelOrder(sl_trade.order)
    await _wait_status(sl_trade, ("Cancelled", "ApiCancelled"), args.timeout)


# ----------------------------------------------------------------------------- driver
async def run(args: argparse.Namespace, ib_factory: Callable[[], Any], rep: Report,
              http_get: Optional[Callable[[str], str]] = None) -> int:
    ib = ib_factory()
    errors: List[str] = []
    ib.errorEvent += lambda reqId, code, msg, contract=None: (
        errors.append(f"{code} (reqId {reqId}): {msg}") if M.error_severity(code) not in ("info",) else None)
    try:
        account = await connect(ib, args, rep)
        await account_section(ib, account, rep)
        await book_section(ib, account, rep)
        stock = await equity_section(ib, args.symbol, rep, args, account)
        if args.option and stock is not None:
            await option_section(ib, args.symbol, stock, rep)
        await reconnect_positions_check(ib, args, account, rep)
        if args.dump_executions:
            await executions_section(ib, account, rep)
        if args.flex_token and args.flex_query:
            flex_section(args, account, rep, http_get)
        if args.place_test_order:
            await place_test_order(ib, account, args.symbol, stock, rep, args)
        rep.section("Non-info errors/warnings IB sent during this run")
        for e in errors or ["none"]:
            rep.line(f"    {e}")
        held = [t for t in ib.trades() if t.orderStatus.status == "ValidationError"]
        listed = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), args.timeout) if held else []
        rep.check("are HELD orders (399 held until the open / 404 shares being located) LISTED by "
                  "reqAllOpenOrders? (only answerable if one arose this run: place a market order pre-open)",
                  [f"id={t.order.orderId}: listed by IB="
                   f"{any(x.order.orderId == t.order.orderId for x in listed)}" for t in held]
                  or "no held order arose this run")
        rep.check("EXACT 321 text on a Gateway with 'Read-Only API' ON: run once with the Gateway's Read-Only "
                  "API checked and --place-test-order; the adapter refuses a new order on a 321 warning "
                  "containing 'read-only' (it never went live) -- compare the wording printed above",
                  [e for e in errors if e.startswith("321")] or "no 321 seen this run")
        seen = sorted({int(e.split(" ")[0]) for e in errors if e.split(" ")[0].isdigit()
                       and M.error_severity(int(e.split(" ")[0])) == "order_warning"})
        rep.check("order WARNING codes seen (399 held until the open, 404 shares being located, 10349 TIF "
                  "preset): ib_async sets status 'ValidationError' for them while the order stays LIVE. "
                  "Pre-market, place a 1-share MARKET order from TWS and note whether these arrive; also "
                  "note the Gateway's Configure > API > Precautions (order precautions) settings",
                  seen or "none this run")
        rep.section("Summary: facts to confirm (copy this block back)")
        for fact in rep.facts:
            rep.line(f"  - {fact}")
        rep.line("")
        rep.ok("done" + (" (nothing was placed)" if not args.place_test_order else ""))
        return 0
    except Refusal as e:
        rep.warn(f"REFUSED: {e}")
        return 2
    finally:
        if ib.isConnected():
            ib.disconnect()


def main(argv: Optional[List[str]] = None, ib_factory: Optional[Callable[[], Any]] = None,
         out: Callable[[str], None] = print,
         http_get: Optional[Callable[[str], str]] = None) -> int:
    args = parse_args(argv)
    if ib_factory is None:
        from ib_async import IB
        ib_factory = IB
    rep = Report(out)
    try:
        return asyncio.run(run(args, ib_factory, rep, http_get))
    except Exception as e:  # noqa: BLE001
        rep.warn(f"FAILED: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
