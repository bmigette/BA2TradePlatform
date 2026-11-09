"""
TradeManager - Core component for handling trade recommendations and order execution

This class reviews trade recommendations from market experts and places orders based on
expert settings, rulesets, and trading permissions.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import math
import threading
from ..logger import logger
from .models import ExpertRecommendation, ExpertInstance, TradingOrder, Ruleset, Transaction
from .types import OrderRecommendation, OrderStatus, OrderDirection, OrderOpenType, OrderType
from .db import get_instance, get_all_instances, add_instance, update_instance
from ba2_common.core.replay.clock import replay_now
from ba2_common.core.trade_cycle import (
    entry_submit_stop, row_or_none, select_enter_candidates, stage_funded_entry,
    submit_funded_entry)
from ba2_common.core.replay.decision_record import (
    note_funded, note_gate, note_pass_error, note_selection, pass_discard, pass_fail, pass_skip,
    record_execute, record_pass_state)
from ba2_common.core.washtrade import (
    went_out_as_contended_complex, was_rejected_as_washtrade, stamp_rejection)
# ONE constant, TWO readers: the OCO stop leg places its limit this far through the stop,
# and _force_close_breached_stops below decides a stop was jumped by the same number. See
# the constant's own comment in AlpacaAccount for why they can never be allowed to drift.
from ..modules.accounts.AlpacaAccount import OCO_STOP_LIMIT_CUSHION


# Serializes account refreshes across all entry points (scheduled job, immediate job,
# manual triggers). It does NOT serialize WAITING_TRIGGER submission on its own: every
# expert's entry pass also calls _check_all_waiting_trigger_orders, outside this lock --
# that is what the per-order claim below is for.
_REFRESH_LOCK = threading.Lock()

# Per-ORDER claim on the submission of a WAITING_TRIGGER dependent order. The scheduled
# refresh and any number of entry passes run _check_all_waiting_trigger_orders concurrently,
# each submitting from its own Phase-1 snapshot; without this, two of them could send the
# same protective leg twice (a duplicate stop at the broker, or a 40310000 rejection written
# back as ERROR over the leg the first one placed). A claim is held from the fresh re-read of
# the row until its outcome is in the DB, so a later claimant re-reads that outcome and skips.
#
# Why not a DB claim (UPDATE ... WHERE status=WAITING_TRIGGER): the only "being submitted"
# status is PENDING, and a PENDING row with depends_on_order is exactly what
# AlpacaAccount._check_and_submit_dependent_orders submits -- the claim itself would invite
# a second submit. Why not one lock around the method: the entry pass would have to skip
# (its just-filled position unprotected until the next refresh) or wait behind slow broker
# calls. The guard below is held only for a set operation, never across I/O.
# In-process only: one live process owns a DB.
_DEPENDENT_SUBMIT_CLAIMS: set = set()
_DEPENDENT_SUBMIT_CLAIMS_GUARD = threading.Lock()


def _claim_dependent_submit(order_id: int) -> bool:
    """Claim the right to submit WAITING_TRIGGER order ``order_id``; False if another thread holds it."""
    with _DEPENDENT_SUBMIT_CLAIMS_GUARD:
        if order_id in _DEPENDENT_SUBMIT_CLAIMS:
            return False
        _DEPENDENT_SUBMIT_CLAIMS.add(order_id)
        return True


def _release_dependent_submit(order_id: int) -> None:
    with _DEPENDENT_SUBMIT_CLAIMS_GUARD:
        _DEPENDENT_SUBMIT_CLAIMS.discard(order_id)

# Market-regime benchmark + its once-per-day classification cache (see
# TradeManager._publish_market_regime). SPY: the classifier in ba2_common.core.market_regime was
# measured on it, and the backtest uses the same symbol so live and simulated agree.
_REGIME_BENCHMARK = "SPY"
_LIVE_REGIME_CACHE = {"day": None, "stressed": None, "retry_after": None, "failures": 0}

# How long a FAILED classification waits before the next attempt. A failure is never cached for
# the day: until 2026-10-07 one failed SPY fetch (a single WARNING) left every overlay-enabled
# expert trading at neutral scales until the next calendar day. The data provider already retries
# the HTTP call itself; this spaces the attempts of a refresh loop that runs many times a day.
_REGIME_RETRY_SECONDS = 300

# SPY benchmark regime for the ``spy-trend`` / ``spy-vol`` entry gates (TradeManager._publish_benchmark_regime).
# ``session`` is the newest FINISHED session whose regime is cached (NOT a calendar day: after the 16:00
# close the key changes and the new close is picked up the same evening); ``retry_after``/``failures``
# mirror _LIVE_REGIME_CACHE; ``failure`` is the reason the last attempt published nothing usable;
# ``feed_warned_day`` is the day the source-consistency WARNING last fired.
_BENCHMARK_REGIME_CACHE = {"session": None, "regime": None, "retry_after": None, "failures": 0,
                           "failure": None, "feed_warned_day": None}
#: Calendar days of SPY daily history to fetch (~825 sessions, above the 524 the vol axis needs).
_BENCHMARK_LOOKBACK_DAYS = 1200


def _reset_benchmark_regime_cache() -> None:
    """Forget the cached regime / failure / warning day (tests, and a clean start)."""
    _BENCHMARK_REGIME_CACHE.update({"session": None, "regime": None, "retry_after": None,
                                    "failures": 0, "failure": None, "feed_warned_day": None})

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

# Grace period before a WAITING transaction whose entry looks dead is actually failed.
# A transaction and its entry order are written by two separate statements, so a verdict
# taken between them would act on a half-written state. Mirrors the 5-minute floor
# AlpacaAccount's refresh already applies before concluding anything about a freshly
# submitted order. See _check_stranded_waiting_transactions.
_STRANDED_ENTRY_GRACE_MINUTES = 5.0

# How long a CLOSING transaction must have had NOTHING working (its last order at least this
# old) before _retry_stuck_closes asks close_transaction again. Long enough that a close just
# submitted or just rejected is not hammered every refresh; short enough that a stranded
# position is retried the same session. See _retry_stuck_closes.
_STUCK_CLOSE_RETRY_MINUTES = 30.0


def classify_waiting_trigger(parent_status, trigger_status, parent_filled_qty=None):
    """Decide what to do with a WAITING_TRIGGER dependent order given its parent's current
    status, the configured trigger status and how much the parent filled.

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
    # A parent that ended CANCELED/EXPIRED after a PARTIAL fill still traded those shares, and
    # its fill-triggered exit is what protects them. 2026-10-29 RAPP (dev account 1): entry
    # 1438 filled 34 of 35 and was cancelled; its staged OCO was cancelled with it and the 34
    # shares sat with no stop. The caller sizes the exit to the filled quantity.
    if (trigger_status == OrderStatus.FILLED and parent_filled_qty
            and parent_filled_qty > 0 and parent_status in OrderStatus.get_terminal_statuses()):
        return "submit"
    if parent_status in OrderStatus.get_terminal_statuses():
        return "cancel"
    return "wait"


def classify_waiting_entry(entry_orders):
    """Decide the fate of a WAITING transaction from the state of its ENTRY orders.

    A transaction is created in WAITING by ``submit_order`` and only leaves that state
    when its entry FILLS (-> OPENED) or when something explicitly fails it. Nothing today
    fails an entry that DID reach the broker and was then killed there, so such a
    transaction stays WAITING forever — and the enter_market safety check refuses any
    symbol+expert that already has a WAITING transaction, so the symbol is retired for
    that expert permanently. Measured on dev 2026-09-22: txns 28/29 (SHOP, UNH, since
    09-14) and 131/133 (NVDA, AVGO, since 09-21), all expert 2 / account 1, each with a
    CANCELED entry that filled 0. See docs/WASHTRADE-LOCK.md.

    Returns one of:
        "wait"      - no entry row yet, or at least one entry is still in flight. Nothing
                      to conclude; ask again next refresh.
        "filled"    - every entry is terminal and at least one reports filled quantity.
                      A PARTIAL fill is a REAL POSITION: it must never be failed, or the
                      platform forgets shares it actually owns. Reconciliation
                      (``TransactionHelper.reconcile_canceled_partial_fill``) owns this
                      case, not the give-up path.
        "ambiguous" - every entry is terminal, none reports filled quantity, and the
                      evidence does not agree that nothing traded: a NULL ``filled_qty``
                      on an order the broker has touched (unknown, not zero), or a zero
                      quantity next to a recorded fill PRICE. A disagreement is not a
                      licence to act: refuse loudly rather than guess.
        "dead"      - every entry is terminal and nothing filled. Give up on it.

    Terminality is checked FIRST so that a partially-filled but still-working entry reads
    as "wait" (it is progressing toward FILLED) rather than as a finished partial.

    A NULL ``filled_qty`` is only read as "nothing filled" when the order carries no
    ``broker_order_id`` — an order the broker never saw CANNOT have traded, which is a
    measured fact rather than a coercion of unknown to zero. With a broker id, NULL means
    the row was never synced and the quantity is genuinely unknown.
    """
    orders = list(entry_orders or [])
    if not orders:
        return "wait"
    terminal = OrderStatus.get_terminal_statuses()
    # FILLED is deliberately NOT in get_terminal_statuses() (that set drives buying-power
    # release); an entry that filled is nonetheless final and is caught by the fill checks
    # below, so treat it as settled here rather than as "still working".
    for order in orders:
        if order.status not in terminal and order.status != OrderStatus.FILLED:
            return "wait"
    for order in orders:
        filled_qty = getattr(order, 'filled_qty', None)
        if filled_qty is not None and float(filled_qty) > 0:
            return "filled"
    for order in orders:
        if (getattr(order, 'filled_qty', None) is None
                and getattr(order, 'broker_order_id', None)):
            return "ambiguous"
        open_price = getattr(order, 'open_price', None)
        if open_price is not None and float(open_price) > 0:
            return "ambiguous"
    return "dead"


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


# The re-base rules live in ba2_common (shared with the backtest). rebase_price_to_fill is
# re-exported here because it has long been importable from TradeManager.
from ba2_common.core.tpsl_fill_rebase import (  # noqa: E402,F401
    read_anchor, rebase_levels_at_fill, rebase_price_to_fill, stamp_anchor)


