"""Custom daily multi-asset backtest engine.

Drives the REAL ba2trade decision/order path against the simulated ``BacktestAccount``,
with NO ``TradeManager`` import — the thin driver loop is re-implemented here from the
SAME packaged pieces the live ``TradeManager.process_expert_recommendations_after_analysis``
uses (BA2TradePlatform/.../core/TradeManager.py lines ~901-1190):

  per bar (a single simulated trading day ``as_of``):
    1. advance the virtual clock + BUST the per-account price cache (the gotcha);
    2. resolve the universe for the bar (static enabled_instruments, filtered to bars);
    3. for each (expert, settings):
         a. build the Phase-1 ``BacktestContext`` (providers / settings / account / as_of);
         b. for each symbol: ``rec = expert.analyze_as_of(as_of, ctx)`` — the SAME _gather+
            _process the live ``run_analysis`` runs — then, for a non-skip / non-HOLD
            actionable recommendation, persist an ``ExpertRecommendation`` row in the
            backtest DB and run it through the enter_market ruleset via
            ``TradeActionEvaluator.evaluate(...).execute(submit_to_broker=False)`` (creates a
            PENDING qty=0 ``TradingOrder``, exactly like live);
         c. once per expert: ``TradeRiskManagement(indicator_provider=<pandas indicators>)
            .review_and_prioritize_pending_orders(expert_instance_id)`` sizes the pending
            orders (classic RM + ``position_sizing.compute_risk_based_quantity`` /
            ``get_latest_atr``), then ``account.submit_order(order)`` for each sized order;
    4. ``account.refresh_orders()`` (the fill engine) + ``account.refresh_transactions()``
       (inherited WAITING->OPENED->CLOSED lifecycle) roll the bar's order/transaction state;
    4c. for a ``classic_options`` sleeve ONLY: ``update_sleeve_breaker`` — the SAME shared
       transition the live exit pass calls — ratchets the sleeve's peak equity and trips or
       re-arms its drawdown breaker, whose latch the entry gate then reads on the next bar.
       An equity trial never reaches it (see ``_option_sleeves``);
    5. ``account.snapshot_equity(as_of)`` records the per-bar equity curve point.

The decision logic is NOT perturbed: ``analyze_as_of`` is byte-identical to the Phase-1
golden path; the engine only wires the as_of clock + the order/RM driver around it.

Determinism: ``random``/``numpy`` are seeded from ``config["seed"]`` before the loop so a
run is reproducible (same cache + same params + same seed => identical equity curve).

Reuses (does NOT redefine):
  * ``ba2_common.core.backtest_context.BacktestContext`` + ``LiveProviderBundle`` (Phase 1).
  * ``ba2_common.core.TradeActionEvaluator.TradeActionEvaluator`` (enter/exit ruleset).
  * ``ba2_common.core.TradeRiskManagement.TradeRiskManagement`` (classic RM + sizing).
  * ``app.services.backtest.seam_wiring.make_indicator_provider`` (ATR injection seam).
  * the host ``BacktestAccount`` (submit_order / refresh_orders / refresh_transactions).
"""
from __future__ import annotations

import bisect
import random
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

import numpy as np

from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.models import ExpertRecommendation, TradingOrder, Transaction
from ba2_common.core.types import (
    AnalysisUseCase,
    OrderDirection,
    OrderRecommendation,
    OrderStatus,
    OrderType,
    RiskLevel,
    TimeHorizon,
    TransactionStatus,
)
from ba2_common.core.OptionRiskManagement import (
    option_risk_manager_enabled, update_sleeve_breaker,
)
from ba2_common.core.regime_overlay import reset_stressed, set_stressed
from ba2_common.logger import logger

from app.services.backtest.seam_wiring import make_indicator_provider, make_atr_cache_indicator_provider


# ---------------------------------------------------------------------------
# Clock + universe hooks
# ---------------------------------------------------------------------------
def trading_days(start: datetime, end: datetime, price_source) -> List[Any]:
    """The backtest clock = the union of dataset bar keys in ``[start, end]``.

    Using the price source's own bar keys (not a synthetic calendar) keeps the clock
    aligned to available data: no phantom bars when nothing traded. Returns sorted bar
    keys — ``date`` for a daily source, ``datetime`` for an intraday source (so the
    loop steps once per intraday bar). Filtering is done on datetimes so a date key and
    a datetime key compare consistently against the ``[start, end]`` bounds.
    """
    lo = _to_dt(start)
    hi_intraday = getattr(price_source, "is_intraday", False)
    # For an intraday source compare to the exact end timestamp; for a daily source
    # keep the inclusive end-of-day bound (a date key compares within [lo_date, hi_date]).
    hi = _to_dt(end) if hi_intraday else _to_dt(end).replace(hour=23, minute=59, second=59)
    return [d for d in price_source.all_dates() if lo <= _to_dt(d) <= hi]


def resolve_universe(as_of: datetime, config: Dict[str, Any], price_source) -> List[str]:
    """v1 universe: the static ``enabled_instruments`` list, filtered to symbols that
    actually have a bar on ``as_of`` (a symbol with no bar today cannot be analysed/priced).

    Phase 3 replaces the body with the historical-screener reconstruction; the hook
    (signature + filter) is built now so the swap is body-only.
    """
    universe = config["enabled_instruments"]
    return [s for s in universe if price_source.bar_at(s, as_of) is not None]


def _screened_symbols_for_bar(
    screener_runtime: Optional[Dict[str, Any]], as_of_dt: datetime,
    cache: Optional[Dict[str, List[str]]] = None,
) -> Optional[List[str]]:
    """The dynamic per-day universe of symbols ALLOWED TO ENTER on this bar.

    Returns ``None`` when this run carries no screener (the common case — the gate is then a
    cheap no-op and behaviour is byte-identical to a non-screener run). Otherwise resolves the
    run's effective screener settings against the precomputed metric store AS-OF this bar: the
    LATEST scan date <= the bar (the scan cadence is weekly by default, so the universe holds
    constant between scans). The returned list gates ENTRIES only — open-position management /
    exits are NOT restricted (handled at the call site).

    PERF: the screened set only changes per SCAN DATE (weekly), not per 5-min bar — so (1) the
    as-of scan date is resolved via an O(log n) bisect over the store's memoised sorted scan
    dates (NOT a per-bar ``df['date'] <= day`` object comparison over the whole store, which was
    ~28% of a screener backtest), and (2) the screen for a scan date is computed ONCE and reused
    for every bar in that period via ``cache`` (the engine passes its per-run dict). Without the
    cache (e.g. unit tests) it still returns the correct set, just recomputed each call.

    ``screener_runtime`` = ``{"store": <metric-store dir>, "settings": {screener thresholds}}``;
    the store is memoised per worker by ``load_store``.
    """
    if not screener_runtime:
        return None
    from ba2_providers.screener import metric_store as ms

    store = screener_runtime["store"]
    df = ms.load_store(store)
    days = ms.scan_dates(df, store_key=store)
    i = bisect.bisect_right(days, as_of_dt.strftime("%Y-%m-%d")) - 1
    if i < 0:
        return []
    day = days[i]
    if cache is not None and day in cache:
        return cache[day]
    syms = ms.screen_universe_for_day(df, day, screener_runtime["settings"])
    if cache is not None:
        cache[day] = syms
    return syms


def _to_dt(d: Any) -> datetime:
    """Normalise a date/datetime/str bar key to a tz-naive ``datetime`` for comparison.

    A ``date`` key becomes that day's midnight; a tz-aware datetime is converted to
    naive UTC. Lets daily (date) and intraday (datetime) clocks be range-filtered uniformly.
    """
    if isinstance(d, datetime):
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day)
    if isinstance(d, str):
        return _to_dt(datetime.fromisoformat(d))
    raise TypeError(f"Cannot normalise {d!r} ({type(d)}) to a datetime")


