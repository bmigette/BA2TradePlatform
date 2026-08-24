"""Options capability interface — a sibling mixin to AccountInterface.

Brokers that support options inherit BOTH, e.g.:
    class AlpacaAccount(AccountInterface, OptionsAccountInterface): ...

Capability detection elsewhere should use isinstance(account, OptionsAccountInterface).
The concrete submit_option_order() owns TradingOrder/Transaction persistence and
delegates the broker call to the abstract _submit_option_order_impl().
"""
import math
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, List, Optional

from ba2_common.core.option_types import OptionContract, OptionQuote, OptionLeg, OptionPosition
from ba2_common.core.types import OptionRight


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

        Raises ValueError if the legs span more than one expiry (see the guard below).
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
            multiplier=100,
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
                    multiplier=100,
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
        dropped) — still ~5x cheaper than the old full strike*100 cash proxy."""
        if strike is None or strike <= 0:
            return 0.0
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
        "bear_call_spread", "credit_spread",
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
        ``ZERO_RESERVE_STRATEGIES`` and answer 0.0 by name."""
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
            if strike is None:
                return 0.0
            return strike * 100.0 * quantity
        if strategy in ("bear_call_spread", "credit_spread"):
            if spread_width is None or net_credit is None:
                return 0.0
            max_loss = (spread_width - net_credit)
            return max(0.0, max_loss) * 100.0 * quantity
        if strategy in ("short_straddle", "short_strangle", "naked_put"):
            # NAKED short premium: reserve the Reg-T naked-option margin, not full cash.
            if strike is None:
                return 0.0
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
            if strike is None:
                return 0.0
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
            if strike is None or spread_width is None:
                return 0.0
            credit = net_credit if net_credit is not None else 0.0
            per_contract = strike * 100.0 + spread_width * 100.0 - credit * 100.0
            return max(0.0, per_contract) * quantity
        if strategy in ("iron_condor", "call_butterfly", "debit_spread"):
            if spread_width is None:
                return 0.0
            credit = net_credit if net_credit is not None else 0.0
            return max(0.0, (spread_width - credit)) * 100.0 * quantity
        # Unreachable while RESERVING_STRATEGIES and the branches above agree. It is a
        # raise and not a `return 0.0` because the two CAN drift — someone adds a name
        # to the set and forgets the branch — and the cost of that drift being silent is
        # a structure that reserves nothing and is therefore always affordable.
        raise ValueError(
            f"option_reserve_required({strategy!r}): listed in RESERVING_STRATEGIES but "
            f"no branch prices it — refusing to fall back to a zero reserve.")

    def reserved_option_buying_power(self) -> float:
        """Sum of stored reserves across this account's OPEN short-premium option positions.

        A reserve belongs to the POSITION, not to the order row that created it: the broker
        frees the margin/cash the moment the structure is flattened, so this must too.

        Previously this summed ``data["option_reserve"]`` over every order not in a TERMINAL
        status — but ``FILLED`` is NOT terminal (see ``OrderStatus.get_terminal_statuses``),
        and nothing ever cleared the field or terminalised a filled entry order. The reserve
        was therefore a ONE-WAY RATCHET: every credit/naked structure ever opened consumed
        buying power for the remainder of the run, even long after it closed. On the options
        grid's $20k account that exhausted BP after 1-3 structures, which is why the RESERVING
        groups (OS2/OS3) capped out at 10-20 trades all clustered in the run's opening weeks
        while the non-reserving debit groups (OS1/OS4) traded 43-214 times over the identical
        window — and why the GA appeared to "win by barely trading" (it could not trade).

        Now a reserve counts only while its owning transaction is still open. WAITING/OPENED/
        CLOSING all still hold the position (a submitted-but-unfilled close has not freed
        anything yet); CLOSED/FAILED release it. A reserve-carrying order with no transaction
        yet (submitted, not linked) is still counted — that capital is genuinely in flight.
        """
        from ba2_common.core.trade_store import orders_where, transactions_where
        from ba2_common.core.types import AssetClass, OrderStatus, TransactionStatus

        terminal = OrderStatus.get_terminal_statuses()
        unlinked_total = 0.0
        linked: list = []  # (transaction_id, reserve)
        for o in orders_where(account_id=self.id, not_statuses=terminal):
            if o.asset_class != AssetClass.OPTION:
                continue
            reserve = float((o.data or {}).get("option_reserve", 0) or 0)
            if reserve <= 0:
                continue
            if o.transaction_id is None:
                unlinked_total += reserve
            else:
                linked.append((o.transaction_id, reserve))
        if not linked:
            return unlinked_total
        # One bulk lookup of the OPEN book (small — bounded by held positions), not N queries.
        live_ids = {
            t.id for t in transactions_where(
                not_statuses=(TransactionStatus.CLOSED, TransactionStatus.FAILED))
        }
        return unlinked_total + sum(r for txn_id, r in linked if txn_id in live_ids)

    def available_option_buying_power(self) -> float:
        bal = self.get_balance() or 0.0
        return bal - self.reserved_option_buying_power()

    def check_option_buying_power(self, required: float) -> bool:
        """True if `required` reserve fits in available buying power."""
        if required <= 0:
            return True
        return required <= self.available_option_buying_power()
