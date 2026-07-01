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
from ba2_common.core.db import add_instance
from ba2_common.core.models import ExpertRecommendation, Transaction
from ba2_common.core.types import (
    OrderDirection,
    OrderRecommendation,
    RiskLevel,
    TimeHorizon,
    TransactionStatus,
)
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
# Option expiry / exercise / assignment
# ---------------------------------------------------------------------------
def option_expiry_outcome(opt_type, side, *, strike, spot, qty, multiplier=100):
    """Resolve one option position at expiry. Pure. Long ITM -> exercise; short ITM -> assigned;
    OTM -> worthless. ITM: call when spot>strike, put when spot<strike."""
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
    ) -> None:
        self.account = account
        self.experts = experts
        self.price = price_source
        self.config = config
        self.progress_cb = progress_cb or (lambda pct, msg: None)
        self.seed = config["seed"]
        self._indicator_provider = indicator_provider
        # Per-day dynamic screener universe (screener-settings optimization). The optimizer's
        # trial config sets ``screener_runtime`` ({"store", "settings"[, "cadence_days"]}); when
        # absent (every non-screener run) this is None and the per-bar entry gate is a no-op, so
        # behaviour is byte-identical to before.
        self._screener_runtime = config.get("screener_runtime")
        # Per-run memo for the screener entry gate: {resolved_scan_date: [symbols]}. The screened
        # set only changes per scan date (weekly cadence), so it's computed once per scan date and
        # reused for every bar in that period (vs recomputing the full-store filter every 5min bar).
        self._screened_cache: Dict[str, List[str]] = {}
        # BYPASS-expert (FactorRanker) per-run caches. The FactorPortfolioManager holds only
        # run-CONSTANT state (the resolver expert/account instances + ids), and the per-bar stop
        # pass runs on ~every non-rebalance 5min bar — so reconstructing the manager (and its
        # ExpertInstance DB query) per bar was a profiled hotspot (~44% of the loop on a held
        # book). Build it ONCE per expert and reuse. virtual_equity_pct is likewise run-constant,
        # cached so the per-bar stop equity is account.get_balance() * pct with NO per-bar
        # ExpertInstance query (get_virtual_balance's hidden DB round-trip). Results-identical:
        # the cached manager reads live account/holdings on each call exactly as a fresh one did,
        # and the passed equity equals get_virtual_balance()'s value bit-for-bit (same balance,
        # same pct, same multiply order).
        self._bypass_pm: Dict[int, Any] = {}
        self._bypass_veq_pct: Dict[int, float] = {}

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

    def _bypass_manager(self, expert_id: int) -> Any:
        """Lazily build + cache the FactorPortfolioManager for a bypass expert (run-constant).

        Also caches the expert's ``virtual_equity_pct`` (read ONCE here, not per bar) so the
        per-bar stop can compute equity without re-querying ExpertInstance. Both are stable for
        the whole run; the manager itself reads live account state on every call.
        """
        pm = self._bypass_pm.get(expert_id)
        if pm is None:
            from ba2_experts.FactorRanker.portfolio import FactorPortfolioManager

            pm = FactorPortfolioManager(expert_id)
            self._bypass_pm[expert_id] = pm
            try:
                from ba2_common.core.db import get_instance
                from ba2_common.core.models import ExpertInstance

                inst = get_instance(ExpertInstance, expert_id)
                self._bypass_veq_pct[expert_id] = float(
                    getattr(inst, "virtual_equity_pct", None) or 100.0
                )
            except Exception:  # noqa: BLE001 — fall back to equity=None (method self-computes)
                self._bypass_veq_pct[expert_id] = 100.0
        return pm

    # -- the loop -----------------------------------------------------------
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
        # management ~78x/day (profiled: ~90k date-parses, the dominant 5min cost). We run that
        # block once per (expert, calendar day); the OCO TP/SL fills still run EVERY bar via
        # refresh_orders below, so trade closes stay 5min-precise. Matches live (RM manages on
        # the analysis cadence, fills are continuous).
        analyzed_days: set = set()

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

            # 2. universe for the bar.
            universe = resolve_universe(as_of_dt, self.config, self.price)

            # 2a. per-day DYNAMIC screener gate (screener-settings optimization). Computed ONCE
            #     per bar from this run's effective screener settings, resolving to the latest
            #     scan date <= the bar (the universe holds between weekly scans). When
            #     ``allowed is not None`` it restricts which symbols may ENTER this bar — the
            #     ENTRY candidate universe fed to ``_run_expert_bar`` is intersected with it,
            #     PRESERVING bar order so determinism is unchanged. Open-position management /
            #     exits are NOT gated: ``_manage_open_positions``, ``_apply_bypass_stops``,
            #     the bypass rebalance, ``_apply_option_expiry`` and the OCO bracket fills all
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

            # 2.5 per-bar STOP pass for bypass experts. The cadence-gated analyse/rebalance pass
            #     below skips non-entry bars, so a bypass expert (sizes by weight, skips the classic
            #     RM) gets its only between-rebalance downside protection here: a per-name equity-loss
            #     stop reusing risk_per_trade_pct. Skipped on rebalance bars (the rebalance owns the book).
            for expert, expert_id, settings, ruleset_id in self.experts:
                if not getattr(expert, "bypasses_classic_rm", False):
                    continue
                if _schedule_allows_entry(as_of_dt, self._entry_schedule(expert),
                                          self.price.is_intraday, _date_ctx):
                    continue
                if self._apply_bypass_stops(expert, expert_id, settings, as_of_dt):
                    # Only mark dirty when a stop SELL was actually submitted; flat/no-sell bars
                    # leave the order cache byte-identical and skip the invalidate_order_cache.
                    book_dirty = True

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
                # intraday bar of the day — run the (expensive) analyse+manage pass at most ONCE per
                # (expert, calendar day) so 5min runs don't re-analyse 78x. (When `times` IS set,
                # only one bar/day passes, so this never triggers.)
                _day_key = (expert_id, as_of_dt.date())
                if self.price.is_intraday and _day_key in analyzed_days:
                    continue
                analyzed_days.add(_day_key)
                book_dirty = True  # an analysis/management pass runs -> orders may be created
                if getattr(expert, "bypasses_classic_rm", False):
                    # Bypass experts (FactorRanker) rebalance the whole book on their ENTRY cadence;
                    # the rebalance IS the management, so run it only on entry bars.
                    if entry_ok:
                        self._run_bypass_expert_bar(expert, expert_id, settings, as_of_dt)
                    continue
                if entry_ok:
                    created_any = self._run_expert_bar(
                        expert, expert_id, settings, ruleset_id, entry_universe, as_of_dt
                    )
                    if created_any:
                        self._size_and_submit(expert_id, indicator_provider, as_of_dt)

                # Manage EXISTING positions through the OPEN_POSITIONS ruleset (real RM/evaluator) on
                # the MANAGE cadence — identical to live. Adjust-TP/SL/Close/Sell per the exit
                # conditions; no-op when the expert has no open_positions ruleset configured.
                if manage_ok:
                    self._manage_open_positions(expert, expert_id, settings, as_of_dt)

            # The analysis/bypass passes above create orders via ba2_common's DB-backed RM/submit
            # path; reload the account's order cache so the fill engine sees them this bar.
            if book_dirty:
                self.account.invalidate_order_cache()

            # 4. fills on THIS bar's working orders; roll order state into transactions.
            #     A transaction only changes state when one of its orders fills, and the bracket
            #     pass only has work when a transaction freshly OPENED — both are no-ops on a bar
            #     where nothing filled. On a 5-minute fill clock almost every bar has no fill, and
            #     the roll (incl. the ba2_common base sync_transaction_orders) + bracket pass were
            #     ~half of per-bar runtime (profiled), so gate them on the fill signal.
            filled = self.account.refresh_orders()
            if filled:
                self.account.refresh_transactions()

            # 4a. resolve any option positions reaching expiry on THIS bar: OTM -> worthless;
            #     ITM long -> exercise; ITM short -> assigned (converting to a SHARE position in
            #     the equity ledger settled at the strike). Runs after the transaction roll (so
            #     freshly-OPENED option positions are visible) and before snapshot_equity (so the
            #     resulting equity position is marked this bar). Date-driven (an option can expire
            #     on a no-fill bar), so it runs every bar — but get_option_positions() short-
            #     circuits to [] for equity-only runs (no options provider), so this is ~free
            #     there. Early American assignment is NOT modelled — options resolve at expiry.
            self._apply_option_expiry(as_of_dt)

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
            if filled:
                self.account.invalidate_order_cache()

            # 5. record per-bar equity / drawdown point.
            self.account.snapshot_equity(as_of_dt)

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

            rec_id = _recommendation_to_expert_recommendation(
                rec, expert_instance_id=expert_id, symbol=symbol, as_of=as_of
            )
            if rec_id is None:
                continue  # SKIP / HOLD / ERROR — nothing to stage.

            # Re-read the persisted row so the evaluator/actions see a DB-attached object
            # carrying its id (BuyAction links the order to expert_recommendation.id).
            from ba2_common.core.db import get_instance as _get_instance

            recommendation = _get_instance(ExpertRecommendation, rec_id)
            if recommendation is None:
                continue

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
                    continue  # conditions not met / evaluation error -> no order this symbol.

                # Equity entry: create PENDING qty=0 orders (NOT submitted; RM sizes + submits
                # next). Option entry: the option action sizes + submits ITSELF, so submit
                # directly (like the open-positions path) — there is no equity leg.
                results = evaluator.execute(submit_to_broker=self._entry_is_option)
                if any(r.get("success") and (r.get("data") or {}).get("order_id") for r in results):
                    created_any = True
            except Exception as e:  # noqa: BLE001
                self._log(f"ruleset eval/execute failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
                continue

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
            )
            try:
                rec = expert.analyze_as_of(as_of, ctx)
            except Exception as e:  # noqa: BLE001 — one symbol must not abort the bar
                self._log(f"open-pos analyze failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
                continue
            rec_id = _recommendation_to_expert_recommendation(
                rec, expert_instance_id=expert_id, symbol=symbol, as_of=as_of, allow_hold=True
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
                if any(r.get("success") and (r.get("data") or {}).get("order_id") for r in results):
                    created_any = True
            except Exception as e:  # noqa: BLE001
                self._log(f"open-pos eval/execute failed for {symbol} @ {as_of:%Y-%m-%d}: {e}")
                continue

        if created_any:
            self._size_and_submit(expert_id, self._indicator_provider, as_of)

    def _held_transactions(self, expert_id: int) -> Dict[str, List[Any]]:
        """{symbol: [OPENED Transaction, ...]} for this expert.

        OPENED only (not WAITING): the backtest enters with MARKET orders that fill on the NEXT
        bar, so a WAITING transaction is just THIS bar's freshly-created, un-filled entry —
        managing it now would cancel the entry before it ever opens. Live includes WAITING
        because there a limit entry can genuinely sit working; here only a filled position is a
        real open position to manage.
        """
        from sqlmodel import select, Session
        from ba2_common.core.db import get_db
        from ba2_common.core.types import TransactionStatus

        out: Dict[str, List[Any]] = {}
        with Session(get_db().bind) as session:
            rows = session.exec(
                select(Transaction).where(
                    Transaction.expert_id == expert_id,
                    Transaction.status == TransactionStatus.OPENED,
                )
            ).all()
        for t in rows:
            out.setdefault(t.symbol, []).append(t)
        return out

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
        A per-bar failure is logged and swallowed (one bad bar must not abort the run), matching
        the classic path's per-bar try/except.
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

    def _apply_bypass_stops(self, expert, expert_id, settings, as_of) -> bool:
        """Per-name EQUITY-loss stop for a BYPASS expert (FactorRanker), reusing
        risk_per_trade_pct as a max-loss-per-name cap (% of equity). Sells any held name
        whose unrealized loss has reached that % of equity. Runs only on NON-rebalance bars
        (the rebalance pass owns the book on its scheduled bars). Lookahead-safe: submits a
        MARKET sell that fills on a later bar per the fill model (same discipline as
        _apply_initial_brackets). A per-bar failure is logged and swallowed.

        Returns True iff at least one stop SELL was actually submitted, so the caller can mark
        the order cache dirty ONLY when there is a new order for the fill engine to see. When it
        returns False nothing was submitted (no stop_pct, flat account, or no name breached) so
        the cache is byte-identical and need not be invalidated.
        """
        try:
            stop_pct = expert.get_setting_with_interface_default(
                "risk_per_trade_pct", log_warning=False
            )
        except Exception:  # noqa: BLE001 — a stub/unschedulable expert -> no stop
            stop_pct = None
        if not (stop_pct and stop_pct > 0):
            return False

        # Flat account -> nothing to stop. Results-identical fast path: skips a pass that could
        # only ever sell nothing (no positions exist). Avoids constructing the portfolio manager
        # and its expert-scoped OPENED-Transaction query on the (common) no-position bars.
        if not self.account.get_positions():
            return False

        # Reuse the run-constant portfolio manager (built once) and compute the stop equity
        # cheaply from the account cash + cached virtual_equity_pct — byte-identical to
        # apply_stop_losses' own get_virtual_balance() (same balance, same pct) but WITHOUT the
        # two per-bar ExpertInstance DB queries (manager __init__ + get_virtual_balance). When
        # the balance is unavailable, pass equity=None so the method self-computes (old path).
        pm = self._bypass_manager(expert_id)
        balance = self.account.get_balance()
        if balance is None:
            equity = None
        else:
            equity = balance * (self._bypass_veq_pct.get(expert_id, 100.0) / 100.0)

        try:
            submitted = pm.apply_stop_losses(float(stop_pct), equity=equity)
        except Exception as e:  # noqa: BLE001 — a stop failure must not kill the run
            self._log(f"bypass stop failed for expert {expert_id} @ {as_of:%Y-%m-%d}: {e}")
            # Unknown whether an order was submitted before the failure -> assume YES so the fill
            # engine cannot miss a sell (the safe default: an unnecessary cache reload is harmless,
            # a missed one changes results).
            return True
        return bool(submitted)

    # -- option expiry / exercise / assignment ------------------------------
    def _apply_option_expiry(self, as_of: datetime) -> None:
        """Resolve every held single-leg option position that has reached its expiry.

        For each held option whose ``expiry <= as_of.date()`` the engine reads the underlying's
        bar CLOSE and resolves the outcome via the pure ``option_expiry_outcome`` helper:

          * worthless -> close the option transaction at premium 0 (realise the entry P&L).
          * exercise/assignment -> close the option at its intrinsic value AND create the
            resulting SHARE position in the equity ledger, settled at the STRIKE; the new
            equity position then marks-to-market on every subsequent bar.

        Early American assignment is NOT modelled — options resolve at expiry only.

        A missing underlying close skips the position (logged). Per-position failures are
        caught + logged so one bad expiry cannot abort the run (matching the per-bar style).
        """
        as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
        for pos in self.account.get_option_positions():
            try:
                if pos.expiry is None or pos.expiry > as_of_date:
                    continue
                spot = self.price.close_at(pos.underlying)
                if spot is None:
                    self._log(
                        f"option expiry: no underlying close for {pos.underlying} "
                        f"({pos.contract_symbol}) @ {as_of_date} — skipped"
                    )
                    continue
                out = option_expiry_outcome(
                    pos.option_type,
                    pos.side,
                    strike=pos.strike,
                    spot=spot,
                    qty=pos.quantity,
                    multiplier=pos.multiplier,
                )
                if out["action"] == "worthless":
                    self.account.settle_option_expiry(pos, close_premium=0.0)
                else:
                    intrinsic = abs(float(spot) - float(pos.strike))  # per-share intrinsic value
                    share_side = (
                        OrderDirection.BUY if out["side"] == "buy" else OrderDirection.SELL
                    )
                    self.account.settle_option_expiry(
                        pos,
                        close_premium=intrinsic,
                        share_side=share_side,
                        shares=int(out["shares"]),
                        share_price=float(out["price"]),
                    )
            except Exception as e:  # noqa: BLE001 — one bad expiry must not abort the run
                self._log(
                    f"option expiry failed for {pos.contract_symbol} @ {as_of_date}: {e}"
                )

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
                    # order.stop_price carries the RM's safeguard SL (min ATR×mult / risk%, floored
                    # at min_stop_loss_pct) when the strategy's conditions set no stop of their own
                    # — pass it as sl_price so submit_order creates the protective WAITING_TRIGGER
                    # SL leg (mirrors the live TradeManager call below).
                    self.account.submit_order(order, sl_price=order.stop_price or None)
                except Exception as e:  # noqa: BLE001
                    self._log(f"submit_order failed for order {order.id}: {e}")


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
        }

    @staticmethod
    def _log(msg: str) -> None:
        logger.warning(f"[daily_engine] {msg}")