def _as_date(d: Any) -> date:
    """Normalise a datetime/date to a calendar ``date`` (the bar-index key type)."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        return datetime.fromisoformat(d).date()
    raise TypeError(f"Cannot normalise {d!r} ({type(d)}) to a date")


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class _BarDateContext(NamedTuple):
    """The per-bar date-context the schedule check needs, computed ONCE per bar.

    ``_schedule_allows_entry`` ran per expert AND per bar and recomputed
    ``as_of_dt.weekday()`` + ``as_of_dt.strftime("%H:%M")`` every call — on a 5-minute clock
    that strftime alone was a dominant per-bar cost (profiled). Precomputing these once per bar
    in the engine loop and passing the context in removes the redundant work without changing
    which bars are entry bars.

      * ``weekday``: ``as_of_dt.weekday()`` (Mon=0 .. Sun=6) — index into ``_WEEKDAYS``.
      * ``hhmm``: ``"HH:MM"`` of the bar (the intraday ``times`` match key).
      * ``nth_weekday``: the 1-based occurrence of this weekday within its month
        (``(day - 1) // 7 + 1`` -> 1st/2nd/3rd/4th/5th such weekday). Precomputed for a
        future monthly "Nth weekday" schedule mode; the current schedule format has no such
        mode, so it does not (yet) affect gating.
    """
    weekday: int
    hhmm: str
    nth_weekday: int


def _bar_date_context(as_of_dt: datetime) -> _BarDateContext:
    """Compute the per-bar date-context (see ``_BarDateContext``) once for a bar."""
    return _BarDateContext(
        weekday=as_of_dt.weekday(),
        hhmm=as_of_dt.strftime("%H:%M"),
        nth_weekday=(as_of_dt.day - 1) // 7 + 1,
    )


def _schedule_allows_entry(as_of_dt: datetime, schedule: Optional[Dict[str, Any]],
                           is_intraday: bool,
                           ctx: Optional[_BarDateContext] = None) -> bool:
    """Whether ``as_of_dt`` is a scheduled ENTRY bar for an expert.

    Honours the common ``execution_schedule_enter_market`` setting
    ``{"days": {monday..sunday: bool}, "times": ["HH:MM", ...]}``: the expert only
    analyses for NEW positions on enabled weekdays (and, on an intraday clock, only on
    bars whose clock time matches one of ``times`` — so a 5m fill clock still runs the
    expert just once/day). Fills + open-position management run EVERY bar regardless;
    this gate is the "run at" cadence, decoupled from the fill clock.

    A missing/empty schedule means "every bar" (legacy behaviour). On a daily clock the
    ``times`` are ignored (the single daily bar represents the whole session).

    ``ctx`` is the precomputed per-bar date-context (``_bar_date_context(as_of_dt)``). The
    engine builds it ONCE per bar and passes it to every per-expert call so the weekday /
    HH:MM are not recomputed per expert per bar. When omitted (external/legacy callers) it is
    computed on the fly — behaviour is identical either way.
    """
    if not schedule:
        return True
    if ctx is None:
        ctx = _bar_date_context(as_of_dt)
    days = schedule.get("days") or {}
    wd = _WEEKDAYS[ctx.weekday]
    if not days.get(wd, True):
        return False
    if not is_intraday:
        return True
    times = schedule.get("times") or []
    if not times:
        return True
    return ctx.hhmm in set(times)


# ---------------------------------------------------------------------------
# BYPASS experts run their ENTRY pass on entry bars and rebalance through
# FactorRanker's FactorPortfolioManager. (The spec §3.3 seams that let an expert
# declare its own manager class — ``portfolio_manager_classpath`` — and a
# manage-bar exit pass — ``manages_between_entries`` — were deleted 2026-08-31
# with their sole producer, PremiumSeller; option-model plan Task 12.)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Option expiry / exercise / assignment
# ---------------------------------------------------------------------------
def option_expiry_outcome(opt_type, side, *, strike, spot, qty, multiplier=100):
    """THEORETICAL outcome of one option position at expiry. Pure. Long ITM -> exercise;
    short ITM -> assigned; OTM -> worthless. ITM: call when spot>strike, put when spot<strike.

    NOTE: the engine settles through ``BacktestAccount.settle_single_leg_expiry``, which
    applies the no-orphaned-stock backtest policy on top of this theoretical outcome (a
    long ITM is NEVER exercised — always sold to close at the expiry premium/intrinsic;
    shorts always physically assign, with the assigned stock liquidated at the next bar's
    open). This helper remains the pure ITM/side contract."""
    from ba2_common.core.types import OptionRight, OrderDirection
    itm = (spot > strike) if opt_type == OptionRight.CALL else (spot < strike)
    if not itm:
        return {"action": "worthless"}
    long = side == OrderDirection.BUY
    if opt_type == OptionRight.CALL:
        share_side = "buy" if long else "sell"
    else:
        share_side = "sell" if long else "buy"
    return {"action": "exercise" if long else "assigned", "side": share_side,
            "shares": int(qty) * multiplier, "price": float(strike)}


# ---------------------------------------------------------------------------
# Recommendation -> ExpertRecommendation row
# ---------------------------------------------------------------------------
def _recommendation_to_expert_recommendation(
    rec: Any,
    *,
    expert_instance_id: int,
    symbol: str,
    as_of: datetime,
    allow_hold: bool = False,
    subtype: Optional["AnalysisUseCase"] = None,
) -> Optional[int]:
    """Persist a Phase-1 ``Recommendation`` value object as an ``ExpertRecommendation`` row
    in the backtest DB and return its id (or ``None`` if not actionable).

    Mirrors live ``run_analysis`` step 6 (BA2TradePlatform core) which maps the value
    object to an ``ExpertRecommendation`` row. SKIP and HOLD are NOT persisted as actionable
    rows (the live enter loop filters ``recommended_action != HOLD`` and skips SKIP), so the
    engine returns ``None`` for them — they leave the ledger untouched this bar.

    The row's ``instance_id`` MUST equal the ExpertInstance id so the inherited
    ``_create_transaction_for_order`` derives the correct ``Transaction.expert_id`` (which
    the ruleset position conditions and the RM query by).
    """
    if getattr(rec, "skip", False):
        return None
    action = rec.signal
    if action == OrderRecommendation.ERROR:
        return None
    # HOLD is normally not staged (the enter loop skips it), but the OPEN_POSITIONS pass
    # persists it (allow_hold=True) so exit conditions — days_opened / profit_loss_percent /
    # bearish-vs-not — have a recommendation row to read for a held symbol, exactly like the
    # live OPEN_POSITIONS analysis creates one.
    if action == OrderRecommendation.HOLD and not allow_hold:
        return None

    # expected_profit_percent / confidence are required (non-nullable) on the row; the
    # RM prioritises by expected_profit_percent. Live uses 0.0 when the expert leaves it
    # unset, but our clean experts populate it — fall back to 0.0 only if genuinely None.
    expected_profit = rec.expected_profit_percent
    if expected_profit is None:
        expected_profit = 0.0

    row = ExpertRecommendation(
        instance_id=expert_instance_id,
        market_analysis_id=None,
        symbol=symbol,
        recommended_action=action,
        expected_profit_percent=float(expected_profit),
        # The expert's recommended TP price (FMPRating's analyst target etc.); None for
        # experts with no price target -> the bracket falls back to expected_profit_percent.
        target_price=(None if getattr(rec, "target_price", None) is None
                      else float(rec.target_price)),
        price_at_date=float(rec.current_price),
        details=rec.details or "",
        confidence=(None if rec.confidence is None else float(rec.confidence)),
        risk_level=RiskLevel.MEDIUM,
        time_horizon=TimeHorizon.MEDIUM_TERM,
        # Stamp which use-case produced this rec so the OPEN_POSITIONS manage pass can select it
        # by subtype (gap #5). None for un-stamped callers -> the manage selection's all-rec
        # fallback still finds it.
        subtype=subtype,
        data=(dict(rec.raw_outputs) if rec.raw_outputs else None),
        created_at=as_of,
    )
    return add_instance(row)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class DailyBacktestEngine:
    """Daily multi-asset simulator driving the real ba2trade order path.

    Args (keyword-only):
        account: the wired ``BacktestAccount`` (already registered on the resolver).
        experts: list of ``(expert_instance, expert_instance_id, expert_settings, ruleset_id)``
            tuples. ``expert_instance`` is a ba2_experts object (e.g. ``FMPEarningsDrift``)
            registered on the resolver under ``expert_instance_id``; ``expert_settings`` is the
            resolved settings dict fed to ``_process`` (the optimizer-override seam);
            ``ruleset_id`` is the enter_market ruleset to evaluate (seeded in the backtest DB);
            it is ignored (and may be ``None``) for a BYPASS expert that declares
            ``bypasses_classic_rm`` — such an expert rebalances to target weights via its own
            FactorPortfolioManager instead of the enter/exit ruleset + classic RM.
        price_source: the ``AsOfPriceSource`` (the virtual clock + bar store).
        config: the run config dict (validated fail-early by the handler). Required keys read
            here: ``start_date``, ``end_date``, ``enabled_instruments``, ``seed``. Optional:
            ``subtype``.
        progress_cb: ``callable(pct: float, msg: str)`` invoked once per bar (the handler
            wires pause/progress through it). Defaults to a no-op.
        indicator_provider: the injected indicators provider for ATR sizing. Defaults to
            ``make_indicator_provider()`` (the ohlcv/'fmp'-backed pandas indicator calc).
    """

    def __init__(
        self,
        *,
        account: Any,
        experts: List[Tuple[Any, int, Dict[str, Any], int]],
        price_source: Any,
        config: Dict[str, Any],
        progress_cb: Optional[Callable[[float, str], None]] = None,
        indicator_provider: Any = None,
        regime_calendar: Any = None,
    ) -> None:
        self.account = account
        self.experts = experts
        self.price = price_source
        self.config = config
        self.progress_cb = progress_cb or (lambda pct, msg: None)
        self.seed = config["seed"]
        self._indicator_provider = indicator_provider
        # Precomputed benchmark stress flags (ba2_common.core.regime_overlay.StressedCalendar),
        # or None when the benchmark history was unreadable. run() refuses to start in the
        # None case IF any expert enables the overlay -- see _check_regime_calendar.
        self._regime_calendar = regime_calendar
        # Per-day dynamic screener universe (screener-settings optimization). The optimizer's
        # trial config sets ``screener_runtime`` ({"store", "settings"[, "cadence_days"]}); when
        # absent (every non-screener run) this is None and the per-bar entry gate is a no-op, so
        # behaviour is byte-identical to before.
        self._screener_runtime = config.get("screener_runtime")
        # Per-run memo for the screener entry gate: {resolved_scan_date: [symbols]}. The screened
        # set only changes per scan date (weekly cadence), so it's computed once per scan date and
        # reused for every bar in that period (vs recomputing the full-store filter every 5min bar).
        self._screened_cache: Dict[str, List[str]] = {}
        # BYPASS-expert (FactorRanker) per-run manager cache. The portfolio manager
        # holds only run-CONSTANT state (the resolver expert/account instances + ids), so building
        # it ONCE per expert avoids an ExpertInstance DB query on every rebalance bar.
        # (``_bypass_veq_pct`` lived here too until 2026-08-06; it existed solely to feed the
        # per-bar stop pass its equity, and went with it.)
        self._bypass_pm: Dict[int, Any] = {}

        # Entry-option path: when the run's enter_market action IS an option action (pure-option
        # entry, no equity leg), the option action must size + submit itself — so the entry runs
        # with ``submit_to_broker=True`` (like the open-positions path), unlike the equity entry
        # which stages a PENDING qty=0 order the RM sizes next. ONE strategy per run, so a single
        # global flag derived from ``config["entry_action"]`` is sufficient + unambiguous.
        self._entry_is_option = False
        ea = config.get("entry_action")
        if isinstance(ea, dict):
            from ba2_common.core.types import is_option_action
            a = ea.get("action_type") or ea.get("action") or ea.get("option_strategy")
            self._entry_is_option = bool(a and is_option_action(str(a)))

        # The option sleeves whose drawdown breaker this run transitions per bar, as
        # (expert, expert_instance_id). Filled in by ``run()`` from the SAME
        # ``option_risk_manager_enabled`` dispatch the entry gate uses, and EMPTY for every
        # equity run -- which is what keeps the option risk manager out of an equity trial
        # entirely rather than merely making it cheap.
        self._option_sleeves: List[Tuple[Any, int]] = []

        # UNCOVERED-ASSIGNED telemetry (M8): per symbol, the current consecutive streak of
        # bars on which a covered call was asked for and declined, plus the running max and
        # total. See ``_record_uncovered_assigned`` for why this state is worth carrying.
        self._uncovered_assigned_streak: Dict[str, int] = {}
        self._uncovered_assigned_max: Dict[str, int] = {}
        self._uncovered_assigned_total: Dict[str, int] = {}
        self._uncovered_assigned_reasons: Dict[str, str] = {}

    def _bypass_manager(self, expert_id: int) -> Any:
        """Lazily build + cache the portfolio manager for a bypass expert (run-constant).

        Always FactorRanker's FactorPortfolioManager (the per-expert manager-class seam
        went with PremiumSeller, 2026-08-31 — see the module-level note).

        The manager is stable for the whole run; it reads live account state on every call.
        """
        pm = self._bypass_pm.get(expert_id)
        if pm is None:
            from ba2_experts.FactorRanker.portfolio import FactorPortfolioManager

            pm = FactorPortfolioManager(expert_id)
            self._bypass_pm[expert_id] = pm
        return pm

    # -- the loop -----------------------------------------------------------
    def _check_regime_calendar(self) -> None:
        """Refuse to run an overlay-enabled genome with no benchmark history.

        Without this the overlay would degrade to "never stressed" -> every regime_*_scale a
        no-op -> the GA spends a whole grid searching four genes that cannot change an outcome.
        That is precisely how ``use_atr_stop`` was searched for months against dead code, and the
        only defence is to make the missing input fatal instead of quiet.
        """
        from ba2_common.core.regime_overlay import overlay_enabled

        if self._regime_calendar is not None:
            return
        wanted = [eid for expert, eid, _s, _r in self.experts if overlay_enabled(expert)]
        if wanted:
            raise RuntimeError(
                f"regime_overlay_enabled is set on expert instance(s) {wanted} but no benchmark "
                f"regime calendar could be built — the overlay would silently do nothing. "
                f"Pre-cache the benchmark's daily bars (`ba2-test fetch-cache`) and re-run."
            )

    def run(self) -> Dict[str, Any]:
        """Run the full simulation and return a results dict (Task 5 ``build_results`` shape).

        Task 4 returns a minimal results payload (equity_history + filled trades) so the
        loop is independently testable; Task 5's ``build_results`` consumes the SAME account
        (``get_balance_history``/``get_filled_trades``) to produce the final metrics blob.
        """
        # Determinism: seed BEFORE any decision so a run is byte-reproducible.
        random.seed(self.seed)
        np.random.seed(self.seed & 0xFFFFFFFF)

        # ATR injection seam: build once, reuse across bars/experts (also stashed on self so
        # _manage_open_positions can size any Sell orders the exit ruleset produces).
        #
        # PREFER the metric-store-backed ATR cache when this run carries a screener store: the
        # GA/optimize trial-worker path has no hermetic route to a live/as-of-clamped indicator
        # provider (unlike the single-backtest handler's AsOfClampedOHLCVProvider), so the plain
        # make_indicator_provider() fallback below would either hit the network mid-run or
        # silently return no ATR — the cache read is offline, hermetic, and as-of correct (see
        # MetricStoreATRProvider). Falls back to the live provider when no store is configured
        # (unchanged behaviour for static-universe runs).
        indicator_provider = self._indicator_provider
        if indicator_provider is None:
            store = (self._screener_runtime or {}).get("store") if self._screener_runtime else None
            indicator_provider = make_atr_cache_indicator_provider(store) or make_indicator_provider()
        self._indicator_provider = indicator_provider

        self._check_regime_calendar()
        # Start from a clean regime even if a PREVIOUS trial in this worker died mid-loop and
        # never reached its own reset.
        reset_stressed()

        days = trading_days(self.config["start_date"], self.config["end_date"], self.price)
        total = max(len(days), 1)
        # Progress throttle: the handler's progress_cb does DB work every call (a task-queue
        # pause-check + a progress write). On a 5-minute fill clock a 1-year/8-symbol run is
        # ~490k bars, so calling it per bar made progress alone ~36% of runtime (profiled).
        # Emit only when the integer percent advances (<=100 calls) plus the final bar. Progress
        # is side-effect-only, so throttling cannot change results (determinism preserved).
        last_pct = -1

        # Once-per-scheduled-DAY dedup for the expensive analyse+manage pass. On an intraday
        # clock with a weekday schedule but no explicit `times`, _schedule_allows_entry is True
        # for EVERY bar of an enabled day, which re-ran the expert analysis + open-position
        # management ~78x/day (profiled: ~90k date-parses, the dominant 5min cost). We run each
        # SUB-PASS once per (expert, calendar day); the OCO TP/SL fills still run EVERY bar via
        # refresh_orders below, so trade closes stay 5min-precise. Matches live (RM manages on
        # the analysis cadence, fills are continuous). SEPARATE sets per sub-pass: with one
        # shared set, whichever gate fired first in the day claimed the (expert, day) key and
        # STARVED the other pass whenever the entry and manage schedules pin different times
        # (benign while both pin 09:30, but a one-line trap for any future schedule change).
        analyzed_entry_days: set = set()
        analyzed_manage_days: set = set()

        # ----- skip-flat-bars -------------------------------------------------------------------
        # When NOTHING is open and NO order is working, no fill is possible until the next ANALYSIS
        # bar — so jump straight there instead of stepping every intraday bar doing nothing. This is
        # the big 5min win (and bigger still for a dynamic screener universe): a strategy that is
        # flat most of the time collapses ~59k bars to a handful. Trades are UNCHANGED (a fill needs
        # a working order) and Calmar/total-return/maxDD are identical (equity is constant cash while
        # flat). Precompute the analysis-bar indices once (bars where some expert may analyse/enter).
        import bisect as _bisect

        def _to_aware(a: Any) -> datetime:
            if isinstance(a, datetime):
                return a if a.tzinfo else a.replace(tzinfo=timezone.utc)
            return datetime(a.year, a.month, a.day, tzinfo=timezone.utc)

        _scheds = [self._entry_schedule(e) for e, _eid, _s, _r in self.experts]
        _is_intraday = self.price.is_intraday

        def _day_is_analysis(a: Any) -> bool:
            aw = _to_aware(a)
            _ctx = _bar_date_context(aw)  # compute the day's date-context ONCE, reuse per schedule
            return any(_schedule_allows_entry(aw, s, _is_intraday, _ctx) for s in _scheds)

        analysis_idx = [j for j, a in enumerate(days) if _day_is_analysis(a)]

        # THE option-sleeve gate, evaluated ONCE per run rather than once per bar. The check
        # is ``option_risk_manager_enabled`` over ``expert.settings`` -- byte for byte the
        # dispatch ``TradeActions._option_risk_manager`` uses for the ENTRY gate, so the bar
        # loop's breaker and the entry rails engage on exactly the same experts. An equity
        # trial leaves this list EMPTY and therefore makes ZERO calls to the option risk
        # manager per bar (pinned by call count, not by timing). ``risk_manager_mode`` cannot
        # change mid-run, so hoisting the check out of the loop changes no answer -- it only
        # keeps the hot path a single truthiness test.
        self._option_sleeves = [
            (expert, expert_id)
            for expert, expert_id, _settings, _ruleset in self.experts
            if option_risk_manager_enabled(getattr(expert, "settings", None),
                                           expert_instance_id=expert_id)
        ]

        i = 0
        n_days = len(days)
        while i < n_days:
            as_of = days[i]
            # Tz-AWARE UTC clock — the SAME contract the live path assumes: the experts'
            # _process does ``now = as_of or datetime.now(timezone.utc)`` and then subtracts
            # tz-aware report/transaction dates, so a NAIVE as_of would raise
            # "can't subtract offset-naive and offset-aware datetimes". Using aware UTC here
            # makes the backtest clock byte-identical to the live ``datetime.now(timezone.utc)``.
            # A daily key (date) becomes midnight UTC (historical behaviour); an intraday key
            # (datetime) keeps its time component so the bar timestamp is preserved.
            if isinstance(as_of, datetime):
                as_of_dt = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
            else:
                as_of_dt = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)

            # Per-bar date-context (weekday / HH:MM / nth-weekday) computed ONCE here and passed to
            # every per-expert _schedule_allows_entry call below, instead of recomputing weekday +
            # strftime per expert per bar (the dominant per-bar cost on a 5-minute clock).
            _date_ctx = _bar_date_context(as_of_dt)

            # 1. advance the clock + bust the per-account price cache (the gotcha).
            self.price.set_clock(as_of_dt)
            self._bust_price_cache()

            # 1b. publish this bar's market regime ONCE, market-wide. Everything downstream
            #     (TradeRiskManagement sizing/stops, the TP/SL adjust actions) reads it from the
            #     regime_overlay seam instead of classifying per symbol. Cheap: a bisect into the
            #     precomputed calendar. None (no calendar) publishes None = neutral, which
            #     _check_regime_calendar has already proven no expert depends on.
            set_stressed(self._regime_calendar.at(as_of_dt) if self._regime_calendar else None)

            # 2. universe for the bar.
            universe = resolve_universe(as_of_dt, self.config, self.price)

            # 2a. per-day DYNAMIC screener gate (screener-settings optimization). Computed ONCE
            #     per bar from this run's effective screener settings, resolving to the latest
            #     scan date <= the bar (the universe holds between weekly scans). When
            #     ``allowed is not None`` it restricts which symbols may ENTER this bar — the
            #     ENTRY candidate universe fed to ``_run_expert_bar`` is intersected with it,
            #     PRESERVING bar order so determinism is unchanged. Open-position management /
            #     exits are NOT gated: ``_manage_open_positions``, the bypass rebalance, ``_apply_option_expiry`` and the OCO bracket fills all
            #     run over held positions / the full universe regardless. When no screener is
            #     configured ``_screened_symbols_for_bar`` returns None and this is a no-op
            #     (byte-identical to a non-screener run — the hot path is untouched).
            entry_universe = universe
            if self._screener_runtime:
                allowed = _screened_symbols_for_bar(self._screener_runtime, as_of_dt, self._screened_cache)
                if allowed is not None:
                    allowed_set = set(allowed)
                    entry_universe = [s for s in universe if s in allowed_set]

            # The fill engine reads working orders from BacktestAccount's in-memory order cache
            # (no per-bar DB query). That cache only goes stale when this bar CREATES new orders —
            # a bypass stop pass, an expert analysis/management pass, or a post-fill bracket
            # attach. Track that and reload the cache once, right before refresh_orders reads, so
            # the common no-event bars do zero order DB reads.
            book_dirty = False

            # 2.5 REMOVED 2026-08-06 — the per-bar bypass STOP pass used to live here.
            #
            #     It called back into EXPERT code (FactorPortfolioManager.apply_stop_losses) once
            #     per bar to price every held name, compare it against an equity-loss cap, and
            #     submit market sells. That is the exchange's job, not the expert's: a per-bar
            #     price check is precisely what a resting stop order IS, and the simulator already
            #     owns one (``refresh_orders`` fills ``stop_price`` orders every bar, stops first).
            #     Expert code re-implementing it produced a mechanism that existed ONLY in
            #     backtest — live FactorRanker had no stop at all, and 22 open positions across 6
            #     instances sat unprotected at the broker.
            #
            #     FactorRanker now attaches a resting SELL_STOP at entry (``_submit_buy`` ->
            #     ``protective_stop_price``), so Alpaca enforces it live and ``refresh_orders``
            #     fills it here — the same stop, one mechanism, both sides. Reinstating this pass
            #     would DOUBLE-EXIT: the resting stop fills AND this submits a second market sell.
            #
            #     Consequence for results: a stop now fills AT the stop price on the bar that
            #     touches it, not as a market order on the bar after a close-based check. Pre-2026-08-06
            #     FactorRanker backtest numbers are therefore not directly comparable.

            # 3. each expert: analyze_as_of -> persist rec -> ruleset -> RM -> submit.
            #    BYPASS experts (piece 1b): an expert that declares ``bypasses_classic_rm``
            #    (e.g. FactorRanker) does NOT use the enter/exit ruleset OR the classic risk
            #    manager. It emits {symbol: weight} target weights once per bar and rebalances
            #    via its own FactorPortfolioManager — so we route its targets DIRECTLY to the
            #    portfolio manager (which itself prices + submits orders), SKIPPING
            #    TradeActionEvaluator/TradeConditions, TradeRiskManagement and position_sizing.
            for expert, expert_id, settings, ruleset_id in self.experts:
                # Run-cadence gate: only ANALYSE for new positions on the expert's
                # scheduled entry bars (execution_schedule_enter_market). Between run
                # bars the loop still advances — fills + open-position management below
                # run every bar — but the expert no-ops (no new analysis/orders).
                # SEPARATE cadences (mirrors live, which schedules enter_market and
                # open_positions independently): ENTRY runs on the entry schedule (e.g. weekly),
                # MANAGEMENT of open positions on its own schedule (e.g. DAILY). A bar runs the pass
                # if EITHER gate allows; each sub-pass is then guarded by its own gate below.
                entry_ok = _schedule_allows_entry(
                    as_of_dt, self._entry_schedule(expert), self.price.is_intraday, _date_ctx)
                manage_ok = _schedule_allows_entry(
                    as_of_dt, self._manage_schedule(expert), self.price.is_intraday, _date_ctx)
                if not (entry_ok or manage_ok):
                    continue
                # Safety net: if a schedule pins weekdays but no `times`, the gate is True for EVERY
                # intraday bar of the day — run each (expensive) sub-pass at most ONCE per
                # (expert, calendar day) so 5min runs don't re-analyse 78x. (When `times` IS set,
                # only one bar/day passes, so this never triggers.) Dedup PER SUB-PASS: a manage
                # bar earlier in the day must not consume the entry pass's slot (or vice versa).
                _day_key = (expert_id, as_of_dt.date())
                if self.price.is_intraday:
                    if entry_ok and _day_key in analyzed_entry_days:
                        entry_ok = False
                    if manage_ok and _day_key in analyzed_manage_days:
                        manage_ok = False
                    if not (entry_ok or manage_ok):
                        continue
                if entry_ok:
                    analyzed_entry_days.add(_day_key)
                if manage_ok:
                    analyzed_manage_days.add(_day_key)
                book_dirty = True  # an analysis/management pass runs -> orders may be created
                if getattr(expert, "bypasses_classic_rm", False):
                    # Bypass experts rebalance on their ENTRY cadence only (FactorRanker
                    # byte-identical). The manage-bar exit pass (manages_between_entries)
                    # was deleted with its sole producer, PremiumSeller — 2026-08-31.
                    if entry_ok:
                        self._run_bypass_expert_bar(expert, expert_id, settings, as_of_dt)
                    continue
                if getattr(expert, "analyzes_as_basket", False):
                    # Basket experts (e.g. FMPSenateTraderWeight/FMPSenateTraderCopy) call
                    # analyze_as_of ONCE per bar for the whole universe, returning
                    # List[Recommendation] instead of one Recommendation per symbol — but,
                    # UNLIKE a bypass expert, they still go through the full classic pipeline
                    # (TradeActionEvaluator/TradeConditions ruleset eval, one ExpertRecommendation
                    # row per symbol, TradeRiskManagement sizing) per recommendation item. Run
                    # only on entry bars (mirrors the classic per-symbol branch below). NO
                    # `continue` here (unlike the bypass branch above): a basket expert's
                    # OPEN_POSITIONS management is NOT folded into its entry pass the way a
                    # bypass expert's rebalance-IS-the-management is — it must fall through to
                    # the shared `if manage_ok: self._manage_open_positions(...)` below exactly
                    # like the classic per-symbol branch does, or exit conditions (Adjust-TP/SL/
                    # Close/Sell) would never evaluate for a basket expert on ANY bar.
                    if entry_ok:
                        self._run_basket_expert_bar(
                            expert, expert_id, settings, ruleset_id, as_of_dt
                        )
                elif entry_ok:
                    # _run_expert_bar now self-sizes + submits equity entries via the temp-list flow
                    # (_size_and_submit_candidates) — only funded orders are persisted + submitted, so
                    # there is no separate _size_and_submit DB pass over qty=0 rows here anymore.
                    self._run_expert_bar(
                        expert, expert_id, settings, ruleset_id, entry_universe, as_of_dt
                    )

                # Manage EXISTING positions through the OPEN_POSITIONS ruleset (real RM/evaluator) on
                # the MANAGE cadence — identical to live. Adjust-TP/SL/Close/Sell per the exit
                # conditions; no-op when the expert has no open_positions ruleset configured.
                if manage_ok:
                    self._manage_open_positions(expert, expert_id, settings, as_of_dt)

            # The analysis/bypass passes above create orders via ba2_common's DB-backed RM/submit
            # path; reload the account's order cache so the fill engine sees them this bar.
            if book_dirty:
                self.account.invalidate_order_cache()

            # 4..4b. fills on THIS bar, the settlement passes (assignment
            #     liquidation / option expiry / margin call) and the transaction
            #     roll — extracted into _fills_and_settlements so the bar tail is
            #     testable on its own.
            self._fills_and_settlements(as_of_dt)

            # 4c. the option sleeve's drawdown circuit breaker, once per bar, for a
            #     ``classic_options`` expert and no other. Until 2026-09-01 the breaker
            #     TRANSITIONED only in the live tree (``option_lifecycle_service``, off
            #     ``JobManager``), so a backtest never ratcheted the peak, never tripped and
            #     never re-armed: ``RAIL_BREAKER_HALTED`` was unreachable here and a
            #     classic_options backtest was systematically MORE PERMISSIVE than live.
            #     This is the same shared function live calls -- one implementation, two
            #     callers -- and it reads the sleeve's equity through
            #     ``sleeve_true_equity`` -> ``ReadOnlyAccountInterface.true_equity``, NOT
            #     through ``AccountSnapshot.equity`` (review 2026-08-30 dev-merge, FIX 6:
            #     the earlier wording named the snapshot and would invite putting it back).
            #     The distinction is the whole point of the 2026-09-01 fix: on
            #     ``BacktestAccount`` the snapshot resolves to
            #     ``deployed_equity() = min(cap, equity())``, and that clamp is ONE-SIDED --
            #     it compresses peaks and never troughs, so a capped account falling
            #     100k -> 64k reports a 0.0 % drawdown and never stands down. Live, where
            #     there is no cap, ``true_equity`` and the snapshot are the same number and
            #     nothing changes. The sizing rails keep the capped reader
            #     (``sleeve_equity``); only this LOSS measurement looks past it.
            #
            #     HERE, deliberately: after the expiry settlement and the margin-call
            #     liquidation have marked this bar and BEFORE ``snapshot_equity``, so the
            #     breaker measures exactly the equity the reported curve records. The entry
            #     pass (step 3) reads the latch on the NEXT bar, which is the same ordering
            #     live has (the exit pass transitions; the next entry cycle is gated).
            if self._option_sleeves:
                self._update_option_breakers()

            # 5. record per-bar equity / drawdown point.
            self.account.snapshot_equity(as_of_dt)

            # 5a. account wipeout (net_liquidating_value <= 0): a real account can't go
            #     negative, so continuing to simulate further bars/trades on top of a wiped
            #     book produces meaningless numbers (the -1900%-drawdown class of bug). Stop
            #     the run here — results.py/strategy_fitness.py read the flag and invalidate
            #     the trial rather than scoring it.
            if getattr(self.account, "_wiped_out", False):
                self._log(f"account wiped out @ {as_of_dt} — stopping run early")
                break

            pct = (i + 1) / total * 100.0
            pct_i = int(pct)
            if pct_i != last_pct or (i + 1) == total:
                last_pct = pct_i
                self.progress_cb(pct, f"bar {as_of:%Y-%m-%d}")

            # Advance: step to the NEXT bar while there is something to fill (open position or
            # working order); otherwise (flat) jump straight to the next analysis bar.
            if self._has_activity():
                i += 1
            else:
                _k = _bisect.bisect_right(analysis_idx, i)
                i = analysis_idx[_k] if _k < len(analysis_idx) else n_days

        # The published regime is PROCESS-global (one market, one regime) and the GA reuses a
        # worker across individuals -- clear it so the next trial cannot inherit this run's last
        # bar before its own first set_stressed.
        reset_stressed()
        return self._build_minimal_results()

    def _has_activity(self) -> bool:
        """True if a fill is possible next bar: an OPEN position OR a working/waiting order. When
        False the run is flat — the loop can jump to the next analysis bar (no fills until then).
        Cheap: reuses the account's cached order list + positions (no DB round-trip)."""
        try:
            if self.account.get_positions():
                return True
        except Exception:  # noqa: BLE001 — be conservative: unknown -> step densely
            return True
        try:
            from ba2_common.core.types import OrderStatus
            active = set(OrderStatus.get_active_statuses())
            # Any working/waiting order means a fill is still possible. Scan the O(active) working
            # set (the active-status query) rather than materialising EVERY order ever created. The
            # cache may hold instances that went terminal IN PLACE this bar (the active query ran
            # before they filled), so keep the explicit status filter — identical to the old check,
            # just over the small active set instead of the full one.
            return any(getattr(o, "status", None) in active for o in self.account._active_orders())
        except Exception:  # noqa: BLE001
            return True

    # -- run-cadence --------------------------------------------------------
    def _entry_schedule(self, expert: Any) -> Optional[Dict[str, Any]]:
        """The expert's ``execution_schedule_enter_market`` (common base setting), or None.

        An optional ``run_schedule_override`` on the run config wins (so the optimizer can
        drive the cadence as a parameter). None/empty -> every bar (legacy)."""
        override = self.config.get("run_schedule_override")
        if override:
            return override
        try:
            return expert.get_setting_with_interface_default("execution_schedule_enter_market")
        except Exception:  # noqa: BLE001 — a stub/unschedulable expert -> run every bar
            return None

    def _manage_schedule(self, expert: Any) -> Optional[Dict[str, Any]]:
        """The open-positions MANAGEMENT cadence, separate from entry — mirrors live, which
        schedules ``open_positions`` independently (typically far more often, e.g. DAILY).

        A ``manage_schedule_override`` on the run config wins (the optimizer drives it daily); else
        the expert's ``execution_schedule_open_positions``; else falls back to the ENTRY schedule
        (legacy: manage on the same cadence as entry, preserving old single-backtest behaviour)."""
        override = self.config.get("manage_schedule_override")
        if override:
            return override
        try:
            sched = expert.get_setting_with_interface_default("execution_schedule_open_positions")
            if sched:
                return sched
        except Exception:  # noqa: BLE001
            pass
        return self._entry_schedule(expert)

    # -- per-expert, per-bar ------------------------------------------------
    def _run_expert_bar(
        self,
        expert: Any,
        expert_id: int,
        settings: Dict[str, Any],
        ruleset_id: int,
        universe: List[str],
        as_of: datetime,
    ) -> bool:
        """Analyse every universe symbol for one expert and stage PENDING orders.

        Returns True iff at least one PENDING order was created (so the caller knows to run
        the risk manager). Per-symbol failures are logged and skipped (a bad symbol must not
        abort the whole bar) — matching the live loop's per-recommendation try/except.
        """
        from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

        providers = self._provider_bundle()
        created_any = False
        # TEMP-ORDER-LIST FLOW (equity entries): instead of persisting a qty=0 PENDING order per
        # passing symbol and letting the RM size + DELETE the unfunded (churn), we build a transient
        # candidate per passing symbol, size them ALL in one in-memory RM pass, then persist + submit
        # ONLY the funded ones. Each entry: (transient_candidate_order, evaluator, symbol, recommendation).
        equity_candidates: List[Any] = []

        for symbol in universe:
            # The per-symbol expert decision: ``analyze_as_of`` -> ``_gather`` reads
            # ``self._gather_symbol`` (the live ``run_analysis`` sets it before _gather), so
            # the engine must pin the symbol on the shared expert object each iteration.
            # The STUB experts in the unit tests ignore it; the real ba2_experts require it.
            try:
                expert._gather_symbol = symbol
            except Exception:  # noqa: BLE001 — a stub without the attr is fine
                pass
            ctx = BacktestContext(
                providers=providers,
                settings=settings,
                as_of=as_of,
                account=self.account,
                subtype=self.config.get("subtype"),
            )
            try:
                rec = expert.analyze_as_of(as_of, ctx)
            except Exception as e:  # noqa: BLE001 — one symbol must not abort the bar
                # A hermetic cache miss (un-prewarmed data) must ABORT loudly, NOT be silently
                # skipped per-symbol — otherwise a missing pre-warm degrades results invisibly.
                from app.services.backtest.price_source import BacktestCacheMiss
                from ba2_providers.fmp_common import FMPHistoryCacheMiss
                if isinstance(e, (BacktestCacheMiss, FMPHistoryCacheMiss)):
                    raise
                self._log(f"analyze_as_of failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
                continue

            if self._stage_recommendation_candidate(
                rec, expert=expert, expert_id=expert_id, symbol=symbol,
                ruleset_id=ruleset_id, as_of=as_of, equity_candidates=equity_candidates,
            ):
                created_any = True

        # Temp-list RM sizing for the bar's equity candidates: size ALL in one in-memory pass, then
        # persist + submit ONLY the funded (qty>0). Unfunded candidates are never written to the DB
        # (no qty=0 churn, no delete) — the requested order-flow redesign.
        if equity_candidates:
            created_any = self._size_and_submit_candidates(
                expert_id, equity_candidates, as_of) or created_any

        return created_any

    def _stage_recommendation_candidate(
        self,
        rec: Any,
        *,
        expert: Any,
        expert_id: int,
        symbol: str,
        ruleset_id: int,
        as_of: datetime,
        equity_candidates: List[Any],
    ) -> bool:
        """Persist ONE ``Recommendation`` for ``symbol`` and run it through the ruleset/RM gates.

        Extracted from ``_run_expert_bar``'s per-symbol loop body (everything from
        ``_recommendation_to_expert_recommendation`` through the ``equity_candidates.append``
        call) so both the classic per-symbol loop (``_run_expert_bar``) and the basket
        per-list-item loop (``_run_basket_expert_bar``) share the EXACT SAME downstream
        machinery: persist the ``ExpertRecommendation`` row, re-read it DB-attached, evaluate
        the enter ruleset via ``TradeActionEvaluator``, apply the live-parity dup-position and
        equity gates, then either execute an OPTION entry directly or append an EQUITY entry
        candidate to ``equity_candidates`` for the caller's batched RM sizing pass.

        For an OPTION entry this returns True iff an order was actually submitted (mirroring
        ``_run_expert_bar``'s ``created_any`` bump); for an EQUITY entry it always returns False
        (the candidate is appended, not submitted — ``created_any`` for THAT case is driven by
        the caller's own ``_size_and_submit_candidates`` call after the whole loop finishes).

        BEHAVIOR-PRESERVING EXTRACTION: this is a verbatim lift of ``_run_expert_bar``'s old
        per-symbol loop body, INCLUDING its exception-handling shape — ``_recommendation_to_
        expert_recommendation``/``get_instance`` remain UNGUARDED here (any exception there
        propagates to the caller), and only the ``TradeActionEvaluator``-onward section keeps
        its own try/except (logged + skipped). ``_run_expert_bar`` calls this helper exactly as
        it inlined this code before (no new try/except at its call site either) — so the classic
        per-symbol path's behavior for a malformed ``rec`` (e.g. a list fed to a non-basket
        expert, the exact shape Task 1 of the senate-basket-dispatch plan documented) is
        UNCHANGED: it still propagates out of ``_run_expert_bar``/``engine.run()`` uncaught.
        ``_run_basket_expert_bar``'s per-list-item loop is the one that adds a wrapping
        try/except AROUND ITS CALL to this helper (see that method) — because a basket expert
        feeds this helper once per LIST ITEM instead of once per trusted per-symbol
        ``analyze_as_of`` call, and a single malformed item must not crash the whole bar there.
        """
        from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
        from ba2_common.core.db import get_instance as _get_instance

        rec_id = _recommendation_to_expert_recommendation(
            rec, expert_instance_id=expert_id, symbol=symbol, as_of=as_of,
            subtype=AnalysisUseCase.ENTER_MARKET,
        )
        if rec_id is None:
            return False  # SKIP / HOLD / ERROR — nothing to stage.

        # Re-read the persisted row so the evaluator/actions see a DB-attached object
        # carrying its id (BuyAction links the order to expert_recommendation.id).
        recommendation = _get_instance(ExpertRecommendation, rec_id)
        if recommendation is None:
            return False

        try:
            evaluator = TradeActionEvaluator(
                account=self.account,
                instrument_name=symbol,
                existing_transactions=None,
            )
            action_summaries = evaluator.evaluate(
                instrument_name=symbol,
                expert_recommendation=recommendation,
                ruleset_id=ruleset_id,
                existing_order=None,
            )
            if not action_summaries or any("error" in s for s in action_summaries):
                return False  # conditions not met / evaluation error -> no order this symbol.

            # LIVE-PARITY DUP-POSITION GATE (mirrors TradeManager.process_expert_recommendations_
            # after_analysis:1130-1144): after the enter ruleset passes but BEFORE executing,
            # skip if an OPENED/WAITING transaction already exists for this (expert, symbol).
            # The ruleset's has_no_position flag only counts OPENED, so a not-yet-filled WAITING
            # entry from a prior bar could otherwise stack a duplicate the live engine blocks.
            # (No-op for standard strategies whose entries fill before the next analysis bar.)
            if self._has_open_or_waiting_position(expert_id, symbol):
                self._log(f"entry dup-gate: {symbol} already OPENED/WAITING for expert "
                          f"{expert_id} @ {as_of:%Y-%m-%d} — skip")
                return False

            # LIVE-PARITY EQUITY GATE (mirrors TradeManager.process_expert_recommendations_
            # after_analysis:1146-1155): before staging an entry, skip if the expert lacks
            # sufficient available equity (available_balance >= minimum_equity_threshold_percent
            # of virtual balance, default 5%). Calls the SAME shared
            # MarketExpertInterface.has_sufficient_equity_for_trading the live path uses — no
            # re-implementation. Note: BacktestAccount.get_balance() is cash (not NLV), which is
            # the same balance BT sizing already uses (TradeRiskManagement), so the gate stays
            # consistent with BT sizing. Stub experts without the method are treated as allowed.
            equity_check = getattr(expert, "has_sufficient_equity_for_trading", None)
            if callable(equity_check):
                try:
                    ok_equity, equity_reason = equity_check()
                except Exception as e:  # noqa: BLE001 — a wiring gap must not silently over-block
                    self._log(f"equity-gate check errored for {symbol} @ {as_of:%Y-%m-%d} "
                              f"(treating as allowed): {e}")
                    ok_equity = True
                if not ok_equity:
                    self._log(f"entry equity-gate: {symbol} skipped for expert {expert_id} "
                              f"@ {as_of:%Y-%m-%d} — {equity_reason}")
                    return False

            if self._entry_is_option:
                # OPTION entry: the option action sizes + submits ITSELF in execute (no equity
                # leg, no RM candidate sizing) — submit directly, like the open-positions path.
                results = evaluator.execute(submit_to_broker=True)
                return any(r.get("success") and (r.get("data") or {}).get("order_id") for r in results)

            # EQUITY entry: stage a TRANSIENT candidate (NOT persisted) for the temp-list RM
            # pass via the shared trade_cycle builder (same shape live uses).
            from ba2_common.core.trade_cycle import build_entry_candidate
            candidate = build_entry_candidate(recommendation, self.account.id)
            equity_candidates.append((candidate, evaluator, symbol, recommendation))
            return False
        except Exception as e:  # noqa: BLE001
            self._log(f"ruleset eval/execute failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
            return False

    # -- basket-dispatch experts (analyzes_as_basket) ------------------------
    def _run_basket_expert_bar(
        self,
        expert: Any,
        expert_id: int,
        settings: Dict[str, Any],
        ruleset_id: int,
        as_of: datetime,
    ) -> bool:
        """Run ONE bar for a BASKET expert (``analyzes_as_basket = True``, e.g.
        ``FMPSenateTraderWeight``/``FMPSenateTraderCopy``): ``analyze_as_of`` is called ONCE for
        the bar and returns ``List[Recommendation]`` (one per qualifying symbol), instead of
        being called once per universe symbol and returning a single ``Recommendation``.

        UNLIKE a BYPASS expert (``bypasses_classic_rm``), a basket expert KEEPS the full classic
        pipeline per symbol — ``TradeActionEvaluator``/``TradeConditions`` ruleset evaluation,
        one persisted ``ExpertRecommendation`` row per symbol, ``TradeRiskManagement`` position
        sizing — nothing about position sizing or ruleset evaluation changes; only HOW MANY
        symbols' worth of ``Recommendation`` come out of one ``analyze_as_of`` call. Each item
        in the returned list is fed through the EXACT SAME ``_stage_recommendation_candidate``
        helper ``_run_expert_bar``'s per-symbol loop uses.

        Because there is only ONE ``analyze_as_of`` call for the whole bar, a raised exception
        from ``analyze_as_of`` itself aborts the WHOLE bar for this expert — there is no
        "skip this symbol, try the next" for the GATHER step any more (that granularity only
        exists once the list comes back). A hermetic cache miss still re-raises (must abort
        loudly, matching every other analyze_as_of call site in this engine). Each
        recommendation ITEM returned by the list, however, is staged through
        ``_stage_recommendation_candidate``, which has its own try/except — a single
        malformed/bad list item must not crash the whole bar (see the senate-basket-dispatch
        plan's Task 1 finding: an earlier UNGUARDED per-item
        ``_recommendation_to_expert_recommendation`` call crashed ``engine.run()`` outright,
        after only the first symbol, when fed a list — this method's whole point is to not
        reproduce that bug in the new dispatch mode).

        Each list item MUST self-identify its symbol via ``rec.raw_outputs["symbol"]`` (there is
        no ``for symbol in universe:`` loop here to infer it from). A missing/falsy symbol is
        logged and that item is skipped — never guessed.

        Returns True iff at least one PENDING order was created, mirroring ``_run_expert_bar``'s
        return contract.
        """
        ctx = BacktestContext(
            providers=self._provider_bundle(),
            settings=settings,
            as_of=as_of,
            account=self.account,
            subtype=self.config.get("subtype"),
        )
        try:
            recs = expert.analyze_as_of(as_of, ctx)
        except Exception as e:  # noqa: BLE001 — the whole bar aborts (no per-symbol granularity
                                 # left at the gather step for a basket expert)
            from app.services.backtest.price_source import BacktestCacheMiss
            from ba2_providers.fmp_common import FMPHistoryCacheMiss
            if isinstance(e, (BacktestCacheMiss, FMPHistoryCacheMiss)):
                raise
            self._log(f"basket analyze_as_of failed for expert {expert_id} @ {as_of:%Y-%m-%d}: {e}")
            return False

        # TYPE GUARD: a basket expert's analyze_as_of MUST return List[Recommendation] (one per
        # qualifying symbol), not a single Recommendation. A single Recommendation is truthy, so
        # `if not recs:` alone would NOT catch it here, and `for rec in recs:` would then raise an
        # uncaught TypeError ('Recommendation' object is not iterable) that propagates out of
        # engine.run(), aborting the whole run -- reproducing, one layer up, the exact failure
        # class Task 1 of the senate-basket-dispatch plan documented (a shape mismatch between
        # what analyze_as_of returns and what the caller expects, left unguarded). This is a real
        # risk for a future dual-mode analyze_as_of (Task 5's planned symbol-pinned vs.
        # basket-mode branching) that could accidentally take the wrong branch. Log + skip the
        # bar, same "log+skip, don't crash the run" convention every other failure mode in this
        # method already uses.
        if not isinstance(recs, list):
            self._log(f"basket analyze_as_of for expert {expert_id} @ {as_of:%Y-%m-%d} must "
                      f"return a list, got {type(recs).__name__} — skipping bar")
            return False

        if not recs:
            return False

        created_any = False
        equity_candidates: List[Any] = []
        for rec in recs:
            try:
                raw = getattr(rec, "raw_outputs", None) or {}
                symbol = raw.get("symbol")
            except Exception as e:  # noqa: BLE001 — a malformed list item must not crash the bar
                self._log(f"basket recommendation item malformed for expert {expert_id} "
                          f"@ {as_of:%Y-%m-%d}: {e}")
                continue
            if not symbol:
                self._log(f"basket recommendation missing raw_outputs['symbol'] for expert "
                          f"{expert_id} @ {as_of:%Y-%m-%d} — skip")
                continue

            # PER-ITEM GUARD (the correction to the plan's Step 3 text — see the class docstring
            # above and the senate-basket-dispatch plan's Task 1 finding): unlike
            # ``_run_expert_bar``'s call into ``_stage_recommendation_candidate`` (left UNGUARDED,
            # matching its pre-extraction behavior), THIS call site wraps the helper because a
            # basket expert feeds it once per LIST ITEM rather than once per trusted per-symbol
            # ``analyze_as_of`` call. ``_stage_recommendation_candidate`` itself leaves
            # ``_recommendation_to_expert_recommendation``/``get_instance`` unguarded (see its
            # docstring), so without this wrapper a single malformed item (e.g. missing
            # ``.signal``) would still crash the WHOLE bar for every other qualifying symbol —
            # reproducing Task 1's bug in a new location instead of fixing it. A hermetic cache
            # miss still re-raises (must abort loudly); everything else is logged + skipped.
            try:
                if self._stage_recommendation_candidate(
                    rec, expert=expert, expert_id=expert_id, symbol=symbol,
                    ruleset_id=ruleset_id, as_of=as_of, equity_candidates=equity_candidates,
                ):
                    created_any = True
            except Exception as e:  # noqa: BLE001 — one bad list item must not abort the whole bar
                from app.services.backtest.price_source import BacktestCacheMiss
                from ba2_providers.fmp_common import FMPHistoryCacheMiss
                if isinstance(e, (BacktestCacheMiss, FMPHistoryCacheMiss)):
                    raise
                self._log(f"basket item staging failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
                continue

        if equity_candidates:
            created_any = self._size_and_submit_candidates(
                expert_id, equity_candidates, as_of) or created_any

        return created_any

    # -- open-positions management (live-identical, packaged evaluator) ------
    def _manage_open_positions(
        self,
        expert: Any,
        expert_id: int,
        settings: Dict[str, Any],
        as_of: datetime,
    ) -> None:
        """Evaluate the expert's OPEN_POSITIONS ruleset for each held position on an analysis bar.

        A faithful, thin mirror of the live
        ``TradeManager.process_open_positions_recommendations``: for every symbol this expert
        currently holds, run a fresh OPEN_POSITIONS-subtype analysis, persist the recommendation
        (even HOLD), then drive the SAME packaged ``TradeActionEvaluator`` (open_positions use
        case, ``existing_transactions=...``) + ``execute()`` — so Adjust-TP/Adjust-SL/Close/Sell
        actions are produced by the real RM/action code, not re-implemented here. The engine only
        provides the loop (it cannot import the live TradeManager). RM sizing runs afterwards for
        any pending (Sell) orders; Adjust/Close act directly on the account.
        """
        from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
        from ba2_common.core.db import get_instance as _get_instance
        from ba2_common.core.models import ExpertInstance
        from ba2_common.core.types import AnalysisUseCase

        instance = _get_instance(ExpertInstance, expert_id)
        open_ruleset_id = getattr(instance, "open_positions_ruleset_id", None) if instance else None
        if not open_ruleset_id:
            return

        held = self._held_transactions(expert_id)  # {symbol: [Transaction, ...]}
        if not held:
            return

        providers = self._provider_bundle()
        created_any = False
        for symbol, txns in held.items():
            try:
                expert._gather_symbol = symbol
            except Exception:  # noqa: BLE001
                pass
            ctx = BacktestContext(
                providers=providers, settings=settings, as_of=as_of,
                account=self.account, subtype=AnalysisUseCase.OPEN_POSITIONS,
                # Basket experts (analyzes_as_basket=True, e.g. FMPSenateTraderWeight/Copy)
                # dual-mode dispatch analyze_as_of on context.extra["symbol"] alone -- the
                # attribute pin above is the OLD convention their dispatch no longer reads.
                # Without this, every OPEN_POSITIONS call for a basket expert silently took
                # the basket branch and returned List[Recommendation] where this loop expects
                # one Recommendation, crashing at _recommendation_to_expert_recommendation's
                # `rec.signal` (confirmed: killed every trial in the 2026-07-19 Senate matrix
                # run once a trial opened its first position).
                extra={"symbol": symbol},
            )
            try:
                rec = expert.analyze_as_of(as_of, ctx)
            except Exception as e:  # noqa: BLE001 — one symbol must not abort the bar
                # A hermetic cache miss (un-prewarmed data) must ABORT loudly, NOT be silently
                # skipped per-symbol — otherwise a missing pre-warm degrades results invisibly.
                # Mirrors the ENTER_MARKET path's handling (see above); this OPEN_POSITIONS
                # path had been missing it (found 2026-07-15 while auditing the senate
                # trader-skill feature's hermeticity).
                from app.services.backtest.price_source import BacktestCacheMiss
                from ba2_providers.fmp_common import FMPHistoryCacheMiss
                if isinstance(e, (BacktestCacheMiss, FMPHistoryCacheMiss)):
                    raise
                self._log(f"open-pos analyze failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
                continue
            rec_id = _recommendation_to_expert_recommendation(
                rec, expert_instance_id=expert_id, symbol=symbol, as_of=as_of, allow_hold=True,
                subtype=AnalysisUseCase.OPEN_POSITIONS,
            )
            if rec_id is None:
                continue
            recommendation = _get_instance(ExpertRecommendation, rec_id)
            if recommendation is None:
                continue
            existing_order = self._oldest_entry_order(txns)
            try:
                evaluator = TradeActionEvaluator(
                    account=self.account, instrument_name=symbol, existing_transactions=txns
                )
                summaries = evaluator.evaluate(
                    instrument_name=symbol, expert_recommendation=recommendation,
                    ruleset_id=open_ruleset_id, existing_order=existing_order,
                )
                if not summaries or any("error" in s for s in summaries):
                    continue
                # submit_to_broker=True (matches live process_open_positions_recommendations with
                # allow_automated_trade_modification): Close/Adjust-TP/SL act DIRECTLY on the
                # position/legs (no RM sizing); a Sell that stages a PENDING order is sized below.
                results = evaluator.execute(submit_to_broker=True)
                self._record_uncovered_assigned(symbol, results, as_of)
                if any(r.get("success") and (r.get("data") or {}).get("order_id") for r in results):
                    created_any = True
            except Exception as e:  # noqa: BLE001
                self._log(f"open-pos eval/execute failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
                continue

        if created_any:
            self._size_and_submit(expert_id, self._indicator_provider, as_of)

    #: Consecutive bars a symbol has been UNCOVERED-ASSIGNED before this is shouted about.
    #: Not a liquidation trigger -- selling assigned shares is a strategy decision the
    #: operator owns -- just the point at which a WARNING per bar stops being enough.
    UNCOVERED_ASSIGNED_ALARM_BARS = 5

    def _record_uncovered_assigned(self, symbol: str, results: Any, as_of: datetime) -> None:
        """Count the bars on which the wheel wanted a covered call and did not get one.

        THE STATE THIS MEASURES is a reachable steady one, and it is silent by construction
        without this: on a bar holding assigned shares with no written call, ``cc_sell``
        MATCHES (its trigger is ``has_assigned_shares``), so ``wheel_stock_guard`` halts the
        ruleset behind it and no other rule can act. The assignment liquidation is a no-op
        under ``hold_assigned_stock``, the lot carries no bracket, and there is no end-of-run
        flatten -- so if ``SellCoveredCallAction`` declines, the sleeve holds naked-long stock
        for as long as the decline persists.

        RECORDED, NOT SCORED. It lands in the results payload beside the other telemetry so a
        run can be read for it afterwards; nothing here feeds fitness, because a wheel that
        cannot write a call this week is not thereby a worse genome -- it is a genome whose
        result needs explaining.
        """
        from ba2_common.core.TradeActions import COVERED_CALL_DECLINE_KEY

        reason = next((str((r.get("data") or {}).get(COVERED_CALL_DECLINE_KEY))
                       for r in (results or [])
                       if isinstance(r, dict) and (r.get("data") or {}).get(
                           COVERED_CALL_DECLINE_KEY)), None)
        if reason is None:
            # A call was written (or nothing asked for one): the streak, if any, is over.
            self._uncovered_assigned_streak.pop(symbol, None)
            return
        streak = self._uncovered_assigned_streak.get(symbol, 0) + 1
        self._uncovered_assigned_streak[symbol] = streak
        self._uncovered_assigned_total[symbol] = (
            self._uncovered_assigned_total.get(symbol, 0) + 1)
        self._uncovered_assigned_max[symbol] = max(
            self._uncovered_assigned_max.get(symbol, 0), streak)
        self._uncovered_assigned_reasons[symbol] = reason
        if streak == self.UNCOVERED_ASSIGNED_ALARM_BARS:
            # ONCE, on the bar it crosses -- not every bar after, which would bury it.
            logger.error(
                f"[daily_engine] {symbol} has held ASSIGNED SHARES with NO covered call for "
                f"{streak} consecutive bars (as of {as_of:%Y-%m-%d}); latest reason: "
                f"{reason}. Nothing in the ruleset can act on this position while it lasts. "
                f"No liquidation is taken -- that is a strategy decision.")

    def _uncovered_assigned_metric(self) -> Dict[str, Any]:
        """The results-payload shape. Empty dicts for a run that never held assigned stock,
        so an equity run's payload gains nothing but the key."""
        return {
            "max_consecutive": max(self._uncovered_assigned_max.values(), default=0),
            "total_bars": sum(self._uncovered_assigned_total.values()),
            "by_symbol": {
                sym: {"max_consecutive": self._uncovered_assigned_max.get(sym, 0),
                      "total_bars": count,
                      "last_reason": self._uncovered_assigned_reasons.get(sym)}
                for sym, count in sorted(self._uncovered_assigned_total.items())
            },
        }

    def _held_transactions(self, expert_id: int) -> Dict[str, List[Any]]:
        """{symbol: [OPENED Transaction, ...]} for this expert.

        OPENED only (not WAITING): the backtest enters with MARKET orders that fill on the NEXT
        bar, so a WAITING transaction is just THIS bar's freshly-created, un-filled entry —
        managing it now would cancel the entry before it ever opens. Live includes WAITING
        because there a limit entry can genuinely sit working; here only a filled position is a
        real open position to manage.

        Excludes a transaction that already has a closing order WORKING (submitted on an
        earlier bar, not yet filled/canceled) — see
        ``ReadOnlyAccountInterface.has_pending_closing_order`` (shared with live's
        ``TradeManager.process_open_positions_recommendations``). Without this, exit rules get
        re-evaluated on this transaction every bar the close is still in flight and can submit
        ANOTHER closing order for the same position; each one credits cash for contracts that
        may already be gone, compounding into runaway equity (the 2026-07-21 options-grid
        trillion-scale fitness anomaly).
        """
        from ba2_common.core.trade_store import transactions_where
        from ba2_common.core.types import TransactionStatus

        out: Dict[str, List[Any]] = {}
        rows = transactions_where(expert_id=expert_id, status=TransactionStatus.OPENED)
        for t in rows:
            if self.account.has_pending_closing_order(t.id):
                continue
            out.setdefault(t.symbol, []).append(t)
        return out

    def _has_open_or_waiting_position(self, expert_id: int, symbol: str) -> bool:
        """True if this expert already holds an OPENED or WAITING transaction for ``symbol``.

        The live-parity duplicate-position gate (mirrors TradeManager's enter_market safety
        check, which queries OPENED+WAITING). Unlike ``_held_transactions`` (OPENED-only, for
        MANAGEMENT), the dup gate must also count a WAITING (not-yet-filled) entry so a second
        entry can't stack on it before it fills — exactly as live blocks it."""
        from ba2_common.core.trade_store import transactions_where
        from ba2_common.core.types import TransactionStatus

        rows = transactions_where(
            expert_id=expert_id, symbol=symbol,
            statuses=[TransactionStatus.OPENED, TransactionStatus.WAITING])
        return len(rows) > 0

    def _oldest_entry_order(self, txns: List[Any]) -> Optional[Any]:
        """The FILLED entry order of the oldest transaction (for DaysOpened-style conditions)."""
        if not txns:
            return None
        oldest = min(txns, key=lambda t: t.open_date or t.created_at or datetime.max.replace(tzinfo=timezone.utc))
        return self.account._entry_order_for_transaction(oldest)

    def _run_bypass_expert_bar(
        self,
        expert: Any,
        expert_id: int,
        settings: Dict[str, Any],
        as_of: datetime,
    ) -> None:
        """Run ONE bar for a BYPASS expert (piece 1b): rebalance to target weights.

        A bypass expert (``getattr(expert, 'bypasses_classic_rm', False)`` is True, e.g.
        FactorRanker) resolves its OWN universe internally, so ``analyze_as_of`` is called
        ONCE for the bar (not per-symbol). The returned recommendation carries
        ``raw_outputs['targets']`` — the ``{symbol: weight}`` book — which is routed DIRECTLY
        through ``FactorPortfolioManager(expert_id).rebalance(targets)``. That manager prices
        each name off the account, diffs the targets against the expert's current holdings, and
        calls ``account.submit_order`` for each delta. The classic decision path is SKIPPED in
        full: NO TradeActionEvaluator/TradeConditions, NO ExpertRecommendation row, NO
        TradeRiskManagement / position_sizing.

        A skip / empty-targets recommendation is a no-op for the bar (nothing to rebalance).
        A per-bar failure is logged and swallowed (one bad bar must not abort the run) — EXCEPT
        a hermetic cache miss (un-prewarmed data), which ABORTS loudly instead: silently
        swallowing it would degrade results invisibly rather than surfacing the missing
        prewarm. Matches the classic (ENTER_MARKET) path's per-bar try/except exactly.
        """
        ctx = BacktestContext(
            providers=self._provider_bundle(),
            settings=settings,
            as_of=as_of,
            account=self.account,
            subtype=self.config.get("subtype"),
        )
        try:
            rec = expert.analyze_as_of(as_of, ctx)
        except Exception as e:  # noqa: BLE001 — one bar must not abort the run
            from app.services.backtest.price_source import BacktestCacheMiss
            from ba2_providers.fmp_common import FMPHistoryCacheMiss
            if isinstance(e, (BacktestCacheMiss, FMPHistoryCacheMiss)):
                raise
            self._log(f"bypass analyze_as_of failed @ {as_of:%Y-%m-%d}: {e}")
            return

        if getattr(rec, "skip", False):
            return
        raw = getattr(rec, "raw_outputs", None) or {}
        targets = raw.get("targets")
        if not targets:
            return  # no target weights this bar -> nothing to rebalance.

        try:
            # Reuse the run-constant portfolio manager (built once; see _bypass_manager).
            self._bypass_manager(expert_id).rebalance(targets)
        except Exception as e:  # noqa: BLE001 — a rebalance failure must not kill the run
            self._log(f"bypass rebalance failed for expert {expert_id} @ {as_of:%Y-%m-%d}: {e}")

    # -- option expiry / exercise / assignment ------------------------------
    def _fills_and_settlements(self, as_of_dt: datetime) -> None:
        """Steps 4..4b of one bar: fills, settlement passes, and the transaction roll.

        Extracted verbatim from ``run()`` so the bar tail is testable on its own.
        """
        # 4. fills on THIS bar's working orders; roll order state into transactions.
        #     A transaction changes state when one of its orders fills, when a bracket
        #     crosses, OR when an option DAY-limit EXPIRES unfilled (refresh_orders folds
        #     the expiry sweep into its signal — the roll is what releases the parent
        #     WAITING Transaction so the dup gate frees the symbol; F1). On a bar with none
        #     of those the roll + bracket pass are no-ops; on a 5-minute fill clock almost
        #     every bar qualifies, and the roll (incl. the ba2_common base
        #     sync_transaction_orders) + bracket pass were ~half of per-bar runtime
        #     (profiled), so gate them on the change signal.
        filled = self.account.refresh_orders()
        if filled:
            self.account.refresh_transactions()

        # Settlement change signal (review 2026-08-30 F8). The three passes below run
        # AFTER the roll above and write synthetic FILLED orders of their own — the
        # assignment paths in particular book a closing fill on an offsetting EQUITY
        # transaction and rely on the roll to CLOSE it. Gating the roll on ``filled``
        # alone left that transaction OPENED on a quiet bar until any later fill
        # anywhere, and ``_has_open_or_waiting_position`` locked the symbol out of
        # re-entry for an arbitrary time (O_CC/O_WHEEL arms). Mirror of the F1
        # precedent: each pass reports whether it changed the book, and a second roll
        # runs after 4a-bis when any did. A fills-only bar keeps the single roll above.
        settled = False

        # 4a-pre. Post-assignment liquidation from a PRIOR bar's expiry: a short option
        #     is always PHYSICALLY assigned, and the assigned stock is unmanaged in a
        #     backtest — close ALL of it at THIS bar's open (before this bar's expiries
        #     settle). One dict check when nothing is pending; equity-only runs never
        #     schedule any.
        #
        #     THIS RUNS AFTER THE MANAGE PASS (step 3), which is why the wheel needs
        #     ``hold_assigned_stock``: the overlay writes a covered call against the
        #     assigned shares in step 3 and this would sell them on the same bar,
        #     leaving a naked short call. With the switch on nothing is ever scheduled,
        #     so this stays the same single dict check.
        if hasattr(self.account, "process_pending_assignment_liquidations"):
            try:
                if self.account.process_pending_assignment_liquidations():
                    settled = True
            except Exception as e:  # noqa: BLE001 — cleanup failure must not abort the run
                self._log(f"assignment liquidation failed @ {as_of_dt}: {e}")

        # 4a. resolve any option positions reaching expiry on THIS bar (no-orphaned-stock
        #     backtest policy — see BacktestAccount.settle_single_leg_expiry): OTM ->
        #     worthless; ITM long -> NEVER exercised, sold to close at the expiry
        #     premium (intrinsic fallback) — no shares; ITM short -> ALWAYS physically
        #     assigned (shares at the strike), with the assigned stock liquidated at
        #     the next bar's open (4a-pre above).
        #     Defined-risk combos unit-settle as a group instead. Runs after the
        #     transaction roll (so freshly-OPENED option positions are visible) and before
        #     snapshot_equity (so the resulting equity position is marked this bar).
        #     Date-driven (an option can expire on a no-fill bar), so it runs every bar —
        #     but get_option_positions() short-circuits to [] for equity-only runs (no
        #     options provider), so this is ~free there. Early American assignment is NOT
        #     modelled — options resolve at expiry.
        if self._apply_option_expiry(as_of_dt):
            settled = True

        # 4a-bis. Broker-style maintenance-margin check + forced liquidation. After marking
        #     this bar, if net-liquidating-value has fallen below the book's maintenance-margin
        #     requirement (or below zero), force-close the unbounded SHORT exposure at the
        #     current bar so equity cannot blow arbitrarily negative (the -256% drawdown).
        #     Gated behind the account's own breach check (no work on healthy bars), so it adds
        #     no per-bar DB churn on the common no-breach path. Runs BEFORE snapshot_equity so
        #     the bounded post-liquidation equity is what the curve records.
        if getattr(self.account, "supports_options", False) and hasattr(
            self.account, "maybe_margin_call_liquidation"
        ):
            try:
                if self.account.maybe_margin_call_liquidation():
                    settled = True
                    self.account.invalidate_order_cache()
            except Exception as e:  # noqa: BLE001 — a liquidation failure must not abort the run
                self._log(f"margin-call liquidation failed @ {as_of_dt}: {e}")

        # 4b. (removed) The engine no longer attaches a baseline "Position protection" TP/SL
        #     bracket on entry. Exits are driven SOLELY by the strategy's exit conditions
        #     (adjust_take_profit / adjust_stop_loss / close / sell), evaluated by the SAME
        #     shared engine the LIVE platform uses — where TP/SL are CREATED on demand when an
        #     adjust rule fires (AlpacaAccount.adjust_tp_sl creates/updates), not pre-bracketed.
        #     A strategy with no exit conditions therefore holds (matches live). If the roll
        #     touched orders this bar, reload the cache so the next bar's fill engine sees them.
        # F8: the settlement passes wrote synthetic FILLED orders — roll them into
        # their transactions NOW (same bar), not on the next unrelated fill. After
        # 4a-bis so ONE roll covers all three passes; a fills-only bar keeps the single
        # roll at the top (no double roll), and a no-event bar still rolls nothing.
        if settled:
            self.account.refresh_transactions()
        if filled or settled:
            self.account.invalidate_order_cache()

    def _apply_option_expiry(self, as_of: datetime) -> bool:
        """Resolve every held option position that has reached its expiry.

        Returns True when at least one position/combo actually settled (synthetic
        orders/transactions were written) — ``_fills_and_settlements`` feeds this into
        the transaction-roll change signal (review 2026-08-30 F8).

        For each held option whose ``expiry <= as_of.date()`` the engine reads the
        underlying's bar CLOSE; defined-risk combos unit-settle as a group, everything else
        settles per leg via ``BacktestAccount.settle_single_leg_expiry`` (the
        no-orphaned-stock backtest policy):

          * worthless -> close the option transaction at premium 0 (realise the entry P&L).
          * ITM long -> NEVER exercised: sold to close at the expiry bar's premium close
            (intrinsic fallback) — no share position, cash credited at the premium.
          * ITM short -> ALWAYS physically assigned (shares at the STRIKE); the assigned
            stock is unmanaged in a backtest, so it is liquidated in full at the next
            bar's open (``process_pending_assignment_liquidations``) — unless the run sets
            ``hold_assigned_stock``, in which case the shares are HELD for the strategy's
            own rules to manage (the wheel's covered-call overlay).

        Early American assignment is NOT modelled — options resolve at expiry only.

        A missing underlying close skips the position (logged). Per-position failures are
        caught + logged so one bad expiry cannot abort the run (matching the per-bar style).
        """
        as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
        settled_any = False
        positions = self.account.get_option_positions()

        # DEFINED-RISK multi-leg combos (butterfly / verticals / iron condor) must settle as a
        # UNIT — leg-by-leg share assignment does NOT preserve the combo's bounded payoff and
        # blows equity past defined risk (the -473%/-171% O_BF blow-up). Group the EXPIRING legs
        # by their transaction and settle each defined-risk combo once via the net-payoff path;
        # everything else (single-leg + undefined-risk legs) keeps the per-leg share path below.
        combo_groups: Dict[Any, list] = {}
        per_leg: list = []
        for pos in positions:
            if pos.expiry is None or pos.expiry > as_of_date:
                continue
            strat = None
            try:
                strat = self.account.defined_risk_combo_strategy(pos)
            except Exception:  # noqa: BLE001 — classification failure -> fall back to per-leg
                strat = None
            if strat is not None:
                txn = self.account._option_transaction_for_contract(pos.contract_symbol)
                key = getattr(txn, "id", None)
                if key is not None:
                    combo_groups.setdefault(key, []).append(pos)
                    continue
            per_leg.append(pos)

        # Unit-settle each defined-risk combo once.
        for legs in combo_groups.values():
            try:
                spot = self.price.close_at(legs[0].underlying)
                if spot is None:
                    self._log(
                        f"option expiry: no underlying close for {legs[0].underlying} "
                        f"(combo {legs[0].contract_symbol}) @ {as_of_date} — skipped"
                    )
                    continue
                if self.account.settle_defined_risk_combo_expiry(legs, float(spot)):
                    settled_any = True
            except Exception as e:  # noqa: BLE001 — one bad expiry must not abort the run
                self._log(f"combo option expiry failed @ {as_of_date}: {e}")

        for pos in per_leg:
            try:
                spot = self.price.close_at(pos.underlying)
                if spot is None:
                    self._log(
                        f"option expiry: no underlying close for {pos.underlying} "
                        f"({pos.contract_symbol}) @ {as_of_date} — skipped"
                    )
                    continue
                # The account applies the no-orphaned-stock backtest policy (long ITM ->
                # sell-to-close, never exercise / short ITM -> physical assignment with the
                # stock liquidated at the next bar's open) — see
                # BacktestAccount.settle_single_leg_expiry.
                if self.account.settle_single_leg_expiry(pos, float(spot)):
                    settled_any = True
            except Exception as e:  # noqa: BLE001 — one bad expiry must not abort the run
                self._log(
                    f"option expiry failed for {pos.contract_symbol} @ {as_of_date}: {e}"
                )
        return settled_any

    def _update_option_breakers(self) -> None:
        """Transition every ``classic_options`` sleeve's drawdown breaker for this bar.

        ONE line of real work per sleeve, and it is a call into the SHARED
        ``OptionRiskManagement.update_sleeve_breaker`` -- the function the live exit pass
        calls. The engine deliberately owns no breaker arithmetic of its own: a backtest-only
        copy of the transition is precisely the divergence this wiring exists to remove, and
        ``test_the_backtest_engine_carries_no_option_risk_manager_of_its_own`` fails if one
        appears here.

        Never aborts the run. A sleeve whose equity cannot be read leaves the breaker BLIND
        (``update_breaker`` says so and refuses to report a drawdown it did not measure), and
        an unexpected failure is logged and the bar continues -- a bookkeeping fault must not
        invalidate a trial that has already traded.
        """
        for expert, expert_id in self._option_sleeves:
            try:
                update_sleeve_breaker(expert=expert, account=self.account,
                                      expert_instance_id=expert_id)
            except Exception as e:  # noqa: BLE001 — see the docstring
                self._log(f"option breaker update failed for expert {expert_id}: {e}")

    def _size_and_submit(self, expert_id: int, indicator_provider: Any,
                         as_of_dt: Optional[datetime] = None) -> None:
        """Classic RM sizes the PENDING orders, then submit each sized order to the sim.

        This is the live ``process_expert_recommendations_after_analysis`` tail (lines
        ~1167-1190): ``TradeRiskManagement.review_and_prioritize_pending_orders`` sets each
        order's quantity (via ``compute_risk_based_quantity`` / ``get_latest_atr`` using the
        injected indicator provider), then ``account.submit_order(order)`` sends the sized
        ones. ATR injection is the exact Phase-0 seam: ``TradeRiskManagement(indicator_provider=...)``.

        ``as_of_dt`` is the SIMULATED bar clock, passed as ``TradeRiskManagement(as_of=...)`` so
        any ATR fetch is as-of this bar (not wall-clock now()) — required for an offline/cache-
        backed indicator_provider to resolve the correct historical ATR row, not today's.
        """
        from ba2_common.core.TradeRiskManagement import TradeRiskManagement

        rm = TradeRiskManagement(indicator_provider=indicator_provider, as_of=as_of_dt)
        try:
            updated_orders = rm.review_and_prioritize_pending_orders(expert_id)
        except Exception as e:  # noqa: BLE001 — RM failure for one expert must not kill the run
            self._log(f"risk manager failed for expert {expert_id}: {e}")
            return

        for order in updated_orders:
            if order.quantity and order.quantity > 0:
                try:
                    # Effective protective stop: shared TIGHTER-WINS reconciliation of the RULESET
                    # entry-bracket SL (on the transaction) vs the RM SAFEGUARD (order.stop_price,
                    # what the position was SIZED off). See position_sizing.reconcile_protective_stop.
                    from ba2_common.core.position_sizing import reconcile_protective_stop
                    txn = get_instance(Transaction, order.transaction_id) if order.transaction_id else None
                    sl_price = reconcile_protective_stop(
                        ruleset_sl=(txn.stop_loss if txn else None),
                        safeguard_sl=(order.stop_price or None),
                        is_long=(order.side == OrderDirection.BUY))
                    self.account.submit_order(order, sl_price=sl_price)
                except Exception as e:  # noqa: BLE001
                    self._log(f"submit_order failed for order {order.id}: {e}")

    def _size_and_submit_candidates(self, expert_id: int, candidates: List[Any],
                                    as_of: datetime) -> bool:
        """Temp-order-list entry flow: size the bar's TRANSIENT equity candidates in ONE in-memory
        RM pass, then persist + submit ONLY the funded ones — no qty=0 DB churn, no unfunded deletes.

        ``candidates`` is a list of ``(candidate_order, evaluator, symbol, recommendation)``. The
        RM (``size_candidate_orders``) sets ``candidate.quantity`` (+ ``candidate.stop_price`` for
        risk_atr sizing) on the funded transient orders. For each funded symbol we then run the SAME
        ``evaluator.execute()`` that the DB path used (persisting the real order + transaction + TP/SL
        legs), stamp the pre-computed funded quantity + tighter-wins protective stop onto the persisted
        order, and submit. Unfunded candidates are dropped by the RM and never touch the DB.

        Returns True iff at least one order was funded, persisted and submitted.
        """
        from ba2_common.core.TradeRiskManagement import TradeRiskManagement

        by_symbol = {c[2]: c for c in candidates}
        rm = TradeRiskManagement(indicator_provider=self._indicator_provider, as_of=as_of)
        try:
            funded = rm.size_candidate_orders(expert_id, [(c[0], c[3]) for c in candidates])
        except Exception as e:  # noqa: BLE001 — RM failure for one expert must not kill the run
            self._log(f"candidate risk manager failed for expert {expert_id}: {e}")
            return False

        created_any = False
        for cand in funded:
            entry = by_symbol.get(cand.symbol)
            if entry is None or not (cand.quantity and cand.quantity > 0):
                continue
            _cand, evaluator, symbol, _rec = entry
            try:
                # Persist the real order + transaction + TP/SL bracket (execute is byte-identical to
                # the old flow; we just call it ONLY for funded symbols now).
                results = evaluator.execute(submit_to_broker=False)
                order_id = next((r["data"]["order_id"] for r in results
                                 if r.get("success") and (r.get("data") or {}).get("order_id")), None)
                if not order_id:
                    continue
                order = get_instance(TradingOrder, order_id)
                if order is None:
                    continue
                order.quantity = cand.quantity  # the RM-sized quantity computed on the candidate
                # Shared TIGHTER-WINS reconciliation: RM safeguard (cand.stop_price, what the size
                # was keyed off) vs the ruleset entry-bracket SL (on the transaction).
                from ba2_common.core.position_sizing import reconcile_protective_stop
                txn = get_instance(Transaction, order.transaction_id) if order.transaction_id else None
                sl_price = reconcile_protective_stop(
                    ruleset_sl=(txn.stop_loss if txn else None),
                    safeguard_sl=(cand.stop_price or None),
                    is_long=(order.side == OrderDirection.BUY))
                self.account.submit_order(order, sl_price=sl_price)
                created_any = True
            except Exception as e:  # noqa: BLE001
                self._log(f"funded submit failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
                continue
        return created_any

    # -- helpers ------------------------------------------------------------
    def _provider_bundle(self) -> Any:
        """The as_of-aware ProviderBundle fed to each expert's ``_gather``.

        Reuses Phase-1's ``LiveProviderBundle`` over the host's ba2_providers registry
        (resolved via the same ``TradeConditions`` provider resolver wired in Task 1). The
        providers are as_of-aware (the engine threads ``as_of`` into ``analyze_as_of``), so
        the bundle is constructed once and shared across bars.
        """
        bundle = getattr(self, "_bundle_cache", None)
        if bundle is None:
            from ba2_common.core.TradeConditions import _get_provider

            bundle = LiveProviderBundle(
                lambda category, name, **kw: _get_provider(category, name, **kw)
            )
            self._bundle_cache = bundle
        return bundle

    def _bust_price_cache(self) -> None:
        """Pop the per-account entry from the inherited wall-clock price cache.

        Belt-and-braces with ``BacktestAccount.get_instrument_current_price`` (which already
        bypasses the cache): any inherited caller that still routes through the cached path
        gets a fresh as-of price every bar instead of a stale virtual-day-N value.
        """
        cache = getattr(type(self.account), "_GLOBAL_PRICE_CACHE", None)
        if isinstance(cache, dict):
            cache.pop(self.account.id, None)

    def _build_minimal_results(self) -> Dict[str, Any]:
        """Task-4 results payload: the equity history + filled trades from the account.

        Task 5's ``build_results`` produces the full Backtest metric blob from the SAME
        account; this minimal dict keeps the engine independently testable and gives the
        handler a consistent return shape until Task 5 lands.
        """
        return {
            "equity_history": self.account.get_balance_history(),
            "trades": self.account.get_filled_trades(),
            "final_equity": self.account.equity(),
            "initial_capital": float(self.account._cfg["starting_cash"]),
            # RECORDED, NOT SCORED -- see ``_record_uncovered_assigned``.
            "uncovered_assigned_bars": self._uncovered_assigned_metric(),
        }

    @staticmethod
    def _log(msg: str) -> None:
        logger.warning(f"[daily_engine] {msg}")
