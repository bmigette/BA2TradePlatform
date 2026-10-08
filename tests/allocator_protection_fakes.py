"""A faithful fake of the TastyTrade complex-order API for the allocator TP/SL tests.

NOTHING HERE TALKS TO A BROKER. The fake implements the coroutines the real
``tastytrade.account.Account`` exposes (``place_complex_order``, ``get_complex_order``,
``delete_complex_order``, ``get_live_complex_orders``, ``get_positions``) and is driven through the
REAL ``TastyTradeAccount`` methods, so the order construction, the dry-run-first rule, the
read-back, the cancel-confirmation polling and the classification are all exercised. It returns
REAL SDK models (``PlacedOrder`` / ``PlacedComplexOrder`` / ``Leg`` / ``FillInfo``) so a validator
or a field the code relies on is genuinely part of the test.

Faithful means: the behaviours the SDK documents. What the real broker does that the SDK does not
document (which status a surviving OCO leg takes when its partner fills, what an expiry looks like,
the GTC lifetime) is MY READING, written down in one place (``BROKER_ASSUMPTIONS``) so the
supervised live test knows exactly what to verify.
"""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Dict, List, Optional
from unittest.mock import patch

from tastytrade.instruments import TickSize
from tastytrade.order import (
    FillInfo, InstrumentType as TTInstrumentType, Leg, NewComplexOrder, OrderAction,
    OrderStatus as TTOrderStatus, OrderTimeInForce, OrderType as TTOrderType, PlacedComplexOrder,
    PlacedOrder,
)
from tastytrade.utils import TastytradeError

from ba2_trade_platform.core.models import Position
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount

BROKER_ASSUMPTIONS = (
    "A1: TastyTrade refuses a SELL_TO_CLOSE larger than the shares held, including when a resting "
    "stop triggers on a position that has since become smaller.",
    "A2: resting closing orders RESERVE shares: a new closing order for shares an older one reserves "
    "is refused (or, if accepted, over-reserves).",
    "A3: an OCO counts its quantity ONCE (q, not 2q) against the held shares; otherwise an OCO above "
    "half the position fails its dry run.",
    "A4: after a PARTIAL fill of one OCO leg the partner leg stays live for the remainder (it is not "
    "cancelled and not reduced).",
    "A5: a timeout/connection error AFTER the broker accepted a placement leaves the order resting "
    "(the call raised, the order exists) -- resolved by finding the tag.",
    "A6: get_order / get_complex_order work for a GTC order placed on an EARLIER day.",
    "A7: the position read can LAG a fill by seconds to minutes.",
    "A8: external_identifier accepts the 'ba2prot:<id>:<n>' format and length.",
    "A9: GTC stops outside regular hours report one of Received / Live / Contingent / Routed (the "
    "classifier treats all of them as resting).",
    "When one OCO member fills, the partner is reported 'Cancelled' (the classifier does not "
    "depend on it: any fill wins).",
    "An expired GTC order is reported with status 'Expired' on its members.",
    "A cancelled OCO is reported 'Cancel Requested' for a while and then 'Cancelled'.",
    "A placement response carries the new complex order with members 'Live'; its read-back via "
    "get_complex_order shows the same.",
    "gtc_date is populated on the placed members.",
    "A3 is CONFIRMED on the live broker (a second OCO on the second share dry-runs fine: an OCO counts its "
    "quantity once). A11: TastyTrade margin-checks a STOP as if it filled AT ITS TRIGGER: the buying-power "
    "change is about q x (stop - p0) (p0 = price minus the margin released, ~$215 for a $287 stock). Stops "
    "within ~25% of the price cost nothing; deeper ones RESERVE buying power and resting stops keep it "
    "reserved; when it is not available the order is refused with 'margin_check_failed'. gtc_date is NULL on "
    "live GTC orders (no expiry, so no renewal).",
    "A10: order HISTORY ordering. get_order_history(sort='Desc', start_at=...) lists newest first and filters "
    "server side; get_complex_order_history has no sort option, so its ordering is DETECTED (a page whose first "
    "entry is older than its last is ascending: every page is read). Verify page 0 of both on the real broker.",
)


def sync_run(coro):
    return asyncio.run(coro)


class FakeEquity:
    """Stand-in for ``tastytrade.instruments.Equity`` returning REAL Legs."""

    def __init__(self, symbol, tick_sizes=None):
        self.symbol = symbol
        self.instrument_type = TTInstrumentType.EQUITY
        self.is_fractional_quantity_eligible = True
        self.tick_sizes = tick_sizes

    def build_leg(self, quantity, action):
        return Leg(instrument_type=TTInstrumentType.EQUITY, symbol=self.symbol,
                   quantity=quantity, action=action)


