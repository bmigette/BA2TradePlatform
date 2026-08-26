"""Options capability interface — a sibling mixin to AccountInterface.

Brokers that support options inherit BOTH, e.g.:
    class AlpacaAccount(AccountInterface, OptionsAccountInterface): ...

Capability detection elsewhere should use isinstance(account, OptionsAccountInterface).
The concrete submit_option_order() owns TradingOrder/Transaction persistence and
delegates the broker call to the abstract _submit_option_order_impl().
"""
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.option_types import OptionContract, OptionQuote, OptionLeg, OptionPosition
from ba2_common.core.types import OptionRight

#: Marker carried by EVERY assignment-capacity refusal, so it can be told apart from a
#: buying-power refusal at a glance and with one grep.
#:
#: The two have DIFFERENT REMEDIES and conflating them wastes an afternoon. A
#: buying-power refusal is about the RESERVE POOL: it frees up by closing (or repairing
#: the ``option_reserve`` of) *any* reserving structure — a bear call spread will do —
#: or by adding margin. A capacity refusal is about CASH FOR DELIVERY: nothing but
#: closing a SHORT PUT or funding the account moves it, and closing that bear call
#: spread does precisely nothing. Neither message uses the other's vocabulary.
ASSIGNMENT_CAPACITY_REFUSAL = "ASSIGNMENT CAPACITY"

#: Contracts below this are "flat". Order quantities are whole contracts, but they
#: arrive as floats off ``filled_qty`` and are summed, so an exact ``== 0`` test would
#: eventually be defeated by float addition.
_ASSIGNMENT_EPS = 1e-9

#: Marker carried by EVERY cover refusal at the submission boundary, so it can be told
#: apart at a glance — and with one grep — from the two money refusals above.
#:
#: THREE REFUSALS, THREE REMEDIES, and conflating them wastes an afternoon each time.
#: A buying-power refusal frees up by closing any reserving structure. A capacity
#: refusal needs cash for delivery. This one needs FREE SHARES: buy the missing shares of
#: the underlying, write fewer contracts, or close whichever open short call is holding
#: the pledge — which, because ``shares_pledged_to_short_calls`` counts every short call
#: in the book, includes the short leg of a credit spread on the same ticker. Adding
#: margin does nothing for it, and nothing it asks for helps the others.
COVER_REFUSAL = "UNCOVERED SHORT CALL"

#: The one strategy tag whose entire promise is "these contracts are covered by shares
#: this account holds". The cover guard REFUSES on exactly that promise and no other:
#:
#: * a ``short_strangle``/``short_put`` is DELIBERATELY undefined-risk and is gated by
#:   PremiumSeller's ``undefined_risk_max_pct`` rail, not by share inventory;
#: * a ``bear_call_spread`` answers for its short call with a LONG CALL rather than with
#:   shares, so it is never refused HERE — but it is not outside the cover ledger either:
#:   ``shares_pledged_to_short_calls`` counts its short leg as pledging shares like any
#:   other short call, so an open credit spread does consume cover on that ticker and can
#:   refuse a genuinely covered call written beside it. Fail-safe and deliberate (that
#:   short call really can have 100 shares called away), and a real constraint: two such
#:   sleeves on one ticker will not both write;
#: * every close path submits under ``"close"`` (``option_lifecycle_service``,
#:   ``PremiumSeller.portfolio``, ``close_option_position``), so flattening is never
#:   gated on a cover reading — a refusal there would strand an open position.
COVERED_CALL_STRATEGY = "covered_call"

#: Shares one contract delivers, and the ONLY value this platform can currently record:
#: ``submit_option_order`` stamps it on the parent and on every leg child unconditionally,
#: and ``shares_pledged_to_short_calls`` reads those rows back to price the pledge.
#:
#: THE COVER GUARD REQUIRES EXACTLY THIS NUMBER, BECAUSE IT IS THE NUMBER THAT WILL BE
#: WRITTEN. It used to prefer a multiplier the LEG published (OPT-L7 — an adjusted
#: contract does not deliver 100), which was correct arithmetic against a row the write
#: was incapable of producing: a leg publishing 10 was validated against 30 shares for 3
#: contracts and admitted, and the rows it then wrote reported 300 shares pledged. A gate
#: that approves one position and creates a different one is worse than no gate, so a leg
#: publishing anything other than this constant is now REFUSED rather than believed or
#: ignored (``check_cover_for_covered_call``). ``OptionLeg`` carries no such field, so
#: nothing on the platform can trip it today; teaching it to must change this constant's
#: two write sites and the accessor that reads them, together.
DEFAULT_OPTION_MULTIPLIER = 100

#: Sentinel telling "this leg publishes NO multiplier field" apart from "this leg
#: publishes one". The first is today's ``OptionLeg`` and takes the platform default,
#: which is not a guess but the very number being persisted; the second is a claim about
#: delivery size the rows cannot carry, and is a refusal whether it is readable or junk.
#: A plain ``getattr(leg, "multiplier", None)`` collapses the two, and collapsing them
#: refuses every covered call on the platform.
_MULTIPLIER_ABSENT = object()


@dataclass(frozen=True)
class ReservePool:
    """The buying-power reserve, WITH the orders whose reserve could not be read.

    ``total`` is a lower bound whenever ``unmeasurable`` is non-empty: those orders hold
    an unknown amount of capital, and an unknown is not zero. Every gate must treat an
    unmeasurable pool as a refusal rather than as ``total``.
    """
    total: float
    unmeasurable: Tuple[str, ...] = ()

    @property
    def is_measurable(self) -> bool:
        return not self.unmeasurable


@dataclass(frozen=True)
class AssignmentExposure:
    """Cash owed if every open SHORT PUT were assigned at once — the second view.

    ``cost`` is ``None`` when ANY held short put could not be priced: a sum with a
    missing addend is an unknown sum, not a smaller one. ``unmeasurable`` then names
    each order and why, so a caller learns which input was missing rather than only
    that something was.

    ``contracts`` is the short-put contract count that WAS measurable, and is reported
    even alongside an unknown cost: "how much of the book is short puts" stays useful
    when "what it would cost" does not.
    """
    cost: Optional[float]
    contracts: float = 0.0
    unmeasurable: Tuple[str, ...] = ()

    @property
    def is_measurable(self) -> bool:
        return self.cost is not None


@dataclass(frozen=True)
class AssignmentCapacity:
    """A capacity verdict WITH the reason it was reached.

    ``check_assignment_capacity`` answers yes/no, which is all a gate needs but not all
    an OPERATOR needs: "refused" and "refused because order 41's strike is missing" send
    someone to two different places. ``reason`` is empty when ``ok``; otherwise it is a
    complete sentence carrying ``ASSIGNMENT_CAPACITY_REFUSAL`` and naming the input that
    is missing or the money that is short.

    The figures are reported even when the verdict is a refusal, and each is ``None``
    exactly when it could not be measured — an unknown must not arrive as a zero here
    either.
    """
    ok: bool
    reason: str = ""
    held_cost: Optional[float] = None
    candidate_cost: Optional[float] = None
    cash: Optional[float] = None
    unmeasurable: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverCapacity:
    """A SHARE-cover verdict WITH the reason it was reached — ``AssignmentCapacity``'s shape.

    THE THREE REFUSALS ARE ONE FAMILY AND MUST TRAVEL THE SAME WAY. Buying power,
    assignment capacity and share cover are three rails on the same entry path with
    three different remedies (see ``COVER_REFUSAL``), and the first two already reach a
    caller as a RETURNED verdict that becomes a failed ``TradeActionResult``. Cover was
    the odd one out: it was only ever raised, which is the channel the *structural*
    validations use (an empty leg list, five legs, two expiries) — defects in the call
    itself, not operational outcomes. "The shares are not free yet" is an operational
    outcome: routine, expected, and something an operator acts on. Raised from
    ``TradeActions``' entry path it propagates out of ``execute()``, skipping every
    action queued behind it on that instrument and writing no result row for any of
    them, while logging a stack trace for a feed outage.

    ``reason`` is empty when ``ok``; otherwise it is a complete sentence carrying
    ``COVER_REFUSAL``, naming the input that could not be measured or the shares that
    are short, and ending with the remedy. It is the SAME sentence the seam raises, so
    an operator reading a refused action result and an operator reading the exception a
    direct caller got are reading one text.

    The figures are reported even on a refusal, and each is ``None`` exactly when it
    could not be measured — an unknown must never arrive here as a zero either.
    ``underlying`` names the ticker the verdict is about, which is not always the
    action's instrument: the guard groups by leg underlying so a stray leg on another
    name cannot be covered by the first name's shares.
    """
    ok: bool
    reason: str = ""
    underlying: Optional[str] = None
    required: Optional[int] = None
    held: Optional[int] = None
    pledged: Optional[int] = None


