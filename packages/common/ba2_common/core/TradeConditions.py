"""
TradeConditions - Core component for evaluating trading conditions

This module provides base classes and implementations for evaluating various trading conditions
that can be used in rulesets and automated trading decisions.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Any, Dict
from datetime import date, datetime, time as _time, timezone, timedelta
import operator

from ba2_common.core.interfaces import AccountInterface
from ba2_common.core.models import TradingOrder, ExpertRecommendation
from ba2_common.core.types import OrderRecommendation, ExpertEventType, RiskLevel, TimeHorizon
from ba2_common.core.db import get_db
from ba2_common.logger import logger
from sqlmodel import select
from ba2_common.core.failure_modes import absorb_if_benign
# The established vocabulary for "the broker's position fetch FAILED" — reused rather
# than re-invented so the engine, the live service, the UI and the conditions all raise
# and catch the same class. See its docstring for the 2026-07-03 incident.
from ba2_common.core.portfolio_allocation import PositionFetchFailed
# The earnings-event STAMP CONTRACT (design 2026-08-31 leaps-grid S9): the key paths the
# ranking expert writes and the two O_ERN timing conditions below read back. Shared with
# TradeActions, which stamps the event date onto the entry order at submit.
from ba2_common.core.earnings_stamp import (
    order_event_date,
    stamped_days_to_earnings,
)


# --- Provider-injection seam -------------------------------------------------
# ba2_common must NOT import ba2_providers (or fmpsdk). The few conditions that
# need market data resolve a provider through a host-injected resolver. The live
# platform / backtest calls set_provider_resolver(get_provider) at startup; until
# then any data-driven condition raises a loud, explicit error rather than
# silently importing a provider package.
_provider_resolver = None

#: Returned by ``TradeCondition._simulated_as_of_date`` when the account ADVERTISES a
#: simulated clock (``_as_of_date``) that could not be read. Deliberately distinct from
#: ``None``, which means "live — there is no simulated clock and the wall clock is correct".
#: Collapsing the two would let a broken backtest clock silently degrade into wall-clock
#: reads, which is the lookahead this sentinel exists to prevent.
AS_OF_UNAVAILABLE = object()


def set_provider_resolver(fn):
    """Inject the provider resolver used by data-driven conditions.

    fn(category: str, name: str, **kwargs) -> provider instance, matching the
    ba2_providers.get_provider signature. Injected by the host app at startup.
    """
    global _provider_resolver
    _provider_resolver = fn


def get_provider_resolver():
    """Return the injected provider resolver (or None if not configured)."""
    return _provider_resolver


def _is_missing(value) -> bool:
    """True for None and for NaN, without importing pandas/numpy into ba2_common.

    ``x != x`` is the NaN identity and holds for float('nan'), numpy scalars and pandas NA
    alike. Written out because ``if not value`` would also swallow a legitimate 0 -- and a
    zero VOLUME is a real measurement (the name did not trade), which the callers must be able
    to tell apart from a missing one.
    """
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:  # noqa: BLE001 - an exotic type that cannot be compared is not a number
        return True


def _get_provider(category, name, **kwargs):
    """Resolve a provider via the injected resolver; raise if not configured."""
    if _provider_resolver is None:
        raise RuntimeError(
            "TradeConditions provider resolver not configured. The host app must "
            "call ba2_common.core.TradeConditions.set_provider_resolver(get_provider) "
            "at startup before evaluating data-driven conditions."
        )
    return _provider_resolver(category, name, **kwargs)


class TradeCondition(ABC):
    """
    Base class for all trading conditions.
    
    Provides common functionality for evaluating trading conditions based on:
    - Account state
    - Instrument information  
    - Current trade recommendation
    - Existing orders
    """
    
    def __init__(self, account: AccountInterface, instrument_name: str, 
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        """
        Initialize the trade condition.
        
        Args:
            account: Account interface for accessing account data
            instrument_name: Name of the instrument being evaluated
            expert_recommendation: The expert recommendation being evaluated
            existing_order: Optional existing order related to this evaluation
        """
        self.account = account
        self.instrument_name = instrument_name
        self.expert_recommendation = expert_recommendation
        self.existing_order = existing_order
        
    @abstractmethod
    def evaluate(self) -> bool:
        """
        Evaluate the condition and return True/False.
        
        Returns:
            bool: True if condition is met, False otherwise
        """
        pass
    
    @abstractmethod
    def get_description(self) -> str:
        """
        Get a human-readable description of what this condition checks.
        
        Returns:
            str: Description of the condition
        """
        pass
    
    def get_previous_recommendations(self, expert_instance_id: int, limit: int = 10) -> List[ExpertRecommendation]:
        """
        Get previous recommendations for this expert and instrument.
        
        Args:
            expert_instance_id: ID of the expert instance
            limit: Maximum number of recommendations to return
            
        Returns:
            List of previous recommendations ordered by creation date (newest first)
        """
        try:
            from ba2_common.core.trade_store import inmem_trades_active
            if inmem_trades_active():
                # BT store: ExpertRecommendation is an in-mem model (see trade_store.
                # IN_MEM_MODELS) — a raw ``select()`` bypasses it and would silently return
                # nothing (rows never touch the real :memory: SQLite table). The per-run
                # store only ever holds THIS backtest's recommendations, so an unfiltered
                # fetch-then-filter-in-Python is cheap (unlike the unbounded live table).
                from ba2_common.core.db import get_all_instances
                matches = [
                    r for r in get_all_instances(ExpertRecommendation)
                    if r.instance_id == expert_instance_id and r.symbol == self.instrument_name
                ]
                matches.sort(key=lambda r: r.created_at, reverse=True)
                return matches[:limit]

            with get_db() as session:
                statement = (
                    select(ExpertRecommendation)
                    .where(
                        ExpertRecommendation.instance_id == expert_instance_id,
                        ExpertRecommendation.symbol == self.instrument_name
                    )
                    .order_by(ExpertRecommendation.created_at.desc())
                    .limit(limit)
                )

                recommendations = session.exec(statement).all()
                return list(recommendations)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error getting previous recommendations: {e}", exc_info=True)
            return []
    
    def get_current_position(self) -> Optional[float]:
        """
        Get current position quantity for the instrument.

        ``get_positions()`` is TRI-STATE (see ``ReadOnlyAccountInterface.get_positions``):
        a list of positions, ``[]`` for a CONFIRMED flat account, and ``None`` when the
        FETCH ITSELF FAILED. This method collapses only the first two into a quantity;
        the third is raised, because "we could not find out" must never be reported with
        the same value as "we looked and there is nothing".

        It used to return ``None`` for both. During a positions outage that made
        ``has_account_position()`` False and ``HasNoPositionAccountCondition`` TRUE, so a
        live "when there is no position, BUY" ruleset opened a DUPLICATE position on top
        of one the broker was already holding. Same family as the 2026-07-03 incident, in
        the opposite direction: there an unverified book mass-CLOSED, here it BUYS.

        Returns:
            Position quantity (positive long / negative short) for this instrument, or
            ``None`` when the fetch SUCCEEDED and this instrument is simply not held.

        Raises:
            PositionFetchFailed: the position book is UNVERIFIED. Callers must decide,
                and every caller must decide NOT to act.
        """
        try:
            positions = self.account.get_positions()
        except Exception as e:
            # A transport failure (OSError covers ConnectionError / socket.gaierror / timeouts)
            # is a GENUINE runtime condition at this site, named per failure_modes' "say what
            # you expect" rule; anything else is a defect and still propagates under enforce.
            absorb_if_benign(e, OSError)
            logger.error(
                f"Position fetch RAISED for {self.instrument_name}: {e} — the account's "
                f"position book is unverified", exc_info=True)
            raise PositionFetchFailed(
                f"get_positions() raised while checking {self.instrument_name}: {e}") from e

        if positions is None:
            # The documented failure signal. Every broker adapter is required to report an
            # outage this way rather than as [] (a lie that reads as "the account is flat").
            logger.error(
                f"Position fetch FAILED for {self.instrument_name}: "
                f"{type(self.account).__name__}.get_positions() returned None "
                f"(fetch failure, NOT a flat account) — position-dependent conditions "
                f"must refuse to fire")
            raise PositionFetchFailed(
                f"get_positions() returned None while checking {self.instrument_name}")

        for position in positions:
            # Dict-shaped books are real (some adapters/tests hand back plain dicts). The old
            # `hasattr(position, 'symbol')` test silently skipped every one of them and
            # answered "no position" — its own silent wrong answer.
            if isinstance(position, dict):
                symbol, qty = position.get('symbol'), position.get('qty')
            else:
                symbol, qty = getattr(position, 'symbol', None), getattr(position, 'qty', None)
            if symbol == self.instrument_name:
                return qty
        return None


    def get_current_price(self) -> Optional[float]:
        """
        Get current market price for the instrument.

        Returns:
            Current price or None if unavailable
        """
        try:
            return self.account.get_instrument_current_price(self.instrument_name)
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error getting current price: {e}", exc_info=True)
            return None

    # --- the evaluation clock -------------------------------------------------------
    # A condition that reads the WALL CLOCK in a backtest does not merely lose accuracy,
    # it fabricates signal: the simulated bar can be years before ``date.today()``, and a
    # historical fetch left unclamped returns the whole run window, so a "recent high" or
    # a "days to earnings" is computed from the simulated FUTURE. Both helpers below are
    # duck-typed on the account exactly as ``TradeActions._today()`` is, so LIVE behaviour
    # is byte-identical (no ``_as_of_date`` -> nothing changes).

    def _simulated_as_of_date(self) -> Any:
        """The BACKTEST bar's calendar date, ``None`` in live, or ``AS_OF_UNAVAILABLE``.

        ``BacktestAccount`` exposes its simulated bar via ``_as_of_date()``; a live account
        has no such attribute and its wall clock IS the right answer.

        Unlike ``TradeActions._today()`` this deliberately does NOT fall back to
        ``date.today()`` when the accessor exists but fails or returns None. For an action
        the wall clock is a degraded answer; for a CONDITION it is the lookahead bug itself,
        so the caller must treat the condition as unevaluable rather than measure against a
        date the simulation never reached.
        """
        accessor = getattr(self.account, "_as_of_date", None)
        if not callable(accessor):
            return None  # live: no simulated clock
        try:
            as_of = accessor()
        except Exception as e:  # noqa: BLE001 — a broken sim clock must not read as "live"
            # Deliberately NOT absorb_if_benign: this is not error absorption, it is a
            # measurement that failed, translated into the tri-state result the callers
            # already handle — the same discipline as DaysToExpiryCondition._resolve_expiry
            # returning (None, reason). Re-raising here would escape evaluate()'s own
            # absorb_if_benign in strict mode and abort the WHOLE rule evaluation for the
            # bar, taking every other gate with it, to report one unreadable clock.
            logger.error(
                f"Simulated clock (_as_of_date) failed for {self.instrument_name}; refusing "
                f"to substitute the wall clock: {e}", exc_info=True)
            return AS_OF_UNAVAILABLE
        if as_of is None:
            logger.error(
                f"Simulated clock (_as_of_date) returned None for {self.instrument_name}; "
                f"refusing to substitute the wall clock")
            return AS_OF_UNAVAILABLE
        return as_of.date() if isinstance(as_of, datetime) else as_of

    def _as_of_fetch_end(self):
        """``(end_date, ok)`` — the as-of ceiling for a historical market-data fetch.

        ``end_date`` is ``None`` in LIVE. That is not laziness: ``end_date=None`` is the
        spelling ``MarketDataProviderInterface._is_latest_request`` recognises as "give me
        the latest", which is what permits the parquet cache top-up. Passing an explicit
        end-of-day stamp instead would suppress the top-up whenever the local date is behind
        UTC (US evenings), so live keeps the exact call it makes today.

        In a BACKTEST it is the END of the simulated bar's day, so the bar itself is included
        whatever time of day its rows are stamped, and nothing after it can be.

        ``ok`` is False only when a simulated clock exists and could not be read — the caller
        must then not fetch at all.
        """
        as_of = self._simulated_as_of_date()
        if as_of is AS_OF_UNAVAILABLE:
            return None, False
        if as_of is None:
            return None, True
        return datetime.combine(as_of, _time.max, tzinfo=timezone.utc), True

    def _evaluation_date(self) -> Optional[date]:
        """The condition's "today" as a concrete ``date``, or ``None`` when unmeasurable.

        The simulated bar in a backtest, ``date.today()`` in live. ``None`` only when the
        account advertises a simulated clock that could not be read — in which case the
        caller must report the condition UNEVALUABLE rather than measure a duration against
        a date the simulation never reached.

        Use this (not ``date.today()``) for anything that counts days: in a backtest the
        wall clock is not "now", it is years after the last bar, so a wall-clock duration is
        the same wrong number on every bar of every year.
        """
        as_of = self._simulated_as_of_date()
        if as_of is AS_OF_UNAVAILABLE:
            return None
        return date.today() if as_of is None else as_of


    def has_expert_position(self) -> bool:
        """
        Check if this expert has an open position for this instrument by checking transactions.
        
        Returns:
            True if expert has open transactions for this instrument, False otherwise
        """
        try:
            from ba2_common.core.trade_store import transactions_where
            from ba2_common.core.types import TransactionStatus

            expert_id = self.expert_recommendation.instance_id

            # Check for open transactions for this expert and instrument
            open_transactions = transactions_where(
                expert_id=expert_id, symbol=self.instrument_name,
                status=TransactionStatus.OPENED)
            return len(open_transactions) > 0

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error checking expert position for {self.instrument_name}: {e}", exc_info=True)
            return False
    
    def get_actual_value_display(self) -> Optional[str]:
        """
        Get a human-readable string of the actual value evaluated by this condition.

        Returns:
            Formatted string of the actual value, or None if not available
        """
        return None

    def has_account_position(self) -> bool:
        """
        Check if there's an open position for this instrument at the account level.
        This is the original account-level position check behavior.

        DELIBERATELY NOT TOTAL. ``PositionFetchFailed`` propagates instead of being
        flattened into ``False``, because a bool cannot carry "unknown" and every
        collapse of unknown-to-a-bool picks a direction that is wrong half the time:
        ``False`` fires "no position -> BUY" (duplicate position), ``True`` fires
        "has position -> CLOSE" (blind close). Callers must handle the third state
        explicitly and refuse to act.

        Returns:
            True if the account holds a non-zero position in this instrument.

        Raises:
            PositionFetchFailed: the position book is UNVERIFIED — see
                :meth:`get_current_position`.
        """
        position = self.get_current_position()
        return position is not None and position != 0


class FlagCondition(TradeCondition):
    """
    Base class for flag-based (boolean) conditions.
    """
    pass


class CompareCondition(TradeCondition):
    """
    Base class for comparison-based conditions.
    """
    
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, operator_str: str, value: float,
                 existing_order: Optional[TradingOrder] = None):
        """
        Initialize comparison condition.
        
        Args:
            account: Account interface
            instrument_name: Instrument name
            expert_recommendation: Expert recommendation
            operator_str: Comparison operator ('>', '<', '>=', '<=', '==', '!=')
            value: Value to compare against
            existing_order: Optional existing order
        """
        super().__init__(account, instrument_name, expert_recommendation, existing_order)
        self.operator_str = operator_str
        self.value = value
        self.calculated_value = None  # Store the actual calculated value
        
        # Map operator strings to functions
        self.operator_map = {
            '>': operator.gt,
            '<': operator.lt,
            '>=': operator.ge,
            '<=': operator.le,
            '==': operator.eq,
            '!=': operator.ne
        }
        
        if operator_str not in self.operator_map:
            raise ValueError(f"Invalid operator: {operator_str}")
            
        self.operator_func = self.operator_map[operator_str]
    
    def get_calculated_value(self) -> Optional[float]:
        """
        Get the last calculated value from condition evaluation.

        Returns:
            The calculated value or None if not yet evaluated
        """
        return self.calculated_value

    def get_actual_value_display(self) -> Optional[str]:
        """Return formatted calculated value. Subclasses override for specific formatting."""
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.2f}"


# Flag Condition Implementations

class BearishCondition(FlagCondition):
    """Check if market sentiment is bearish."""

    def evaluate(self) -> bool:
        try:
            # Check if current recommendation is bearish (SELL)
            return self.expert_recommendation.recommended_action == OrderRecommendation.SELL

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating bearish condition: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        """Get description of bearish condition."""
        return f"Check if current recommendation is bearish (SELL) for {self.instrument_name}"

    def get_actual_value_display(self) -> Optional[str]:
        action = getattr(self.expert_recommendation, 'recommended_action', None)
        return f"Recommendation: {action.value}" if action else None


class BullishCondition(FlagCondition):
    """Check if market sentiment is bullish."""

    def evaluate(self) -> bool:
        try:
            # Check if current recommendation is bullish (BUY)
            return self.expert_recommendation.recommended_action == OrderRecommendation.BUY

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating bullish condition: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        """Get description of bullish condition."""
        return f"Check if current recommendation is bullish (BUY) for {self.instrument_name}"

    def get_actual_value_display(self) -> Optional[str]:
        action = getattr(self.expert_recommendation, 'recommended_action', None)
        return f"Recommendation: {action.value}" if action else None


class HasNoPositionCondition(FlagCondition):
    """Check if this expert has no open position for the instrument (expert-level check based on transactions)."""

    def evaluate(self) -> bool:
        self._has_position = self.has_expert_position()
        return not self._has_position

    def get_description(self) -> str:
        """Get description of no position condition."""
        return f"Check if this expert has no open position for {self.instrument_name} (based on transactions)"

    def get_actual_value_display(self) -> Optional[str]:
        has_pos = getattr(self, '_has_position', None)
        if has_pos is None:
            return None
        return f"Position found: {'Yes' if has_pos else 'No'}"


class HasPositionCondition(FlagCondition):
    """Check if this expert has an open position for the instrument (expert-level check based on transactions)."""

    def evaluate(self) -> bool:
        self._has_position = self.has_expert_position()
        return self._has_position

    def get_description(self) -> str:
        return f"Check if this expert has an open position for {self.instrument_name} (based on transactions)"

    def get_actual_value_display(self) -> Optional[str]:
        has_pos = getattr(self, '_has_position', None)
        if has_pos is None:
            return None
        return f"Position found: {'Yes' if has_pos else 'No'}"


class HasBuyPositionCondition(FlagCondition):
    """Check if this expert has an open BUY (long) position for the instrument."""
    def evaluate(self) -> bool:
        try:
            from ba2_common.core.trade_repository import get_trade_repository
            from ba2_common.core.types import OrderDirection

            # Repository, never a raw select(): Transaction is an IN_MEM_MODEL, so under the
            # backtest store a select() returns EMPTY instead of raising and this condition
            # would be permanently False in every GA trial while working live. See
            # trade_repository's module docstring for the measured case.
            open_txns = get_trade_repository().open_transactions(
                expert_id=self.expert_recommendation.instance_id,
                symbol=self.instrument_name, side=OrderDirection.BUY,
            )
            self._has_position = len(open_txns) > 0
            return self._has_position
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error checking BUY position for {self.instrument_name}: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if this expert has an open BUY position for {self.instrument_name}"

    def get_actual_value_display(self) -> Optional[str]:
        has_pos = getattr(self, '_has_position', None)
        if has_pos is None:
            return None
        return f"BUY position found: {'Yes' if has_pos else 'No'}"


class HasSellPositionCondition(FlagCondition):
    """Check if this expert has an open SELL (short) position for the instrument."""
    def evaluate(self) -> bool:
        try:
            from ba2_common.core.trade_repository import get_trade_repository
            from ba2_common.core.types import OrderDirection

            # See HasBuyPositionCondition: raw select() is blind to the in-memory BT store.
            open_txns = get_trade_repository().open_transactions(
                expert_id=self.expert_recommendation.instance_id,
                symbol=self.instrument_name, side=OrderDirection.SELL,
            )
            self._has_position = len(open_txns) > 0
            return self._has_position
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error checking SELL position for {self.instrument_name}: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if this expert has an open SELL position for {self.instrument_name}"

    def get_actual_value_display(self) -> Optional[str]:
        has_pos = getattr(self, '_has_position', None)
        if has_pos is None:
            return None
        return f"SELL position found: {'Yes' if has_pos else 'No'}"


# Account-level Position Conditions
class AccountPositionCondition(FlagCondition):
    """Shared fail-safe wiring for the two account-level position conditions.

    Both are FALSE when the position book is unverified. That is the only direction
    that is safe for both of them at once: an outage must not read as "no position"
    (which opens a duplicate on top of a real holding) NOR as "has position" (which
    closes against a book nobody confirmed). Triggers are ANDed and never negated by
    the evaluator, so a False condition simply means the rule does not fire.
    """

    def _has_account_position_or_none(self) -> Optional[bool]:
        """True/False when the book is CONFIRMED, None when the fetch failed."""
        self._fetch_failed = False
        try:
            self._has_position = self.has_account_position()
        except PositionFetchFailed as e:
            self._fetch_failed = True
            self._has_position = None
            logger.error(
                f"{type(self).__name__} for {self.instrument_name} evaluates FALSE: the "
                f"account position book is UNVERIFIED ({e}). An unverified book is not an "
                f"empty book — refusing to act rather than guess.")
        return self._has_position

    def get_actual_value_display(self) -> Optional[str]:
        if getattr(self, '_fetch_failed', False):
            # Must not read like a confirmed "No" — that conflation is the bug.
            return "Account position: UNKNOWN (broker position fetch failed)"
        has_pos = getattr(self, '_has_position', None)
        if has_pos is None:
            return None
        return f"Account position found: {'Yes' if has_pos else 'No'}"


class HasNoPositionAccountCondition(AccountPositionCondition):
    """Check if there's no open position for the instrument at the account level."""

    def evaluate(self) -> bool:
        has_position = self._has_account_position_or_none()
        if has_position is None:
            return False        # unknown != "no position"
        return not has_position

    def get_description(self) -> str:
        """Get description of account-level no position condition."""
        return f"Check if account has no open position for {self.instrument_name} (account-level)"