class FakeTastyBroker:
    """The broker side. State is plain data so a test can inspect or corrupt it."""

    def __init__(self):
        self.positions: Dict[str, Decimal] = {}
        self.prices: Dict[str, float] = {}
        self.tick_sizes = [TickSize(value=Decimal("0.0001"), threshold=Decimal("1")),
                           TickSize(value=Decimal("0.01"))]
        self.complex: Dict[int, dict] = {}
        self.singles: Dict[int, dict] = {}        # plain (stop-only) orders by id
        self.single_place_calls: List[tuple] = []   # (dry_run, NewOrder)
        self.single_delete_calls: List[int] = []
        self._next_complex = 9000
        self._next_order = 70000
        self.place_calls: List[tuple] = []     # (dry_run, NewComplexOrder)
        self.delete_calls: List[int] = []
        # --- fault injection ---
        self.dry_run_errors: List[str] = []
        self.place_errors: List[str] = []       # errors reported in the live response
        self.raise_on_place: Optional[Exception] = None
        self.raise_on_read: Optional[Exception] = None
        self.raise_on_delete: Optional[Exception] = None
        self.reject_after_accept = False        # accepted, then the read-back says Rejected
        self.cancel_polls = 1                   # reads that still say 'Cancel Requested'
        self.never_confirm_cancel = False
        self.fill_on_delete: Optional[str] = None   # "TP" or "SL": the order fills instead of cancelling
        self.single_raise_on_place: Optional[Exception] = None
        self.single_dry_run_errors: List[str] = []
        self.single_reject_after_accept = False
        self.single_raise_on_read: Optional[Exception] = None
        self.single_fill_on_delete = False
        self.positions_fail = False
        self.history_fails = False
        self.bp_report_zero = False   # the dry run reports a buying-power change of 0
        self.bp_p0 = None          # when set, a stop's BP effect is q x (stop - p0)
        self.available_bp = 0.0    # the account's buying power for that model
        self.history_ignores_sort = False   # a server that ignores sort='Desc' (lists oldest first)
        self.history_ascending = False   # history lists oldest first unless sort='Desc' is passed (plain orders)

    # ------------------------------------------------------------- SDK surface
    def _stop_bp_change(self, orders) -> Decimal:
        """BP change of the STOP legs among ``orders`` under the live-broker model (A11)."""
        if self.bp_p0 is None:
            return Decimal(0)
        total = Decimal(0)
        for o in orders:
            if getattr(o, "stop_trigger", None) is not None:
                total += Decimal(str(o.legs[0].quantity)) * (Decimal(str(o.stop_trigger)) - Decimal(str(self.bp_p0)))
        return total

    def _reserved_bp(self) -> Decimal:
        """Buying power the RESTING stops already reserve (only their negative effects)."""
        total = Decimal(0)
        live = (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED)
        members = [r["member"] for r in self.singles.values()] + \
            [m for r in self.complex.values() for m in r["members"]]
        for m in members:
            if m.status in live and getattr(m, "stop_trigger", None) is not None and self.bp_p0 is not None:
                change = Decimal(str(m.size)) * (Decimal(str(m.stop_trigger)) - Decimal(str(self.bp_p0)))
                if change < 0:
                    total += -change
        return total

    def net_bp(self) -> float:
        """Buying power REMAINING: the total less what the resting stops reserve (what the real snapshot reports)."""
        return float(self.available_bp) - float(self._reserved_bp())

    def _bp_check(self, orders):
        """The margin check: refuse (raise) when the stops' reservation is not affordable; else the effect."""
        change = self._stop_bp_change(orders)
        if change < 0 and -change + self._reserved_bp() > Decimal(str(self.available_bp)):
            raise TastytradeError("margin_check_failed: Your account does not have sufficient buying power")
        return SimpleNamespace(change_in_buying_power=Decimal(0) if self.bp_report_zero else change)

    async def place_complex_order(self, session, order: NewComplexOrder, dry_run: bool = True):
        self.place_calls.append((dry_run, order))
        effect = self._bp_check(order.orders)
        if dry_run:
            errors = [SimpleNamespace(code="x", message=m) for m in self.dry_run_errors]
            return SimpleNamespace(complex_order=SimpleNamespace(id=-1, orders=[]),
                                   errors=errors or None, warnings=None, buying_power_effect=effect)
        if self.raise_on_place is not None:
            raise self.raise_on_place
        cid = self._next_complex
        self._next_complex += 1
        members = []
        for new in order.orders:
            members.append(self._placed_from_new(new))
            members[-1].complex_order_id = cid      # the legs show their complex order in the PLAIN history
        self.complex[cid] = {"id": cid, "members": members, "cancel_left": None,
                             "type": str(order.type.value)}
        errors = [SimpleNamespace(code="x", message=m) for m in self.place_errors]
        if self.reject_after_accept:
            for m in members:
                m.status = TTOrderStatus.REJECTED
        return SimpleNamespace(complex_order=self._placed_complex(cid), errors=errors or None,
                               warnings=None)

    async def get_complex_order(self, session, order_id: int):
        if self.raise_on_read is not None:
            raise self.raise_on_read
        record = self.complex.get(int(order_id))
        if record is None:
            raise TastytradeError(f"complex order {order_id} not found")
        if record["cancel_left"] is not None:
            if self.never_confirm_cancel:
                pass
            elif record["cancel_left"] <= 0:
                for m in record["members"]:
                    if m.status == TTOrderStatus.CANCEL_REQUESTED:
                        m.status = TTOrderStatus.CANCELLED
                record["cancel_left"] = None
            else:
                record["cancel_left"] -= 1
        return self._placed_complex(int(order_id))

    async def delete_complex_order(self, session, order_id: int):
        self.delete_calls.append(int(order_id))
        if self.raise_on_delete is not None:
            raise self.raise_on_delete
        record = self.complex.get(int(order_id))
        if record is None:
            raise TastytradeError(f"complex order {order_id} not found")
        if all(m.status in (TTOrderStatus.FILLED, TTOrderStatus.CANCELLED,
                            TTOrderStatus.EXPIRED, TTOrderStatus.REJECTED)
               for m in record["members"]):
            raise TastytradeError("order is not cancellable")
        if self.fill_on_delete:
            self.fill(int(order_id), self.fill_on_delete)
            return
        for m in record["members"]:
            m.status = TTOrderStatus.CANCEL_REQUESTED
        record["cancel_left"] = self.cancel_polls

    # ---- plain orders (the stop-only runner) ---------------------------------
    def single_place_count(self) -> int:
        """LIVE (non dry-run) plain-order placements so far."""
        return sum(1 for dry, _ in self.single_place_calls if not dry)

    def place_foreign_stop(self, symbol: str, qty: int, stop: float, tag: str, received_at=None) -> int:
        """A resting STOP order for ANOTHER symbol that carries ``tag`` (a reused-id collision)."""
        from tastytrade.order import Leg, NewOrder, OrderAction, OrderTimeInForce
        from tastytrade.order import OrderType as TTOT
        leg = Leg(instrument_type=TTInstrumentType.EQUITY, symbol=symbol, action=OrderAction.SELL_TO_CLOSE,
                  quantity=Decimal(qty))
        new = NewOrder(time_in_force=OrderTimeInForce.GTC, order_type=TTOT.STOP, legs=[leg],
                       stop_trigger=Decimal(str(stop)), external_identifier=tag)
        member = self._placed_from_new(new)
        if received_at is not None:
            member.received_at = received_at
            member.updated_at = received_at
        self.singles[int(member.id)] = {"member": member, "cancel_left": None}
        return int(member.id)

    async def place_order(self, session, order, dry_run: bool = True):
        self.single_place_calls.append((dry_run, order))
        effect = self._bp_check([order])
        if dry_run:
            errors = [SimpleNamespace(code="x", message=m) for m in self.single_dry_run_errors]
            return SimpleNamespace(order=SimpleNamespace(id=-1), errors=errors or None, warnings=None,
                                   buying_power_effect=effect)
        if self.single_raise_on_place is not None:
            raise self.single_raise_on_place
        member = self._placed_from_new(order)
        oid = int(member.id)
        self.singles[oid] = {"member": member, "cancel_left": None}
        if self.single_reject_after_accept:
            member.status = TTOrderStatus.REJECTED
        return SimpleNamespace(order=member, errors=None, warnings=None)

    async def get_order(self, session, order_id: int):
        if self.single_raise_on_read is not None:
            raise self.single_raise_on_read
        record = self.singles.get(int(order_id))
        if record is None:
            raise TastytradeError(f"order {order_id} not found")
        if record["cancel_left"] is not None:
            if self.never_confirm_cancel:
                pass
            elif record["cancel_left"] <= 0:
                if record["member"].status == TTOrderStatus.CANCEL_REQUESTED:
                    record["member"].status = TTOrderStatus.CANCELLED
                record["cancel_left"] = None
            else:
                record["cancel_left"] -= 1
        return record["member"]

    async def delete_order(self, session, order_id: int):
        self.single_delete_calls.append(int(order_id))
        if self.raise_on_delete is not None:
            raise self.raise_on_delete
        record = self.singles.get(int(order_id))
        if record is None:
            raise TastytradeError(f"order {order_id} not found")
        member = record["member"]
        if member.status in (TTOrderStatus.FILLED, TTOrderStatus.CANCELLED,
                             TTOrderStatus.EXPIRED, TTOrderStatus.REJECTED):
            raise TastytradeError("order is not cancellable")
        if self.single_fill_on_delete:
            self.fill_single(int(order_id))
            return
        member.status = TTOrderStatus.CANCEL_REQUESTED
        record["cancel_left"] = self.cancel_polls

    async def get_live_orders(self, session):
        if self.history_fails:
            raise TastytradeError("order list unavailable")
        return [r["member"] for r in self.singles.values()
                if r["member"].status in (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED)]

    async def get_order_history(self, session, per_page=50, page_offset=0, **kw):
        if self.history_fails:
            raise TastytradeError("order history unavailable")
        rows = [r["member"] for r in self.singles.values()] + \
            [m for r in self.complex.values() for m in r["members"]]
        rows.sort(key=lambda m: int(m.id))
        ascending = self.history_ignores_sort or (self.history_ascending and kw.get("sort") != "Desc")
        if not ascending:
            rows = rows[::-1]
        return rows[page_offset * per_page:(page_offset + 1) * per_page]

    async def get_complex_order_history(self, session, per_page=50, page_offset=0):
        if self.history_fails:
            raise TastytradeError("complex order history unavailable")
        rows = [self._placed_complex(cid) for cid in self.complex]
        if not self.history_ascending:
            rows = rows[::-1]
        return rows[page_offset * per_page:(page_offset + 1) * per_page]

    async def get_live_complex_orders(self, session):
        return [self._placed_complex(cid) for cid, r in self.complex.items()
                if any(m.status in (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED)
                       for m in r["members"])]

    async def get_positions(self, session, include_marks=True):
        if self.positions_fail:
            raise TastytradeError("positions unavailable")
        return [SimpleNamespace(
            symbol=sym, quantity=abs(qty), quantity_direction="Long" if qty >= 0 else "Short",  # the REAL SDK shape: absolute quantity + a str direction
            average_open_price=Decimal("50"), close_price=Decimal(str(self.prices.get(sym, 50))),
            mark_price=Decimal(str(self.prices.get(sym, 50))), multiplier=1,
            instrument_type=TTInstrumentType.EQUITY, realized_day_gain=Decimal("0"))
            for sym, qty in self.positions.items() if qty != 0]

    # ------------------------------------------------------------- broker events
    def _member(self, complex_id: int, kind: str):
        for m in self.complex[complex_id]["members"]:
            is_limit = m.order_type == TTOrderType.LIMIT
            if (kind == "TP") == is_limit:
                return m
        raise KeyError(kind)

    def fill(self, complex_id: int, kind: str, qty: Optional[int] = None, price: Optional[float] = None):
        """A member fills (``kind`` TP or SL). ``qty`` None = fully. The position shrinks."""
        record = self.complex[complex_id]
        member = self._member(complex_id, kind)
        leg = member.legs[0]
        size = int(member.size)
        done = size if qty is None else int(qty)
        px = price if price is not None else float(member.price if kind == "TP" else member.stop_trigger)
        leg.fills = (leg.fills or []) + [FillInfo(
            fill_id=f"f{complex_id}-{kind}", quantity=Decimal(done), fill_price=Decimal(str(px)),
            filled_at=datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))]
        self.positions[member.underlying_symbol] = (
            self.positions.get(member.underlying_symbol, Decimal(0)) - Decimal(done))
        if done >= size:
            member.status = TTOrderStatus.FILLED
            for other in record["members"]:
                if other is not member and other.status in (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED):
                    other.status = TTOrderStatus.CANCELLED

    def fill_single(self, order_id: int, qty: Optional[int] = None, price: Optional[float] = None):
        """A stop-only order fills (fully, or ``qty`` shares). The position shrinks."""
        member = self.singles[order_id]["member"]
        size = int(member.size)
        done = size if qty is None else int(qty)
        px = price if price is not None else float(member.stop_trigger)
        leg = member.legs[0]
        leg.fills = (leg.fills or []) + [FillInfo(
            fill_id=f"s{order_id}", quantity=Decimal(done), fill_price=Decimal(str(px)),
            filled_at=datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))]
        self.positions[member.underlying_symbol] = (
            self.positions.get(member.underlying_symbol, Decimal(0)) - Decimal(done))
        if done >= size:
            member.status = TTOrderStatus.FILLED

    def expire_single(self, order_id: int):
        self.singles[order_id]["member"].status = TTOrderStatus.EXPIRED

    def external_cancel_single(self, order_id: int):
        self.singles[order_id]["member"].status = TTOrderStatus.CANCELLED

    def expire(self, complex_id: int):
        for m in self.complex[complex_id]["members"]:
            m.status = TTOrderStatus.EXPIRED

    def external_cancel(self, complex_id: int):
        """Somebody cancelled it on the broker's site (we did not ask)."""
        for m in self.complex[complex_id]["members"]:
            m.status = TTOrderStatus.CANCELLED

    def set_member_status(self, complex_id: int, kind: str, status: TTOrderStatus):
        self._member(complex_id, kind).status = status

    # ------------------------------------------------------------- helpers
    def _placed_from_new(self, new) -> PlacedOrder:
        oid = self._next_order
        self._next_order += 1
        legs = [Leg(instrument_type=l.instrument_type, symbol=l.symbol, action=l.action,
                    quantity=l.quantity, remaining_quantity=l.quantity, fills=None)
                for l in new.legs]
        size = sum(Decimal(str(l.quantity)) for l in new.legs)
        return PlacedOrder(
            account_number="5WX00000", time_in_force=new.time_in_force, order_type=new.order_type,
            underlying_symbol=new.legs[0].symbol, underlying_instrument_type=TTInstrumentType.EQUITY,
            status=TTOrderStatus.LIVE, cancellable=True, editable=True, edited=False,
            updated_at=datetime.now(timezone.utc),
            received_at=datetime.now(timezone.utc), legs=legs, id=oid,
            size=size, price=new.price, stop_trigger=new.stop_trigger,
            gtc_date=date(2027, 1, 3), external_identifier=new.external_identifier)

    def _placed_complex(self, cid: int) -> PlacedComplexOrder:
        record = self.complex[cid]
        return PlacedComplexOrder(account_number="5WX00000", type=record["type"],
                                  orders=list(record["members"]), id=cid)