def resolve_entry_order(session, transaction):
    """The transaction's ENTRY order: its first-written ROOT order (no ``depends_on_order``,
    no ``parent_order_id``), by UTC ``created_at`` then id, any status.

    ONE definition, shared with the backtest (``BacktestAccount._entry_order_for_transaction``):
    ``ba2_common.core.entry_order.entry_order_for_transaction`` (plan 2026-09-25
    option-stage2-build, Task 8 review I1). It is the row every entry path writes its facts and
    its style-member tag onto (a combo's PARENT, never a leg child), so the tag an assignment
    passes on (``AlpacaAccount._position_entry_tag``) and the tag the exits compare
    (``entry_order.oldest_entry_order``) are read off the same row. It replaced "the oldest
    FILLED order on the transaction's side", which is the same row on every normal position.

    A surprising amount hangs off this row being found: ``DaysOpenedCondition``,
    ``ProfitLossAmountCondition`` and ``ProfitLossPercentCondition`` each return False
    unconditionally without one, and ``CloseAction`` only takes its ``close_transaction()``
    path (the one that links the close back to the transaction, so
    ``has_pending_closing_order`` can see it) when the order carries a ``transaction_id``. A
    position whose shares arrived without an order row — an option assignment used to be
    exactly that — is therefore unmanageable; the open-positions pass refuses it by name
    (``EntryOrderUnresolved``).
    """
    from ba2_common.core.entry_order import entry_order_for_transaction

    if transaction is None:
        return None
    return entry_order_for_transaction(transaction.id, session=session)


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

        A SUCCESSFUL classification is cached for the day, because its inputs are DAILY closes:
        re-fetching 3 years of SPY on every refresh (which runs many times a day) buys nothing.

        A FAILED one is NOT cached. It publishes ``None`` = "unclassified" = neutral scales (never
        trade off a guessed regime), logs an ERROR -- an overlay-enabled expert is then sizing
        without its regime scale, which is not the strategy that was backtested -- and is retried
        on the next refresh once ``_REGIME_RETRY_SECONDS`` have passed, until it succeeds.
        "Failed" covers an exception, no bars, and a classifier that could not answer (too few
        closes): the last used to read as "not stressed" with no trace at all.
        """
        from datetime import datetime, timedelta, timezone

        from ba2_common.core.market_regime import STRESSED, classify_volatility_regime
        from ba2_common.core.regime_overlay import set_stressed

        now = datetime.now(timezone.utc)
        today = now.date()
        retry_after = _LIVE_REGIME_CACHE["retry_after"]
        if _LIVE_REGIME_CACHE["day"] != today and (retry_after is None or now >= retry_after):
            stressed = None
            failure = None
            try:
                from ba2_providers import get_provider
                # 1200 calendar days ~= 825 sessions, comfortably above the 524 daily closes
                # (20 vol window + 504 rank lookback) the classifier needs before it will answer.
                df = get_provider("ohlcv", "fmp").get_ohlcv_data(
                    _REGIME_BENCHMARK,
                    start_date=now - timedelta(days=1200),
                    end_date=now,
                    interval="1d",
                )
                if df is None or not len(df):
                    failure = f"no {_REGIME_BENCHMARK} daily bars returned"
                else:
                    closes = [c for c in df["Close"].tolist() if c is not None]
                    verdict = classify_volatility_regime(closes)
                    if verdict["regime"] is None:
                        failure = (f"classifier could not answer on {len(closes)} "
                                   f"{_REGIME_BENCHMARK} closes ({verdict['reason']})")
                    else:
                        stressed = verdict["regime"] == STRESSED
            except Exception as e:  # noqa: BLE001 -- the regime must never block a refresh
                failure = f"{type(e).__name__}: {e}"
                self.logger.error(f"Market regime fetch failed: {failure}", exc_info=True)

            if failure is None:
                _LIVE_REGIME_CACHE.update(
                    {"day": today, "stressed": stressed, "retry_after": None, "failures": 0})
                self.logger.info(f"Market regime for {today}: stressed={stressed}")
            else:
                _LIVE_REGIME_CACHE["failures"] += 1
                _LIVE_REGIME_CACHE["stressed"] = None
                _LIVE_REGIME_CACHE["retry_after"] = now + timedelta(seconds=_REGIME_RETRY_SECONDS)
                self.logger.error(
                    f"Market regime for {today} is UNCLASSIFIED (attempt "
                    f"{_LIVE_REGIME_CACHE['failures']}: {failure}). Experts with the regime "
                    f"overlay enabled trade at NEUTRAL scales until it is classified; retrying "
                    f"in {_REGIME_RETRY_SECONDS}s.")

        set_stressed(_LIVE_REGIME_CACHE["stressed"])

    def _benchmark_regime_needed(self) -> bool:
        """True when some ENABLED expert's entry or exit ruleset carries a ``spy_trend_regime`` /
        ``spy_vol_regime`` leaf. A platform with no gated ruleset makes no SPY provider call."""
        from ba2_common.core.market_condition_live import benchmark_leaves_in_ruleset

        for expert in get_all_instances(ExpertInstance):
            if not expert.enabled:
                continue
            for ruleset_id in (expert.enter_market_ruleset_id, expert.open_positions_ruleset_id):
                if ruleset_id is not None and benchmark_leaves_in_ruleset(ruleset_id):
                    return True
        return False

    def _fetch_benchmark_daily(self, now):
        """``(dates, closes)`` of SPY DAILY bars up to ``now`` from the history provider.

        FMP, deliberately: owner decision 2026-10-24 -- the SPY regime reads the same source as the
        stock price, which is FMP on all three live instances. (The provider may include a partial
        TODAY bar after the open; ``knowable_closes`` drops it.)"""
        from datetime import timedelta

        import pandas as pd
        from ba2_providers import get_provider

        df = get_provider("ohlcv", "fmp").get_ohlcv_data(
            _REGIME_BENCHMARK,
            start_date=now - timedelta(days=_BENCHMARK_LOOKBACK_DAYS),
            end_date=now,
            interval="1d",
        )
        if df is None or not len(df):
            return [], []
        return ([pd.Timestamp(d).date() for d in df["Date"].tolist()],
                [None if pd.isna(c) else float(c) for c in df["Close"].tolist()])

    def _warn_benchmark_source_mismatch(self, today) -> None:
        """WARNING (never a refusal), once per day: the SPY regime reads FMP, so an Alpaca account that
        trades with a stored ``data_feed`` other than ``fmp`` prices its stocks from a different source
        than the regime the gate decides on. Unset = the declared default (``delayed_sip``)."""
        if _BENCHMARK_REGIME_CACHE["feed_warned_day"] == today:
            return
        from sqlmodel import select

        from .db import get_db
        from .models import AccountDefinition, AccountSetting

        from ..modules.accounts.AlpacaAccount import AlpacaAccount

        default_feed = AlpacaAccount.get_settings_definitions()["data_feed"]["default"]
        with get_db() as session:
            feeds = {s.account_id: s.value_str for s in session.exec(
                select(AccountSetting).where(AccountSetting.key == "data_feed")).all()}
        trading_accounts = {e.account_id for e in get_all_instances(ExpertInstance) if e.enabled}
        mismatched = []
        for account in get_all_instances(AccountDefinition):
            if account.provider != "AlpacaAccount" or account.id not in trading_accounts:
                continue
            feed = (feeds.get(account.id) or default_feed).lower()
            if feed != "fmp":
                mismatched.append(f"{account.name} (id {account.id}) prices from {feed}")
        _BENCHMARK_REGIME_CACHE["feed_warned_day"] = today
        if mismatched:
            self.logger.warning(
                "SPY regime reads FMP but " + "; ".join(mismatched) + ". The SPY regime gates classify FMP "
                "daily closes while these accounts price stocks from another feed; set the account's "
                "data_feed to fmp so live decides on the data the backtest assumed.")

    def _publish_benchmark_regime(self, now=None) -> None:
        """Publish the SPY trend/vol regime from FINISHED sessions on the ``benchmark_regime`` seam.

        Live counterpart of the backtest engine's per-bar ``set_benchmark_regime(calendar.at(...))``: the
        SAME ``classify_benchmark_regime`` the calendar is asserted equal to, fed the closes of finished
        sessions only (``knowable_closes`` drops a partial TODAY bar), so a live decision sees exactly
        the last close the backtest's decision at that instant would.

        Cached under the newest FINISHED session (not the calendar day) on success only. A FAILURE
        (exception, no bars, nothing finished, calendar outage, data more than one session stale)
        publishes NOTHING usable -- ``None`` plus a reason the gate reports -- logs an ERROR, and is
        retried once ``_REGIME_RETRY_SECONDS`` have passed. The gate then reads ``no_regime`` and never
        passes, which is loud, not silent. Never raises into the caller: an entry/exit pass must go on.
        """
        from datetime import datetime, timedelta, timezone

        from ba2_common.core import benchmark_regime as br
        from ba2_common.core.market_calendar import regular_session_close_utc, regular_session_dates

        cache = _BENCHMARK_REGIME_CACHE
        now = datetime.now(timezone.utc) if now is None else now
        try:
            if not self._benchmark_regime_needed():
                br.reset_benchmark_regime()
                return
        except Exception as e:  # noqa: BLE001 -- detection must never block a pass
            self.logger.error(f"Benchmark regime: could not tell whether a ruleset needs it ({e}); "
                              f"publishing nothing", exc_info=True)
            br.set_benchmark_regime(None)
            br.set_benchmark_regime_failure(f"{type(e).__name__}: {e}")
            return
        try:
            self._warn_benchmark_source_mismatch(now.date())
        except Exception as e:  # noqa: BLE001 -- a diagnostic, never a blocker
            self.logger.warning(f"Benchmark regime: source-consistency check failed: {e}")

        failure = None
        lagged = False
        regime = None
        newest = None
        try:
            sessions = regular_session_dates(now.date() - timedelta(days=12), now.date())
            finished = [d for d in sessions if regular_session_close_utc(d) <= now]
            if len(finished) < 2:
                raise RuntimeError(f"no finished NYSE session found before {now.isoformat()}")
            newest, previous = finished[-1], finished[-2]

            if cache["session"] == newest and cache["regime"] is not None:
                br.set_benchmark_regime(cache["regime"])
                br.set_benchmark_regime_failure(None)
                return
            if cache["retry_after"] is not None and now < cache["retry_after"]:
                # inside the retry window: no fetch; republish what the last attempt left (a lagged
                # regime stays published; a failure stays None plus its reason)
                br.set_benchmark_regime(cache["regime"] if cache["failure"] is None else None)
                br.set_benchmark_regime_failure(cache["failure"])
                return

            dates, closes = self._fetch_benchmark_daily(now)
            if not len(dates):
                failure = f"no {_REGIME_BENCHMARK} daily bars returned"
            else:
                dates, closes = br.knowable_closes(dates, closes, now)
                if not len(dates):
                    failure = f"no finished {_REGIME_BENCHMARK} session among the returned bars"
                elif dates[-1] < previous:
                    failure = (f"stale {_REGIME_BENCHMARK} data: newest finished bar is {dates[-1]}, "
                               f"expected {newest} (at most one session of lag is tolerated)")
                else:
                    regime = br.classify_benchmark_regime(closes, session=dates[-1])
                    lagged = dates[-1] < newest
        except Exception as e:  # noqa: BLE001 -- the regime must never block an entry/exit pass
            failure = f"{type(e).__name__}: {e}"
            self.logger.error(f"Benchmark regime fetch failed: {failure}", exc_info=True)

        if failure is not None:
            cache["failures"] += 1
            cache.update({"session": None, "regime": None, "failure": failure,
                          "retry_after": now + timedelta(seconds=_REGIME_RETRY_SECONDS)})
            br.set_benchmark_regime(None)
            br.set_benchmark_regime_failure(failure)
            self.logger.error(
                f"Benchmark regime is UNCLASSIFIED (attempt {cache['failures']}: {failure}). The "
                f"spy-trend / spy-vol gates read no_regime and will NOT pass until it is classified; "
                f"retrying in {_REGIME_RETRY_SECONDS}s.")
            return

        cache["failures"] = 0
        cache["failure"] = None
        cache["regime"] = regime
        if lagged:
            # published, but not cached: the missing session may arrive within minutes
            cache["session"] = None
            cache["retry_after"] = now + timedelta(seconds=_REGIME_RETRY_SECONDS)
            self.logger.warning(
                f"Benchmark regime uses {regime.session}, one session behind {newest} (provider lag); "
                f"retrying in {_REGIME_RETRY_SECONDS}s.")
        else:
            cache["session"] = newest
            cache["retry_after"] = None
            self.logger.info(f"Benchmark regime as of {regime.session}: trend={regime.trend} vol={regime.vol}")
        br.set_benchmark_regime(regime)
        br.set_benchmark_regime_failure(None)

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
            self._publish_benchmark_regime()

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

                    # The OPTION half of the same question (OPT-S4). The reconciler above
                    # SKIPS every option transaction -- get_positions() is equity-only --
                    # and the activity reconciler below sees only assignment/exercise/
                    # expiry over a 7-day window. A manual close in the broker's own UI, or
                    # a broker risk action, matched NEITHER, so the ledger position and its
                    # buying-power reserve leaked permanently (FILLED is not a terminal
                    # transaction status). Matches per OCC contract, and does nothing at all
                    # on a fetch failure, an unreadable size, or a structure only partly
                    # gone. No-op for non-options accounts.
                    if hasattr(account, 'reconcile_externally_closed_option_transactions'):
                        try:
                            closed = account.reconcile_externally_closed_option_transactions()
                            if closed:
                                self.logger.info(
                                    f"Reconciled {closed} externally-closed OPTION transaction(s) "
                                    f"for {account_def.name}"
                                )
                        except Exception as e:
                            self.logger.error(
                                f"Error reconciling externally-closed option transactions for "
                                f"{account_def.name}: {e}",
                                exc_info=True,
                            )

                    # Reconcile option assignment/exercise/expiry events from the
                    # broker (no-op for non-options accounts). Idempotent.
                    self._reconcile_account_option_activities(account)

                    # SAFETY NET: a stop-LIMIT exit leg that a gap jumped can never fill,
                    # leaving an open position with no stop under it. Force-close those at
                    # market. Runs LAST, on the freshly-refreshed order/position/transaction
                    # state above. Idempotent, and isolated so a price/DB failure here
                    # cannot cost the rest of the refresh.
                    try:
                        self._force_close_breached_stops(account)
                    except Exception as e:
                        self.logger.error(
                            f"Error force-closing breached stops for {account_def.name}: {e}",
                            exc_info=True,
                        )

                    # A protective OCO whose TP leg PARTLY filled: the broker cancels the
                    # stop leg, leaving the remainder with a take-profit and no stop.
                    try:
                        self._reprotect_partially_filled_exits(account)
                    except Exception as e:
                        self.logger.error(
                            f"Error re-protecting partly filled exits for {account_def.name}: {e}",
                            exc_info=True,
                        )

                    # A CLOSING transaction whose close died at the broker with nothing
                    # left working: no other pass revisits CLOSING, so retry it here.
                    try:
                        self._retry_stuck_closes(account)
                    except Exception as e:
                        self.logger.error(
                            f"Error retrying stuck closes for {account_def.name}: {e}",
                            exc_info=True,
                        )

                    # ALLOCATOR TP/SL (TastyTrade): detect protective fills (-> the symbol is
                    # HELD), lost/expired/cancelled OCOs (-> loud alert) and size mismatches,
                    # and finish a re-placement a rebalance started. A no-op for every account
                    # that does not declare ``supports_allocator_protection``; reconcile never
                    # raises, and the try below keeps even a bug in it from costing the rest of
                    # the refresh.
                    try:
                        from .allocator_protection_service import reconcile_account, renew_expiring
                        reconcile_account(account)
                        # GTC renewal lives HERE (the account refresh), never on page load: orders
                        # with a broker-reported gtc_date within 7 days are cancelled (confirmed)
                        # and re-placed with the same prices, one symbol per cycle.
                        renew_expiring(account)
                    except Exception as e:
                        self.logger.error(
                            f"Error reconciling allocator TP/SL protection for {account_def.name}: {e}",
                            exc_info=True,
                        )

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

            # Step 4: Give up on WAITING transactions whose entry died at the broker
            # without filling. Runs LAST so it reads the order state Steps 1-3 have just
            # reconciled — in particular an entry the broker cancelled and a protective
            # leg a by-id lookup has just settled. Nothing else can fail such a
            # transaction, and a stranded WAITING permanently retires its symbol for its
            # expert (see the method's docstring and docs/WASHTRADE-LOCK.md).
            self._check_stranded_waiting_transactions()

            self.logger.info("Account refresh completed")

        except Exception as e:
            self.logger.error(f"Error during account refresh: {e}", exc_info=True)
        finally:
            _REFRESH_LOCK.release()
    
    # Reason stamped on a transaction the safety net closes, and the string an operator
    # greps for. Distinct from "tp_sl_filled": nothing filled — that is the whole problem.
    STOP_BREACH_CLOSE_REASON = "stop_breached_forced_close"

    def _force_close_breached_stops(self, account):
        """Force-close, at market, any position whose stop the market has passed without
        the broker's stop leg filling.

        LIVE-ONLY BY CONSTRUCTION. ``TradeManager`` runs only in the live platform; the
        backtest engine never calls this refresh path. It exists precisely to make live
        agree with the backtest:

        WHY. Until APP 2026.09.1261 Alpaca OCO exits had a stop-LIMIT stop leg, its limit
        ``OCO_STOP_LIMIT_CUSHION`` through the stop. When a gap cleared BOTH the stop and
        that cushion in one print, the triggered leg became a limit order on the wrong side
        of the market: it rested unfilled and the position stayed open with no stop beneath
        it (prod RARE/ENOV 2026-09-10, PLTR 2026-11-03). The backtest models every stop as a
        MARKET stop that fills at the stop or at the gap open
        (``backtest_account._gap_stop_fill``). New Alpaca stop legs are plain stops, so they
        match it; this sweep remains the backstop for legacy stop-limit legs still resting
        at the broker, for IBKR's stop-limit legs, and for a stop that is held or missing.

        THE RULE. For each OPENED **equity** transaction of ``account`` carrying a
        ``stop_loss``, breached means the price is past the stop by more than the cushion:
        a long below ``stop * (1 - cushion)``, a short above ``stop * (1 + cushion)``.
        Inside the cushion the triggered limit can still fill normally, so we leave it to
        the broker. Options are skipped: an option transaction's ``symbol`` is the
        UNDERLYING and its stop is not a share price.

        WHAT IT REFUSES TO DO. Nothing is closed on an assumption. A price that is
        missing, non-finite or non-positive is UNKNOWN, and unknown is not "safe" — the
        symbol is skipped with a WARNING, never closed and never presumed healthy. A
        position book that cannot be read is likewise a skip, not a close. Each
        transaction is handled independently: one failure logs and the loop continues.

        Runs ONLY while the broker reports the regular session open (step 0 below); an after-hours quote is not trusted.
        """
        from sqlmodel import Session, select
        from .db import get_db
        from .types import AssetClass, TransactionStatus

        # 0) ONLY WHILE THE MARKET IS OPEN (operator decision 2026-09-10). Outside regular
        # hours the latest quote can be a stale or one-sided after-hours print on a thin
        # name, and acting on it would queue a market sell for the open that no real trade
        # justified. Nothing is lost by waiting: the first refresh after the bell (at most
        # one interval later) still catches an overnight gap, and the open is exactly where
        # the backtest fills a gapped stop. ``is_market_open`` FAILS CLOSED (an unreadable
        # clock reads as not open), so a broken clock pauses the sweep, never the reverse.
        if not account.is_market_open():
            self.logger.debug(
                f"Breached-stop sweep for account {account.id}: market not open, skipped")
            return

        # 1) The watch list: OPENED equity transactions of THIS account (transactions link
        #    to an account only through their orders) that actually have a stop to breach.
        watched = []
        with Session(get_db().bind) as session:
            open_txns = session.exec(
                select(Transaction).join(TradingOrder).where(
                    TradingOrder.account_id == account.id,
                    Transaction.status == TransactionStatus.OPENED,
                ).distinct()
            ).all()
            for txn in open_txns:
                if txn.asset_class == AssetClass.OPTION or txn.multiplier not in (None, 1):
                    continue
                if txn.stop_loss is None or not txn.symbol:
                    continue
                # Plain tuples, so nothing below depends on a live ORM session.
                watched.append((txn.id, txn.symbol, txn.side, float(txn.stop_loss)))

        if not watched:
            return

        # 2) One bulk price call for every watched symbol.
        symbols = sorted({symbol for _, symbol, _, _ in watched})
        prices = account.get_instrument_current_price(symbols)
        if not isinstance(prices, dict):
            self.logger.error(
                f"[Account {account.id}] breached-stop check: bulk price fetch for "
                f"{len(symbols)} symbol(s) returned {type(prices).__name__}, not a mapping; "
                f"no position can be checked this pass"
            )
            return

        for txn_id, symbol, side, stop in watched:
            try:
                if symbol not in prices:
                    self.logger.warning(
                        f"[Account {account.id}] breached-stop check: no price returned for "
                        f"{symbol} (transaction {txn_id}); skipping — an unknown price is not "
                        f"a healthy position, it is an unchecked one"
                    )
                    continue
                raw_price = prices[symbol]
                if raw_price is None:
                    self.logger.warning(
                        f"[Account {account.id}] breached-stop check: price unavailable for "
                        f"{symbol} (transaction {txn_id}); skipping this pass"
                    )
                    continue
                price = float(raw_price)
                if not math.isfinite(price) or price <= 0:
                    self.logger.warning(
                        f"[Account {account.id}] breached-stop check: unusable price "
                        f"{raw_price!r} for {symbol} (transaction {txn_id}); skipping"
                    )
                    continue

                if side == OrderDirection.BUY:
                    threshold = stop * (1.0 - OCO_STOP_LIMIT_CUSHION)
                    breached = price < threshold
                elif side == OrderDirection.SELL:
                    threshold = stop * (1.0 + OCO_STOP_LIMIT_CUSHION)
                    breached = price > threshold
                else:
                    self.logger.warning(
                        f"[Account {account.id}] breached-stop check: transaction {txn_id} "
                        f"({symbol}) has an unrecognised side {side!r}; skipping"
                    )
                    continue

                if not breached:
                    continue

                # 3) NEVER close on a guess: the position must still be at the broker, on
                #    the side we think we hold. None means the book could not be read.
                held_qty = account.get_signed_position_quantity(symbol)
                if held_qty is None:
                    self.logger.warning(
                        f"[Account {account.id}] {symbol} stop {stop} appears breached at "
                        f"{price}, but the broker position book is UNREADABLE; not closing "
                        f"transaction {txn_id} on a guess"
                    )
                    continue
                still_held = (held_qty > 0) if side == OrderDirection.BUY else (held_qty < 0)
                if not still_held:
                    self.logger.warning(
                        f"[Account {account.id}] {symbol} stop {stop} appears breached at "
                        f"{price}, but the broker reports qty={held_qty} for a "
                        f"{side.value if hasattr(side, 'value') else side} position; nothing "
                        f"to close for transaction {txn_id} (the reconciler owns this case)"
                    )
                    continue

                # 4) Idempotency: close_transaction moves the row to CLOSING, so a later
                #    refresh finds it out of OPENED. Re-read anyway — the watch list was
                #    built before the price and position round-trips above.
                txn = get_instance(Transaction, txn_id)
                if txn.status != TransactionStatus.OPENED:
                    continue

                self.logger.warning(
                    f"[Account {account.id}] STOP BREACHED WITHOUT FILLING: {symbol} "
                    f"{side.value if hasattr(side, 'value') else side} transaction {txn_id}, "
                    f"stop={stop}, price={price}, cushion={OCO_STOP_LIMIT_CUSHION:.3%}. The "
                    f"stop exit did not fill — force-closing at market."
                )

                result = account.close_transaction(txn_id)
                if not isinstance(result, dict):
                    raise ValueError(
                        f"close_transaction({txn_id}) returned {type(result).__name__}, "
                        f"not a result dict"
                    )
                success = bool(result['success'])

                txn = get_instance(Transaction, txn_id)
                if success:
                    # Stamp WHY, so the closure is attributable once the market close fills.
                    txn.close_reason = self.STOP_BREACH_CLOSE_REASON
                    update_instance(txn)
                else:
                    self.logger.error(
                        f"[Account {account.id}] force-close of breached-stop transaction "
                        f"{txn_id} ({symbol}) FAILED: {result['message']}. The position is "
                        f"still open and unprotected — it will be retried next refresh."
                    )

                # Same activity-log helper every other close path uses.
                from ba2_common.core.utils import log_close_order_activity
                log_close_order_activity(
                    transaction=txn,
                    account_id=account.id,
                    success=success,
                    error_message=None if success else result['message'],
                    close_order_id=result['close_order_id'],
                    quantity=txn.quantity,
                    side=(OrderDirection.SELL if side == OrderDirection.BUY
                          else OrderDirection.BUY),
                    canceled_count=result['canceled_count'],
                    deleted_count=result['deleted_count'],
                )

            except Exception as e:
                # One bad transaction must never cost the others their safety net.
                self.logger.error(
                    f"[Account {account.id}] breached-stop check failed for transaction "
                    f"{txn_id} ({symbol}): {e}",
                    exc_info=True,
                )
                continue

    def _equity_transactions_of(self, account, status):
        """``account``'s equity transactions in ``status``, as detached rows.

        Transactions link to an account only through their orders (same join as
        ``_force_close_breached_stops``). Options are skipped: their ``symbol`` is the
        UNDERLYING and they have their own close path."""
        from sqlmodel import Session, select
        from .db import get_db
        from .types import AssetClass

        with Session(get_db().bind) as session:
            txns = session.exec(
                select(Transaction).join(TradingOrder).where(
                    TradingOrder.account_id == account.id,
                    Transaction.status == status,
                ).distinct()
            ).all()
            out = [t for t in txns
                   if t.asset_class != AssetClass.OPTION and t.multiplier in (None, 1)
                   and t.symbol]
            for t in out:
                session.expunge(t)
        return out

    def _reprotect_partially_filled_exits(self, account):
        """Re-protect a position whose protective OCO partly filled and lost its stop leg.

        WHY. When the take-profit side of an Alpaca OCO fills only PART of the position, the
        broker cancels the stop leg and keeps the take-profit working for the remainder (dev
        8080 CELH, transaction 72: 29 of 52 sold 2026-09-28, the other 23 then rode with no
        stop for six weeks). Nothing noticed: the order refresh records the leg as CANCELED
        and the parent as PARTIALLY_FILLED, both "normal" states.

        THE RULE. For each OPENED equity transaction with a ``stop_loss``: an OCO on it that
        is PARTIALLY_FILLED, whose stop leg(s) (``parent_order_id`` = the OCO) are all
        terminal and unfilled -- or that has no leg row at all -- and that no staged
        replacement (WAITING_TRIGGER, ``depends_on_order`` = the OCO) is already waiting on,
        is replaced through ``account.adjust_tp_sl`` at the transaction's own TP/SL. That
        path cancels the working OCO and stages the new exit for the remainder on the
        cancel (sized by ``TransactionHelper.post_fill_position_quantity``). A refusal is
        logged as an ERROR and in the activity log; it is retried next refresh.

        Live-only (TradeManager is never called by the backtest, which fills exits whole).
        """
        from ba2_common.core.trade_store import orders_where
        from .types import TransactionStatus, ActivityLogSeverity, ActivityLogType
        from .db import log_activity

        terminal = OrderStatus.get_terminal_statuses()
        for txn in self._equity_transactions_of(account, TransactionStatus.OPENED):
            if txn.stop_loss is None:
                continue
            try:
                orders = orders_where(transaction_id=txn.id)
                stranded = []
                for oco in orders:
                    if (oco.order_type != OrderType.OCO
                            or oco.status != OrderStatus.PARTIALLY_FILLED):
                        continue
                    legs = [o for o in orders if o.parent_order_id == oco.id]
                    stop_alive = any(o.status not in terminal or o.status == OrderStatus.FILLED
                                     for o in legs)
                    staged = any(o.depends_on_order == oco.id
                                 and o.status == OrderStatus.WAITING_TRIGGER for o in orders)
                    if not stop_alive and not staged:
                        stranded.append(oco)
                if not stranded:
                    continue

                ids = [o.id for o in stranded]
                self.logger.warning(
                    f"[Account {account.id}] {txn.symbol} transaction {txn.id}: protective "
                    f"OCO {ids} partly filled and its stop leg is gone -- the remainder has no "
                    f"stop. Replacing the exit at TP={txn.take_profit} SL={txn.stop_loss}.")
                ok = account.adjust_tp_sl(txn, txn.take_profit, txn.stop_loss,
                                          source="partial_fill_reprotect")
                if not ok:
                    msg = (f"Re-protecting {txn.symbol} transaction {txn.id} after partly "
                           f"filled OCO {ids} FAILED: the remainder still has no stop. "
                           f"Retried next refresh.")
                    self.logger.error(f"[Account {account.id}] {msg}")
                    log_activity(ActivityLogSeverity.FAILURE, ActivityLogType.TP_SL_ADJUSTED,
                                 msg, data={"transaction_id": txn.id, "symbol": txn.symbol,
                                            "oco_order_ids": ids},
                                 source_account_id=account.id)
            except Exception as e:
                self.logger.error(
                    f"[Account {account.id}] partly-filled-exit check failed for transaction "
                    f"{txn.id} ({txn.symbol}): {e}", exc_info=True)
                continue

    def _retry_stuck_closes(self, account, *, now=None):
        """Retry the close of a CLOSING transaction that has nothing left working.

        WHY. ``close_transaction`` flips a transaction to CLOSING before it submits the close.
        If the broker then rejects or cancels that close, the transaction stays CLOSING and no
        pass ever looks at it again: every exit pass, the breached-stop safety net included,
        scans OPENED only; the external-close reconciler closes it only once the broker holds
        no shares of the symbol at all. Dev 8080 PRG, transaction 258: the stop-breach forced
        close of 2026-10-28 was refused (insufficient qty) and the last share sat unprotected
        for 11 days, while the safety net's own log said "it will be retried next refresh".

        THE RULE. While the market is open, for each CLOSING equity transaction of
        ``account`` with NO order still working (no active or unsent status) and whose newest
        order is at least ``_STUCK_CLOSE_RETRY_MINUTES`` old, call
        ``account.close_transaction`` again. It is the one close path: it re-checks the broker
        position, retires a dead close order and sizes the new one on the open quantity. A
        failed retry is an ERROR and an activity-log FAILURE, and is retried after the same
        wait.
        """
        from datetime import timedelta
        from ba2_common.core.trade_store import orders_where
        from ba2_common.core.utils import as_utc_key, log_close_order_activity
        from .types import TransactionStatus

        # Same gate as the breached-stop sweep: a market close sent after hours would queue
        # for the open on a decision nobody re-checked.
        if not account.is_market_open():
            return

        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=_STUCK_CLOSE_RETRY_MINUTES)
        working = OrderStatus.get_active_statuses() | OrderStatus.get_unsent_statuses()
        for txn in self._equity_transactions_of(account, TransactionStatus.CLOSING):
            try:
                orders = orders_where(transaction_id=txn.id)
                if not orders or any(o.status in working for o in orders):
                    continue
                newest = max(as_utc_key(o.created_at, default=now) for o in orders)
                if newest > cutoff:
                    continue

                self.logger.warning(
                    f"[Account {account.id}] {txn.symbol} transaction {txn.id} is CLOSING with "
                    f"no order working since {newest:%Y-%m-%d %H:%M} UTC -- its close died at "
                    f"the broker. Retrying the close.")
                result = account.close_transaction(txn.id)
                if not isinstance(result, dict):
                    raise ValueError(f"close_transaction({txn.id}) returned "
                                     f"{type(result).__name__}, not a result dict")
                success = bool(result['success'])
                if not success:
                    self.logger.error(
                        f"[Account {account.id}] retrying the close of {txn.symbol} "
                        f"transaction {txn.id} FAILED: {result['message']}. Retried again "
                        f"in {_STUCK_CLOSE_RETRY_MINUTES:g} min.")
                log_close_order_activity(
                    transaction=txn, account_id=account.id, success=success,
                    error_message=None if success else result['message'],
                    close_order_id=result['close_order_id'],
                    canceled_count=result['canceled_count'],
                    deleted_count=result['deleted_count'], is_retry=True)
            except Exception as e:
                self.logger.error(
                    f"[Account {account.id}] stuck-close retry failed for transaction "
                    f"{txn.id} ({txn.symbol}): {e}", exc_info=True)
                continue

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
            if activities:
                results = account.reconcile_option_assignments(activities)
                applied = [r for r in (results or [])
                           if not str(r.get("result", "")).startswith(
                               ("already_processed", "skipped"))]
                self.logger.info(
                    f"[Account {account.id}] Option reconciliation: {len(activities)} "
                    f"activity(ies), {len(applied)} newly applied"
                )
        except Exception as e:
            self.logger.error(
                f"[Account {getattr(account, 'id', '?')}] Option reconciliation failed: {e}",
                exc_info=True
            )
        # The live twin of the backtest's next-bar assignment liquidation: sell the stock lots
        # the reconciler flagged "not held" (assignment_hold). Runs on EVERY refresh, not only
        # when new activities arrived, because a refused/failed sale stays pending and is retried
        # here. Its own try: an activity-feed failure must not stop a sale already owed.
        if hasattr(account, "process_pending_assignment_liquidations"):
            try:
                account.process_pending_assignment_liquidations()
            except Exception as e:
                self.logger.error(
                    f"[Account {getattr(account, 'id', '?')}] assignment liquidation sweep "
                    f"failed: {e}", exc_info=True)

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

                is_closing = self._is_closing_order(order)

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
                    sl_price = entry_submit_stop(order, order.stop_price or None)
                account.submit_order(order, sl_price=sl_price, is_closing_order=is_closing)
            except Exception as e:
                self.logger.error(f"Error processing WASHTRADE_LOCKED order {order_id}: {e}", exc_info=True)

    @staticmethod
    def _is_closing_order(order) -> bool:
        """Does this order REDUCE a position? One answer, every re-submit path.

        ``is_closing_order`` is what tells ``AccountInterface`` to skip the budget checks
        that only make sense for spending capital -- the expert's available balance and
        the account's stock-exposure ceiling. Getting it wrong on a REDUCTION is not a
        missed optimisation: a fully invested expert (or, with margin on, an account past
        ``balance x margin_factor``, whose headroom is negative) refuses every quantity,
        so the order that would have de-risked the book is the one that cannot be placed.

        Two derivations used to live inline and disagreed:
          * the wash-trade retry read only the transaction, so a transaction-LESS close
            (``portfolio_allocation_service._sell_untracked_symbol``) came back False;
          * the dependent-order path read only ``MARKET`` + ``'closing' in comment``,
            which does not match ``TransactionHelper``'s "Partial close order (triggered
            by TP/SL cancel)" -- a trim that shrinks a position was validated as an open.

        This is the union; both ``'close'`` and ``'closing'`` must be checked:

          1. the order has a transaction whose side is OPPOSITE to its own -> closing.
             ``Transaction.side`` is the direction the POSITION points, the same field
             ``_validate_account_exposure`` and ``_pending_stock_entry_notional`` read;
             a same-side order adds exposure regardless of its comment;
          2. else a MARKET order whose comment says ``close`` or ``closing`` (the platform's own close
             paths write it) -> closing;
          3. else not closing. An order that opens is the default, because mislabelling
             an OPEN as a close would skip the risk checks entirely.

        A transaction-less closing MARKET order still passes the account exposure gate
        without this: the gate reads the broker's position book for such orders. This
        flag is what keeps the EXPERT's available-balance check off it as well.
        """
        comment = (order.comment or '').lower()
        if order.transaction_id:
            txn = get_instance(Transaction, order.transaction_id)
            if txn and txn.side != order.side:
                logger.debug(
                    f"Order {order.id} ({order.symbol} {order.side}) is CLOSING: "
                    f"transaction {txn.id} side {txn.side} is the opposite side.")
                return True
            if txn:
                return False  # A comment cannot override a known exposure increase.
        if order.order_type == OrderType.MARKET and ('close' in comment or 'closing' in comment):
            logger.debug(
                f"Order {order.id} ({order.symbol} {order.side}) is CLOSING: MARKET order "
                f"whose comment says close ({order.comment!r}).")
            return True
        logger.debug(
            f"Order {order.id} ({order.symbol} {order.side}) is NOT closing: no "
            f"opposite-side transaction and no close comment on a MARKET order.")
        return False

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
        order_id = getattr(order, 'id', None)
        if not order_id:
            self.logger.error(f"Cannot compensate an unsaved entry order ({reason})")
            return False

        on_disk = row_or_none(TradingOrder, order_id)
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

        self._cancel_waiting_trigger_dependents(order_id)

        # The transaction id is read from the IN-MEMORY object first: submit_order creates the
        # Transaction and stamps trading_order.transaction_id, then persists it with a SEPARATE
        # update_instance. When that update is what failed, the row on disk has no
        # transaction_id at all while a WAITING transaction very much exists — and that orphan
        # is precisely what would keep blocking the symbol.
        transaction_id = getattr(order, 'transaction_id', None) or on_disk.transaction_id
        self._fail_waiting_transaction(
            transaction_id,
            f"its entry order {order_id} never reached the broker",
        )
        return True

    def _cancel_waiting_trigger_dependents(self, order_id: int) -> int:
        """Cancel every WAITING_TRIGGER order chained on ``order_id``.

        Those legs wait on a parent status that will now never arrive, so leaving them
        behind only moves the leak. WAITING_TRIGGER is an unsent status — the row exists
        only in this database — so this can never hide a live broker order.
        """
        from sqlmodel import select
        from .db import get_db

        with get_db() as session:
            dependents = session.exec(
                select(TradingOrder).where(
                    TradingOrder.depends_on_order == order_id,
                    TradingOrder.status == OrderStatus.WAITING_TRIGGER,
                )
            ).all()
            dependent_ids = [d.id for d in dependents]
        cancelled = 0
        for dep_id in dependent_ids:
            dep = row_or_none(TradingOrder, dep_id)
            if dep and dep.status == OrderStatus.WAITING_TRIGGER:
                dep.status = OrderStatus.CANCELED
                update_instance(dep)
                cancelled += 1
                self.logger.info(f"Cancelled protective leg {dep_id} of dead entry {order_id}")
        return cancelled

    def _fail_waiting_transaction(self, transaction_id, reason: str) -> bool:
        """Mark a WAITING transaction FAILED. THE single give-up transition.

        WHY IT IS SHARED. Every way of giving up on an entry leaves the same expensive
        debris: the Transaction that ``submit_order`` created in WAITING before anything
        went wrong. The enter_market SAFETY CHECK refuses any symbol+expert that already
        has an OPENED or WAITING transaction, and nothing sweeps a stranded WAITING — so
        one lost entry retires that symbol for that expert permanently. Measured on PROD
        (CVS 2026-08-10, WSC 2026-08-24, both unsent) and on dev (SHOP/UNH 2026-09-14,
        NVDA/AVGO 2026-09-21, all four SENT and then cancelled by the broker).

        Only a WAITING transaction is failed. An OPENED one has filled orders behind it
        and a real position at the broker; the dead order was an add-on, not the position.
        """
        from .types import TransactionStatus

        if not transaction_id:
            return False
        txn = row_or_none(Transaction, transaction_id)
        if not txn or txn.status != TransactionStatus.WAITING:
            return False
        txn.status = TransactionStatus.FAILED
        update_instance(txn)
        self.logger.warning(
            f"Transaction {txn.id} ({txn.symbol}) marked FAILED — {reason}; leaving it "
            f"WAITING would block every future entry for this symbol+expert"
        )
        return True

    def _settle_orphaned_children(self, account, txn_id: int, entry_ids) -> None:
        """Leave nothing non-terminal behind a dead entry.

        Everything on transaction ``txn_id`` that is not one of ``entry_ids`` is a
        protective leg (or an OCO wrapper) that exists only to guard a position the entry
        never opened. Three shapes, three different correct actions — and the difference
        is exactly the one ``_fail_unsent_entry`` refuses to guess at:

        * **unsent** (PENDING / WAITING_TRIGGER / WASHTRADE_LOCKED, no broker id) — the
          row exists only here, so write CANCELED directly.
        * **working at the broker** (has a broker id, not terminal, not already
          PENDING_CANCEL) — ask the BROKER to cancel it. ``cancel_order`` sets
          PENDING_CANCEL, and the by-id reconciliation promotes that to the broker's real
          answer. Writing CANCELED here instead would hide a live order.
        * **already PENDING_CANCEL** — the cancel is in flight; the by-id reconciliation
          owns it. Say so and leave it alone.

        A row that claims an unsent status while carrying a broker id is a contradiction;
        it is reported and left untouched rather than resolved by assumption.
        """
        from sqlmodel import select
        from .db import get_db

        unsent = OrderStatus.get_unsent_statuses()
        terminal = OrderStatus.get_terminal_statuses()
        with get_db() as session:
            children = session.exec(
                select(TradingOrder).where(TradingOrder.transaction_id == txn_id)
            ).all()
            child_ids = [c.id for c in children if c.id not in entry_ids]

        for child_id in child_ids:
            child = row_or_none(TradingOrder, child_id)
            if child is None or child.status in terminal or child.status == OrderStatus.FILLED:
                continue
            broker_id = getattr(child, 'broker_order_id', None)
            if child.status in unsent:
                if broker_id:
                    self.logger.error(
                        f"Order {child_id} ({child.symbol}) claims unsent status "
                        f"{child.status} but carries broker id {broker_id} — REFUSING to "
                        f"touch it; a row that contradicts itself must be looked at, not "
                        f"resolved by assumption"
                    )
                    continue
                child.status = OrderStatus.CANCELED
                update_instance(child)
                self.logger.info(
                    f"Cancelled orphaned protective order {child_id} ({child.symbol} "
                    f"{child.order_type.value}) of failed transaction {txn_id}")
                continue
            if child.status == OrderStatus.PENDING_CANCEL:
                self.logger.info(
                    f"Orphaned protective order {child_id} ({child.symbol}) of failed "
                    f"transaction {txn_id} is already PENDING_CANCEL — the by-id "
                    f"reconciliation will settle it against the broker")
                continue
            if not broker_id:
                self.logger.error(
                    f"Order {child_id} ({child.symbol}) is {child.status} with NO broker id "
                    f"on failed transaction {txn_id} — cannot cancel it at the broker and "
                    f"cannot safely cancel it here; leaving it for an operator")
                continue
            if account is None:
                self.logger.error(
                    f"Order {child_id} ({child.symbol}) is working at the broker "
                    f"({broker_id}) on failed transaction {txn_id}, but its account could "
                    f"not be instantiated — the protective leg is left working")
                continue
            self.logger.warning(
                f"Cancelling orphaned protective order {child_id} ({child.symbol} "
                f"{child.order_type.value}, {child.status}) at the broker: its entry never "
                f"opened a position on transaction {txn_id}")
            account.cancel_order(child_id)

    def _fail_terminal_unfilled_entry(self, entry, txn, account, reason: str) -> bool:
        """Give up on an entry the BROKER killed without filling it.

        SIBLING OF ``_fail_unsent_entry``, same rationale, opposite precondition. That one
        compensates an entry that never reached the broker and therefore refuses anything
        carrying a ``broker_order_id``. This one handles precisely the case it refuses: an
        entry that DID reach the broker, was accepted, and was then CANCELED / REJECTED /
        EXPIRED there with zero filled quantity. Both converge on the same give-up
        transition (``_fail_waiting_transaction``) because the debris is identical.

        SAFETY — WHY THIS CAN NEVER DISCARD A REAL POSITION. A partial fill IS a position,
        and failing its transaction would make the platform forget shares it owns. So the
        compensation is refused unless the in-memory object AND the row on disk BOTH agree
        that the order is in a terminal status and that its filled quantity is zero. The
        order's own status is left exactly as the broker set it — nothing here rewrites
        broker-reported state.

        Returns True when it failed the transaction, False when it refused.
        """
        from .types import TransactionStatus

        entry_id = getattr(entry, 'id', None)
        if not entry_id:
            self.logger.error(f"Cannot give up on an unsaved entry order ({reason})")
            return False

        on_disk = row_or_none(TradingOrder, entry_id)
        if on_disk is None:
            self.logger.warning(
                f"Order {entry_id} no longer exists — nothing to give up on ({reason})")
            return False

        terminal = OrderStatus.get_terminal_statuses()
        for view, where in ((entry, "in memory"), (on_disk, "on disk")):
            if view.status not in terminal:
                self.logger.error(
                    f"REFUSING to fail transaction {txn.id} on order {entry_id} "
                    f"({entry.symbol}): status is {view.status} ({where}), which is not a "
                    f"terminal status — the order may still fill. Reason was: {reason}")
                return False
            # Mirrors classify_waiting_entry exactly, so the classifier and the actor can
            # never disagree about what "unfilled" means.
            filled = getattr(view, 'filled_qty', None)
            if filled is None:
                if getattr(view, 'broker_order_id', None):
                    self.logger.error(
                        f"REFUSING to fail transaction {txn.id} on order {entry_id} "
                        f"({entry.symbol}): filled_qty is NULL ({where}) on an order the "
                        f"broker has seen, so how much traded is UNKNOWN — and unknown is "
                        f"not zero. Reason was: {reason}")
                    return False
            elif float(filled) > 0:
                self.logger.error(
                    f"REFUSING to fail transaction {txn.id} on order {entry_id} "
                    f"({entry.symbol}): filled_qty is {filled} ({where}), so shares really "
                    f"traded — failing it would make the platform forget a real position. "
                    f"Reason was: {reason}")
                return False

        # FIX 3's half of the wash-trade story. The entry took the complex-order exemption
        # (stamped at submit time) and the broker killed it anyway — so the exemption did
        # not hold against this blocker. Record it on the order before the give-up, so the
        # rejection is diagnosable long after the logs roll.
        if went_out_as_contended_complex(entry) and not was_rejected_as_washtrade(on_disk):
            record = stamp_rejection(on_disk, on_disk.status)
            update_instance(on_disk)
            self.logger.error(
                f"WASH-TRADE REJECTION CONFIRMED: order {entry_id} ({entry.symbol} "
                f"{entry.side.value}) was submitted as a COMPLEX order to escape blocker "
                f"{record['blocker_order_id']} ({record['blocker_order_type']}) and the "
                f"broker still ended it {record['final_status']} with zero fill. The "
                f"bracket/OCO exemption does NOT hold against this blocker shape — see the "
                f"2026-09-22 section of docs/WASHTRADE-LOCK.md"
            )
            reason = f"{reason}; the complex-order wash-trade exemption did not hold"

        self._settle_orphaned_children(account, txn.id, {entry_id})
        self._cancel_waiting_trigger_dependents(entry_id)
        failed = self._fail_waiting_transaction(txn.id, reason)
        if not failed:
            current = row_or_none(Transaction, txn.id)
            self.logger.info(
                f"Transaction {txn.id} was no longer WAITING when the give-up path reached "
                f"it (now {current.status if current else 'gone'})")
        return failed

    def _check_stranded_waiting_transactions(self):
        """Fail every WAITING transaction whose entry died at the broker without filling.

        THE HOLE THIS FILLS. ``_fail_unsent_entry`` is the only other path to
        ``TransactionStatus.FAILED`` and it covers exactly one story — "the entry never
        reached the broker". An entry that DID reach the broker and was cancelled there
        unfilled matches nothing, so its transaction stays WAITING forever and the
        enter_market safety check retires that symbol for that expert. Four such
        transactions were measured on dev 2026-09-22 (SHOP/UNH since 09-14, NVDA/AVGO
        since 09-21, all expert 2 / account 1); all four entries were accepted by Alpaca
        and cancelled by it within ~100 ms, filled 0, blocked by another expert's resting
        protective stop. See docs/WASHTRADE-LOCK.md.

        Runs after the broker re-sync so every order status it reads is fresh.
        """
        from sqlmodel import select
        from .db import get_db
        from ..modules.accounts import get_account_class
        from .models import AccountDefinition
        from .types import TransactionStatus

        with get_db() as session:
            waiting_ids = [
                t.id for t in session.exec(
                    select(Transaction).where(Transaction.status == TransactionStatus.WAITING)
                ).all()
            ]

        if not waiting_ids:
            self.logger.debug("No WAITING transactions to check for a dead entry")
            return

        self.logger.debug(f"Checking {len(waiting_ids)} WAITING transaction(s) for a dead entry")
        account_cache: Dict[int, Any] = {}

        for txn_id in waiting_ids:
            try:
                txn = row_or_none(Transaction, txn_id)
                if txn is None or txn.status != TransactionStatus.WAITING:
                    continue  # changed since we listed it

                # The ENTRY orders are the top-level orders pointing the same way as the
                # position. Protective legs and the OCO wrapper are on the OPPOSITE side,
                # which is what keeps them out of this classification; ``parent_order_id``
                # then excludes the LEG rows of a composite order, whose fate belongs to
                # their parent. (A multi-leg OPTION structure's net parent carries no side
                # at all, so no entry matches and such a transaction is simply left alone
                # — deliberately conservative: this sweep is for the equity entry that the
                # broker cancelled.)
                with get_db() as session:
                    entries = session.exec(
                        select(TradingOrder).where(
                            TradingOrder.transaction_id == txn_id,
                            TradingOrder.side == txn.side,
                            TradingOrder.parent_order_id.is_(None),
                        ).order_by(TradingOrder.created_at.asc())
                    ).all()
                    entry_rows = [
                        (e.id, e.account_id, e.created_at) for e in entries
                    ]
                    verdict = classify_waiting_entry(entries)

                if verdict == "wait":
                    continue

                # GRACE PERIOD. Mirrors the 5-minute floor AlpacaAccount's refresh already
                # applies before concluding anything about a freshly submitted order: a
                # transaction and its entry are written by two separate statements, and a
                # verdict taken between them would act on a half-written state.
                youngest = self._youngest_age_minutes(entry_rows)
                if youngest is not None and youngest < _STRANDED_ENTRY_GRACE_MINUTES:
                    self.logger.debug(
                        f"Transaction {txn_id} ({txn.symbol}) reads {verdict} but its "
                        f"newest entry is only {youngest:.1f} min old — leaving it")
                    continue

                if verdict == "filled":
                    self.logger.warning(
                        f"Transaction {txn_id} ({txn.symbol}) is WAITING with a TERMINAL "
                        f"entry that reports a fill — this is a real position, NOT failed "
                        f"here. Partial-fill reconciliation owns it "
                        f"(TransactionHelper.reconcile_canceled_partial_fill)")
                    continue

                if verdict == "ambiguous":
                    self.logger.error(
                        f"Transaction {txn_id} ({txn.symbol}) is WAITING with a terminal "
                        f"entry whose filled_qty is 0 but which carries a fill PRICE. The "
                        f"two witnesses disagree about whether anything traded, so nothing "
                        f"is decided here — an operator must reconcile it against the broker")
                    continue

                # verdict == "dead"
                entry_id, account_id, _ = entry_rows[-1]
                entry = row_or_none(TradingOrder, entry_id)
                if entry is None:
                    continue
                account = account_cache.get(account_id)
                if account is None:
                    account_def = row_or_none(AccountDefinition, account_id)
                    account_class = (
                        get_account_class(account_def.provider) if account_def else None)
                    if account_class is None:
                        self.logger.error(
                            f"No account class for order {entry_id}'s account {account_id}; "
                            f"a broker-side protective leg cannot be cancelled this pass")
                    else:
                        account = account_class(account_def.id)
                        account_cache[account_id] = account

                self._fail_terminal_unfilled_entry(
                    entry, txn, account,
                    f"its entry order {entry_id} reached the broker and ended "
                    f"{entry.status.value if hasattr(entry.status, 'value') else entry.status} "
                    f"with zero filled quantity",
                )
            except Exception as e:
                self.logger.error(
                    f"Error checking WAITING transaction {txn_id}: {e}", exc_info=True)

    @staticmethod
    def _youngest_age_minutes(rows):
        """Age in minutes of the most RECENTLY created row, or None when none is dated.

        A missing ``created_at`` must not read as age 0 (that would disable the grace
        period silently); callers skip the check on None, exactly as
        ``_washtrade_lock_age_hours`` does.
        """
        ages = []
        for row in rows:
            created_at = row[-1]
            if not created_at:
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            ages.append((datetime.now(timezone.utc) - created_at).total_seconds() / 60.0)
        return min(ages) if ages else None

    # Transient DB-lock retries for a FUNDED entry. See _submit_funded_entry_with_retry.
    _ENTRY_SUBMIT_RETRIES = 3
    _ENTRY_SUBMIT_BACKOFF_S = 2.0

    def _submit_funded_entry_with_retry(self, account, order, sl_price=None):
        """Submit a funded entry, retrying when the DB (not the broker) is what failed
        (``trade_cycle.submit_funded_entry``: shared with the offline decision replay, whose
        docstring carries the history -- CVS 2026-08-10, order 460)."""
        return submit_funded_entry(account, order, sl_price, retries=self._ENTRY_SUBMIT_RETRIES,
                                   backoff_s=self._ENTRY_SUBMIT_BACKOFF_S, log=self.logger)

    def _rechain_behind_entry_legs(self, account, dependent_order, parent_order_id) -> bool:
        """Chain an exit that fires on its ENTRY's fill behind the cancellation of that entry's
        own live OTO/bracket legs (the wash-trade escape path), and cancel those legs.

        Submitted while the legs still hold the shares, the exit is refused (2026-10-22 DY /
        APLD: 40310000 insufficient qty) and the position keeps only the leg. Returns True when
        the exit was re-chained (it then waits for the newest leg's CANCELED, the ordinary
        cancel-and-replace path, qty gate included)."""
        trig = getattr(dependent_order.depends_order_status_trigger, "value",
                       dependent_order.depends_order_status_trigger)
        if str(trig).lower() != OrderStatus.FILLED.value:
            return False
        from sqlmodel import select
        from .db import get_db
        terminal = OrderStatus.get_terminal_statuses()
        with get_db() as session:
            legs = [o for o in session.exec(select(TradingOrder).where(
                        TradingOrder.parent_order_id == parent_order_id,
                        TradingOrder.broker_order_id.isnot(None))).all()
                    if o.status not in terminal and o.id != dependent_order.id]
        if not legs:
            return False
        legs.sort(key=lambda o: o.id, reverse=True)
        dependent_order.depends_on_order = legs[0].id
        dependent_order.depends_order_status_trigger = OrderStatus.CANCELED
        dependent_order.comment = (f"{dependent_order.comment or ''} "
                                   f"(chained on cancel of entry leg {legs[0].id})")
        update_instance(dependent_order)
        failed = []
        for leg in legs:
            try:
                ok = account.cancel_order(leg.id)
            except Exception as e:  # noqa: BLE001 -- reported below
                ok = False
                self.logger.error(f"Failed to cancel entry leg {leg.id}: {e}", exc_info=True)
            if not ok:
                failed.append(leg.id)
        if failed:
            self.logger.error(
                f"Exit {dependent_order.id} ({dependent_order.symbol}) waits on entry leg "
                f"{legs[0].id}, but the broker did not accept the cancel of leg(s) {failed}: "
                f"the leg keeps protecting the position and the exit is not submitted")
        else:
            self.logger.info(
                f"Exit {dependent_order.id} ({dependent_order.symbol}) chained on the cancel of "
                f"entry {parent_order_id}'s leg(s) {[leg.id for leg in legs]}, which hold the shares")
        return True

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
                        action = classify_waiting_trigger(current_status, trigger_status,
                                                          parent_order.filled_qty)
                        if action == "submit" and current_status != trigger_status \
                                and current_status != OrderStatus.FILLED:
                            # Partial fill, then cancelled: protect what filled, not what was ordered.
                            self.logger.warning(
                                f"Parent order {parent_order_id} ended {current_status} after filling "
                                f"{parent_order.filled_qty} of {parent_order.quantity}; submitting dependent "
                                f"order {dependent_order.id} for the filled {parent_order.filled_qty} "
                                f"(was {dependent_order.quantity})")
                            dependent_order.quantity = parent_order.filled_qty
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
                            
                            # ===== Fill-time adjustment of the protective levels =====
                            # The rules (stop re-based to the real fill, take-profit minimum-distance
                            # floor from the real fill, legacy percent recalc) live in ONE pure
                            # function shared with the backtest: ba2_common.core.tpsl_fill_rebase.
                            # This block only gathers its inputs, calls it, and writes the result to
                            # the order row / Transaction.
                            transaction_updated = False
                            try:
                                fill_px = parent_order.open_price
                                data_dict = dependent_order.data if isinstance(dependent_order.data, dict) else None
                                if fill_px:
                                    # -- reference anchor for the stop (recorded when the exit order was built)
                                    ref_val = None
                                    ref_raw = data_dict.get("tpsl_reference_price") if data_dict else None
                                    # The price the stop was actually COMPUTED FROM, stamped on the
                                    # transaction when the stop was set, beats the anchor recorded on
                                    # the exit order at build time (which a later stop update never
                                    # refreshed) and the legacy reference chain behind it.
                                    anchor_txn = (session.get(Transaction, dependent_order.transaction_id)
                                                  if dependent_order.transaction_id else None)
                                    stamped_anchor = read_anchor(
                                        anchor_txn.meta_data if anchor_txn else None, "stop")
                                    if stamped_anchor:
                                        ref_raw = stamped_anchor
                                    if ref_raw:
                                        try:
                                            ref_val = float(ref_raw)
                                        except (TypeError, ValueError) as rebase_err:
                                            self.logger.warning(
                                                f"Could not re-base SL for order {dependent_order.id}: {rebase_err}")
                                    rebase_stop = bool(ref_val is not None and ref_val > 0)
                                    if dependent_order.stop_price and not rebase_stop:
                                        self.logger.warning(
                                            f"SL of order {dependent_order.id} NOT re-based to the parent fill: "
                                            f"no usable tpsl_reference_price on the order "
                                            f"(data value {ref_raw!r}); the stop keeps its pre-fill level")
                                    # -- take-profit floor inputs
                                    side_str = str(parent_order.side.value if hasattr(parent_order.side, "value")
                                                   else parent_order.side).upper()
                                    is_long = side_str == "BUY"
                                    apply_floor = bool(data_dict is not None and "tp_percent_target" in data_dict
                                                       and dependent_order.limit_price)
                                    min_pct = None
                                    if apply_floor:
                                        from ba2_common.core.TradeActions import resolve_min_take_profit_pct
                                        min_pct = resolve_min_take_profit_pct(parent_order.expert_recommendation_id)
                                    # -- legacy percent fields
                                    legacy_tp = legacy_sl = None
                                    tpsl_data = data_dict.get("TP_SL") if data_dict else None
                                    if isinstance(tpsl_data, dict):
                                        try:
                                            if "tp_percent" in tpsl_data:
                                                legacy_tp = float(tpsl_data.get("tp_percent"))
                                            elif "sl_percent" in tpsl_data:
                                                legacy_sl = float(tpsl_data.get("sl_percent"))
                                        except (KeyError, TypeError, ValueError) as data_error:
                                            self.logger.warning(
                                                f"Could not recalculate TP/SL price for order {dependent_order.id} "
                                                f"from data field: {data_error}")
                                    adj = rebase_levels_at_fill(
                                        is_long=is_long, fill_price=fill_px, reference_price=ref_val,
                                        take_profit=dependent_order.limit_price, stop_loss=dependent_order.stop_price,
                                        rebase_stop=rebase_stop, apply_tp_floor=apply_floor,
                                        min_take_profit_pct=min_pct,
                                        legacy_tp_percent=legacy_tp, legacy_sl_percent=legacy_sl)
                                    txn = None
                                    if (adj.stop_rebased or adj.tp_floored) and dependent_order.transaction_id:
                                        txn = session.get(Transaction, dependent_order.transaction_id)

                                    if adj.stop_rebased:
                                        old_sl = dependent_order.stop_price
                                        new_sl = adj.stop_loss
                                        dependent_order.stop_price = new_sl
                                        self.logger.info(
                                            f"Re-based SL for order {dependent_order.id} to parent fill: "
                                            f"${old_sl:.4f} -> ${new_sl:.4f} "
                                            f"(ref ${ref_val:.4f}, fill ${fill_px:.4f})"
                                        )
                                        if txn:
                                            txn.stop_loss = new_sl
                                            # measured from the fill now: a later pass finds nothing to re-base
                                            txn.meta_data = stamp_anchor(txn.meta_data, stop=fill_px)
                                            session.add(txn)
                                            transaction_field_updates = True
                                        if data_dict is not None:
                                            dependent_order.data["sl_rebased_to_fill"] = True
                                            dependent_order.data["parent_filled_price"] = fill_px

                                    if adj.tp_floored:
                                        old_tp = dependent_order.limit_price
                                        dependent_order.limit_price = adj.take_profit
                                        self.logger.warning(
                                            f"TP floor re-check against real fill: order {dependent_order.id} "
                                            f"was ${old_tp:.4f} (pre-fill), below the {min_pct}% minimum from "
                                            f"the real fill ${fill_px:.4f}. Adjusted to "
                                            f"${adj.take_profit:.4f}"
                                        )
                                        if txn:
                                            txn.take_profit = adj.take_profit
                                            session.add(txn)
                                            transaction_field_updates = True
                                        dependent_order.data["tp_floor_rechecked_at_fill"] = True

                                    # ===== Legacy: percent-based recalc (rule 3 of the shared function) =====
                                    # Overrides the two adjustments above, exactly as it always did.
                                    if adj.legacy_tp_applied:
                                        old_limit_price = dependent_order.limit_price
                                        dependent_order.limit_price = adj.take_profit
                                        self.logger.info(
                                            f"Recalculated TP price for order {dependent_order.id}: "
                                            f"parent filled ${fill_px:.2f} * (1 + {legacy_tp:.2f}%) "
                                            f"= ${adj.take_profit:.2f} (was ${old_limit_price:.2f})"
                                        )
                                        dependent_order.data["parent_filled_price"] = fill_px
                                        dependent_order.data["recalculated_at_trigger"] = True
                                        transaction_updated = True
                                    elif adj.legacy_sl_applied:
                                        old_stop_price = dependent_order.stop_price
                                        dependent_order.stop_price = adj.stop_loss
                                        self.logger.info(
                                            f"Recalculated SL price for order {dependent_order.id}: "
                                            f"parent filled ${fill_px:.2f} * (1 + {legacy_sl:.2f}%) "
                                            f"= ${adj.stop_loss:.2f} (was ${old_stop_price:.2f})"
                                        )
                                        dependent_order.data["TP_SL"]["parent_filled_price"] = fill_px
                                        dependent_order.data["TP_SL"]["recalculated_at_trigger"] = True
                                        transaction_updated = True
                                    elif data_dict is None:
                                        # No data field yet - will be populated when account submits the order
                                        self.logger.debug(f"No data field in order {dependent_order.id}, will ensure percent is calculated during submission")
                                    else:
                                        self.logger.debug(f"No TP/SL percent found in order {dependent_order.id}.data, attempting fallback calculation")
                            except (KeyError, TypeError, ValueError) as adjust_err:
                                self.logger.warning(
                                    f"Could not adjust the protective levels of order {dependent_order.id} "
                                    f"to the parent fill (left as they were): {adjust_err}")
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
                # ONE submitter per order (see _DEPENDENT_SUBMIT_CLAIMS). The claim is
                # released in the `finally` below, after every outcome write of this order.
                if not _claim_dependent_submit(dependent_order.id):
                    self.logger.debug(
                        f"Dependent order {dependent_order.id} is being submitted by another "
                        f"thread; skipping it in this pass")
                    continue
                try:
                    # Phase 1 read this row before the claim: another pass may have submitted,
                    # failed, deferred-and-rechained or cancelled it since. Act only if it is
                    # still the row Phase 1 decided on; otherwise the next pass re-evaluates it.
                    current = row_or_none(TradingOrder, dependent_order.id)
                    if (current is None or current.status != OrderStatus.WAITING_TRIGGER
                            or current.broker_order_id
                            or current.depends_on_order != dependent_order.depends_on_order
                            or current.depends_order_status_trigger
                            != dependent_order.depends_order_status_trigger):
                        self.logger.debug(
                            f"Dependent order {dependent_order.id} changed since this pass read "
                            f"it (now {getattr(current, 'status', 'deleted')}, broker id "
                            f"{getattr(current, 'broker_order_id', None)}); not submitting it here")
                        continue

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

                    if self._rechain_behind_entry_legs(account, dependent_order, parent_order_id):
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
                    # (the position is shrinking, not growing).
                    is_closing = self._is_closing_order(dependent_order)
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
                finally:
                    _release_dependent_submit(dependent_order.id)

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
        
    def _decision_capture_scope(self, expert_instance_id: int, use_case=None, trigger: str = "scheduler"):
        """A replay-capture scope for ONE decision pass of ANY expert (no-op when capture is off).

        ``use_case`` is the pass: ``AnalysisUseCase.ENTER_MARKET`` (default, the entry pass) or
        ``AnalysisUseCase.OPEN_POSITIONS`` (the exit pass). ``trigger`` is ``"scheduler"`` or
        ``"manual"`` (the UI / API calls). Used as ``with self._decision_capture_scope(...) as ctx``;
        ``ctx`` is the pass' ``CaptureContext`` or ``None`` when nothing is being recorded.

        WHY EVERY EXPERT. The pass is where the rules are evaluated, the candidates sized and the
        orders created: the stock decision that nobody could audit or replay before. It is also where
        the market-condition windows and the option action taps are read (capture is opened per
        ANALYSIS by ``MarketExpertInterface._analysis_capture``; these reads happen here, after every
        analysis scope has closed). The scope therefore opens for every expert whenever a store
        exists; it records what the pass reads and does (``decision_record``), it does not change it.

        The pass carries the REAL expert class of the instance (not ``TradeManager`` as an expert)
        and ``branch_flags = {record_kind: decision_pass, pass, trigger, host_timezone}``.

        RECORDING IS NEVER A DEPENDENCY OF TRADING, AND NEVER SILENT. Every failure to open the scope
        (no session, the expert row cannot be read, the scope itself fails) is counted in
        ``CaptureHealth.PASS_NOT_RECORDED``, logged once at ERROR, and -- when a session is open --
        written as a minimal ``error`` pass record naming the cause. The pass then runs unrecorded.
        """
        from contextlib import contextmanager

        @contextmanager
        def _scope():
            from ba2_common.core.replay import capture_scope, get_replay_store
            from ba2_common.core.replay.context import CaptureHealth

            from .types import AnalysisUseCase

            uc = AnalysisUseCase.ENTER_MARKET if use_case is None else use_case
            pass_name = "enter-market" if uc == AnalysisUseCase.ENTER_MARKET else "open-positions"
            store = get_replay_store()
            if store is None:
                from ba2_common.core.replay import decision_sizing

                if not decision_sizing.dry_run_enabled():
                    yield None
                    return
                # BA2_REPLAY_DECISION_DRYRUN=1 and no store: record into a throwaway context and only
                # COUNT the bytes (design Task 1). The recorders are the read-only ones, nothing is written.
                try:
                    dry_meta = self._decision_pass_meta(
                        decision_sizing.DryRunStore(), expert_instance_id, uc, pass_name, trigger)
                except Exception as e:  # noqa: BLE001 -- the dry run must not stop a decision
                    logger.error(f"decision dry run: pass meta not built: {e}", exc_info=True)
                    yield None
                    return
                with decision_sizing.dry_run_scope(dry_meta) as dry_context:
                    yield dry_context
                return
            try:
                meta = self._decision_pass_meta(store, expert_instance_id, uc, pass_name, trigger)
                from . import replay_capture

                allowed = replay_capture.decision_pass_expert_allowlist()
                not_allowed = allowed is not None and meta["expert_class"] not in allowed
                if not_allowed:
                    # ROLLOUT allow-list (replay_capture.DECISION_PASS_EXPERTS_KEY): not an error, a
                    # decision not to record; counted per reason and expert so it stays visible.
                    store.health.record_discard("expert_not_allowed", meta["expert_class"])
            except Exception as e:  # noqa: BLE001 -- a recording problem never stops a decision
                self._decision_pass_not_recorded(store, expert_instance_id, uc, pass_name, trigger, e)
                yield None
                return
            if not_allowed:
                yield None          # outside the try: the pass body's own exceptions land here
                return
            with capture_scope(store, meta) as context:
                if context is None:
                    # capture_scope logged the cause; this makes it a pass-level fact.
                    store.health.record(CaptureHealth.PASS_NOT_RECORDED)
                    yield None
                    return
                yield context

        return _scope()

    @staticmethod
    def _decision_id_prefix(use_case) -> str:
        """The analysis-id prefix of a pass: ``decision`` (entry) or ``exit-decision`` (exit)."""
        from ba2_common.core.types import AnalysisUseCase

        return "decision" if use_case == AnalysisUseCase.ENTER_MARKET else "exit-decision"

    def _decision_pass_meta(self, store, expert_instance_id: int, use_case, pass_name: str,
                            trigger: str) -> Dict[str, Any]:
        """The analysis meta of a decision pass (design section 2.2)."""
        import uuid

        from ba2_common.core.replay.clock import host_timezone
        from ba2_common.core.replay.decision_record import RECORD_KIND
        from ba2_common.core.types import AnalysisUseCase

        # NOT current_session_id(): that runs the host's rollover check (a 2 s drain plus index writes) on THIS
        # thread, and a pass - an exit pass especially - must never wait on it. The background session roller
        # (replay_capture) rolls at the UTC date change; until it has, the pass records into the still-open
        # previous session, which is a valid session.
        session_id = store.session_id
        if session_id is None:
            raise RuntimeError("replay capture is enabled but no session is open")
        instance = get_instance(ExpertInstance, expert_instance_id)
        started_at = datetime.now(timezone.utc)
        id_prefix = self._decision_id_prefix(use_case)
        return {
            # The pass is not a MarketAnalysis, so it carries an id of its own shape rather than
            # borrowing one analysis's id and colliding with that analysis's bundle.
            "analysis_id": f"{id_prefix}-{expert_instance_id}-{uuid.uuid4().hex}",
            "attempt_id": uuid.uuid4().hex,
            "session_id": session_id,
            "expert_class": instance.expert,
            "expert_instance_id": expert_instance_id,
            # One pass decides for every symbol the expert recommended; the per-symbol identity is
            # on each recorded step, not on the pass.
            "symbol": "",
            "use_case": str(use_case.value),
            "scheduled_at": None,
            "started_at": started_at,
            "branch_flags": {
                "record_kind": RECORD_KIND,
                "pass": pass_name,
                "trigger": trigger,
                "host_timezone": host_timezone(started_at),
            },
        }

    def _decision_pass_not_recorded(self, store, expert_instance_id: int, use_case, pass_name: str,
                                    trigger: str, cause: BaseException) -> None:
        """Count, log and (with a session) persist the fact that a pass is not being recorded."""
        from ba2_common.core.replay.context import CaptureHealth
        from ba2_common.core.replay.decision_record import RECORD_KIND

        store.health.record(CaptureHealth.PASS_NOT_RECORDED)
        logger.error(
            f"replay capture: the {pass_name} decision pass of expert {expert_instance_id} is not "
            f"recorded ({type(cause).__name__}: {cause}); the pass runs normally", exc_info=cause)
        try:
            import uuid

            from ba2_common.core.replay.schemas import AnalysisRecord, ReplayStatus

            session_id = store.session_id
            if session_id is None:
                return                      # the counter is the only evidence there is
            try:
                expert_class = get_instance(ExpertInstance, expert_instance_id).expert
            except Exception:  # noqa: BLE001 -- the very failure being reported may be this lookup
                expert_class = "unknown"
            now = datetime.now(timezone.utc)
            store.submit(AnalysisRecord(
                analysis_id=f"{self._decision_id_prefix(use_case)}-{expert_instance_id}-{uuid.uuid4().hex}",
                attempt_id=uuid.uuid4().hex, session_id=session_id,
                expert_class=expert_class, expert_instance_id=expert_instance_id, symbol="",
                use_case=str(use_case.value), started_at=now, finished_at=now,
                outcome=ReplayStatus.OUTCOME_ERROR,
                error=f"pass not recorded: {type(cause).__name__}: {cause}",
                branch_flags={"record_kind": RECORD_KIND, "pass": pass_name, "trigger": trigger},
                capture_failures={CaptureHealth.PASS_NOT_RECORDED: 1}))
        except Exception as e:  # noqa: BLE001 -- counted above; this is only the persisted evidence
            logger.error(f"replay capture: could not persist the pass-not-recorded evidence: {e}",
                         exc_info=True)

    @staticmethod
    def _created_order_ids(created) -> list:
        """The entry pass' created orders as ids."""
        return [o.id for o in created]

    @staticmethod
    def _created_result_order_ids(created) -> list:
        """The exit pass' action results as the ids of the orders they created."""
        ids = [(r.get("data") or {}).get("order_id") for r in created
               if isinstance(r, dict) and r.get("success")]
        return [i for i in ids if i]

    def _finish_decision_pass(self, context, created, order_ids=None) -> None:
        """End a pass that ran to its end: freeze its summary as the ``decision`` outcome.

        ``created`` is what the pass returns and ``order_ids`` extracts the ids from it: that
        bookkeeping runs INSIDE the guard, because the orders were already sent by now and a failure
        reading them must cost the record, never the caller's return value.
        A pass that already ended as a skip / error / discard keeps that outcome. Never raises.
        """
        if context is None:
            return
        try:
            from ba2_common.core.replay.decision_record import pass_summary

            if not context.has_outcome and context.discard_reason is None:
                created_order_ids = (self._created_order_ids if order_ids is None else order_ids)(created)
                context.set_decision(pass_summary(context, created=created_order_ids))
            if not context.has_outcome and context.discard_reason is None:
                raise RuntimeError("the decision pass ended without a recorded outcome")
        except Exception as e:  # noqa: BLE001 -- recording never reaches the decision
            from ba2_common.core.replay.context import CaptureHealth

            context.note_failure("decision pass summary not recorded", e,
                                 CaptureHealth.PASS_NOT_RECORDED)

    def _option_entry_decisions(self, expert, expert_instance_id: int, symbol: str,
                                results) -> list:
        """Run-record rows for a classic-mode expert's option ENTRY results.

        ``[]`` for an expert on the option risk manager (``classic_options``): that manager
        journals the same entries into its own ``options`` run, and recording them here too
        would show every entry twice. Never raises -- the record is observability, and the
        entries it describes have already been sent.
        """
        try:
            from ba2_common.core.OptionRiskManagement import option_risk_manager_enabled
            from ba2_common.core.risk_manager_run import option_entry_decision
            from .types import get_option_entry_action_values

            if option_risk_manager_enabled(getattr(expert, "settings", None),
                                           expert_instance_id=expert_instance_id):
                return []
            entry_values = set(get_option_entry_action_values())
            rows = []
            for result in results or []:
                action_type = result.get("action_type")
                value = getattr(action_type, "value", action_type)
                if value in entry_values:
                    rows.append(option_entry_decision(symbol, result))
            return rows
        except Exception as e:  # noqa: BLE001 -- see the docstring
            self.logger.warning(f"Could not record the option entries for {symbol} "
                                f"(expert {expert_instance_id}): {e}")
            return []

    def process_expert_recommendations_after_analysis(self, expert_instance_id: int, lookback_days: int = 1,
                                                      trigger: str = "scheduler") -> List[TradingOrder]:
        """Process enter_market recommendations inside ONE market-condition decision scope.

        The scope reads the evaluation clock once, on this (coordinating) thread, and only when
        THIS EXPERT's ``market_condition_profile`` setting names a profile; otherwise it is a
        no-op. Every market-condition leaf of this pass then resolves the same frozen context.
        See ``_process_expert_recommendations_after_analysis`` for the processing itself.

        ORDER IS LOAD-BEARING: the capture scope opens FIRST. ``begin_decision`` reads
        ``current_capture()`` to decide whether to wrap the reader in a
        ``CapturingMarketConditionReader``, so a capture scope opened inside the decision scope
        would be too late and the pass would record no windows at all.
        """
        from ba2_common.core.market_condition_live import market_condition_decision_scope

        with self._decision_capture_scope(expert_instance_id, trigger=trigger) as pass_context:
            with market_condition_decision_scope(expert_instance_id=expert_instance_id):
                created = self._process_expert_recommendations_after_analysis(expert_instance_id, lookback_days)
            self._finish_decision_pass(pass_context, created)
            return created

    #: The per-expert processing locks, in the fixed order ``hold_expert_locks`` takes them.
    _PROCESSING_USE_CASES = ("enter_market", "open_positions")

    def _processing_lock(self, expert_instance_id: int, use_case: str) -> threading.Lock:
        lock_key = f"expert_{expert_instance_id}_usecase_{use_case}"
        with self._locks_dict_lock:
            if lock_key not in self._processing_locks:
                self._processing_locks[lock_key] = threading.Lock()
            return self._processing_locks[lock_key]

    def hold_expert_locks(self, expert_instance_id: int, timeout: float, use_cases=None):
        """Context manager for a MANAGEMENT-API write: holds the per-expert processing locks named by
        ``use_cases`` (default BOTH: the ones the enter_market and open_positions passes take) so the write cannot
        interleave with a pass. Waits up to ``timeout`` seconds in total; yields True when all were taken, False
        (holding none) otherwise.
        Passes use a 0.5 s try-lock and skip when busy, so a held write makes them skip, never deadlock."""
        import time
        from contextlib import contextmanager

        @contextmanager
        def _hold():
            deadline = time.monotonic() + timeout
            held = []
            wanted = self._PROCESSING_USE_CASES if use_cases is None else [
                u for u in self._PROCESSING_USE_CASES if u in use_cases]
            try:
                for use_case in wanted:      # fixed order: two writers cannot deadlock
                    lock = self._processing_lock(expert_instance_id, use_case)
                    if not lock.acquire(blocking=True, timeout=max(0.0, deadline - time.monotonic())):
                        yield False
                        return
                    held.append(lock)
                yield True
            finally:
                for lock in reversed(held):
                    lock.release()

        return _hold()

    def _process_expert_recommendations_after_analysis(self, expert_instance_id: int, lookback_days: int = 1) -> List[TradingOrder]:
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
            pass_discard("lock_busy")
            return []
        
        # We have the lock - make sure we release it when done
        created_orders = []
        # TEMP-ORDER-LIST FLOW: instead of persisting a qty=0 PENDING order per passing rec and then
        # letting the RM size + DELETE the unfunded (churn), we stage a TRANSIENT candidate per passing
        # rec, size them ALL in one in-memory RM pass, then persist + submit ONLY the funded ones.
        # Each entry: (transient_candidate_order, evaluator, recommendation).
        entry_candidates = []
        # Run-record rows for the OPTION entries this pass executes (they size and submit
        # themselves); appended to the classic run below. See _option_entry_decisions.
        option_decisions = []

        try:
            self.logger.debug(f"Acquired processing lock for expert {expert_instance_id} (enter_market)")
            # The SPY regime the spy-trend / spy-vol entry gates read, from FINISHED sessions, BEFORE any
            # rule is evaluated (the pass can run outside a refresh).
            # THE pass's decision instant: the ONE wall-clock read of this pass, taken through the
            # evaluation-clock seam (the wall clock when no capture is open) BEFORE anything else could
            # read it, so a replay finds it first. The regime publish and the recommendation cutoff
            # below both use it.
            decision_now = replay_now()
            self._publish_benchmark_regime(now=decision_now)
            
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
                pass_fail(f"expert instance {expert_instance_id} not found")
                return created_orders

            # Get the expert instance model (for ruleset IDs)
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                self.logger.error(f"Expert instance model {expert_instance_id} not found")
                pass_fail(f"expert instance model {expert_instance_id} not found")
                return created_orders

            # Check if "Allow automated trade opening" is enabled
            allow_automated_trade_opening = expert.get_setting_with_interface_default(
                'allow_automated_trade_opening', log_warning=False
            )
            if not allow_automated_trade_opening:
                self.logger.debug(f"Automated trade opening disabled for expert {expert_instance_id}, skipping recommendation processing")
                pass_skip("automated trade opening disabled")
                return created_orders

            # Check if there's an enter_market ruleset configured
            if not expert_instance.enter_market_ruleset_id:
                self.logger.debug(f"No enter_market ruleset configured for expert {expert_instance_id}, skipping automated order creation")
                pass_skip("no enter_market ruleset configured")
                return created_orders

            # Check if there are still pending analysis jobs for this expert
            if self._has_pending_analysis_jobs(expert_instance_id):
                self.logger.debug(f"Still has pending analysis jobs for expert {expert_instance_id}, skipping automated order creation")
                pass_discard("pending_analysis_jobs")
                return created_orders

            # Get the account instance for this expert
            account_def = get_instance(AccountDefinition, expert_instance.account_id)
            if not account_def:
                self.logger.error(f"Account definition {expert_instance.account_id} not found")
                pass_fail(f"account definition {expert_instance.account_id} not found")
                return created_orders

            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found")
                pass_fail(f"account provider {account_def.provider} not found")
                return created_orders

            account = account_class(account_def.id)

            # Get recent recommendations based on lookback_days parameter
            cutoff_time = decision_now - timedelta(days=lookback_days)
            from ba2_common.core.hold_entry import evaluate_hold_entries
            allow_hold = evaluate_hold_entries(expert.settings)

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
                ).order_by(ExpertRecommendation.created_at.desc())  # Most recent first
                if not allow_hold:
                    statement = statement.where(
                        ExpertRecommendation.recommended_action != OrderRecommendation.HOLD)

                all_recommendations = session.exec(statement).all()

                if not all_recommendations:
                    self.logger.info(f"No actionable recommendations found for expert {expert_instance_id}")
                    pass_skip("no actionable recommendations")
                    return created_orders
                
                # Latest recommendation per instrument (prevents processing several for one symbol),
                # best profit potential first: the SHARED selection the decision replay recomputes.
                recommendations = select_enter_candidates(all_recommendations)

                note_selection(recommendations)
                record_pass_state(
                    expert=expert, expert_instance=expert_instance, pass_name="enter-market",
                    lookback_days=lookback_days, cutoff_time=cutoff_time, decision_time=decision_now,
                    account=account, ruleset_id=expert_instance.enter_market_ruleset_id,
                    fetched_recommendations=all_recommendations, selected_recommendations=recommendations)
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
                        # for this symbol and expert to prevent duplicate positions. The SHARED gate
                        # the backtest engine applies too (trade_cycle.open_or_waiting_position).
                        from .trade_cycle import open_or_waiting_position
                        existing_txn = open_or_waiting_position(
                            expert_instance_id, recommendation.symbol, session=session)
                        
                        note_gate(recommendation.symbol, "open_or_waiting_position", existing_txn is None,
                                  None if existing_txn is None else
                                  f"transaction {existing_txn.id} {existing_txn.status}")
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
                        note_gate(recommendation.symbol, "sufficient_equity", bool(has_sufficient_equity),
                                  equity_reason)
                        if not has_sufficient_equity:
                            self.logger.warning(
                                f"EQUITY CHECK: Skipping recommendation {recommendation.id} for {recommendation.symbol} - "
                                f"expert {expert_instance_id}: {equity_reason}"
                            )
                            continue
                        
                        # PER-ENTRY ROUTING through the SHARED router the backtest engine calls
                        # too (trade_cycle.entry_route; plan 2026-09-25 option-stage2-build E1),
                        # so the two runtimes cannot route the same fired rule differently.
                        from .trade_cycle import (ENTRY_ROUTE_OPTION, MixedEntryActionsRefused,
                                                  entry_route)
                        try:
                            route = entry_route(s['action_type'] for s in action_summaries)
                        except MixedEntryActionsRefused as mixed:
                            note_gate(recommendation.symbol, "entry_route", False, str(mixed))
                            # Neither path can size both halves: the equity RM would stamp a
                            # share count + stock stop onto the option order, and the option
                            # path would submit the equity action unsized. Refuse loudly.
                            self.logger.error(
                                f"Refusing recommendation {recommendation.id} for {recommendation.symbol}: "
                                f"{mixed}")
                            continue
                        note_gate(recommendation.symbol, "entry_route", True, str(route))
                        if route == ENTRY_ROUTE_OPTION:
                            # Option actions size and submit themselves through their shared
                            # risk/capacity guards, exactly like the backtest option path,
                            # whatever the signal. They must never reach the equity RM below:
                            # it re-labels the contracts as shares and stages a STOCK safeguard
                            # stop at the broker (8082, 2026-09-24: SELL 9 GILD @ 138.56 behind
                            # a 2-contract call).
                            option_results = evaluator.execute(submit_to_broker=True)
                            for result in option_results:
                                if result.get("success"):
                                    oid = (result.get("data") or {}).get("order_id")
                                    if oid:
                                        order = get_instance(TradingOrder, oid)
                                        if order:
                                            created_orders.append(order)
                            option_decisions.extend(self._option_entry_decisions(
                                expert, expert_instance_id, recommendation.symbol, option_results))
                            continue

                        # TEMP-ORDER-LIST FLOW: do NOT execute (persist) yet. Stage a TRANSIENT
                        # candidate order (via the shared trade_cycle builder — same shape the
                        # backtest uses) for the in-memory RM sizing pass below; only the funded
                        # subset is executed + submitted (no qty=0 churn, no unfunded deletes).
                        from .trade_cycle import build_entry_candidate, fired_entry_lot_size
                        candidate = build_entry_candidate(
                            recommendation, account.id,
                            lot_size=fired_entry_lot_size(evaluator))
                        entry_candidates.append((candidate, evaluator, recommendation))

                    except Exception as e:
                        self.logger.error(f"Error processing recommendation {recommendation.id}: {e}", exc_info=True)
                        note_pass_error(f"recommendation {recommendation.id}", e)
                        continue

                # TEMP-ORDER-LIST RM SIZING: size ALL the bar's candidates in one in-memory pass; only
                # the funded (qty>0) get persisted (execute) + submitted. Unfunded candidates never
                # touch the DB — the requested order-flow redesign (was: persist qty=0 -> RM DB pass ->
                # delete unfunded). The funded set + quantities are identical to the old DB path
                # (size_candidate_orders == review_and_prioritize_pending_orders; proven by tests).
                submitted_count = 0
                if option_decisions and not entry_candidates:
                    # Nothing to size, but the option entries still get their run record.
                    from .TradeRiskManagement import get_risk_management
                    get_risk_management().size_candidate_orders(
                        expert_instance_id, [], option_decisions=option_decisions)
                if entry_candidates and allow_automated_trade_opening:
                    self.logger.info(f"Sizing {len(entry_candidates)} entry candidate(s) for expert {expert_instance_id}")
                    try:
                        from .TradeRiskManagement import get_risk_management
                        risk_management = get_risk_management()
                        # option_decisions only when there are some: an equity-only pass
                        # makes exactly the call it always made.
                        funded = risk_management.size_candidate_orders(
                            expert_instance_id, [(c, rec) for (c, _e, rec) in entry_candidates],
                            **({"option_decisions": option_decisions} if option_decisions else {}))
                        funded_by_symbol = {o.symbol: o for o in funded}
                        self.logger.info(f"RM funded {len(funded)} of {len(entry_candidates)} candidate(s)")

                        for candidate, evaluator, recommendation in entry_candidates:
                            fo = funded_by_symbol.get(candidate.symbol)
                            if fo is None or not (fo.quantity and fo.quantity > 0):
                                continue  # unfunded — never persisted (no churn / delete)
                            note_funded(candidate.symbol, fo.quantity, fo.stop_price)
                            # BEFORE the try, and per iteration: the except handler below
                            # compensates `order`, and a name left bound by the PREVIOUS
                            # iteration would make it compensate the wrong symbol's entry.
                            order = None
                            try:
                                with record_execute(evaluator, candidate.symbol, recommendation, True) as step:
                                    def _executed(results, main_order, step=step):
                                        nonlocal order
                                        step.result = results
                                        step.order = main_order
                                        # Bind the order the moment it is KNOWN, before anything is
                                        # persisted: execute() has already created the WAITING
                                        # Transaction + PENDING order, so a raise in the persist /
                                        # leg stamp must still reach the except handler's
                                        # _fail_unsent_entry for THIS order (pre-refactor the loop
                                        # bound it before persisting).
                                        order = main_order

                                    # Execute (the real order + transaction + TP/SL bracket, ONLY for
                                    # funded symbols), fetch the main order DETACHED, write the RM-sized
                                    # quantity + safeguard stop onto it BEFORE the broker is asked for
                                    # anything and stamp the protective legs: ONE shared function
                                    # (trade_cycle.stage_funded_entry), the same the offline decision
                                    # replay calls. Its docstrings carry the history of each step.
                                    stage_funded_entry(
                                        evaluator, candidate.symbol, fo.quantity, fo.stop_price or None,
                                        on_executed=_executed)
                                    if order is None:
                                        continue

                                    # The submit wrapper reconciles this RM safeguard with the
                                    # transaction's ruleset stop using the same helper as backtests.
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
                        note_pass_error("risk management", e)
                
        except Exception as e:
            self.logger.error(f"Error processing expert recommendations after analysis for expert {expert_instance_id}: {e}", exc_info=True)
            note_pass_error("pass", e)
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

    #: ``(expert instance id, exception type name)`` whose exit-pass scope failure was already
    #: logged at ERROR with a traceback. Process-wide (a class attribute: TradeManager is a
    #: singleton), so a persistent fault logs its traceback once and WARNING lines afterwards.
    _exit_scope_failures_reported: set = set()
    _exit_scope_failures_lock = threading.Lock()

    def _open_exit_pass_market_condition_scope(self, expert_instance_id: int,
                                               account_id: Optional[int]):
        """Open the market-condition decision scope for ONE open-positions (exit) pass.

        Returns an entered ``ExitStack`` for the caller's ``with``: it closes the scope when the
        pass ends, and it is EMPTY of a scope when the scope could not be opened. Either way it
        also marks the pass as an EXIT pass for the market-condition dispatcher
        (``begin_exit_pass``) until the pass ends, so a leaf's own resolver lookup never raises
        during it, and -- after a failed opening -- answers with the guard's cause at once
        instead of retrying the lookup per leaf.

        EXITS ARE NEVER BLOCKED BY THE MARKET-CONDITION MACHINERY. Opening the scope can raise
        for a gated expert -- a malformed manifest environment variable, a resolver build or
        coverage refresh failing, a defect in the settings path (TypeError/AttributeError). The
        entry pass lets that propagate: refusing entries is the safe reading. For exits it is
        not, because the same exception would skip every stop, take-profit and close rule of
        this expert for the pass. So a failure here is reported (ERROR + a FAILURE activity row)
        and the pass runs WITHOUT the scope: its market-condition leaves read ``no_context`` and
        never pass, and every other exit and protective rule runs as usual.

        ONLY THE OPENING IS GUARDED. The stack is returned to the caller's ``with``, so an
        exception raised by the pass body propagates exactly as before.
        """
        from contextlib import ExitStack

        from ba2_common.core.failure_modes import absorb_if_benign
        from ba2_common.core.market_condition_live import (
            begin_exit_pass,
            market_condition_decision_scope,
            record_exit_pass_scope_failure,
        )

        stack = ExitStack()
        # Registered FIRST, so it is unwound LAST: the scope closes, then the exit-pass mark.
        stack.callback(begin_exit_pass(expert_instance_id))
        try:
            stack.enter_context(market_condition_decision_scope(expert_instance_id=expert_instance_id))
        except Exception as e:
            # DELIBERATELY broad, and named as such so it survives BA2_ERROR_MODE=enforce: the
            # scope can fail in ways this site cannot enumerate (manifest config, reader build,
            # an injected settings seam), and whatever it is must not stop the exits. Refusals
            # that failure_modes never absorbs still propagate (after unwinding the mark).
            try:
                absorb_if_benign(e, Exception)
            except BaseException:
                stack.close()
                raise
            record_exit_pass_scope_failure(e)
            self._report_exit_scope_failure(expert_instance_id, account_id, e)
        except BaseException:
            stack.close()
            raise
        return stack

    def _report_exit_scope_failure(self, expert_instance_id: int, account_id: Optional[int],
                                   e: BaseException) -> None:
        """ERROR with the traceback once per (expert, exception type) per process, WARNING on
        repeats; and a FAILURE activity row EVERY pass, so operators see each degraded pass."""
        message = (
            f"Expert instance {expert_instance_id}: the market-condition decision scope could "
            f"not be opened for the open-positions pass ({type(e).__name__}: {e}). Its "
            f"market-condition exit rules read no_context this pass and will not fire; every "
            f"other exit and protective rule still runs.")
        key = (expert_instance_id, type(e).__name__)
        with TradeManager._exit_scope_failures_lock:
            first = key not in TradeManager._exit_scope_failures_reported
            TradeManager._exit_scope_failures_reported.add(key)
        if first:
            self.logger.error(message, exc_info=e)
        else:
            self.logger.warning(message + " (repeat of an error already logged with its "
                                          "traceback)")
        try:
            from .db import log_activity
            from .types import ActivityLogSeverity, ActivityLogType

            log_activity(
                severity=ActivityLogSeverity.FAILURE,
                activity_type=ActivityLogType.RISK_MANAGER_RAN,
                description=(f"Market-condition scope failed for the open-positions pass "
                             f"({type(e).__name__}: {e}); market-condition exit rules read "
                             f"no_context this pass, all other exit rules ran"),
                data={
                    "mode": "classic",
                    "use_case": "open_positions",
                    "stage": "market_condition_scope",
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
                source_expert_id=expert_instance_id,
                source_account_id=account_id,
            )
        except Exception as log_error:
            self.logger.warning(f"Failed to log market-condition scope failure activity: {log_error}")

    def process_open_positions_recommendations(self, expert_instance_id: int, lookback_days: int = 1,
                                               trigger: str = "scheduler") -> List[TradingOrder]:
        """Process OPEN_POSITIONS recommendations inside ONE decision capture scope.

        The scope opens FIRST (before the lock, the early returns and the market-condition scope):
        every exit of the pass is then either a recorded decision or a counted discard. It is a
        no-op without a capture store, and a failure to open it never stops an exit (it is counted
        and the pass runs unrecorded). See ``_process_open_positions_recommendations``.
        """
        from .types import AnalysisUseCase

        with self._decision_capture_scope(expert_instance_id, AnalysisUseCase.OPEN_POSITIONS,
                                          trigger=trigger) as pass_context:
            created = self._process_open_positions_recommendations(expert_instance_id, lookback_days)
            self._finish_decision_pass(pass_context, created, self._created_result_order_ids)
            return created

    def _process_open_positions_recommendations(self, expert_instance_id: int, lookback_days: int = 1) -> List[TradingOrder]:
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
            pass_discard("lock_busy")
            return []
        
        created_orders = []
        
        try:
            self.logger.debug(f"Acquired processing lock for expert {expert_instance_id} (open_positions)")
            # Exit rules may read the SPY regime too: publish before they are evaluated.
            self._publish_benchmark_regime()
            
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
                pass_fail(f"expert instance {expert_instance_id} not found")
                return created_orders

            # Get the expert instance model (for ruleset IDs)
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                self.logger.error(f"Expert instance model {expert_instance_id} not found")
                pass_fail(f"expert instance model {expert_instance_id} not found")
                return created_orders

            # Check if "Allow automated trade modification" is enabled
            allow_automated_trade_modification = expert.get_setting_with_interface_default(
                'allow_automated_trade_modification', log_warning=False
            )
            if not allow_automated_trade_modification:
                self.logger.debug(f"Automated trade modification disabled for expert {expert_instance_id}, skipping recommendation processing")
                pass_skip("automated trade modification disabled")
                return created_orders

            # Check if there's an open_positions ruleset configured
            if not expert_instance.open_positions_ruleset_id:
                self.logger.debug(f"No open_positions ruleset configured for expert {expert_instance_id}, skipping automated trade modification")
                pass_skip("no open_positions ruleset configured")
                return created_orders

            # Get the account instance for this expert
            account_def = get_instance(AccountDefinition, expert_instance.account_id)
            if not account_def:
                self.logger.error(f"Account definition {expert_instance.account_id} not found")
                pass_fail(f"account definition {expert_instance.account_id} not found")
                return created_orders

            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found")
                pass_fail(f"account provider {account_def.provider} not found")
                return created_orders

            account = account_class(account_def.id)

            # Get recent recommendations based on lookback_days parameter
            # (the recommendation cutoff is taken inside the capture scope below, through the clock seam)

            # ONE market-condition decision pass for this expert's exit rules, mirroring the
            # entry pass (process_expert_recommendations_after_analysis). Without it a market
            # leaf in an open_positions rule reads no_context live while the backtest evaluates
            # it: a BT/live parity break. Opened HERE -- after the lock and the early returns,
            # around the evaluation only -- so an expert with trade modification off or no
            # open_positions ruleset never reads its profile. With no market_condition_profile
            # setting the scope yields None at once (no clock read, no state): a strict no-op.
            # The replay-capture scope opens FIRST (same load-bearing order as the entry pass:
            # begin_decision reads current_capture()); it is a no-op unless capture is on and the
            # expert is gated or on an options account. A failure to OPEN the market-condition
            # scope never stops the exits: see _open_exit_pass_market_condition_scope.
            with self._open_exit_pass_market_condition_scope(expert_instance_id, account_def.id), get_db() as session:
                # Recent recommendations by lookback_days. Read INSIDE the capture scope (opened by the
                # public wrapper), so the pass's decision instant is a recorded clock read and replays as such.
                decision_now = replay_now()
                cutoff_time = decision_now - timedelta(days=lookback_days)
                # Get all recommendations for this expert instance within the time window
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.instance_id == expert_instance_id,
                    ExpertRecommendation.created_at >= cutoff_time
                ).order_by(ExpertRecommendation.created_at.desc())

                all_recommendations = session.exec(statement).all()

                if not all_recommendations:
                    self.logger.info(f"No recommendations found for expert {expert_instance_id}")
                    pass_skip("no recommendations")
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
                
                note_selection(recommendations)
                record_pass_state(
                    expert=expert, expert_instance=expert_instance, pass_name="open-positions",
                    lookback_days=lookback_days, cutoff_time=cutoff_time, decision_time=decision_now,
                    account=account, ruleset_id=expert_instance.open_positions_ruleset_id,
                    fetched_recommendations=all_recommendations, selected_recommendations=recommendations)
                self.logger.info(f"Found {len(recommendations)} unique instruments with open_positions recommendations for expert {expert_instance_id} (filtered from {len(all_recommendations)} total recommendations)")
                self.logger.info(f"Evaluating recommendations through open_positions ruleset: {expert_instance.open_positions_ruleset_id}")
                
                # Process each recommendation through the open_positions ruleset
                for recommendation in recommendations:
                    try:
                        # THE BOOK this symbol's exits are evaluated against: THIS expert's OPENED
                        # transactions, minus any with a closing order already WORKING (else each
                        # cycle could submit ANOTHER close for the same position; see
                        # ReadOnlyAccountInterface.has_pending_closing_order). The SAME filter the
                        # backtest engine applies (entry_order.positions_to_manage, plan 2026-09-25
                        # option-stage2-build Task 8 review asymmetry (a)). WAITING transactions
                        # are NOT positions to exit: evaluated in the filled-but-not-promoted
                        # window with existing_order=None, a close rule needing no entry order took
                        # CloseAction's legacy broker-position branch and closed the broker
                        # position without linking the close to the transaction.
                        from ba2_common.core.entry_order import positions_to_manage
                        statement = select(Transaction).where(
                            Transaction.symbol == recommendation.symbol,
                            Transaction.status == TransactionStatus.OPENED,
                            Transaction.expert_id == expert_instance_id
                        )
                        all_transactions = session.exec(statement).all()
                        existing_transactions = positions_to_manage(all_transactions, account)
                        # An OPENED transaction left out has a closing order still WORKING
                        # (positions_to_manage). Each skip is logged: a silent skip here hid 13
                        # prod positions' exit rules for weeks.
                        managed_ids = {t.id for t in existing_transactions}
                        for t in all_transactions:
                            if t.status == TransactionStatus.OPENED and t.id not in managed_ids:
                                self.logger.info(
                                    f"Skipping exit-rule evaluation of transaction {t.id} "
                                    f"({t.symbol}): a closing order is still working")

                        if not existing_transactions:
                            self.logger.debug(f"No OPENED transaction to manage for {recommendation.symbol}, skipping recommendation {recommendation.id}")
                            continue
                        
                        # Create TradeActionEvaluator with existing transactions for open_positions use case
                        evaluator = TradeActionEvaluator(
                            account=account,
                            instrument_name=recommendation.symbol,
                            existing_transactions=existing_transactions
                        )

                        # The entry order every condition reads (DaysOpened, P&L, the style-member
                        # entry_tag): the entry order of the oldest OPENED transaction, by the ONE
                        # definition the backtest engine uses too (entry_order.oldest_entry_order,
                        # plan 2026-09-25 option-stage2-build Task 8 review I1). An OPENED position
                        # with no entry order row is REFUSED by name: evaluated against None, every
                        # P&L / days-opened / entry_tag condition would read False and CloseAction
                        # would lose its transaction link.
                        from ba2_common.core.entry_order import (EntryOrderUnresolved,
                                                                 oldest_entry_order)
                        try:
                            existing_order = oldest_entry_order(existing_transactions,
                                                                session=session)
                        except EntryOrderUnresolved as e:
                            self.logger.error(
                                f"Skipping open_positions evaluation of recommendation "
                                f"{recommendation.id} for {recommendation.symbol}: {e}")
                            continue
                        self.logger.debug(f"Resolved entry order {existing_order.id} for {recommendation.symbol} (created: {existing_order.created_at})")

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
                                with record_execute(evaluator, recommendation.symbol, recommendation,
                                                    allow_automated_trade_modification) as step:
                                    execution_results = evaluator.execute(
                                        submit_to_broker=allow_automated_trade_modification)
                                    step.result = execution_results
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
                                note_pass_error(f"execute {recommendation.symbol}", e)

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
                        note_pass_error(f"recommendation {recommendation.id}", e)
                
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