class HasPositionAccountCondition(AccountPositionCondition):
    """Check if there's an open position for the instrument at the account level."""

    def evaluate(self) -> bool:
        has_position = self._has_account_position_or_none()
        if has_position is None:
            return False        # unknown != "has position"
        return has_position

    def get_description(self) -> str:
        return f"Check if account has an open position for {self.instrument_name} (account-level)"

# Time Horizon Flag Conditions
class LongTermCondition(FlagCondition):
    """Check if expert recommendation is long term."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.time_horizon == TimeHorizon.LONG_TERM
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating long term condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} is LONG_TERM"

    def get_actual_value_display(self) -> Optional[str]:
        horizon = getattr(self.expert_recommendation, 'time_horizon', None)
        return f"Time horizon: {horizon.value}" if horizon else "Time horizon: N/A"

class MediumTermCondition(FlagCondition):
    """Check if expert recommendation is medium term."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.time_horizon == TimeHorizon.MEDIUM_TERM
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating medium term condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} is MEDIUM_TERM"

    def get_actual_value_display(self) -> Optional[str]:
        horizon = getattr(self.expert_recommendation, 'time_horizon', None)
        return f"Time horizon: {horizon.value}" if horizon else "Time horizon: N/A"

class ShortTermCondition(FlagCondition):
    """Check if expert recommendation is short term."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.time_horizon == TimeHorizon.SHORT_TERM
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating short term condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} is SHORT_TERM"

    def get_actual_value_display(self) -> Optional[str]:
        horizon = getattr(self.expert_recommendation, 'time_horizon', None)
        return f"Time horizon: {horizon.value}" if horizon else "Time horizon: N/A"


# Current Rating Flag Conditions
class CurrentRatingPositiveCondition(FlagCondition):
    """Check if current recommendation is positive (BUY)."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.recommended_action == OrderRecommendation.BUY
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating current rating positive condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if current recommendation for {self.instrument_name} is BUY (positive)"

    def get_actual_value_display(self) -> Optional[str]:
        action = getattr(self.expert_recommendation, 'recommended_action', None)
        return f"Current rating: {action.value}" if action else None


class CurrentRatingNeutralCondition(FlagCondition):
    """Check if current recommendation is neutral (HOLD)."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.recommended_action == OrderRecommendation.HOLD
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating current rating neutral condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if current recommendation for {self.instrument_name} is HOLD (neutral)"

    def get_actual_value_display(self) -> Optional[str]:
        action = getattr(self.expert_recommendation, 'recommended_action', None)
        return f"Current rating: {action.value}" if action else None


class CurrentRatingNegativeCondition(FlagCondition):
    """Check if current recommendation is negative (SELL)."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.recommended_action == OrderRecommendation.SELL
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating current rating negative condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if current recommendation for {self.instrument_name} is SELL (negative)"

    def get_actual_value_display(self) -> Optional[str]:
        action = getattr(self.expert_recommendation, 'recommended_action', None)
        return f"Current rating: {action.value}" if action else None


class CurrentRatingOverweightCondition(FlagCondition):
    """Check if current recommendation is OVERWEIGHT."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.recommended_action == OrderRecommendation.OVERWEIGHT
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating current rating overweight condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if current recommendation for {self.instrument_name} is OVERWEIGHT"

    def get_actual_value_display(self) -> Optional[str]:
        action = getattr(self.expert_recommendation, 'recommended_action', None)
        return f"Current rating: {action.value}" if action else None


class CurrentRatingUnderweightCondition(FlagCondition):
    """Check if current recommendation is UNDERWEIGHT."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.recommended_action == OrderRecommendation.UNDERWEIGHT
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating current rating underweight condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if current recommendation for {self.instrument_name} is UNDERWEIGHT"

    def get_actual_value_display(self) -> Optional[str]:
        action = getattr(self.expert_recommendation, 'recommended_action', None)
        return f"Current rating: {action.value}" if action else None


# Risk Level Flag Conditions
class HighRiskCondition(FlagCondition):
    """Check if expert recommendation has high risk."""
    def evaluate(self) -> bool:
        try:
            return getattr(self.expert_recommendation, 'risk_level', None) == RiskLevel.HIGH
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating high risk condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} has HIGH risk"

    def get_actual_value_display(self) -> Optional[str]:
        risk = getattr(self.expert_recommendation, 'risk_level', None)
        return f"Risk level: {risk.value}" if risk else "Risk level: N/A"


class MediumRiskCondition(FlagCondition):
    """Check if expert recommendation has medium risk."""
    def evaluate(self) -> bool:
        try:
            return getattr(self.expert_recommendation, 'risk_level', None) == RiskLevel.MEDIUM
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating medium risk condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} has MEDIUM risk"

    def get_actual_value_display(self) -> Optional[str]:
        risk = getattr(self.expert_recommendation, 'risk_level', None)
        return f"Risk level: {risk.value}" if risk else "Risk level: N/A"


class LowRiskCondition(FlagCondition):
    """Check if expert recommendation has low risk."""
    def evaluate(self) -> bool:
        try:
            return getattr(self.expert_recommendation, 'risk_level', None) == RiskLevel.LOW
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating low risk condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} has LOW risk"

    def get_actual_value_display(self) -> Optional[str]:
        risk = getattr(self.expert_recommendation, 'risk_level', None)
        return f"Risk level: {risk.value}" if risk else "Risk level: N/A"


class NewTargetHigherCondition(FlagCondition):
    """Check if new expert target is higher than current TP (with 2% tolerance)."""
    
    TOLERANCE_PERCENT = 2.0  # 2% tolerance
    
    def evaluate(self) -> bool:
        try:
            # Initialize tracking variables
            self.current_tp_price = None
            self.new_target_price = None
            self.percent_diff = None
            
            if not self.existing_order:
                logger.debug(f"No existing order for new target higher evaluation")
                return False
            
            # Get current TP price from transaction
            # First check metadata for current_target_price (set by adjust_tp TradeAction)
            # Fallback to transaction.take_profit if metadata not available
            current_tp_price = None
            if self.existing_order.transaction_id:
                from ba2_common.core.db import get_instance
                from ba2_common.core.models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction:
                    # Try to get current_target_price from metadata first
                    if transaction.meta_data and "TradeConditionsData" in transaction.meta_data:
                        current_tp_price = transaction.meta_data["TradeConditionsData"].get("current_target_price")
                        if current_tp_price is not None:
                            logger.debug(f"Using current_target_price from transaction metadata: ${current_tp_price:.2f}")
                    
                    # Fallback to take_profit field if metadata not available
                    if current_tp_price is None and transaction.take_profit:
                        current_tp_price = transaction.take_profit
                        logger.debug(f"Using take_profit from transaction field (metadata not available): ${current_tp_price:.2f}")
            
            if current_tp_price is None:
                logger.debug(f"No current TP price available for order {self.existing_order.id}")
                return False
            
            # Calculate new expert target price
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                return False
            
            base_price = self.expert_recommendation.price_at_date
            expected_profit = self.expert_recommendation.expected_profit_percent
            
            # Calculate new target based on recommendation direction
            from ba2_common.core.types import OrderRecommendation
            if self.expert_recommendation.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                new_target_price = base_price * (1 + expected_profit / 100)
            elif self.expert_recommendation.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                new_target_price = base_price * (1 - expected_profit / 100)
            else:
                logger.debug(f"Recommendation action is HOLD, cannot calculate target")
                return False
            
            # Calculate percent difference (new_target vs current_tp)
            percent_diff = ((new_target_price - current_tp_price) / current_tp_price) * 100
            
            # Store values for external access
            self.current_tp_price = current_tp_price
            self.new_target_price = new_target_price
            self.percent_diff = percent_diff
            
            # Check if new target is higher by more than tolerance
            is_higher = percent_diff > self.TOLERANCE_PERCENT
            
            logger.info(f"New target comparison for {self.instrument_name}: current_TP=${current_tp_price:.2f}, new_target=${new_target_price:.2f}, diff={percent_diff:+.2f}%, is_higher={is_higher} (tolerance={self.TOLERANCE_PERCENT}%)")
            
            return is_higher
            
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating new target higher condition: {e}", exc_info=True)
            # Clear tracking variables on error
            self.current_tp_price = None
            self.new_target_price = None
            self.percent_diff = None
            return False
    
    def get_description(self) -> str:
        """Get description of new target higher condition."""
        return f"Check if new expert target is higher than current TP for {self.instrument_name} (>{self.TOLERANCE_PERCENT}% tolerance)"

    def get_actual_value_display(self) -> Optional[str]:
        pct = getattr(self, 'percent_diff', None)
        if pct is not None:
            return f"Target diff: {pct:+.2f}% (current TP: ${self.current_tp_price:.2f}, new: ${self.new_target_price:.2f})"
        return None


