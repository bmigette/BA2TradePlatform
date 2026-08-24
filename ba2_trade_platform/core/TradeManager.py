"""
TradeManager - Core component for handling trade recommendations and order execution

This class reviews trade recommendations from market experts and places orders based on
expert settings, rulesets, and trading permissions.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import threading
from ..logger import logger
from .models import ExpertRecommendation, ExpertInstance, TradingOrder, Ruleset, Transaction
from .types import OrderRecommendation, OrderStatus, OrderDirection, OrderOpenType, OrderType
from .db import get_instance, get_all_instances, add_instance, update_instance


# Serializes account refreshes across all entry points (scheduled job, immediate job,
# manual triggers). Dependent-order submission funnels through refresh_accounts; running
# two refreshes concurrently could submit the same dependent order twice.
_REFRESH_LOCK = threading.Lock()

# Market-regime benchmark + its once-per-day classification cache (see
# TradeManager._publish_market_regime). SPY: the classifier in ba2_common.core.market_regime was
# measured on it, and the backtest uses the same symbol so live and simulated agree.
_REGIME_BENCHMARK = "SPY"
_LIVE_REGIME_CACHE = {"day": None, "stressed": None}

# Wash-trade lock lifetime. A lock is a WAIT, and a wait that outlives its signal is a
# deadlock: an entry blocked by a protective stop on an open position can never clear on
# its own, because that stop stays working at the broker for the life of the position.
# Measured 2026-08-05: 13 entries stuck this way, the oldest for 9 days. Past the max age
# the order is cancelled rather than retried forever — a market entry signal a day stale is
# not one that should still fill. See docs/WASHTRADE-LOCK.md.
_WASHTRADE_LOCK_MAX_AGE_HOURS = 24.0
# Below the max age but past this, log at WARNING instead of DEBUG. The 2026-08-05 deadlock
# went unnoticed for 9 days precisely because a still-blocked order only logged at DEBUG.
_WASHTRADE_LOCK_WARN_AGE_HOURS = 1.0


def classify_waiting_trigger(parent_status, trigger_status):
    """Decide what to do with a WAITING_TRIGGER dependent order given its parent's current
    status and the configured trigger status.

    Returns one of:
        "submit" - parent reached the trigger status; submit the dependent now
        "cancel" - parent reached a different terminal status; the dependent can't fire
        "wait"   - keep waiting (includes the explicit PARTIALLY_FILLED no-op, H2)

    The trigger match is checked first so a dependent that waits for a terminal trigger
    (e.g. CANCELED in the cancel-replace flow) submits rather than being cancelled.
    """
    if trigger_status is not None and parent_status == trigger_status:
        return "submit"
    # H2: a partially-filled parent is still working toward FILLED — keep waiting, never
    # cancel. (PARTIALLY_FILLED is not terminal, so this is also the implicit default;
    # the explicit branch documents intent and is robust to status re-categorization.)
    if parent_status == OrderStatus.PARTIALLY_FILLED:
        return "wait"
    # A FILLED parent is FINAL even though FILLED is deliberately NOT in
    # get_terminal_statuses() (that set drives buying-power release, where a filled order
    # still holds a position and must not be treated as done). Without this branch a
    # dependent whose trigger was anything other than FILLED fell through to "wait" and
    # waited FOREVER, because the parent can never change status again.
    #
    # Observed live (dev account 3, JSPR txn 46, 2026-07-26): order 153, a SELL_STOP for the
    # 181 shares still held, chained on order 152 with trigger=CANCELED. 152 FILLED instead
    # of cancelling — a cancel/replace that lost the race — so 153 sat in WAITING_TRIGGER
    # with broker_order_id NULL for three days and the position carried NO protective stop
    # at the broker.
    #
    # Resolved as "submit" rather than "cancel" because the asymmetry is severe: the staged
    # leg is protective, so cancelling leaves an open position naked, whereas submitting a
    # possibly-stale quantity is already guarded — replacement_blocked_by_qty() defers a
    # submission the broker would reject for insufficient qty, and the caller independently
    # rejects a dependent whose quantity is <= 0. Over-protection is recoverable; no
    # protection is not.
    #
    # Scoped to dependents that DO have a configured trigger: trigger_status None means no
    # auto-submit was ever intended (see test_no_trigger_status_waits), and this branch must
    # not quietly override that contract.
    if trigger_status is not None and parent_status == OrderStatus.FILLED:
        return "submit"
    if parent_status in OrderStatus.get_terminal_statuses():
        return "cancel"
    return "wait"


def replacement_blocked_by_qty(trigger_status, available_qty, required_qty) -> bool:
    """Whether a cancel-and-replace order must keep waiting because the broker has
    not yet released the prior order's held position quantity.

    Only applies to replacements triggered by a prior order's CANCELED status (a
    trailing-stop raise / OCO swap). Returns False — i.e. proceed — when this isn't
    a cancel-triggered replacement, when the broker's available qty is unknown
    (``None``), or when enough qty is already available. This prevents submitting a
    replacement that the broker would reject with 40310000 "insufficient qty
    available" and then hard-ERROR (silently dropping the position's protection).
    """
    trig = getattr(trigger_status, "value", trigger_status)
    if str(trig).lower() != OrderStatus.CANCELED.value:
        return False
    if available_qty is None or required_qty is None:
        return False
    return available_qty < required_qty


def rebase_price_to_fill(target_price, reference_price, fill_price):
    """Re-scale a TP/SL target that was computed off a pre-fill reference so it keeps
    the same proportional distance from the order's ACTUAL fill.

    new = fill * (target / reference)

    Sign-agnostic (works for stops below and targets above) and a no-op when the
    reference already equals the fill. Returns target_price unchanged if any input
    is missing or the reference is non-positive.
    """
    if not target_price or not reference_price or not fill_price or reference_price <= 0:
        return target_price
    return round(fill_price * (target_price / reference_price), 4)


def resolve_entry_order(session, transaction):
    """The transaction's ENTRY order: its oldest FILLED order on the transaction's own side.

    This single row is what the open-positions pass hands to every condition as
    ``existing_order``, and a surprising amount hangs off it being found:
    ``DaysOpenedCondition``, ``ProfitLossAmountCondition`` and
    ``ProfitLossPercentCondition`` each return False unconditionally without one, and
    ``CloseAction`` only takes its ``close_transaction()`` path (the one that links the
    close back to the transaction, so ``has_pending_closing_order`` can see it) when the
    order carries a ``transaction_id``. A position whose shares arrived without an order
    row — an option assignment used to be exactly that — is therefore unmanageable and
    silently so. Extracted from ``process_open_positions_recommendations`` so that
    property is directly testable.
    """
    from sqlmodel import select

    if transaction is None:
        return None
    stmt = (
        select(TradingOrder)
        .where(
            TradingOrder.transaction_id == transaction.id,
            TradingOrder.side == transaction.side,
            TradingOrder.status == OrderStatus.FILLED
        )
        .order_by(TradingOrder.created_at.asc())
        .limit(1)
    )
    return session.exec(stmt).first()


class TradeManager:

    """
    Manages the execution of trade recommendations from market experts.
    
    Responsibilities:
    - Review expert recommendations
    - Apply trading rules and rulesets
    - Check expert permissions and settings
    - Execute trades through account interfaces
    - Track order status and results
    """
    
    def __init__(self):
        """Initialize the trade manager."""
        # Use the parent logger directly instead of getChild to avoid double logging
        # The parent logger already has all necessary handlers configured
        self.logger = logger
        # Lock dictionary for preventing concurrent processing of recommendations
        # Key format: "expert_{expert_id}_usecase_{use_case}"
        self._processing_locks: Dict[str, threading.Lock] = {}
        self._locks_dict_lock = threading.Lock()  # Lock for accessing the locks dictionary
    
    def _publish_market_regime(self) -> None:
        """Classify the benchmark ONCE per calendar day and publish it on the regime seam.

        Live counterpart of the backtest engine's per-bar ``set_stressed``. Both read the same
        ``ba2_common.core.market_regime`` classifier off the same daily closes, so an
        overlay-enabled genome behaves identically in a backtest and in production — the whole
        point of keeping the classifier in ``ba2_common``.

        Cached per day because the classifier's inputs are DAILY closes: re-fetching 3 years of
        SPY on every refresh (which runs many times a day) would buy nothing.

        A failure publishes ``None`` = "unclassified" = neutral scales, i.e. exactly today's
        behaviour. That is safe in LIVE (never trade off a guessed regime); the loud-failure
        stance belongs in the backtest, where a silently-neutral overlay would waste a grid.
        """
        from datetime import datetime, timedelta, timezone

        from ba2_common.core.market_regime import is_stressed
        from ba2_common.core.regime_overlay import set_stressed

        today = datetime.now(timezone.utc).date()
        if _LIVE_REGIME_CACHE["day"] != today:
            stressed = None
            try:
                from ba2_providers import get_provider
                # 1200 calendar days ~= 825 sessions, comfortably above the 524 daily closes
                # (20 vol window + 504 rank lookback) the classifier needs before it will answer.
                df = get_provider("ohlcv", "fmp").get_ohlcv_data(
                    _REGIME_BENCHMARK,
                    start_date=datetime.now(timezone.utc) - timedelta(days=1200),
                    end_date=datetime.now(timezone.utc),
                    interval="1d",
                )
                if df is not None and len(df):
                    stressed = is_stressed([c for c in df["Close"].tolist() if c is not None])
                else:
                    self.logger.warning(
                        f"Market regime: no {_REGIME_BENCHMARK} daily bars returned")
            except Exception as e:  # noqa: BLE001 -- regime is advisory; never block a refresh
                self.logger.warning(f"Market regime unavailable ({type(e).__name__}: {e})")
            _LIVE_REGIME_CACHE["day"] = today
            _LIVE_REGIME_CACHE["stressed"] = stressed
            self.logger.info(f"Market regime for {today}: stressed={stressed}")

        set_stressed(_LIVE_REGIME_CACHE["stressed"])

    def refresh_accounts(self):
        """
        Refresh account information for all registered accounts.
        
        This method iterates through all account definitions and calls their
        refresh methods to update account information, positions, and orders.

        Guarded by a non-blocking reentrancy lock: if a refresh is already running
        (e.g. the scheduled job overlapping an immediate/manual refresh), this call
        returns immediately. This prevents concurrent dependent-order submission.
        """
        if not _REFRESH_LOCK.acquire(blocking=False):
            self.logger.info("Account refresh already in progress — skipping this run")
            return
        try:
            # Publish the market regime for this pass BEFORE any order work, so the RM's sizing
            # and the ruleset's TP/SL actions all read the same value. Live counterpart of the
            # backtest engine's per-bar set_stressed(); cached per calendar day (the classifier
            # reads DAILY benchmark closes, so it cannot change intraday).
            self._publish_market_regime()

            from .models import AccountDefinition
            from ..modules.accounts import get_account_class
            from sqlmodel import select
            from .db import get_db

            # Get all account definitions
            account_definitions = get_all_instances(AccountDefinition)

            self.logger.info(f"Starting account refresh for {len(account_definitions)} accounts")

            # Step 1: Perform account refresh
            for account_def in account_definitions:
                try:
                    # Get the account class for this provider
                    account_class = get_account_class(account_def.provider)
                    if not account_class:
                        self.logger.warning(f"No account class found for provider {account_def.provider}")
                        continue
                    
                    # Create account instance
                    account = account_class(account_def.id)
                    
                    # Refresh account data if the method exists
                    if hasattr(account, 'refresh_positions'):
                        account.refresh_positions()
                        self.logger.debug(f"Refreshed positions for {account_def.name}")
                    
                    if hasattr(account, 'refresh_orders'):
                        # Use fetch_all=True on startup to ensure complete synchronization
                        account.refresh_orders(fetch_all=True)
                        self.logger.debug(f"Refreshed orders for {account_def.name}")
                    
                    if hasattr(account, 'refresh_transactions'):
                        account.refresh_transactions()
                        self.logger.debug(f"Refreshed transactions for {account_def.name}")

                    # Reconcile positions closed DIRECTLY at the broker (outside the
                    # platform): the order-driven refresh above can't detect those (no
                    # filled close order in our ledger), so a manually-closed position
                    # would otherwise stay OPENED forever. Closes OPENED transactions
                    # whose symbol the broker no longer holds. No-op on a positions-fetch
                    # error (never mass-closes the book). Live-only — the backtest engine
                    # does not call this refresh path.
                    if hasattr(account, 'reconcile_externally_closed_transactions'):
                        try:
                            closed = account.reconcile_externally_closed_transactions()
                            if closed:
                                self.logger.info(
                                    f"Reconciled {closed} externally-closed transaction(s) for {account_def.name}"
                                )
                        except Exception as e:
                            self.logger.error(
                                f"Error reconciling externally-closed transactions for {account_def.name}: {e}",
                                exc_info=True,
                            )

                    # Reconcile option assignment/exercise/expiry events from the
                    # broker (no-op for non-options accounts). Idempotent.
                    self._reconcile_account_option_activities(account)

                except Exception as e:
                    self.logger.error(f"Error refreshing account {account_def.name} (ID: {account_def.id}): {e}", exc_info=True)
                    continue

            # Step 2: Process WAITING_TRIGGER dependent orders whose parent reached its
            # trigger (single consolidated method; also recalculates TP/SL from fill).
            # NOTE: the Alpaca PENDING-dependent path (cancel-replace flow) is handled
            # separately inside each account's refresh_orders().
            self._check_all_waiting_trigger_orders()

            # Step 3: Re-submit WASHTRADE_LOCKED orders whose symbol is now clear of an
            # opposite-side order working at the broker (runs after Step 1 re-synced
            # broker order state).
            self._check_all_washtrade_locked_orders()

            self.logger.info("Account refresh completed")

        except Exception as e:
            self.logger.error(f"Error during account refresh: {e}", exc_info=True)
        finally:
            _REFRESH_LOCK.release()
    
    def _reconcile_account_option_activities(self, account):
        """Reconcile broker option lifecycle events (assignment / exercise / expiry)
        against the local Transaction ledger for one account.

        Called once per account during refresh, after broker order/position state has
        been re-synced. No-op for accounts that are not options-capable or do not
        expose the reconcile API. The underlying reconcile is idempotent (deduped by
        OptionActivity rows), so re-running each refresh is safe. Never raises — a
        broker hiccup here must not break the rest of the refresh cycle.
        """
        from .interfaces.OptionsAccountInterface import OptionsAccountInterface

        if not isinstance(account, OptionsAccountInterface):
            return
        if not (hasattr(account, "get_option_activities")
                and hasattr(account, "reconcile_option_assignments")):
            return

        try:
            from datetime import datetime, timezone, timedelta
            # Bounded lookback covers weekend expiry/assignment gaps; idempotency
            # makes the overlapping windows across refreshes harmless.
            after = datetime.now(timezone.utc) - timedelta(days=7)
            activities = account.get_option_activities(after=after)
            if not activities:
                return
            results = account.reconcile_option_assignments(activities)
            applied = [r for r in (results or [])
                       if not str(r.get("result", "")).startswith(("already_processed", "skipped"))]
            self.logger.info(
                f"[Account {account.id}] Option reconciliation: {len(activities)} activity(ies), "
                f"{len(applied)} newly applied"
            )
        except Exception as e:
            self.logger.error(
                f"[Account {getattr(account, 'id', '?')}] Option reconciliation failed: {e}",
                exc_info=True
            )

    # ======================================================================
    # Daily ATM-IV recorder — the series behind IVRankCondition
    # ======================================================================
    def _resolve_options_account(self, account_id: int):
        """Return ``(AccountDefinition | None, options-capable account | None)``.

        The account is None (having logged why) for a missing definition, an unknown
        provider, a construction failure, or an equity-only account. Callers here are
        scheduled jobs, so a single bad account must never abort the pass.
        """
        from .interfaces.OptionsAccountInterface import OptionsAccountInterface
        from ..modules.accounts import get_account_class
        from .models import AccountDefinition

        try:
            account_def = get_instance(AccountDefinition, account_id)
            if account_def is None:
                self.logger.warning(
                    f"iv_rank-gated rules reference account {account_id}, which no longer exists")
                return None, None
            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.warning(
                    f"No account class for provider {account_def.provider} "
                    f"(account {account_def.name}); cannot record ATM IV")
                return account_def, None
            account = account_class(account_id)
            if not isinstance(account, OptionsAccountInterface):
                self.logger.warning(
                    f"Account {account_def.name} hosts iv_rank-gated rules but is not "
                    f"options-capable; those rules can never fire")
                return account_def, None
            return account_def, account
        except Exception as e:
            self.logger.error(
                f"Could not resolve account {account_id} for ATM-IV recording: {e}",
                exc_info=True)
            return None, None

    def record_daily_iv_snapshots(self):
        """Record ONE trailing ATM-IV sample per iv_rank-gated underlying.

        CADENCE: once per weekday, after the US close (JobManager's
        ``IV_SNAPSHOT_JOB_ID`` cron). ``get_iv_rank`` is an unweighted percentile over
        a 252-day window, so the sample GRID is part of the statistic's definition, not
        an implementation detail: hanging this off the 5-minute account refresh would
        put ~288 rows/day into the window and quietly redefine "IV rank" as a
        last-few-days percentile. ``record_atm_iv`` is idempotent per UTC day, so a
        coalesced/missed run, a manual trigger or an app restart cannot double-sample —
        this method is safe to call as often as you like.

        WHAT is sampled comes from ``iv_rank_audit.recording_targets()``: only the
        underlyings of enabled experts that actually have an iv_rank-gated rule. Each
        symbol costs one full option-chain request, so sampling the whole account
        universe would be a large daily bill for series nothing reads.

        A symbol whose chain carries no IV is SKIPPED and named in the log. Nothing is
        ever interpolated, carried forward or defaulted: this table feeds a live
        trading gate, and a fabricated sample would arm real option rules off invented
        data. A missing sample leaves ``IVRankCondition`` failing closed instead.
        """
        from ba2_common.core.iv_rank_audit import find_iv_rank_gates, recording_targets

        gates = find_iv_rank_gates()
        targets = recording_targets(gates)
        blind = [g for g in gates if not g.universe_is_known]
        if blind:
            # NOT a debug line. "No targets" and "I cannot see what the targets are" are
            # opposite facts, and the second one means live rules stay permanently inert.
            self.logger.warning(
                f"ATM-IV recorder cannot see the instrument universe of {len(blind)} "
                f"iv_rank-gated expert(s): "
                + "; ".join(f"{g.expert_id} ({g.expert})"
                            + (f" selects via {'/'.join(g.deferred_modes)} and has not "
                               f"analysed anything recently" if g.deferred_modes
                               else " could not be resolved")
                            + f" — inert rule(s): {', '.join(g.rule_names)}"
                            for g in blind)
                + ". No ATM-IV series is being kept for them, so those rules cannot fire.")
        if not targets:
            if not gates:
                self.logger.debug(
                    "No iv_rank-gated rules configured; skipping ATM-IV recording")
            return

        total_recorded = 0
        total_missing = 0
        for account_id, symbols in sorted(targets.items()):
            account_def, account = self._resolve_options_account(account_id)
            if account is None:
                total_missing += len(symbols)
                continue

            recorded, missing = 0, []
            for symbol in symbols:
                try:
                    if account.record_atm_iv(symbol) is None:
                        missing.append(symbol)
                    else:
                        recorded += 1
                except Exception as e:
                    missing.append(symbol)
                    self.logger.error(
                        f"[Account {account_def.name}] ATM-IV sampling failed for {symbol}: {e}",
                        exc_info=True)

            total_recorded += recorded
            total_missing += len(missing)
            self.logger.info(
                f"[Account {account_def.name}] ATM-IV series up to date for "
                f"{recorded}/{len(symbols)} iv_rank-gated underlying(s)")
            if missing:
                self.logger.warning(
                    f"[Account {account_def.name}] No ATM IV available for "
                    f"{len(missing)}/{len(symbols)} iv_rank-gated underlying(s): "
                    f"{', '.join(missing)}. No sample was recorded for them (a rank is "
                    f"never fabricated), so their iv_rank gates stay CLOSED.")

        if total_recorded == 0 and total_missing > 0:
            self.logger.error(
                f"ATM-IV recorder sampled NOTHING ({total_missing} underlying(s) attempted). "
                f"Every iv_rank-gated rule is permanently inert until this is fixed. Most "
                f"likely the broker's option feed returns no implied_volatility on the "
                f"configured feed — check the account's options_feed setting.")

    def report_iv_rank_readiness(self):
        """Log which iv_rank-gated rules are inert and which are armed.

        Called once at JobManager startup. Nine live rules across seven experts have
        never been able to fire (no ATM-IV history existed, so ``get_iv_rank`` always
        returned None and ``IVRankCondition`` always failed closed). Once the recorder
        has run ``IV_RANK_MIN_SAMPLES`` times they begin placing real orders. That is a
        deliberate change of behaviour and the operator should read it in the startup
        log, not infer it from an unexpected fill.

        Deliberately counts samples only — it does NOT compute the rank, because that
        would fetch a full option chain per symbol on every app start.

        NO GATE MAY BE RENDERED AS A COUNT THE REPORT CANNOT STAND BEHIND. An expert
        whose universe is UNKNOWN (a screener that has not run, a resolver that raised)
        has no denominator, and printing one — ``0/0 underlying(s) ARMED`` — is the worst
        available output: literally true, indistinguishable from a healthy start, and
        emitted precisely when the recorder is blind and the rules are dead. Those gates
        go to WARNING with the rules they are killing named, and the summary line carries
        the count so it cannot read as an all-clear.
        """
        from ba2_common.core import iv_rank_audit as iv_audit
        from ba2_common.core.iv_rank_audit import find_iv_rank_gates, UNIVERSE_CONFIGURED
        from ba2_common.core.TradeConditions import IVRankCondition

        gates = find_iv_rank_gates()
        if not gates:
            return

        min_samples = IVRankCondition.IV_RANK_MIN_SAMPLES
        self.logger.info(
            f"IV-rank gate readiness: {len(gates)} enabled expert(s) have iv_rank-gated "
            f"rules. A gate cannot fire until {min_samples} trailing daily ATM-IV samples "
            f"exist for its underlying; once they do, those rules start trading.")

        accounts = {}
        armed_total = 0
        blind_total = 0
        for gate in gates:
            if gate.account_id not in accounts:
                accounts[gate.account_id] = self._resolve_options_account(gate.account_id)[1]
            account = accounts[gate.account_id]
            rules = ', '.join(gate.rule_names)
            self.logger.info(
                f"  expert {gate.expert_id} ({gate.expert}) — gated rule(s): {rules}")

            if not gate.universe_is_known:
                blind_total += 1
                why = (f"selects instruments via {'/'.join(gate.deferred_modes)} and has "
                       f"analysed nothing in the last "
                       f"{iv_audit.DEFERRED_UNIVERSE_LOOKBACK_DAYS} days"
                       if gate.deferred_modes else
                       "its instrument universe could not be resolved (see the error above)")
                self.logger.warning(
                    f"    UNIVERSE UNKNOWN — expert {gate.expert_id} ({gate.expert}) {why}, "
                    f"so no ATM-IV series is being recorded for it and its rule(s) "
                    f"({rules}) CANNOT FIRE. This is not the same as having no "
                    f"underlyings: the recorder does not know what to record.")
                continue

            if not gate.symbols:
                self.logger.warning(
                    f"    NO UNDERLYINGS — expert {gate.expert_id} ({gate.expert}) has an "
                    f"iv_rank-gated rule ({rules}) but no enabled instruments configured, "
                    f"so it has nothing to trade and nothing to sample.")
                continue

            if account is None:
                self.logger.info(
                    f"    {len(gate.symbols)} underlying(s) — UNKNOWN (account "
                    f"{gate.account_id} unavailable; see the warning above)")
                continue

            counts = {s: account.iv_sample_count(s) for s in gate.symbols}
            armed = [s for s, c in counts.items() if c >= min_samples]
            inert = [s for s, c in counts.items() if c < min_samples]
            armed_total += len(armed)
            origin = ("" if gate.universe_source == UNIVERSE_CONFIGURED else
                      f" [{gate.universe_source}: recovered from the last "
                      f"{iv_audit.DEFERRED_UNIVERSE_LOOKBACK_DAYS} days of analyses, not a "
                      f"configured list — it moves with the {'/'.join(gate.deferred_modes)}]")
            self.logger.info(
                f"    {len(armed)}/{len(gate.symbols)} underlying(s) ARMED{origin}"
                + (f": {self._sample_list(armed, counts, min_samples)}" if armed else "")
                + (f"; INERT (iv_rank is None, so the rule cannot fire): "
                   f"{self._sample_list(inert, counts, min_samples)}" if inert else ""))

        self.logger.info(
            f"IV-rank gate readiness: {armed_total} underlying(s) armed across "
            f"{len(gates)} expert(s)"
            + (f"; {blind_total} expert(s) with an UNKNOWN universe whose gated rules "
               f"cannot fire at all" if blind_total else ""))

    @staticmethod
    def _sample_list(symbols, counts, min_samples, limit: int = 12) -> str:
        """``AAPL 3/5, MSFT 5/5, ... (+18 more)`` — capped so a 30-name universe stays
        one readable log line."""
        head = ", ".join(f"{s} {counts[s]}/{min_samples}" for s in symbols[:limit])
        extra = len(symbols) - limit
        return head + (f", (+{extra} more)" if extra > 0 else "")

    def _check_all_washtrade_locked_orders(self):
        """Re-submit WASHTRADE_LOCKED orders whose symbol no longer has an opposite-side
        order working at the broker.

        Called each refresh after broker order state has been re-synced (Step 1). For
        each locked order, re-run the opposing-order scan via the account; if the symbol
        is clear, reset to PENDING and re-submit through ``submit_order`` (which re-runs
        validation and the wash-trade gate). Still-blocked orders are left locked.
        """
        from sqlmodel import select
        from .db import get_db
        from ..modules.accounts import get_account_class
        from .models import AccountDefinition

        with get_db() as session:
            locked = session.exec(
                select(TradingOrder).where(TradingOrder.status == OrderStatus.WASHTRADE_LOCKED)
            ).all()
            locked_ids = [o.id for o in locked]

        if not locked_ids:
            self.logger.debug("No WASHTRADE_LOCKED orders to check")
            return

        self.logger.info(f"Checking {len(locked_ids)} WASHTRADE_LOCKED orders")
        account_cache: Dict[int, Any] = {}

        for order_id in locked_ids:
            try:
                order = get_instance(TradingOrder, order_id)
                if not order or order.status != OrderStatus.WASHTRADE_LOCKED:
                    continue  # changed since we listed it

                account = account_cache.get(order.account_id)
                if account is None:
                    account_def = get_instance(AccountDefinition, order.account_id)
                    if not account_def:
                        self.logger.error(f"Account {order.account_id} not found for locked order {order_id}")
                        continue
                    account_class = get_account_class(account_def.provider)
                    if not account_class:
                        self.logger.warning(f"No account class for provider {account_def.provider}")
                        continue
                    account = account_class(account_def.id)
                    account_cache[order.account_id] = account

                # Same parent exclusion as submit_order: a dependent leg's own parent is a
                # genuine bracket pair, not a wash-trade blocker — without this a protective leg
                # could sit locked behind the very entry it protects.
                blocker = account._find_opposing_working_order(
                    order.symbol, order.side,
                    exclude_order_id=getattr(order, 'depends_on_order', None),
                )
                if blocker is not None:
                    age_hours = self._washtrade_lock_age_hours(order)
                    if age_hours is not None and age_hours >= _WASHTRADE_LOCK_MAX_AGE_HOURS:
                        self._expire_washtrade_locked_order(order, blocker, age_hours)
                        continue
                    msg = (
                        f"Order {order_id} ({order.symbol} {order.side.value}) still locked "
                        f"after {age_hours:.1f}h: opposite-side order {blocker.id} "
                        f"({blocker.order_type.value}) working at broker"
                        if age_hours is not None else
                        f"Order {order_id} ({order.symbol} {order.side.value}) still locked: "
                        f"opposite-side order {blocker.id} working at broker"
                    )
                    if age_hours is not None and age_hours >= _WASHTRADE_LOCK_WARN_AGE_HOURS:
                        self.logger.warning(msg)
                    else:
                        self.logger.debug(msg)
                    continue

                # Infer whether this is a closing order (side opposite to its position).
                is_closing = False
                if order.transaction_id:
                    txn = get_instance(Transaction, order.transaction_id)
                    if txn and txn.side != order.side:
                        is_closing = True

                self.logger.info(
                    f"Symbol {order.symbol} clear — re-submitting WASHTRADE_LOCKED order {order_id} "
                    f"(is_closing_order={is_closing})"
                )
                order.status = OrderStatus.PENDING
                update_instance(order)
                # Re-thread the safeguard SL: the ORIGINAL submit early-returned at the washtrade
                # lock BEFORE the TP/SL bracket block ran, so no protective leg exists yet. For a
                # MARKET entry, order.stop_price carries the risk manager's safeguard SL (see
                # _risk_atr_quantity) — pass it as sl_price so the WAITING_TRIGGER protective leg
                # is created now, exactly like the first submit would have. Restricted to MARKET
                # non-closing primaries: on stop/stop-limit types stop_price is the entry TRIGGER,
                # not a protective stop, and closing orders need no bracket.
                sl_price = None
                if (order.order_type == OrderType.MARKET and not is_closing
                        and not order.depends_on_order):
                    sl_price = order.stop_price or None
                account.submit_order(order, sl_price=sl_price, is_closing_order=is_closing)
            except Exception as e:
                self.logger.error(f"Error processing WASHTRADE_LOCKED order {order_id}: {e}", exc_info=True)

    @staticmethod
    def _washtrade_lock_age_hours(order) -> Optional[float]:
        """Hours since ``order`` was created, or None when it carries no timestamp.

        A missing ``created_at`` must never be treated as age 0 (would disable the expiry
        silently) nor as infinitely old (would cancel a fresh order); callers skip the age
        checks entirely on None.
        """
        created_at = getattr(order, 'created_at', None)
        if not created_at:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created_at).total_seconds() / 3600.0

    def _expire_washtrade_locked_order(self, order, blocker, age_hours: float) -> None:
        """Give up on a lock that can no longer clear: cancel the order and fail its transaction.

        Safe to do without talking to the broker — WASHTRADE_LOCKED is an unsent status (see
        OrderStatus.get_unsent_statuses()), so nothing is working at the broker for this order.

        Also cancels any WAITING_TRIGGER protective leg hanging off it. Those legs wait on a
        parent status that will now never arrive, so leaving them would just move the leak.
        """
        self._fail_unsent_entry(
            order,
            f"locked {age_hours:.1f}h (limit {_WASHTRADE_LOCK_MAX_AGE_HOURS}h) behind "
            f"opposite-side order {blocker.id} ({blocker.order_type.value}, still working); "
            f"the entry signal is stale and this blocker may never clear",
        )

    @staticmethod
    def _row_or_none(model_class, row_id):
        """``get_instance`` that answers None instead of raising InstanceNotFound.

        "The row is gone" is a legitimate answer for a compensation routine — it means there is
        nothing left to compensate — and must not abort the caller.
        """
        from .db import InstanceNotFound
        if not row_id:
            return None
        try:
            return get_instance(model_class, row_id)
        except InstanceNotFound:
            return None

    def _fail_unsent_entry(self, order, reason: str) -> bool:
        """Cancel an entry that never reached the broker, and release everything behind it.

        WHY IT IS SHARED. Two places give up on an entry — the wash-trade lock expiry and a
        failed submit in the funded-entry loop — and both leave the SAME debris: the entry row,
        its WAITING_TRIGGER protective legs, and the Transaction that ``submit_order`` created
        in WAITING before anything went wrong. The transaction is the expensive one. The
        enter_market SAFETY CHECK refuses any symbol+expert that already has an OPENED or
        WAITING transaction, and nothing sweeps a stranded WAITING — so one failed submit
        retires that symbol for that expert permanently. Measured on PROD: CVS 2026-08-10 and
        WSC 2026-08-24, both funded by the RM, both lost, both leaving the symbol blocked.

        SAFETY — WHY THIS CAN NEVER CANCEL A LIVE ORDER. Writing CANCELED onto an order that is
        actually working at the broker would be far worse than the leak it fixes: the position
        opens and the platform cannot see it. So the compensation is refused unless the
        in-memory object AND the row on disk BOTH agree that

          * the order carries no ``broker_order_id`` (brokers hand one back on acceptance), and
          * its status is one of ``OrderStatus.get_unsent_statuses()`` — PENDING,
            WAITING_TRIGGER, WASHTRADE_LOCKED — i.e. states that exist only in this database.

        Anything the broker has touched (NEW/ACCEPTED/FILLED/... ) or that the submit path has
        already marked ERROR falls outside that set and is left strictly alone.

        Returns True when it compensated, False when it refused or had nothing to do.
        """
        from sqlmodel import select
        from .db import get_db
        from .types import TransactionStatus

        order_id = getattr(order, 'id', None)
        if not order_id:
            self.logger.error(f"Cannot compensate an unsaved entry order ({reason})")
            return False

        on_disk = self._row_or_none(TradingOrder, order_id)
        if on_disk is None:
            self.logger.warning(
                f"Order {order_id} no longer exists — nothing to compensate ({reason})")
            return False

        unsent = OrderStatus.get_unsent_statuses()
        for view, where in ((order, "in memory"), (on_disk, "on disk")):
            broker_id = getattr(view, 'broker_order_id', None)
            if broker_id:
                self.logger.error(
                    f"REFUSING to compensate order {order_id} ({order.symbol}): it carries "
                    f"broker id {broker_id} ({where}), so it REACHED the broker — cancelling it "
                    f"in the database would hide a live order. Reason was: {reason}")
                return False
            if view.status not in unsent:
                self.logger.error(
                    f"REFUSING to compensate order {order_id} ({order.symbol}): status is "
                    f"{view.status} ({where}), which is not an unsent status — it may be working "
                    f"at the broker. Reason was: {reason}")
                return False

        self.logger.warning(
            f"Order {order_id} ({order.symbol} {order.side.value} {order.quantity}) never "
            f"reached the broker — cancelling: {reason}"
        )
        order.status = OrderStatus.CANCELED
        update_instance(order)

        with get_db() as session:
            dependents = session.exec(
                select(TradingOrder).where(
                    TradingOrder.depends_on_order == order_id,
                    TradingOrder.status == OrderStatus.WAITING_TRIGGER,
                )
            ).all()
            dependent_ids = [d.id for d in dependents]
        for dep_id in dependent_ids:
            dep = self._row_or_none(TradingOrder, dep_id)
            if dep and dep.status == OrderStatus.WAITING_TRIGGER:
                dep.status = OrderStatus.CANCELED
                update_instance(dep)
                self.logger.info(f"Cancelled protective leg {dep_id} of unsent order {order_id}")

        # Only fail a transaction this order never managed to open. An OPENED transaction has
        # other filled orders behind it and a real position at the broker; the unsent order was
        # an add-on, not the position itself.
        #
        # The transaction id is read from the IN-MEMORY object first: submit_order creates the
        # Transaction and stamps trading_order.transaction_id, then persists it with a SEPARATE
        # update_instance. When that update is what failed, the row on disk has no
        # transaction_id at all while a WAITING transaction very much exists — and that orphan
        # is precisely what would keep blocking the symbol.
        transaction_id = getattr(order, 'transaction_id', None) or on_disk.transaction_id
        if transaction_id:
            txn = self._row_or_none(Transaction, transaction_id)
            if txn and txn.status == TransactionStatus.WAITING:
                txn.status = TransactionStatus.FAILED
                update_instance(txn)
                self.logger.warning(
                    f"Transaction {txn.id} ({txn.symbol}) marked FAILED — its entry order "
                    f"{order_id} never reached the broker; leaving it WAITING would block every "
                    f"future entry for this symbol+expert"
                )
        return True

    def _persist_funded_entry(self, order, quantity: float, stop_price=None) -> None:
        """Write the risk manager's decision onto the entry row BEFORE the broker is called.

        WHY IT MUST BE BEFORE. At this point the RM-sized quantity and the safeguard stop exist
        only in memory, on a TRANSIENT candidate object that is about to be thrown away — see
        ``_submit_funded_entry_with_retry``'s own docstring: "re-deriving them later from a
        stranded row is not possible". Everything that can go wrong from here (a lock, a broker
        rejection, a crash, a restart) leaves a row on disk, and that row is either sized and
        re-drivable or it is a dead qty-0 stub nothing can reconstruct. Order 460 on PROD
        2026-08-10 was the latter and sat untouched for 8 hours.

        Persisting first also makes the broker submit the ONLY thing left that can fail, which
        is what lets the compensation in ``_fail_unsent_entry`` be a simple, provable statement
        about a row rather than a guess about how far the submit got.

        ``stop_price`` mirrors what the DB sizing path already does. There the candidate IS the
        persisted row, so ``_ensure_safeguard_stop``'s write lands in the database for free; the
        temp-order-list path sizes a transient candidate and only ever passed the stop as a
        ``submit_order`` argument, leaving the row's ``stop_price`` null. That null is what
        ``_check_all_washtrade_locked_orders`` re-reads to rebuild the protective leg when it
        re-submits a cleared lock — with no stop on the row it re-sends the entry naked.

        An explicit stop the ruleset already put on the order always wins, exactly as in
        ``_ensure_safeguard_stop``: the RM's is a SAFEGUARD, not an override.

        Safe to call here only because the order is DETACHED (see the enter loop's read-only
        session invariant). On an attached row this update_instance is the self-deadlock.
        """
        order.quantity = quantity
        if stop_price and not order.stop_price:
            order.stop_price = stop_price
        update_instance(order)

    # Transient DB-lock retries for a FUNDED entry. See _submit_funded_entry_with_retry.
    _ENTRY_SUBMIT_RETRIES = 3
    _ENTRY_SUBMIT_BACKOFF_S = 2.0

    def _submit_funded_entry_with_retry(self, account, order, sl_price=None):
        """Submit a funded entry, retrying when the DB (not the broker) is what failed.

        WHY. A sqlite write lock must never cost a trade. Measured on PROD 2026-08-10: CVS was
        screened, passed its ruleset and was FUNDED by the RM (1 share @ $95.69), then
        submit_order's Transaction insert hit "database is locked". db.retry_on_lock burns 4
        attempts against a 30s busy_timeout -- ~2 minutes -- then re-raises; the caller logged it
        and moved on. The trade was simply lost, leaving a stranded qty-0 PENDING row with no
        transaction and no broker id. Nothing sweeps those: the only code that touches PENDING
        entries DELETES them, and order 460 sat untouched for 8 hours. Dev is worse -- 161 lock
        events in a day and 5 entries lost the same way.

        The lock is transient and the retry is trivially correct HERE, because at this point the
        RM-sized quantity and safeguard stop are still in memory (fo.quantity / fo.stop_price);
        re-deriving them later from a stranded row is not possible, which is why this belongs in
        the funded loop and not in a sweeper.

        ONLY lock failures are retried. A broker rejection, a validation error or a wash-trade
        lock is a real answer and must not be re-attempted -- re-sending on those risks a
        duplicate order, which is far worse than a missed one.
        """
        import time as _time

        last_err = None
        for attempt in range(1, self._ENTRY_SUBMIT_RETRIES + 1):
            try:
                return account.submit_order(order, sl_price=sl_price)
            except Exception as e:  # noqa: BLE001 — classified immediately below
                if "database is locked" not in str(e).lower():
                    raise  # a real answer from the broker/validator: never re-send
                last_err = e
                if attempt < self._ENTRY_SUBMIT_RETRIES:
                    delay = self._ENTRY_SUBMIT_BACKOFF_S * attempt
                    self.logger.warning(
                        f"Entry submit for order {order.id} ({order.symbol}) lost to a DB lock "
                        f"(attempt {attempt}/{self._ENTRY_SUBMIT_RETRIES}); retrying in {delay:.0f}s")
                    _time.sleep(delay)
        self.logger.error(
            f"Entry submit for order {order.id} ({order.symbol}) ABANDONED after "
            f"{self._ENTRY_SUBMIT_RETRIES} DB-lock retries: {last_err}. The RM funded this trade "
            f"and it never reached the broker; order {order.id} is left PENDING with no broker id.",
            exc_info=True)
        return None

    def _check_all_waiting_trigger_orders(self):
        """
        Check all orders in WAITING_TRIGGER status to see if their parent orders have reached the trigger status.
        
        This method is called periodically to ensure no orders get stuck waiting for triggers,
        catching cases where status change detection may have missed an update.
        """
        try:
            from sqlmodel import select
            from .db import get_db
            from ..modules.accounts import get_account_class
            from .models import AccountDefinition
            
            # PHASE 1: Collect all orders to process WHILE session is open
            orders_to_submit = []  # List of (order, parent_order_id)
            status_updates = {}  # Map of order_id -> new_status
            # True if the SL-rebase or TP-floor-recheck blocks below wrote a Transaction field
            # (txn.stop_loss / txn.take_profit). Those updates live only in this Phase-1 session
            # and are otherwise silently lost on close: order.limit_price/stop_price mutations
            # happen to survive because Phase 2 re-persists the SAME order object via
            # update_instance()/account.submit_order(), but nothing analogous ever re-touches
            # Transaction, so without this flag the commit below (previously gated on
            # status_updates alone) would never fire for a pure rebase/floor-recheck run.
            transaction_field_updates = False
            
            with get_db() as session:
                # Phase 2 keeps using these SAME order objects (in orders_to_submit) after this
                # session/commit — expire_on_commit's default would detach their already-loaded
                # attributes on commit, raising DetachedInstanceError once the session below
                # closes. Keep the in-memory values usable across the commit; Phase 2 re-fetches
                # explicitly wherever it needs a truly fresh read (e.g. the `fresh = get_instance(...)`
                # re-fetch on submit failure).
                session.expire_on_commit = False
                # Get all orders in WAITING_TRIGGER status
                statement = select(TradingOrder).where(
                    TradingOrder.status == OrderStatus.WAITING_TRIGGER,
                    TradingOrder.depends_on_order.isnot(None),
                    TradingOrder.depends_order_status_trigger.isnot(None)
                )
                waiting_orders = session.exec(statement).all()
                
                if not waiting_orders:
                    self.logger.debug("No orders in WAITING_TRIGGER status")
                    return
                
                self.logger.info(f"Checking {len(waiting_orders)} orders in WAITING_TRIGGER status")
                
                for dependent_order in waiting_orders:
                    try:
                        parent_order_id = dependent_order.depends_on_order
                        trigger_status = dependent_order.depends_order_status_trigger
                        
                        # Get current parent order status
                        parent_order = session.get(TradingOrder, parent_order_id)
                        if not parent_order:
                            self.logger.warning(f"Parent order {parent_order_id} not found for dependent order {dependent_order.id} - setting to ERROR")
                            status_updates[dependent_order.id] = OrderStatus.ERROR
                            continue
                        
                        current_status = parent_order.status
                        
                        self.logger.debug(f"Checking order {dependent_order.id}: parent {parent_order_id} status is {current_status}, trigger is {trigger_status}")

                        # Decide: submit (parent reached trigger), cancel (parent hit a
                        # different terminal status), or wait (still in flight, incl. the
                        # explicit PARTIALLY_FILLED no-op).
                        action = classify_waiting_trigger(current_status, trigger_status)
                        if action == "submit":
                            self.logger.info(f"Parent order {parent_order_id} is in trigger status {trigger_status}, processing dependent order {dependent_order.id}")
                            
                            # Get the account for this dependent order
                            account_def = session.get(AccountDefinition, dependent_order.account_id)
                            if not account_def:
                                self.logger.error(f"Account definition {dependent_order.account_id} not found for dependent order {dependent_order.id} - setting to ERROR")
                                status_updates[dependent_order.id] = OrderStatus.ERROR
                                continue
                            
                            # Copy quantity from parent order if dependent order quantity is 0
                            if dependent_order.quantity == 0:
                                if parent_order.quantity > 0:
                                    self.logger.info(
                                        f"Copying quantity {parent_order.quantity} from parent order {parent_order_id} "
                                        f"(symbol: {parent_order.symbol}) to dependent order {dependent_order.id} (symbol: {dependent_order.symbol})"
                                    )
                                    dependent_order.quantity = parent_order.quantity
                                else:
                                    self.logger.error(
                                        f"Cannot submit dependent order {dependent_order.id} (symbol: {dependent_order.symbol}): "
                                        f"quantity is 0 and parent order {parent_order_id} (symbol: {parent_order.symbol}) "
                                        f"also has quantity 0. Setting dependent order to ERROR status."
                                    )
                                    status_updates[dependent_order.id] = OrderStatus.ERROR
                                    continue
                            
                            # ===== Re-base the STOP-LOSS to the parent's actual fill =====
                            # Market entries compute TP/SL at enter time off a PRE-FILL
                            # reference (the market order has no fill yet), so a fill that
                            # differs from that reference leaves the stop at the wrong
                            # distance. Re-scale the SL proportionally against the real fill
                            # using the reference anchor stored on the order. The TP
                            # (limit_price) is intentionally left untouched.
                            transaction_updated = False
                            try:
                                ref_price = (dependent_order.data or {}).get("tpsl_reference_price") \
                                    if isinstance(dependent_order.data, dict) else None
                                if ref_price and parent_order.open_price and dependent_order.stop_price:
                                    new_sl = rebase_price_to_fill(
                                        dependent_order.stop_price, ref_price, parent_order.open_price)
                                    if new_sl and abs(new_sl - dependent_order.stop_price) > 1e-9:
                                        old_sl = dependent_order.stop_price
                                        dependent_order.stop_price = new_sl
                                        self.logger.info(
                                            f"Re-based SL for order {dependent_order.id} to parent fill: "
                                            f"${old_sl:.4f} -> ${new_sl:.4f} "
                                            f"(ref ${ref_price:.4f}, fill ${parent_order.open_price:.4f})"
                                        )
                                        txn = session.get(Transaction, dependent_order.transaction_id) \
                                            if dependent_order.transaction_id else None
                                        if txn:
                                            txn.stop_loss = new_sl
                                            session.add(txn)
                                            transaction_field_updates = True
                                        if isinstance(dependent_order.data, dict):
                                            dependent_order.data["sl_rebased_to_fill"] = True
                                            dependent_order.data["parent_filled_price"] = parent_order.open_price
                            except (KeyError, TypeError, ValueError) as rebase_err:
                                self.logger.warning(
                                    f"Could not re-base SL for order {dependent_order.id}: {rebase_err}")

                            # ===== Re-check the TAKE-PROFIT floor against the parent's actual fill =====
                            # AdjustTakeProfitAction._enforce_minimum_distance() enforces
                            # min_take_profit_percent against the "real fill price" — but for a
                            # MARKET-order entry it runs in Phase 2, before the order is even
                            # submitted, so limit_price/open_price are both still None and it can
                            # only fall back to a live quote snapshot, not the actual fill. Re-run
                            # that same floor check now that the real fill is known, and bump the
                            # TP up (never down) if it's under-floor. Unlike SL this is a re-check
                            # of an existing safety floor, not a full proportional rebase: a TP
                            # that already clears the floor is left untouched, in keeping with TP
                            # being "intentionally left untouched" for anything that isn't a floor
                            # violation.
                            try:
                                if (isinstance(dependent_order.data, dict)
                                        and "tp_percent_target" in dependent_order.data
                                        and parent_order.open_price and dependent_order.limit_price):
                                    from ba2_common.core.TradeActions import (
                                        compute_tp_floor_price, resolve_min_take_profit_pct)
                                    min_pct = resolve_min_take_profit_pct(parent_order.expert_recommendation_id)
                                    side_str = str(parent_order.side.value if hasattr(parent_order.side, "value")
                                                   else parent_order.side).upper()
                                    is_long = side_str == "BUY"
                                    floor_price = compute_tp_floor_price(
                                        dependent_order.limit_price, parent_order.open_price, min_pct, is_long)
                                    if floor_price is not None:
                                        old_tp = dependent_order.limit_price
                                        dependent_order.limit_price = floor_price
                                        self.logger.warning(
                                            f"TP floor re-check against real fill: order {dependent_order.id} "
                                            f"was ${old_tp:.4f} (pre-fill), below the {min_pct}% minimum from "
                                            f"the real fill ${parent_order.open_price:.4f}. Adjusted to "
                                            f"${floor_price:.4f}"
                                        )
                                        txn = session.get(Transaction, dependent_order.transaction_id) \
                                            if dependent_order.transaction_id else None
                                        if txn:
                                            txn.take_profit = floor_price
                                            session.add(txn)
                                            transaction_field_updates = True
                                        dependent_order.data["tp_floor_rechecked_at_fill"] = True
                            except (KeyError, TypeError, ValueError) as tp_floor_err:
                                self.logger.warning(
                                    f"Could not re-check TP floor for order {dependent_order.id}: {tp_floor_err}")

                            # ===== Legacy: percent-based recalc (kept as fallback) =====
                            # This ensures TP/SL prices use the parent's filled price, not stale market data
                            # If percent is not stored, it will be calculated and stored as a fallback
                            if dependent_order.data and isinstance(dependent_order.data, dict):
                                try:
                                    # Check if this is a TP order (has tp_percent in data)
                                    if dependent_order.data and "TP_SL" in dependent_order.data and "tp_percent" in dependent_order.data["TP_SL"] and parent_order.open_price:
                                        tp_percent = dependent_order.data["TP_SL"].get("tp_percent")
                                        old_limit_price = dependent_order.limit_price
                                        
                                        # Recalculate TP price from parent's filled price: price = filled_price * (1 + percent/100)
                                        new_limit_price = parent_order.open_price * (1 + tp_percent / 100)
                                        
                                        # Round price to 4 decimal places (standard for forex/stocks)
                                        new_limit_price = round(new_limit_price, 4)
                                        
                                        # Update the limit price
                                        dependent_order.limit_price = new_limit_price
                                        
                                        self.logger.info(
                                            f"Recalculated TP price for order {dependent_order.id}: "
                                            f"parent filled ${parent_order.open_price:.2f} * (1 + {tp_percent:.2f}%) "
                                            f"= ${new_limit_price:.2f} (was ${old_limit_price:.2f})"
                                        )
                                        
                                        # Update data field to record when recalculation happened
                                        dependent_order.data["parent_filled_price"] = parent_order.open_price
                                        dependent_order.data["recalculated_at_trigger"] = True
                                        
                                        # Mark transaction for update with new TP price
                                        transaction_updated = True
                                    
                                    # Check if this is an SL order (has sl_percent in data)
                                    elif dependent_order.data and "TP_SL" in dependent_order.data and "sl_percent" in dependent_order.data["TP_SL"] and parent_order.open_price:
                                        sl_percent = dependent_order.data["TP_SL"].get("sl_percent")
                                        old_stop_price = dependent_order.stop_price
                                        
                                        # Recalculate SL price from parent's filled price: price = filled_price * (1 + percent/100)
                                        # For SL, percent is typically negative, so 1 + (-5/100) = 0.95 for a 5% loss
                                        new_stop_price = parent_order.open_price * (1 + sl_percent / 100)
                                        
                                        # Round price to 4 decimal places
                                        new_stop_price = round(new_stop_price, 4)
                                        
                                        # Update the stop price
                                        dependent_order.stop_price = new_stop_price
                                        
                                        self.logger.info(
                                            f"Recalculated SL price for order {dependent_order.id}: "
                                            f"parent filled ${parent_order.open_price:.2f} * (1 + {sl_percent:.2f}%) "
                                            f"= ${new_stop_price:.2f} (was ${old_stop_price:.2f})"
                                        )
                                        
                                        # Update data field to record when recalculation happened
                                        if "TP_SL" not in dependent_order.data:
                                            dependent_order.data["TP_SL"] = {}
                                        dependent_order.data["TP_SL"]["parent_filled_price"] = parent_order.open_price
                                        dependent_order.data["TP_SL"]["recalculated_at_trigger"] = True
                                        
                                        # Mark transaction for update with new SL price
                                        transaction_updated = True
                                    
                                    else:
                                        # No tp_percent or sl_percent in data - try to calculate as fallback
                                        # This handles cases where TP/SL orders were created before percent storage was implemented
                                        self.logger.debug(f"No TP/SL percent found in order {dependent_order.id}.data, attempting fallback calculation")
                                        # Note: We can't call AccountInterface method here, but the calculation will happen
                                        # when the account's submit_order is called in PHASE 2 below
                                
                                except (KeyError, TypeError, ValueError) as data_error:
                                    self.logger.warning(
                                        f"Could not recalculate TP/SL price for order {dependent_order.id} from data field: {data_error}"
                                    )
                            else:
                                # No data field yet - will be populated when account submits the order
                                self.logger.debug(f"No data field in order {dependent_order.id}, will ensure percent is calculated during submission")
                            # ===== END: Price recalculation =====
                            
                            # Update the associated Transaction if TP/SL price was recalculated
                            if transaction_updated and dependent_order.transaction_id:
                                transaction = session.get(Transaction, dependent_order.transaction_id)
                                if transaction:
                                    # Update TP or SL price depending on order type
                                    if dependent_order.data and "TP_SL" in dependent_order.data and "tp_percent" in dependent_order.data["TP_SL"]:
                                        old_tp = transaction.take_profit
                                        transaction.take_profit = dependent_order.limit_price
                                        self.logger.info(f"Updated Transaction {dependent_order.transaction_id} take_profit to ${dependent_order.limit_price:.2f}")
                                        # Log activity for TP recalculation
                                        try:
                                            from .db import log_activity
                                            from .types import ActivityLogSeverity, ActivityLogType
                                            old_tp_str = f"${old_tp:.2f}" if old_tp else "none"
                                            log_activity(
                                                severity=ActivityLogSeverity.INFO,
                                                activity_type=ActivityLogType.TP_SL_ADJUSTED,
                                                description=f"Recalculated TP {old_tp_str} → ${dependent_order.limit_price:.2f} for {transaction.symbol} (source: price_recalculation)",
                                                data={
                                                    "transaction_id": transaction.id,
                                                    "symbol": transaction.symbol,
                                                    "old_tp": old_tp,
                                                    "new_tp": dependent_order.limit_price,
                                                    "source": "price_recalculation",
                                                    "parent_filled_price": parent_order.open_price
                                                },
                                                source_expert_id=transaction.expert_id
                                            )
                                        except Exception as log_error:
                                            self.logger.warning(f"Failed to log TP recalculation activity: {log_error}")
                                    elif dependent_order.data and "TP_SL" in dependent_order.data and "sl_percent" in dependent_order.data["TP_SL"]:
                                        old_sl = transaction.stop_loss
                                        transaction.stop_loss = dependent_order.stop_price
                                        self.logger.info(f"Updated Transaction {dependent_order.transaction_id} stop_loss to ${dependent_order.stop_price:.2f}")
                                        # Log activity for SL recalculation
                                        try:
                                            from .db import log_activity
                                            from .types import ActivityLogSeverity, ActivityLogType
                                            old_sl_str = f"${old_sl:.2f}" if old_sl else "none"
                                            log_activity(
                                                severity=ActivityLogSeverity.INFO,
                                                activity_type=ActivityLogType.TP_SL_ADJUSTED,
                                                description=f"Recalculated SL {old_sl_str} → ${dependent_order.stop_price:.2f} for {transaction.symbol} (source: price_recalculation)",
                                                data={
                                                    "transaction_id": transaction.id,
                                                    "symbol": transaction.symbol,
                                                    "old_sl": old_sl,
                                                    "new_sl": dependent_order.stop_price,
                                                    "source": "price_recalculation",
                                                    "parent_filled_price": parent_order.open_price
                                                },
                                                source_expert_id=transaction.expert_id
                                            )
                                        except Exception as log_error:
                                            self.logger.warning(f"Failed to log SL recalculation activity: {log_error}")
                                    session.add(transaction)
                            
                            # Double-check quantity one more time before adding to submit list
                            if dependent_order.quantity <= 0:
                                self.logger.error(
                                    f"Dependent order {dependent_order.id} (symbol: {dependent_order.symbol}) "
                                    f"still has invalid quantity {dependent_order.quantity}. "
                                    f"Parent order {parent_order_id} (symbol: {parent_order.symbol}) quantity: {parent_order.quantity}. "
                                    f"Setting to ERROR status."
                                )
                                status_updates[dependent_order.id] = OrderStatus.ERROR
                                continue
                            
                            # Add to submit list
                            orders_to_submit.append((dependent_order, parent_order_id))
                        elif action == "cancel":
                            # Parent reached a different terminal status - dependent can't fire
                            self.logger.warning(
                                f"Parent order {parent_order_id} is in terminal status {current_status} "
                                f"(but dependent order {dependent_order.id} was waiting for {trigger_status}). "
                                f"Syncing dependent order to CANCELED"
                            )
                            status_updates[dependent_order.id] = OrderStatus.CANCELED
                        else:
                            # action == "wait": parent still in flight (incl. PARTIALLY_FILLED) — keep waiting
                            self.logger.debug(
                                f"Dependent order {dependent_order.id} waiting: parent {parent_order_id} "
                                f"status {current_status} has not reached trigger {trigger_status}"
                            )
                    
                    except Exception as order_error:
                        self.logger.error(f"Error processing waiting order {dependent_order.id}: {order_error}", exc_info=True)
                        status_updates[dependent_order.id] = OrderStatus.ERROR
                
                # PHASE 1 COMPLETE: Session is still open, now apply any status-only updates
                for order_id, new_status in status_updates.items():
                    order_obj = session.get(TradingOrder, order_id)
                    if order_obj:
                        order_obj.status = new_status
                        session.add(order_obj)
                
                if status_updates or transaction_field_updates:
                    session.commit()
                    if status_updates:
                        self.logger.debug(f"Applied {len(status_updates)} status-only updates")
                    if transaction_field_updates:
                        self.logger.debug("Committed SL-rebase/TP-floor Transaction field updates")
                # Session will close here
            
            # PHASE 2: Process all order submissions OUTSIDE of session context
            submitted_count = 0
            for dependent_order, parent_order_id in orders_to_submit:
                try:
                    account_def = get_instance(AccountDefinition, dependent_order.account_id)
                    if not account_def:
                        self.logger.error(f"Account definition {dependent_order.account_id} not found for dependent order {dependent_order.id}")
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        continue
                    
                    account_class = get_account_class(account_def.provider)
                    if not account_class:
                        self.logger.error(f"Account provider {account_def.provider} not found for dependent order {dependent_order.id}")
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        continue
                    
                    account = account_class(account_def.id)

                    if not getattr(account, 'supports_trading', True):
                        self.logger.error(f"Account {account_def.id} ({account_def.provider}) is read-only, cannot submit orders")
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        continue

                    # Gate cancel-and-replace orders (e.g. a trailing-stop OCO swap): only
                    # submit once the broker has ACTUALLY released the prior order's qty.
                    # Our DB can mark the prior order CANCELED before the broker frees the
                    # held qty; submitting too early is rejected (40310000 insufficient qty)
                    # and hard-ERRORs, silently dropping the position's protection. If the
                    # qty isn't free yet, leave this order WAITING_TRIGGER to retry next refresh.
                    #
                    # get_available_position_quantity is a CONCRETE seam on
                    # ReadOnlyAccountInterface (I9). It used to exist only on
                    # AlpacaAccount, so on every other broker this raised AttributeError,
                    # the except set None, and replacement_blocked_by_qty(None) returned
                    # False — the guard was a PERMANENT NO-OP everywhere but Alpaca.
                    # The except now falls back to 0.0 (defer and retry), not None
                    # (submit blind): a broker whose answer we could not obtain is the
                    # case that must NOT be read as "the qty is free".
                    try:
                        available_qty = account.get_available_position_quantity(dependent_order.symbol)
                    except Exception as e:
                        self.logger.warning(
                            f"get_available_position_quantity({dependent_order.symbol}) raised "
                            f"on account {dependent_order.account_id}: {e} — treating as 0 "
                            f"available (defer, retry next refresh)"
                        )
                        available_qty = 0.0
                    if replacement_blocked_by_qty(
                        dependent_order.depends_order_status_trigger, available_qty,
                        dependent_order.quantity,
                    ):
                        self.logger.info(
                            f"Deferring replacement order {dependent_order.id} "
                            f"({dependent_order.symbol}): broker available qty {available_qty} < "
                            f"required {dependent_order.quantity} — prior order not yet released "
                            f"at broker; staying WAITING_TRIGGER to retry next refresh."
                        )
                        continue

                    # A deferred CLOSE order (submit_close_order_for_transaction with a
                    # dependency) reaches the broker through this generic dependent-order
                    # path, so it must carry the same is_closing_order=True its immediate
                    # sibling call gets — otherwise the position-size validation wrongly
                    # blocks a close just because the position grew past the entry cap
                    # (the position is shrinking, not growing). Same MARKET+"closing"
                    # comment heuristic close_transaction() already uses elsewhere.
                    is_closing = (
                        dependent_order.order_type == OrderType.MARKET and
                        dependent_order.comment and
                        'closing' in dependent_order.comment.lower()
                    )
                    self.logger.info(
                        f"Submitting dependent order {dependent_order.id}: {dependent_order.side.value} "
                        f"{dependent_order.quantity} {dependent_order.symbol} @ {dependent_order.order_type.value} "
                        f"(triggered by parent order {parent_order_id})"
                    )
                    submitted_order = account.submit_order(dependent_order, is_closing_order=is_closing)

                    if submitted_order:
                        self.logger.info(f"Successfully submitted dependent order {dependent_order.id}")
                        submitted_count += 1
                    else:
                        # submit_order's own broker-agnostic error handling
                        # (AccountInterface._handle_order_submit_error) has already marked the
                        # FRESH db row ERROR with the classified reason in its comment — re-fetch
                        # rather than writing the stale pre-submit `dependent_order` object back,
                        # which would clobber that comment with the old pre-failure value.
                        fresh = get_instance(TradingOrder, dependent_order.id)
                        if fresh and fresh.status == OrderStatus.ERROR:
                            self.logger.error(
                                f"Failed to submit dependent order {dependent_order.id} "
                                f"(symbol: {dependent_order.symbol}) — already marked ERROR "
                                f"with broker reason: {fresh.comment}"
                            )
                        else:
                            self.logger.error(
                                f"Failed to submit dependent order {dependent_order.id} (symbol: {dependent_order.symbol}) - "
                                f"setting to ERROR status"
                            )
                            (fresh or dependent_order).status = OrderStatus.ERROR
                            update_instance(fresh or dependent_order)

                except Exception as submit_error:
                    self.logger.error(
                        f"Exception submitting dependent order {dependent_order.id} (symbol: {dependent_order.symbol}, "
                        f"qty: {dependent_order.quantity}): {submit_error}",
                        exc_info=True
                    )
                    try:
                        # Re-fetch: this exception is typically raised BEFORE broker submission
                        # (e.g. order validation), so nothing has written a comment yet here —
                        # but re-fetch anyway for a consistent, non-stale write.
                        fresh = get_instance(TradingOrder, dependent_order.id) or dependent_order
                        fresh.status = OrderStatus.ERROR
                        error_msg = f"[validation_error] {str(submit_error)[:180]}"
                        fresh.comment = (f"{fresh.comment} | {error_msg}" if fresh.comment else error_msg)[:500]
                        update_instance(fresh)
                    except Exception as update_error:
                        self.logger.error(f"Could not update order {dependent_order.id} to ERROR status: {update_error}")
            
            if submitted_count > 0:
                self.logger.info(f"Processed {submitted_count} waiting trigger orders")
                
        except Exception as e:
            self.logger.error(f"Error checking all waiting trigger orders: {e}", exc_info=True)
    
    def process_recommendation(self, recommendation: ExpertRecommendation) -> Optional[TradingOrder]:
        """
        Process a single expert recommendation and potentially place an order.
        
        Args:
            recommendation: The expert recommendation to process
            
        Returns:
            TradingOrder if an order was placed, None otherwise
        """
        try:
            # Get the expert instance
            expert_instance = get_instance(ExpertInstance, recommendation.instance_id)
            if not expert_instance:
                self.logger.error(f"Expert instance {recommendation.instance_id} not found")
                return None
                
            # Check if expert is enabled
            if not expert_instance.enabled:
                self.logger.debug(f"Expert instance {expert_instance.id} is disabled, skipping recommendation")
                return None
                
            # Get expert trading permissions
            trading_permissions = self._get_expert_trading_permissions(expert_instance)
            
            # Check if the recommended action is allowed
            if not self._is_action_allowed(recommendation.recommended_action, trading_permissions):
                self.logger.info(f"Action {recommendation.recommended_action} not allowed for expert {expert_instance.id}")
                return None
                
            # Check if automated trade opening is enabled (modern setting)
            if not trading_permissions.get('allow_automated_trade_opening', False):
                self.logger.info(f"Automated trade opening disabled (allow_automated_trade_opening=False) for expert {expert_instance.id}, recommendation logged only")
                return None
                
            # Note: Ruleset evaluation is handled by TradeActionEvaluator in process_expert_recommendations_after_analysis()
            # This method (process_recommendation) is a legacy path and should eventually be deprecated
            self.logger.warning(f"Using legacy process_recommendation() for recommendation {recommendation.id} - "
                              f"this path is deprecated, use process_expert_recommendations_after_analysis() instead")
                
            # Create the order. Do not place it yet.
            order = self._create_order_from_recommendation(recommendation, expert_instance)
            
            if order:
                self.logger.info(f"Created order from recommendation {recommendation.id} via legacy path")
                return order
            else:
                self.logger.debug(f"No order created from recommendation {recommendation.id}")
                return None
                    
        except Exception as e:
            self.logger.error(f"Error processing recommendation {recommendation.id}: {e}", exc_info=True)
            return None
        
    def _get_expert_trading_permissions(self, expert_instance: ExpertInstance) -> Dict[str, Any]:
        """
        Get trading permissions for an expert instance.
        
        Args:
            expert_instance: The expert instance
            
        Returns:
            Dictionary of trading permissions
        """
        try:
            # Load expert instance with appropriate class
            from .utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(expert_instance.id)
            if not expert:
                self.logger.error(f"Expert instance {expert_instance.id} not found or invalid expert type {expert_instance.expert}")
                return {}
            
            # Check for legacy automatic_trading setting and new settings
            legacy_automatic_trading = expert.settings.get('automatic_trading', False)  # Legacy setting, keep hardcoded default
            
            # Use interface defaults for modern settings
            allow_automated_trade_opening = expert.get_setting_with_interface_default(
                'allow_automated_trade_opening', log_warning=False
            )
            allow_automated_trade_modification = expert.get_setting_with_interface_default(
                'allow_automated_trade_modification', log_warning=False
            )
            
            return {
                'enable_buy': expert.get_setting_with_interface_default(
                    'enable_buy', log_warning=False
                ),
                'enable_sell': expert.get_setting_with_interface_default(
                    'enable_sell', log_warning=False
                ),
                'allow_automated_trade_opening': allow_automated_trade_opening,
                'allow_automated_trade_modification': allow_automated_trade_modification,
                # Keep legacy setting for backward compatibility
                'automatic_trading': legacy_automatic_trading
            }
            
        except Exception as e:
            self.logger.error(f"Error getting trading permissions for expert {expert_instance.id}: {e}", exc_info=True)
            return {}
            
    def _is_action_allowed(self, action: OrderRecommendation, permissions: Dict[str, Any]) -> bool:
        """
        Check if a trading action is allowed based on expert permissions.
        
        Args:
            action: The recommended action
            permissions: Expert trading permissions
            
        Returns:
            True if action is allowed, False otherwise
        """
        if action == OrderRecommendation.BUY:
            return permissions.get('enable_buy', False)
        elif action == OrderRecommendation.OVERWEIGHT:
            return permissions.get('enable_buy', False)
        elif action == OrderRecommendation.SELL:
            return permissions.get('enable_sell', False)
        elif action == OrderRecommendation.UNDERWEIGHT:
            return permissions.get('enable_sell', False)
        elif action == OrderRecommendation.HOLD:
            return True  # HOLD is always allowed as it means no action
        else:
            return False  # ERROR and unknown actions are not allowed
            
    def _create_order_from_recommendation(self, recommendation: ExpertRecommendation, expert_instance: ExpertInstance) -> Optional[TradingOrder]:
        """
        Create a trading order from an expert recommendation.
        
        Args:
            recommendation: The expert recommendation
            expert_instance: The expert instance
            
        Returns:
            TradingOrder if created successfully, None otherwise
        """
        try:
            if recommendation.recommended_action == OrderRecommendation.HOLD:
                return None  # No order needed for HOLD

            # Map recommendation action to order direction
            if recommendation.recommended_action in (OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT):
                side = "buy"  # Both BUY and OVERWEIGHT open long positions
            elif recommendation.recommended_action in (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT):
                side = "sell"  # Both SELL and UNDERWEIGHT open short/close positions
            else:
                return None

            # NOTE: This is a LEGACY path that doesn't use TradeRiskManagement for quantity calculation
            # Quantity will be set to 0 here and should be calculated later by risk management
            # The proper path is process_expert_recommendations_after_analysis() which handles this correctly
            quantity = 0
            
            self.logger.warning(f"Creating order with quantity=0 for {recommendation.symbol} - "
                              f"quantity should be calculated by TradeRiskManagement.review_and_prioritize_pending_orders()")
            
            # Create the order
            # Convert side to uppercase to match OrderDirection enum
            side_upper = side.upper() if isinstance(side, str) else side
            order = TradingOrder(
                symbol=recommendation.symbol,
                quantity=abs(quantity),  # Ensure positive quantity
                side=side_upper,
                order_type="market",  # Default to market order
                status=OrderStatus.PENDING,
                limit_price=None,  # Market order
                stop_price=None,
                comment=f"expert_{expert_instance.expert}-{expert_instance.id}_{recommendation.id}",
                # Link to the recommendation and mark as automatic
                expert_recommendation_id=recommendation.id,
                open_type=OrderOpenType.AUTOMATIC
            )
            
            return order
            
        except Exception as e:
            self.logger.error(f"Error creating order from recommendation {recommendation.id}: {e}", exc_info=True)
            return None
            
    def _place_order(self, order: TradingOrder, expert_instance: ExpertInstance) -> Optional[TradingOrder]:
        """
        Execute the order through the appropriate account interface.
        
        Args:
            order: The trading order to place
            expert_instance: The expert instance
            
        Returns:
            TradingOrder with updated status if successful, None otherwise
        """
        try:
            # Get the account for this expert
            from ..modules.accounts import get_account_class
            from .models import AccountDefinition
            
            account_def = get_instance(AccountDefinition, expert_instance.account_id)
            if not account_def:
                self.logger.error(f"Account definition {expert_instance.account_id} not found")
                return None

            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found")
                return None
                
            account = account_class(account_def.id)

            if not getattr(account, 'supports_trading', True):
                self.logger.error(f"Account {account_def.id} ({account_def.provider}) is read-only, cannot submit orders")
                return None

            # Submit the order through the account interface
            submitted_order = account.submit_order(order)
            if submitted_order:
                # Save the order to database
                # Note: add_instance will set the ID on the instance before committing
                db_id = add_instance(submitted_order)
                if db_id:
                    # The submitted_order instance is now detached, but should have the ID set
                    # Create a simple return value to avoid detached instance issues
                    self.logger.info(f"Order {db_id} successfully placed for {order.symbol}")

                    # Return a fresh instance from the database to avoid detached instance errors
                    # NOTE: do not import get_instance here - an inner import previously created a
                    # local variable shadowing the module-level `get_instance`, which caused an
                    # UnboundLocalError at runtime when exception handling referenced it. Using
                    # the already-imported `get_instance` from the module scope avoids that bug.
                    return get_instance(TradingOrder, db_id)
                    
        except Exception as e:
            self.logger.error(f"Error placing order: {e}", exc_info=True)
            
        return None
        
    def process_expert_recommendations_after_analysis(self, expert_instance_id: int, lookback_days: int = 1) -> List[TradingOrder]:
        """
        Process expert recommendations after all market analysis jobs for enter_market are completed.
        
        This function is called when there are no more pending analysis jobs for a given expert.
        It evaluates recommendations through the enter_market ruleset using TradeActionEvaluator
        and executes the resulting actions (if automated trading is enabled).
        
        This method uses thread-safe locking to ensure only one thread processes recommendations
        for a given expert/use_case at a time. If the lock cannot be acquired within 0.5 seconds,
        the method returns immediately (another thread is already processing).
        
        Args:
            expert_instance_id: The expert instance ID to process recommendations for
            lookback_days: Number of days to look back for recommendations (default: 1)
            
        Returns:
            List of TradingOrder objects that were created (in PENDING state)
        """
        # Use enter_market as the use case for this method (it only handles enter_market)
        lock_key = f"expert_{expert_instance_id}_usecase_enter_market"
        
        # Get or create a lock for this expert/use_case combination
        with self._locks_dict_lock:
            if lock_key not in self._processing_locks:
                self._processing_locks[lock_key] = threading.Lock()
            processing_lock = self._processing_locks[lock_key]
        
        # Try to acquire the lock with a very short timeout (0.5 seconds)
        # If we can't get it, another thread is already processing this expert
        lock_acquired = processing_lock.acquire(blocking=True, timeout=0.5)
        
        if not lock_acquired:
            self.logger.info(f"Could not acquire lock for expert {expert_instance_id} (enter_market) - another thread is already processing. Skipping.")
            return []
        
        # We have the lock - make sure we release it when done
        created_orders = []
        # TEMP-ORDER-LIST FLOW: instead of persisting a qty=0 PENDING order per passing rec and then
        # letting the RM size + DELETE the unfunded (churn), we stage a TRANSIENT candidate per passing
        # rec, size them ALL in one in-memory RM pass, then persist + submit ONLY the funded ones.
        # Each entry: (transient_candidate_order, evaluator, recommendation).
        entry_candidates = []

        try:
            self.logger.debug(f"Acquired processing lock for expert {expert_instance_id} (enter_market)")
            
            from sqlmodel import select
            from .db import get_db
            from .models import Transaction, AccountDefinition
            from .types import AnalysisUseCase, TransactionStatus
            from .utils import get_expert_instance_from_id
            from datetime import timedelta
            from .TradeActionEvaluator import TradeActionEvaluator
            from ..modules.accounts import get_account_class

            # Get the expert instance (with loaded settings)
            expert = get_expert_instance_from_id(expert_instance_id)
            if not expert:
                self.logger.error(f"Expert instance {expert_instance_id} not found")
                return created_orders

            # Get the expert instance model (for ruleset IDs)
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                self.logger.error(f"Expert instance model {expert_instance_id} not found")
                return created_orders

            # Check if "Allow automated trade opening" is enabled
            allow_automated_trade_opening = expert.get_setting_with_interface_default(
                'allow_automated_trade_opening', log_warning=False
            )
            if not allow_automated_trade_opening:
                self.logger.debug(f"Automated trade opening disabled for expert {expert_instance_id}, skipping recommendation processing")
                return created_orders

            # Check if there's an enter_market ruleset configured
            if not expert_instance.enter_market_ruleset_id:
                self.logger.debug(f"No enter_market ruleset configured for expert {expert_instance_id}, skipping automated order creation")
                return created_orders

            # Check if there are still pending analysis jobs for this expert
            if self._has_pending_analysis_jobs(expert_instance_id):
                self.logger.debug(f"Still has pending analysis jobs for expert {expert_instance_id}, skipping automated order creation")
                return created_orders

            # Get the account instance for this expert
            account_def = get_instance(AccountDefinition, expert_instance.account_id)
            if not account_def:
                self.logger.error(f"Account definition {expert_instance.account_id} not found")
                return created_orders

            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found")
                return created_orders

            account = account_class(account_def.id)

            # Get recent recommendations based on lookback_days parameter
            cutoff_time = datetime.now(timezone.utc) - timedelta(days=lookback_days)

            # INVARIANT FOR THIS WHOLE BLOCK: `session` is READ-ONLY. Nothing loaded through it
            # may be mutated, and nothing may be added to it.
            #
            # It stays open across the broker round trip, and sqlite has ONE write lock per
            # database. The moment this session owns a modified row, the next query on it
            # autoflushes, takes that lock, and holds it for the rest of the block — while
            # submit_order writes the Transaction and the order from a SECOND connection on the
            # same thread. That connection then waits for a lock its own caller holds and nobody
            # is left to release it: measured on PROD as ~15 minutes with no write of any kind,
            # ending 15 ms after this block exited. CVS 2026-08-10 and WSC 2026-08-24 were both
            # funded by the risk manager and both lost that way.
            #
            # The 2026-08-10 attempt wrapped ONE query in `no_autoflush`. It did not hold: the
            # rows it hid stayed dirty, and the NEXT candidate's first query flushed them instead.
            # Guarding flush SITES is unwinnable — every future query is another one. Owning no
            # dirty state is the invariant that cannot be routed around. Writes from inside this
            # block go through update_instance()/add_instance() on their own short-lived sessions.
            with get_db() as session:
                # Get all recommendations for this expert instance within the time window
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.instance_id == expert_instance_id,
                    ExpertRecommendation.created_at >= cutoff_time,
                    ExpertRecommendation.recommended_action != OrderRecommendation.HOLD
                ).order_by(ExpertRecommendation.created_at.desc())  # Most recent first

                all_recommendations = session.exec(statement).all()

                if not all_recommendations:
                    self.logger.info(f"No actionable recommendations found for expert {expert_instance_id}")
                    return created_orders
                
                # Filter to get only the latest recommendation per instrument
                # This prevents processing multiple recommendations for the same symbol
                latest_per_instrument = {}
                for rec in all_recommendations:
                    # Keep only the first (most recent) recommendation for each symbol
                    if rec.symbol not in latest_per_instrument:
                        latest_per_instrument[rec.symbol] = rec
                
                # Convert to list and sort by profit potential
                recommendations = sorted(
                    latest_per_instrument.values(),
                    key=lambda r: r.expected_profit_percent,
                    reverse=True
                )
                
                self.logger.info(f"Found {len(recommendations)} unique instruments with recommendations for expert {expert_instance_id} (filtered from {len(all_recommendations)} total recommendations)")
                self.logger.info(f"Evaluating recommendations through enter_market ruleset: {expert_instance.enter_market_ruleset_id}")
                
                # Process each recommendation through the enter_market ruleset
                for recommendation in recommendations:
                    try:
                        # Create TradeActionEvaluator with instrument_name for this recommendation
                        # No existing_transactions for entering_markets use case
                        evaluator = TradeActionEvaluator(
                            account=account,
                            instrument_name=recommendation.symbol,
                            existing_transactions=None
                        )
                        
                        # Evaluate recommendation through the enter_market ruleset
                        self.logger.debug(f"Evaluating recommendation {recommendation.id} for {recommendation.symbol}")
                        
                        action_summaries = evaluator.evaluate(
                            instrument_name=recommendation.symbol,
                            expert_recommendation=recommendation,
                            ruleset_id=expert_instance.enter_market_ruleset_id,
                            existing_order=None  # No existing order when entering market
                        )
                        
                        # Check if evaluation produced any actions
                        if not action_summaries:
                            self.logger.debug(f"Recommendation {recommendation.id} for {recommendation.symbol} - no actions to execute (conditions not met)")
                            
                            # Store evaluation details even when no actions are created
                            # This is crucial for analysis, debugging, and understanding why rules didn't trigger
                            evaluation_details = evaluator.get_evaluation_details()
                            if evaluation_details:
                                from ..core.models import TradeActionResult
                                from ..core.db import add_instance
                                
                                evaluation_result = TradeActionResult(
                                    action_type='evaluation_only',
                                    success=True,  # Evaluation succeeded, just no actions needed
                                    message=f'Rule evaluation completed for {recommendation.symbol} - no actions triggered (conditions not met)',
                                    data={'evaluation_details': evaluation_details},
                                    expert_recommendation_id=recommendation.id
                                )
                                add_instance(evaluation_result)
                                self.logger.debug(f"Stored evaluation details for recommendation {recommendation.id} (no actions)")
                            
                            continue
                        
                        # Check for evaluation errors
                        if any('error' in summary for summary in action_summaries):
                            errors = [s.get('error') for s in action_summaries if 'error' in s]
                            self.logger.warning(f"Recommendation {recommendation.id} evaluation had errors: {errors}")
                            
                            # Store evaluation details even when errors occurred
                            evaluation_details = evaluator.get_evaluation_details()
                            if evaluation_details:
                                from ..core.models import TradeActionResult
                                from ..core.db import add_instance
                                
                                evaluation_result = TradeActionResult(
                                    action_type='evaluation_error',
                                    success=False,
                                    message=f'Rule evaluation encountered errors for {recommendation.symbol}: {"; ".join(errors)}',
                                    data={'evaluation_details': evaluation_details, 'errors': errors},
                                    expert_recommendation_id=recommendation.id
                                )
                                add_instance(evaluation_result)
                                self.logger.debug(f"Stored evaluation details for recommendation {recommendation.id} (errors)")
                            
                            continue
                        
                        self.logger.info(f"Recommendation {recommendation.id} for {recommendation.symbol} passed ruleset - {len(action_summaries)} action(s) to execute")
                        
                        # SAFETY CHECK: For enter_market, check if there's already an open/waiting transaction
                        # for this symbol and expert to prevent duplicate positions
                        existing_txn_statement = select(Transaction).where(
                            Transaction.expert_id == expert_instance_id,
                            Transaction.symbol == recommendation.symbol,
                            Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING])
                        )
                        existing_txn = session.exec(existing_txn_statement).first()
                        
                        if existing_txn:
                            self.logger.warning(
                                f"SAFETY CHECK: Skipping recommendation {recommendation.id} for {recommendation.symbol} - "
                                f"existing transaction {existing_txn.id} in {existing_txn.status} status for expert {expert_instance_id}"
                            )
                            continue
                        
                        # EQUITY CHECK: Verify expert has sufficient available equity before creating orders
                        # Uses minimum_equity_threshold_percent from account settings (default 5%)
                        # Skip execution if insufficient funds to avoid creating pending orders that can't be filled
                        has_sufficient_equity, equity_reason = expert.has_sufficient_equity_for_trading()
                        if not has_sufficient_equity:
                            self.logger.warning(
                                f"EQUITY CHECK: Skipping recommendation {recommendation.id} for {recommendation.symbol} - "
                                f"expert {expert_instance_id}: {equity_reason}"
                            )
                            continue
                        
                        # TEMP-ORDER-LIST FLOW: do NOT execute (persist) yet. Stage a TRANSIENT
                        # candidate order (via the shared trade_cycle builder — same shape the
                        # backtest uses) for the in-memory RM sizing pass below; only the funded
                        # subset is executed + submitted (no qty=0 churn, no unfunded deletes).
                        from .trade_cycle import build_entry_candidate
                        candidate = build_entry_candidate(recommendation, account.id)
                        entry_candidates.append((candidate, evaluator, recommendation))

                    except Exception as e:
                        self.logger.error(f"Error processing recommendation {recommendation.id}: {e}", exc_info=True)
                        continue

                # TEMP-ORDER-LIST RM SIZING: size ALL the bar's candidates in one in-memory pass; only
                # the funded (qty>0) get persisted (execute) + submitted. Unfunded candidates never
                # touch the DB — the requested order-flow redesign (was: persist qty=0 -> RM DB pass ->
                # delete unfunded). The funded set + quantities are identical to the old DB path
                # (size_candidate_orders == review_and_prioritize_pending_orders; proven by tests).
                submitted_count = 0
                if entry_candidates and allow_automated_trade_opening:
                    self.logger.info(f"Sizing {len(entry_candidates)} entry candidate(s) for expert {expert_instance_id}")
                    try:
                        from .TradeRiskManagement import get_risk_management
                        risk_management = get_risk_management()
                        funded = risk_management.size_candidate_orders(
                            expert_instance_id, [(c, rec) for (c, _e, rec) in entry_candidates])
                        funded_by_symbol = {o.symbol: o for o in funded}
                        self.logger.info(f"RM funded {len(funded)} of {len(entry_candidates)} candidate(s)")

                        for candidate, evaluator, recommendation in entry_candidates:
                            fo = funded_by_symbol.get(candidate.symbol)
                            if fo is None or not (fo.quantity and fo.quantity > 0):
                                continue  # unfunded — never persisted (no churn / delete)
                            # BEFORE the try, and per iteration: the except handler below
                            # compensates `order`, and a name left bound by the PREVIOUS
                            # iteration would make it compensate the wrong symbol's entry.
                            order = None
                            try:
                                # Persist the real order + transaction + TP/SL bracket (execute is
                                # byte-identical to the old flow; now called ONLY for funded symbols).
                                execution_results = evaluator.execute()
                                for result in execution_results:
                                    if result.get('success') and isinstance(result.get('data'), dict):
                                        oid = result['data'].get('order_id')
                                        if oid:
                                            # DETACHED on purpose — see the block comment above
                                            # this loop's `with get_db() as session:`. Fetching
                                            # through the long-lived `session` attaches the row,
                                            # the RM stamp two lines below makes that session
                                            # DIRTY, and the next query on it autoflushes and
                                            # takes sqlite's one write lock — which submit_order
                                            # then blocks on from its own second connection.
                                            # get_instance(session=None) opens and closes its own
                                            # session and hands back a fully loaded, detached row.
                                            o = self._row_or_none(TradingOrder, oid)
                                            if o and o.side in (OrderDirection.BUY, OrderDirection.SELL) and order is None:
                                                order = o
                                if order is None:
                                    self.logger.warning(f"No main order created for funded {candidate.symbol}")
                                    continue
                                # RM-sized quantity + safeguard stop from the candidate pass,
                                # WRITTEN TO DISK before the broker is asked for anything.
                                self._persist_funded_entry(order, fo.quantity, fo.stop_price or None)

                                # ...and stamp the SAME quantity onto the protective legs execute()
                                # just created. They were built by the account from the entry's
                                # quantity while it was still the TRANSIENT 0 that
                                # build_entry_candidate() sets, because the bracket is created inside
                                # execute() and the RM size is stamped only here, one line above.
                                #
                                # A leg is therefore born correct only when the stamp happens to land
                                # first, which is a race, not a rule. Measured 2026-08-08: 23 of 96
                                # entry-staged legs across dev+prod carried quantity 0 (every one of
                                # them `trigger=FILLED`, i.e. staged against an unfilled entry), and
                                # the three live ones — WKC / GNTX / CSTL — were refused by the
                                # quantity<=0 guard and cancelled, leaving those positions with a
                                # stop and NO take-profit.
                                #
                                # Doing it here closes the race at the source: same value, same
                                # moment. Scoped to legs of THIS entry, created seconds ago by
                                # THIS execute(), so they are all full-position legs — the
                                # partial-close case (a leg sized for the REMAINING shares behind a
                                # close order) hangs off a different parent and cannot be reached.
                                #
                                # NOT shared with the backtest deliberately: BacktestAccount builds
                                # its legs lazily at fill time from the FILLED entry quantity
                                # (`held = abs(net)`), so it cannot inherit a transient 0 and has no
                                # equivalent defect. Live must stage the leg before the fill because
                                # the broker needs it resting; that constraint is live-only.
                                #
                                # PERSISTED HERE, per leg, through its own short-lived session —
                                # NOT left as a pending mutation on the enter loop's session. That
                                # is what the previous version did behind `no_autoflush`, and it
                                # only moved the problem: the legs stayed dirty and attached, so
                                # the NEXT candidate's first query flushed them instead and took
                                # the write lock right before ITS submit. `no_autoflush` closes one
                                # column; owning nothing on that session closes them all.
                                try:
                                    from sqlmodel import select as _leg_select
                                    from .db import get_db as _get_db
                                    with _get_db() as leg_session:
                                        leg_ids = [l.id for l in leg_session.exec(
                                            _leg_select(TradingOrder).where(
                                                TradingOrder.depends_on_order == order.id)).all()]
                                    for leg_id in leg_ids:
                                        leg = self._row_or_none(TradingOrder, leg_id)
                                        if leg is not None and leg.quantity != order.quantity:
                                            self.logger.info(
                                                f"Stamped protective leg {leg.id} ({leg.order_type}) "
                                                f"qty {leg.quantity} -> {order.quantity} from entry {order.id}")
                                            leg.quantity = order.quantity
                                            update_instance(leg)
                                except Exception as leg_err:  # noqa: BLE001 — never block the entry
                                    self.logger.error(
                                        f"Failed to stamp protective-leg quantity for order {order.id}: "
                                        f"{leg_err}", exc_info=True)

                                # Live parity: submit with the RM safeguard SL (fo.stop_price) — the
                                # live path does NOT apply the backtest's tighter-wins merge (that is a
                                # separate, not-yet-approved live change), so behavior is preserved.
                                self.logger.info(f"Auto-submitting order {order.id} for {order.symbol}: {order.quantity} shares")
                                submitted_order = self._submit_funded_entry_with_retry(
                                    account, order, sl_price=fo.stop_price or None)
                                if submitted_order:
                                    submitted_count += 1
                                    # No expunge: `order` was never attached to this session in
                                    # the first place (see the detached fetch above), so there is
                                    # nothing to detach — and expunging only ever covered the
                                    # entry, never the legs, which is how the flush found another
                                    # way through on the next iteration.
                                    created_orders.append(order)
                                    self.logger.info(f"Successfully submitted order {order.id} to broker")
                                else:
                                    # COMPENSATE. Logging and moving on used to leave the entry
                                    # PENDING, its protective legs WAITING_TRIGGER and — the part
                                    # that actually costs trades — the Transaction submit_order
                                    # had already created sitting in WAITING. The SAFETY CHECK
                                    # above refuses any symbol+expert with a WAITING transaction
                                    # and nothing sweeps one, so a single failed submit retired
                                    # the symbol permanently. _fail_unsent_entry refuses unless
                                    # the order provably never reached the broker.
                                    self.logger.warning(f"Failed to submit order {order.id} to broker")
                                    self._fail_unsent_entry(
                                        order,
                                        f"the funded entry submit for {order.symbol} returned "
                                        f"nothing (DB-lock retries exhausted, or the broker "
                                        f"layer declined to send it)")
                            except Exception as submit_error:
                                self.logger.error(f"Error executing/submitting funded {candidate.symbol}: {submit_error}", exc_info=True)
                                # Same leak, other exit: submit_order RAISED (a validation error,
                                # a broker rejection, a lock that escaped the retry classifier).
                                # `order` is None when execute() itself failed, in which case
                                # there is nothing persisted to compensate.
                                if order is not None:
                                    self._fail_unsent_entry(
                                        order, f"submit raised for {candidate.symbol}: {submit_error}")

                        self.logger.info(f"Created + auto-submitted {submitted_count}/{len(entry_candidates)} orders to broker")
                        
                        # Refresh order statuses from broker to detect if any orders are already FILLED
                        # This is important for market orders which fill immediately
                        if submitted_count > 0:
                            self.logger.info("Refreshing order statuses from broker after submission")
                            try:
                                # Use fetch_all=True to ensure we get all orders including newly submitted ones
                                account.refresh_orders(fetch_all=True)
                                self.logger.info("Order status refresh completed")
                            except Exception as refresh_error:
                                self.logger.error(f"Error refreshing order statuses: {refresh_error}", exc_info=True)
                        
                        # After submitting orders, check for any dependent orders (e.g., TP/SL WAITING_TRIGGER)
                        # that may now be ready to execute
                        self.logger.info("Checking for dependent orders after risk management")
                        self._check_all_waiting_trigger_orders()
                    except Exception as e:
                        self.logger.error(f"Error during risk management for expert {expert_instance_id}: {e}", exc_info=True)
                
        except Exception as e:
            self.logger.error(f"Error processing expert recommendations after analysis for expert {expert_instance_id}: {e}", exc_info=True)
        finally:
            # Always release the lock when we're done
            processing_lock.release()
            self.logger.debug(f"Released processing lock for expert {expert_instance_id} (enter_market)")
        
        return created_orders
    
    def _has_pending_analysis_jobs(self, expert_instance_id: int) -> bool:
        """
        Check if there are pending market analysis jobs for a given expert instance.
        
        Args:
            expert_instance_id: The expert instance ID to check
            
        Returns:
            True if there are pending jobs, False otherwise
        """
        try:
            from .WorkerQueue import get_worker_queue
            from .types import AnalysisUseCase, WorkerTaskStatus
            
            worker_queue = get_worker_queue()
            all_tasks = worker_queue.get_all_tasks()
            
            # Check for pending enter_market tasks for this expert. The shared queue also holds
            # SmartRiskManagerTask entries, which have no `subtype` (they aren't analysis tasks) -
            # use getattr so those don't crash this check and get misread as "pending analysis".
            for task in all_tasks.values():
                if (task.expert_instance_id == expert_instance_id and
                    getattr(task, "subtype", None) == AnalysisUseCase.ENTER_MARKET and
                    task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]):
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking for pending analysis jobs for expert {expert_instance_id}: {e}", exc_info=True)
            return True  # Assume there are pending jobs if we can't check
    
    def force_sync_all_transactions(self):
        """
        Force synchronization of all transactions based on their linked order states.
        
        This method is intended to be run at startup to ensure all transaction states
        are in sync with their orders, without waiting for order state change triggers.
        
        It calls refresh_transactions for all accounts which will:
        - Update WAITING -> OPENED when market entry orders are FILLED
        - Update OPENED -> CLOSED when closing orders are FILLED
        - Update WAITING -> CLOSED when orders are canceled/rejected
        - Set open_price, close_price, open_date, close_date appropriately
        """
        try:
            from .models import AccountDefinition
            from ..modules.accounts import get_account_class
            
            self.logger.info("Starting force sync of all transactions at startup...")
            
            # Get all account definitions
            account_definitions = get_all_instances(AccountDefinition)
            
            total_synced = 0
            
            for account_def in account_definitions:
                try:
                    # Get the account class for this provider
                    account_class = get_account_class(account_def.provider)
                    if not account_class:
                        self.logger.warning(f"No account class found for provider {account_def.provider}")
                        continue
                    
                    # Create account instance
                    account = account_class(account_def.id)
                    
                    # Force sync transactions based on current order states
                    if hasattr(account, 'refresh_transactions'):
                        self.logger.info(f"Force syncing transactions for account {account_def.name}...")
                        success = account.refresh_transactions()
                        if success:
                            total_synced += 1
                            self.logger.info(f"Successfully synced transactions for {account_def.name}")
                        else:
                            self.logger.warning(f"Failed to sync transactions for {account_def.name}")
                    else:
                        self.logger.warning(f"Account {account_def.name} does not support transaction refresh")
                        
                except Exception as e:
                    self.logger.error(f"Error syncing transactions for account {account_def.name}: {e}", exc_info=True)
                    continue
            
            self.logger.info(f"Force sync completed: {total_synced}/{len(account_definitions)} accounts synced")
            
        except Exception as e:
            self.logger.error(f"Error during force sync of transactions: {e}", exc_info=True)
    
    def clean_pending_orders(self) -> Dict[str, Any]:
        """
        Clean unsubmitted pending and error orders (PENDING and ERROR only).
        
        CRITICAL: Do NOT delete WAITING_TRIGGER orders unless their parent order is ALSO being deleted.
        This prevents orphaning valid take-profit/stop-loss orders on existing open positions.
        
        For each PENDING/ERROR order:
        1. Find the associated transaction (if any)
        2. Close the transaction if it exists
        3. Delete the order and ONLY its dependent orders that are WAITING_TRIGGER
           (if the parent is being deleted, the dependent is also deleted)
        
        WAITING_TRIGGER orders whose parents are NOT being deleted are PRESERVED.
        
        Returns:
            Dict with cleanup statistics:
            {
                'orders_deleted': int,
                'transactions_closed': int,
                'dependents_deleted': int,
                'errors': List[str]
            }
        """
        try:
            from sqlmodel import select
            from .db import get_db
            from .models import Transaction, TradingOrder
            from .types import TransactionStatus, OrderStatus

            stats = {
                'orders_deleted': 0,
                'transactions_closed': 0,
                'dependents_deleted': 0,
                'errors': []
            }

            self.logger.info("Starting cleanup of pending orders (PENDING and ERROR only - preserving valid WAITING_TRIGGER orders)...")

            with get_db() as session:
                # CRITICAL: Only clean PENDING and ERROR orders - NOT WAITING_TRIGGER
                # WAITING_TRIGGER orders are preserved unless their parent order is also being deleted
                # ALSO: Skip orders that have depends_on_order set (dependent orders like TP/SL)
                pending_statuses = [OrderStatus.PENDING, OrderStatus.ERROR]
                statement = select(TradingOrder).where(
                    TradingOrder.status.in_(pending_statuses),
                    TradingOrder.depends_on_order.is_(None)  # Only delete orders that are NOT dependent on another order
                )
                pending_orders = session.exec(statement).all()
                
                self.logger.info(f"Found {len(pending_orders)} PENDING/ERROR orders to clean (excluding dependent orders)")
                
                # Create a set of order IDs being deleted for quick lookup
                orders_to_delete_ids = {order.id for order in pending_orders}
                
                # Track transactions to close
                transactions_to_close = set()
                orders_to_delete = []
                dependents_to_delete = []
                
                for order in pending_orders:
                    # Track associated transaction
                    if order.transaction_id:
                        transactions_to_close.add(order.transaction_id)
                        self.logger.debug(f"Order {order.id} linked to transaction {order.transaction_id}")
                    
                    # Find all dependent orders (orders that depend on this order)
                    dependent_statement = select(TradingOrder).where(
                        TradingOrder.depends_on_order == order.id
                    )
                    dependents = session.exec(dependent_statement).all()
                    
                    if dependents:
                        self.logger.debug(f"Order {order.id} has {len(dependents)} dependent orders")
                        # Only delete dependents - don't preserve WAITING_TRIGGER orders if their parent is deleted
                        dependents_to_delete.extend(dependents)
                    
                    orders_to_delete.append(order)
                
                # PHASE 1: Close transactions
                # CRITICAL: Only close transactions that have NO other active orders
                for txn_id in transactions_to_close:
                    try:
                        txn = session.get(Transaction, txn_id)
                        if txn:
                            # Check if this transaction has any other orders that are NOT being deleted
                            other_orders_statement = select(TradingOrder).where(
                                TradingOrder.transaction_id == txn_id,
                                TradingOrder.id.not_in(orders_to_delete_ids)
                            )
                            other_orders = session.exec(other_orders_statement).all()
                            
                            if other_orders:
                                # Transaction has other active orders - DO NOT CLOSE
                                self.logger.info(
                                    f"Preserving transaction {txn_id} - has {len(other_orders)} other orders: "
                                    f"{[f'{o.id}({o.status.value})' for o in other_orders]}"
                                )
                                continue
                            
                            # No other orders - safe to close
                            from .utils import close_transaction_with_logging
                            close_transaction_with_logging(
                                transaction=txn,
                                account_id=0,  # No specific account context in cleanup
                                close_reason="cleanup",
                                session=session
                            )
                            session.add(txn)
                            self.logger.info(f"Marked transaction {txn_id} as CLOSED (no remaining orders)")
                            stats['transactions_closed'] += 1
                        else:
                            error_msg = f"Transaction {txn_id} not found"
                            self.logger.warning(error_msg)
                            stats['errors'].append(error_msg)
                    except Exception as e:
                        error_msg = f"Error closing transaction {txn_id}: {e}"
                        self.logger.error(error_msg)
                        stats['errors'].append(error_msg)
                
                # PHASE 2: Delete dependent orders
                # CRITICAL SAFETY CHECK: Only delete dependents if their parent is being deleted
                # This prevents orphaning valid TP/SL orders on existing open positions
                for dependent_order in dependents_to_delete:
                    try:
                        # Verify the parent order is actually being deleted
                        parent_order_id = dependent_order.depends_on_order
                        if parent_order_id not in orders_to_delete_ids:
                            # Parent is NOT being deleted - PRESERVE this dependent order
                            self.logger.debug(
                                f"Skipping dependent order {dependent_order.id} "
                                f"(parent order {parent_order_id} is not being deleted - preserving valid order)"
                            )
                            continue
                        
                        session.delete(dependent_order)
                        self.logger.debug(f"Deleted dependent order {dependent_order.id}")
                        stats['dependents_deleted'] += 1
                    except Exception as e:
                        error_msg = f"Error deleting dependent order {dependent_order.id}: {e}"
                        self.logger.error(error_msg)
                        stats['errors'].append(error_msg)
                
                # PHASE 3: Delete main orders
                for order in orders_to_delete:
                    try:
                        session.delete(order)
                        self.logger.debug(f"Deleted pending order {order.id} (symbol: {order.symbol}, status: {order.status})")
                        stats['orders_deleted'] += 1
                    except Exception as e:
                        error_msg = f"Error deleting order {order.id}: {e}"
                        self.logger.error(error_msg)
                        stats['errors'].append(error_msg)
                
                # Commit all changes
                try:
                    session.commit()
                    self.logger.info(
                        f"Cleanup completed: deleted {stats['orders_deleted']} orders, "
                        f"{stats['dependents_deleted']} dependents, "
                        f"closed {stats['transactions_closed']} transactions"
                    )
                except Exception as e:
                    error_msg = f"Error committing cleanup: {e}"
                    self.logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    session.rollback()
            
            return stats
            
        except Exception as e:
            self.logger.error(f"Error during pending order cleanup: {e}", exc_info=True)
            return {
                'orders_deleted': 0,
                'transactions_closed': 0,
                'dependents_deleted': 0,
                'errors': [str(e)]
            }

    def process_open_positions_recommendations(self, expert_instance_id: int, lookback_days: int = 1) -> List[TradingOrder]:
        """
        Process expert recommendations for OPEN_POSITIONS analysis.
        
        This function evaluates recommendations against the open_positions ruleset
        and executes resulting trade actions (if automated trading is enabled).
        
        For OPEN_POSITIONS:
        - Process recommendations for symbols with existing positions
        - Use open_positions_ruleset_id instead of enter_market_ruleset_id
        - Consider all action types (CLOSE, SELL, BUY, HOLD)
        - Load existing transactions for each symbol
        
        Args:
            expert_instance_id: The expert instance ID to process recommendations for
            lookback_days: Number of days to look back for recommendations (default: 1)
            
        Returns:
            List of TradingOrder objects that were created
        """
        # Use open_positions as the use case for this method
        lock_key = f"expert_{expert_instance_id}_usecase_open_positions"
        
        # Get or create a lock for this expert/use_case combination
        with self._locks_dict_lock:
            if lock_key not in self._processing_locks:
                self._processing_locks[lock_key] = threading.Lock()
            processing_lock = self._processing_locks[lock_key]
        
        # Try to acquire the lock with a very short timeout (0.5 seconds)
        lock_acquired = processing_lock.acquire(blocking=True, timeout=0.5)
        
        if not lock_acquired:
            self.logger.info(f"Could not acquire lock for expert {expert_instance_id} (open_positions) - another thread is already processing. Skipping.")
            return []
        
        created_orders = []
        
        try:
            self.logger.debug(f"Acquired processing lock for expert {expert_instance_id} (open_positions)")
            
            from sqlmodel import select
            from .db import get_db
            from .models import Transaction, AccountDefinition, ExpertInstance
            from .types import AnalysisUseCase, TransactionStatus
            from .utils import get_expert_instance_from_id
            from datetime import timedelta
            from .TradeActionEvaluator import TradeActionEvaluator
            from ..modules.accounts import get_account_class

            # Get the expert instance (with loaded settings)
            expert = get_expert_instance_from_id(expert_instance_id)
            if not expert:
                self.logger.error(f"Expert instance {expert_instance_id} not found")
                return created_orders

            # Get the expert instance model (for ruleset IDs)
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                self.logger.error(f"Expert instance model {expert_instance_id} not found")
                return created_orders

            # Check if "Allow automated trade modification" is enabled
            allow_automated_trade_modification = expert.get_setting_with_interface_default(
                'allow_automated_trade_modification', log_warning=False
            )
            if not allow_automated_trade_modification:
                self.logger.debug(f"Automated trade modification disabled for expert {expert_instance_id}, skipping recommendation processing")
                return created_orders

            # Check if there's an open_positions ruleset configured
            if not expert_instance.open_positions_ruleset_id:
                self.logger.debug(f"No open_positions ruleset configured for expert {expert_instance_id}, skipping automated trade modification")
                return created_orders

            # Get the account instance for this expert
            account_def = get_instance(AccountDefinition, expert_instance.account_id)
            if not account_def:
                self.logger.error(f"Account definition {expert_instance.account_id} not found")
                return created_orders

            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found")
                return created_orders

            account = account_class(account_def.id)

            # Get recent recommendations based on lookback_days parameter
            cutoff_time = datetime.now(timezone.utc) - timedelta(days=lookback_days)

            with get_db() as session:
                # Get all recommendations for this expert instance within the time window
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.instance_id == expert_instance_id,
                    ExpertRecommendation.created_at >= cutoff_time
                ).order_by(ExpertRecommendation.created_at.desc())

                all_recommendations = session.exec(statement).all()

                if not all_recommendations:
                    self.logger.info(f"No recommendations found for expert {expert_instance_id}")
                    return created_orders

                # Prefer recommendations from OPEN_POSITIONS-subtype analyses: without this, a
                # NEWER enter-market rec (e.g. this morning's entry scan for a held symbol)
                # shadows the open-positions assessment and the exit rules are evaluated against
                # an entry thesis. Selection precedence (each falls back to the next), always
                # ending at ALL recs so a legacy/unstamped set is never silently dropped:
                #   1. ExpertRecommendation.subtype == OPEN_POSITIONS  (the direct column, gap #5 —
                #      preferred; writers now stamp it, and the backtest always evaluates a fresh
                #      OPEN_POSITIONS rec, so this closes the live/backtest selection gap);
                #   2. linked MarketAnalysis.subtype == OPEN_POSITIONS (audit-A3 heuristic, for
                #      rows written before the column existed but with a stamped analysis);
                #   3. ALL recs (manual analyses / legacy NULL-subtype rows) — preserve old behaviour.
                candidate_recommendations = [r for r in all_recommendations
                                             if r.subtype == AnalysisUseCase.OPEN_POSITIONS]
                if not candidate_recommendations:
                    from .models import MarketAnalysis
                    ma_ids = {r.market_analysis_id for r in all_recommendations if r.market_analysis_id}
                    open_pos_rec_ids = set()
                    if ma_ids:
                        ma_rows = session.exec(
                            select(MarketAnalysis).where(MarketAnalysis.id.in_(ma_ids))
                        ).all()
                        open_pos_ma = {m.id for m in ma_rows
                                       if m.subtype == AnalysisUseCase.OPEN_POSITIONS}
                        open_pos_rec_ids = {r.id for r in all_recommendations
                                            if r.market_analysis_id in open_pos_ma}
                    candidate_recommendations = (
                        [r for r in all_recommendations if r.id in open_pos_rec_ids]
                        if open_pos_rec_ids else all_recommendations)

                # Filter to get only the latest recommendation per instrument
                latest_per_instrument = {}
                for rec in candidate_recommendations:
                    if rec.symbol not in latest_per_instrument:
                        latest_per_instrument[rec.symbol] = rec
                
                # Convert to list
                recommendations = list(latest_per_instrument.values())
                
                self.logger.info(f"Found {len(recommendations)} unique instruments with open_positions recommendations for expert {expert_instance_id} (filtered from {len(all_recommendations)} total recommendations)")
                self.logger.info(f"Evaluating recommendations through open_positions ruleset: {expert_instance.open_positions_ruleset_id}")
                
                # Process each recommendation through the open_positions ruleset
                for recommendation in recommendations:
                    try:
                        # Check if this symbol has existing transactions for THIS expert only
                        statement = select(Transaction).where(
                            Transaction.symbol == recommendation.symbol,
                            Transaction.status.in_([TransactionStatus.WAITING, TransactionStatus.OPENED]),
                            Transaction.expert_id == expert_instance_id
                        )
                        existing_transactions = session.exec(statement).all()

                        # Skip a transaction that already has a closing order WORKING
                        # (submitted on an earlier cycle, not yet filled/canceled) — otherwise
                        # this loop re-evaluates exit rules and can submit ANOTHER closing
                        # order for the same position on every subsequent cycle until the
                        # first one resolves. Shared with the backtest engine's equivalent
                        # guard (found investigating the 2026-07-21 options-grid equity
                        # runaway); see ReadOnlyAccountInterface.has_pending_closing_order.
                        existing_transactions = [
                            t for t in existing_transactions
                            if not account.has_pending_closing_order(t.id)
                        ]

                        if not existing_transactions:
                            self.logger.debug(f"No existing transactions for {recommendation.symbol}, skipping recommendation {recommendation.id}")
                            continue
                        
                        # Create TradeActionEvaluator with existing transactions for open_positions use case
                        evaluator = TradeActionEvaluator(
                            account=account,
                            instrument_name=recommendation.symbol,
                            existing_transactions=existing_transactions
                        )

                        # Resolve the primary (entry) order from the oldest open transaction
                        # This is needed for conditions like DaysOpenedCondition that use order.created_at
                        oldest_transaction = min(existing_transactions, key=lambda t: t.open_date or t.created_at)
                        existing_order = resolve_entry_order(session, oldest_transaction)
                        if existing_order:
                            self.logger.debug(f"Resolved entry order {existing_order.id} for {recommendation.symbol} (created: {existing_order.created_at})")
                        else:
                            # Not benign: every P&L / days-opened condition on this symbol will
                            # now evaluate False and CloseAction loses its transaction link.
                            self.logger.warning(
                                f"No FILLED {oldest_transaction.side} order on transaction "
                                f"{oldest_transaction.id} ({recommendation.symbol}) — exit "
                                f"conditions needing an entry order cannot evaluate")

                        # Evaluate recommendation through the open_positions ruleset
                        self.logger.debug(f"Evaluating recommendation {recommendation.id} for {recommendation.symbol} (open_positions)")

                        action_summaries = evaluator.evaluate(
                            instrument_name=recommendation.symbol,
                            expert_recommendation=recommendation,
                            ruleset_id=expert_instance.open_positions_ruleset_id,
                            existing_order=existing_order
                        )
                        
                        # Check if evaluation produced any actions
                        if not action_summaries:
                            self.logger.debug(f"Recommendation {recommendation.id} for {recommendation.symbol} - no actions to execute (conditions not met)")
                            
                            # Store evaluation details even when no actions are created
                            # This is crucial for analysis, debugging, and understanding why rules didn't trigger
                            evaluation_details = evaluator.get_evaluation_details()
                            if evaluation_details:
                                from ..core.models import TradeActionResult
                                from ..core.db import add_instance
                                
                                evaluation_result = TradeActionResult(
                                    action_type='evaluation_only',
                                    success=True,  # Evaluation succeeded, just no actions needed
                                    message=f'Rule evaluation completed for {recommendation.symbol} (OPEN_POSITIONS) - no actions triggered (conditions not met)',
                                    data={'evaluation_details': evaluation_details},
                                    expert_recommendation_id=recommendation.id
                                )
                                add_instance(evaluation_result)
                                self.logger.debug(f"Stored evaluation details for recommendation {recommendation.id} (OPEN_POSITIONS, no actions)")
                            
                        # Check for evaluation errors
                        elif any('error' in summary for summary in action_summaries):
                            errors = [s.get('error') for s in action_summaries if 'error' in s]
                            self.logger.warning(f"Recommendation {recommendation.id} evaluation had errors: {errors}")
                            
                            # Store evaluation details even when errors occurred
                            evaluation_details = evaluator.get_evaluation_details()
                            if evaluation_details:
                                from ..core.models import TradeActionResult
                                from ..core.db import add_instance
                                
                                evaluation_result = TradeActionResult(
                                    action_type='evaluation_error',
                                    success=False,
                                    message=f'Rule evaluation encountered errors for {recommendation.symbol} (OPEN_POSITIONS): {"; ".join(errors)}',
                                    data={'evaluation_details': evaluation_details, 'errors': errors},
                                    expert_recommendation_id=recommendation.id
                                )
                                add_instance(evaluation_result)
                                self.logger.debug(f"Stored evaluation details for recommendation {recommendation.id} (OPEN_POSITIONS, errors)")
                            
                        else:
                            self.logger.info(f"Recommendation {recommendation.id} for {recommendation.symbol} passed ruleset - {len(action_summaries)} action(s) to execute")

                            # Capture evaluation details before execution so they are always stored
                            evaluation_details = evaluator.get_evaluation_details()

                            # Execute the actions with submit_to_broker flag set based on permission setting
                            try:
                                execution_results = evaluator.execute(submit_to_broker=allow_automated_trade_modification)
                                if execution_results:
                                    created_count = sum(1 for r in execution_results if r.get('success'))
                                    pending_count = sum(1 for r in execution_results if not allow_automated_trade_modification and r.get('success'))
                                    if allow_automated_trade_modification:
                                        self.logger.info(f"Executed {created_count} action(s) for {recommendation.symbol}")
                                    else:
                                        self.logger.info(f"Created {created_count} pending action(s) for {recommendation.symbol} (awaiting manual review)")
                                    created_orders.extend(execution_results)
                                else:
                                    self.logger.debug(f"No orders created from actions for {recommendation.symbol}")
                            except Exception as e:
                                self.logger.error(f"Error executing actions for recommendation {recommendation.id}: {e}", exc_info=True)

                            # Store evaluation details so the rule analysis icon appears in the UI
                            if evaluation_details:
                                from ..core.models import TradeActionResult
                                from ..core.db import add_instance

                                evaluation_result = TradeActionResult(
                                    action_type='evaluation_with_actions',
                                    success=True,
                                    message=f'Rule evaluation completed for {recommendation.symbol} (OPEN_POSITIONS) - {len(action_summaries)} action(s) triggered',
                                    data={'evaluation_details': evaluation_details},
                                    expert_recommendation_id=recommendation.id
                                )
                                add_instance(evaluation_result)
                                self.logger.debug(f"Stored evaluation details for recommendation {recommendation.id} (OPEN_POSITIONS, actions executed)")
                        
                    except Exception as e:
                        self.logger.error(f"Error evaluating open_positions recommendation {recommendation.id}: {e}", exc_info=True)
                
                return created_orders
                
        finally:
            # Release the lock
            processing_lock.release()
            self.logger.debug(f"Released processing lock for expert {expert_instance_id} (open_positions)")


# Global trade manager instance
_trade_manager = None

def get_trade_manager() -> TradeManager:
    """Get the global trade manager instance."""
    global _trade_manager
    if _trade_manager is None:
        _trade_manager = TradeManager()
    return _trade_manager