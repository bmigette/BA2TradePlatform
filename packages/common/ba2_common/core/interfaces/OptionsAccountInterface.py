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
from typing import Any, List, Optional, Tuple

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
#: refusal needs cash for delivery. This one needs SHARES: buy the missing shares of the
#: underlying, write fewer contracts, or close a short call to release the cover it
#: holds. Adding margin does nothing for it, and nothing it asks for helps the others.
COVER_REFUSAL = "UNCOVERED SHORT CALL"

#: The one strategy tag whose entire promise is "these contracts are covered by shares
#: this account holds". The cover guard enforces exactly that promise and no other:
#:
#: * a ``short_strangle``/``short_put`` is DELIBERATELY undefined-risk and is gated by
#:   PremiumSeller's ``undefined_risk_max_pct`` rail, not by share inventory;
#: * a ``bear_call_spread``'s short call is covered by a LONG CALL, not by shares;
#: * every close path submits under ``"close"`` (``option_lifecycle_service``,
#:   ``PremiumSeller.portfolio``, ``close_option_position``), so flattening is never
#:   gated on a cover reading — a refusal there would strand an open position.
COVERED_CALL_STRATEGY = "covered_call"

#: Shares one contract delivers when nothing says otherwise — and the value this method
#: stamps on every option row it writes. The cover guard reads it from the LEG when the
#: leg publishes one (an adjusted contract does not deliver 100 — OPT-L7) and falls back
#: to this only because ``OptionLeg`` carries no such field yet: the fallback is then not
#: a guess but the very number being persisted two blocks below.
DEFAULT_OPTION_MULTIPLIER = 100