def make_account(broker: FakeTastyBroker, *, account_id: int = 1) -> TastyTradeAccount:
    """A REAL ``TastyTradeAccount`` (no ``__init__``: no network, no settings lookup) whose SDK
    account object is the fake. Quotes come from ``broker.prices``; the cancel poll never sleeps."""
    acct = object.__new__(TastyTradeAccount)
    acct.id = account_id
    acct._authentication_error = None
    acct._session = SimpleNamespace(label="fake-session")
    acct._account = broker
    acct._loop = None
    acct._loop_thread = None
    acct._settings_cache = {}
    acct._run_async = sync_run
    # A tiny REAL sleep: with a no-op the unconfirmed-cancel poll spun for the whole timeout, opening thousands
    # of asyncio loops (a socketpair each) and exhausting Windows' loopback ports (the suite then hung).
    acct._sleep = lambda seconds: __import__("time").sleep(0.02)
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 2.0
    acct.get_instrument_current_price = lambda symbols, price_type="mark": {
        s: broker.prices.get(s) for s in (symbols if isinstance(symbols, list) else [symbols])}
    acct._get_instrument_current_price_impl = lambda symbols, price_type="bid": {
        s: broker.prices.get(s) for s in (symbols if isinstance(symbols, list) else [symbols])}
    original_snapshot = acct.get_account_snapshot

    def snapshot():
        if broker.bp_p0 is None:
            return original_snapshot()
        return SimpleNamespace(buying_power=broker.net_bp())      # net of what resting stops reserve
    acct.get_account_snapshot = snapshot
    return acct


def patch_equity(broker: FakeTastyBroker):
    """Context manager: ``Equity.get`` returns a ``FakeEquity`` carrying the broker's tick table."""
    async def _get(session, symbols):
        return FakeEquity(symbols, tick_sizes=broker.tick_sizes)
    return patch("tastytrade.instruments.Equity.get", new=_get)