class NewTargetLowerCondition(FlagCondition):
    """Check if new expert target is lower than current TP (with 2% tolerance)."""
    
    TOLERANCE_PERCENT = 2.0  # 2% tolerance
    
    def evaluate(self) -> bool:
        try:
            # Initialize tracking variables
            self.current_tp_price = None
            self.new_target_price = None
            self.percent_diff = None
            
            if not self.existing_order:
                logger.debug(f"No existing order for new target lower evaluation")
                return False
            
            # Get current TP price from transaction
            # First check metadata for current_target_price (set by adjust_tp TradeAction)
            # Fallback to transaction.take_profit if metadata not available
            current_tp_price = None
            if self.existing_order.transaction_id:
                from ba2_common.core.db import get_instance
                from ba2_common.core.models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction:
                    # Try to get current_target_price from metadata first
                    if transaction.meta_data and "TradeConditionsData" in transaction.meta_data:
                        current_tp_price = transaction.meta_data["TradeConditionsData"].get("current_target_price")
                        if current_tp_price is not None:
                            logger.debug(f"Using current_target_price from transaction metadata: ${current_tp_price:.2f}")
                    
                    # Fallback to take_profit field if metadata not available
                    if current_tp_price is None and transaction.take_profit:
                        current_tp_price = transaction.take_profit
                        logger.debug(f"Using take_profit from transaction field (metadata not available): ${current_tp_price:.2f}")
            
            if current_tp_price is None:
                logger.debug(f"No current TP price available for order {self.existing_order.id}")
                return False
            
            # Calculate new expert target price
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                return False
            
            base_price = self.expert_recommendation.price_at_date
            expected_profit = self.expert_recommendation.expected_profit_percent
            
            # Calculate new target based on recommendation direction
            from ba2_common.core.types import OrderRecommendation
            if self.expert_recommendation.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                new_target_price = base_price * (1 + expected_profit / 100)
            elif self.expert_recommendation.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                new_target_price = base_price * (1 - expected_profit / 100)
            else:
                logger.debug(f"Recommendation action is HOLD, cannot calculate target")
                return False
            
            # Calculate percent difference (new_target vs current_tp)
            percent_diff = ((new_target_price - current_tp_price) / current_tp_price) * 100
            
            # Store values for external access
            self.current_tp_price = current_tp_price
            self.new_target_price = new_target_price
            self.percent_diff = percent_diff
            
            # Check if new target is lower by more than tolerance
            is_lower = percent_diff < -self.TOLERANCE_PERCENT
            
            logger.info(f"New target comparison for {self.instrument_name}: current_TP=${current_tp_price:.2f}, new_target=${new_target_price:.2f}, diff={percent_diff:+.2f}%, is_lower={is_lower} (tolerance={self.TOLERANCE_PERCENT}%)")
            
            return is_lower
            
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating new target lower condition: {e}", exc_info=True)
            # Clear tracking variables on error
            self.current_tp_price = None
            self.new_target_price = None
            self.percent_diff = None
            return False
    
    def get_description(self) -> str:
        """Get description of new target lower condition."""
        return f"Check if new expert target is lower than current TP for {self.instrument_name} (<-{self.TOLERANCE_PERCENT}% tolerance)"

    def get_actual_value_display(self) -> Optional[str]:
        pct = getattr(self, 'percent_diff', None)
        if pct is not None:
            return f"Target diff: {pct:+.2f}% (current TP: ${self.current_tp_price:.2f}, new: ${self.new_target_price:.2f})"
        return None


class RatingChangeCondition(FlagCondition):
    """Check if rating changed from one recommendation type to another."""
    
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, from_rating: OrderRecommendation,
                 to_rating: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        """
        Initialize rating change condition.
        
        Args:
            account: Account interface
            instrument_name: Instrument name
            expert_recommendation: Current expert recommendation
            from_rating: Expected previous rating
            to_rating: Expected current rating
            existing_order: Optional existing order
        """
        super().__init__(account, instrument_name, expert_recommendation, existing_order)
        self.from_rating = from_rating
        self.to_rating = to_rating
    
    def evaluate(self) -> bool:
        try:
            recommendations = self.get_previous_recommendations(
                self.expert_recommendation.instance_id, limit=2)
            if len(recommendations) < 2:
                self._previous_action = None
                self._current_action = None
                return False

            previous = recommendations[1]  # Second most recent
            current = recommendations[0]   # Most recent

            self._previous_action = previous.recommended_action
            self._current_action = current.recommended_action

            return (previous.recommended_action == self.from_rating and
                   current.recommended_action == self.to_rating)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating rating change condition ({self.from_rating} -> {self.to_rating}): {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        """Get description of rating change condition."""
        return f"Check if rating changed from {self.from_rating.value} to {self.to_rating.value} for {self.instrument_name}"

    def get_actual_value_display(self) -> Optional[str]:
        prev = getattr(self, '_previous_action', None)
        curr = getattr(self, '_current_action', None)
        if prev is not None and curr is not None:
            return f"Rating: {prev.value} -> {curr.value}"
        return "Rating: insufficient history"


# Convenience classes for specific rating changes (optional - can be removed if not needed)
class RatingNegativeToNeutralCondition(RatingChangeCondition):
    """Check if rating changed from negative to neutral."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.SELL, OrderRecommendation.HOLD, existing_order)


class RatingNegativeToPositiveCondition(RatingChangeCondition):
    """Check if rating changed from negative to positive."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.SELL, OrderRecommendation.BUY, existing_order)


class RatingNeutralToNegativeCondition(RatingChangeCondition):
    """Check if rating changed from neutral to negative."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.HOLD, OrderRecommendation.SELL, existing_order)


class RatingNeutralToPositiveCondition(RatingChangeCondition):
    """Check if rating changed from neutral to positive."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.HOLD, OrderRecommendation.BUY, existing_order)


class RatingPositiveToNegativeCondition(RatingChangeCondition):
    """Check if rating changed from positive to negative."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.BUY, OrderRecommendation.SELL, existing_order)


class RatingPositiveToNeutralCondition(RatingChangeCondition):
    """Check if rating changed from positive to neutral."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation,
                        OrderRecommendation.BUY, OrderRecommendation.HOLD, existing_order)


# Ordinal rank of the 5 trading grades, bearish -> bullish. ERROR/unknown grades
# are deliberately absent so any transition involving them yields no signal.
_RATING_RANK = {
    OrderRecommendation.SELL: 0,
    OrderRecommendation.UNDERWEIGHT: 1,
    OrderRecommendation.HOLD: 2,
    OrderRecommendation.OVERWEIGHT: 3,
    OrderRecommendation.BUY: 4,
}


class RatingDirectionCondition(FlagCondition):
    """Base for rating_upgraded / rating_downgraded: compares the ordinal rank of
    the two most recent recommendations for this instance + instrument."""

    def _rank_delta(self) -> Optional[int]:
        recommendations = self.get_previous_recommendations(
            self.expert_recommendation.instance_id, limit=2)
        if len(recommendations) < 2:
            self._previous_action = None
            self._current_action = None
            return None
        previous = recommendations[1].recommended_action
        current = recommendations[0].recommended_action
        self._previous_action = previous
        self._current_action = current
        if previous not in _RATING_RANK or current not in _RATING_RANK:
            return None
        return _RATING_RANK[current] - _RATING_RANK[previous]

    def get_actual_value_display(self) -> Optional[str]:
        prev = getattr(self, '_previous_action', None)
        curr = getattr(self, '_current_action', None)
        if prev is not None and curr is not None:
            return f"Rating: {prev.value} -> {curr.value}"
        return "Rating: insufficient history"


class RatingUpgradedCondition(RatingDirectionCondition):
    """True when the latest recommendation's grade rank rose vs the previous one."""
    def evaluate(self) -> bool:
        try:
            delta = self._rank_delta()
            return delta is not None and delta > 0
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating rating upgraded condition: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        return f"Check if rating was upgraded (rank increased) for {self.instrument_name}"


class RatingDowngradedCondition(RatingDirectionCondition):
    """True when the latest recommendation's grade rank fell vs the previous one."""
    def evaluate(self) -> bool:
        try:
            delta = self._rank_delta()
            return delta is not None and delta < 0
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating rating downgraded condition: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        return f"Check if rating was downgraded (rank decreased) for {self.instrument_name}"


# Numeric Condition Implementations

class ExpectedProfitTargetPercentCondition(CompareCondition):
    """Compare expected profit target percentage."""

    def evaluate(self) -> bool:
        try:
            expected_profit = self.expert_recommendation.expected_profit_percent

            # If no expected profit data, we cannot evaluate
            if expected_profit is None:
                logger.debug(f"No expected profit data available for {self.instrument_name}")
                self.calculated_value = None
                return False

            self.calculated_value = expected_profit  # Store calculated value
            return self.operator_func(expected_profit, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating expected profit target condition: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        """Get description of expected profit target condition."""
        return f"Check if expected profit target percent for {self.instrument_name} is {self.operator_str} {self.value}%"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.2f}%"