class OptionsAccountInterface(ABC):
    """Mixin granting an AccountInterface subclass option-trading capability."""

    supports_options: bool = True

    # --- Market data -------------------------------------------------------
    @abstractmethod
    def get_option_chain(
        self,
        underlying: str,
        expiry_min: date,
        expiry_max: date,
        option_type: Optional[OptionRight] = None,
        strike_min: Optional[float] = None,
        strike_max: Optional[float] = None,
    ) -> List[OptionContract]:
        """Return chain rows (quote + Greeks + liquidity) within the filters."""
        ...

    @abstractmethod
    def get_option_quote(self, contract_symbol: str) -> Optional[OptionQuote]:
        """Latest quote + Greeks for one OCC contract."""
        ...

    @abstractmethod
    def get_atm_implied_volatility(self, underlying: str) -> Optional[float]:
        """Current near-ATM implied volatility for the underlying (0-1)."""
        ...

    # --- Positions ---------------------------------------------------------
    @abstractmethod
    def get_option_positions(self) -> List[OptionPosition]:
        """All currently-held option positions."""
        ...

    # --- Orders ------------------------------------------------------------
    @abstractmethod
    def _submit_option_order_impl(self, trading_order, legs: List[OptionLeg],
                                  leg_orders: Optional[List[Any]] = None) -> Any:
        """Broker-specific submit. Receives the persisted parent TradingOrder and
        the legs; must set broker ids/status and return the parent order."""
        ...

    def submit_option_order(
        self,
        legs: List[OptionLeg],
        quantity: int,
        order_type: str = "limit",            # "market" | "limit"
        limit_price: Optional[float] = None,   # premium; +debit / -credit for spreads
        option_strategy: Optional[str] = None,
        expert_recommendation_id: Optional[int] = None,
        transaction_id: Optional[int] = None,
    ) -> Any:
        """Build & persist option TradingOrder(s), then submit to the broker.

        single leg -> one option TradingOrder (contract_symbol set)
        2-4 legs   -> a parent option order (option_strategy set, no contract_symbol)
                      + leg children linked via parent_order_id.

        Raises ValueError if the legs span more than one expiry, or if a ``covered_call``
        is not covered by free shares (see the two guards below). Both refuse BEFORE any
        row is written, so a refusal leaves nothing half-recorded.

        THE COVER RAISE IS A BACKSTOP, not the channel a caller should rely on. A short
        cover is an operational outcome with a remedy, not a malformed call: a caller
        that has somewhere to put a verdict asks :meth:`check_cover_for_covered_call`
        first and records a refusal (``SellCoveredCallAction`` does, via
        ``_refuse_if_cover_is_short``). The raise exists for the direct callers that have
        no such channel, where the only alternative is a silent naked write.
        """
        from ba2_common.core.db import add_instance, get_instance, update_instance
        from ba2_common.core.models import TradingOrder
        from ba2_common.core.types import AssetClass, OrderDirection, OrderType as CoreOrderType, OrderStatus
        from ba2_common.logger import logger

        if not legs:
            raise ValueError("submit_option_order requires at least one leg")
        if len(legs) > 4:
            raise ValueError("Alpaca supports a maximum of 4 option legs")

        # SINGLE-EXPIRY INVARIANT — checked here, BEFORE the parent order, the leg children
        # and the Transaction are written, so a refusal leaves nothing half-recorded (and
        # before the try/except below, which would otherwise convert this into a silent
        # `return None`).
        #
        # `Transaction.expiry` is ONE date and is documented as "the structure's expiry".
        # That is honest only because all 16 supported structures — the four singles, the
        # four verticals, straddle/strangle and their short forms, iron condor, jade lizard,
        # call butterfly, put ratio spread — put every leg on one expiry. A calendar or a
        # diagonal does not make that field incomplete, it makes it WRONG: a money record
        # asserting a date half the position does not honour, with nothing anywhere to
        # contradict it. Adding such a structure must therefore start by teaching the
        # Transaction to carry per-leg expiries — this refusal is the reminder.
        #
        # A leg whose expiry is None is UNKNOWN, not a second expiry, and is not counted:
        # the close paths rebuild legs from stored order rows (PremiumSeller/portfolio.py
        # reads `getattr(o, "expiry", None)`), and refusing there would strand an open
        # position that can no longer be flattened — much worse than an incomplete intent.
        expiries = sorted({leg.expiry for leg in legs if leg.expiry is not None})
        if len(expiries) > 1:
            raise ValueError(
                f"An option structure must be on a single expiry, but these {len(legs)} legs "
                f"span {len(expiries)}: {', '.join(d.isoformat() for d in expiries)}. "
                "Transaction.expiry holds a SINGLE value for the whole structure, so a "
                "calendar/diagonal would be recorded with an expiry that is simply wrong for "
                "part of the position. No order or transaction has been created."
            )

        # COVER INVARIANT — same placement and the same reason: BEFORE the parent order,
        # the leg children and the Transaction are written, and before the try/except that
        # would turn a refusal into a silent `return None`.
        #
        # Being early is not enough on its own; being before EVERY write is the property,
        # and it is doubly load-bearing here because `shares_pledged_to_short_calls` reads
        # the open order book: a guard that ran after the parent row existed would see this
        # very order as a pledge and refuse the covered call it is in the middle of writing.
        self._refuse_uncovered_covered_call(legs, quantity, option_strategy)

        first = legs[0]
        is_multi = len(legs) > 1
        if limit_price is None:
            net_side = first.side
        else:
            net_side = OrderDirection.BUY if limit_price >= 0 else OrderDirection.SELL

        side_for_type = net_side if is_multi else first.side
        if order_type == "market":
            core_type = CoreOrderType.MARKET
        else:
            core_type = CoreOrderType.BUY_LIMIT if side_for_type == OrderDirection.BUY else CoreOrderType.SELL_LIMIT

        parent = TradingOrder(
            account_id=self.id,
            symbol=(first.underlying or first.contract_symbol),
            underlying_symbol=first.underlying,
            quantity=quantity,
            side=(first.side if not is_multi else net_side),
            order_type=core_type,
            status=OrderStatus.PENDING,
            limit_price=limit_price,
            asset_class=AssetClass.OPTION,
            multiplier=DEFAULT_OPTION_MULTIPLIER,
            option_strategy=option_strategy or ("spread" if is_multi else "single"),
            position_intent=(first.position_intent if not is_multi else None),
            # A MULTI-LEG PARENT HAS NO SINGLE CONTRACT — and says so.
            # Four legs have four contracts, four strikes and (for a condor) both rights;
            # any one of them recorded here would read as "the" contract of the position.
            # The legs below carry the complete identity, one row each.
            contract_symbol=(first.contract_symbol if not is_multi else None),
            option_type=(first.option_type if not is_multi else None),
            strike=(first.strike if not is_multi else None),
            # EXPIRY IS THE EXCEPTION, and it is a fact about the WHOLE structure.
            # The single-expiry guard above has already refused anything spanning two dates,
            # so `expiries` holds at most one element: the structure's expiry, or nothing when
            # no leg records one (the flatten path, where legs are rebuilt from stored rows and
            # may carry expiry=None — UNKNOWN stays NULL here rather than becoming an invented
            # date). The parent IS the row the broker fills, and it was NULL here for every
            # multi-leg, which is why `OptionPortfolioManager._should_close`'s roll-at-DTE
            # branch — `expiry is not None and (expiry - as_of.date()).days <= roll_dte` — had
            # never once fired for a spread or a strangle.
            expiry=(expiries[0] if expiries else None),
            expert_recommendation_id=expert_recommendation_id,
            transaction_id=transaction_id,
        )
        parent_id = add_instance(parent, expunge_after_flush=True)
        parent = get_instance(TradingOrder, parent_id)

        # Create/link a Transaction so OPEN_POSITIONS rules can manage the position.
        if parent.transaction_id is None and hasattr(self, "_create_transaction_for_order"):
            self._create_transaction_for_order(parent)
            update_instance(parent)
            parent = get_instance(TradingOrder, parent_id)

        # ...and record the INTENT on it. Stamped here rather than inside
        # `_create_transaction_for_order` because that factory is shared with equity and,
        # more importantly, because the transaction is often NOT created here at all:
        # `OptionPortfolioManager.rebalance` pre-creates its own so the structure is
        # attributed to the expert, and passes `transaction_id=`. That is the path every
        # short_strangle and put_credit_spread in the GA took.
        self._record_option_intent_on_transaction(parent)

        leg_orders = []
        if is_multi:
            for leg in legs:
                child = TradingOrder(
                    account_id=self.id,
                    symbol=leg.contract_symbol,
                    underlying_symbol=leg.underlying,
                    quantity=quantity * leg.ratio_qty,
                    side=leg.side,
                    order_type=(CoreOrderType.MARKET if order_type == "market" else (
                        CoreOrderType.BUY_LIMIT if leg.side == OrderDirection.BUY else CoreOrderType.SELL_LIMIT)),
                    status=OrderStatus.PENDING,
                    asset_class=AssetClass.OPTION,
                    multiplier=DEFAULT_OPTION_MULTIPLIER,
                    contract_symbol=leg.contract_symbol,
                    option_type=leg.option_type,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    position_intent=leg.position_intent,
                    parent_order_id=parent.id,
                    transaction_id=parent.transaction_id,
                )
                child_id = add_instance(child, expunge_after_flush=True)
                leg_orders.append(get_instance(TradingOrder, child_id))

        try:
            return self._submit_option_order_impl(parent, legs, leg_orders or None)
        except Exception as e:
            logger.error(f"Option order submission failed for {parent.symbol}: {e}", exc_info=True)
            self._unwind_failed_option_submission(parent, leg_orders, e)
            return None

    def _unwind_failed_option_submission(self, parent, leg_orders, error) -> None:
        """Terminalise the rows a FAILED submission left behind — but only if nothing
        reached the broker.

        THE PARENT WAS NEVER THE WHOLE ORDER. A combo persists a parent plus one child per
        leg BEFORE the broker is called, and this except used to mark only the parent ERROR.
        The N children kept ``status=PENDING`` and ``broker_order_id=None``, and nothing in
        the platform can clear that state: ``refresh_orders`` sweeps only rows that HAVE a
        broker id, ``refresh_transactions``' ``never_opened`` cleanup requires EVERY order on
        the transaction to be terminal, ``_fail_unsent_entry`` is equity-only, and
        ``clean_pending_orders`` is a manual UI button.

        The cost is not cosmetic. ``open_option_orders_book_wide`` keeps every non-terminal
        option order whose transaction is not CLOSED/FAILED, so a stranded SHORT PUT child is
        counted as live delivery obligation for ever — measured at $24,000 on a 2-lot 120-strike
        bull put spread the broker had REJECTED — and ``_refuse_if_cannot_take_delivery`` then
        refuses bear put spread, bull put spread, cash-secured put, short straddle, short
        strangle, iron condor, jade lizard and put ratio spread, account-wide across every
        expert, until someone repairs it by hand.

        THE ASYMMETRY THAT MAKES THIS SAFE. This same ``except`` catches two very different
        events. A rejection (approval tier, buying power, a malformed request) or a network
        failure before the request went out leaves NOTHING at the broker, and those rows must
        vanish from the book. A failure while writing the broker's RESPONSE back leaves the
        contracts genuinely LIVE, and terminalising those rows would hide a real short put
        from the assignment gate — the exact inverse of the harm above, and the more expensive
        direction. The two are told apart by ``broker_order_id``, which the live adapter
        persists the instant ``submit_order`` returns and before any response mapping, so the
        window in which an accepted order looks unsent is a single DB write wide.

        Deliberately NOT touched: the Transaction. Terminalising all of its orders re-arms
        ``refresh_transactions``' own ``never_opened`` cleanup, which deletes the stub row
        (cascading to its orders) with an activity-log entry naming every order error. Writing
        a second, competing cleanup here would race it.
        """
        from ba2_common.core.db import update_instance
        from ba2_common.core.types import OrderStatus
        from ba2_common.logger import logger

        why = str(error)[:200]
        parent.comment = f"{(parent.comment or '')} | option submit error: {why}"

        if parent.broker_order_id:
            # ACCEPTED, then something failed writing the response back. The contracts are
            # live: leave every row non-terminal so the position keeps counting against the
            # reserve and the assignment gate, and so refresh_orders can adopt it by its id.
            update_instance(parent)
            logger.error(
                f"Option order {parent.id} ({parent.symbol}) was ACCEPTED by the broker "
                f"(broker_order_id={parent.broker_order_id}) but processing its response "
                f"failed: {why}. The order and its {len(leg_orders or [])} leg(s) are LEFT "
                f"OPEN — the contracts exist at the broker and refresh_orders will reconcile "
                f"them. Do NOT clear these rows by hand without checking the broker first.")
            return

        parent.status = OrderStatus.ERROR
        update_instance(parent)
        for child in (leg_orders or []):
            child.status = OrderStatus.ERROR
            child.comment = f"{(child.comment or '')} | option submit error: {why}"
            update_instance(child)
        if leg_orders:
            logger.error(
                f"Option order {parent.id} ({parent.symbol}) never reached the broker: {why}. "
                f"Its {len(leg_orders)} leg order(s) have been marked ERROR too — left PENDING "
                f"they would be counted as an open position by every option gate for ever.")

    def _refuse_uncovered_covered_call(self, legs: List[OptionLeg], quantity: int,
                                       option_strategy: Optional[str]) -> None:
        """THE BACKSTOP: raise ``ValueError`` when :meth:`check_cover_for_covered_call` refuses.

        The refusal is DECIDED in ``check_cover_for_covered_call``, which returns a
        ``CoverCapacity`` verdict; a caller with a result channel
        (``SellCoveredCallAction`` via ``_refuse_if_cover_is_short``) consults it first
        and turns the same sentence into a failed ``TradeActionResult``, so nothing is
        raised on the path where a refusal is an ordinary outcome.

        This raise stays because not every caller has such a channel.
        ``PremiumSeller.rebalance``, ``OptionPortfolioManager`` and any future direct
        caller reach ``submit_option_order`` with nowhere to put a verdict, and for them
        the alternative to an exception is a silent naked write. It is a backstop and
        not the primary channel: reaching it means a caller did not ask first.

        It raises ``verdict.reason`` VERBATIM — the same text the action records — so
        the two channels can never drift into describing the same refusal differently.
        """
        verdict = self.check_cover_for_covered_call(legs, quantity, option_strategy)
        if not verdict.ok:
            raise ValueError(verdict.reason)

    def check_cover_for_covered_call(self, legs: List[OptionLeg], quantity: int,
                                     option_strategy: Optional[str]) -> "CoverCapacity":
        """Is this ``covered_call`` actually covered by free shares? A verdict, not an exception.

        Returns ``CoverCapacity(ok=True)`` for anything that is covered — and for
        everything this guard deliberately does not police (see below). Otherwise the
        verdict carries the complete refusal sentence and whichever figures were
        measurable. :meth:`_refuse_uncovered_covered_call` is the raising backstop over
        it for callers that have no result channel.

        WHY THIS LIVES AT THE SEAM AND NOT IN THE ACTION. ``SellCoveredCallAction``
        checks cover too, and it stays — but it was the ONLY caller that did.
        ``submit_option_order`` validated a non-empty leg list, a 4-leg ceiling and a
        single expiry, so ``PremiumSeller.rebalance``, ``OptionPortfolioManager`` and any
        future caller could write a naked short call under the ``covered_call`` tag with
        nothing in the repo to notice. A promise enforced at one of five call sites is a
        convention, not an invariant.

        WHY BOTH ACCESSORS. The obvious test — ``held >= contracts * 100`` — is the one
        ``SellCoveredCallAction`` already performs (``floor(held / 100.0)``) and it is
        NOT SUFFICIENT: it consults no short-call book, so a second covered call written
        against the SAME 100 shares passes it, and a third passes it again. The free
        cover is::

            available = held_shares_for_cover(u) - shares_pledged_to_short_calls(u)

        Both are tri-state. ``None`` from EITHER is a refusal, and the two refusals are
        worded differently on purpose: a broken position feed and a broken option book
        send the operator to different systems, and telling them the wrong one costs an
        afternoon. An unknown is never treated as a zero here — that is the whole
        argument of the accessors' own docstrings.

        WHAT IS *NOT* REFUSED, deliberately. Only the ``covered_call`` tag is checked,
        because only that tag promises share cover (see ``COVERED_CALL_STRATEGY``). A
        short strangle is meant to be naked and is rationed by a different rail; a bear
        call spread answers for its short call with a LONG CALL rather than with shares;
        and every close path submits under ``"close"``, so flattening is never blocked by
        a cover reading — refusing there would strand an open position that can no longer
        be exited, which is strictly worse than the entry this refuses. For the same
        reason a structure with no SHORT CALL leg at all (a buy-to-close mistagged
        ``covered_call``) needs no cover and never consults the feeds: it RELEASES cover
        rather than consuming it.

        BUT "NOT REFUSED" IS NOT "OUTSIDE THE COVER LEDGER", and the difference is
        visible to operators. ``shares_pledged_to_short_calls`` counts EVERY open short
        call on the ticker, the short leg of a credit spread included — it reads the
        order book, where that leg is a short call like any other, and a long call
        standing beside it is not cover it can verify (nothing guarantees the long is
        exercised to satisfy an assignment; it can itself have been sold). So a bear call
        spread on AAPL DOES consume 100 shares of AAPL cover here, and a genuinely
        covered call on AAPL can be refused because of it. That is deliberate and
        fail-safe — a short 160C really can have 100 shares called away — but it is a
        REAL constraint, not a technicality: a covered-call sleeve and a credit-spread
        sleeve on the same ticker will not both write. The shortfall message names the
        pledge rather than telling the operator to buy shares they may already hold.

        LONG CALLS ARE NOT NETTED against the requirement. A covered call is by
        definition one short call against shares, not a spread; if a long call is present
        the tag is wrong, and over-requiring cover is the direction that fails safe.
        """
        from ba2_common.core.types import OrderDirection

        if (option_strategy or "").strip().lower() != COVERED_CALL_STRATEGY:
            return CoverCapacity(True)

        contracts = self._readable_positive_number(quantity)
        if contracts is None:
            return CoverCapacity(False, (
                f"{COVER_REFUSAL}: this covered_call has no usable size "
                f"(quantity={quantity!r}), so how many shares it could have called away "
                f"cannot be worked out — and an obligation of unknown size must not be "
                f"written. No order, leg or transaction has been created."))

        # Grouped by underlying rather than summed flat: a covered call is one ticker, but
        # a stray leg on another name must not be covered by the first name's shares.
        required: dict = {}
        for leg in legs:
            if getattr(leg, "side", None) != OrderDirection.SELL:
                continue           # a LONG leg pledges nothing; a buy-to-close releases
            contract = getattr(leg, "contract_symbol", None) or "?"
            right = getattr(leg, "option_type", None)
            if right is None:
                return CoverCapacity(False, (
                    f"{COVER_REFUSAL}: leg {contract} of this covered_call is a SHORT "
                    f"option with no option type recorded, so whether it is the CALL that "
                    f"has to be covered is unknown — and unknown must not resolve to 'not "
                    f"a call'. No order, leg or transaction has been created."))
            if right != OptionRight.CALL:
                continue           # a short put obliges CASH: the capacity gate's question
            underlying = (getattr(leg, "underlying", None) or "").strip().upper()
            if not underlying:
                return CoverCapacity(False, (
                    f"{COVER_REFUSAL}: leg {contract} of this covered_call is a SHORT CALL "
                    f"with no underlying recorded, so there is no ticker whose shares can "
                    f"be counted as its cover. No order, leg or transaction has been "
                    f"created."))
            ratio = self._readable_positive_number(getattr(leg, "ratio_qty", 1))
            if ratio is None:
                return CoverCapacity(False, (
                    f"{COVER_REFUSAL}: leg {contract} of this covered_call has an "
                    f"unusable ratio_qty ({getattr(leg, 'ratio_qty', None)!r}), and it is "
                    f"what sizes the leg (quantity * ratio_qty), so how many {underlying} "
                    f"shares it can call away is unknown. No order, leg or transaction "
                    f"has been created."), underlying=underlying)
            # THE REQUIREMENT AND THE ROWS ARE THE SAME NUMBER — see the block comment on
            # DEFAULT_OPTION_MULTIPLIER. Validating against a multiplier the write is
            # incapable of persisting is how a gate approves one position and creates
            # another: a leg publishing 10 needed 30 shares for 3 contracts and was
            # ADMITTED, then the parent and leg rows were stamped 100 and a re-read
            # through shares_pledged_to_short_calls reported 300 pledged.
            raw_multiplier = getattr(leg, "multiplier", _MULTIPLIER_ABSENT)
            if raw_multiplier is not _MULTIPLIER_ABSENT:
                published = self._readable_positive_number(raw_multiplier)
                if published is None:
                    return CoverCapacity(False, (
                        f"{COVER_REFUSAL}: leg {contract} of this covered_call reports "
                        f"multiplier {raw_multiplier!r}, so how many {underlying} shares "
                        f"one contract can call away is unknown — and it must NOT be "
                        f"assumed to be {DEFAULT_OPTION_MULTIPLIER}, because an ADJUSTED "
                        f"contract (post-split, post-merger) delivers a different number "
                        f"and guessing under-states exactly the cover this write needs. "
                        f"No order, leg or transaction has been created."),
                        underlying=underlying)
                if published != float(DEFAULT_OPTION_MULTIPLIER):
                    # REFUSED rather than silently honoured OR silently ignored. Honouring
                    # it validates a position the write cannot record; ignoring it
                    # validates the right number for the wrong reason and drops an
                    # ADJUSTED contract's delivery size on the floor. Both are OPT-L7
                    # guesses. The platform simply cannot express this position yet:
                    # OptionLeg carries no multiplier field, and every option row written
                    # below is stamped DEFAULT_OPTION_MULTIPLIER unconditionally. Teaching
                    # it to must start at those two writes and at the pledge accessor that
                    # reads them back — this refusal is the reminder, exactly as the
                    # single-expiry guard is the reminder for Transaction.expiry.
                    return CoverCapacity(False, (
                        f"{COVER_REFUSAL}: leg {contract} of this covered_call publishes "
                        f"multiplier {raw_multiplier!r}, but every option row written here "
                        f"is recorded with multiplier {DEFAULT_OPTION_MULTIPLIER} — so the "
                        f"{underlying} cover this write would be checked against and the "
                        f"cover the resulting book would report are two different numbers, "
                        f"and the smaller one is the check. An adjusted contract cannot be "
                        f"recorded honestly until OptionLeg carries a multiplier and both "
                        f"row writes persist it. No order, leg or transaction has been "
                        f"created."), underlying=underlying)
            multiplier = float(DEFAULT_OPTION_MULTIPLIER)
            required[underlying] = (required.get(underlying, 0.0)
                                    + contracts * ratio * multiplier)

        for underlying in sorted(required):
            # Rounded UP, like the pledge itself: under-stating the requirement by one
            # share is the direction that leaves a contract uncovered. ``round(_, 6)``
            # first so float-addition dust cannot turn 300.0000000001 into 301; the
            # ``ceil`` is what a fractional ``ratio_qty`` lands on, and dropping it for a
            # plain ``int()`` truncation is pinned by
            # ``test_the_requirement_is_rounded_UP_never_truncated``.
            need = int(math.ceil(round(required[underlying], 6)))
            if need <= 0:
                continue
            held = self.held_shares_for_cover(underlying)
            pledged = self.shares_pledged_to_short_calls(underlying)
            if held is None:
                return CoverCapacity(False, (
                    f"{COVER_REFUSAL}: how many {underlying} shares this account holds "
                    f"could not be measured — held_shares_for_cover() returned UNKNOWN, "
                    f"i.e. the POSITION FEED did not answer (the logged error above names "
                    f"the fetch or the row that failed). Whether this covered_call would "
                    f"be covered is therefore unknown, and writing a short call on an "
                    f"unknown is precisely how one goes naked. No order, leg or "
                    f"transaction has been created. This is NOT a shortfall: buying more "
                    f"shares will not clear it, repairing the feed will."),
                    underlying=underlying, required=need, held=None, pledged=pledged)
            if pledged is None:
                return CoverCapacity(False, (
                    f"{COVER_REFUSAL}: how many {underlying} shares are already pledged "
                    f"as cover for open short calls could not be measured — "
                    f"shares_pledged_to_short_calls() returned UNKNOWN, i.e. the OPTION "
                    f"BOOK did not answer (the logged error above names the unreadable "
                    f"row). The {held} shares held may already be spoken for and there is "
                    f"no way to find out, so this covered_call could be the second call "
                    f"written against the same lot. No order, leg or transaction has been "
                    f"created. Repair the option order book and retry."),
                    underlying=underlying, required=need, held=held, pledged=None)
            free = held - pledged
            if free < need:
                return CoverCapacity(False, (
                    f"{COVER_REFUSAL}: this covered_call writes {contracts:g} contract(s) "
                    f"on {underlying} and needs {need} shares of cover, but only {free} "
                    f"are free (the account holds {held}, with {pledged} already pledged "
                    f"to open short calls) — short by {need - free} share(s). No order, "
                    f"leg or transaction has been created. Buy the missing shares, write "
                    f"fewer contracts, or close whichever open short call is holding the "
                    f"pledge — INCLUDING the short leg of a credit spread on "
                    f"{underlying}, which is counted here even though its own cover is a "
                    f"long call; adding margin does nothing for this one."),
                    underlying=underlying, required=need, held=held, pledged=pledged)
        return CoverCapacity(True)

    #: Strategy tags that describe what is being DONE, not what the position IS. An order
    #: carrying one of these must never define the transaction's intent: both close paths
    #: (``TradeActions`` and ``OptionPortfolioManager._close_structure``) submit offsetting
    #: legs tagged "close" on the SAME transaction, so letting one through would relabel
    #: every flattened structure in the book and make the strategy family unrecoverable.
    NON_INTENT_STRATEGIES = ("close",)

    def _record_option_intent_on_transaction(self, parent) -> None:
        """Stamp ``asset_class`` / ``option_strategy`` / ``expiry`` on the parent's Transaction.

        The order rows say which CONTRACTS executed; the transaction says what was MEANT — "a
        bull call spread on ACN expiring 2026-08-21". Before this, the only tell that a
        transaction held an option was ``multiplier == 100``, a coincidence of P&L arithmetic
        standing in for a fact.

        ``symbol`` is deliberately not touched: it is the UNDERLYING ticker and must stay that
        way. ``JobManager._execute_open_positions_analysis`` selects ``distinct
        Transaction.symbol`` and submits one market analysis per value, so an OCC string there
        would be analysed as a ticker and would break the wheel's second leg along with every
        rating, price and screener condition.

        FILL-ONLY for the two nullable fields: a value already recorded is the OPENING intent
        and a later order on the same transaction (a close, an add) does not redefine it.
        ``asset_class`` is set unconditionally because it is not an opinion — every order
        reaching this method is an option order, and the column is NOT NULL with an EQUITY
        default, so "still EQUITY" cannot be distinguished from "deliberately EQUITY".

        A failure here must never cost the order. The broker call has not happened yet, but the
        parent and its legs are already written and the caller is about to submit; an
        unstampable transaction is a reporting gap, whereas raising would turn it into a
        position that exists at the broker with no order rows the platform will act on.
        """
        from ba2_common.core.db import InstanceNotFound, get_instance, update_instance
        from ba2_common.core.failure_modes import absorb_if_benign
        from ba2_common.core.models import Transaction
        from ba2_common.core.types import AssetClass
        from ba2_common.logger import logger

        if parent.transaction_id is None:
            return
        try:
            txn = get_instance(Transaction, parent.transaction_id)
        except Exception as e:
            # A transaction id that resolves to nothing is the only failure this site
            # legitimately expects (a stale id from a caller); anything else is a defect and
            # propagates, per the house `absorb_if_benign` discipline.
            absorb_if_benign(e, InstanceNotFound)
            logger.error(f"Option order {parent.id} points at transaction "
                         f"{parent.transaction_id}, which could not be read — the position's "
                         f"intent (asset class/strategy/expiry) is NOT recorded: {e}",
                         exc_info=True)
            return

        strategy = parent.option_strategy
        changed = False
        if txn.asset_class != AssetClass.OPTION:
            txn.asset_class = AssetClass.OPTION
            changed = True
        if (txn.option_strategy is None and strategy
                and strategy not in self.NON_INTENT_STRATEGIES):
            txn.option_strategy = strategy
            changed = True
        if txn.expiry is None and parent.expiry is not None:
            txn.expiry = parent.expiry
            changed = True
        if changed:
            update_instance(txn)

    def open_option_transaction_id_for_contract(self, contract_symbol: str) -> Optional[int]:
        """The id of the OPEN transaction still HOLDING ``contract_symbol``, or ``None``.

        A CLOSE MUST RIDE THE TRANSACTION IT IS CLOSING. ``submit_option_order`` creates a
        brand-new Transaction whenever ``transaction_id`` is omitted
        (``_create_transaction_for_order`` constructs one unconditionally — it never looks
        for an existing open position), so a closing leg submitted without the id books the
        exit as a fresh OPENING position of the opposite side. The original then never
        reaches CLOSED, the exit condition that decided to close it is still true on the
        next pass and submits again — forever — and neither the buying-power reserve nor
        the short-put assignment exposure is ever released, because
        ``open_option_orders_book_wide`` keeps every order whose transaction is not
        CLOSED/FAILED and FILLED is not a terminal transaction state.

        This lives at the SEAM rather than at the call sites for the reason the rest of
        this file gives: ``close_option_position`` is reachable from ``CloseOptionAction``,
        from operator scripts and from any future caller, and a link maintained at one of
        those is a convention rather than an invariant.

        MATCHING. Both shapes are covered: the single-leg entry, where the transaction's
        own option order IS the contract, and the multi-leg entry, where the contract is
        carried by a leg CHILD of the parent. Both rows carry ``transaction_id``, so one
        scan over this account's option orders answers it.

        NET, NOT PRESENCE. A contract whose buys and sells already offset is FLAT, and
        re-attaching a second close to it would reduce a position that no longer exists.
        Only a transaction with a non-zero net for the contract is returned. Ties break on
        the LOWEST transaction id — FIFO, the convention used everywhere else here.

        UNKNOWN IS NOT "NO TRANSACTION": an unreadable book returns ``None`` exactly as a
        genuinely absent one does, so the caller must treat ``None`` as "could not link"
        and say so loudly rather than pretending it linked. Only the benign
        world-was-uncooperative errors are absorbed (see ``_cover_benign_errors``); a
        ``ProgrammingError`` still propagates, because a lookup that answers "not found"
        forever is a lookup that has quietly stopped working.
        """
        from ba2_common.core.trade_store import orders_where, transactions_where
        from ba2_common.core.types import (
            AssetClass, OrderDirection, OrderStatus, TransactionStatus)
        from ba2_common.logger import logger

        if not contract_symbol:
            return None
        try:
            executed = OrderStatus.get_executed_statuses()
            rows = [o for o in orders_where(account_id=self.id)
                    if o.asset_class == AssetClass.OPTION
                    and o.contract_symbol == contract_symbol
                    and o.transaction_id is not None
                    and (o.status in executed or self._traded_something(o))]
            if not rows:
                return None
            live_ids = {t.id for t in transactions_where(
                not_statuses=(TransactionStatus.CLOSED, TransactionStatus.FAILED))}
            net: Dict[int, float] = {}
            for o in rows:
                if o.transaction_id not in live_ids:
                    continue
                qty = self._readable_number(o.filled_qty)
                if qty is None:
                    qty = self._readable_number(o.quantity)
                if qty is None:
                    continue
                signed = qty if o.side == OrderDirection.BUY else -qty
                net[o.transaction_id] = net.get(o.transaction_id, 0.0) + signed
            holding = sorted(tid for tid, n in net.items() if abs(n) > 1e-9)
            if not holding:
                return None
            if len(holding) > 1:
                logger.warning(
                    f"{len(holding)} open transactions hold {contract_symbol} "
                    f"({holding}) — attaching the close to the oldest, {holding[0]}")
            return holding[0]
        except Exception as e:
            if not isinstance(e, self._cover_benign_errors()):
                raise
            logger.error(
                f"Could not read the option book to find the open transaction holding "
                f"{contract_symbol}: {e}", exc_info=True)
            return None

    @abstractmethod
    def close_option_position(self, position: OptionPosition,
                              order_type: str = "limit",
                              limit_price: Optional[float] = None,
                              transaction_id: Optional[int] = None) -> Any:
        """Submit a closing order for a held option position (opposite intent).

        ``transaction_id`` is the OPEN position's transaction — the one the close must
        reduce. An implementation not given one must resolve it itself via
        :meth:`open_option_transaction_id_for_contract`; submitting without it books the
        exit as a NEW opening position (see that method for the full consequence).
        """
        ...

    # --- IV rank (self-computed from stored ATM-IV history) ----------------

    #: Trailing window for the IV percentile, in CALENDAR days.
    #:
    #: 365, not 252. "IV rank" universally means the one-year percentile, and the live
    #: rules are named for it ("Rich IV", "Cheap IV"). Both series are sampled on a
    #: SESSION grid — the live recorder is a Mon-Fri cron, the backtest grid is the NYSE
    #: calendar — so a window expressed in calendar days must be ~365 to hold the ~252
    #: sessions the name promises. The previous 252 was 252 *calendar* days ≈ 173
    #: sessions ≈ 8.5 months: a materially shorter, more reactive statistic than every
    #: rule name, docstring and operator threshold assumed. One constant so live and
    #: backtest cannot drift apart on it.
    IV_RANK_LOOKBACK_DAYS = 365

    #: Bounds on a value that can honestly be called an annualised ATM implied
    #: volatility (as a FRACTION: 0.30 == 30%). Outside them the number is a data
    #: error, and this codebase's rule is that a data error is UNKNOWN — never 0.0.
    #:
    #: Lower bound 1%. The quietest ATM IV ever printed on a listed US name is ~4-5%
    #: (SPY, mid-2017); a single name has never traded near 1%. The floor is set at 1%
    #: rather than "> 0" deliberately: the failure mode is an un-populated float field,
    #: and those arrive as 0.0, 1e-9 and 0.0001 as readily as exact zero. A bare
    #: ``iv > 0`` test lets every one of those through.
    #:
    #: Upper bound 500%. A biotech into a binary readout or a name in a squeeze prints
    #: 200-400%; above 500% you are looking at a mis-scaled field (IV quoted in PERCENT,
    #: so 30.0 means 3000%) or a one-tick-wide penny-option mid inverted into nonsense.
    #:
    #: Why this matters more than a usual sanity check: 0.0 ranks strictly below every
    #: stored sample, so it scores rank 0.0 — and SIX of the nine iv_rank-gated rules on
    #: the live book are ``iv_rank <= 35/40/50``. A zero-filled feed field would open
    #: every one of them, submitting real option orders, precisely when the feed is
    #: broken. NaN is worse still: ``v < nan`` is False for all v, so NaN also scores a
    #: clean-looking 0.0 with no error anywhere.
    MIN_PLAUSIBLE_ATM_IV = 0.01
    MAX_PLAUSIBLE_ATM_IV = 5.0

    @classmethod
    def plausible_atm_iv(cls, iv) -> Optional[float]:
        """``float(iv)`` when it can be a real annualised ATM IV, else None.

        THE single definition, shared by the live recorder, the live rank, the backtest
        rank and PremiumSeller's gate, so "what counts as a possible IV" cannot fork the
        way the two IV-rank implementations did. Returning None (not a clamped value, not
        0.0) is the whole point: every caller already treats None as "unknown" and fails
        closed, and clamping would manufacture exactly the fabricated sample the recorder
        refuses to write.

        Coerces via ``float()`` rather than ``isinstance(iv, (int, float))`` so a numpy
        scalar off a parquet/pandas path is a NUMBER, not silently "unknown"
        (``np.float32`` is not a ``float`` subclass, though ``np.float64`` is — a trap
        worth not stepping in). ``bool`` is excluded because it IS an int subclass and
        ``True`` would otherwise become a perfectly plausible 100% IV; ``str``/``bytes``
        because ``float("0.3")`` succeeds and a stringly-typed feed field is a bug to
        surface, not to parse.
        """
        if iv is None or isinstance(iv, (bool, str, bytes)):
            return None
        try:
            value = float(iv)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):          # NaN and ±inf
            return None
        if value < cls.MIN_PLAUSIBLE_ATM_IV or value > cls.MAX_PLAUSIBLE_ATM_IV:
            return None
        return value

    @classmethod
    def _iv_rank_from_series(cls, series, current, min_samples: int = 20):
        """Percentile (0-100) of `current` against `series`, or None.

        Entries that are not a plausible ATM IV — None, NaN, 0.0, 800% — are DROPPED
        from the series, and an implausible `current` returns None outright. Filtering
        here as well as at ``record_atm_iv`` is not belt-and-braces: the backtest override
        builds its series straight from the options provider and never writes a row, so
        this is that path's ONLY boundary. Dropping rather than clamping keeps the
        distinction the whole feature rests on — an unusable sample is absent, so it can
        neither be counted as "below current" nor pad the denominator.

        Returns None when `current` is unusable or fewer than `min_samples` USABLE
        samples exist. Counts strictly below `current`.
        """
        vals = [f for f in (cls.plausible_atm_iv(v) for v in series) if f is not None]
        current = cls.plausible_atm_iv(current)
        if current is None or len(vals) < min_samples:
            return None
        below = sum(1 for v in vals if v < current)
        return round(below / len(vals) * 100, 2)

    @staticmethod
    def _utc_day_start():
        """Midnight UTC today — the dedup boundary for the daily ATM-IV sample."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    def _todays_iv_snapshot_id(self, underlying: str) -> Optional[int]:
        """Row id of today's (UTC) ATM-IV sample for `underlying`, or None."""
        from sqlmodel import select
        from ba2_common.core.db import get_db
        from ba2_common.core.models import OptionIVSnapshot
        with get_db() as session:
            return session.exec(
                select(OptionIVSnapshot.id).where(
                    OptionIVSnapshot.account_id == self.id,
                    OptionIVSnapshot.underlying == underlying,
                    OptionIVSnapshot.recorded_at >= self._utc_day_start(),
                ).order_by(OptionIVSnapshot.id)
            ).first()

    def record_atm_iv(self, underlying: str, iv: Optional[float] = None) -> Optional[int]:
        """Persist ONE ATM-IV sample per (account, underlying) per UTC calendar day.

        Returns the row id of today's sample (freshly written or pre-existing), or
        None when no honest ATM IV is available.

        DAILY IDEMPOTENCY IS PART OF THE CONTRACT, NOT THE CALLER'S JOB. The series
        this feeds is read by ``get_iv_rank`` as an unweighted percentile over a
        252-day window, so N samples on one day give that day N/252 of the vote. The
        most natural-looking hook in the codebase (``TradeManager.refresh_accounts``)
        runs every 5 minutes; wiring the recorder there without this guard would put
        ~288 rows/day into the window and silently convert a "1-year IV percentile"
        into a "last-few-days IV percentile" — a live trading gate reading a
        differently-defined statistic than its name and its rules assume. Enforcing it
        here means no future caller can reintroduce that. A matching
        ``UNIQUE(account_id, underlying, date(recorded_at))`` index backs it at the DB
        level (alembic ``a3f1c07d9e21``); this check also spares the broker call.

        The guard runs BEFORE the IV fetch on purpose: ``get_atm_implied_volatility``
        is a full option-chain request per symbol, so a re-run of the daily job (or a
        manual trigger) costs nothing.

        NO FABRICATION. When the chain carries no IV the sample is simply not written
        and the omission is logged. An invented number here would silently arm nine
        live option rules with a statistic derived from nothing; a missing row leaves
        ``IVRankCondition`` failing closed, which is the safe direction.
        """
        from ba2_common.core.db import add_instance
        from ba2_common.core.models import OptionIVSnapshot
        from ba2_common.logger import logger

        existing = self._todays_iv_snapshot_id(underlying)
        if existing is not None:
            logger.debug(
                f"ATM-IV sample already recorded today for {underlying} on account "
                f"{self.id} (row {existing}); skipping")
            return existing

        raw = self.get_atm_implied_volatility(underlying) if iv is None else iv
        iv = self.plausible_atm_iv(raw)
        if iv is None:
            logger.warning(
                f"No usable ATM implied volatility for {underlying} on account "
                f"{self.id} (feed returned {raw!r}; a sample must be a finite fraction "
                f"in [{self.MIN_PLAUSIBLE_ATM_IV}, {self.MAX_PLAUSIBLE_ATM_IV}]) — NO IV "
                f"sample recorded. Any iv_rank-gated rule for this underlying stays "
                f"inert (IVRankCondition fails closed).")
            return None
        return add_instance(OptionIVSnapshot(account_id=self.id, underlying=underlying, atm_iv=iv))

    def _iv_series(self, underlying: str, lookback_days: Optional[int] = None):
        """HISTORY: stored ATM-IV samples strictly BEFORE today (UTC), in window.

        ``lookback_days`` is CALENDAR days (defaulting to ``IV_RANK_LOOKBACK_DAYS``),
        because that is what the stored ``recorded_at`` supports; the recorder's Mon-Fri
        cron is what turns it into ~252 SESSIONS. Spelling that out because "252" read
        as trading days for as long as the default was 252 calendar days, and the two
        differ by nearly three months.

        Today's own sample is excluded. ``_iv_rank_from_series`` counts strictly ``<``,
        so a sample equal to (or, after the daily recorder ran, nearly equal to)
        ``current`` can never count as below it. Including it would therefore bias the
        rank down by 100/N — 20 whole points at the production min_samples of 5 — and,
        worse, only for evaluations that happen AFTER the recorder's 16:30 ET run: the
        same rule on the same tape would score differently in the morning and in the
        evening. Excluding it makes the series mean one thing ("the trailing days") at
        every hour, and makes the live definition identical to the backtest override's.

        Not collapsed per day: ``record_atm_iv`` plus the unique index are the single
        enforcement point for one-sample-per-day. Collapsing here as well would mask a
        writer that broke that contract instead of letting the duplicate show up.
        """
        from datetime import datetime, timezone, timedelta
        from sqlmodel import select
        from ba2_common.core.db import get_db
        from ba2_common.core.models import OptionIVSnapshot
        if lookback_days is None:
            lookback_days = self.IV_RANK_LOOKBACK_DAYS
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        with get_db() as session:
            rows = session.exec(
                select(OptionIVSnapshot).where(
                    OptionIVSnapshot.account_id == self.id,
                    OptionIVSnapshot.underlying == underlying,
                    OptionIVSnapshot.recorded_at >= cutoff,
                    OptionIVSnapshot.recorded_at < self._utc_day_start(),
                )
            ).all()
            return [r.atm_iv for r in rows]   # read while session is open

    def iv_sample_count(self, underlying: str, lookback_days: Optional[int] = None) -> int:
        """How many trailing ATM-IV samples ``get_iv_rank`` would actually use.

        Exposed so readiness can be REPORTED rather than inferred: "no rank" and
        "rank of 0" are different facts, and an operator needs to see which
        underlyings are still short of ``min_samples`` before their rules wake up.
        Counts exactly what the rank counts (today's sample excluded), so the report
        cannot say "armed" a day before the gate can actually open.

        ROWS, not usable samples: this deliberately does NOT apply the plausibility
        filter. The readiness report answers "is the recorder keeping up?", and a row the
        rank later discards is still evidence the recorder ran. Where the two numbers
        disagree, the rank is the stricter one and the gate stays shut — the safe way
        round.
        """
        return len(self._iv_series(underlying, lookback_days))

    def get_iv_rank(self, underlying: str, lookback_days: Optional[int] = None,
                    min_samples: int = 20,
                    current: Optional[float] = None) -> Optional[float]:
        """IV percentile (0-100) of the CURRENT ATM IV against the stored trailing
        window (today's own sample excluded — see ``_iv_series``), or None if fewer
        than `min_samples` historical points exist.

        None means "not enough data" and is deliberately DISTINCT from 0.0 ("cheapest
        IV in the window"): ``IVRankCondition`` turns None into a closed gate, whereas
        0.0 would satisfy every "IV is low" rule.

        `current` may be supplied by a caller that already holds the ATM IV; otherwise
        it is fetched. Note this is a full option-chain request per call, and
        ``iv_rank`` is ``trigger_0`` on some live rules (nothing short-circuits ahead of
        it), so a 30-symbol enter-market pass costs 30 chain fetches per expert. During
        market hours that fetch is unavoidable — the day's sample has not been recorded
        yet and a stale IV would be the wrong number — so the parameter exists for
        callers (the backtest override; any future per-pass memo) that legitimately have
        it in hand.
        """
        series = self._iv_series(underlying, lookback_days)
        if current is None:
            current = self.get_atm_implied_volatility(underlying)
        return self._iv_rank_from_series(series, current, min_samples)

    # --- Cash / buying-power reserve (short-premium defense-in-depth) -------
    #: Reg-T / CBOE naked-option initial-margin fraction of the underlying notional.
    #: A NAKED short option is NOT cash-secured (only an assigned cash-secured PUT is);
    #: brokers margin it at ~20% of notional less OTM amount, floored at ~10%. Reserving
    #: the FULL strike*100 (cash-secured proxy) made naked structures (short straddle/
    #: strangle, jade lizard, put ratio spread) impossible to size on a realistic account
    #: ($10k can't reserve $22k for one AAPL contract), so they never opened. The margin
    #: model below mirrors how a broker actually reserves a naked short.
    NAKED_MARGIN_FRACTION = 0.20
    NAKED_MARGIN_FLOOR_FRACTION = 0.10

    @classmethod
    def naked_margin_per_contract(cls, strike: float, *, option_type: OptionRight,
                                  spot: float | None = None) -> float:
        """Reg-T naked single-option initial margin for ONE contract (x100 multiplier).

        Reg-T / CBOE naked-short initial margin is::

            premium + max(20% * underlying - OTM_amount, 10% * underlying)

        where the OTM amount is 0 for an ITM option (NEVER negative — an ITM short
        carries the full 20%-of-notional requirement, there is no OTM deduction):
          * CALL: OTM_amount = max(strike - spot, 0)
          * PUT:  OTM_amount = max(spot - strike, 0)

        The PREMIUM term is NOT added here: this returns only the per-share bracket
        x100, and the collected premium already sits in the account's cash where it
        offsets the requirement (callers reserve/margin the bracket only). ``spot``
        is used when known; without it, falls back to ``0.20*strike*100`` (OTM term
        dropped) — still ~5x cheaper than the old full strike*100 cash proxy.

        An UNUSABLE ``strike`` raises. It used to answer ``0.0``, which is a margin
        requirement of nothing on a NAKED short — the most fail-open answer in this
        file, and one that ``check_option_buying_power(0.0)`` passes unconditionally.
        ``<= 0`` counts as unusable alongside ``None`` because no listed equity option
        has a strike of zero: it is an unpopulated field, exactly as
        ``option_lifecycle.put_assignment_cost`` already treats it."""
        if strike is None or strike <= 0:
            raise ValueError(
                f"naked_margin_per_contract(strike={strike!r}): a naked short's margin "
                f"cannot be computed from a missing or non-positive strike, and it must "
                f"not be reported as 0.0 — a zero requirement passes every "
                f"buying-power gate on a position with unbounded risk.")
        if spot is None or spot <= 0:
            return cls.NAKED_MARGIN_FRACTION * strike * 100.0
        if option_type == OptionRight.CALL:
            otm = max(float(strike) - float(spot), 0.0)
        else:
            otm = max(float(spot) - float(strike), 0.0)
        primary = cls.NAKED_MARGIN_FRACTION * spot - otm
        floor = cls.NAKED_MARGIN_FLOOR_FRACTION * spot
        return max(primary, floor) * 100.0

    #: Strategies whose reserve is GENUINELY zero, named one by one.
    #:
    #: Every one of these is long/debit (the maximum loss is the premium already paid
    #: at entry, so there is nothing further to set aside) except ``covered_call``,
    #: whose short call is covered by the 100 shares rather than by cash. This list is
    #: the *only* way to get a zero out of ``option_reserve_required``: a strategy that
    #: is merely unrecognised raises instead. The distinction is the whole point — an
    #: unknown capital requirement and a zero capital requirement are not the same
    #: fact, and collapsing them let every unrecognised structure pass every
    #: buying-power gate.
    ZERO_RESERVE_STRATEGIES = frozenset({
        "long_call", "long_put", "bull_call_spread", "bear_put_spread",
        "straddle", "strangle", "covered_call", "protective_put",
    })

    #: Strategies ``option_reserve_required`` prices with a branch of its own. Kept in
    #: lockstep with those branches by ``test_the_two_strategy_lists_match_the_branches``.
    RESERVING_STRATEGIES = frozenset({
        "cash_secured_put",
        "bear_call_spread", "bull_put_spread", "credit_spread",
        "short_straddle", "short_strangle", "naked_put",
        "put_ratio_spread",
        "jade_lizard",
        "iron_condor", "call_butterfly", "debit_spread",
    })

    @classmethod
    def option_reserve_required(cls, strategy: str, quantity: int, *, strike: float | None = None,
                               spread_width: float | None = None, net_credit: float | None = None,
                               spot: float | None = None, option_type: OptionRight | None = None) -> float:
        """Cash/BP that a short-premium strategy must reserve. 0 for long/debit strategies.

        ``option_type`` is REQUIRED for the single-sided naked strategies
        (``short_strangle``/``naked_put``/``put_ratio_spread``) — the Reg-T OTM amount
        is direction-aware, so a missing right would silently misstate the reserve.
        ``short_straddle`` needs none: it shorts BOTH rights at the same strike and
        Reg-T margins a straddle at the GREATER of the two legs, so the worst case
        over both rights is reserved.

        An **unrecognised** ``strategy`` raises ``ValueError``. It used to fall off the
        end of the branch chain into a bare ``return 0.0``, which meant that the one
        structure nobody had priced was also the one structure the buying-power gate
        could never refuse: ``check_option_buying_power(0.0)`` always passes. A capital
        requirement we do not know is not a capital requirement of nothing. The
        strategies that genuinely need no reserve are enumerated in
        ``ZERO_RESERVE_STRATEGIES`` and answer 0.0 by name.

        A **known** strategy whose SIZING INPUT is missing raises for the same reason.
        Six branches below used to answer ``0.0`` when the strike / spread width / net
        credit they price with was ``None`` — the identical fail-open one layer in, and
        arguably worse, because the strategy was recognised and looked priced. A
        ``cash_secured_put`` that arrived without a strike reserved nothing and was
        therefore affordable at any size on any account. A non-positive STRIKE counts as
        missing too (no listed equity option has one). The only zeros left are the two
        legitimate ones — ``quantity <= 0``, and ``ZERO_RESERVE_STRATEGIES`` by name —
        plus a genuinely COMPUTED zero (a credit at least as wide as the spread has no
        max loss to set aside), which is arithmetic rather than an absent field."""
        def _require(**inputs):
            """Refuse a priced branch whose sizing input is absent (or, for a strike,
            non-positive). Named per field so the error says which one to go and fix."""
            for field, value in inputs.items():
                if value is None:
                    raise ValueError(
                        f"option_reserve_required({strategy!r}): {field} is missing, so "
                        f"the capital this structure must set aside is UNKNOWN — and an "
                        f"unknown requirement must not be reported as zero (a zero "
                        f"reserve passes every buying-power gate).")
                if field == "strike" and value <= 0:
                    raise ValueError(
                        f"option_reserve_required({strategy!r}): strike is {value!r}. No "
                        f"listed equity option has a non-positive strike, so this is an "
                        f"unpopulated field, and pricing it would reserve nothing.")
        # Validated BEFORE the quantity short-circuit: an unrecognised strategy is a
        # code defect, and a defect does not stop being one at size zero.
        if (strategy not in cls.ZERO_RESERVE_STRATEGIES
                and strategy not in cls.RESERVING_STRATEGIES):
            raise ValueError(
                f"option_reserve_required({strategy!r}): unknown option strategy — its "
                f"capital requirement is undefined, and an undefined requirement must "
                f"not be reported as zero (that would pass every buying-power gate). "
                f"Add a branch that prices it, or name it in ZERO_RESERVE_STRATEGIES "
                f"if it genuinely reserves nothing.")
        if quantity <= 0:
            return 0.0
        if strategy in cls.ZERO_RESERVE_STRATEGIES:
            return 0.0
        if strategy == "cash_secured_put":
            # A CSP is fully cash-secured by definition (the cash to buy the assigned
            # shares is set aside): reserve the full assignment cost.
            _require(strike=strike)
            return strike * 100.0 * quantity
        if strategy in ("bear_call_spread", "bull_put_spread", "credit_spread"):
            # Defined-risk credit VERTICALS: the broker margins them at their textbook max
            # loss (the two-leg case Alpaca's MLEG engine does recognise — unlike the
            # jade_lizard / put_ratio_spread shapes below). Identical arithmetic on either
            # right: the call spread's risk is (higher - lower) above the short call, the
            # put spread's is (higher - lower) below the short put.
            _require(spread_width=spread_width, net_credit=net_credit)
            max_loss = (spread_width - net_credit)
            return max(0.0, max_loss) * 100.0 * quantity
        if strategy in ("short_straddle", "short_strangle", "naked_put"):
            # NAKED short premium: reserve the Reg-T naked-option margin, not full cash.
            _require(strike=strike)
            if strategy == "short_straddle":
                # Both a short call AND a short put at the SAME strike: reserve the
                # worst-case leg (only one side can finish ITM; Reg-T charges the greater).
                return max(
                    cls.naked_margin_per_contract(strike, option_type=OptionRight.CALL, spot=spot),
                    cls.naked_margin_per_contract(strike, option_type=OptionRight.PUT, spot=spot),
                ) * quantity
            if option_type is None:
                raise ValueError(
                    f"option_reserve_required({strategy!r}) requires option_type — the Reg-T "
                    "OTM amount is direction-aware (call: strike-spot, put: spot-strike).")
            return cls.naked_margin_per_contract(strike, option_type=option_type, spot=spot) * quantity
        if strategy == "put_ratio_spread":
            # Empirically confirmed against a real Alpaca paper submission (2026-07-24, same
            # session as the jade_lizard fix): a 1-long/2-short put ratio spread is NOT given
            # Reg-T naked-margin netting for its net-1-naked-short leg, nor any credit for the
            # long leg's partial protection -- Alpaca's MLEG engine margins the WHOLE position as
            # if it were a plain naked short put at the SHORT strike (cash-secured-style full
            # notional), netted by the total credit collected. Verified EXACT to the dollar: a
            # real 30-contract order (short strike 290, net_credit 3.65) reported Alpaca
            # cost_basis=$859,050 == 30 * (290*100 - 3.65*100) = 30 * 28,635. The previous formula
            # (naked_margin_per_contract on the short strike alone) reserved only ~$3,289/contract
            # for the same order -- an ~8.7x underestimate.
            _require(strike=strike)
            credit = net_credit if net_credit is not None else 0.0
            per_contract = strike * 100.0 - credit * 100.0
            return max(0.0, per_contract) * quantity
        if strategy == "jade_lizard":
            # Empirically confirmed against a real Alpaca paper submission (2026-07-24): a 3-leg
            # combo mixing a naked short put with a defined-risk call credit spread is NOT given
            # the standard Reg-T naked-margin netting/discount -- Alpaca's MLEG risk engine
            # apparently doesn't recognize "jade lizard" as an eligible strategy for that
            # treatment (unlike the 2-leg vertical / 4-leg iron condor cases below, which ARE
            # margined at their textbook defined-risk max-loss with no issue). It instead charges
            # the naked put leg at FULL cash-secured-style notional (put_strike*100, same as a
            # standalone cash_secured_put) plus the call spread's own max-loss (spread_width*100),
            # netted by the total premium collected. Verified EXACT to the dollar: a real order
            # (put strike 290, call wing width 17.5, net_credit 3.04) reported Alpaca
            # cost_basis=$30,446 == 290*100 + 17.5*100 - 3.04*100. The previous formula (plain
            # Reg-T naked_margin_per_contract on the put strike alone, ignoring the call wing
            # entirely) reserved only ~$3,289 for that same order -- a ~10x underestimate that
            # would silently let the platform think a position is affordable when Alpaca will
            # actually reject it.
            _require(strike=strike, spread_width=spread_width)
            credit = net_credit if net_credit is not None else 0.0
            per_contract = strike * 100.0 + spread_width * 100.0 - credit * 100.0
            return max(0.0, per_contract) * quantity
        if strategy in ("iron_condor", "call_butterfly", "debit_spread"):
            _require(spread_width=spread_width)
            credit = net_credit if net_credit is not None else 0.0
            return max(0.0, (spread_width - credit)) * 100.0 * quantity
        # Unreachable while RESERVING_STRATEGIES and the branches above agree. It is a
        # raise and not a `return 0.0` because the two CAN drift — someone adds a name
        # to the set and forgets the branch — and the cost of that drift being silent is
        # a structure that reserves nothing and is therefore always affordable.
        raise ValueError(
            f"option_reserve_required({strategy!r}): listed in RESERVING_STRATEGIES but "
            f"no branch prices it — refusing to fall back to a zero reserve.")

    def open_option_orders_book_wide(self) -> List[Any]:
        """EVERY non-terminal OPTION order on this ACCOUNT whose position is still open.

        The account-level counterpart of ``trade_repository.open_option_orders``, which
        is scoped to one expert *and* one underlying and therefore cannot answer any
        question about the book as a whole. Two account-level views are built on this —
        the buying-power reserve pool and the short-put assignment exposure — and they
        deliberately consume the SAME list, so "the same CSP appears in both totals" is
        a property of one query rather than a coincidence of two.

        What counts as still open is exactly what the reserve pool has always meant:
        WAITING/OPENED/CLOSING still hold the position (a submitted-but-unfilled close
        has freed nothing yet); CLOSED/FAILED release it. An order with no transaction
        yet — submitted, not linked — is still in flight and still counts.

        A TERMINAL ORDER THAT FILLED SOMETHING IS STILL IN THIS BOOK. The status filter
        used to be ``not_statuses=get_terminal_statuses()`` alone, and ``CANCELED`` is
        terminal — so a sell-to-open of 3 contracts that filled 1 and was then cancelled
        left the book entirely, taking the one contract that GENUINELY TRADED with it.
        Measured on the doubles: 300 shares pledged while ``PARTIALLY_FILLED``, then 0 the
        instant the status flipped to ``CANCELED`` with ``filled_qty`` still 1, and the
        equity close that had just been refused went straight through and sold the cover.
        The platform knows this state occurs — ``AlpacaAccount`` handles "a cancel that
        raced a live fill leaves the order CANCELED with filled_qty > 0" explicitly — and
        it is the FAIL-OPEN TWIN of the partial-fill window closed in the accessors below:
        one under-reported the part still working, this one lost the part that was done.
        A cancel does not un-trade a contract; those contracts are open until something
        closes them, so the row stays until its TRANSACTION closes, exactly like any other.

        The consumers are responsible for the other half of that rule — the CANCELLED
        REMAINDER is genuinely gone and must not be counted — which is why they classify
        such a row as EXECUTED for its filled size rather than as in-flight for its
        ordered size (see ``_traded_something``).

        The status filter therefore moves out of the query and into Python. That costs
        nothing on the in-memory backtest store (``orders_where`` already scans every row
        and filters there) and one narrowed ``WHERE`` on SQLite, which is the price of not
        being able to express "filled_qty > 0" through this seam.

        Routed through ``orders_where``/``transactions_where`` rather than a raw
        ``select`` because a raw select silently returns EMPTY while the SQL-less
        in-memory "dict trades" backtest store is active, and an empty book reads as a
        flat book at every gate.
        """
        from ba2_common.core.trade_store import orders_where, transactions_where
        from ba2_common.core.types import AssetClass, OrderStatus, TransactionStatus

        terminal = OrderStatus.get_terminal_statuses()
        option_orders = [o for o in orders_where(account_id=self.id)
                         if o.asset_class == AssetClass.OPTION
                         and (o.status not in terminal or self._traded_something(o))]
        if not any(o.transaction_id is not None for o in option_orders):
            return option_orders
        # One bulk lookup of the OPEN book (small — bounded by held positions), not N queries.
        live_ids = {
            t.id for t in transactions_where(
                not_statuses=(TransactionStatus.CLOSED, TransactionStatus.FAILED))
        }
        return [o for o in option_orders
                if o.transaction_id is None or o.transaction_id in live_ids]

    # --- Cover: "are these shares spoken for?" -----------------------------
    #
    # Seam 1 stopped the wrong-instrument order — an option transaction can no longer be
    # routed through the equity close/adjust paths, and options no longer enter the
    # allocation plan. It did NOT stop the dangerous half: a "set AAPL to 0%" allocation
    # run still sells the 100 shares collateralising an open short call and leaves that
    # call NAKED, because nothing anywhere could ask whether those shares were pledged.
    # These two accessors are that question. They only MEASURE; the refusals that consume
    # them live at the entry, close and monitoring seams.
    #
    # BOTH RETURN ``Optional[int]`` AND BOTH ARE TRI-STATE:
    #
    #   * an int, INCLUDING ``0`` — MEASURED. Zero means "nothing is pledged" / "the
    #     account holds none", and the caller may proceed.
    #   * ``None`` — UNMEASURABLE. The caller must REFUSE, not assume.
    #
    # DO NOT "SIMPLIFY" THE ``None`` AWAY. Returning ``0`` on an unreadable book is what
    # strips a covered call of its cover during a broker outage, and it is the single most
    # expensive instance of this codebase's recurring unknown-reads-as-zero defect — the
    # same shape as ``get_positions()`` returning ``None`` (fetch failed) versus ``[]``
    # (genuinely flat), which read as one thing on 2026-07-03 and force-closed 8 real open
    # transactions during a DNS outage.

    #: Types that mean "the world was uncooperative" on the seams below, absorbed into an
    #: UNMEASURABLE answer rather than propagated. ``OSError`` (the network/filesystem
    #: family) is already benign everywhere; ``OperationalError`` is named here because the
    #: book is a DB read and a locked/failed database is a data condition, not a defect in
    #: this file.
    #:
    #: NARROWED FROM ``SQLAlchemyError`` (2026-08), which was far wider than the sentence
    #: above justifies. Its subtree also contains ``ProgrammingError`` (bad SQL / "no such
    #: column: multiplier"), ``IntegrityError`` (a constraint THIS code violated),
    #: ``InvalidRequestError``/``DetachedInstanceError`` (a misused session) and
    #: ``ArgumentError`` (a malformed query) — every one of them a statement about this
    #: program's correctness rather than about the world. Probed on the pre-fix code: a
    #: ``ProgrammingError`` was absorbed, ``None`` came back, and with the exit guard in
    #: place that is a PERMANENT refusal of every share sale on every underlying, wearing
    #: the face of a safety measure. ``OperationalError`` is the one SQLAlchemy class that
    #: means what the justification says — "database is locked", "disk I/O error", "unable
    #: to open database file", a dropped connection — and it is the class the rest of this
    #: repo already uses to stand for exactly that (``test_funded_entry_loop``,
    #: ``test_db_attached_instance_guard``, and the sibling seam's
    #: ``test_a_LOCKED_DATABASE_is_not_read_as_NOT_AN_OPTION``).
    #:
    #: ``DBAPIError`` was considered and REJECTED as the wider option: it is
    #: ``ProgrammingError``'s own parent, so choosing it would re-admit the very defect
    #: this narrowing removes. The socket-level half of "the world was uncooperative" is
    #: already covered by the globally-benign ``OSError``.
    #:
    #: NOTE THE INVERSION relative to the seam guards. There, absorbing a locked database
    #: meant "carry on" and was PROVED to cancel protective legs. Here, absorbing means
    #: returning ``None``, which every caller must treat as a REFUSAL — the fail-CLOSED
    #: direction. Anything outside this tuple (a ``TypeError`` from a bad row shape, say)
    #: still propagates under ``BA2_ERROR_MODE=enforce``, because a defect that quietly
    #: answers "unmeasurable" forever is a gate that has silently stopped working.
    @staticmethod
    def _cover_benign_errors() -> Tuple[type, ...]:
        from sqlalchemy.exc import OperationalError
        return (OperationalError,)

    @staticmethod
    def _readable_number(raw) -> Optional[float]:
        """``raw`` as a finite float, or ``None`` when it is not a measurement.

        Rejects ``None``, ``bool`` (``True`` is not a quantity of 1), non-numerics and
        NaN/inf. A numeric STRING is accepted, for the reason ``must_measure`` accepts
        one: broker payloads arrive that way and ``"100"`` is not ambiguous. Sign is the
        CALLER's business — a share count is legitimately negative (a short), a contract
        multiplier is not.
        """
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        return value

    @staticmethod
    def _position_field(pos, name):
        """One field off a position row, whichever shape the adapter hands back.

        Both shapes are real on this seam and
        ``ReadOnlyAccountInterface.get_available_position_quantity`` already accepts
        either; a cover reader that silently saw nothing in a dict book would report a
        flat account, which is the whole defect being closed here.
        """
        return pos.get(name) if isinstance(pos, dict) else getattr(pos, name, None)

    @classmethod
    def _traded_something(cls, order) -> bool:
        """Did this order put contracts on the book, whatever its status says NOW?

        The test that keeps a cancelled-after-a-partial-fill row inside
        ``open_option_orders_book_wide``. ``filled_qty`` is the only field that answers
        it: a cancel does not un-trade what already printed.

        A ``filled_qty`` that is PRESENT BUT UNREADABLE answers ``True``, not ``False``.
        The row is claiming a fill it will not quantify, and dropping it would let the
        least trustworthy row in the book be the one that silently frees cover; kept, its
        size is judged by the accessors, which flag exactly that shape as UNMEASURABLE.
        ``None`` is the one honest "nothing filled" and is the only value that excludes.
        """
        raw = getattr(order, "filled_qty", None)
        if raw is None:
            return False
        value = cls._readable_number(raw)
        return True if value is None else value > 0

    @classmethod
    def _working_remainder(cls, order, filled: float, *, what: str, label,
                           consequence: str) -> Tuple[Optional[float], Optional[str]]:
        """``(remainder, reason)`` — how much of ``order`` is STILL WORKING at the broker.

        ONE definition for a window three readings of the same book have to agree about.
        The pledge view, the assignment-exposure view and the in-flight equity view each
        need "ordered minus filled" and each used to spell it out for itself, differing
        only in the noun in the error message; the two option views' docstrings both
        insist they must not drift about this window, and a comment is a weaker way to
        say that than a shared function.

        ``filled`` is passed IN rather than re-read here on purpose: the callers have
        already decided what counts as filled for their row shape (the option views fall
        back to ``quantity`` when ``filled_qty`` is falsy, and re-deriving it here would
        double-count such a row — once in their ``net``, once in this remainder).

        ``remainder`` is ``None`` exactly when the ORDERED quantity is unreadable, which
        is an obligation of unknown size and must reach the caller as UNMEASURABLE rather
        than as a zero remainder. ``max(0.0, ...)``: a ``filled_qty`` above the ordered
        quantity is a damaged row, not a NEGATIVE obligation that hands the cover back.
        """
        ordered = cls._readable_positive_number(order.quantity)
        if ordered is None:
            return None, (
                f"order {getattr(order, 'id', '?')} ({label}) is a {what} whose ordered "
                f"quantity is unreadable (quantity={order.quantity!r}, "
                f"filled_qty={order.filled_qty!r}) — how much of it is still working at "
                f"the broker, and so {consequence}, is unknown")
        return max(0.0, ordered - filled), None

    @classmethod
    def _readable_positive_number(cls, raw) -> Optional[float]:
        """:meth:`_readable_number`, additionally refusing ``<= 0``.

        Same discipline as ``option_lifecycle.put_assignment_cost``, which refuses a
        strike of ``0`` on the grounds that no listed option has one, so it is a missing
        field rather than a free put. A multiplier of ``0`` and a short call of ``0``
        contracts are missing fields by the same argument.
        """
        value = cls._readable_number(raw)
        if value is None or value <= 0:
            return None
        return value

    def shares_pledged_to_short_calls(self, underlying: str) -> Optional[int]:
        """Shares of ``underlying`` already acting as COVER for open short calls.

        TRI-STATE (see the block comment above): an int including ``0`` is MEASURED and
        the caller may proceed; ``None`` is UNMEASURABLE and the caller must REFUSE.
        ``0`` here means "no short call has a claim on these shares"; ``None`` means "we
        could not find out", and selling on that is how a covered call goes naked.

        Built on ``open_option_orders_book_wide()`` — the same list the reserve pool and
        the assignment exposure read, so a third view can never disagree with them about
        what is still open (it already counts ``CLOSING`` as open: a submitted-but-unfilled
        buy-to-close has released nothing).

        SHORT PUTS ARE EXCLUDED, deliberately. A short put obliges CASH, which is
        ``short_put_assignment_exposure``'s question; it places no claim on share
        inventory, and counting it here would refuse to sell shares nothing has a claim
        on. LONG options pledge nothing at all — only the short side can be called away.

        A SPREAD'S SHORT CALL IS COUNTED LIKE ANY OTHER. This reads the ORDER BOOK, where
        the short leg of a bear call spread is a short call on the ticker; the long wing
        beside it is not cover this can verify (nothing guarantees the long is exercised
        to satisfy an assignment, and it can itself have been sold). Netting the two
        would hand back shares on a promise, so the leg pledges its full share count.
        Fail-safe and deliberate, with a real consequence the callers must state: an open
        credit spread consumes cover on its ticker, so ``check_cover_for_covered_call``
        can refuse a genuinely covered call written beside one.

        THE MULTIPLIER IS READ PER CONTRACT AND NEVER ASSUMED TO BE 100 (OPT-L7). An
        adjusted contract — post-split, post-merger — can deliver a different number of
        shares, and a hard-coded 100 would under-report the pledge on precisely the
        contract whose oddity nobody remembers. A contract whose multiplier cannot be read
        (or whose rows disagree about it) is therefore UNMEASURABLE, not a guess.

        Netted per contract symbol over EXECUTED orders (SELL ``−``, BUY ``+``), the same
        netting the exposure view and the close paths perform, so a call bought back stops
        pledging. An unfilled SELL-to-open is added on top WITHOUT netting: it can fill at
        any moment and can only ever ADD an obligation, whereas an unfilled buy-to-close
        has closed nothing and must not hand the cover back early. (Against a contract we
        hold NET LONG an in-flight SELL can only close the long, so counting it there
        OVER-reports — the safe direction, and cheaper than deciding which it is.)

        A PARTIALLY FILLED SELL PLEDGES ITS WHOLE ORDERED SIZE, not just the filled part.
        ``PARTIALLY_FILLED`` is an EXECUTED status, so the filled contracts net normally;
        the unfilled remainder is added to the in-flight total under the very rule above —
        it is still working at the broker and can fill at any moment. Reading only the
        filled part reported a sell-to-open of 3 contracts with 1 filled as 100 shares
        pledged rather than 300, and during that window a consumer frees 200 shares the
        next fill leaves naked.

        A CANCELLED PARTIAL FILL PLEDGES EXACTLY WHAT IT TRADED — the mirror of that rule,
        and its FAIL-OPEN twin. The same 3-contract sell-to-open, once its status flips to
        ``CANCELED`` with ``filled_qty`` still 1, has ONE real short call on the book and
        two contracts that will never exist. Both halves matter and they point opposite
        ways: the cancelled remainder is gone and must not be counted (300 would refuse
        share sales against an obligation nobody owes), while the filled part is real and
        must be (0 hands back cover for a call that can still be assigned — measured, the
        pledge dropped 300 -> 0 and the equity close that had just been refused went
        through). Such a row is therefore treated as EXECUTED FOR ITS FILLED SIZE: it
        NETS, so a later buy-to-close releases it, and it contributes NO in-flight
        remainder, because nothing of it is working any more. (``open_option_orders_book_
        wide`` is what keeps the row visible at all; it used to drop every terminal row.)

        WHAT MAKES A ROW UNMEASURABLE — each case is "this row could be a short call on
        the ticker you asked about, and I cannot rule it out":

        * a SELL CALL with no usable quantity — an obligation of unknown size;
        * a PARTIALLY FILLED SELL CALL whose ORDERED quantity is unreadable — the part
          still working at the broker is an obligation of unknown size;
        * a SELL with no ``option_type``      — it might be the call;
        * a SELL CALL with no ``underlying_symbol`` — it might be on this ticker (there is
          no fallback to ``symbol``: on a leg child that field holds the OCC contract
          string, which would never match and would report the shares as free);
        * a held short call with no readable multiplier.

        THE LAST THREE ARE DEFERRED PER CONTRACT, not raised where they are found. Each is
        a question about a CONTRACT, and a contract that is flat by the end of the book
        pledges nothing whatever the answer would have been — so a call fully bought back
        whose sell row lost its right, its ticker or its multiplier must not lock the gate
        forever. The damaged row is still netted pessimistically (counted as a short call
        on this ticker), so the deferral is only ever discharged by a real offsetting BUY.
        The first two cannot be deferred: a size we cannot read cannot be netted off, so
        there is no way to learn that the contract went flat.

        A BUY in any of those states is NOT flagged: failing to recognise a buy-to-close
        can only fail to RELIEVE a short, which overstates the pledge — the safe
        direction. Neither is a multi-leg PARENT row, which carries no contract, no right
        and no size (its legs carry all three, one row each); flagging parents would make
        every spread in the book permanently unknown.

        ONE unreadable row makes the WHOLE answer ``None``, never "the part we could
        read": a partial sum is a smaller number that looks exactly like a measured one,
        and the caller would free the difference.
        """
        from ba2_common.core.failure_modes import absorb_if_benign
        from ba2_common.core.types import OrderDirection, OrderStatus
        from ba2_common.logger import logger

        wanted = (underlying or "").strip().upper()
        if not wanted:
            logger.error(
                f"Account {self.id}: shares_pledged_to_short_calls({underlying!r}) has no "
                f"underlying to measure — reporting UNKNOWN rather than 'nothing is "
                f"pledged', which is what a 0 would be read as")
            return None

        try:
            book = self.open_option_orders_book_wide()
        except Exception as e:  # noqa: BLE001 — narrowed by absorb_if_benign
            absorb_if_benign(e, *self._cover_benign_errors())
            logger.error(
                f"Account {self.id}: the open option book could not be read ({e}), so how "
                f"many {wanted} shares are pledged to short calls is UNKNOWN. Every share "
                f"of {wanted} must be treated as spoken for until it can be read — an "
                f"unreadable book is not an empty one.", exc_info=True)
            return None
        if book is None:
            logger.error(
                f"Account {self.id}: the open option book came back as None, so how many "
                f"{wanted} shares are pledged to short calls is UNKNOWN (None is a FETCH "
                f"FAILURE, not a flat book).")
            return None

        executed = OrderStatus.get_executed_statuses()
        terminal = OrderStatus.get_terminal_statuses()
        net: dict = {}          # contract -> signed contracts, EXECUTED only (SELL −)
        pending: dict = {}      # contract -> in-flight SELL contracts, never netted
        mults: dict = {}        # contract -> set of readable multipliers seen
        mult_blind: dict = {}   # contract -> a reason its multiplier is unreadable
        id_blind: dict = {}     # contract -> a reason a SELL row's identity is unreadable
        blind: List[str] = []

        for o in book:
            if o.status in terminal and not self._traded_something(o):
                # A dead order that printed NOTHING: it owes nothing and it is not in
                # flight either. ``open_option_orders_book_wide`` already withholds these,
                # so this is belt-and-braces — but without it the arithmetic depends on
                # the query, and a row that reached ``pending`` here would pledge its full
                # ordered size for a cancel that freed everything.
                continue
            contract = getattr(o, "contract_symbol", None)
            if not contract:
                # A multi-leg PARENT: no contract, no right, no strike. Its legs carry the
                # identity, one row each. Skipping it is not a gap; flagging it would make
                # every spread unknown, and counting it would double-count.
                continue
            right = getattr(o, "option_type", None)
            if right is not None and right != OptionRight.CALL:
                # A put pledges no shares whoever it belongs to — no need to know more.
                continue
            is_sell = getattr(o, "side", None) == OrderDirection.SELL
            row_underlying = (getattr(o, "underlying_symbol", None) or "").strip().upper()
            if row_underlying and row_underlying != wanted:
                continue                    # definitively a different ticker

            # An identity field this SELL row has lost. DEFERRED per CONTRACT rather than
            # raised on the spot, for exactly the reason the multiplier below is deferred:
            # a contract that is FLAT by the end of the book pledges nothing whatever its
            # right or ticker turns out to have been, and refusing every share sale on
            # {wanted} forever over a closed-out call's damaged row is a false refusal —
            # one that, now the exit guard consumes this number, blocks selling the shares
            # rather than merely blocking a write. The row is still netted PESSIMISTICALLY
            # (counted as a short call on {wanted}) so the deferral can only ever be
            # discharged by a real offsetting BUY, never by ignoring the row.
            id_gap = None
            if right is None:
                id_gap = (
                    f"order {getattr(o, 'id', '?')} ({contract}) is a SHORT option with "
                    f"no option type recorded — whether it is a CALL that pledges "
                    f"{wanted} shares is unknown, and unknown must not resolve to 'not "
                    f"a call'")
            elif not row_underlying:
                id_gap = (
                    f"order {getattr(o, 'id', '?')} ({contract}) is a SHORT CALL with no "
                    f"underlying recorded — whether it is written on {wanted} is "
                    f"unknown, and unknown must not resolve to 'some other ticker'")
            if id_gap is not None and not is_sell:
                # A BUY in either state is NOT flagged and NOT netted: failing to
                # recognise a buy-to-close can only fail to RELIEVE a short, which
                # overstates the pledge — the safe direction.
                continue

            raw_qty = o.filled_qty if o.filled_qty else o.quantity
            qty = self._readable_positive_number(raw_qty)
            if qty is None:
                if is_sell:
                    blind.append(
                        f"order {getattr(o, 'id', '?')} ({contract}) is a SHORT CALL with no "
                        f"usable quantity (filled_qty={o.filled_qty!r}, "
                        f"quantity={o.quantity!r}) — how many {wanted} shares it can call "
                        f"away is unknown, and unknown is not zero")
                continue
            if id_gap is not None:
                # Its SIZE is readable, so it can be netted and the gap deferred. A size
                # we could not read is handled above and is unconditionally blind: an
                # obligation we cannot measure cannot be netted off either.
                id_blind.setdefault(contract, id_gap)

            raw_mult = getattr(o, "multiplier", None)
            mult = self._readable_positive_number(raw_mult)
            if mult is None:
                # Recorded against the CONTRACT, not raised now: a contract that is flat by
                # the end of the book pledges nothing, and refusing every share sale over a
                # closed-out call's damaged row would be a false refusal.
                mult_blind.setdefault(
                    contract,
                    f"order {getattr(o, 'id', '?')} ({contract}) has multiplier "
                    f"{raw_mult!r} — how many {wanted} shares one contract delivers is "
                    f"unknown, and it must NOT be assumed to be 100 (an adjusted contract "
                    f"can deliver a different number, and guessing under-reports the "
                    f"pledge on exactly that contract)")
            else:
                mults.setdefault(contract, set()).add(mult)

            # A TERMINAL row reaching this line FILLED something (the others were skipped
            # at the top of the loop), so it is EXECUTED for that filled size — ``raw_qty``
            # above already resolved to ``filled_qty``. It goes into ``net``, never into
            # ``pending``: those contracts really traded and must be releasable by a later
            # buy-to-close, whereas ``pending`` is never netted and would pledge them
            # forever. It grows no remainder either — a dead order is not working at the
            # broker — which is the "the cancelled remainder is gone" half of the rule.
            if o.status in executed or o.status in terminal:
                net[contract] = net.get(contract, 0.0) + (-qty if is_sell else qty)
                if is_sell and o.status == OrderStatus.PARTIALLY_FILLED:
                    # THE PARTIAL-FILL WINDOW. ``qty`` above is only what has FILLED
                    # (PARTIALLY_FILLED is an EXECUTED status — see
                    # ``OrderStatus.get_executed_statuses``), so netting it alone reports
                    # a sell-to-open of 3 contracts with 1 filled as a 100-share pledge
                    # instead of 300. The unfilled remainder is still working at the
                    # broker, and the docstring's rule for an in-flight SELL applies to it
                    # verbatim: it can fill at any moment and can only ever ADD an
                    # obligation. During that window a consumer would otherwise free 200
                    # shares that the next fill leaves naked. It goes into ``pending``,
                    # never into ``net``, so a buy-to-close cannot net it away early.
                    remainder, reason = self._working_remainder(
                        o, qty, what="PARTIALLY FILLED SHORT CALL", label=contract,
                        consequence=f"how many more {wanted} shares it can call away")
                    if remainder is None:
                        blind.append(reason)
                    elif remainder > _ASSIGNMENT_EPS:
                        pending[contract] = pending.get(contract, 0.0) + remainder
            elif is_sell:
                pending[contract] = pending.get(contract, 0.0) + qty

        shares = 0.0
        for contract in sorted(set(net) | set(pending)):
            held = net.get(contract, 0.0)
            short_contracts = pending.get(contract, 0.0)
            if held < -_ASSIGNMENT_EPS:      # net SHORT: those contracts still pledge
                short_contracts += -held
            if short_contracts <= _ASSIGNMENT_EPS:
                continue                     # flat, or net LONG: nothing is pledged
            if contract in id_blind:
                blind.append(id_blind[contract])
                continue
            if contract in mult_blind:
                blind.append(mult_blind[contract])
                continue
            seen = mults.get(contract, set())
            if len(seen) != 1:
                blind.append(
                    f"contract {contract} is held SHORT but its rows report "
                    f"{sorted(seen)!r} as the multiplier — one contract cannot deliver two "
                    f"different share counts, and there is no way to tell which row is "
                    f"wrong")
                continue
            shares += short_contracts * next(iter(seen))

        if blind:
            logger.error(
                f"Account {self.id}: how many {wanted} shares are pledged as cover for "
                f"open short calls is UNKNOWN ({len(blind)} unreadable row(s)), so every "
                f"{wanted} share must be treated as spoken for until they are repaired. "
                + "; ".join(blind))
            return None
        # Rounded UP (contracts are whole and multipliers are integers, so this only ever
        # absorbs float-addition dust): under-reporting the pledge by even one share is
        # the direction that uncovers a call.
        return int(math.ceil(round(shares, 6)))

    def held_shares_for_cover(self, underlying: str) -> Optional[int]:
        """Shares of ``underlying`` this ACCOUNT holds, for cover arithmetic.

        TRI-STATE, identically to :meth:`shares_pledged_to_short_calls`: an int including
        ``0`` is MEASURED (``0`` = the broker confirmed it holds none); ``None`` is
        UNMEASURABLE and the caller must REFUSE rather than assume either way.

        DELIBERATELY NOT ``_OptionEntryAction._held_equity_shares``, which sums one
        EXPERT's own filled buys. That scoping is correct for its own question and wrong
        for this one — see ``HasAssignedSharesCondition``'s docstring, which draws the same
        line: "coverage and eligibility are different questions". Cover is an
        ACCOUNT-WIDE fact. Shares bought by a different expert still cover the call,
        because the broker does not care who bought them, and an expert-scoped reading
        would report a covered call as naked (and, at the close seam, would let another
        expert's shares be sold out from under it).

        Read from ``get_positions()``, whose tri-state contract is honoured exactly:
        ``None`` is the FETCH FAILING (-> ``None`` here), ``[]`` is a genuinely flat
        account (-> a measured ``0``). Never ``for pos in (positions or [])``: that idiom
        is what re-conflates the two, and it force-closed 8 real transactions on
        2026-07-03.

        OPTION ROWS ARE SKIPPED, on the same ``"option" in asset_class`` tell
        ``AlpacaAccount.get_option_positions`` uses. Not belt-and-braces:
        ``AlpacaAccount.get_positions`` maps EVERY row ``get_all_positions()`` returns,
        options included (only TastyTrade filters them). One contract is not one share, so
        counting one would report a single share of cover against a 100-share obligation.
        The symbol filter alone would not catch it either, because IBKR builds its
        ``Position`` from ``ib_pos.contract.symbol`` — the UNDERLYING for an option.

        KNOWN GAP, recorded rather than papered over: that same ``IBKRAccount.get_positions``
        constructs ``Position`` without ``asset_class`` at all (it is ``None`` — SQLModel
        ``table=True`` skips validation), so an IBKR-held option WOULD be counted here as
        equity cover under its underlying's ticker. Nothing in a ``Position`` distinguishes
        it, so the fix belongs in that adapter — it should filter options the way
        ``TastyTradeAccount.get_positions`` does — and not in a guess here.

        A SHORT equity position is NEGATIVE cover, and the sign is taken from ``side`` as
        well as from ``qty`` because the adapters disagree: Alpaca reports a short as a
        negative ``qty``, TastyTrade as a POSITIVE ``qty`` with ``side=SELL``. Reading the
        magnitude alone would credit an account that is short 100 shares with 100 shares
        of cover.

        A row for this symbol that publishes NO readable quantity is UNMEASURABLE, not
        ``0``: "the broker holds it but will not say how much" is exactly the case where
        selling the lot could uncover a call. A row for ANOTHER symbol is never inspected,
        so it cannot poison the answer.

        KNOWN GAP, the ENTRY face of the one recorded in
        :meth:`equity_shares_working_to_sell` — read that one for the whole story. This
        reads ``qty``, the FULL holding, NOT ``qty_available`` ("total shares minus open
        orders"). Shares already committed to a resting SELL_STOP therefore still count as
        cover, so writing the equity stop first and the covered call second — the normal
        ordering — lets :meth:`check_cover_for_covered_call` admit a call against shares the
        stop can sell. Pinned by
        ``test_a_RESTING_STOP_does_not_reduce_the_cover_a_WRITE_sees__a_recorded_gap``.
        """
        from ba2_common.core.failure_modes import absorb_if_benign
        from ba2_common.core.types import OrderDirection
        from ba2_common.logger import logger

        wanted = (underlying or "").strip().upper()
        if not wanted:
            logger.error(
                f"Account {self.id}: held_shares_for_cover({underlying!r}) has no symbol to "
                f"measure — reporting UNKNOWN rather than 'we hold none'")
            return None

        try:
            positions = self.get_positions()
        except Exception as e:  # noqa: BLE001 — narrowed by absorb_if_benign
            absorb_if_benign(e, *self._cover_benign_errors())
            logger.error(
                f"Account {self.id}: the position fetch raised ({e}), so how many {wanted} "
                f"shares this account holds is UNKNOWN — an unverified book is not an "
                f"empty one.", exc_info=True)
            return None
        if positions is None:
            logger.error(
                f"Account {self.id}: get_positions() returned None (FETCH FAILURE, not a "
                f"flat account), so how many {wanted} shares this account holds is "
                f"UNKNOWN. Nothing may be sold or written against that.")
            return None

        total = 0.0
        for pos in positions:
            field = self._position_field
            symbol = (field(pos, "symbol") or "").strip().upper()
            if symbol != wanted:
                continue
            if "option" in str(field(pos, "asset_class")).lower():
                continue
            raw_qty = field(pos, "qty")
            quantity = self._readable_number(raw_qty)
            if quantity is None:
                logger.error(
                    f"Account {self.id}: the {wanted} position publishes no readable "
                    f"quantity (qty={raw_qty!r}), so how many shares are available as "
                    f"cover is UNKNOWN — and unknown is not zero.")
                return None
            # A measured 0 is a real answer (a flat row) and contributes nothing.
            is_short = quantity < 0 or field(pos, "side") == OrderDirection.SELL
            total += -abs(quantity) if is_short else quantity

        # Rounded DOWN — the mirror of the pledge's round-up. Here it is OVER-reporting
        # the cover that would let a call be written naked.
        return int(math.floor(round(total, 6)))

    def equity_shares_working_to_sell(
        self, symbol: str, *, except_transaction_id: Optional[int] = None,
    ) -> Optional[int]:
        """Shares of ``symbol`` this account has already COMMITTED to sell but not sold.

        THE THIRD COVER ACCESSOR, and the one that makes the other two answer a question
        about the FUTURE rather than about this instant. TRI-STATE on the same terms: an
        int including ``0`` is MEASURED; ``None`` is UNMEASURABLE and the caller refuses.

        WHY IT EXISTS. ``held_shares_for_cover`` reports what the broker holds RIGHT NOW,
        and the exit guard compared one transaction's size against it — with no account of
        the closes the same run had already authorised. An allocation run that targets 0%
        on a symbol walks EVERY transaction for it (``_close_symbol``), and the codebase
        explicitly models multi-lot holdings (``split_delta_fifo``: "30 shares held as
        20 + 10"). Measured on the doubles, 200 shares held as two 100-share lots with 100
        pledged to one short call: both closes were admitted and 200 shares went out, so
        the call was left NAKED by a guard whose whole purpose was to prevent that.

        On the DEFERRED close branch it is not even a race. When ``close_transaction``
        cancels a TP/SL, the close order is written ``PENDING`` and NOTHING is sent, so
        ``get_positions()`` cannot decrement between iterations however slowly the loop
        runs — both rows are written every time. That is also what makes this readable:
        the first close is already durable state by the time the second one asks.

        WHAT COUNTS. Every EQUITY MARKET SELL on this ACCOUNT, for this symbol, in an
        ACTIVE status (``OrderStatus.get_active_statuses`` — "not terminal and not fully
        filled"), for its UNFILLED REMAINDER only. The filled part of a partially filled
        sell has already left the position and ``get_positions()`` reports it; counting it
        again would double-subtract it. ``PENDING`` is in that set and is the case that
        matters most: a deferred close is written PENDING and reaches the broker later.

        MARKET IS THE TEST FOR "A SALE ALREADY DECIDED", and it is exact rather than
        heuristic here. Every exit this platform writes is a MARKET order —
        ``submit_close_order_for_transaction``, ``adjust_quantity_with_tpsl``'s partial
        close, and ``TradeActions._close_position`` all build ``OrderType.MARKET`` — while
        a protective leg is never one (a TP is ``SELL_LIMIT``, an SL ``SELL_STOP``, both
        together an ``OCO``).

        KNOWN GAP — A RESTING PROTECTIVE LEG IS INVISIBLE TO THE COVER ARITHMETIC IN BOTH
        DIRECTIONS. Recorded rather than papered over, and it is ONE gap with two faces:

        * THE EXIT face, here. A stop-loss on the covering shares can strip a call, and it
          is not counted as "already committed to sell". It WAS counted, and was reverted
          (828e65a9): ``close_transaction`` cancels a transaction's legs AT THE BROKER
          without terminalising the DB rows (they clear on the next ``refresh_orders``),
          so every later exit on the ticker refused on the strength of orders that no
          longer existed — a guard that blocks every exit is its own incident, and the
          finding that prompted this accessor is about closes the run itself authorised.
          ``MARKET`` is therefore the test for "a sale already decided".
        * THE ENTRY face, in :meth:`held_shares_for_cover` and so in
          :meth:`check_cover_for_covered_call`. ``held`` is read from the position row's ``qty``,
          which is the full holding — shares already committed to a resting SELL_STOP
          included. Write the equity stop FIRST and the covered call SECOND (the normal
          ordering, since equity positions here are routinely opened with TP/SL) and the
          write is admitted against shares the stop can sell out from under it. Naming the
          remedy as belonging "where the stop is WRITTEN" does not cover this: that ordering
          never reaches the stop-writing seam with the call already open.

        ``qty_available`` IS THE FIELD THAT WOULD ANSWER BOTH — "total shares minus open
        orders", the number Alpaca actually enforces (``models.Position.qty_available``;
        ``AlpacaAccount.get_positions`` populates it, ``TastyTradeAccount`` mirrors ``qty``
        into it, ``IBKRAccount`` leaves it unset). Reading it instead of ``qty`` would
        subtract a resting leg at the source, on live broker state rather than on DB rows
        the platform is known to leave stale — which is what made counting the legs
        unworkable at the exit face. It is not done here because a tri-state accessor
        cannot treat an adapter that does not publish the field as "nothing is available",
        and closing that properly means fixing the adapters first. Until then the gap is
        tracked, in both directions, by
        ``test_a_RESTING_PROTECTIVE_LEG_is_not_netted__a_recorded_gap`` and
        ``test_a_RESTING_STOP_does_not_reduce_the_cover_a_WRITE_sees__a_recorded_gap``.

        ACCOUNT-WIDE, not per transaction, for the same reason cover itself is account-
        wide (see :meth:`held_shares_for_cover`): the broker does not care which
        transaction sells the share, and a per-transaction reading would let two lots each
        believe the other's shares are still there.

        ``except_transaction_id`` EXISTS TO PREVENT A DOUBLE COUNT ON A RE-ISSUED CLOSE,
        and it is the one exclusion — and it belongs to the CLOSE seam alone. A caller
        asking "may I close THIS transaction?" proposes an alternative disposition of the
        very shares its own staged close would take, not an additional one: a close covers
        the WHOLE net open quantity, so a second one is the same inventory read twice, and
        ``close_transaction`` cancels that transaction's own legs on the way (that is what
        ``last_broker_canceled_order_id`` is for). Counted, a retried close would refuse
        itself for as long as the earlier row stayed unfilled, with no action that could
        clear it.

        IT IS WRONG FOR A PARTIAL TRIM, which is why ``adjust_quantity_with_tpsl`` passes
        ``None``. Every trim stages an ADDITIONAL slice and writes ``transaction.quantity``
        DOWN, so the next trim sizes itself against the reduced figure and its own earlier
        slices are exactly the rows the netting needs; and the trim's cancel step clears
        ``get_active_tpsl_orders`` — TP/SL/OCO legs — not a partial-close MARKET row (one
        submitted with no protective leg to trigger off carries no ``depends_on_order`` and
        is not in that list at all). Excluded, two −50 trims of a 150-share holding with
        100 pledged were BOTH admitted — the second saw working = 0 instead of 50 — and
        committed 100 shares to be sold, leaving 50 against a 100-share pledge.

        Other transactions' working sells stay counted in every case: those dispose of
        shares this one does not.

        OPTION ROWS ARE SKIPPED on the ``asset_class`` field: one contract is not one
        share, and an option SELL order counted here would subtract contracts from a share
        count.

        A SELL ROW THAT WILL NOT SAY HOW MANY SHARES IT WILL SELL IS UNMEASURABLE, never
        ``0`` — the same rule the other two accessors apply to their own missing sizes.
        """
        from ba2_common.core.failure_modes import absorb_if_benign
        from ba2_common.core.trade_store import orders_where
        from ba2_common.core.types import (
            AssetClass, OrderDirection, OrderStatus, OrderType)
        from ba2_common.logger import logger

        wanted = (symbol or "").strip().upper()
        if not wanted:
            logger.error(
                f"Account {self.id}: equity_shares_working_to_sell({symbol!r}) has no "
                f"symbol to measure — reporting UNKNOWN rather than 'nothing is on its "
                f"way out'")
            return None

        try:
            rows = orders_where(account_id=self.id,
                                statuses=OrderStatus.get_active_statuses())
        except Exception as e:  # noqa: BLE001 — narrowed by absorb_if_benign
            absorb_if_benign(e, *self._cover_benign_errors())
            logger.error(
                f"Account {self.id}: the working order book could not be read ({e}), so "
                f"how many {wanted} shares are already committed to be sold is UNKNOWN — "
                f"an unreadable order book is not an empty one.", exc_info=True)
            return None
        if rows is None:
            logger.error(
                f"Account {self.id}: the working order book came back as None, so how "
                f"many {wanted} shares are already committed to be sold is UNKNOWN (None "
                f"is a FETCH FAILURE, not an empty book).")
            return None

        total = 0.0
        blind: List[str] = []
        for o in rows:
            if getattr(o, "asset_class", None) == AssetClass.OPTION:
                continue
            if (getattr(o, "symbol", None) or "").strip().upper() != wanted:
                continue
            if getattr(o, "side", None) != OrderDirection.SELL:
                continue
            if getattr(o, "order_type", None) != OrderType.MARKET:
                continue
            if (except_transaction_id is not None
                    and getattr(o, "transaction_id", None) == except_transaction_id):
                continue
            # A negative / non-numeric filled_qty is read as "nothing has filled", which
            # makes the whole ordered size count — the direction that refuses.
            filled = self._readable_number(getattr(o, "filled_qty", None)) or 0.0
            remainder, reason = self._working_remainder(
                o, max(0.0, filled), what="working EQUITY SELL order", label=wanted,
                consequence=f"how many more {wanted} shares are on their way out")
            if remainder is None:
                blind.append(reason)
                continue
            total += remainder

        if blind:
            logger.error(
                f"Account {self.id}: how many {wanted} shares are already committed to be "
                f"sold is UNKNOWN ({len(blind)} unreadable row(s)), so no further "
                f"{wanted} sale can be shown to leave an open short call covered. "
                + "; ".join(blind))
            return None
        # Rounded UP, like the pledge and for the same reason: under-reporting what is
        # already on its way out is the direction that uncovers a call.
        return int(math.ceil(round(total, 6)))

    def reserved_option_buying_power_detail(self) -> "ReservePool":
        """The reserve pool WITH its unknowns named — the honest form of the answer.

        A reserve belongs to the POSITION, not to the order row that created it: the broker
        frees the margin/cash the moment the structure is flattened, so this must too.

        Previously the sum ran over every order not in a TERMINAL status — but ``FILLED``
        is NOT terminal (see ``OrderStatus.get_terminal_statuses``), and nothing ever
        cleared the field or terminalised a filled entry order. The reserve was therefore a
        ONE-WAY RATCHET: every credit/naked structure ever opened consumed buying power for
        the remainder of the run, even long after it closed. On the options grid's $20k
        account that exhausted BP after 1-3 structures, which is why the RESERVING groups
        (OS2/OS3) capped out at 10-20 trades all clustered in the run's opening weeks while
        the non-reserving debit groups (OS1/OS4) traded 43-214 times over the identical
        window — and why the GA appeared to "win by barely trading" (it could not trade).

        UNKNOWN IS NOT ZERO — the defect this method exists for. The sum used to read
        ``float((o.data or {}).get("option_reserve", 0) or 0)``, so an OPEN order for a
        strategy that MUST reserve, whose ``option_reserve`` had gone missing (the persist
        step in ``TradeActions._submit_option_order`` is best-effort and logs on failure; a
        row written by an older build; a manual repair), contributed 0. An unknown reserve
        therefore *freed* buying power, and the next structure was waved through on money
        already committed. Measured: one such row on a $100k account reported
        ``reserved=0.0``, ``available=100000.0``, ``check_option_buying_power(100000)=True``.

        Which orders are expected to carry one is decided BY NAME, never by guessing:

        * ``option_strategy in RESERVING_STRATEGIES`` -> a reserve is mandatory, and a
          missing / unreadable / non-positive one is UNMEASURABLE. Non-positive counts as
          unmeasurable because every ``return 0.0`` inside ``option_reserve_required`` for a
          reserving name fires when a *sizing input was missing*, so a stored 0 on a priced
          strategy is that same fail-open one layer down. Checked against the write path:
          all eight credit builders size with ``_size_by_reserve``, which returns 0 for a
          non-positive per-contract reserve, and every one of them refuses at
          ``quantity < 1`` — so a legitimate submission can never persist a 0 here, and
          this branch only ever fires on a corrupted or legacy row.
        * anything else contributes 0.0, genuinely: ``ZERO_RESERVE_STRATEGIES`` reserve
          nothing by definition, ``"close"`` describes an action rather than a position, and
          multi-leg leg CHILDREN carry no ``option_strategy`` at all (the parent holds the
          reserve). Flagging those would make every spread permanently unknown.

        A TERMINAL ROW IS PRO-RATED TO WHAT IT ACTUALLY FILLED. Such a row is in this book
        only because it traded something before it died (see
        ``open_option_orders_book_wide``), and its stored ``option_reserve`` was sized for
        the ORDERED quantity — the broker released the part that never traded. A
        3-contract cash-secured put that filled 1 and was then cancelled commits one
        strike, not three. Counting the whole figure would be the same error this method
        exists to prevent, only inverted: capital reserved against contracts that do not
        exist, refusing structures the account can plainly afford. When the ratio itself
        cannot be read the WHOLE reserve stands: over-reserving is a refusal, and this
        method's entire argument is that an unknown must never resolve to the number that
        frees money.
        """
        from ba2_common.core.types import OrderStatus
        from ba2_common.logger import logger

        terminal = OrderStatus.get_terminal_statuses()
        total = 0.0
        blind: List[str] = []
        for o in self.open_option_orders_book_wide():
            strategy = getattr(o, "option_strategy", None)
            raw = (o.data or {}).get("option_reserve") if isinstance(o.data, dict) else None
            reserve = None
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                value = float(raw)
                if math.isfinite(value):
                    reserve = value
            if reserve is not None and getattr(o, "status", None) in terminal:
                ordered = self._readable_positive_number(getattr(o, "quantity", None))
                filled = self._readable_positive_number(getattr(o, "filled_qty", None))
                if ordered is not None and filled is not None and filled < ordered:
                    reserve = reserve * (filled / ordered)
            if strategy in self.RESERVING_STRATEGIES:
                if reserve is None or reserve <= 0:
                    blind.append(
                        f"order {getattr(o, 'id', '?')} ({strategy} on "
                        f"{getattr(o, 'symbol', '?')}) is OPEN and must reserve capital, "
                        f"but its data['option_reserve'] is {raw!r} — an unreadable "
                        f"reserve is UNKNOWN, and unknown must not read as zero (that "
                        f"frees buying power that is already committed)")
                    continue
                total += reserve
            elif reserve is not None and reserve > 0:
                # Not a priced reserving name, but it recorded a reserve anyway. Honour
                # it: the money is spoken for whatever the row calls itself.
                total += reserve
        if blind:
            logger.error(
                f"Account {self.id}: {len(blind)} open option order(s) have an "
                f"unreadable buying-power reserve, so available option buying power is "
                f"UNKNOWN and every buying-power gate will refuse until it is repaired. "
                + "; ".join(blind))
        return ReservePool(total=total, unmeasurable=tuple(blind))

    def reserved_option_buying_power(self) -> float:
        """Sum of the reserves this account can actually READ, as a plain float.

        Kept for reporting and for backward compatibility. It is a LOWER BOUND: when
        ``reserved_option_buying_power_detail().unmeasurable`` is non-empty the true
        figure is higher by an unknown amount, which is precisely why the gate
        (``check_option_buying_power``) consults the detail and not this number.
        """
        return self.reserved_option_buying_power_detail().total

    def available_option_buying_power(self) -> Optional[float]:
        """Balance minus reserves, or ``None`` when either is unknown.

        ``None``, not ``0.0``. The previous ``self.get_balance() or 0.0`` turned an
        unreadable balance into a real number; it happened to fail closed, but "we could
        not read the balance" and "the balance is zero" are still different facts and
        only one of them is ever true.
        """
        pool = self.reserved_option_buying_power_detail()
        if not pool.is_measurable:
            return None
        bal = self.get_balance()
        if bal is None:
            return None
        return bal - pool.total

    def check_option_buying_power(self, required: float) -> bool:
        """True if `required` reserve fits in available buying power.

        A required reserve of zero always passes: reserving nothing needs no capacity,
        and refusing it would break the entire long/debit arm the moment one unrelated
        row lost its reserve. Anything above zero measured against an UNKNOWN pool
        refuses — "we cannot measure this" and "this is fine" must never be the same
        answer.
        """
        if required <= 0:
            return True
        available = self.available_option_buying_power()
        if available is None:
            return False
        return required <= available

    # --- Assignment capacity (the SECOND view of the same legs) -------------
    def short_put_assignment_exposure(self) -> "AssignmentExposure":
        """Cash this account would owe if EVERY open short put were assigned at once.

        A SECOND VIEW of the same order rows the reserve pool reads, answering a
        different question. ``reserved_option_buying_power`` asks "how much margin/cash
        is already spoken for?", priced the way a broker prices each structure — the
        full ``strike x 100`` for a ``cash_secured_put``, but only Reg-T naked margin
        (~20% of notional, floored at 10%) for a ``short_strangle``. This asks "could we
        take delivery?", which is one number for every short put alive: the strike.

        WHY THIS IS NOT SIMPLY ADDED TO THE RESERVE POOL. CSP, jade lizard and put ratio
        already reserve the full strike there. Charging them again in the same pool
        would spend the same cash twice against the same budget and refuse trades the
        account can plainly afford. The two totals are each measured against their own
        independently-derived budget — the pool against balance-minus-reserves, this
        against the balance itself — and neither subtracts the other.

        SHORT CALLS ARE EXCLUDED, deliberately. A short call assigned delivers shares
        and pays cash IN; it consumes share inventory, not cash. A covered call charged
        here would decline trades for an obligation that does not exist. (A *share*
        capacity notion for uncovered short calls is a real and separate feature —
        nothing here tracks share inventory — and is out of scope.)

        Netting is per contract symbol over EXECUTED orders (BUY ``+``, SELL ``−``), the
        same netting the close paths perform, so a short bought back stops counting.
        A submitted-but-unfilled SELL is added on top without netting: it can fill at any
        moment and can only ever ADD an obligation, whereas an unfilled BUY-to-close has
        closed nothing and must not hand capacity back early.

        A PARTIALLY FILLED SELL IS CHARGED ITS WHOLE ORDERED SIZE, the cash-side twin of
        the rule in ``shares_pledged_to_short_calls`` and fixed with it. ``PARTIALLY_FILLED``
        is an EXECUTED status, so the filled contracts net normally and the unfilled
        remainder joins the in-flight total under the rule above. Charging only the filled
        part priced a 3-contract cash-secured put with 1 filled at one strike instead of
        three, and admitted the next structure on capacity the next fill consumes. The two
        views deliberately read ONE query; leaving them inconsistent about the same window
        would be worse than either behaviour alone.

        ONCE THAT ORDER IS CANCELLED IT IS CHARGED EXACTLY WHAT IT FILLED — the fail-open
        twin of the same window, fixed with the pledge view for the same reason. Measured:
        the 3-contract CSP with 1 filled owed 45,000 while working and 0 the instant its
        status became ``CANCELED``, though one put was genuinely sold and can still be
        assigned. The remaining contract nets like any fill (so a buy-to-close relieves
        it) and grows no in-flight remainder, because nothing of that order is working.
        """
        from ba2_common.core.option_lifecycle import put_assignment_cost
        from ba2_common.core.types import OrderDirection, OrderStatus

        executed = OrderStatus.get_executed_statuses()
        terminal = OrderStatus.get_terminal_statuses()
        net: dict = {}            # contract symbol -> signed contracts (BUY +, SELL -)
        meta: dict = {}           # contract symbol -> a representative order row
        pending_shorts: list = []
        blind: List[str] = []

        for o in self.open_option_orders_book_wide():
            if o.status in terminal and not self._traded_something(o):
                # Dead and it printed nothing: no delivery obligation, and not in flight
                # either. Belt-and-braces for the same reason as the pledge view's copy —
                # this arithmetic must not depend on which rows the query happened to send.
                continue
            contract = getattr(o, "contract_symbol", None)
            if not contract:
                # A multi-leg PARENT carries no contract, no strike and no right — its
                # legs do, one row each. Skipping it is not a gap; counting it would be
                # a double count with no identity to attach.
                continue
            is_sell = o.side == OrderDirection.SELL
            if o.option_type is None:
                if is_sell:
                    blind.append(
                        f"order {getattr(o, 'id', '?')} ({contract}) is a SHORT option "
                        f"with no option type recorded — whether it is a put that can be "
                        f"assigned to us is unknown, and unknown must not resolve to "
                        f"'not a put'")
                # A BUY with no right cannot be netted off a short, which only ever
                # OVERSTATES the exposure. Conservative, so it is not an unknown.
                continue
            if o.option_type != OptionRight.PUT:
                continue
            raw_qty = o.filled_qty if o.filled_qty else o.quantity
            if is_sell and (raw_qty is None or float(raw_qty) <= 0):
                # A SHORT PUT with no usable size is an obligation of unknown size, and
                # `or 0.0` would price it at nothing — the same fail-open one field over.
                # A BUY is left to fall through to 0: it can only fail to relieve a
                # short, which overstates the bill, which is the safe direction.
                blind.append(
                    f"order {getattr(o, 'id', '?')} ({contract}) is a SHORT PUT with no "
                    f"usable quantity (filled_qty={o.filled_qty!r}, "
                    f"quantity={o.quantity!r}) — how many contracts could be assigned to "
                    f"us is unknown, and unknown is not zero")
                continue
            qty = float(raw_qty or 0.0)
            # A TERMINAL row reaching this line FILLED something (the others were skipped
            # at the top of the loop), and ``raw_qty`` already resolved to that filled
            # size. It is EXECUTED for that size — netted, so a buy-to-close relieves it —
            # and grows no in-flight remainder: a dead order is not working at the broker.
            if o.status in executed or o.status in terminal:
                net[contract] = net.get(contract, 0.0) + (-qty if is_sell else qty)
                meta[contract] = o
                if is_sell and o.status == OrderStatus.PARTIALLY_FILLED:
                    # The unfilled remainder of a partly-filled sell-to-open. See the
                    # docstring: ``qty`` above is only what FILLED, and the rest is still
                    # working at the broker under the in-flight rule. Priced through the
                    # SAME pending list, so one code path covers "never filled" and "half
                    # filled" and they cannot drift.
                    remainder, reason = self._working_remainder(
                        o, qty, what="PARTIALLY FILLED SHORT PUT", label=contract,
                        consequence="how many more contracts could be assigned to us")
                    if remainder is None:
                        blind.append(reason)
                    elif remainder > _ASSIGNMENT_EPS:
                        pending_shorts.append((o, remainder))
            elif is_sell:
                pending_shorts.append((o, qty))

        cost = 0.0
        contracts = 0.0
        for contract in sorted(net):
            held = net[contract]
            if held >= -_ASSIGNMENT_EPS:      # flat, or net LONG: nothing can be put to us
                continue
            o = meta[contract]
            leg = put_assignment_cost(o.strike, abs(held), getattr(o, "multiplier", None))
            if leg is None:
                blind.append(
                    f"order {getattr(o, 'id', '?')} ({contract}) is a held SHORT PUT but "
                    f"its strike ({o.strike!r}) / contract count ({held!r}) / multiplier "
                    f"({getattr(o, 'multiplier', None)!r}) cannot price delivery — the "
                    f"cash it would take to accept assignment is unmeasurable")
                continue
            cost += leg
            contracts += abs(held)

        for o, qty in pending_shorts:
            leg = put_assignment_cost(o.strike, qty, getattr(o, "multiplier", None))
            if leg is None:
                blind.append(
                    f"order {getattr(o, 'id', '?')} ({o.contract_symbol}) is an in-flight "
                    f"SHORT PUT but its strike ({o.strike!r}) / quantity ({o.quantity!r}) "
                    f"/ multiplier ({getattr(o, 'multiplier', None)!r}) cannot price "
                    f"delivery — the cash it would take to accept assignment is "
                    f"unmeasurable")
                continue
            cost += leg
            contracts += qty

        if blind:
            return AssignmentExposure(None, contracts, tuple(blind))
        return AssignmentExposure(cost, contracts, ())

    def assignment_capacity(self, additional_cost: float) -> "AssignmentCapacity":
        """Could this account still take delivery after adding ``additional_cost``?

        THE decision, with its reason. ``check_assignment_capacity`` is the yes/no view
        of this same method, so the gate and the message a refusal carries can never
        disagree about why.

        Measured against the BALANCE, never against ``available_option_buying_power()``:
        the reserve pool has already subtracted the very CSP strikes this total is
        charging, so netting the two would double-charge the same cash and refuse a
        fully funded wheel at exactly the size it is funded for.

        Exactly equal ADMITS. That is the same boundary every other cap in the option
        risk path uses (``check_option_buying_power``, and the deployment / leverage /
        naked rails in ``option_book``), and one gate with a different boundary is a
        trap nobody will remember. The safety margin belongs in the operator's cash
        figure, not smuggled into the comparison.

        Unknown ANYTHING refuses: an unmeasurable exposure, an unreadable balance, or a
        negative ``additional_cost`` (which would buy capacity nobody funded).
        """
        if additional_cost is None or additional_cost < 0:
            return AssignmentCapacity(
                False,
                f"{ASSIGNMENT_CAPACITY_REFUSAL}: the delivery cost of this structure is "
                f"{additional_cost!r} — an unknown or negative cost cannot be admitted "
                f"(a negative one would BUY capacity nobody funded).")
        exposure = self.short_put_assignment_exposure()
        if not exposure.is_measurable:
            return AssignmentCapacity(
                False,
                f"{ASSIGNMENT_CAPACITY_REFUSAL}: what this account already owes on "
                f"assignment cannot be measured, so whether it could take delivery of "
                f"one more short put is unknown — and unknown is not 'fine'. "
                + "; ".join(exposure.unmeasurable),
                candidate_cost=additional_cost,
                unmeasurable=exposure.unmeasurable)
        cash = self.get_balance()
        if cash is None:
            return AssignmentCapacity(
                False,
                f"{ASSIGNMENT_CAPACITY_REFUSAL}: the account balance could not be read, "
                f"so the cash available to take delivery on "
                f"{exposure.contracts:g} open short put contract(s) "
                f"(costing {exposure.cost:,.2f}) is unknown.",
                held_cost=exposure.cost,
                candidate_cost=additional_cost)
        ok = exposure.cost + additional_cost <= cash
        reason = "" if ok else (
            f"{ASSIGNMENT_CAPACITY_REFUSAL}: this account could not take delivery. "
            f"Assignment of every open short put would cost "
            f"{exposure.cost + additional_cost:,.2f} "
            f"({exposure.cost:,.2f} for the {exposure.contracts:g} contract(s) already "
            f"open + {additional_cost:,.2f} for this structure) against {cash:,.2f} of "
            f"cash. Close a short put or fund the account — the reserve pool is a "
            f"separate budget and repairing it will not help.")
        return AssignmentCapacity(ok, reason, held_cost=exposure.cost,
                                  candidate_cost=additional_cost, cash=cash)

    def check_assignment_capacity(self, additional_cost: float) -> bool:
        """Yes/no form of :meth:`assignment_capacity` — see it for the whole contract."""
        return self.assignment_capacity(additional_cost).ok

    def check_short_put_assignment_capacity(
        self, *, strike: Optional[float], contracts: Optional[float],
        multiplier: Optional[int] = 100,
    ) -> "AssignmentCapacity":
        """Capacity verdict for a CANDIDATE short put, priced from its LEG facts.

        The entry point every credit builder uses. It takes the leg (strike, how many
        contracts, the multiplier) rather than a dollar figure so no caller can fork the
        arithmetic: the price comes from ``option_lifecycle.put_assignment_cost``, the
        single definition the pure rail and the account-wide exposure already share.

        THE CANDIDATE IS INCLUDED IN THE SUM, which is the entire point — admitting the
        fifth short put because the first four fit is the defect this gate exists for.

        A candidate that cannot be priced REFUSES and says which field was missing. A
        structure whose delivery cost we cannot compute is not a structure that costs
        nothing.
        """
        from ba2_common.core.option_lifecycle import put_assignment_cost

        cost = put_assignment_cost(strike, contracts, multiplier)
        if cost is None:
            return AssignmentCapacity(
                False,
                f"{ASSIGNMENT_CAPACITY_REFUSAL}: this structure's delivery cost cannot "
                f"be priced from strike={strike!r}, contracts={contracts!r}, "
                f"multiplier={multiplier!r} — an unpriceable obligation must not be "
                f"admitted as a free one.")
        return self.assignment_capacity(cost)
