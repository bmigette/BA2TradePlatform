from typing import Any, Dict, Optional
from datetime import datetime, timezone
import json
import logging
import requests

from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ba2_common.core.db import get_db, update_instance, add_instance
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon,
)
from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.core.provider_utils import parse_provider_date
from ba2_common.logger import get_expert_logger
from ba2_common.config import get_app_setting
from ba2_experts.expert_mixins import AnalysisStatusRenderMixin, FMPApiKeyMixin
from ba2_providers.fmp_common import fmp_http_get, FMPError, TTLCache, fmp_history_disk_cached


# Process-wide short-TTL caches so the many experts that analyze overlapping
# universes don't each re-fetch the same symbol's FMP data within a run. Keyed by
# symbol; shared across all FMPRating instances.
_FMP_CACHE_TTL_SECONDS = 900  # 15 minutes
_CONSENSUS_CACHE = TTLCache(_FMP_CACHE_TTL_SECONDS)
_UPGRADE_CACHE = TTLCache(_FMP_CACHE_TTL_SECONDS)
# Dated (backtest-path) caches: the full per-row history is time-invariant for a
# given symbol within the TTL window, so reconstruction at many as_of dates re-uses
# one fetch. Keyed by symbol (NOT by as_of) — the no-lookahead date filtering runs
# in the pure reconstruction calculators on the cached full history.
_GRADES_HISTORICAL_CACHE = TTLCache(_FMP_CACHE_TTL_SECONDS)
_PRICE_TARGET_HISTORY_CACHE = TTLCache(_FMP_CACHE_TTL_SECONDS)
_ANALYST_GRADES_CACHE = TTLCache(_FMP_CACHE_TTL_SECONDS)

# ~30 days/month — the coarse calendar used to convert max_analyst_age_months to a
# trailing day-window for the rating-recency filter (the gene steps in 3-month units).
_DAYS_PER_MONTH = 30

# FMP's price-target CONSENSUS endpoint windows to ~the last quarter, so the count
# of price targets behind a live consensus is measured over this trailing window.
_QUARTER_DAYS = 90

# Module logger for the standalone cached fetchers (used when there is no expert
# instance, e.g. the optimization pre-warm). The instance methods keep using their
# per-instance expert logger.
_logger = logging.getLogger(__name__)


_UNPARSED = object()  # sentinel: distinguishes "not yet parsed" from a parsed None


def _memo_provider_date(row: dict, key: str):
    """``parse_provider_date(row[key])`` memoized ON the row dict under ``_pd``.

    The per-symbol grades / price-target history rows are STABLE objects: fetched once and
    held for the whole (frozen) backtest in the process-wide TTLCache, then filtered by as_of
    on every analysis bar. Re-parsing each row's date on every bar made ``parse_provider_date``
    the #1 backtest CPU cost (~2M calls). Parsing once per row and stashing it (None included,
    via the sentinel, so unparseable rows aren't retried) collapses that to ~one parse/row.
    A row carries exactly one of these histories, so the single ``_pd`` slot never collides.
    """
    cached = row.get("_pd", _UNPARSED)
    if cached is _UNPARSED:
        cached = parse_provider_date(row.get(key))
        row["_pd"] = cached
    return cached


def fetch_grades_historical_cached(api_key: str, symbol: str) -> list:
    """Fetch the FULL dated analyst-grade history for a symbol (backtest path),
    WITHOUT needing an FMPRating instance.

    Same body as ``FMPRating._fetch_grades_historical``'s inner ``_do_fetch``, wrapped
    through the in-process TTLCache -> backtest-only disk cache -> FMP network chain so
    the optimization pre-warm and the spawned GA workers share one per-symbol fetch.
    Endpoint: ``stable/grades-historical?symbol=`` — rows carry a ``date`` plus dated
    StrongBuy/Buy/Hold/Sell/StrongSell counts. Returns the raw list (no-lookahead date
    filtering happens in ``_counts_as_of``)."""
    def _do_fetch():
        url = "https://financialmodelingprep.com/stable/grades-historical"
        params = {"symbol": symbol, "apikey": api_key}
        _logger.debug(f"Fetching FMP grades-historical for {symbol}")
        try:
            response = fmp_http_get(url, params, symbol=symbol,
                                    endpoint="grades-historical", timeout=60)
        except FMPError as e:
            raise ValueError(str(e)) from e
        data = response.json()
        return data if isinstance(data, list) else []

    # In-process TTLCache -> backtest-only disk cache -> FMP network. The disk layer lets
    # spawned GA workers read this per-symbol history from disk instead of re-fetching.
    return _GRADES_HISTORICAL_CACHE.get_or_call(
        symbol, lambda: fmp_history_disk_cached("grades_historical", symbol, _do_fetch))


def fetch_price_target_history_cached(api_key: str, symbol: str) -> list:
    """Fetch the FULL dated individual analyst price-target history (backtest path),
    WITHOUT needing an FMPRating instance.

    Same body as ``FMPRating._fetch_price_target_history``'s inner ``_do_fetch``, wrapped
    through the same TTLCache -> disk-cache -> network chain. Endpoint:
    ``v4/price-target?symbol=`` — rows carry ``publishedDate`` and ``priceTarget`` (one
    row per analyst note). Returns the raw list (no-lookahead window filtering + averaging
    happen in ``_consensus_target_as_of``)."""
    def _do_fetch():
        url = "https://financialmodelingprep.com/api/v4/price-target"
        params = {"symbol": symbol, "apikey": api_key}
        _logger.debug(f"Fetching FMP price-target history for {symbol}")
        try:
            response = fmp_http_get(url, params, symbol=symbol,
                                    endpoint="price-target", timeout=60)
        except FMPError as e:
            raise ValueError(str(e)) from e
        data = response.json()
        return data if isinstance(data, list) else []

    # In-process TTLCache -> backtest-only disk cache -> FMP network (see grades-historical).
    return _PRICE_TARGET_HISTORY_CACHE.get_or_call(
        symbol, lambda: fmp_history_disk_cached("price_target", symbol, _do_fetch))


def fetch_analyst_grades_cached(api_key: str, symbol: str) -> list:
    """Fetch the FULL dated INDIVIDUAL analyst-grade history for a symbol (rating recency),
    WITHOUT needing an FMPRating instance.

    Endpoint: ``stable/grades?symbol=`` — one row per analyst action carrying ``date`` +
    ``gradingCompany`` (+ previous/new grade, action). UNLIKE the aggregate
    upgrades-downgrades-consensus / grades-historical bucket counts (which are undated or
    monthly snapshots and can be inflated by long-stale ratings), these per-analyst dated rows
    let ``_count_recent_analysts`` count DISTINCT analysts active within a recency window
    (``max_analyst_age_months``), as-of, no-lookahead. Cached per symbol (full history is
    time-invariant within the TTL window); reusable by the optimization pre-warm + GA workers."""
    def _do_fetch():
        url = "https://financialmodelingprep.com/stable/grades"
        params = {"symbol": symbol, "apikey": api_key}
        _logger.debug(f"Fetching FMP analyst grades for {symbol}")
        try:
            response = fmp_http_get(url, params, symbol=symbol,
                                    endpoint="grades", timeout=60)
        except FMPError as e:
            raise ValueError(str(e)) from e
        data = response.json()
        return data if isinstance(data, list) else []

    # In-process TTLCache -> backtest-only disk cache -> FMP network (see grades-historical).
    return _ANALYST_GRADES_CACHE.get_or_call(
        symbol, lambda: fmp_history_disk_cached("analyst_grades", symbol, _do_fetch))