class PercentToCurrentTargetCondition(CompareCondition):
    """Compare percent from current price to current TP target price."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for percent to current target evaluation")
                self.calculated_value = None
                return False
            
            # Get current market price
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                self.calculated_value = None
                return False
            
            # Get current TP price from transaction or order
            current_tp_price = None
            
            # First try to get from transaction's take_profit field
            if self.existing_order.transaction_id:
                from ba2_common.core.db import get_instance
                from ba2_common.core.models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction and transaction.take_profit:
                    current_tp_price = transaction.take_profit
                    logger.debug(f"Current TP from transaction: ${current_tp_price:.2f}")
            
            # If no TP in transaction, we can't evaluate
            if current_tp_price is None:
                logger.debug(f"No current TP price available for order {self.existing_order.id}")
                self.calculated_value = None
                return False
            
            # Calculate percent to current target
            percent_to_current_target = ((current_tp_price - current_price) / current_price) * 100
            
            self.calculated_value = percent_to_current_target  # Store calculated value
            
            logger.info(f"Percent to CURRENT target for {self.instrument_name}: current=${current_price:.2f}, TP=${current_tp_price:.2f}, distance={percent_to_current_target:+.2f}%")
            
            return self.operator_func(percent_to_current_target, self.value)
            
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating percent to current target condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of percent to current target condition."""
        return f"Check if percent from current price to current TP for {self.instrument_name} is {self.operator_str} {self.value}%"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class NewTargetPercentCondition(CompareCondition):
    """Compare percent change from current TP to new expert target (positive if higher, negative if lower)."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for new target percent evaluation")
                self.calculated_value = None
                return False
            
            # Get current TP price from transaction
            # First check metadata for current_target_price (set by adjust_tp TradeAction)
            # Fallback to transaction.take_profit if metadata not available
            current_tp_price = None
            if self.existing_order.transaction_id:
                from ba2_common.core.db import get_instance
                from ba2_common.core.models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction:
                    # Try to get current_target_price from metadata first
                    if transaction.meta_data and "TradeConditionsData" in transaction.meta_data:
                        current_tp_price = transaction.meta_data["TradeConditionsData"].get("current_target_price")
                        if current_tp_price is not None:
                            logger.debug(f"Using current_target_price from transaction metadata: ${current_tp_price:.2f}")
                    
                    # Fallback to take_profit field if metadata not available
                    if current_tp_price is None and transaction.take_profit:
                        current_tp_price = transaction.take_profit
                        logger.debug(f"Using take_profit from transaction field (metadata not available): ${current_tp_price:.2f}")
            
            if current_tp_price is None:
                logger.debug(f"No current TP price available for order {self.existing_order.id}")
                self.calculated_value = None
                return False
            
            # Calculate new expert target price
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                self.calculated_value = None
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                self.calculated_value = None
                return False
            
            base_price = self.expert_recommendation.price_at_date
            expected_profit = self.expert_recommendation.expected_profit_percent
            
            # Calculate new target based on recommendation direction
            from ba2_common.core.types import OrderRecommendation
            if self.expert_recommendation.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                new_target_price = base_price * (1 + expected_profit / 100)
            elif self.expert_recommendation.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                new_target_price = base_price * (1 - expected_profit / 100)
            else:
                logger.debug(f"Recommendation action is HOLD, cannot calculate target")
                self.calculated_value = None
                return False
            
            # Calculate percent difference: positive if new target higher, negative if lower
            new_target_percent = ((new_target_price - current_tp_price) / current_tp_price) * 100
            
            self.calculated_value = new_target_percent  # Store calculated value
            
            logger.info(f"New target percent for {self.instrument_name}: current_TP=${current_tp_price:.2f}, new_target=${new_target_price:.2f}, change={new_target_percent:+.2f}%")
            
            return self.operator_func(new_target_percent, self.value)
            
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating new target percent condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of new target percent condition."""
        return f"Check if new target percent change for {self.instrument_name} is {self.operator_str} {self.value}%"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class PercentToNewTargetCondition(CompareCondition):
    """Compare percent from current price to new expert target price.

    Works for both enter_market (no existing order) and open_positions rulesets.
    Calculates: (expert_target - current_price) / current_price * 100
    For BUY: positive means target is above current price (upside)
    For SELL: negative means target is below current price (downside potential)
    """

    def evaluate(self) -> bool:
        try:
            # Get current market price
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                self.calculated_value = None
                return False
            
            # Calculate new expert target price from current recommendation
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                self.calculated_value = None
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                self.calculated_value = None
                return False
            
            base_price = self.expert_recommendation.price_at_date
            expected_profit = self.expert_recommendation.expected_profit_percent
            
            # Calculate new target based on recommendation direction
            from ba2_common.core.types import OrderRecommendation
            if self.expert_recommendation.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                new_target_price = base_price * (1 + expected_profit / 100)
            elif self.expert_recommendation.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                new_target_price = base_price * (1 - expected_profit / 100)
            else:
                logger.debug(f"Recommendation action is HOLD, cannot calculate target")
                self.calculated_value = None
                return False
            
            # Calculate percent to new target
            percent_to_new_target = ((new_target_price - current_price) / current_price) * 100
            
            self.calculated_value = percent_to_new_target  # Store calculated value
            
            logger.info(f"Percent to NEW target for {self.instrument_name}: current=${current_price:.2f}, new_target=${new_target_price:.2f} (base=${base_price:.2f}, profit={expected_profit:.1f}%), distance={percent_to_new_target:+.2f}%")
            
            return self.operator_func(percent_to_new_target, self.value)
            
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating percent to new target condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of percent to new target condition."""
        return f"Check if percent from current price to new expert target for {self.instrument_name} is {self.operator_str} {self.value}%"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class PercentOpenToNewTargetCondition(CompareCondition):
    """Compare percent from open price to new expert target price.

    For open_positions rulesets only (requires existing order with open price).
    Calculates: (expert_target - open_price) / open_price * 100
    Answers: "how much profit does the expert target represent from my entry?"
    """

    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for percent open-to-target evaluation")
                self.calculated_value = None
                return False

            # Get open price from transaction
            from ba2_common.core.trade_store import get_or_none
            from ba2_common.core.models import Transaction

            transaction_id = getattr(self.existing_order, 'transaction_id', None)
            if not transaction_id:
                logger.warning(f"Order {self.existing_order.id} has no transaction_id")
                self.calculated_value = None
                return False

            transaction = get_or_none(Transaction, transaction_id)

            if not transaction or not transaction.open_price:
                logger.warning(f"Transaction {transaction_id} not found or has no open_price")
                self.calculated_value = None
                return False

            open_price = transaction.open_price

            # Calculate expert target price from recommendation
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for open-to-target evaluation")
                self.calculated_value = None
                return False

            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                self.calculated_value = None
                return False

            base_price = self.expert_recommendation.price_at_date
            expected_profit = self.expert_recommendation.expected_profit_percent

            from ba2_common.core.types import OrderRecommendation
            if self.expert_recommendation.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                new_target_price = base_price * (1 + expected_profit / 100)
            elif self.expert_recommendation.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                new_target_price = base_price * (1 - expected_profit / 100)
            else:
                logger.debug(f"Recommendation action is HOLD, cannot calculate target")
                self.calculated_value = None
                return False

            # Calculate percent from open price to expert target
            percent_open_to_target = ((new_target_price - open_price) / open_price) * 100

            self.calculated_value = percent_open_to_target

            logger.info(f"Percent open-to-target for {self.instrument_name}: open=${open_price:.2f}, target=${new_target_price:.2f} (base=${base_price:.2f}, profit={expected_profit:.1f}%), delta={percent_open_to_target:+.2f}%")

            return self.operator_func(percent_open_to_target, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating percent open-to-target condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return f"Check if percent from open price to new expert target for {self.instrument_name} is {self.operator_str} {self.value}%"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


def _get_transaction_for_order(existing_order):
    """Look up the Transaction linked to existing_order (None if not found)."""
    from ba2_common.core.models import Transaction

    transaction_id = getattr(existing_order, 'transaction_id', None)
    if not transaction_id:
        logger.warning(f"Order {existing_order.id} has no transaction_id — cannot compute P&L via transaction")
        return None

    try:
        from ba2_common.core.trade_store import get_or_none
        transaction = get_or_none(Transaction, transaction_id)
    except Exception as e:
        absorb_if_benign(e)
        logger.error(f"Error fetching transaction {transaction_id}: {e}", exc_info=True)
        return None

    if not transaction:
        logger.warning(f"Transaction {transaction_id} not found for order {existing_order.id}")
        return None

    return transaction


def _get_pnl_via_transaction(existing_order, current_price) -> Optional[Dict]:
    """
    Look up the Transaction linked to existing_order and calculate P&L using
    TransactionHelper.calculate_pnl — the same formula as the Live Trades page.
    Returns the pnl dict {'amount', 'percent'} or None on failure.
    """
    from ba2_common.core.TransactionHelper import TransactionHelper

    transaction = _get_transaction_for_order(existing_order)
    if transaction is None:
        return None

    return TransactionHelper.calculate_pnl(transaction, current_price)


def _get_option_pnl_via_transaction(account, existing_order) -> Optional[Dict]:
    """
    P&L of an OPTION position, priced off the option premium and scaled by the
    contract multiplier.

    For an option transaction open_price is the per-share PREMIUM, so comparing
    it against the underlying share price (what get_current_price() returns for
    the underlying symbol) overstates P&L by orders of magnitude. Instead the
    current premium comes from the account's option quote: long (BUY) positions
    mark at the bid, short (SELL) at the ask, each falling back to last when
    that side of the quote is missing. Returns the pnl dict {'amount', 'percent'}
    or None when no premium or multiplier is obtainable (never a fabricated price).
    """
    from ba2_common.core.TransactionHelper import TransactionHelper
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    from ba2_common.core.types import OrderDirection

    if not isinstance(account, OptionsAccountInterface):
        logger.warning(f"Account does not support options; cannot compute option P&L for order {existing_order.id}")
        return None

    transaction = _get_transaction_for_order(existing_order)
    if transaction is None:
        return None

    try:
        quote = account.get_option_quote(existing_order.contract_symbol)
    except Exception as e:
        absorb_if_benign(e)
        logger.error(f"Error fetching option quote for {existing_order.contract_symbol}: {e}", exc_info=True)
        return None
    if quote is None:
        logger.warning(f"No option quote for {existing_order.contract_symbol} — cannot compute option P&L")
        return None

    if transaction.side == OrderDirection.BUY:
        current_premium = quote.bid if quote.bid is not None else quote.last
    else:
        current_premium = quote.ask if quote.ask is not None else quote.last
    if current_premium is None:
        logger.warning(f"Option quote for {existing_order.contract_symbol} has no usable premium (bid/ask/last all None)")
        return None

    # The multiplier is carried from the originating order onto the transaction
    # at creation (100 for standard options); refuse to guess when it is missing.
    multiplier = transaction.multiplier or existing_order.multiplier
    if not multiplier:
        logger.warning(f"No contract multiplier on transaction {transaction.id} or order {existing_order.id} — cannot compute option P&L")
        return None

    return TransactionHelper.calculate_option_pnl(transaction, current_premium, multiplier)


def _get_spread_pnl_via_transaction(account, existing_order) -> Optional[Dict]:
    """
    P&L of a MULTI-LEG option structure (spread/strangle/condor/butterfly/...), priced
    off the structure's NET premium.

    The parent order of a multi-leg combo has asset_class OPTION but NO contract_symbol
    (the contract lives on each child leg), so the single-leg path
    (_get_option_pnl_via_transaction) does not apply — and the legacy equity path
    compared the UNDERLYING price against the parent's net-premium open_price, producing
    astronomic percentages (~+4900% for a $3.75 debit on a $190 stock, ~-7500% for a
    credit) that fired any TP/SL on the FIRST evaluation (the B9 defect: debit spreads
    TP'd after 1 bar at a real +1-12%; credit spreads could never TP and any SL fired
    instantly).

    The P&L is computed from the transaction's PER-CONTRACT view over EVERY executed
    option order (entry legs, MLEG-close legs, standalone single-leg closes, synthetic
    expiry closes):
        amount = cash_collected + cost_to_flatten_held_legs
    where cash_collected is the signed premium cash of all executed fills (sells +,
    buys −) — so a leg closed individually mid-life contributes its REALIZED P&L — and
    the still-held contracts are marked to flatten from the account's option quotes
    exactly like the single-leg path (long at bid, short at ask, last fallback). For an
    untouched structure this reduces to (net_current − open_net) x qty x multiplier with
    net_current = Σ sign x current_premium x leg_ratio. The parent transaction's
    open_price is the entry net premium per structure (positive = debit, negative =
    credit — normalised here via the parent's side, so a stored absolute value prices
    identically). Then:
        percent = amount / (|open_net| x |qty| x multiplier) x 100
    which reduces to "% of credit captured" for a SELL parent (net premium halving =
    +50%) and to the debit multiple for a BUY parent. Returns None (decline to
    evaluate — never a fabricated number) when a held-leg quote/multiplier is missing,
    no executed option orders exist, the structure is already flat (nothing left to
    manage), or the entry net premium is ~0 (even-money structure: the percent basis is
    undefined).
    """
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    from ba2_common.core.trade_store import orders_where
    from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus

    if not isinstance(account, OptionsAccountInterface):
        logger.warning(f"Account does not support options; cannot compute structure P&L for order {existing_order.id}")
        return None

    transaction = _get_transaction_for_order(existing_order)
    if transaction is None:
        return None

    executed = OrderStatus.get_executed_statuses()
    cash_collected = 0.0          # signed premium cash per share: sells +, buys −
    remaining: Dict[str, float] = {}  # contract_symbol -> signed qty (BUY +)
    for o in orders_where(transaction_id=transaction.id):
        if getattr(o, "asset_class", None) != AssetClass.OPTION or not o.contract_symbol:
            continue
        if o.status not in executed:
            continue
        if o.open_price is None:
            logger.warning(f"Executed option order {o.id} ({o.contract_symbol}) has no fill price — cannot compute structure P&L")
            return None
        o_qty = float(o.filled_qty or o.quantity or 0.0)
        if o_qty <= 0:
            continue
        sign = 1.0 if o.side == OrderDirection.BUY else -1.0
        cash_collected += -sign * float(o.open_price) * o_qty
        remaining[o.contract_symbol] = remaining.get(o.contract_symbol, 0.0) + sign * o_qty

    if not remaining:
        logger.warning(f"No executed option legs for multi-leg parent order {existing_order.id} — cannot compute structure P&L")
        return None

    structures = abs(float(existing_order.quantity or 0.0))
    if structures <= 0:
        logger.warning(f"Multi-leg parent order {existing_order.id} has no structure quantity — cannot compute structure P&L")
        return None

    # Signed cash inflow of flattening every still-held contract at current quotes.
    flatten_cash = 0.0
    any_held = False
    for contract, net in remaining.items():
        if abs(net) < 1e-9:
            continue
        any_held = True
        try:
            quote = account.get_option_quote(contract)
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error fetching option quote for leg {contract}: {e}", exc_info=True)
            return None
        if quote is None:
            logger.warning(f"No option quote for leg {contract} — cannot compute structure P&L")
            return None
        if net > 0:  # long leg: mark at the bid (what selling it back yields)
            leg_premium = quote.bid if quote.bid is not None else quote.last
        else:        # short leg: mark at the ask (what buying it back costs)
            leg_premium = quote.ask if quote.ask is not None else quote.last
        if leg_premium is None:
            logger.warning(f"Option quote for leg {contract} has no usable premium (bid/ask/last all None)")
            return None
        # Flatten trade of signed qty -net at the mark: long net>0 -> sell -> +net*prem;
        # short net<0 -> buy back -> net*prem (negative cash). One expression covers both.
        flatten_cash += net * leg_premium

    if not any_held:
        # Every leg already resolved individually — nothing left to manage; the
        # transaction closes itself via refresh (per-contract balance).
        return None

    multiplier = transaction.multiplier or existing_order.multiplier
    if not multiplier:
        logger.warning(f"No contract multiplier on transaction {transaction.id} or order {existing_order.id} — cannot compute structure P&L")
        return None

    open_net = abs(float(transaction.open_price or 0.0))
    if transaction.side != OrderDirection.BUY:
        open_net = -open_net
    if abs(open_net) < 1e-9:
        logger.warning(f"Even-money / zero net premium on multi-leg parent order {existing_order.id} — structure P&L% undefined")
        return None

    qty = abs(float(transaction.quantity or 0.0)) or structures
    pnl_amount = (cash_collected + flatten_cash) * multiplier
    pnl_pct = pnl_amount / (abs(open_net) * qty * multiplier) * 100
    return {'amount': round(pnl_amount, 2), 'percent': round(pnl_pct, 4)}


def _get_pnl_for_condition(condition) -> Optional[Dict]:
    """
    P&L of the condition's existing_order: single-leg option orders are priced off the
    option premium (see _get_option_pnl_via_transaction), multi-leg option structures
    off the structure's net premium (see _get_spread_pnl_via_transaction), equity
    orders off the instrument's current price (legacy behavior, unchanged).
    """
    from ba2_common.core.types import AssetClass

    existing_order = condition.existing_order
    if getattr(existing_order, 'asset_class', None) == AssetClass.OPTION:
        if existing_order.contract_symbol:
            return _get_option_pnl_via_transaction(condition.account, existing_order)
        return _get_spread_pnl_via_transaction(condition.account, existing_order)

    current_price = condition.get_current_price()
    if current_price is None:
        return None

    return _get_pnl_via_transaction(existing_order, current_price)


class ProfitLossAmountCondition(CompareCondition):
    """Compare profit/loss amount using the same formula as the Live Trades page."""

    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                self.calculated_value = None
                return False

            pnl = _get_pnl_for_condition(self)
            if pnl is None:
                self.calculated_value = None
                return False

            self.calculated_value = pnl['amount']
            return self.operator_func(pnl['amount'], self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating profit loss amount condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return f"Check if profit/loss amount for {self.instrument_name} is {self.operator_str} ${self.value}"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"${self.calculated_value:.2f}"


class ProfitLossPercentCondition(CompareCondition):
    """Compare profit/loss percentage using the same formula as the Live Trades page."""

    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                self.calculated_value = None
                return False

            pnl = _get_pnl_for_condition(self)
            if pnl is None:
                self.calculated_value = None
                return False

            self.calculated_value = pnl['percent']
            return self.operator_func(pnl['percent'], self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating profit loss percent condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return f"Check if profit/loss percentage for {self.instrument_name} is {self.operator_str} {self.value}%"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.2f}%"


class LossPctOfMaxLossCondition(CompareCondition):
    """Unrealized LOSS as a percentage of the position's persisted DEFINED maximum loss.

    value = -pnl_amount / (max_loss_per_contract x contracts) x 100 -- POSITIVE while
    losing, +100 when the whole defined risk is gone, NEGATIVE while profitable (so a
    ``>`` stop can never fire on a winner). Scale-free by construction: the denominator
    carries the same contract count the P&L already does, so 1 contract and 5 read the
    same percentage -- the property a %-of-credit stop (``opt_sl``) does not have.

    THE DENOMINATOR IS READ BACK, NEVER RECONSTRUCTED. ``TradeActions._submit_option_order``
    persisted ``max_loss_per_contract`` onto the parent order's ``data`` beside
    ``option_reserve`` (design 2026-08-29 S8.2), and ONLY when ``option_payoff.max_loss``
    returned MEASURED. A structure with no measured max loss (a short call; a broken
    quote) has NO key, which is "contracts that support it" enforced by the data: this
    condition then refuses to evaluate rather than special-casing structure shapes here.

    UNKNOWN NEVER FIRES -- the ``DaysToExpiryCondition`` discipline. An absent key, a
    zero/negative/stringly-typed/NaN persisted value, an unknowable contract count, or a
    P&L that cannot be resolved each leave ``calculated_value`` None and ``evaluate()``
    False for EVERY operator. The two defaults specifically refused:

    * absence read as some number -> a stop fires on a position whose risk was never
      measured (the worst available failure: it closes positions on sight);
    * unevaluable read as 0 % -> any ``<`` gate fires for every position we merely
      failed to price, while looking configured.
    """

    def _defined_risk_dollars(self) -> Optional[float]:
        """``max_loss_per_contract x contracts`` in dollars, or None -- no measurement.

        The number-or-None reading reuses ``option_payoff._numeric``, the module rule for
        "is this a usable quantity" (rejects bool, str, NaN, infinity) -- the persisted
        value must never be *parsed* into firing, only read.
        """
        from ba2_common.core.option_payoff import _numeric

        order = self.existing_order
        data = getattr(order, "data", None)
        per_contract = _numeric((data or {}).get("max_loss_per_contract"))
        if per_contract is None or per_contract <= 0:
            return None
        contracts = _numeric(getattr(order, "filled_qty", None))
        if contracts is None or contracts == 0:
            contracts = _numeric(getattr(order, "quantity", None))
        if contracts is None or contracts == 0:
            return None
        return per_contract * abs(contracts)

    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                self.calculated_value = None
                return False

            # Denominator FIRST: it is a pure row read, while the P&L fetches option
            # quotes -- and a structure that never persisted a max loss should cost
            # nothing per bar.
            denominator = self._defined_risk_dollars()
            if denominator is None:
                self.calculated_value = None
                return False

            pnl = _get_pnl_for_condition(self)
            if pnl is None:
                self.calculated_value = None
                return False

            self.calculated_value = -pnl['amount'] / denominator * 100.0
            return self.operator_func(self.calculated_value, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating loss_pct_of_max_loss condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if unrealized loss for {self.instrument_name} is "
                f"{self.operator_str} {self.value}% of the structure's max loss")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.2f}% of max loss"


class ProfitMultipleOfPremiumCondition(CompareCondition):
    """Current structure value as a MULTIPLE of the entry premium paid (LONG/debit only).

    value = current_structure_value / entry_premium. ``_get_pnl_for_condition`` (single-leg
    via ``TransactionHelper.calculate_option_pnl``, multi-leg via
    ``_get_spread_pnl_via_transaction``) already computes ``pnl_pct = pnl_amount /
    (entry_premium x contracts x multiplier) x 100`` for both paths, and
    ``current_structure_value = entry_premium + pnl_amount / (contracts x multiplier)``, so:

        multiple = current_structure_value / entry_premium = 1 + pnl_pct / 100

    SCALE-FREE by the same construction as ``LossPctOfMaxLossCondition``: the denominator
    the P&L machinery divides by already carries the same contract count the numerator
    does, so 1 contract and 5 contracts read the identical multiple.

    A "multiple of premium PAID" is only a coherent number for a DEBIT entry (a long
    option, or a net-debit spread) -- there is no such multiple for a credit RECEIVED. The
    persisted ``open_price`` is always an absolute magnitude (see
    ``_get_spread_pnl_via_transaction``'s docstring: "a stored absolute value prices
    identically"); the SIGN lives entirely in ``transaction.side`` -- BUY == debit ==
    positive premium, SELL == credit == negative. So this condition refuses to evaluate,
    NEVER firing in EITHER operator direction, whenever the transaction is not a BUY.
    That check is a pure row read, done BEFORE the P&L fetch (which hits live option
    quotes) -- mirroring ``LossPctOfMaxLossCondition``'s "denominator first" ordering, so a
    credit structure costs nothing extra per bar.

    UNKNOWN NEVER FIRES -- the ``DaysToExpiryCondition``/``LossPctOfMaxLossCondition``
    discipline. No ``existing_order``, no resolvable transaction, a credit (SELL) entry, or
    a P&L ``_get_pnl_for_condition`` cannot resolve (missing quote, missing multiplier, an
    already-flat structure) each leave ``calculated_value`` None and ``evaluate()`` False
    for EVERY operator. The two defaults specifically refused:

    * a credit structure reads as firing (denominator sign mishandled) -> a TP fires on a
      position that never paid a premium to be "worth a multiple of";
    * unevaluable read as 0 -> ``profit_multiple_of_premium < N`` fires on sight for any
      position we merely failed to price.

    This is a PROFIT-side gate (like ``profit_loss_percent``'s ``>`` reading), never a
    stop -- see ``TradeActionEvaluator._LOSS_SIDE_STOP_OPERATORS``, which deliberately
    omits it so a rule naming this field classifies DISCRETIONARY, not forced.
    """

    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                self.calculated_value = None
                return False

            from ba2_common.core.types import OrderDirection

            transaction = _get_transaction_for_order(self.existing_order)
            if transaction is None:
                self.calculated_value = None
                return False

            # Side FIRST -- a pure row read, cheaper than the option-quote fetch
            # _get_pnl_for_condition does, and a credit entry never qualifies.
            if transaction.side != OrderDirection.BUY:
                self.calculated_value = None
                return False

            pnl = _get_pnl_for_condition(self)
            if pnl is None:
                self.calculated_value = None
                return False

            self.calculated_value = 1.0 + pnl['percent'] / 100.0
            return self.operator_func(self.calculated_value, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating profit_multiple_of_premium condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if current value for {self.instrument_name} is "
                f"{self.operator_str} {self.value}x the entry premium paid")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.2f}x"


# Confidence Condition Implementation
class ConfidenceCondition(CompareCondition):
    """Compare expert confidence value."""
    def evaluate(self) -> bool:
        try:
            confidence = getattr(self.expert_recommendation, 'confidence', None)
            if confidence is None:
                logger.debug(f"No confidence value available for {self.instrument_name}")
                self.calculated_value = None
                return False
            
            self.calculated_value = confidence  # Store calculated value
            return self.operator_func(confidence, self.value)
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating confidence condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert confidence for {self.instrument_name} is {self.operator_str} {self.value}"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.1f}%"


class DaysOpenedCondition(CompareCondition):
    """Compare time since the position was opened (in days).

    The reference "now" is the recommendation's ``created_at`` (the SIMULATED as-of bar
    in a backtest, ≈ wall-clock in live) — NOT ``datetime.now()`` — so the value is
    correct under the backtest clock and never leaks wall time (mirrors the sibling
    ``DaysSinceLastCloseCondition``).

    The OPEN reference is the transaction's ``open_date`` (sim-stamped by the backtest
    account on OPEN; real-time in live), looked up via the entry order's
    ``transaction_id``. It falls back to ``existing_order.created_at`` only when no
    sim-stamped ``open_date`` is available, since the order row's ``created_at`` is
    wall-clock and would collapse ``days_opened`` to ~0 in a backtest.
    """

    def _open_reference(self) -> Optional[datetime]:
        """Sim-correct open time: transaction.open_date, else order.created_at."""
        txn_id = getattr(self.existing_order, "transaction_id", None)
        if txn_id is not None:
            try:
                # Repository, not session.get(): the transaction lives in the in-memory store
                # during a RAM-only backtest, where a raw lookup returns None and would silently
                # fall back to the order's wall-clock created_at (collapsing days_opened to ~0).
                from ba2_common.core.trade_repository import get_trade_repository
                txn = get_trade_repository().transaction(txn_id)
                open_date = txn.open_date if txn is not None else None
                if open_date is not None:
                    return open_date
            except Exception as e:  # noqa: BLE001
                absorb_if_benign(e)
                logger.debug(f"DaysOpenedCondition: open_date lookup failed for "
                             f"transaction {txn_id}: {e}")
        return self.existing_order.created_at

    def evaluate(self) -> bool:
        try:
            if not self.existing_order or not self.existing_order.created_at:
                self.calculated_value = None
                return False

            # Sim-aware "now": the recommendation's as-of bar in a backtest, wall-clock
            # in live. Falls back to wall-clock only if no recommendation timestamp.
            now = getattr(self.expert_recommendation, "created_at", None) or datetime.now(timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

            opened_at = self._open_reference()
            if opened_at is None:
                self.calculated_value = None
                return False
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)

            days_opened = (now - opened_at).total_seconds() / 86400  # 86400 seconds in a day

            self.calculated_value = days_opened  # Store calculated value

            return self.operator_func(days_opened, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating days opened condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of days opened condition."""
        return f"Check if days since {self.instrument_name} order was opened is {self.operator_str} {self.value} days"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.1f} days"