#: Sentinel telling "this leg publishes NO multiplier field" apart from "this leg
#: publishes one and it is unreadable". The first is today's ``OptionLeg`` and takes the
#: platform default; the second is a damaged input and is a refusal. A plain
#: ``getattr(leg, "multiplier", None)`` collapses the two, and collapsing them either
#: refuses every covered call on the platform or accepts a leg whose multiplier is junk.
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
            parent.status = OrderStatus.ERROR
            parent.comment = f"{(parent.comment or '')} | option submit error: {str(e)[:200]}"
            update_instance(parent)
            return None

    def _refuse_uncovered_covered_call(self, legs: List[OptionLeg], quantity: int,
                                       option_strategy: Optional[str]) -> None:
        """Raise ``ValueError`` if a ``covered_call`` is not actually covered.

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

        WHAT IS *NOT* GUARDED, deliberately. Only the ``covered_call`` tag, because only
        that tag promises share cover (see ``COVERED_CALL_STRATEGY``). A short strangle is
        meant to be naked and is rationed by a different rail; a bear call spread is
        covered by its long call; and every close path submits under ``"close"``, so
        flattening is never blocked by a cover reading — refusing there would strand an
        open position that can no longer be exited, which is strictly worse than the
        entry this refuses. For the same reason a structure with no SHORT CALL leg at all
        (a buy-to-close mistagged ``covered_call``) needs no cover and never consults the
        feeds: it RELEASES cover rather than consuming it.

        LONG CALLS ARE NOT NETTED against the requirement. A covered call is by
        definition one short call against shares, not a spread; if a long call is present
        the tag is wrong, and over-requiring cover is the direction that fails safe.
        """
        from ba2_common.core.types import OrderDirection

        if (option_strategy or "").strip().lower() != COVERED_CALL_STRATEGY:
            return

        contracts = self._readable_positive_number(quantity)
        if contracts is None:
            raise ValueError(
                f"{COVER_REFUSAL}: this covered_call has no usable size "
                f"(quantity={quantity!r}), so how many shares it could have called away "
                f"cannot be worked out — and an obligation of unknown size must not be "
                f"written. No order, leg or transaction has been created.")

        # Grouped by underlying rather than summed flat: a covered call is one ticker, but
        # a stray leg on another name must not be covered by the first name's shares.
        required: dict = {}
        for leg in legs:
            if getattr(leg, "side", None) != OrderDirection.SELL:
                continue           # a LONG leg pledges nothing; a buy-to-close releases
            contract = getattr(leg, "contract_symbol", None) or "?"
            right = getattr(leg, "option_type", None)
            if right is None:
                raise ValueError(
                    f"{COVER_REFUSAL}: leg {contract} of this covered_call is a SHORT "
                    f"option with no option type recorded, so whether it is the CALL that "
                    f"has to be covered is unknown — and unknown must not resolve to 'not "
                    f"a call'. No order, leg or transaction has been created.")
            if right != OptionRight.CALL:
                continue           # a short put obliges CASH: the capacity gate's question
            underlying = (getattr(leg, "underlying", None) or "").strip().upper()
            if not underlying:
                raise ValueError(
                    f"{COVER_REFUSAL}: leg {contract} of this covered_call is a SHORT CALL "
                    f"with no underlying recorded, so there is no ticker whose shares can "
                    f"be counted as its cover. No order, leg or transaction has been "
                    f"created.")
            ratio = self._readable_positive_number(getattr(leg, "ratio_qty", 1))
            if ratio is None:
                raise ValueError(
                    f"{COVER_REFUSAL}: leg {contract} of this covered_call has an "
                    f"unusable ratio_qty ({getattr(leg, 'ratio_qty', None)!r}), and it is "
                    f"what sizes the leg (quantity * ratio_qty), so how many {underlying} "
                    f"shares it can call away is unknown. No order, leg or transaction "
                    f"has been created.")
            raw_multiplier = getattr(leg, "multiplier", _MULTIPLIER_ABSENT)
            if raw_multiplier is _MULTIPLIER_ABSENT:
                multiplier = float(DEFAULT_OPTION_MULTIPLIER)
            else:
                multiplier = self._readable_positive_number(raw_multiplier)
                if multiplier is None:
                    raise ValueError(
                        f"{COVER_REFUSAL}: leg {contract} of this covered_call reports "
                        f"multiplier {raw_multiplier!r}, so how many {underlying} shares "
                        f"one contract can call away is unknown — and it must NOT be "
                        f"assumed to be {DEFAULT_OPTION_MULTIPLIER}, because an ADJUSTED "
                        f"contract (post-split, post-merger) delivers a different number "
                        f"and guessing under-states exactly the cover this write needs. "
                        f"No order, leg or transaction has been created.")
            required[underlying] = (required.get(underlying, 0.0)
                                    + contracts * ratio * multiplier)

        for underlying in sorted(required):
            # Rounded UP, like the pledge itself: under-stating the requirement by one
            # share is the direction that leaves a contract uncovered.
            need = int(math.ceil(round(required[underlying], 6)))
            if need <= 0:
                continue
            held = self.held_shares_for_cover(underlying)
            pledged = self.shares_pledged_to_short_calls(underlying)
            if held is None:
                raise ValueError(
                    f"{COVER_REFUSAL}: how many {underlying} shares this account holds "
                    f"could not be measured — held_shares_for_cover() returned UNKNOWN, "
                    f"i.e. the POSITION FEED did not answer (the logged error above names "
                    f"the fetch or the row that failed). Whether this covered_call would "
                    f"be covered is therefore unknown, and writing a short call on an "
                    f"unknown is precisely how one goes naked. No order, leg or "
                    f"transaction has been created. This is NOT a shortfall: buying more "
                    f"shares will not clear it, repairing the feed will.")
            if pledged is None:
                raise ValueError(
                    f"{COVER_REFUSAL}: how many {underlying} shares are already pledged "
                    f"as cover for open short calls could not be measured — "
                    f"shares_pledged_to_short_calls() returned UNKNOWN, i.e. the OPTION "
                    f"BOOK did not answer (the logged error above names the unreadable "
                    f"row). The {held} shares held may already be spoken for and there is "
                    f"no way to find out, so this covered_call could be the second call "
                    f"written against the same lot. No order, leg or transaction has been "
                    f"created. Repair the option order book and retry.")
            free = held - pledged
            if free < need:
                raise ValueError(
                    f"{COVER_REFUSAL}: this covered_call writes {contracts:g} contract(s) "
                    f"on {underlying} and needs {need} shares of cover, but only {free} "
                    f"are free (the account holds {held}, with {pledged} already pledged "
                    f"to open short calls) — short by {need - free} share(s). No order, "
                    f"leg or transaction has been created. Buy the missing shares, write "
                    f"fewer contracts, or close a short call to release the cover it "
                    f"holds; adding margin does nothing for this one.")

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

    @abstractmethod
    def close_option_position(self, position: OptionPosition,
                              order_type: str = "limit",
                              limit_price: Optional[float] = None) -> Any:
        """Submit a closing order for a held option position (opposite intent)."""
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

        Routed through ``orders_where``/``transactions_where`` rather than a raw
        ``select`` because a raw select silently returns EMPTY while the SQL-less
        in-memory "dict trades" backtest store is active, and an empty book reads as a
        flat book at every gate.
        """
        from ba2_common.core.trade_store import orders_where, transactions_where
        from ba2_common.core.types import AssetClass, OrderStatus, TransactionStatus

        terminal = OrderStatus.get_terminal_statuses()
        option_orders = [o for o in orders_where(account_id=self.id,
                                                 not_statuses=terminal)
                         if o.asset_class == AssetClass.OPTION]
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
    #: family) is already benign everywhere; ``SQLAlchemyError`` is named here because the
    #: book is a DB read and a locked/failed database is a data condition, not a defect in
    #: this file.
    #:
    #: NOTE THE INVERSION relative to the seam guards. There, absorbing a locked database
    #: meant "carry on" and was PROVED to cancel protective legs. Here, absorbing means
    #: returning ``None``, which every caller must treat as a REFUSAL — the fail-CLOSED
    #: direction. Anything outside this tuple (a ``TypeError`` from a bad row shape, say)
    #: still propagates under ``BA2_ERROR_MODE=enforce``, because a defect that quietly
    #: answers "unmeasurable" forever is a gate that has silently stopped working.
    @staticmethod
    def _cover_benign_errors() -> Tuple[type, ...]:
        from sqlalchemy.exc import SQLAlchemyError
        return (SQLAlchemyError,)

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

        THE MULTIPLIER IS READ PER CONTRACT AND NEVER ASSUMED TO BE 100 (OPT-L7). An
        adjusted contract — post-split, post-merger — can deliver a different number of
        shares, and a hard-coded 100 would under-report the pledge on precisely the
        contract whose oddity nobody remembers. A contract whose multiplier cannot be read
        (or whose rows disagree about it) is therefore UNMEASURABLE, not a guess.

        Netted per contract symbol over EXECUTED orders (SELL ``−``, BUY ``+``), the same
        netting the exposure view and the close paths perform, so a call bought back stops
        pledging. An unfilled SELL-to-open is added on top WITHOUT netting: it can fill at
        any moment and can only ever ADD an obligation, whereas an unfilled buy-to-close
        has closed nothing and must not hand the cover back early.

        WHAT MAKES A ROW UNMEASURABLE — each case is "this row could be a short call on
        the ticker you asked about, and I cannot rule it out":

        * a SELL with no ``option_type``      — it might be the call;
        * a SELL CALL with no ``underlying_symbol`` — it might be on this ticker (there is
          no fallback to ``symbol``: on a leg child that field holds the OCC contract
          string, which would never match and would report the shares as free);
        * a SELL CALL with no usable quantity — an obligation of unknown size;
        * a held short call with no readable multiplier.

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
        net: dict = {}          # contract -> signed contracts, EXECUTED only (SELL −)
        pending: dict = {}      # contract -> in-flight SELL contracts, never netted
        mults: dict = {}        # contract -> set of readable multipliers seen
        mult_blind: dict = {}   # contract -> a reason its multiplier is unreadable
        blind: List[str] = []

        for o in book:
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
            if right is None:
                if is_sell:
                    blind.append(
                        f"order {getattr(o, 'id', '?')} ({contract}) is a SHORT option with "
                        f"no option type recorded — whether it is a CALL that pledges "
                        f"{wanted} shares is unknown, and unknown must not resolve to 'not "
                        f"a call'")
                continue
            if not row_underlying:
                if is_sell:
                    blind.append(
                        f"order {getattr(o, 'id', '?')} ({contract}) is a SHORT CALL with no "
                        f"underlying recorded — whether it is written on {wanted} is "
                        f"unknown, and unknown must not resolve to 'some other ticker'")
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

            if o.status in executed:
                net[contract] = net.get(contract, 0.0) + (-qty if is_sell else qty)
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
        """
        from ba2_common.logger import logger

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
        """
        from ba2_common.core.option_lifecycle import put_assignment_cost
        from ba2_common.core.types import OrderDirection, OrderStatus

        executed = OrderStatus.get_executed_statuses()
        net: dict = {}            # contract symbol -> signed contracts (BUY +, SELL -)
        meta: dict = {}           # contract symbol -> a representative order row
        pending_shorts: list = []
        blind: List[str] = []

        for o in self.open_option_orders_book_wide():
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
            if o.status in executed:
                net[contract] = net.get(contract, 0.0) + (-qty if is_sell else qty)
                meta[contract] = o
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