class FMPRating(AnalysisStatusRenderMixin, FMPApiKeyMixin, MarketExpertInterface):
    """
    FMPRating Expert Implementation
    
    Expert that uses FMP's analyst price target consensus and upgrade/downgrade data
    to generate trading recommendations. Calculates expected profit based on price
    targets weighted by analyst confidence and configurable profit ratio.
    """
    
    RENDER_PENDING_MESSAGE = 'FMPRating analysis for {symbol} is queued'
    RENDER_RUNNING_MESSAGE = 'Fetching analyst price targets for {symbol}...'

    @classmethod
    def description(cls) -> str:
        return "FMP analyst price consensus with profit potential calculation"
    
    def __init__(self, id: int):
        """Initialize FMPRating expert with database instance."""
        super().__init__(id)
        
        self._load_expert_instance(id)
        self._api_key = self._get_fmp_api_key()
        
        # Initialize expert-specific logger
        self.logger = get_expert_logger("FMPRating", id)
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for FMPRating expert."""
        return {
            "profit_ratio": {
                "type": "float", 
                "required": True, 
                "default": 1.0,
                "description": "Profit ratio multiplier for expected profit calculation",
                "tooltip": "Multiplier applied to the weighted price target delta. Default 1.0 means use full analyst consensus range. Lower values (0.5-0.8) are more conservative."
            },
            "min_analysts": {
                "type": "int",
                "required": True,
                "default": 10,
                "description": "Minimum total analyst ratings required to run analysis",
                "tooltip": "Total analyst count (Strong Buy + Buy + Hold + Sell + Strong Sell) must meet this threshold. If below, the analysis is skipped entirely. Default 10 avoids acting on thinly-covered stocks."
            },
            "target_price_type": {
                "type": "str",
                "required": True,
                "default": "consensus",
                "description": "Price target to use for profit calculation",
                "valid_values": ["low", "consensus", "median", "high", "low_consensus_avg"],
                "tooltip": "Which analyst price target to use for expected profit calculation. Options: 'low' (conservative), 'consensus' (average), 'median' (middle value), 'high' (optimistic), 'low_consensus_avg' (average of low and consensus). Default is 'consensus'."
            },
            "price_target_window_days": {
                "type": "int",
                "required": True,
                "default": 90,
                "description": "Backtest-only: trailing window (days) for the reconstructed consensus price target",
                "tooltip": "Used ONLY on the backtest (as_of) path. FMP exposes only the CURRENT consensus, so for a past as_of date the consensus price target is reconstructed as the rolling average of individual analyst price-target rows whose publishedDate is within this many days on/before the as_of date. Has no effect on the live (as_of=None) path, which keeps using FMP's current consensus endpoint. Default 90 days."
            },
            "min_price_targets_per_quarter": {
                "type": "int",
                "required": True,
                "default": 3,
                "description": "Minimum analyst price targets behind the consensus (0 = no check)",
                "tooltip": "FMP's price-target CONSENSUS is windowed to ~the last quarter, so a thinly-targeted name (e.g. a single recent analyst) yields a DEGENERATE consensus where high==low==median==consensus. This requires at least N price targets behind the consensus before acting; below it the analysis is skipped. This is DISTINCT from min_analysts, which counts RATINGS (Strong Buy..Strong Sell — a much larger pool that can be plentiful even when only 1 analyst set a price target). Live: counts targets in the trailing ~quarter (matching FMP's consensus window); backtest: counts the targets behind the reconstructed consensus (the price_target_window_days window). Default 3. Set 0 to disable the check."
            },
            "max_analyst_age_months": {
                "type": "int",
                "required": True,
                "default": 0,
                "description": "Recency window (months) for an analyst rating to count toward min_analysts (0 = no filter)",
                "tooltip": "Applies a recency filter to the analyst count BEFORE the min_analysts gate. When > 0, min_analysts counts only DISTINCT analysts who issued or affirmed a rating within this many months — read from FMP's DATED individual 'grades' endpoint, as-of the analysis date (no lookahead). So 'max_analyst_age_months=6, min_analysts=10' means 'at least 10 analysts active within 6 months'. When 0, the recency filter is OFF and the count falls back to FMP's full standing-analyst bucket sum (the undated upgrades-downgrades-consensus / grades-historical count), which can be inflated by long-stale ratings (e.g. ASC shows 17 standing vs ~2-4 active). Optimized in the grid as {0,3,6,9,12}."
            }
        }
    
    # ------------------------------------------------------------------
    # Backtest contract: _gather (provider/API I/O) + _process (pure). The SAME
    # pure decision math (_calculate_recommendation) runs live (run_analysis,
    # as_of=None) and in backtest (analyze_as_of, as_of=<date>). Only the current
    # price is routed through the providers bundle (Decision 1: one price source
    # for all experts); the FMP endpoints stay direct (not in the get_provider
    # registry).
    #
    # AS_OF reconstruction (resolves the former Decision-4 caveat — FMPRating is now
    # a real backtestable expert):
    #   * LIVE (as_of=None): FMP's price-target-consensus and
    #     upgrades-downgrades-consensus endpoints return ONLY the latest snapshot
    #     (n=1, NO per-row date). The live path keeps using those snapshots
    #     UNCHANGED — using a past as_of against THOSE endpoints would be a
    #     lookahead because the snapshot reflects today's analyst view.
    #   * BACKTEST (as_of set): instead of the latest-snapshot endpoints, _gather
    #     reconstructs BOTH inputs as-of the date with NO LOOKAHEAD from FMP's dated
    #     history — buy/hold/sell counts from grades-historical (latest dated row
    #     <= as_of) and the consensus price target from a rolling average of
    #     v4/price-target analyst rows whose publishedDate <= as_of within the
    #     trailing price_target_window_days window. The reconstructed dicts are
    #     shaped identically to the live snapshots, so _calculate_recommendation
    #     runs verbatim and the golden test now exercises the as_of path too.
    # A regression test guards that this docstring keeps describing the live
    # snapshot/lookahead distinction so the design rationale is never silently lost.
    # ------------------------------------------------------------------
    _SETTING_KEYS = ("profit_ratio", "min_analysts", "target_price_type",
                     "price_target_window_days", "min_price_targets_per_quarter",
                     "max_analyst_age_months")

    @staticmethod
    def _count_analysts(upgrade_data: Optional[list]) -> int:
        """Total analyst count from the latest upgrade/downgrade-consensus row.

        Analyst counts live on the upgrades-downgrades-consensus endpoint (NOT the
        price-target-consensus), so this reads upgrade_data[0]'s rating buckets —
        identical to the inline computation the live run_analysis used (lines
        596-604 of the pre-refactor file)."""
        if not upgrade_data or len(upgrade_data) == 0:
            return 0
        latest = upgrade_data[0]
        return (
            latest.get('strongBuy', 0) + latest.get('buy', 0) +
            latest.get('hold', 0) + latest.get('sell', 0) +
            latest.get('strongSell', 0)
        )

    @staticmethod
    def _count_targets_in_window(price_target_history: Optional[list],
                                 ref_date: datetime, window_days: int) -> int:
        """Count individual analyst price targets published within ``window_days``
        on/before ``ref_date`` (the pool behind a windowed consensus).

        Mirrors the no-lookahead filter in ``_consensus_target_as_of`` (a row counts
        only when its ``publishedDate`` is in (ref_date - window_days, ref_date] and it
        carries a non-null ``priceTarget``). Used to guard against a degenerate
        thinly-targeted consensus (see ``min_price_targets_per_quarter``)."""
        if not price_target_history:
            return 0
        from datetime import timedelta
        floor = ref_date - timedelta(days=int(window_days))
        n = 0
        for r in price_target_history:
            d = _memo_provider_date(r, "publishedDate")
            if d is None or d > ref_date or d < floor:
                continue
            if r.get("priceTarget") is not None:
                n += 1
        return n

    @staticmethod
    def _count_recent_analysts(grades: Optional[list], ref_date: datetime,
                               window_months: int) -> int:
        """Count DISTINCT analysts (``gradingCompany``) with a grade dated within
        ``window_months`` (≈30 days/month) on/before ``ref_date`` — the recency-filtered
        coverage count for the ``min_analysts`` gate.

        NO-LOOKAHEAD: rows dated after ``ref_date``, or older than the trailing window, are
        ignored. Distinct by ``gradingCompany`` so an analyst who re-affirmed several times in
        the window counts once. Reads FMP's dated individual ``grades`` rows (see
        ``fetch_analyst_grades_cached``). Returns 0 for an empty list or ``window_months<=0``."""
        if not grades or window_months <= 0:
            return 0
        from datetime import timedelta
        floor = ref_date - timedelta(days=int(window_months) * _DAYS_PER_MONTH)
        seen = set()
        for r in grades:
            d = _memo_provider_date(r, "date")
            if d is None or d > ref_date or d < floor:
                continue
            company = r.get("gradingCompany")
            if company:
                seen.add(company)
        return len(seen)

    def _gather(self, providers: ProviderBundle, as_of: Optional[datetime]) -> Dict[str, Any]:
        """Fetch (live) or reconstruct (backtest) the consensus + upgrade inputs and
        the as_of close.

        LIVE (as_of=None): use FMP's CURRENT consensus snapshots UNCHANGED
        (price-target-consensus + upgrades-downgrades-consensus, n=1).

        BACKTEST (as_of set): FMP exposes only the current consensus, so reconstruct
        BOTH inputs as-of the date (no-lookahead) from dated FMP history and feed the
        EXISTING pure math verbatim:
          * upgrade_data  <- _counts_as_of(grades-historical)  (latest row date<=as_of)
          * consensus_data <- _consensus_target_as_of(v4/price-target)  (rolling avg
            of analyst targets whose publishedDate<=as_of within the trailing window)
        The reconstructed dicts are shaped identically to the live snapshots.
        """
        symbol = self._gather_symbol
        # Rating-recency: only fetch the dated individual grades when the recency filter is
        # active (max_analyst_age_months > 0), so a run that leaves it OFF needs no extra fetch
        # (and a hermetic backtest without the gene never requires the grades cache).
        max_age = int(getattr(self, "_gather_max_analyst_age", 0) or 0)
        analyst_grades = None
        if as_of is None:
            # LIVE path — unchanged: current consensus snapshots.
            consensus_data = self._fetch_price_target_consensus(symbol)
            # Only fetch upgrade/downgrade data when there is consensus coverage,
            # mirroring the live run_analysis ordering (it skips before fetching
            # upgrades when consensus is None).
            upgrade_data = (self._fetch_upgrade_downgrade(symbol)
                            if consensus_data is not None else None)
            if consensus_data is not None and max_age > 0:
                analyst_grades = self._fetch_analyst_grades(symbol)
            # Count the price targets behind FMP's live consensus (windowed to ~the last
            # quarter) so _process can reject a thinly-targeted DEGENERATE consensus. The
            # consensus endpoint exposes no count, so derive it from the individual
            # price-target history over the trailing quarter. Copy the cached consensus
            # dict before annotating so the shared TTLCache entry is never mutated.
            if consensus_data is not None:
                consensus_data = dict(consensus_data)
                pt_history = self._fetch_price_target_history(symbol)
                consensus_data["targetCount"] = self._count_targets_in_window(
                    pt_history, datetime.now(timezone.utc), _QUARTER_DAYS)
            current_price = self._get_current_price(symbol)
        else:
            # BACKTEST path — no-lookahead reconstruction from dated history.
            window_days = int(getattr(self, "_gather_window_days", 90))
            grades_history = self._fetch_grades_historical(symbol)
            price_target_history = self._fetch_price_target_history(symbol)
            consensus_data = self._consensus_target_as_of(
                price_target_history, as_of, window_days)
            # Mirror the live ordering: only assemble counts when there is consensus
            # coverage as-of the date (no consensus target => no coverage => skip).
            upgrade_data = (self._counts_as_of(grades_history, as_of)
                            if consensus_data is not None else None)
            if consensus_data is not None and max_age > 0:
                analyst_grades = self._fetch_analyst_grades(symbol)
            current_price = providers.price_at_date(symbol, as_of)
        return {"consensus_data": consensus_data, "upgrade_data": upgrade_data,
                "current_price": current_price, "symbol": symbol,
                "analyst_grades": analyst_grades}

    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: Optional[datetime] = None) -> Recommendation:
        """PURE decision logic. SKIP is first-class: no analyst coverage and
        insufficient-analyst-count both return skip=True (preserving the live
        FMPRating SKIPPED outcomes). settings is a resolved plain dict; _process
        never reads self for config (target_price_type is passed through)."""
        consensus = data_bundle["consensus_data"]
        current_price = data_bundle["current_price"]
        if not consensus:
            return Recommendation(
                signal=OrderRecommendation.HOLD, confidence=0.0,
                current_price=current_price, details="No analyst coverage",
                expected_profit_percent=0.0,
                skip=True, skip_reason="no consensus data")

        # Guard against a DEGENERATE thinly-targeted consensus: FMP windows its
        # price-target consensus to ~the last quarter, so a name with a single recent
        # analyst yields high==low==median (a one-analyst "consensus"). Require at least
        # min_price_targets_per_quarter targets behind the consensus; 0 disables the
        # check (the value the grid explores as "no check"). Distinct from min_analysts,
        # which counts RATINGS (a much larger pool).
        min_targets = int(settings.get("min_price_targets_per_quarter", 0) or 0)
        if min_targets > 0:
            target_count = consensus.get("targetCount")
            if target_count is None or target_count < min_targets:
                return Recommendation(
                    signal=OrderRecommendation.HOLD, confidence=0.0,
                    current_price=current_price,
                    details=(f"Insufficient price targets behind consensus "
                             f"({target_count if target_count is not None else 'unknown'} "
                             f"< {min_targets})"),
                    expected_profit_percent=0.0,
                    skip=True, skip_reason="insufficient price targets")

        upgrade_data = data_bundle["upgrade_data"]
        min_analysts = int(settings["min_analysts"])
        # Rating-recency filter (kicks BEFORE the min_analysts gate): when
        # max_analyst_age_months > 0, count only DISTINCT analysts who issued/affirmed a rating
        # within that window (from the dated individual `grades`), as-of the analysis date — so
        # "6mo + min_analysts 10" means 10 analysts active within 6 months. 0 = no recency
        # filter: fall back to FMP's full standing-analyst bucket sum (which can be inflated by
        # long-stale ratings). The buckets still drive the signal direction/confidence below.
        max_age = int(settings.get("max_analyst_age_months", 0) or 0)
        if max_age > 0:
            ref_date = as_of if as_of is not None else datetime.now(timezone.utc)
            analyst_count = self._count_recent_analysts(
                data_bundle.get("analyst_grades"), ref_date, max_age)
            count_desc = f"{analyst_count} active within {max_age}mo"
        else:
            analyst_count = self._count_analysts(upgrade_data)
            count_desc = str(analyst_count)
        if analyst_count < min_analysts:
            return Recommendation(
                signal=OrderRecommendation.HOLD, confidence=0.0,
                current_price=current_price,
                details=f"Insufficient analysts ({count_desc} < {min_analysts})",
                expected_profit_percent=0.0,
                skip=True, skip_reason="insufficient analysts")

        rec = self._calculate_recommendation(
            consensus, upgrade_data, current_price,
            float(settings["profit_ratio"]), min_analysts,
            settings["target_price_type"])
        return Recommendation(
            signal=rec["signal"], confidence=round(rec["confidence"], 1),
            current_price=current_price, details=rec["details"],
            expected_profit_percent=rec["expected_profit_percent"],
            # Surface the analyst price target so the backtest can reference it for the
            # initial TP bracket (Prereq 2 / S1 fidelity); None -> backtest falls back to
            # expected_profit_percent. This is the SAME target the profit math used.
            target_price=rec.get("target_price"),
            raw_outputs={"name": "Analyst Rating Analysis",
                         "type": "analyst_rating_analysis", "text": rec["details"],
                         "calc": rec})

    def analyze_as_of(self, as_of: datetime, context: BacktestContext) -> Recommendation:
        """BacktestInterface entry: runs the SAME _gather+_process as the live path.

        The backtest (as_of) path reconstructs BOTH consensus inputs as-of the date
        (no-lookahead) from FMP's dated grades-historical + v4/price-target history
        (see _gather), so FMPRating is a real backtestable expert. The trailing
        window for the reconstructed price target is read from
        settings['price_target_window_days'] (default 90)."""
        self._gather_symbol = context.extra.get("symbol", getattr(self, "_gather_symbol", None))
        self._gather_window_days = int(context.settings.get("price_target_window_days", 90))
        self._gather_max_analyst_age = int(context.settings.get("max_analyst_age_months", 0) or 0)
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)

    def _fetch_price_target_consensus(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetch price target consensus from FMP API.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            API response data or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch price target consensus: FMP API key not configured")
            return None

        def _do_fetch():
            url = "https://financialmodelingprep.com/api/v4/price-target-consensus"
            params = {"symbol": symbol, "apikey": self._api_key}
            self.logger.debug(f"Fetching FMP price target consensus for {symbol}")
            try:
                # Shared backoff-retry helper handles HTTP 429 / 5xx / transient errors.
                response = fmp_http_get(url, params, symbol=symbol,
                                        endpoint="price-target-consensus", timeout=60)
            except FMPError as e:
                # Surface as ValueError to preserve the existing caller contract.
                raise ValueError(str(e)) from e

            data = response.json()
            self.logger.debug(f"Received price target consensus data for {symbol}")

            # FMP returns a list with one item; extract the first element.
            if isinstance(data, list) and len(data) > 0:
                return data[0]
            elif isinstance(data, dict):
                return data
            elif isinstance(data, list):
                self.logger.warning(f"No price target consensus data for {symbol} (empty list — likely no analyst coverage)")
                return None
            else:
                self.logger.warning(f"Unexpected price target consensus format for {symbol}: {type(data)}")
                return None

        # Deduped across experts within the TTL window.
        return _CONSENSUS_CACHE.get_or_call(symbol, _do_fetch)
    
    def _fetch_upgrade_downgrade(self, symbol: str) -> Optional[list]:
        """
        Fetch analyst upgrade/downgrade summary from FMP API.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            API response data or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch upgrade/downgrade data: FMP API key not configured")
            return None

        def _do_fetch():
            url = "https://financialmodelingprep.com/api/v4/upgrades-downgrades-consensus"
            params = {"symbol": symbol, "apikey": self._api_key}
            self.logger.debug(f"Fetching FMP upgrade/downgrade consensus for {symbol}")
            try:
                response = fmp_http_get(url, params, symbol=symbol,
                                        endpoint="upgrades-downgrades-consensus", timeout=60)
            except FMPError as e:
                raise ValueError(str(e)) from e

            data = response.json()
            self.logger.debug(f"Received {len(data) if isinstance(data, list) else 0} upgrade/downgrade records")
            # Return the list as-is (used to count analysts).
            return data if isinstance(data, list) else None

        return _UPGRADE_CACHE.get_or_call(symbol, _do_fetch)

    # ------------------------------------------------------------------
    # BACKTEST-PATH (as_of) reconstruction: dated fetchers + pure no-lookahead
    # reconstructors. FMP exposes only the CURRENT consensus on the two
    # price-target-consensus / upgrades-downgrades-consensus endpoints (n=1, no
    # per-row date), so the LIVE (as_of=None) path keeps using those snapshots
    # UNCHANGED. For the BACKTEST (as_of set) path we instead reconstruct the
    # SAME two inputs as-of each date from dated FMP history:
    #   * Buy/Hold/Sell counts  <- grades-historical (dated StrongBuy..StrongSell)
    #   * Consensus price target <- rolling average of v4/price-target individual
    #     analyst targets whose publishedDate <= as_of, over a trailing window.
    # The reconstructed dicts are shaped IDENTICALLY to the live snapshots so the
    # EXISTING pure math (_calculate_recommendation) runs verbatim on either path.
    # The reconstructors are pure + no-lookahead: only rows dated on/before as_of
    # are ever used; rows after as_of are ignored.
    # ------------------------------------------------------------------

    # grades-historical rows use FMP's verbose ``analystRatings*`` field names;
    # map each rating bucket to the short names _calculate_recommendation expects.
    # Both forms are accepted so the reconstruction is robust to FMP field casing.
    _GRADES_FIELD_ALIASES = {
        "strongBuy": ("analystRatingsStrongBuy", "strongBuy"),
        "buy": ("analystRatingsbuy", "analystRatingsBuy", "buy"),
        "hold": ("analystRatingsHold", "hold"),
        "sell": ("analystRatingsSell", "sell"),
        "strongSell": ("analystRatingsStrongSell", "strongSell"),
    }

    def _fetch_grades_historical(self, symbol: str) -> list:
        """Fetch the FULL dated analyst-grade history for a symbol (backtest path).

        Endpoint: ``stable/grades-historical?symbol=`` — rows carry a ``date`` plus
        dated StrongBuy/Buy/Hold/Sell/StrongSell counts. Returns the raw list (the
        no-lookahead date filtering happens in ``_counts_as_of``). Cached per symbol
        (the full history is time-invariant within the TTL window)."""
        if not self._api_key:
            self.logger.error("Cannot fetch grades-historical: FMP API key not configured")
            return []

        # Delegate to the module-level fetcher (reusable by the optimization pre-warm,
        # which has no FMPRating instance). Behaviour is byte-identical to the prior
        # inline _do_fetch + TTLCache/disk-cache wrapping.
        return fetch_grades_historical_cached(self._api_key, symbol)

    def _fetch_price_target_history(self, symbol: str) -> list:
        """Fetch the FULL dated individual analyst price-target history (backtest path).

        Endpoint: ``v4/price-target?symbol=`` — rows carry ``publishedDate`` and
        ``priceTarget`` (one row per analyst note). Returns the raw list (the
        no-lookahead window filtering + averaging happen in
        ``_consensus_target_as_of``). Cached per symbol."""
        if not self._api_key:
            self.logger.error("Cannot fetch price-target history: FMP API key not configured")
            return []

        # Delegate to the module-level fetcher (reusable by the optimization pre-warm).
        # Behaviour is byte-identical to the prior inline _do_fetch + cache wrapping.
        return fetch_price_target_history_cached(self._api_key, symbol)

    def _fetch_analyst_grades(self, symbol: str) -> list:
        """Fetch the FULL dated INDIVIDUAL analyst-grade history (rating-recency path).

        Endpoint: ``stable/grades?symbol=`` — one row per analyst action (``date`` +
        ``gradingCompany``). Returns the raw list (the no-lookahead window filtering +
        distinct-analyst counting happen in ``_count_recent_analysts``). Cached per symbol via
        the module-level fetcher (reusable by the optimization pre-warm)."""
        if not self._api_key:
            self.logger.error("Cannot fetch analyst grades: FMP API key not configured")
            return []
        return fetch_analyst_grades_cached(self._api_key, symbol)

    @classmethod
    def _counts_as_of(cls, grades_history: list, as_of: datetime) -> Optional[list]:
        """Reconstruct the upgrade/downgrade-shaped analyst counts as-of ``as_of``.

        NO-LOOKAHEAD: takes the LATEST grades-historical row whose ``date`` is
        on/before ``as_of`` (rows dated after as_of are ignored). Returns a
        single-element list ``[{strongBuy, buy, hold, sell, strongSell}]`` shaped
        EXACTLY like the live upgrades-downgrades-consensus payload, so
        ``_calculate_recommendation`` / ``_count_analysts`` consume it verbatim.
        Returns ``None`` when no row qualifies (no coverage as-of that date)."""
        if not grades_history:
            return None
        eligible = [
            r for r in grades_history
            if (d := _memo_provider_date(r, "date")) is not None and d <= as_of
        ]
        if not eligible:
            return None
        latest = max(eligible, key=lambda r: _memo_provider_date(r, "date"))

        def _val(row, names):
            for n in names:
                if n in row and row[n] is not None:
                    try:
                        return int(row[n])
                    except (TypeError, ValueError):
                        return 0
            return 0

        return [{k: _val(latest, aliases) for k, aliases in cls._GRADES_FIELD_ALIASES.items()}]

    @staticmethod
    def _consensus_target_as_of(price_target_history: list, as_of: datetime,
                                window_days: int) -> Optional[Dict[str, Any]]:
        """Reconstruct the consensus-price-target-shaped dict as-of ``as_of``.

        NO-LOOKAHEAD: averages the individual analyst ``priceTarget`` values whose
        ``publishedDate`` is within ``window_days`` on/before ``as_of`` (rows
        published after as_of, or older than the trailing window, are ignored).
        Returns a dict shaped EXACTLY like the live price-target-consensus payload
        (``targetConsensus`` = mean, ``targetHigh`` = max, ``targetLow`` = min,
        ``targetMedian`` = median over the window), so ``_calculate_recommendation``
        consumes it verbatim. Returns ``None`` when no target falls in the window."""
        if not price_target_history:
            return None
        from datetime import timedelta
        floor = as_of - timedelta(days=int(window_days))
        targets = []
        for r in price_target_history:
            d = _memo_provider_date(r, "publishedDate")
            if d is None or d > as_of or d < floor:
                continue
            pt = r.get("priceTarget")
            if pt is None:
                continue
            try:
                targets.append(float(pt))
            except (TypeError, ValueError):
                continue
        if not targets:
            return None
        targets_sorted = sorted(targets)
        n = len(targets_sorted)
        if n % 2 == 1:
            median = targets_sorted[n // 2]
        else:
            median = (targets_sorted[n // 2 - 1] + targets_sorted[n // 2]) / 2.0
        return {
            "targetConsensus": sum(targets) / n,
            "targetHigh": max(targets),
            "targetLow": min(targets),
            "targetMedian": median,
            # Number of analyst targets behind THIS reconstructed consensus (the pool
            # over the trailing window) — guards a degenerate thinly-targeted consensus
            # (see min_price_targets_per_quarter in _process).
            "targetCount": n,
        }

    def _calculate_recommendation(self, consensus_data: Dict[str, Any],
                                 upgrade_data: list,
                                 current_price: float,
                                 profit_ratio: float,
                                 min_analysts: int,
                                 target_price_type: str = "consensus") -> Dict[str, Any]:
        """
        Calculate trading recommendation from price target consensus.

        New Formula (matching FinnHub methodology with price target boost):
        1. Calculate base score from analyst buy/sell ratings (FinnHub style)
        2. Determine signal based on dominant rating
        3. Calculate price target boost from current price to lower/consensus targets
        4. Average the boosts and add to base confidence
        5. Clamp final confidence to 0-100%

        Args:
            consensus_data: Price target consensus from FMP
            upgrade_data: Upgrade/downgrade data from FMP
            current_price: Current stock price
            profit_ratio: Profit ratio multiplier setting
            min_analysts: Minimum analysts required
            target_price_type: which analyst target to use ('low'|'consensus'|'median'
                |'high'|'low_consensus_avg'). Phase 1: passed IN as a parameter so
                _process never reads self for config (was an internal
                self.get_setting_with_interface_default call). Defaults to 'consensus'
                to preserve the prior behaviour for any direct legacy caller.

        Returns:
            Dictionary with recommendation details
        """
        if not consensus_data:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'expected_profit_percent': 0.0,
                'details': 'No price target consensus data available',
                'target_consensus': None,
                'target_high': None,
                'target_low': None,
                'target_median': None,
                'analyst_count': 0
            }
        
        # Extract consensus data
        target_consensus = consensus_data.get('targetConsensus')
        target_high = consensus_data.get('targetHigh')
        target_low = consensus_data.get('targetLow')
        target_median = consensus_data.get('targetMedian')
        
        # Get analyst ratings from upgrade/downgrade data
        analyst_count = 0
        strong_buy = 0
        buy = 0
        hold = 0
        sell = 0
        strong_sell = 0
        
        if upgrade_data and len(upgrade_data) > 0:
            latest_grade = upgrade_data[0]
            # Sum all rating categories to get total analyst count
            strong_buy = latest_grade.get('strongBuy', 0)
            buy = latest_grade.get('buy', 0)
            hold = latest_grade.get('hold', 0)
            sell = latest_grade.get('sell', 0)
            strong_sell = latest_grade.get('strongSell', 0)
            analyst_count = strong_buy + buy + hold + sell + strong_sell
        
        # Check minimum analysts threshold
        if analyst_count < min_analysts:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 20.0,  # Low confidence due to insufficient data
                'expected_profit_percent': 0.0,
                'details': f'Insufficient analyst coverage ({analyst_count} analysts, minimum {min_analysts} required)',
                'target_consensus': target_consensus,
                'target_high': target_high,
                'target_low': target_low,
                'target_median': target_median,
                'analyst_count': analyst_count
            }
        
        # === NEW CONFIDENCE CALCULATION (FinnHub style with price target boost) ===
        
        # Step 1: Calculate base score from analyst ratings (same as FinnHub)
        strong_factor = 2.0  # Weight for strong ratings
        buy_score = (strong_buy * strong_factor) + buy
        sell_score = (strong_sell * strong_factor) + sell
        hold_score = hold
        
        total_weighted = buy_score + sell_score + hold_score
        
        # Step 2: Determine signal and base confidence from ratings
        if buy_score > sell_score and buy_score > hold_score:
            signal = OrderRecommendation.BUY
            dominant_score = buy_score
        elif sell_score > buy_score and sell_score > hold_score:
            signal = OrderRecommendation.SELL
            dominant_score = sell_score
        else:
            signal = OrderRecommendation.HOLD
            dominant_score = hold_score
        
        # Select target price based on setting (passed in via _process; see signature)
        if target_price_type == 'low':
            target_price = target_low
        elif target_price_type == 'high':
            target_price = target_high
        elif target_price_type == 'median':
            target_price = target_median
        elif target_price_type == 'low_consensus_avg':
            if target_low is not None and target_consensus is not None:
                target_price = (target_low + target_consensus) / 2
            else:
                target_price = target_consensus or target_low
        else:  # 'consensus' is default
            target_price = target_consensus
        
        # Base confidence from analyst consensus (0-100 scale)
        base_confidence = (dominant_score / total_weighted * 100) if total_weighted > 0 else 0.0
        
        # Step 3: Calculate price target boost.
        # boost_to_* is the signed % distance from the current price to each target
        # (positive when the target is ABOVE the current price). This is the
        # BUY-oriented magnitude; the directional sign is applied in Step 4.
        price_target_boost = 0.0
        boost_to_lower = 0.0
        boost_to_consensus = 0.0

        if current_price and target_low and target_consensus:
            boost_to_lower = ((target_low - current_price) / current_price) * 100
            boost_to_consensus = ((target_consensus - current_price) / current_price) * 100
            # Average the two boosts (still BUY-oriented at this point)
            price_target_boost = (boost_to_lower + boost_to_consensus) / 2.0

        # Step 4: Apply the price target boost to base confidence, ORIENTED BY SIGNAL.
        # BUY: upside above the current price (positive boost) increases confidence.
        # SELL: the short thesis is stronger the further the targets sit BELOW the
        #       current price, so the sign is flipped — otherwise more downside would
        #       wrongly REDUCE SELL confidence (the original bug).
        # HOLD: directional boost is meaningless, so none is applied.
        if signal == OrderRecommendation.BUY:
            applied_boost = price_target_boost
        elif signal == OrderRecommendation.SELL:
            applied_boost = -price_target_boost
        else:
            applied_boost = 0.0
        confidence = base_confidence + applied_boost
        
        # Step 5: Clamp final confidence to 0-100%
        confidence = max(0.0, min(100.0, confidence))
        
        # Conservative-target guards: if a one-sided target (low/high) is already
        # behind the current price relative to the trade direction, the trade has
        # no remaining edge from that target alone. Demote to HOLD with a low
        # confidence floor so the entry ruleset rejects it cleanly.
        # Applies when target_price_type explicitly picked one tail ('low' or
        # 'high'), not for averaged/consensus targets.
        if target_price and current_price:
            if (signal == OrderRecommendation.BUY
                    and target_price_type == 'low'
                    and target_price <= current_price):
                signal = OrderRecommendation.HOLD
                confidence = min(confidence, 10.0)
                self.logger.info(
                    f"FMPRating: demoting BUY -> HOLD with "
                    f"target_low=${target_price:.2f} <= current=${current_price:.2f} "
                    f"(target_price_type='low'); no upside vs. the bear target"
                )
            elif (signal == OrderRecommendation.SELL
                    and target_price_type == 'high'
                    and target_price >= current_price):
                signal = OrderRecommendation.HOLD
                confidence = min(confidence, 10.0)
                self.logger.info(
                    f"FMPRating: demoting SELL -> HOLD with "
                    f"target_high=${target_price:.2f} >= current=${current_price:.2f} "
                    f"(target_price_type='high'); no downside vs. the bull target"
                )

        # Calculate expected profit
        if signal == OrderRecommendation.BUY and target_price and current_price:
            # Profit potential: (target - current) * confidence * profit_ratio
            price_delta = target_price - current_price
            weighted_delta = price_delta * (confidence / 100.0) * profit_ratio
            expected_profit_percent = (weighted_delta / current_price) * 100
        elif signal == OrderRecommendation.SELL and target_price and current_price:
            # For SELL, profit is from current to consensus target
            price_delta = current_price - target_price
            weighted_delta = price_delta * (confidence / 100.0) * profit_ratio
            expected_profit_percent = (weighted_delta / current_price) * 100
        else:
            expected_profit_percent = 0.0
        
        # Build display values for target prices (any can be None)
        tc_display = f"${target_consensus:.2f}" if target_consensus is not None else "N/A"
        th_display = f"${target_high:.2f}" if target_high is not None else "N/A"
        tl_display = f"${target_low:.2f}" if target_low is not None else "N/A"
        tm_display = f"${target_median:.2f}" if target_median is not None else "N/A"
        tp_display = f"${target_price:.2f}" if target_price is not None else "N/A"

        tc_pct = f"{((target_consensus - current_price) / current_price * 100):.1f}% from current" if target_consensus is not None and current_price else "N/A"
        th_pct = f"{((target_high - current_price) / current_price * 100):.1f}% from current" if target_high is not None and current_price else "N/A"
        tl_pct = f"{((target_low - current_price) / current_price * 100):.1f}% from current" if target_low is not None and current_price else "N/A"
        tm_pct = f"{((target_median - current_price) / current_price * 100):.1f}% from current" if target_median is not None and current_price else "N/A"

        # Build profit calculation lines (require target_price and current_price)
        if target_price is not None and current_price:
            profit_calc = f"""Expected Profit Calculation (using {target_price_type} target):
Price Delta = {target_price_type.capitalize()} Target - Current = {tp_display} - ${current_price:.2f} = ${target_price - current_price:.2f}
Weighted Delta = Price Delta × Confidence × Profit Ratio = ${target_price - current_price:.2f} × {confidence/100:.2f} × {profit_ratio} = ${(target_price - current_price) * (confidence/100) * profit_ratio:.2f}
Expected Profit % = (Weighted Delta / Current) × 100 = {expected_profit_percent:.1f}%"""
        else:
            profit_calc = f"""Expected Profit Calculation (using {target_price_type} target):
Target price data unavailable - cannot calculate expected profit.
Expected Profit % = {expected_profit_percent:.1f}%"""

        # Build boost calculation lines (require target_low, target_consensus, current_price)
        if target_low is not None and target_consensus is not None and current_price:
            boost_calc = f"""Step 3 - Price Target Boost:
Boost to Lower Target = (({tl_display} - ${current_price:.2f}) / ${current_price:.2f}) × 100 = {boost_to_lower:.1f}%
Boost to Consensus = (({tc_display} - ${current_price:.2f}) / ${current_price:.2f}) × 100 = {boost_to_consensus:.1f}%
Avg Price Target Boost = ({boost_to_lower:.1f}% + {boost_to_consensus:.1f}%) / 2 = {price_target_boost:.1f}%"""
        else:
            boost_calc = f"""Step 3 - Price Target Boost:
Price target data unavailable - boost set to {price_target_boost:.1f}%"""

        # Build details string
        details = f"""FMP Analyst Price Target Consensus Analysis

Current Price: ${current_price:.2f}

Analyst Ratings:
- Strong Buy: {strong_buy}
- Buy: {buy}
- Hold: {hold}
- Sell: {sell}
- Strong Sell: {strong_sell}
Total Analysts: {analyst_count}

Price Targets:
- Consensus Target: {tc_display} ({tc_pct})
- High Target: {th_display} ({th_pct})
- Low Target: {tl_display} ({tl_pct})
- Median Target: {tm_display} ({tm_pct})

Recommendation: {signal.value}
Confidence: {confidence:.1f}%
Expected Profit: {expected_profit_percent:.1f}%

Confidence Calculation (FinnHub Methodology + Price Target Boost):

Step 1 - Weighted Scores (Strong Factor: {strong_factor}x):
Buy Score = (Strong Buy × {strong_factor}) + Buy = ({strong_buy} × {strong_factor}) + {buy} = {buy_score:.1f}
Hold Score = Hold = {hold}
Sell Score = (Strong Sell × {strong_factor}) + Sell = ({strong_sell} × {strong_factor}) + {sell} = {sell_score:.1f}
Total Weighted = {total_weighted:.1f}

Step 2 - Base Confidence from Analyst Ratings:
Base Confidence = Dominant Score / Total × 100 = {dominant_score:.1f} / {total_weighted:.1f} × 100 = {base_confidence:.1f}%

{boost_calc}

Step 4 - Final Confidence (clamped to 0-100%):
Final Confidence = Base Confidence + Directional Boost ({signal.value}) = {base_confidence:.1f}% + {applied_boost:+.1f}% = {confidence:.1f}%
(Boost sign is oriented by signal: BUY adds upside, SELL adds downside, HOLD adds none.)

{profit_calc}
"""
        
        return {
            'signal': signal,
            'confidence': confidence,
            'expected_profit_percent': expected_profit_percent,
            'details': details,
            'target_consensus': target_consensus,
            'target_high': target_high,
            'target_low': target_low,
            'target_median': target_median,
            'analyst_count': analyst_count,
            # New calculation components
            'strong_buy': strong_buy,
            'buy': buy,
            'hold': hold,
            'sell': sell,
            'strong_sell': strong_sell,
            'buy_score': buy_score,
            'sell_score': sell_score,
            'hold_score': hold_score,
            'base_confidence': base_confidence,
            'boost_to_lower': boost_to_lower,
            'boost_to_consensus': boost_to_consensus,
            'price_target_boost': price_target_boost,
            'applied_boost': applied_boost,
            'target_price': target_price
        }
    
    def _create_expert_recommendation(self, recommendation_data: Dict[str, Any], 
                                     symbol: str, market_analysis_id: int,
                                     current_price: Optional[float]) -> int:
        """Create ExpertRecommendation record in database."""
        try:
            expert_recommendation = ExpertRecommendation(
                instance_id=self.id,
                symbol=symbol,
                recommended_action=recommendation_data['signal'],
                expected_profit_percent=recommendation_data['expected_profit_percent'],
                price_at_date=current_price,
                details=recommendation_data['details'][:100000] if recommendation_data['details'] else None,
                confidence=round(recommendation_data['confidence'], 1),  # Store as 1-100 scale
                risk_level=RiskLevel.MEDIUM,  # Always medium risk
                time_horizon=TimeHorizon.MEDIUM_TERM,  # Always medium term
                market_analysis_id=market_analysis_id,
                # Persist analyst price targets so downstream consumers (e.g. the
                # option `consensus_target` strike-selection method) can read the
                # true consensus target. Nested under the expert name by convention.
                data={
                    "FMPRating": {
                        "target_consensus": recommendation_data['target_consensus'],
                        "target_high": recommendation_data['target_high'],
                        "target_low": recommendation_data['target_low'],
                        "target_median": recommendation_data['target_median'],
                    }
                },
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            self.logger.info(f"Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal'].value} with {recommendation_data['confidence']:.1f}% confidence, "
                       f"expected profit: {recommendation_data['expected_profit_percent']:.1f}%")
            return recommendation_id
            
        except Exception as e:
            self.logger.error(f"Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
            raise
    
    def _format_analyst_details_md(self, symbol: str, limit: int = 15) -> Optional[str]:
        """Markdown of the most recent INDIVIDUAL analyst ratings + price targets, for the UI.

        LIVE-ONLY (called from _store_analysis_outputs, which the backtest never invokes), so it
        has NO effect on the backtest/_process decision path. Reads the dated grades + price-target
        history caches (both already warmed); best-effort — any fetch error returns None so it can
        never break analysis persistence. Returns None when neither source has data."""
        try:
            grades = self._fetch_analyst_grades(symbol) or []
            targets = self._fetch_price_target_history(symbol) or []
        except Exception as e:  # noqa: BLE001 — detail block is best-effort, never fatal
            self.logger.debug(f"Analyst details unavailable for {symbol}: {e}")
            return None
        if not grades and not targets:
            return None

        _floor = datetime.min.replace(tzinfo=timezone.utc)

        def _ds(row, key):
            d = _memo_provider_date(row, key)
            return d.strftime("%Y-%m-%d") if d is not None else "?"

        def _money(v):
            return f"${v:.2f}" if isinstance(v, (int, float)) else "?"

        parts: list = []
        if grades:
            recent = sorted(grades, key=lambda r: _memo_provider_date(r, "date") or _floor,
                            reverse=True)[:limit]
            parts.append(f"**Recent Analyst Ratings** ({len(grades)} total)\n")
            parts.append("| Date | Analyst | Action | Grade |")
            parts.append("|---|---|---|---|")
            for r in recent:
                parts.append(f"| {_ds(r, 'date')} | {r.get('gradingCompany', '?')} | "
                             f"{r.get('action', '')} | {r.get('newGrade', '')} |")
            parts.append("")
        if targets:
            recent = sorted(targets, key=lambda r: _memo_provider_date(r, "publishedDate") or _floor,
                            reverse=True)[:limit]
            parts.append(f"**Recent Price Targets** ({len(targets)} total)\n")
            parts.append("| Date | Analyst | Price Target | Price When Posted |")
            parts.append("|---|---|---|---|")
            for r in recent:
                company = r.get("analystCompany") or r.get("analystName") or "?"
                parts.append(f"| {_ds(r, 'publishedDate')} | {company} | "
                             f"{_money(r.get('priceTarget'))} | {_money(r.get('priceWhenPosted'))} |")
            parts.append("")
        return "\n".join(parts)

    def _store_analysis_outputs(self, market_analysis_id: int, symbol: str,
                               recommendation_data: Dict[str, Any],
                               consensus_data: Dict[str, Any],
                               upgrade_data: list) -> None:
        """Store detailed analysis outputs in database."""
        session = get_db()
        
        try:
            # Store analysis details
            details_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="FMP Price Target Analysis",
                type="fmp_rating_analysis",
                text=recommendation_data['details']
            )
            session.add(details_output)
            
            # Store price targets as structured output
            tc = recommendation_data['target_consensus']
            th = recommendation_data['target_high']
            tl = recommendation_data['target_low']
            tm = recommendation_data['target_median']
            tc_str = f"${tc:.2f}" if tc is not None else "N/A"
            th_str = f"${th:.2f}" if th is not None else "N/A"
            tl_str = f"${tl:.2f}" if tl is not None else "N/A"
            tm_str = f"${tm:.2f}" if tm is not None else "N/A"
            targets_text = f"""Analyst Price Targets:
- Consensus: {tc_str}
- High: {th_str}
- Low: {tl_str}
- Median: {tm_str}
- Analysts: {recommendation_data['analyst_count']}"""
            
            targets_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Price Targets",
                type="price_targets",
                text=targets_text
            )
            session.add(targets_output)
            
            # Store full consensus API response
            consensus_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="FMP Consensus API Response",
                type="fmp_consensus_response",
                text=json.dumps(consensus_data, indent=2)
            )
            session.add(consensus_output)
            
            # Store upgrade/downgrade data if available
            if upgrade_data:
                upgrade_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="FMP Upgrade/Downgrade Data",
                    type="fmp_upgrade_downgrade",
                    text=json.dumps(upgrade_data, indent=2)
                )
                session.add(upgrade_output)

            # Per-analyst grades + price targets (markdown) so the UI shows WHO rated and WHEN,
            # and the individual targets behind the consensus. Best-effort (None -> omit).
            analyst_details_md = self._format_analyst_details_md(symbol)
            if analyst_details_md:
                session.add(AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="Analyst Grades & Price Targets",
                    type="fmp_analyst_details",
                    text=analyst_details_md,
                ))

            session.commit()
            
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run FMPRating analysis for a symbol and create ExpertRecommendation.
        
        Args:
            symbol: Financial instrument symbol to analyze
            market_analysis: MarketAnalysis instance to update with results
        """
        self.logger.info(f"Starting FMPRating analysis for {symbol} (Analysis ID: {market_analysis.id})")

        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            # Resolve settings as a plain dict (so _process never reads self).
            settings = self._resolve_settings(self._SETTING_KEYS)
            profit_ratio = float(settings['profit_ratio'])
            min_analysts = int(settings['min_analysts'])

            # Gather (consensus + upgrade snapshots + as_of close) then process
            # (pure decision logic). Runs the EXACT same _gather/_process the
            # backtest engine drives via analyze_as_of. The live (as_of=None) path
            # uses the current consensus snapshots; the window setting only affects
            # the backtest reconstruction path, but is threaded for consistency.
            self._gather_symbol = symbol
            self._gather_window_days = int(settings.get("price_target_window_days", 90))
            self._gather_max_analyst_age = int(settings.get("max_analyst_age_months", 0) or 0)
            providers = self._live_providers()
            bundle = self._gather(providers, as_of=None)
            current_price = bundle["current_price"]
            consensus_data = bundle["consensus_data"]
            upgrade_data = bundle["upgrade_data"]

            # current_price is required for the calculation (live-data, no fallback).
            # The no-coverage skip below does not need a price, but a genuine
            # missing-price is still a hard error (preserves the live guard).
            if consensus_data is not None and not current_price:
                raise ValueError(f"Unable to get current price for {symbol}")

            rec = self._process(bundle, settings, as_of=None)

            # Honor SKIP first-class: map _process skip -> the live SKIPPED outcomes
            # (state shape kept byte-identical to the pre-refactor skip blocks).
            if rec.skip:
                if rec.skip_reason == "no consensus data":
                    self.logger.warning(f"Skipping FMPRating analysis for {symbol}: no analyst coverage available from FMP API")
                    market_analysis.state = {
                        'skipped': True,
                        'skip_reason': 'no_analyst_coverage',
                        'skip_message': f"No price target consensus available for {symbol} — stock likely has no analyst coverage on FMP"
                    }
                else:  # "insufficient analysts"
                    analyst_count = self._count_analysts(upgrade_data)
                    self.logger.info(
                        f"Skipping FMPRating analysis for {symbol}: insufficient analyst coverage "
                        f"({analyst_count} analysts, minimum {min_analysts} required)"
                    )
                    market_analysis.state = {
                        'skipped': True,
                        'skip_reason': 'insufficient_analyst_coverage',
                        'skip_message': (
                            f"Insufficient analyst coverage for {symbol}: {analyst_count} analysts found, "
                            f"minimum {min_analysts} required"
                        )
                    }
                market_analysis.status = MarketAnalysisStatus.SKIPPED
                update_instance(market_analysis)
                return

            # The full calculation dict (for the persisted artifacts: the
            # ExpertRecommendation row, the AnalysisOutputs, and the state block —
            # all kept byte-identical to the pre-refactor live behaviour).
            recommendation_data = rec.raw_outputs["calc"]

            # Create ExpertRecommendation record
            recommendation_id = self._create_expert_recommendation(
                recommendation_data, symbol, market_analysis.id, current_price
            )
            
            # Store analysis outputs
            self._store_analysis_outputs(
                market_analysis.id, symbol, recommendation_data, 
                consensus_data, upgrade_data or []
            )
            
            # Store analysis state
            market_analysis.state = {
                'fmp_rating': {
                    'recommendation': {
                        'signal': recommendation_data['signal'].value,
                        'confidence': recommendation_data['confidence'],
                        'expected_profit_percent': recommendation_data['expected_profit_percent'],
                        'details': recommendation_data['details']
                    },
                    'price_targets': {
                        'consensus': recommendation_data['target_consensus'],
                        'high': recommendation_data['target_high'],
                        'low': recommendation_data['target_low'],
                        'median': recommendation_data['target_median'],
                        'analyst_count': recommendation_data['analyst_count']
                    },
                    'analyst_breakdown': {
                        'strong_buy': recommendation_data.get('strong_buy', 0),
                        'buy': recommendation_data.get('buy', 0),
                        'hold': recommendation_data.get('hold', 0),
                        'sell': recommendation_data.get('sell', 0),
                        'strong_sell': recommendation_data.get('strong_sell', 0)
                    },
                    'confidence_breakdown': {
                        # New calculation components (FinnHub methodology + price target boost)
                        'base_confidence': recommendation_data.get('base_confidence', 0),
                        'price_target_boost': recommendation_data.get('price_target_boost', 0),
                        'boost_to_lower': recommendation_data.get('boost_to_lower', 0),
                        'boost_to_consensus': recommendation_data.get('boost_to_consensus', 0),
                        'buy_score': recommendation_data.get('buy_score', 0),
                        'sell_score': recommendation_data.get('sell_score', 0),
                        'hold_score': recommendation_data.get('hold_score', 0),
                    },
                    'consensus_data': consensus_data,
                    'upgrade_data': upgrade_data,
                    'settings': {
                        'profit_ratio': profit_ratio,
                        'min_analysts': min_analysts
                    },
                    'expert_recommendation_id': recommendation_id,
                    'analysis_timestamp': datetime.now(timezone.utc).isoformat(),
                    'current_price': current_price
                }
            }
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
            self.logger.info(f"Completed FMPRating analysis for {symbol}: {recommendation_data['signal'].value} "
                       f"(confidence: {recommendation_data['confidence']:.1f}%, "
                       f"expected profit: {recommendation_data['expected_profit_percent']:.1f}%)")
            
        except Exception as e:
            self.logger.error(f"FMPRating analysis failed for {symbol}: {e}", exc_info=True)
            
            # Update status to failed
            market_analysis.state = {
                'error': str(e),
                'error_timestamp': datetime.now(timezone.utc).isoformat(),
                'analysis_failed': True
            }
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            
            # Create error output
            try:
                session = get_db()
                error_output = AnalysisOutput(
                    market_analysis_id=market_analysis.id,
                    name="Analysis Error",
                    type="error",
                    text=f"FMPRating analysis failed for {symbol}: {str(e)}"
                )
                session.add(error_output)
                session.commit()
                session.close()
            except Exception as db_error:
                self.logger.error(f"Failed to store error output: {db_error}", exc_info=True)
            
            raise
    
    def _render_completed(self, market_analysis: MarketAnalysis) -> None:
        """Render completed analysis with beautiful UI."""
        from nicegui import ui
        
        if not market_analysis.state or 'fmp_rating' not in market_analysis.state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        
        state = market_analysis.state['fmp_rating']
        rec = state.get('recommendation', {})
        targets = state.get('price_targets', {})
        settings = state.get('settings', {})
        current_price = state.get('current_price')
        
        # Main card
        with ui.card().classes('w-full').style('background-color: #1e2a3a'):
            # Header with recommendation
            with ui.card_section().style('background: linear-gradient(135deg, #00d4aa 0%, #00b894 100%)'):
                ui.label('FMP Analyst Price Target Consensus').classes('text-h5 text-weight-bold').style('color: white')
                ui.label(f'{market_analysis.symbol} - Price Target Analysis').style('color: rgba(255,255,255,0.8)')
            
            # Recommendation summary
            signal = rec.get('signal', 'HOLD')
            confidence = rec.get('confidence', 0.0)
            expected_profit = rec.get('expected_profit_percent', 0.0)
            
            # Color based on signal
            if signal == 'BUY':
                signal_color = 'positive'
                signal_icon = 'trending_up'
            elif signal == 'SELL':
                signal_color = 'negative'
                signal_icon = 'trending_down'
            else:
                signal_color = 'grey'
                signal_icon = 'trending_flat'
            
            with ui.card_section():
                with ui.row().classes('w-full items-center justify-between'):
                    with ui.column():
                        ui.label('Recommendation').classes('text-caption').style('color: #a0aec0')
                        with ui.row().classes('items-center gap-2'):
                            ui.icon(signal_icon, color=signal_color, size='2rem')
                            ui.label(signal).classes(f'text-h4 text-{signal_color}')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Confidence').classes('text-caption').style('color: #a0aec0')
                        ui.label(f'{confidence:.1f}%').classes('text-h4').style('color: #e2e8f0')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Expected Profit').classes('text-caption').style('color: #a0aec0')
                        profit_color = 'positive' if expected_profit > 0 else 'negative' if expected_profit < 0 else 'grey'
                        ui.label(f'{expected_profit:+.1f}%').classes(f'text-h4 text-{profit_color}')
                
                if current_price:
                    ui.separator().classes('my-2')
                    ui.label(f'Current Price: ${current_price:.2f}').style('color: #a0aec0')
            
            # Price Targets
            consensus = targets.get('consensus')
            high = targets.get('high')
            low = targets.get('low')
            median = targets.get('median')
            analyst_count = targets.get('analyst_count', 0)
            
            if consensus and high and low:
                with ui.card_section().style('background-color: #141c28'):
                    ui.label(f'Analyst Price Targets ({analyst_count} analysts)').classes('text-subtitle1 text-weight-medium mb-3').style('color: #e2e8f0')
                    
                    with ui.grid(columns=2).classes('w-full gap-4'):
                        # Consensus Target
                        with ui.card().style('background-color: rgba(66, 153, 225, 0.15)'):
                            ui.label('Consensus Target').classes('text-caption').style('color: #a0aec0')
                            ui.label(f'${consensus:.2f}').classes('text-h5').style('color: #63b3ed')
                            if current_price:
                                delta_pct = ((consensus - current_price) / current_price) * 100
                                delta_color = 'positive' if delta_pct > 0 else 'negative'
                                ui.label(f'{delta_pct:+.1f}% from current').classes(f'text-xs text-{delta_color}')
                        
                        # Median Target
                        with ui.card().style('background-color: rgba(160, 174, 192, 0.15)'):
                            ui.label('Median Target').classes('text-caption').style('color: #a0aec0')
                            ui.label(f'${median:.2f}').classes('text-h5').style('color: #a0aec0')
                            if current_price:
                                delta_pct = ((median - current_price) / current_price) * 100
                                delta_color = 'positive' if delta_pct > 0 else 'negative'
                                ui.label(f'{delta_pct:+.1f}% from current').classes(f'text-xs text-{delta_color}')
                        
                        # High Target
                        with ui.card().style('background-color: rgba(0, 212, 170, 0.15)'):
                            ui.label('High Target').classes('text-caption').style('color: #a0aec0')
                            ui.label(f'${high:.2f}').classes('text-h5').style('color: #00d4aa')
                            if current_price:
                                delta_pct = ((high - current_price) / current_price) * 100
                                ui.label(f'{delta_pct:+.1f}% upside').classes('text-xs text-positive')
                        
                        # Low Target
                        with ui.card().style('background-color: rgba(255, 107, 107, 0.15)'):
                            ui.label('Low Target').classes('text-caption').style('color: #a0aec0')
                            ui.label(f'${low:.2f}').classes('text-h5').style('color: #ff6b6b')
                            if current_price:
                                delta_pct = ((low - current_price) / current_price) * 100
                                if delta_pct >= 0:
                                    ui.label(f'{delta_pct:+.1f}% upside').classes('text-xs text-positive')
                                else:
                                    ui.label(f'{delta_pct:+.1f}% downside').classes('text-xs text-negative')
                    
                    # Target range visualization
                    if current_price:
                        ui.separator().classes('my-3')
                        ui.label('Price Range').classes('text-caption mb-2').style('color: #a0aec0')
                        
                        # Calculate positions for visualization
                        # Include current_price in scale to ensure it's always visible
                        scale_min = min(current_price, low)
                        scale_max = max(current_price, high)
                        price_range = scale_max - scale_min
                        current_pos = ((current_price - scale_min) / price_range * 100) if price_range > 0 else 50
                        consensus_pos = ((consensus - scale_min) / price_range * 100) if price_range > 0 else 50
                        
                        with ui.element('div').classes('relative w-full h-12 rounded').style('background-color: #2d3748'):
                            # Low to High gradient background
                            ui.element('div').classes('absolute inset-0 rounded').style('background: linear-gradient(to right, rgba(255,107,107,0.3), rgba(160,174,192,0.3), rgba(0,212,170,0.3))')
                            
                            # Current price marker
                            with ui.element('div').classes('absolute top-0 bottom-0 w-1').style(f'left: {current_pos}%; background-color: #63b3ed'):
                                with ui.element('div').classes('absolute -top-6 left-1/2 transform -translate-x-1/2'):
                                    ui.label('Current').classes('text-xs font-bold').style('color: #63b3ed')
                            
                            # Consensus marker
                            with ui.element('div').classes('absolute top-0 bottom-0 w-1').style(f'left: {consensus_pos}%; background-color: #ffa94d'):
                                with ui.element('div').classes('absolute -bottom-6 left-1/2 transform -translate-x-1/2'):
                                    ui.label('Target').classes('text-xs font-bold').style('color: #ffa94d')
                        
                        with ui.row().classes('w-full justify-between mt-8'):
                            ui.label(f'${scale_min:.2f}').classes('text-xs').style('color: #718096')
                            ui.label(f'${scale_max:.2f}').classes('text-xs').style('color: #718096')
            
            # Analyst Recommendations Breakdown
            analyst_breakdown = state.get('analyst_breakdown', {})
            if analyst_breakdown and analyst_count > 0:
                strong_buy = analyst_breakdown.get('strong_buy', 0)
                buy = analyst_breakdown.get('buy', 0)
                hold = analyst_breakdown.get('hold', 0)
                sell = analyst_breakdown.get('sell', 0)
                strong_sell = analyst_breakdown.get('strong_sell', 0)
                
                with ui.card_section().style('background-color: #141c28'):
                    ui.label('Analyst Recommendations Breakdown').classes('text-subtitle1 text-weight-medium mb-3').style('color: #e2e8f0')
                    
                    # Create a visual bar chart - show all categories
                    with ui.column().classes('w-full gap-2'):
                        # Strong Buy
                        pct = (strong_buy / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Strong Buy').classes('w-24 text-right text-sm').style('color: #a0aec0')
                            ui.label(str(strong_buy)).classes('w-8 text-sm font-bold').style('color: #00d4aa')
                            with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                if pct > 0:
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #00d4aa')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs').style('color: #718096')
                        
                        # Buy
                        pct = (buy / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Buy').classes('w-24 text-right text-sm').style('color: #a0aec0')
                            ui.label(str(buy)).classes('w-8 text-sm font-bold').style('color: #48bb78')
                            with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                if pct > 0:
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #48bb78')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs').style('color: #718096')
                        
                        # Hold
                        pct = (hold / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Hold').classes('w-24 text-right text-sm').style('color: #a0aec0')
                            ui.label(str(hold)).classes('w-8 text-sm font-bold').style('color: #ffa94d')
                            with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                if pct > 0:
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #ffa94d')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs').style('color: #718096')
                        
                        # Sell
                        pct = (sell / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Sell').classes('w-24 text-right text-sm').style('color: #a0aec0')
                            ui.label(str(sell)).classes('w-8 text-sm font-bold').style('color: #ff6b6b')
                            with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                if pct > 0:
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #ff6b6b')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs').style('color: #718096')
                        
                        # Strong Sell
                        pct = (strong_sell / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Strong Sell').classes('w-24 text-right text-sm').style('color: #a0aec0')
                            ui.label(str(strong_sell)).classes('w-8 text-sm font-bold').style('color: #e53e3e')
                            with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                if pct > 0:
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #e53e3e')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs').style('color: #718096')
                    
                    ui.separator().classes('my-2')
                    ui.label(f'Total Analysts: {analyst_count}').classes('text-sm').style('color: #00d4aa')
            
            # Confidence Breakdown
            confidence_breakdown = state.get('confidence_breakdown', {})
            if confidence_breakdown:
                base_confidence = confidence_breakdown.get('base_confidence', 0)
                price_target_boost = confidence_breakdown.get('price_target_boost', 0)
                # applied_boost is the directional boost actually added to confidence
                # (BUY: +raw, SELL: -raw, HOLD: 0). Fall back to raw for old records.
                applied_boost = confidence_breakdown.get('applied_boost', price_target_boost)
                boost_to_lower = confidence_breakdown.get('boost_to_lower', 0)
                boost_to_consensus = confidence_breakdown.get('boost_to_consensus', 0)
                
                with ui.card_section():
                    ui.label('Confidence Score Breakdown (New Methodology)').classes('text-subtitle1 text-weight-medium mb-2').style('color: #e2e8f0')
                    ui.label('FinnHub Analyst Rating Base + Price Target Boost').classes('text-xs mb-3').style('color: #718096')
                    
                    with ui.grid(columns=2).classes('w-full gap-4'):
                        # Base Confidence from Analyst Ratings
                        with ui.card().style('background-color: rgba(66, 153, 225, 0.15)'):
                            ui.label('Base (from Analyst Ratings)').classes('text-caption').style('color: #a0aec0')
                            ui.label(f'{base_confidence:.1f}%').classes('text-h5').style('color: #63b3ed')
                            ui.label(f'Weighted buy/sell/hold scores').classes('text-xs').style('color: #718096')
                        
                        # Price Target Boost (directional — actually applied to confidence)
                        boost_color = '#00d4aa' if applied_boost > 0 else '#ff6b6b' if applied_boost < 0 else '#a0aec0'
                        boost_bg = 'rgba(0, 212, 170, 0.15)' if applied_boost > 0 else 'rgba(255, 107, 107, 0.15)' if applied_boost < 0 else 'rgba(160, 174, 192, 0.15)'
                        with ui.card().style(f'background-color: {boost_bg}'):
                            ui.label('Price Target Boost (applied)').classes('text-caption').style('color: #a0aec0')
                            ui.label(f'{applied_boost:+.1f}%').classes('text-h5').style(f'color: {boost_color}')
                            ui.label(f'Direction-adjusted avg of targets').classes('text-xs').style('color: #718096')
                        
                        # Boost to Lower Target
                        with ui.card().style('background-color: rgba(160, 174, 192, 0.15)'):
                            ui.label('Boost to Lower Target').classes('text-caption').style('color: #a0aec0')
                            ui.label(f'{boost_to_lower:+.1f}%').classes('text-h6').style('color: #a0aec0')
                        
                        # Boost to Consensus Target
                        with ui.card().style('background-color: rgba(160, 174, 192, 0.15)'):
                            ui.label('Boost to Consensus Target').classes('text-caption').style('color: #a0aec0')
                            ui.label(f'{boost_to_consensus:+.1f}%').classes('text-h6').style('color: #a0aec0')
                    
                    ui.separator().classes('my-2')
                    
                    # Calculate what confidence should be (directional boost)
                    calculated_confidence = base_confidence + applied_boost
                    clamped_confidence = max(0.0, min(100.0, calculated_confidence))

                    # Show the formula
                    ui.label(f'Final Confidence = Base + Directional Boost = {base_confidence:.1f}% + {applied_boost:+.1f}% = {calculated_confidence:.1f}%').classes('text-sm').style('color: #a0aec0')
                    
                    # If clamping occurred, show it
                    if calculated_confidence != clamped_confidence:
                        ui.label(f'Clamped to valid range [0-100%]: {clamped_confidence:.1f}%').classes('text-sm font-medium').style('color: #ffa94d')
                    
                    # If stored confidence doesn't match what we calculated, show warning
                    if abs(confidence - clamped_confidence) > 0.1:
                        ui.label(f'⚠️ Stored confidence ({confidence:.1f}%) differs from calculated ({clamped_confidence:.1f}%)').classes('text-sm font-bold').style('color: #ff6b6b')
            
            # Settings
            profit_ratio = float(settings.get('profit_ratio', 1.0))
            min_analysts = int(settings.get('min_analysts', 3))
            
            with ui.card_section():
                ui.label('Analysis Settings').classes('text-subtitle1 text-weight-medium mb-2').style('color: #e2e8f0')
                
                with ui.row().classes('gap-4'):
                    ui.label(f'Profit Ratio: {profit_ratio}x').classes('text-sm').style('color: #a0aec0')
                    ui.label(f'Min Analysts: {min_analysts}').classes('text-sm').style('color: #a0aec0')
            
            # Methodology
            with ui.expansion('Calculation Methodology', icon='info').classes('w-full').style('color: #e2e8f0'):
                with ui.card_section().style('background-color: #141c28'):
                    confidence_breakdown = state.get('confidence_breakdown', {})
                    base_conf = confidence_breakdown.get('base_confidence', 0)
                    price_boost = confidence_breakdown.get('price_target_boost', 0)
                    applied_boost = confidence_breakdown.get('applied_boost', price_boost)
                    boost_lower = confidence_breakdown.get('boost_to_lower', 0)
                    boost_consensus = confidence_breakdown.get('boost_to_consensus', 0)
                    buy_score = confidence_breakdown.get('buy_score', 0)
                    sell_score = confidence_breakdown.get('sell_score', 0)
                    hold_score = confidence_breakdown.get('hold_score', 0)
                    
                    analyst_breakdown = state.get('analyst_breakdown', {})
                    strong_buy = analyst_breakdown.get('strong_buy', 0)
                    buy = analyst_breakdown.get('buy', 0)
                    hold = analyst_breakdown.get('hold', 0)
                    sell = analyst_breakdown.get('sell', 0)
                    strong_sell = analyst_breakdown.get('strong_sell', 0)
                    
                    ui.markdown(f'''
**Signal Determination:**

The recommendation signal is based on the dominant analyst score:
- **BUY**: Buy score (Strong Buy × 2 + Buy) is highest
- **SELL**: Sell score (Strong Sell × 2 + Sell) is highest  
- **HOLD**: Hold score is highest or scores are tied

All profit calculations use the **Consensus Target** price.

---

**NEW Confidence Score Calculation (FinnHub Methodology + Price Target Boost):**

1. **Weighted Analyst Scores**
   - Buy Score = (Strong Buy × 2) + Buy = ({strong_buy} × 2) + {buy} = {buy_score:.1f}
   - Hold Score = Hold = {hold}
   - Sell Score = (Strong Sell × 2) + Sell = ({strong_sell} × 2) + {sell} = {sell_score:.1f}
   - Total Weighted = {buy_score + hold_score + sell_score:.1f}

2. **Base Confidence from Analyst Ratings**
   - Base Confidence = Dominant Score / Total × 100
   - Current: {base_conf:.1f}%
   - **Logic**: Analyst consensus strength drives base confidence

3. **Price Target Boost**
   - Boost to Lower Target = ((Low Target - Current) / Current) × 100 = {boost_lower:+.1f}%
   - Boost to Consensus = ((Consensus - Current) / Current) × 100 = {boost_consensus:+.1f}%
   - Avg Price Target Boost (raw) = ({boost_lower:+.1f}% + {boost_consensus:+.1f}%) / 2 = {price_boost:+.1f}%
   - Directional Boost (applied) = {applied_boost:+.1f}%
   - **Logic**: Raw boost is positive when targets are above current price. The
     sign is then oriented by signal — BUY adds upside, SELL adds downside
     (more downside ⇒ higher SELL confidence), HOLD adds none.

4. **Final Confidence (Clamped to 0-100%)**
   - Final = Base + Directional Boost = {base_conf:.1f}% + {applied_boost:+.1f}% = **{confidence:.1f}%**

---

**Expected Profit Calculation:**

For **BUY** signals:
1. **Price Delta** = Consensus Target - Current Price
2. **Weighted Delta** = Price Delta × (Confidence / 100) × Profit Ratio
3. **Expected Profit %** = (Weighted Delta / Current Price) × 100

For **SELL** signals:
1. **Price Delta** = Current Price - Consensus Target
2. **Weighted Delta** = Price Delta × (Confidence / 100) × Profit Ratio
3. **Expected Profit %** = (Weighted Delta / Current Price) × 100

**Profit Ratio Setting**: Adjusts expected profit based on risk tolerance (current: {profit_ratio}x)

---

**Analyst Recommendations:**

The breakdown shows how many analysts rate the stock as:
- **Strong Buy / Buy**: Bullish ratings (expecting price increase) - weighted 2x for strong ratings
- **Hold**: Neutral rating (price expected to stay stable)
- **Sell / Strong Sell**: Bearish ratings (expecting price decrease) - weighted 2x for strong ratings

This distribution helps validate the consensus target and signal strength.

**Signal Logic:**
- **BUY**: Consensus > Current Price + 5%
- **SELL**: Consensus < Current Price - 5%
- **HOLD**: Otherwise

**Profit Ratio**: Multiplier setting (default 1.0) to adjust conservative/aggressive positioning
                    ''').classes('text-sm')