class DaysSinceLastCloseCondition(CompareCondition):
    """Calendar days since this expert last CLOSED a transaction on the symbol.

    A cooldown gate: pair with ``>`` so an entry is allowed only once enough days have
    elapsed since the last exit on the symbol (stops re-buying the same name immediately
    after every close). ``_profit_sign`` narrows WHICH close counts:
        0  -> any close,  +1 -> only a profitable close,  -1 -> only a losing close.

    The reference "now" is the recommendation's ``created_at`` (the simulated as-of bar in a
    backtest, ≈ wall-clock in live) — NOT ``datetime.now()`` — so the value is correct under
    the backtest clock and never leaks wall time. When no qualifying close exists the value is
    a large sentinel (1e9) so a ">" cooldown passes (no prior trade -> entry allowed).

    That sentinel is ONLY for a KNOWABLE "never closed". If a close exists but cannot be
    classified — no P&L to read its profit sign from, or no ``close_date`` to age it — the
    condition goes UNEVALUABLE (``calculated_value = None``, ``evaluate()`` False) instead.
    Reusing 1e9 there answers "infinitely long ago" to a question nobody measured, and the
    cooldown then never fires.
    """

    _profit_sign = 0  # 0=any, +1=profitable only, -1=losing only

    def evaluate(self) -> bool:
        try:
            from ba2_common.core.trade_repository import get_trade_repository

            expert_id = getattr(self.expert_recommendation, "instance_id", None)
            now = getattr(self.expert_recommendation, "created_at", None) or datetime.now(timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

            # MEASURED FAILURE (2026-07-30): this used a raw select(Transaction), which the
            # in-memory backtest store does not serve — with a close 3 days old and a ">15 day"
            # cooldown it returned the 1e9 "never closed" sentinel, so the gate was INERT for
            # every GA trial and the optimizer tuned a gene that did nothing. Live (SQLite) the
            # gate DID fire, so a deployed config behaved unlike its own backtest: the same
            # genome scored 103 trades / 17.55% annualised in-memory vs 169 / 0.20% on disk.
            from ba2_common.core.trade_repository import LAST_CLOSE_UNCLASSIFIABLE

            most_recent, reason = get_trade_repository().last_closed_transaction_with_reason(
                expert_id=expert_id, symbol=self.instrument_name,
                profit_sign=self._profit_sign,
            )

            if most_recent is None:
                if reason == LAST_CLOSE_UNCLASSIFIABLE:
                    # "COULD NOT DETERMINE" IS NOT "NEVER CLOSED". The 1e9 sentinel says
                    # "infinitely long ago", which makes a ">N day" cooldown pass — so a
                    # close the repository could not classify (no close price recorded, so
                    # no profit sign; or no close_date, so no age) silently DISABLED the
                    # gate. The sentinel is only honest for a knowable "never". Go
                    # unevaluable instead of inventing a measurement.
                    logger.error(
                        f"days-since-last-close for {self.instrument_name}: a close EXISTS "
                        f"but cannot be classified (profit_sign={self._profit_sign}); the "
                        f"cooldown is UNDETERMINABLE. Not using the 'never closed' sentinel "
                        f"— that would silently pass the gate."
                    )
                    self.calculated_value = None
                    return False
                self.calculated_value = 1e9  # never closed (qualifying) -> "infinitely" long ago
                return self.operator_func(self.calculated_value, self.value)

            close_date = most_recent.close_date
            if close_date.tzinfo is None:
                close_date = close_date.replace(tzinfo=timezone.utc)
            days = max((now - close_date).total_seconds() / 86400.0, 0.0)
            self.calculated_value = days
            return self.operator_func(days, self.value)
        except Exception as e:  # noqa: BLE001
            absorb_if_benign(e)
            logger.error(f"Error evaluating days-since-last-close for {self.instrument_name}: {e}",
                         exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        kind = {0: "", 1: " profitable", -1: " losing"}[self._profit_sign]
        return (f"Check if days since {self.instrument_name}'s last{kind} close is "
                f"{self.operator_str} {self.value} days")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        if self.calculated_value >= 1e9:
            return "no prior close"
        return f"{self.calculated_value:.1f} days"


class DaysSinceLastProfitableCloseCondition(DaysSinceLastCloseCondition):
    """Calendar days since this expert last closed the symbol for a PROFIT (pnl > 0)."""
    _profit_sign = 1


class DaysSinceLastLosingCloseCondition(DaysSinceLastCloseCondition):
    """Calendar days since this expert last closed the symbol for a LOSS (pnl < 0)."""
    _profit_sign = -1


class InstrumentAccountShareCondition(CompareCondition):
    """Compare current instrument value as percentage of expert virtual equity."""
    
    def evaluate(self) -> bool:
        try:
            # Get current position value
            position_value = self._get_instrument_position_value()
            if position_value is None:
                logger.debug(f"No position value available for {self.instrument_name}")
                self.calculated_value = None
                return False
            
            # Get expert virtual equity
            virtual_equity = self._get_expert_virtual_equity()
            if virtual_equity is None or virtual_equity <= 0:
                logger.error(f"Invalid virtual equity for expert {self.expert_recommendation.instance_id} ({self.instrument_name}): virtual_equity={virtual_equity}")
                self.calculated_value = None
                return False
            
            # Calculate share percentage
            share_percent = (position_value / virtual_equity) * 100.0
            
            self.calculated_value = share_percent  # Store calculated value
            
            logger.debug(f"Instrument {self.instrument_name} share: {share_percent:.2f}% "
                        f"(position_value=${position_value:.2f}, virtual_equity=${virtual_equity:.2f})")
            
            return self.operator_func(share_percent, self.value)
            
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating instrument account share condition: {e}", exc_info=True)
            return False
    
    def _get_instrument_position_value(self) -> Optional[float]:
        """
        Get this expert's share of the instrument's market value.

        Uses the expert's own OPENED transactions (not the broker-level position)
        to avoid counting shares held by other experts.
        """
        try:
            expert_instance_id = self.expert_recommendation.instance_id

            from ba2_common.core.trade_repository import get_trade_repository

            # Repository, not a raw select(): blind to the in-memory BT store otherwise, which
            # would report a 0% instrument share for every position in a backtest.
            transactions = get_trade_repository().open_transactions(
                expert_id=expert_instance_id, symbol=self.instrument_name,
            )

            if not transactions:
                return 0.0

            total_qty = sum(abs(t.quantity) for t in transactions if t.quantity)
            if total_qty == 0:
                return 0.0

            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                return None

            return total_qty * current_price

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error getting instrument position value: {e}", exc_info=True)
            return None

    def _get_expert_virtual_equity(self) -> Optional[float]:
        """
        Get expert's total virtual equity (allocated capital, not just free cash).

        Uses get_virtual_balance() = account_balance × virtual_equity_pct so that
        a fully-invested expert still has a sensible denominator.
        """
        try:
            expert_instance_id = self.expert_recommendation.instance_id

            from ba2_common.core.instance_resolver import get_instance_resolver
            expert = get_instance_resolver().get_expert_instance(expert_instance_id)
            if not expert:
                logger.error(f"Expert instance {expert_instance_id} not found")
                return None

            virtual_balance = expert.get_virtual_balance()
            if virtual_balance is None:
                logger.error(f"Could not get virtual balance for expert {expert_instance_id}")
                return None

            return virtual_balance

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error getting expert virtual equity: {e}", exc_info=True)
            return None
    
    def get_description(self) -> str:
        return f"Check if {self.instrument_name} position value as % of expert virtual equity is {self.operator_str} {self.value}%"

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.2f}%"


# Option-related Conditions

class PercentBelowRecentHighCondition(CompareCondition):
    """Compare how far (in percent) the current price is below the recent high.

    Calculates: (recent_high - current_price) / recent_high * 100
    A larger positive value means a deeper pullback from the recent high.
    Useful for "buy the dip" option entries.
    """

    RECENT_WINDOW = 20  # bars (days) used to compute the recent high

    def evaluate(self) -> bool:
        try:
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                self.calculated_value = None
                return False

            # CLAMP THE FETCH TO THE EVALUATION BAR. Without end_date the backtest's
            # MemoizedOHLCVProvider — which discards lookback_days — returns the ENTIRE
            # [start - warmup, run end] window, so tail(RECENT_WINDOW) below was the high of
            # the last 20 days of the whole RUN: a number out of the simulated future that
            # fabricates dips that never happened. None in live (unchanged call).
            end_date, clock_ok = self._as_of_fetch_end()
            if not clock_ok:
                self.calculated_value = None
                return False

            # Resolve the OHLCV provider via the host-injected resolver
            # (ba2_common never imports ba2_providers).
            df = _get_provider("ohlcv", "yfinance").get_ohlcv_data(
                self.instrument_name, interval="1d", end_date=end_date,
                lookback_days=self.RECENT_WINDOW * 2 + 10)
            if df is None or df.empty:
                logger.warning(f"No OHLCV data for {self.instrument_name}")
                self.calculated_value = None
                return False

            recent_high = float(df.tail(self.RECENT_WINDOW)["High"].max())
            if recent_high <= 0:
                logger.warning(f"Invalid recent high for {self.instrument_name}: {recent_high}")
                self.calculated_value = None
                return False

            self.calculated_value = (recent_high - current_price) / recent_high * 100

            logger.info(
                f"Percent below recent high for {self.instrument_name}: "
                f"current=${current_price:.2f}, recent_high=${recent_high:.2f}, "
                f"below={self.calculated_value:+.2f}%")

            return self.operator_func(self.calculated_value, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating percent below recent high condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} is {self.operator_str} {self.value}% "
                f"below its recent {self.RECENT_WINDOW}-day high")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class PercentAboveRecentLowCondition(CompareCondition):
    """Compare how far (in percent) the current price is above the recent low.

    Calculates: (current_price - recent_low) / recent_low * 100
    A larger positive value means a stronger rebound from the recent low.
    """

    RECENT_WINDOW = 20  # bars (days) used to compute the recent low

    def evaluate(self) -> bool:
        try:
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                self.calculated_value = None
                return False

            # CLAMP THE FETCH TO THE EVALUATION BAR — see PercentBelowRecentHighCondition.
            # Unclamped, tail(RECENT_WINDOW) reads the last 20 bars of the whole RUN.
            end_date, clock_ok = self._as_of_fetch_end()
            if not clock_ok:
                self.calculated_value = None
                return False

            # Resolve the OHLCV provider via the host-injected resolver
            # (ba2_common never imports ba2_providers).
            df = _get_provider("ohlcv", "yfinance").get_ohlcv_data(
                self.instrument_name, interval="1d", end_date=end_date,
                lookback_days=self.RECENT_WINDOW * 2 + 10)
            if df is None or df.empty:
                logger.warning(f"No OHLCV data for {self.instrument_name}")
                self.calculated_value = None
                return False

            recent_low = float(df.tail(self.RECENT_WINDOW)["Low"].min())
            if recent_low <= 0:
                logger.warning(f"Invalid recent low for {self.instrument_name}: {recent_low}")
                self.calculated_value = None
                return False

            self.calculated_value = (current_price - recent_low) / recent_low * 100

            logger.info(
                f"Percent above recent low for {self.instrument_name}: "
                f"current=${current_price:.2f}, recent_low=${recent_low:.2f}, "
                f"above={self.calculated_value:+.2f}%")

            return self.operator_func(self.calculated_value, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating percent above recent low condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} is {self.operator_str} {self.value}% "
                f"above its recent {self.RECENT_WINDOW}-day low")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class PriceVsTargetLowCondition(CompareCondition):
    """Compare current price vs. FMPRating's analyst LOW price target.

    Calculates: (current_price - target_low) / target_low * 100. Positive means price is
    ABOVE the low (most conservative) analyst estimate. Reads target_low from
    expert_recommendation.data["FMPRating"] (persisted by FMPRating.run_analysis for every
    recommendation) - decoupled from the expert's BUY/SELL/HOLD rating so an option entry can
    gate on price positioning independent of the directional signal.
    """

    def evaluate(self) -> bool:
        try:
            current_price = self.get_current_price()
            if current_price is None:
                self.calculated_value = None
                return False
            fmp_data = (self.expert_recommendation.data or {}).get("FMPRating") \
                if self.expert_recommendation is not None else None
            target_low = fmp_data.get("target_low") if fmp_data else None
            if target_low is None or target_low <= 0:
                self.calculated_value = None
                return False
            self.calculated_value = (current_price - target_low) / target_low * 100
            return self.operator_func(self.calculated_value, self.value)
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating price vs target low condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} price is {self.operator_str} {self.value}% "
                f"vs analyst LOW target")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class PriceVsTargetHighCondition(CompareCondition):
    """Compare current price vs. FMPRating's analyst HIGH price target.

    Calculates: (current_price - target_high) / target_high * 100. Positive means price is
    ABOVE the high (most bullish) analyst estimate - i.e. overextended even by the most
    optimistic analyst's number. This is the condition that lets an entry fire a bearish
    structure (e.g. a long put) purely on price positioning, regardless of whether the
    expert's own rating still says BUY.
    """

    def evaluate(self) -> bool:
        try:
            current_price = self.get_current_price()
            if current_price is None:
                self.calculated_value = None
                return False
            fmp_data = (self.expert_recommendation.data or {}).get("FMPRating") \
                if self.expert_recommendation is not None else None
            target_high = fmp_data.get("target_high") if fmp_data else None
            if target_high is None or target_high <= 0:
                self.calculated_value = None
                return False
            self.calculated_value = (current_price - target_high) / target_high * 100
            return self.operator_func(self.calculated_value, self.value)
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating price vs target high condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} price is {self.operator_str} {self.value}% "
                f"vs analyst HIGH target")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class PriceVsTargetConsensusCondition(CompareCondition):
    """Compare current price vs. FMPRating's analyst CONSENSUS (median) price target.

    Calculates: (current_price - target_consensus) / target_consensus * 100.
    """

    def evaluate(self) -> bool:
        try:
            current_price = self.get_current_price()
            if current_price is None:
                self.calculated_value = None
                return False
            fmp_data = (self.expert_recommendation.data or {}).get("FMPRating") \
                if self.expert_recommendation is not None else None
            target_consensus = fmp_data.get("target_consensus") if fmp_data else None
            if target_consensus is None or target_consensus <= 0:
                self.calculated_value = None
                return False
            self.calculated_value = (current_price - target_consensus) / target_consensus * 100
            return self.operator_func(self.calculated_value, self.value)
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating price vs target consensus condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} price is {self.operator_str} {self.value}% "
                f"vs analyst CONSENSUS target")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:+.2f}%"


