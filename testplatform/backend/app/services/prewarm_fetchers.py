"""The ONE per-expert prewarm fetcher table, shared by BOTH prewarm entry points.

``ba2-test prewarm`` (``ba2test_launcher._cmd_prewarm``) and the API/queue handler
(``app.services.data_build_handler.handle_prewarm``) used to carry two independent copies of
this: the CLI knew seven experts, the handler three, and only the CLI warmed
DeterministicScorer's per-symbol financial histories. The 2026-09-10 live-replay readiness
audit ("Two prewarm tooling gaps") found the two entry points were NOT interchangeable -- a
prewarm driven from the UI reported success having written a fraction of what the CLI writes.
One table, one set of fetchers, imported by both, is the fix: an expert added here is warmed
by both entry points or by neither.

Each fetcher warms exactly the ``fmp_history`` namespaces its expert's ``_gather`` reads, by
calling the SAME data-layer function the expert calls -- never a hand-rolled re-implementation
of the fetch, so the warmed surface cannot drift away from the read surface.

THE FREEZE GATE IS THE CALLER'S JOB, AND IT IS THREAD-LOCAL. ``fmp_history_disk_cached`` only
writes to disk while ``ba2_providers.fmp_common``'s thread-local ttl-freeze flag is set (live
is a straight passthrough), so a caller running these fetchers in a ``ThreadPoolExecutor``
MUST pass ``initializer=set_ttl_frozen, initargs=(True,)`` -- entering ``frozen_ttl_cache()``
on the submitting thread alone leaves every worker un-frozen, fetching over the network and
writing nothing. ``persist_empty_sentinel()`` (a module global, so it does reach the workers)
belongs around the same block, so a symbol FMP genuinely has no data for is cached as ``[]``
("checked, no data") instead of looking forever like a prewarm gap.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Expert class name -> the ``PrewarmFetchers`` method that warms it. THE fetcher table: both
#: entry points build their per-(expert, symbol) work list from this mapping and nothing else.
FETCHER_METHODS: Dict[str, str] = {
    "FMPRating": "do_fmprating",
    "FMPEarningsDrift": "do_earnings_drift",
    "FMPInsiderClusterBuy": "do_insider",
    "FactorRanker": "do_factorranker",
    "FMPSenateTraderWeight": "do_senate",
    "FinnHubRating": "do_finnhub",
    "DeterministicScorer": "do_deterministic_scorer",
}

#: The experts prewarm can warm, in table order (for CLI help / API validation messages).
EXPERT_NAMES = tuple(FETCHER_METHODS)

#: Experts whose warming needs the congressional-disclosure feeds (the per-symbol fetcher plus
#: the one-shot unscoped "latest disclosures" warm, ``do_senate_latest``).
SENATE_EXPERTS = ("FMPSenateTraderWeight", "FMPSenateTraderCopy")


class PrewarmConfigError(ValueError):
    """A requested expert cannot be warmed with the configuration it was given.

    Raised UP FRONT (``validate``), before any fetch runs, so missing configuration surfaces as
    one clear message instead of N per-symbol failures -- or, worse, a run that reports success.
    """


class PrewarmFetchers:
    """Per-symbol history fetchers for every expert prewarm supports.

    Construction is cheap: providers/experts are built lazily on first use, so an instance
    covering all seven experts costs nothing for the experts a given run does not warm.

    Args are keyword-only and have NO defaults on purpose (no-defaults rule): a caller states
    every input, and one it cannot supply is passed explicitly as ``None`` and refused by
    ``validate`` rather than silently standing in for a real value.

    ``expert_settings`` maps expert class name -> that instance's settings dict (the settings a
    live/backtest instance would run with). It steers settings-DEPENDENT warming -- today
    FMPInsiderClusterBuy's ``expected_profit_mode`` (see ``do_insider``). An expert absent from
    the mapping is warmed with its own DECLARED settings defaults (``get_settings_definitions``),
    which is the platform's ordinary settings resolution; a key absent from THOSE raises.
    """

    def __init__(self, *,
                 fmp_key: str,
                 end_date: datetime,
                 finnhub_key: Optional[str],
                 senate_hold_floor_days: Optional[float],
                 senate_hold_min_roundtrips: Optional[int],
                 expert_settings: Optional[Dict[str, Dict[str, Any]]] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        if not fmp_key:
            raise PrewarmConfigError(
                "FMP_API_KEY not configured (set it in .env or the app-settings DB).")
        self.fmp_key = fmp_key
        self.end_date = end_date
        self.finnhub_key = finnhub_key
        self.senate_hold_floor_days = senate_hold_floor_days
        self.senate_hold_min_roundtrips = senate_hold_min_roundtrips
        self.expert_settings: Dict[str, Dict[str, Any]] = expert_settings or {}
        self._log_fn = log if log is not None else logger.info

        # Lazily-built provider/expert singletons (shared across the whole run's symbols).
        self._details_provider = None
        self._insider_provider = None
        self._finnhub_expert = None

        # Senate dedup state is SHARED across every universe symbol's do_senate call: the same
        # prolific trader is discovered from dozens of symbols, and re-walking their whole
        # history each time is pure wasted CPU (the disk/memory cache already prevented the
        # redundant NETWORK fetch, not the Python-side work of getting there). Lock-guarded
        # because the caller runs the per-symbol fetchers concurrently.
        self.senate_expert = None
        self.senate_seen_traders: set = set()
        self._senate_warmed_skill_syms: set = set()
        self._senate_lock = threading.Lock()

    # ------------------------------------------------------------------ table
    @property
    def table(self) -> Dict[str, Callable[[str], None]]:
        """expert name -> its per-symbol fetch callable (bound to this instance)."""
        return {name: getattr(self, meth) for name, meth in FETCHER_METHODS.items()}

    def validate(self, experts: List[str]) -> List[str]:
        """Refuse an unwarmable request BEFORE any fetch runs; return the unknown experts.

        Unknown experts are RETURNED (the caller reports them as skipped, as both entry points
        already did); a KNOWN expert this instance is not configured to warm RAISES, because
        that one would otherwise fail per symbol, repeatedly, mid-run.
        """
        unknown = [e for e in experts if e not in FETCHER_METHODS]
        if "FinnHubRating" in experts and not self.finnhub_key:
            raise PrewarmConfigError(
                "finnhub_api_key not configured (set FINNHUB_API_KEY or the app-setting) - "
                "required to warm FinnHubRating.")
        if any(e in experts for e in SENATE_EXPERTS):
            if self.senate_hold_floor_days is None or self.senate_hold_min_roundtrips is None:
                raise PrewarmConfigError(
                    "senate_hold_floor_days / senate_hold_min_roundtrips are required to warm "
                    f"{', '.join(SENATE_EXPERTS)} (the GA grid's gentlest scalper-filter "
                    "setting; the CLI reads both from _EXPERT_OPT['FMPSenateTraderWeight'], the "
                    "API takes them as payload keys).")
        return unknown

    def log(self, message: str) -> None:
        self._log_fn(message)

    # -------------------------------------------------------------- internals
    def _details(self):
        """The shared FMPCompanyDetailsProvider (statements / earnings / estimates)."""
        if self._details_provider is None:
            from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
                FMPCompanyDetailsProvider,
            )
            self._details_provider = FMPCompanyDetailsProvider()
        return self._details_provider

    def _insider(self):
        if self._insider_provider is None:
            from ba2_providers.insider.FMPInsiderProvider import FMPInsiderProvider
            self._insider_provider = FMPInsiderProvider()
        return self._insider_provider

    def _expert_setting(self, expert_name: str, cls: Any, key: str) -> Any:
        """Resolve one setting for *expert_name* the way the platform resolves settings.

        The instance's own value wins when the caller supplied one; otherwise the expert's
        DECLARED default from ``get_settings_definitions()`` -- the same two steps
        ``_setting_or_default`` takes. There is no inline default anywhere in the chain: a key
        that is not declared raises ``KeyError``, and an explicitly-null value raises, so a
        renamed/removed knob fails loudly here instead of silently warming the wrong surface.
        """
        settings = (self.expert_settings[expert_name]
                    if expert_name in self.expert_settings else None)
        if settings is not None and key in settings:
            value = settings[key]
            if value is None:
                raise PrewarmConfigError(
                    f"{expert_name}.{key} was supplied as null - prewarm cannot tell which "
                    f"histories that instance reads. Give it a real value, or omit the key to "
                    f"use the expert's declared default.")
            return value
        return cls.get_settings_definitions()[key]["default"]

    def _warm_estimator_inputs(self, sym: str) -> None:
        """Warm ``analyst_target_model.fetch_estimator_inputs``' two namespaces for *sym*.

        The fundamentals-only price-target model (``expected_profit_mode='model'``) reads
        quarterly past earnings + forward EPS estimates through
        ``FMPCompanyDetailsProvider.get_past_earnings`` / ``get_earnings_estimates``
        (``packages/experts/ba2_experts/analyst_target_model.py``, ``fetch_estimator_inputs``)
        -> the ``past_earnings_quarterly`` and ``earnings_estimates_quarterly`` fmp_history
        namespaces. Both calls mirror that function exactly, including the "quarterly" spelling
        of the estimates namespace (FMP always returns ANNUAL rows there; "quarterly" is the
        cache namespace every warmed file on disk already uses -- see get_earnings_estimates'
        own docstring). ``lookback_periods`` only trims in Python after the fetch; the cached
        payload is the full per-symbol history either way.
        """
        det = self._details()
        det.get_past_earnings(symbol=sym, frequency="quarterly", end_date=self.end_date,
                              lookback_periods=4, format_type="dict")
        det.get_earnings_estimates(symbol=sym, frequency="quarterly", as_of_date=self.end_date,
                                   lookback_periods=2, format_type="dict")

    # --------------------------------------------------------------- fetchers
    def do_fmprating(self, sym: str) -> None:
        from ba2_experts.FMPRating import (
            fetch_grades_historical_cached, fetch_price_target_history_cached,
            fetch_analyst_grades_cached,
        )
        fetch_grades_historical_cached(self.fmp_key, sym)
        fetch_price_target_history_cached(self.fmp_key, sym)
        fetch_analyst_grades_cached(self.fmp_key, sym)   # dated individual grades (rating-recency)

    def do_earnings_drift(self, sym: str) -> None:
        self._details().get_past_earnings(
            sym, frequency="quarterly", end_date=self.end_date,
            lookback_periods=8, format_type="dict")

    def do_insider(self, sym: str) -> None:
        """Insider transactions -- plus, in model mode, the price-target model's inputs.

        ``FMPInsiderClusterBuy._gather`` fetches ``estimator_inputs`` ONLY when
        ``expected_profit_mode == 'model'`` (opt-in I/O, default off), so warming them
        unconditionally would burn two extra FMP calls per symbol on every static-mode run,
        and never warming them leaves a model-mode run reading two namespaces prewarm never
        wrote -- exactly the gap the 2026-09-10 readiness audit found on the deployed
        model-mode instance. Steer on the instance's own resolved setting instead.
        """
        self._insider().get_insider_transactions(
            sym, end_date=self.end_date, lookback_days=400, as_of=self.end_date,
            format_type="dict")
        from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
        if self._expert_setting("FMPInsiderClusterBuy", FMPInsiderClusterBuy,
                                "expected_profit_mode") == "model":
            self._warm_estimator_inputs(sym)

    def do_deterministic_scorer(self, sym: str) -> None:
        """DeterministicScorer: warm the SAME fmp_history namespaces its ``_gather`` reads --
        annual income/balance/cashflow statements (point-in-time F-Score / Altman Z / quality /
        value / growth inputs; the backtest filters them by filing date in Python) + the dated
        analyst-grade history for the OPTIONAL analyst section (weight default 0, but the grid
        may switch it on), plus the EARNINGS/PEAD and ANALYST price-target namespaces. Those
        last two MUST be warmed here or a hermetic trial with w_earnings>0 / w_analyst>0 aborts
        on a cache miss. OHLCV comes from the fetch-cache parquet, not from here.
        """
        from ba2_experts.FMPRating import (
            fetch_grades_historical_cached, fetch_price_target_history_cached,
        )
        det = self._details()
        for fn in (det.get_income_statement, det.get_balance_sheet, det.get_cashflow_statement):
            fn(symbol=sym, frequency="annual", end_date=self.end_date,
               lookback_periods=6, as_of=self.end_date, format_type="dict")
        fetch_grades_historical_cached(self.fmp_key, sym)
        det.get_past_earnings(symbol=sym, frequency="quarterly", end_date=self.end_date,
                              lookback_periods=16, format_type="dict")
        fetch_price_target_history_cached(self.fmp_key, sym)

    def do_factorranker(self, sym: str) -> None:
        """FactorRanker (bypass/rebalance expert): warm ALL of its factor inputs by calling the
        SAME data-layer fetchers the rebalance path uses (so coverage auto-tracks the real fetch
        surface and cannot drift). Per symbol this writes income_statement_annual /
        balance_sheet_annual / cashflow_statement_annual (value+quality), past_earnings_quarterly
        + earnings_estimates_quarterly (pead), AND the 1d OHLCV parquet (momentum + value as_of
        price). All factor inputs are fetched regardless of weight because the GA varies
        factor_weight_* per individual -- any factor can be active. ohlcv_provider is
        intentionally omitted so the fetchers construct an FMPOHLCVProvider() and the parquet
        path engages.

        NOTE: this warms the FACTOR stage of the default static universe, and OHLCV only for
        ~400d ending at end_date; for a multi-bar backtest span run ``ba2-test fetch-cache
        --timeframes 1d`` over [start-warmup, end] as well.
        """
        from ba2_experts.FactorRanker import data as _fr_data
        _fr_data.fetch_value_inputs([sym], as_of=self.end_date)    # statements + OHLCV as_of price
        _fr_data.fetch_quality_inputs([sym], as_of=self.end_date)  # statements (disk hits)
        _fr_data.fetch_pead_inputs([sym], as_of=self.end_date)     # past_earnings + estimates
        _fr_data.fetch_close_prices([sym], as_of=self.end_date)    # momentum: 1d OHLCV parquet

    def do_finnhub(self, sym: str) -> None:
        """FinnHubRating: warm the per-symbol finnhub_reco_trends namespace. A bare instance
        carries the Finnhub key + a logger (all ``_fetch_recommendation_trends`` needs)."""
        if self._finnhub_expert is None:
            if not self.finnhub_key:
                raise PrewarmConfigError(
                    "finnhub_api_key not configured (set FINNHUB_API_KEY or the app-setting) - "
                    "required to warm FinnHubRating.")
            import logging as _lg
            from ba2_experts.FinnHubRating import FinnHubRating
            e = FinnHubRating.__new__(FinnHubRating)
            e._api_key = self.finnhub_key
            e.logger = _lg.getLogger("finnhub-prewarm")
            self._finnhub_expert = e
        self._finnhub_expert._fetch_recommendation_trends(sym)

    # ----------------------------------------------------------------- senate
    def _ensure_senate_expert(self):
        if self.senate_expert is None:
            import logging as _lg
            from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight
            with self._senate_lock:
                if self.senate_expert is None:
                    s = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
                    s._api_key = self.fmp_key
                    s.logger = _lg.getLogger("senate-prewarm")
                    self.senate_expert = s
        return self.senate_expert

    def warm_new_traders(self, s, trades) -> None:
        """Discover new (not-yet-seen) traders from ``trades`` and warm their full disclosure
        history + skill-relevant buy-symbol price history. Shared by ``do_senate`` (traders
        discovered via the per-symbol ``-trades`` endpoint) and ``do_senate_latest`` (traders
        discovered via the unscoped ``-latest`` endpoint) -- a trader who only shows up in the
        unscoped feed still needs their ``congress_trader_history`` entry warmed, or
        ``_gather_all``'s Stage 2 hits a hermetic ``FMPHistoryCacheMiss`` for them mid-backtest.
        """
        new_traders = []
        with self._senate_lock:
            for trade in trades:
                name = s._trader_name(trade)
                if name and name not in self.senate_seen_traders:
                    self.senate_seen_traders.add(name)
                    new_traders.append(name)
        # Skill scoring reads the price history of every symbol in each trader's scored past
        # BUYS. Warm ALL unique buy symbols - not just the most-recent-N as of today - because
        # at an early backtest as_of the scorer's "most recent completed buys" are OLDER trades
        # whose symbols a today-anchored cap would miss, hard-failing the hermetic run. Symbols
        # FMP has no data for persist as the [] sentinel, which the scorer skips cleanly.
        for name in new_traders:
            history = s._fetch_trader_history(name) or []  # warms congress_trader_history (once)
            # Scalper skip: a trader excluded by even the grid's gentlest filter setting
            # contributes to NO GA trial's signal, so their (potentially thousands of)
            # buy-symbols are dead weight - skip the price-history warm entirely for them.
            hold_info = s._calculate_trader_avg_hold_days(history)
            if (hold_info["avg_hold_days"] is not None
                    and hold_info["roundtrips"] >= self.senate_hold_min_roundtrips
                    and hold_info["avg_hold_days"] < self.senate_hold_floor_days):
                continue
            new_skill_syms = []
            with self._senate_lock:
                for t in history:
                    ttype = str(t.get('type', '')).lower()
                    if 'purchase' not in ttype and 'buy' not in ttype:
                        continue
                    ssym = str(t.get('symbol', '')).upper()
                    if ssym and ssym not in self._senate_warmed_skill_syms:
                        self._senate_warmed_skill_syms.add(ssym)
                        new_skill_syms.append(ssym)
            for ssym in new_skill_syms:
                s._get_price_at_date(ssym, self.end_date)  # warms historical_price_full (once)

    def do_senate(self, sym: str) -> None:
        """FMPSenateTraderWeight: warm the SAME fmp_history namespaces ``_gather`` reads --
        per-symbol senate/house trades (congress_{chamber}_trades) + the symbol's full daily
        price history (historical_price_full), plus each DISCLOSED trader's full history
        (congress_trader_history, keyed by trader name, discovered from the trades)."""
        s = self._ensure_senate_expert()
        trades = (s._fetch_senate_trades(sym) or []) + (s._fetch_house_trades(sym) or [])
        s._get_price_at_date(sym, self.end_date)  # warms historical_price_full (once)
        self.warm_new_traders(s, trades)

    def do_senate_latest(self) -> None:
        """Warm the UNSCOPED 'latest disclosures' cache entries (``congress_senate_latest/
        ALL_FULL_HISTORY``, ``congress_house_latest/ALL_FULL_HISTORY``) that basket-mode
        ``_gather_all`` reads -- a DIFFERENT namespace from the per-symbol entries
        ``do_senate`` warms, and independent of any universe symbol, so it runs ONCE per run.

        DEEP PAGINATION (``full_history=True``) on purpose: the single-page fetch reaches back
        ~4 months, which left basket-mode FMPSenateTraderWeight scoring ``trades=0`` for EVERY
        individual across a full 2023-2026 GA matrix grid. Also warms every trader DISCOVERED
        via this unscoped feed -- its trader set is NOT the per-symbol loop's trader set.
        """
        s = self._ensure_senate_expert()
        self.log(">> senate: warming unscoped 'latest disclosures' feed (congress_senate_latest/"
                 "congress_house_latest, ALL_FULL_HISTORY, full pagination)...")
        senate_latest = s._fetch_senate_trades(symbol=None, full_history=True) or []
        house_latest = s._fetch_house_trades(symbol=None, full_history=True) or []
        self.log(f"   senate: {len(senate_latest)} senate + {len(house_latest)} house rows "
                 f"fetched (full pagination)")
        self.warm_new_traders(s, senate_latest + house_latest)


def build_fetchers(*, fmp_key: str, end_date: datetime, finnhub_key: Optional[str],
                   senate_hold_floor_days: Optional[float],
                   senate_hold_min_roundtrips: Optional[int],
                   expert_settings: Optional[Dict[str, Dict[str, Any]]] = None,
                   log: Optional[Callable[[str], None]] = None) -> PrewarmFetchers:
    """Build the shared fetcher set. BOTH prewarm entry points go through this function --
    it is the seam that makes "the CLI and the API warm the same thing" testable."""
    return PrewarmFetchers(
        fmp_key=fmp_key, end_date=end_date, finnhub_key=finnhub_key,
        senate_hold_floor_days=senate_hold_floor_days,
        senate_hold_min_roundtrips=senate_hold_min_roundtrips,
        expert_settings=expert_settings, log=log)