class IVRankCondition(CompareCondition):
    """Compare the implied-volatility rank (0-100) of the underlying.

    IV rank is the percentile of the current ATM IV against the account's stored
    trailing ATM-IV history. Requires an options-capable account.
    """

    # Minimum trailing ATM-IV samples required to compute a rank. Lower than the
    # account default so a rank is available early in an instrument's history.
    IV_RANK_MIN_SAMPLES = 5

    def evaluate(self) -> bool:
        try:
            from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

            if not isinstance(self.account, OptionsAccountInterface):
                logger.warning(
                    f"Account does not support options; cannot compute IV rank for "
                    f"{self.instrument_name}")
                self.calculated_value = None
                return False

            rank = self.account.get_iv_rank(
                self.instrument_name, min_samples=self.IV_RANK_MIN_SAMPLES)
            if rank is None:
                logger.warning(f"IV rank unavailable for {self.instrument_name}")
                self.calculated_value = None
                return False

            self.calculated_value = rank

            logger.info(f"IV rank for {self.instrument_name}: {rank:.1f}")

            return self.operator_func(rank, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating IV rank condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if the IV rank of {self.instrument_name} is "
                f"{self.operator_str} {self.value}")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.1f}"


class RelativeVolumeCondition(CompareCondition):
    """Current bar volume as a MULTIPLE of its own trailing average (1.0 == normal).

    ``calculated_value = volume[as-of bar] / mean(volume over the BASELINE_WINDOW bars BEFORE
    it)``. 2.0 means the name is trading at twice its recent pace — the classic unusual-
    activity / participation confirmation for an entry.

    THE UNDERLYING'S VOLUME, NOT A CONTRACT'S. Most individual option contracts print zero on
    most days (measured over 13.7M cached bars: p10=1, p25=3, p50=14 contracts/day, and that
    is only the rows that exist), so a contract-level ratio is undefined far more often than it
    is informative. The underlying always prints. (The contract-level equivalent worth having
    is volume / OPEN INTEREST, which this codebase cannot compute today: ``open_interest`` is
    NULL on every cached option row — see ``option_selector.passes_liquidity``.)

    THREE PROPERTIES THIS CONDITION MUST HAVE, each of which is a bug if lost:

    1. **The baseline EXCLUDES the current bar.** Include it and the average absorbs the very
       spike the gate exists to detect: a 10x day against a 20-bar window that contains it
       computes 10 / ((19 x 1 + 10) / 20) = 6.9, and the distortion grows exactly as the
       signal does. The slice is ``[-(W+1):-1]``.
    2. **The baseline does NOT reach forward.** The fetch is clamped to the evaluation bar via
       ``_as_of_fetch_end`` — the backtest's memoized provider DISCARDS ``lookback_days`` and
       would otherwise return the whole run window, i.e. an average computed from the
       simulated future. That exact lookahead was already found in
       ``percent_below_recent_high``.
    3. **Insufficient history or a zero-volume average is UNKNOWN, never 1.0.** Defaulting to
       "normal" makes the gate silently free-passing on every newly-listed symbol and on every
       symbol whose feed dropped out — and 1.0 is plausible enough that nobody would look.
       ``calculated_value`` stays None and ``evaluate()`` returns False for every operator.
    """

    #: Trailing bars averaged for the baseline (the current bar is NOT one of them).
    BASELINE_WINDOW = 20

    def evaluate(self) -> bool:
        try:
            # CLAMP THE FETCH TO THE EVALUATION BAR — see PercentBelowRecentHighCondition.
            end_date, clock_ok = self._as_of_fetch_end()
            if not clock_ok:
                self.calculated_value = None
                return False

            df = _get_provider("ohlcv", "yfinance").get_ohlcv_data(
                self.instrument_name, interval="1d", end_date=end_date,
                lookback_days=self.BASELINE_WINDOW * 2 + 10)
            if df is None or df.empty or "Volume" not in df:
                logger.warning(f"No OHLCV volume data for {self.instrument_name}")
                self.calculated_value = None
                return False

            volumes = df["Volume"]
            # W baseline bars PLUS the current one. A shorter history is UNKNOWN: averaging
            # whatever is there would compare a spike against two quiet days and call it 10x.
            if len(volumes) < self.BASELINE_WINDOW + 1:
                logger.warning(
                    f"Only {len(volumes)} bars for {self.instrument_name}; "
                    f"relative volume needs {self.BASELINE_WINDOW + 1}")
                self.calculated_value = None
                return False

            current = volumes.iloc[-1]
            baseline = list(volumes.iloc[-(self.BASELINE_WINDOW + 1):-1])
            # A NaN anywhere in the window is UNKNOWN, not something to skip past: pandas'
            # mean() drops NaNs silently, so a window with 18 of 20 bars missing would average
            # the surviving two and report a confident ratio against them.
            if _is_missing(current) or any(_is_missing(v) for v in baseline):
                logger.warning(f"Missing volume bar(s) for {self.instrument_name}")
                self.calculated_value = None
                return False
            current = float(current)
            average = sum(float(v) for v in baseline) / len(baseline)
            # A zero (or negative) average is not "normal volume", it is no measurement at all:
            # a halted/unlisted name, or a feed that zero-filled the column.
            if average <= 0 or current < 0:
                logger.warning(
                    f"Unusable volume for {self.instrument_name}: current={current}, "
                    f"{self.BASELINE_WINDOW}-bar average={average}")
                self.calculated_value = None
                return False

            self.calculated_value = current / average

            logger.info(
                f"Relative volume for {self.instrument_name}: current={current:,.0f}, "
                f"{self.BASELINE_WINDOW}-bar average={average:,.0f}, "
                f"ratio={self.calculated_value:.2f}x")

            return self.operator_func(self.calculated_value, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating relative volume condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} volume is {self.operator_str} "
                f"{self.value}x its trailing {self.BASELINE_WINDOW}-bar average")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.2f}x"


class IVToRealizedVolCondition(CompareCondition):
    """ATM implied volatility divided by the underlying's REALISED volatility.

    ``calculated_value = atm_iv / realized_vol``, both annualised fractions. This is the
    variance risk premium made explicit, and it is the actual edge in premium selling: you are
    paid IMPLIED and you pay out REALISED. A ratio of 1.3 means options are priced 30 % above
    what the stock has actually been doing; below 1.0 the options are cheap relative to the
    realised move, which is the long-premium case.

    Distinct from ``iv_rank``, which compares a symbol's IV to its OWN history: a name can sit
    at IV rank 90 and still be fairly priced if it is genuinely moving, and at rank 20 while
    still paying a fat premium over a dead tape.

    Realised vol is the annualised sample stdev of daily log returns over the
    ``REALIZED_WINDOW`` bars ending at the evaluation bar (the fetch is clamped by
    ``_as_of_fetch_end``, so it can never reach into the simulated future).

    UNKNOWN IS NEVER A NUMBER HERE, in either direction:

    * a non-options account, or no ATM IV, or an IV outside
      ``OptionsAccountInterface.plausible_atm_iv``'s 1 %-500 % band (the shared definition, so
      live and backtest cannot fork on it),
    * fewer than ``REALIZED_WINDOW + 1`` closes, or a non-positive close,
    * a realised vol below ``MIN_MEASURABLE_REALIZED_VOL``,

    all leave ``calculated_value`` None and make ``evaluate()`` False for every operator. The
    last one matters most: a near-zero denominator does not mean "options are infinitely rich",
    it means the tape was flat or the feed repeated a close, and dividing by it would hand a
    ``>`` gate a spectacular pass on exactly the names with no usable data.
    """

    #: Trading days of returns in the realised-vol estimate.
    REALIZED_WINDOW = 20
    #: Trading days per year, for annualising the daily stdev.
    TRADING_DAYS_PER_YEAR = 252
    #: Floor on a realised vol that can be a DENOMINATOR. Same reasoning (and same 1 %
    #: threshold) as MIN_PLAUSIBLE_ATM_IV: below this the number is a flat tape or a repeated
    #: close, not a measurement, and the ratio it produces is unbounded noise.
    MIN_MEASURABLE_REALIZED_VOL = 0.01

    def _atm_iv(self) -> Optional[float]:
        """The account's current ATM IV, validated by the SHARED plausibility bound."""
        from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

        if not isinstance(self.account, OptionsAccountInterface):
            logger.warning(
                f"Account does not support options; cannot compute IV/realised vol for "
                f"{self.instrument_name}")
            return None
        return OptionsAccountInterface.plausible_atm_iv(
            self.account.get_atm_implied_volatility(self.instrument_name))

    def _realized_vol(self) -> Optional[float]:
        """Annualised realised volatility over the window ENDING at the evaluation bar."""
        import math as _math

        end_date, clock_ok = self._as_of_fetch_end()
        if not clock_ok:
            return None
        df = _get_provider("ohlcv", "yfinance").get_ohlcv_data(
            self.instrument_name, interval="1d", end_date=end_date,
            lookback_days=self.REALIZED_WINDOW * 2 + 10)
        if df is None or df.empty or "Close" not in df:
            logger.warning(f"No OHLCV close data for {self.instrument_name}")
            return None
        closes = df["Close"].iloc[-(self.REALIZED_WINDOW + 1):]
        # N returns need N+1 closes. Fewer is UNKNOWN, not a stdev over whatever exists:
        # a 3-bar sample's stdev is noise wearing a volatility's units.
        if len(closes) < self.REALIZED_WINDOW + 1:
            logger.warning(
                f"Only {len(closes)} closes for {self.instrument_name}; realised vol needs "
                f"{self.REALIZED_WINDOW + 1}")
            return None
        values = [float(c) for c in closes]
        if any(_is_missing(c) or c <= 0 for c in values):
            logger.warning(f"Non-positive or missing close for {self.instrument_name}")
            return None
        rets = [_math.log(values[i] / values[i - 1]) for i in range(1, len(values))]
        n = len(rets)
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)   # sample stdev (ddof=1)
        return _math.sqrt(var) * _math.sqrt(self.TRADING_DAYS_PER_YEAR)

    def evaluate(self) -> bool:
        try:
            iv = self._atm_iv()
            if iv is None:
                logger.warning(f"ATM IV unavailable for {self.instrument_name}")
                self.calculated_value = None
                return False

            rv = self._realized_vol()
            if rv is None:
                self.calculated_value = None
                return False
            if rv < self.MIN_MEASURABLE_REALIZED_VOL:
                logger.warning(
                    f"Realised vol {rv:.4f} for {self.instrument_name} is below the "
                    f"{self.MIN_MEASURABLE_REALIZED_VOL} measurable floor; IV/RV is unknown, "
                    f"not infinite")
                self.calculated_value = None
                return False

            self.calculated_value = iv / rv

            logger.info(
                f"IV/realised vol for {self.instrument_name}: iv={iv:.3f}, rv={rv:.3f}, "
                f"ratio={self.calculated_value:.2f}")

            return self.operator_func(self.calculated_value, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating IV/realised vol condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if {self.instrument_name} implied/realised volatility is "
                f"{self.operator_str} {self.value}")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{self.calculated_value:.2f}"


class DaysToEarningsCondition(CompareCondition):
    """Compare the number of calendar days until the underlying's next earnings.

    ``calculated_value = (next_earnings_date - the evaluation bar).days``. Useful for timing
    volatility plays (e.g. buy a straddle a few days before earnings) or for AVOIDING entries
    that would straddle an earnings event.

    The clock
    ---------
    "Today" is ``self._evaluation_date()`` — the SIMULATED bar in a backtest, the wall clock
    in live — never ``date.today()``. It used to be ``date.today()`` on both sides, which in
    a backtest measured the distance from the REAL-WORLD today to the next earnings:
    identical on every simulated bar of every year, and a live-vs-backtest divergence on a
    documented entry gate.

    The calendar
    ------------
    The date comes from FMP's QUARTERLY earnings calendar (``get_past_earnings``, which
    despite its name wraps ``historical_earning_calendar``: every row carries the actual
    announcement ``report_date`` and the endpoint also returns already-SCHEDULED future
    prints). It used to come from ``get_earnings_estimates``, which never passes a period
    parameter to ``/api/v3/analyst-estimates`` — that endpoint defaults to ANNUAL, so
    ``frequency="quarterly"`` was decorative and the "next earnings" was the next fiscal
    YEAR end. Measured against the on-disk cache, on a 2024-03-15 bar: MSFT 2024-04-25 (41d)
    truth vs 2024-06-30 (107d) annual; AAPL 2024-05-02 (48d) vs 2024-09-27 (196d); NVDA
    2024-05-22 (68d) vs 2025-01-25 (316d). Combined with the wall clock the gate was a
    per-symbol CONSTANT (MSFT 310d, AAPL 34d, NVDA 154d on every bar), which is why the
    documented ``iv_rank<=30 and days_to_earnings<=5 -> open_straddle`` rule could never fire.

    The annual estimates remain as a FALLBACK for a symbol whose calendar carries no
    scheduled future print — degraded, not absent.

    Unknown is never a value: with no usable evaluation date or no upcoming earnings from
    either source, ``calculated_value`` stays None and ``evaluate()`` returns False for every
    operator.

    NOT the condition a CHAINED earnings strategy should use. When the entry runs behind an
    expert that already ranked the event (``FMPEarningsEvent``), the gate belongs on
    ``rec_days_to_earnings`` -- the distance THAT expert stamped on the recommendation --
    so the timing and the rank cannot disagree (design 2026-08-31 leaps-grid S9). This one
    stays the answer for UNCHAINED uses: any expert, no stamp required.
    """

    #: How far past the evaluation bar to read the earnings calendar. Comfortably more than
    #: one quarter (so a delayed print is still found) and bounded, so a stale calendar
    #: cannot hand back a date from the far future as if it were the next one.
    EARNINGS_HORIZON_DAYS = 200
    #: Calendar rows to pull. The provider returns rows <= end_date newest-first, so this
    #: only needs to span [bar, bar + horizon] plus a little history.
    EARNINGS_CALENDAR_PERIODS = 8

    @staticmethod
    def _parse_day(value):
        """A ``date`` from a 'YYYY-MM-DD' string / date / datetime, or None."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    def _next_scheduled_report(self, provider, symbol: str, as_of: date):
        """Earliest quarterly report date on or after ``as_of`` from the earnings CALENDAR.

        Earnings day itself counts as 0 days away, so the bound is inclusive.
        """
        horizon = datetime.combine(as_of + timedelta(days=self.EARNINGS_HORIZON_DAYS),
                                   _time.max, tzinfo=timezone.utc)
        result = provider.get_past_earnings(
            symbol, frequency="quarterly", end_date=horizon,
            lookback_periods=self.EARNINGS_CALENDAR_PERIODS, format_type="dict",
        )
        rows = (result or {}).get("earnings") or []
        upcoming = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # report_date is the announcement; fiscal_date_ending is the period it covers.
            # The gate is about the EVENT, so the announcement wins when both are present.
            d = self._parse_day(row.get("report_date")) or \
                self._parse_day(row.get("fiscal_date_ending"))
            if d is not None and d >= as_of:
                upcoming.append(d)
        return min(upcoming) if upcoming else None

    def _next_estimate_period(self, provider, symbol: str, as_of: date):
        """Earliest forward analyst-estimate period end on or after ``as_of``. FALLBACK ONLY:
        FMP serves these ANNUAL regardless of the frequency argument, so the answer is a
        fiscal year end rather than an earnings date."""
        result = provider.get_earnings_estimates(
            symbol, frequency="quarterly",
            as_of_date=datetime.combine(as_of, _time.min, tzinfo=timezone.utc),
            lookback_periods=4, format_type="dict",
        )
        estimates = (result or {}).get("estimates") or []
        future = []
        for est in estimates:
            if not isinstance(est, dict):
                continue
            d = self._parse_day(est.get("fiscal_date_ending"))
            if d is not None and d >= as_of:
                future.append(d)
        return min(future) if future else None

    def _next_earnings_date(self, symbol: str, as_of: date):
        """Best-effort next earnings date for ``symbol`` as of ``as_of``, or None.

        Resolves the company-details provider via the host-injected provider resolver
        (ba2_common never imports fmpsdk / ba2_providers). Isolated so tests can monkeypatch
        it without any network I/O. Best effort: any failure, missing provider or missing
        data yields None and the condition simply does not fire.
        """
        try:
            if get_provider_resolver() is None:
                logger.warning(
                    "TradeConditions provider resolver not configured; "
                    "cannot fetch next earnings date")
                return None
            provider = _get_provider("fundamentals_details", "fmp")

            scheduled = self._next_scheduled_report(provider, symbol, as_of)
            if scheduled is not None:
                return scheduled

            estimated = self._next_estimate_period(provider, symbol, as_of)
            if estimated is not None:
                logger.info(
                    f"No scheduled earnings report for {symbol} within "
                    f"{self.EARNINGS_HORIZON_DAYS} days of {as_of.isoformat()}; falling back "
                    f"to the (annual) analyst-estimate period {estimated.isoformat()}")
            return estimated
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error fetching next earnings date for {symbol}: {e}", exc_info=True)
            return None

    def evaluate(self) -> bool:
        try:
            as_of = self._evaluation_date()
            if as_of is None:
                # A backtest account whose simulated clock is unreadable. Substituting
                # date.today() here is exactly the bug this condition was fixed for.
                logger.warning(
                    f"days_to_earnings for {self.instrument_name} is unevaluable: no usable "
                    f"evaluation date")
                self.calculated_value = None
                return False

            next_earnings = self._next_earnings_date(self.instrument_name, as_of)
            if next_earnings is None:
                logger.warning(f"No upcoming earnings date for {self.instrument_name}")
                self.calculated_value = None
                return False

            days = (next_earnings - as_of).days
            self.calculated_value = days

            logger.info(
                f"Days to earnings for {self.instrument_name}: {days} "
                f"(next earnings {next_earnings.isoformat()})")

            return self.operator_func(days, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating days to earnings condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if days until {self.instrument_name}'s next earnings is "
                f"{self.operator_str} {self.value}")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{int(self.calculated_value)}d"


class RecommendationDaysToEarningsCondition(CompareCondition):
    """Days to earnings AS THE RANKING EXPERT STAMPED IT -- the chained-entry timing gate.

    ``calculated_value = expert_recommendation.data["FMPEarningsEvent"]["days_to_earnings"]``,
    read back, never recomputed. This is ``O_ERN``'s entry gene (``rec_days_to_earnings <= X``,
    X searched 1-5 -- design 2026-08-31 leaps-grid S9).

    WHY THIS EXISTS BESIDE ``DaysToEarningsCondition``
    -------------------------------------------------
    The design's TIMING SPLIT is: the EXPERT owns the ranking, the STRATEGY owns the timing,
    and there is ONE timing knob. ``FMPEarningsEvent`` already resolved the event date to
    score the symbol at that bar; it stamps the distance it measured. A strategy that gated
    on a SECOND, independently fetched calendar read could disagree with the rank it is
    acting on -- a different date (the calendar moves, a print is delayed, the annual-estimate
    fallback fires), or the same date measured against a different clock. So the chained gate
    reads the STAMP and nothing else.

    ``DaysToEarningsCondition`` (``days_to_earnings``) is NOT changed and NOT deprecated: it
    fetches the FMP calendar itself and is the answer for UNCHAINED uses -- any expert, no
    stamp required (e.g. "do not open anything that would straddle an earnings print").
    Making THAT condition stamp-first-with-a-calendar-fallback was considered and rejected:
    a fallback is exactly the second timing source this split exists to remove, it cannot
    satisfy "absent stamp never fires" (it would fire off the calendar instead), and it would
    silently change the meaning of every ruleset already using the field.

    ABSENT STAMP NEVER FIRES -- the ``DaysToExpiryCondition`` / ``LossPctOfMaxLoss``
    discipline, and here it is load-bearing rather than defensive. Every recommendation from
    every other expert has no ``FMPEarningsEvent`` payload, so if absence read as ``0`` the
    gate ``rec_days_to_earnings <= 5`` would pass for the ENTIRE universe and the strategy
    would buy a straddle on everything while looking timed. ``calculated_value`` therefore
    stays ``None`` and ``evaluate()`` returns False for EVERY operator when the stamp is
    missing, non-dict, or not a real number (bool/str/NaN are refused, not parsed).
    """

    def evaluate(self) -> bool:
        try:
            days = stamped_days_to_earnings(self.expert_recommendation)
            if days is None:
                # DEBUG, not warning: on any non-event expert this is the normal case for
                # every symbol on every bar, and a warning per symbol per bar is noise.
                logger.debug(
                    f"rec_days_to_earnings for {self.instrument_name} is unevaluable: the "
                    f"recommendation carries no FMPEarningsEvent days_to_earnings stamp")
                self.calculated_value = None
                return False

            self.calculated_value = days
            return self.operator_func(days, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating rec_days_to_earnings condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if the expert's stamped days-to-earnings for {self.instrument_name} "
                f"is {self.operator_str} {self.value}")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{int(self.calculated_value)}d"


class DaysAfterEventCondition(CompareCondition):
    """Calendar days SINCE the stamped event -- the exit half of the O_ERN timing split.

    ``calculated_value = (the evaluation bar) - (the event date the ENTRY order carries)``.
    ``O_ERN``'s exit gene is ``days_after_event >= Y``, Y searched 0-2 (design 2026-08-31
    leaps-grid S9): hold the straddle through the print, then take whatever the move and the
    vol crush left, Y days later.

    THE REFERENCE DATE COMES OFF THE ORDER, NOT THE RECOMMENDATION
    --------------------------------------------------------------
    By exit time the recommendation in hand is a DIFFERENT, later one -- possibly for the
    NEXT quarter's print, possibly from another expert entirely. The date that matters is
    the one the position was opened for, so ``TradeActions._submit_option_order`` carries it
    forward onto the parent order's ``data["earnings_event_date"]`` at submit, beside
    ``max_loss_per_contract`` and ``option_reserve``. Same seam, same read-back-never-
    reconstruct rule (design 2026-08-29 S8.2). The alternative -- re-reading the entry
    recommendation through ``TradingOrder.expert_recommendation_id`` -- was rejected: it is a
    row fetch per open position per bar for a value that never changes after entry.

    THE DATE CONVENTION, PINNED
    ---------------------------
    Two ``date`` objects subtracted -- no timezone arithmetic, no wall clock, no partial
    days. "Today" is ``self._evaluation_date()``: the SIMULATED bar in a backtest, the wall
    clock in live (``DaysToExpiryCondition``'s clock, for the same reason -- a backtest's
    wall clock is years past the last bar). The event day itself reads ``0``; the next
    calendar day reads ``1``. So on a Monday event, ``days_after_event >= 1`` first passes on
    the Tuesday bar, and ``>= 0`` passes on the Monday bar itself. Negative before the event
    -- a real state, since the entry is taken 1-5 days BEFORE the print -- and NOT clamped,
    because clamping to 0 would make ``>= 0`` fire the moment the position opened.

    UNKNOWN NEVER FIRES, in either direction. No order, no ``data`` dict, no
    ``earnings_event_date``, an unparseable one, or an unreadable simulated clock each leave
    ``calculated_value`` ``None`` and ``evaluate()`` False for EVERY operator. Absence is the
    normal case for every position not opened off an earnings-event recommendation, and the
    default specifically refused is "absent reads as today" (``0``), which would fire
    ``>= 0`` on sight and flatten every option position in the book.

    FORCED, NOT DISCRETIONARY, when it closes an option structure -- registered in
    ``TradeActionEvaluator._FORCED_EXIT_EVENT_TYPES`` beside ``days_to_expiry``. See the
    note there for why this time exit is classified opposite to ``days_opened``.
    """

    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                self.calculated_value = None
                return False

            # Row read FIRST: a position with no event stamp -- every equity position and
            # every option position from any other expert -- must cost nothing per bar.
            event_day = order_event_date(self.existing_order)
            if event_day is None:
                self.calculated_value = None
                return False

            as_of = self._evaluation_date()
            if as_of is None:
                # A backtest account whose simulated clock is unreadable. Substituting
                # date.today() here is the DaysToEarningsCondition bug, in a position exit.
                logger.warning(
                    f"days_after_event for {self.instrument_name} is unevaluable: no usable "
                    f"evaluation date")
                self.calculated_value = None
                return False

            self.calculated_value = (as_of - event_day).days
            return self.operator_func(self.calculated_value, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating days_after_event condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if calendar days since {self.instrument_name}'s stamped event is "
                f"{self.operator_str} {self.value}")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"{int(self.calculated_value)}d after event"


class DaysToExpiryCondition(CompareCondition):
    """Calendar days of option life REMAINING: ``expiry - the evaluation date``.

    The complement of ``DaysOpenedCondition``, which counts days ELAPSED. They are NOT
    interchangeable: the grid tunes the entry DTE window as its own gene, so "21 days
    after opening" lands on a different remaining life for every trial. Until this
    existed, roll-at-DTE was reachable only from the hardcoded ``OptionPortfolioManager``
    (so only ``PremiumSeller`` could roll, and the GA could not optimise the roll point
    for anything else), and a 0DTE structure had no exit criterion at all.

    Sign: positive while the structure is alive, ``0`` on the expiry date itself, and
    NEGATIVE past it. Past-expiry is a real (and alarming) state for a still-open
    structure; clamping it to 0 would make ``days_to_expiry > 0`` answer "still alive".

    Unknown is never a value
    ------------------------
    Follows ``option_lifecycle``'s ``LIFECYCLE_UNKNOWN`` discipline (and its ``_dte``
    helper specifically). When the expiry cannot be determined the condition is
    UNEVALUABLE: ``calculated_value`` stays ``None``, ``evaluate()`` returns False for
    EVERY operator, and ``get_actual_value_display()`` renders the REASON rather than a
    number, so an audit row can never show a plausible DTE for a measurement we did not
    make. The two defaults it refuses:

    * ``0`` — would satisfy ``days_to_expiry <= N`` and flatten, on sight, every position
      whose expiry we happen not to see. The worst available failure mode.
    * ``+inf`` / a large sentinel — would make the exit permanently inert while looking
      configured. That is precisely the dead roll-DTE gene an entire GA campaign tuned.

    (A ``DaysSinceLastCloseCondition``-style ``1e9`` sentinel is right THERE — "never
    closed" is a real, knowable fact — and wrong here: "no expiry recorded" is ignorance
    about a structure that definitely has one.)

    Where the expiry comes from
    ---------------------------
    Every source is consulted and they must AGREE; the union is the candidate set, the
    same way ``option_lifecycle._dte`` builds it:

    1. ``Transaction.expiry``  — the structure's declared intent.
    2. the parent ``TradingOrder.expiry`` — NULL for every multi-leg before it started
       being stamped, which is exactly why roll-at-DTE never fired.
    3. the HELD legs — netted per contract over the executed option orders (BUY ``+``,
       SELL ``-``), so a leg bought back to close stops counting and its stale expiry
       cannot manufacture a permanent contradiction. Historical rows carry no stamp on
       either row above, so the legs are what keeps them measurable.

    Empty candidate set -> unevaluable. More than one distinct date -> unevaluable: a
    structure whose own rows disagree about when it expires has no DTE, and picking
    ``min()`` (closes early) or ``max()`` (never closes) would be inventing one.
    Multi-expiry structures are refused at submit time, but pre-existing rows are not.
    A leg with no expiry at all simply adds no information and never vetoes the legs
    that have one.

    The "today" is the recommendation's ``created_at`` — the simulated as-of bar in a
    backtest, wall-clock in live — never ``date.today()``, so the value is deterministic
    under a frozen clock and correct in backtest. The comparison is by DATE, so every bar
    of one session reports the same DTE.
    """

    #: Rendered instead of a number when the measurement could not be made.
    unknown_reason: Optional[str] = None

    @staticmethod
    def _as_date(value):
        """A ``date`` from a ``date``/``datetime`` (mirrors ``option_lifecycle._as_date``)."""
        if isinstance(value, datetime):
            return value.date()
        return value

    def _as_of_date(self):
        """The evaluation DATE, or None when there is no as-of to evaluate against."""
        as_of = getattr(self.expert_recommendation, "created_at", None)
        if as_of is None:
            return None
        return self._as_of_to_date(as_of)

    @staticmethod
    def _as_of_to_date(as_of):
        if isinstance(as_of, datetime):
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=timezone.utc)
            return as_of.astimezone(timezone.utc).date()
        return as_of

    def _held_leg_expiries(self, transaction_id):
        """Distinct expiries of the still-HELD legs of ``transaction_id``.

        Netted per contract symbol over the EXECUTED option orders exactly as
        ``_get_spread_pnl_via_transaction`` does — a contract whose signed quantity nets
        to zero is closed and contributes nothing.
        """
        from ba2_common.core.trade_store import orders_where
        from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus

        if transaction_id is None:
            return set()

        executed = OrderStatus.get_executed_statuses()
        net: Dict[str, float] = {}
        # contract -> EVERY distinct expiry its rows carry. Deliberately not "the first
        # one wins": an OCC symbol determines its expiry, so two different values on one
        # contract are corrupt data, and quietly keeping whichever row was read first is
        # the silent-default this class exists to refuse. Both land in the candidate set
        # and the caller's contradiction check turns them into a loud unknown.
        expiry_by_contract: Dict[str, set] = {}
        for o in orders_where(transaction_id=transaction_id):
            if getattr(o, "asset_class", None) != AssetClass.OPTION or not o.contract_symbol:
                continue
            if o.status not in executed:
                continue
            qty = abs(float(o.filled_qty or o.quantity or 0.0))
            if qty <= 0:
                continue
            sign = 1.0 if o.side == OrderDirection.BUY else -1.0
            net[o.contract_symbol] = net.get(o.contract_symbol, 0.0) + sign * qty
            if o.expiry is not None:
                expiry_by_contract.setdefault(o.contract_symbol, set()).add(
                    self._as_date(o.expiry))

        out = set()
        for contract, qty in net.items():
            if abs(qty) > 1e-9:
                out |= expiry_by_contract.get(contract, set())
        return out

    def _resolve_expiry(self):
        """(expiry, "") or (None, why it is unmeasurable). Never guesses."""
        order = self.existing_order
        if order is None:
            return None, ("no open position on this evaluation — there is no option "
                          "life to measure")

        candidates = set()
        txn_id = getattr(order, "transaction_id", None)
        if txn_id is not None:
            from ba2_common.core.models import Transaction
            from ba2_common.core.trade_store import get_or_none
            txn = get_or_none(Transaction, txn_id)
            if txn is not None and txn.expiry is not None:
                candidates.add(self._as_date(txn.expiry))

        order_expiry = getattr(order, "expiry", None)
        if order_expiry is not None:
            candidates.add(self._as_date(order_expiry))

        candidates |= self._held_leg_expiries(txn_id)

        if not candidates:
            return None, (f"no expiry on the transaction, the order or any held leg of "
                          f"{self.instrument_name} — the remaining option life cannot be "
                          f"determined")
        if len(candidates) > 1:
            listed = ", ".join(str(e) for e in sorted(candidates))
            return None, (f"conflicting expiries on one {self.instrument_name} structure "
                          f"({listed}) — its remaining life is undefined")
        return candidates.pop(), ""

    def evaluate(self) -> bool:
        try:
            self.calculated_value = None
            self.unknown_reason = None

            as_of = self._as_of_date()
            if as_of is None:
                # Substituting the wall clock here is the lookahead bug DaysOpenedCondition's
                # docstring was written about; refusing is the only honest option.
                self.unknown_reason = ("no evaluation date on the recommendation — "
                                       "'days remaining' has no reference point")
                logger.warning(f"days_to_expiry for {self.instrument_name} is unevaluable: "
                               f"{self.unknown_reason}")
                return False

            expiry, blind = self._resolve_expiry()
            if expiry is None:
                self.unknown_reason = blind
                logger.warning(f"days_to_expiry for {self.instrument_name} is unevaluable: "
                               f"{blind}")
                return False

            days = (expiry - as_of).days
            self.calculated_value = days

            if days < 0:
                logger.warning(
                    f"{self.instrument_name} structure is {abs(days)} day(s) PAST its "
                    f"expiry {expiry.isoformat()} and still open")
            else:
                logger.debug(f"Days to expiry for {self.instrument_name}: {days} "
                             f"(expiry {expiry.isoformat()}, as of {as_of.isoformat()})")

            return self.operator_func(days, self.value)

        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating days-to-expiry for {self.instrument_name}: {e}",
                         exc_info=True)
            self.calculated_value = None
            self.unknown_reason = f"error computing the remaining option life: {e}"
            return False

    def get_description(self) -> str:
        return (f"Check if days until {self.instrument_name}'s option expiry is "
                f"{self.operator_str} {self.value}")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            reason = getattr(self, "unknown_reason", None)
            # Never None-and-silent, and never a number: the audit row must say that this
            # was not measured, and which input was missing.
            return f"unknown ({reason})" if reason else None
        days = int(self.calculated_value)
        if days < 0:
            return f"{days} DTE (expired {abs(days)}d ago)"
        return f"{days} DTE"


class HasOptionPositionCondition(FlagCondition):
    """Check if this expert has an open option position for the underlying."""

    def evaluate(self) -> bool:
        try:
            from ba2_common.core.trade_repository import get_trade_repository

            # The old select().join() is unavailable under the in-memory store (its join helper
            # is in-mem ONLY), so the repository expresses it as open-transactions -> their
            # orders, giving live and backtest one implementation.
            rows = get_trade_repository().open_option_orders(
                expert_id=self.expert_recommendation.instance_id,
                underlying=self.instrument_name,
            )
            self._has = len(rows) > 0
            return self._has

        except Exception as e:
            absorb_if_benign(e)
            logger.error(
                f"Error checking option position for {self.instrument_name}: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        return f"Check if this expert has an open option position for {self.instrument_name}"

    def get_actual_value_display(self) -> Optional[str]:
        has = getattr(self, '_has', None)
        if has is None:
            return None
        return f"Option position found: {'Yes' if has else 'No'}"


class HasCoveredCallCondition(FlagCondition):
    """Check if this expert has an open covered call (short CALL) on the underlying."""

    def evaluate(self) -> bool:
        try:
            from ba2_common.core.trade_repository import get_trade_repository
            from ba2_common.core.types import OptionRight, OrderDirection

            # See HasOptionPositionCondition for why this goes through the repository.
            rows = get_trade_repository().open_option_orders(
                expert_id=self.expert_recommendation.instance_id,
                underlying=self.instrument_name,
                option_type=OptionRight.CALL, side=OrderDirection.SELL,
                option_strategy="covered_call",
            )
            self._has = len(rows) > 0
            return self._has

        except Exception as e:
            absorb_if_benign(e)
            logger.error(
                f"Error checking covered call for {self.instrument_name}: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        return f"Check if this expert has an open covered call for {self.instrument_name}"

    def get_actual_value_display(self) -> Optional[str]:
        has = getattr(self, '_has', None)
        if has is None:
            return None
        return f"Covered call found: {'Yes' if has else 'No'}"


class HasProtectivePutCondition(FlagCondition):
    """Check if this expert has an open protective put (long PUT) on the underlying."""

    def evaluate(self) -> bool:
        try:
            from ba2_common.core.trade_repository import get_trade_repository
            from ba2_common.core.types import OptionRight, OrderDirection

            # See HasOptionPositionCondition for why this goes through the repository.
            rows = get_trade_repository().open_option_orders(
                expert_id=self.expert_recommendation.instance_id,
                underlying=self.instrument_name,
                option_type=OptionRight.PUT, side=OrderDirection.BUY,
                option_strategy="protective_put",
            )
            self._has = len(rows) > 0
            return self._has

        except Exception as e:
            absorb_if_benign(e)
            logger.error(
                f"Error checking protective put for {self.instrument_name}: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        return f"Check if this expert has an open protective put for {self.instrument_name}"

    def get_actual_value_display(self) -> Optional[str]:
        has = getattr(self, '_has', None)
        if has is None:
            return None
        return f"Protective put found: {'Yes' if has else 'No'}"


class HasAssignedSharesCondition(FlagCondition):
    """True when this expert holds stock it did NOT buy — the leg of an assigned short put.

    ``has_buy_position`` cannot express this. It fires on ANY open equity long the expert
    holds, so a wheel's covered-call overlay hung off it writes calls over ordinary stock
    the same expert bought outright, capping upside on a position that was never meant to
    be covered. That is the whole reason the assignment writes
    ``meta_data["origin"] = "csp_assignment"`` — until this condition existed nothing read
    it back.

    Deliberately a CONDITION rather than a narrowing of
    ``_OptionEntryAction._held_equity_shares``: coverage and eligibility are different
    questions. A covered call written over 100 assigned + 100 bought shares is still fully
    covered, so the SIZING input must keep counting every share whatever its provenance
    (narrowing it would under-size genuinely covered positions — the same bug, quieter).
    Whether the overlay should fire at all is a ruleset decision, and rulesets speak in
    conditions. Opt-in: existing rulesets are untouched; a wheel ANDs this into its trigger.
    """

    def evaluate(self) -> bool:
        try:
            from ba2_common.core.trade_repository import get_trade_repository
            from ba2_common.core.types import OrderDirection, TXN_ORIGIN_CSP_ASSIGNMENT

            # Repository, never a raw select(): Transaction is an IN_MEM_MODEL, so under the
            # backtest store a select() returns EMPTY instead of raising (see
            # HasBuyPositionCondition).
            open_txns = get_trade_repository().open_transactions(
                expert_id=self.expert_recommendation.instance_id,
                symbol=self.instrument_name, side=OrderDirection.BUY,
            )
            self._has = any(
                isinstance(t.meta_data, dict)
                and t.meta_data.get("origin") == TXN_ORIGIN_CSP_ASSIGNMENT
                for t in open_txns
            )
            return self._has
        except Exception as e:
            absorb_if_benign(e)
            logger.error(
                f"Error checking assigned shares for {self.instrument_name}: {e}", exc_info=True)
            return False

    def get_description(self) -> str:
        return (f"Check if this expert holds {self.instrument_name} shares acquired by "
                f"option assignment")

    def get_actual_value_display(self) -> Optional[str]:
        has = getattr(self, '_has', None)
        if has is None:
            return None
        return f"Assigned shares found: {'Yes' if has else 'No'}"


# Factory function to create conditions based on event type

# (from_rating, to_rating) for the six 3-bucket rating TRANSITION events, all served by
# RatingChangeCondition. Module level (not rebuilt per call) and importable, so the
# "every registered condition is reachable from a rule" invariant test can read the
# REAL registry rather than a copy of it that could drift.
RATING_CHANGE_CONDITIONS: Dict[ExpertEventType, tuple] = {
    ExpertEventType.F_RATING_NEGATIVE_TO_NEUTRAL: (OrderRecommendation.SELL, OrderRecommendation.HOLD),
    ExpertEventType.F_RATING_NEGATIVE_TO_POSITIVE: (OrderRecommendation.SELL, OrderRecommendation.BUY),
    ExpertEventType.F_RATING_NEUTRAL_TO_NEGATIVE: (OrderRecommendation.HOLD, OrderRecommendation.SELL),
    ExpertEventType.F_RATING_NEUTRAL_TO_POSITIVE: (OrderRecommendation.HOLD, OrderRecommendation.BUY),
    ExpertEventType.F_RATING_POSITIVE_TO_NEGATIVE: (OrderRecommendation.BUY, OrderRecommendation.SELL),
    ExpertEventType.F_RATING_POSITIVE_TO_NEUTRAL: (OrderRecommendation.BUY, OrderRecommendation.HOLD),
}

# ExpertEventType -> the TradeCondition subclass that implements it. The REGISTRY: every
# event type a rule can name must appear here (or in RATING_CHANGE_CONDITIONS above), and
# every entry here must have a rule_builders.FIELD_EVENT / FLAG_FIELD_EVENT mapping or a
# rule leaf naming it is silently dropped before the engine ever sees it. That invariant is
# enforced by tests/test_condition_registry_coverage.py.
CONDITION_MAP: Dict[ExpertEventType, type] = {
    ExpertEventType.F_BEARISH: BearishCondition,
    ExpertEventType.F_BULLISH: BullishCondition,
    ExpertEventType.F_HAS_NO_POSITION: HasNoPositionCondition,
    ExpertEventType.F_HAS_POSITION: HasPositionCondition,
    ExpertEventType.F_HAS_BUY_POSITION: HasBuyPositionCondition,
    ExpertEventType.F_HAS_SELL_POSITION: HasSellPositionCondition,
    ExpertEventType.F_HAS_NO_POSITION_ACCOUNT: HasNoPositionAccountCondition,
    ExpertEventType.F_HAS_POSITION_ACCOUNT: HasPositionAccountCondition,
    ExpertEventType.F_LONG_TERM: LongTermCondition,
    ExpertEventType.F_MEDIUM_TERM: MediumTermCondition,
    ExpertEventType.F_SHORT_TERM: ShortTermCondition,
    ExpertEventType.F_CURRENT_RATING_POSITIVE: CurrentRatingPositiveCondition,
    ExpertEventType.F_CURRENT_RATING_OVERWEIGHT: CurrentRatingOverweightCondition,
    ExpertEventType.F_CURRENT_RATING_NEUTRAL: CurrentRatingNeutralCondition,
    ExpertEventType.F_CURRENT_RATING_UNDERWEIGHT: CurrentRatingUnderweightCondition,
    ExpertEventType.F_CURRENT_RATING_NEGATIVE: CurrentRatingNegativeCondition,
    ExpertEventType.F_RATING_UPGRADED: RatingUpgradedCondition,
    ExpertEventType.F_RATING_DOWNGRADED: RatingDowngradedCondition,
    ExpertEventType.F_HIGHRISK: HighRiskCondition,
    ExpertEventType.F_MEDIUMRISK: MediumRiskCondition,
    ExpertEventType.F_LOWRISK: LowRiskCondition,
    ExpertEventType.F_NEW_TARGET_HIGHER: NewTargetHigherCondition,
    ExpertEventType.F_NEW_TARGET_LOWER: NewTargetLowerCondition,
    ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT: ExpectedProfitTargetPercentCondition,
    ExpertEventType.N_PERCENT_TO_CURRENT_TARGET: PercentToCurrentTargetCondition,
    ExpertEventType.N_PERCENT_TO_NEW_TARGET: PercentToNewTargetCondition,
    ExpertEventType.N_NEW_TARGET_PERCENT: NewTargetPercentCondition,
    ExpertEventType.N_PROFIT_LOSS_AMOUNT: ProfitLossAmountCondition,
    ExpertEventType.N_PROFIT_LOSS_PERCENT: ProfitLossPercentCondition,
    ExpertEventType.N_LOSS_PCT_OF_MAX_LOSS: LossPctOfMaxLossCondition,
    ExpertEventType.N_PROFIT_MULTIPLE_OF_PREMIUM: ProfitMultipleOfPremiumCondition,
    ExpertEventType.N_DAYS_OPENED: DaysOpenedCondition,
    ExpertEventType.N_DAYS_SINCE_LAST_CLOSE: DaysSinceLastCloseCondition,
    ExpertEventType.N_DAYS_SINCE_LAST_PROFITABLE_CLOSE: DaysSinceLastProfitableCloseCondition,
    ExpertEventType.N_DAYS_SINCE_LAST_LOSING_CLOSE: DaysSinceLastLosingCloseCondition,
    ExpertEventType.N_CONFIDENCE: ConfidenceCondition,
    ExpertEventType.N_PRICE_VS_TARGET_LOW_PERCENT: PriceVsTargetLowCondition,
    ExpertEventType.N_PRICE_VS_TARGET_HIGH_PERCENT: PriceVsTargetHighCondition,
    ExpertEventType.N_PRICE_VS_TARGET_CONSENSUS_PERCENT: PriceVsTargetConsensusCondition,
    ExpertEventType.N_INSTRUMENT_ACCOUNT_SHARE: InstrumentAccountShareCondition,
    ExpertEventType.N_PERCENT_OPEN_TO_NEW_TARGET: PercentOpenToNewTargetCondition,
    ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH: PercentBelowRecentHighCondition,
    ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW: PercentAboveRecentLowCondition,
    ExpertEventType.N_IV_RANK: IVRankCondition,
    ExpertEventType.N_RELATIVE_VOLUME: RelativeVolumeCondition,
    ExpertEventType.N_IV_TO_REALIZED_VOL: IVToRealizedVolCondition,
    ExpertEventType.N_DAYS_TO_EARNINGS: DaysToEarningsCondition,
    ExpertEventType.N_REC_DAYS_TO_EARNINGS: RecommendationDaysToEarningsCondition,
    ExpertEventType.N_DAYS_AFTER_EVENT: DaysAfterEventCondition,
    ExpertEventType.N_DAYS_TO_EXPIRY: DaysToExpiryCondition,
    ExpertEventType.F_HAS_OPTION_POSITION: HasOptionPositionCondition,
    ExpertEventType.F_HAS_COVERED_CALL: HasCoveredCallCondition,
    ExpertEventType.F_HAS_PROTECTIVE_PUT: HasProtectivePutCondition,
    ExpertEventType.F_HAS_ASSIGNED_SHARES: HasAssignedSharesCondition,
}


def create_condition(event_type: ExpertEventType, account: AccountInterface,
                    instrument_name: str, expert_recommendation: ExpertRecommendation,
                    existing_order: Optional[TradingOrder] = None,
                    operator_str: Optional[str] = None, value: Optional[float] = None) -> TradeCondition:
    """
    Factory function to create appropriate condition based on event type.
    """
    if event_type in RATING_CHANGE_CONDITIONS:
        from_rating, to_rating = RATING_CHANGE_CONDITIONS[event_type]
        return RatingChangeCondition(account, instrument_name, expert_recommendation,
                                   from_rating, to_rating, existing_order)
    condition_class = CONDITION_MAP.get(event_type)
    if not condition_class:
        raise ValueError(f"Unknown event type: {event_type}")
    if issubclass(condition_class, FlagCondition):
        return condition_class(account, instrument_name, expert_recommendation, existing_order)
    elif issubclass(condition_class, CompareCondition):
        if operator_str is None or value is None:
            raise ValueError(f"Operator and value required for numeric condition: {event_type}")
        return condition_class(account, instrument_name, expert_recommendation, operator_str, value, existing_order)
    else:
        raise ValueError(f"Unknown condition class type for: {event_type}")