"""The ONE prewarm implementation: the per-expert fetcher table AND the run that drives it.

``ba2-test prewarm`` (``ba2test_launcher._cmd_prewarm``) and the API/queue handler
(``app.services.data_build_handler.handle_prewarm``) used to carry two independent copies of
this: the CLI knew seven experts, the handler three, and only the CLI warmed
DeterministicScorer's per-symbol financial histories. The 2026-09-10 live-replay readiness
audit ("Two prewarm tooling gaps") found the two entry points were NOT interchangeable -- a
prewarm driven from the UI reported success having written a fraction of what the CLI writes.
So the fetchers, the freeze gate, the thread pool, the error handling and the counting all live
here; an entry point contributes argument parsing and reporting, nothing else.

Each fetcher warms exactly the ``fmp_history`` namespaces its expert's ``_gather`` can read, by
calling the SAME data-layer function the expert calls -- never a hand-rolled re-implementation
of the fetch, so the warmed surface cannot drift away from the read surface.

UNION SEMANTICS, NOT PER-INSTANCE SEMANTICS. A fetcher warms every namespace its expert MIGHT
read, not the ones one particular configuration will read. The GA varies the settings that
decide (``expected_profit_mode`` is a gene for both FMPEarningsDrift and FMPInsiderClusterBuy;
``factor_weight_*`` for FactorRanker), so a grid prewarm cannot steer on any instance's
settings: every trial in a run would need a different warm. Warming the union costs two extra
per-symbol fetches; warming less costs a hermetic trial an ``FMPHistoryCacheMiss`` mid-run.

THE FREEZE GATE IS THREAD-LOCAL, WHICH IS WHY ``run_prewarm`` OWNS IT. ``fmp_history_disk_cached``
only writes to disk while ``ba2_providers.fmp_common``'s thread-local ttl-freeze flag is set
(live is a straight passthrough). Entering ``frozen_ttl_cache()`` on the submitting thread alone
leaves every pool worker un-frozen: the fetches go out over the network and NOTHING is written,
while the run reports success. ``initializer=set_ttl_frozen`` sets the flag from inside each
worker thread. ``persist_empty_sentinel()`` (a module global, so it does reach the workers) makes
a genuinely-empty history read back as "checked, no data" instead of an eternal prewarm gap.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

#: The experts prewarm can warm, in table order (CLI help / API validation messages).
EXPERT_NAMES = tuple(FETCHER_METHODS)

#: Experts whose warming needs the congressional-disclosure feeds (the per-symbol fetcher plus
#: the one-shot unscoped "latest disclosures" warm, ``do_senate_latest``).
SENATE_EXPERTS = ("FMPSenateTraderWeight", "FMPSenateTraderCopy")

#: How far back ``do_insider`` warms insider transactions. Union semantics: the widest
#: window any GA trial's ``lookback_days`` gene can ask for, so no trial hits a gap. (A
#: REPLAY warm asks for the recorded instance's own lookback instead -- it answers for one
#: configuration, not for a search space.)
INSIDER_PREWARM_LOOKBACK_DAYS = 400

#: The GENTLEST scalper-filter setting any GA trial for FMPSenateTraderWeight will ever use.
#: A trader excluded by EVEN this setting is excluded by every stricter one too, so no trial can
#: reach their trades and ``warm_new_traders`` skips warming their (often thousands of)
#: buy-symbol price histories.
#:
#: ONE SOURCE, read by both the prewarm and the GA grid: ``ba2test_launcher._EXPERT_OPT``
#: imports these values for the ``min_trader_avg_hold_days`` gene floor and the fixed
#: ``min_trader_hold_roundtrips``. They used to be spelled out in the grid and read back out of
#: it by the prewarm, which meant only the CLI could see them, and a grid edit could silently
#: desync the skip from what the GA actually searches. ``hold_floor_days`` is DELIBERATELY > 0
#: (never "filter disabled"): at 0 a trial could ask for traders this skip never warmed.
SENATE_SCALPER_BOUNDS: Dict[str, Any] = {"hold_floor_days": 1.0, "hold_min_roundtrips": 3}

#: API keys are quoted verbatim inside FMP/Finnhub HTTP error text. A prewarm log/summary is not
#: a place to leak one.
_SECRET_QS = re.compile(r"((?:apikey|api_key|apiKey|token)=)[^&\s'\"]+")


class PrewarmConfigError(ValueError):
    """A requested expert cannot be warmed with the configuration it was given.

    Distinct from a per-symbol fetch failure on purpose: a data gap for one instrument is
    survivable (that symbol is counted as an error and the run continues), while a
    configuration gap would repeat identically for every remaining symbol. So this type is
    named by ``run_prewarm``'s per-symbol handler and re-raised, and both entry points report it
    as a failed run.
    """


def redact(text: str) -> str:
    """Strip API-key query values out of a message bound for a log or a task summary."""
    return _SECRET_QS.sub(r"\1<redacted>", text)


def resolve_keys() -> Dict[str, Optional[str]]:
    """The API keys prewarm needs: env first, then the app-settings DB (the order the providers
    and ``ba2-test fetch-cache`` use). ONE resolver for both entry points.

    An absent key comes back ``None`` rather than raising here: FMP is refused by
    ``PrewarmFetchers`` itself, and Finnhub only matters when FinnHubRating is asked for
    (``validate``).
    """
    def _setting(key: str) -> Optional[str]:
        try:
            from ba2_common.config import get_app_setting
            return get_app_setting(key)
        except Exception:  # noqa: BLE001 - no DB / no settings row: the env answer stands
            return None

    return {
        "fmp": os.getenv("FMP_API_KEY") or _setting("FMP_API_KEY"),
        "finnhub": os.getenv("FINNHUB_API_KEY") or _setting("finnhub_api_key"),
    }


def resolve_fred_key() -> Optional[str]:
    """The FRED key: env first, then the app-settings DB (the order FMP/Finnhub use).

    ``None`` when unconfigured -- refused by :func:`prewarm_fred`, which is the only
    caller that needs it, rather than failing a 500-symbol FMP prewarm over a key
    only DeterministicScorer's macro section uses.
    """
    key = os.getenv("FRED_API_KEY")
    if not key:
        try:
            from ba2_common.config import get_app_setting
            key = get_app_setting("fred_api_key")
        except Exception:  # noqa: BLE001 - no DB / no settings row: the env answer stands
            key = None
    return key


def prewarm_fred(max_age_hours: float, *, log: Optional[Callable[[str], None]] = None,
                 warn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Refresh the FRED macro series DeterministicScorer reads.

    Global, not per-symbol: these are economy-wide series, so they are fetched once per
    run rather than once per (expert, symbol) like the FMP history caches.

    This exists because ``fred_series.get_series_as_of`` RAISES on a missing cache file
    rather than reaching for the network -- a backtest must never silently run on absent
    macro data. That contract is only safe if something populates the cache first, and
    this is it. The files land under CACHE_FOLDER, so remote workers receive them with
    the rest of the cache sync automatically.

    LIVES HERE, not in one entry point. It was defined inside
    ``data_build_handler``, so ``ba2-test prewarm --experts DeterministicScorer``
    warmed every per-symbol history and NO macro series at all -- the CLI half of the
    same "two prewarm tooling gaps" finding that moved the fetcher table here. Both
    entry points now call this one function.

    TWO SINKS. ``log`` takes the progress; ``warn`` takes a series that could not be
    refreshed. They were one, and the API caller passed ``logger.info`` -- so a failed
    series was reported BELOW the level the backend logs at, and the only other trace
    was an ``errors`` count inside a summary dict. The next thing to touch that series
    is a hermetic trial that aborts on the missing file with no trace of why.

    ``warn`` defaults to ``log`` when a caller supplied one (the CLI prints both to
    stdout, where both are equally visible) and to ``logger.warning`` otherwise -- never
    to ``logger.info``.
    """
    import time

    from ba2_providers.macro import fred_series

    say = log if log is not None else logger.info
    complain = warn if warn is not None else (log if log is not None else logger.warning)
    key = resolve_fred_key()
    if not key:
        # Not fatal to the whole prewarm: only DeterministicScorer needs it, and saying
        # so precisely beats failing a 500-symbol FMP prewarm over a missing macro key.
        return {"error": "fred_api_key not configured (AppSetting or FRED_API_KEY)"}

    refreshed = skipped = errors = 0
    for sid in fred_series.SERIES_SPEC:
        path = fred_series.cache_path(sid)
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) / 3600.0 < max_age_hours:
            skipped += 1
            continue
        try:
            fred_series.refresh_series(sid, key)
            refreshed += 1
        except Exception as e:  # noqa: BLE001 - one series must not abort the prewarm
            errors += 1
            complain(f"!! prewarm FRED {sid} failed: {redact(str(e))}")
    return {"refreshed": refreshed, "fresh": skipped, "errors": errors}


class PrewarmFetchers:
    """Per-symbol history fetchers for every expert prewarm supports.

    Construction is cheap: providers/experts are built lazily on first use, so an instance
    covering all seven experts costs nothing for the experts a given run does not warm.

    Args are keyword-only and have NO defaults (no-defaults rule): a caller states every input,
    and one it cannot supply is passed explicitly as ``None`` and refused -- by the constructor
    for the FMP key, by ``validate`` for Finnhub -- rather than silently standing in for a real
    value.
    """

    def __init__(self, *,
                 fmp_key: Optional[str],
                 end_date: datetime,
                 finnhub_key: Optional[str],
                 log: Optional[Callable[[str], None]] = None) -> None:
        if not fmp_key:
            raise PrewarmConfigError(
                "FMP_API_KEY not configured (set it in .env or the app-settings DB).")
        self.fmp_key = fmp_key
        self.end_date = end_date
        self.finnhub_key = finnhub_key
        self._log_fn = log if log is not None else logger.info

        # THE shared namespace -> fetch table (ba2_experts.warm_fetchers). It owns the
        # lazily-built, run-shared provider singletons this class used to build itself.
        # Thread-safe enough for the pool below: they hold the API key and nothing else, and
        # every read they do goes through the shared disk cache (stateless, key-only providers).
        from ba2_experts.warm_fetchers import NamespaceFetchers

        self._namespaces = NamespaceFetchers()
        self._finnhub_expert = None

        # Senate dedup state is SHARED across every universe symbol's do_senate call: the same
        # prolific trader is discovered from dozens of symbols, and re-walking their whole
        # history each time is pure wasted CPU (the disk/memory cache already prevented the
        # redundant NETWORK fetch, not the Python-side work of getting there). Lock-guarded
        # because the pool runs the per-symbol fetchers concurrently.
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
        """Resolve everything configuration-dependent BEFORE any fetch runs; return the unknown
        experts.

        Unknown experts are RETURNED (the run reports them as skipped, as both entry points
        always did); a KNOWN expert this instance is not configured to warm RAISES, because that
        one would otherwise fail per symbol, repeatedly, for the whole run.
        """
        unknown = [e for e in experts if e not in FETCHER_METHODS]
        if "FinnHubRating" in experts and not self.finnhub_key:
            raise PrewarmConfigError(
                "finnhub_api_key not configured (set FINNHUB_API_KEY or the app-setting) - "
                "required to warm FinnHubRating.")
        return unknown

    def log(self, message: str) -> None:
        self._log_fn(message)

    # -------------------------------------------------------------- internals
    def _details(self):
        """The shared FMPCompanyDetailsProvider (statements / earnings / estimates).

        Comes from the shared namespace table, which caches it the same way this class
        used to -- one provider per run, built on first use.
        """
        return self._namespaces.details()

    def _insider(self):
        return self._namespaces.insider()

    def _warm(self, namespace: str, sym: str, lookback_days=None) -> None:
        """Warm ONE fmp_history namespace through the shared table.

        THE table (``ba2_experts.warm_fetchers``), not a second copy of the calls. The
        per-expert methods below are now lists of namespace names; the calls themselves
        -- endpoint, depth, keyword spelling -- live beside the experts that read them,
        so the warm service and this prewarm cannot drift apart the way the CLI and the
        API handler once did.
        """
        from ba2_experts.warm_fetchers import NamespaceRequest

        self._namespaces.fetch(NamespaceRequest(
            namespace=namespace, symbol=sym, end_date=self.end_date,
            fmp_key=self.fmp_key, lookback_days=lookback_days))

    def _warm_estimator_inputs(self, sym: str) -> None:
        """Warm ``analyst_target_model.fetch_estimator_inputs``' two namespaces for *sym*.

        The fundamentals-only price-target model (``expected_profit_mode='model'``) reads
        quarterly past earnings + forward EPS estimates through
        ``FMPCompanyDetailsProvider.get_past_earnings`` / ``get_earnings_estimates``
        (``packages/experts/ba2_experts/analyst_target_model.py``, ``fetch_estimator_inputs``)
        -> the ``past_earnings_quarterly`` and ``earnings_estimates_quarterly`` fmp_history
        namespaces. Which two those are is stated ONCE, in
        ``ba2_experts.warm_fetchers.ESTIMATOR_NAMESPACES``, so this prewarm and the replay
        dependency adapter cannot disagree about what "model mode" needs.

        Warmed UNCONDITIONALLY by every expert that can select the model (see the module
        docstring on union semantics): ``expected_profit_mode`` is a GA gene, so which mode a
        given trial runs is not knowable at prewarm time.
        """
        from ba2_experts.warm_fetchers import ESTIMATOR_NAMESPACES

        for namespace in ESTIMATOR_NAMESPACES:
            self._warm(namespace, sym)

    # --------------------------------------------------------------- fetchers
    def do_fmprating(self, sym: str) -> None:
        """Consensus reconstruction inputs + the dated individual grades (rating-recency)."""
        for namespace in ("grades_historical", "price_target", "analyst_grades"):
            self._warm(namespace, sym)

    def do_earnings_drift(self, sym: str) -> None:
        """Quarterly earnings history, plus the price-target model's inputs: with
        ``expected_profit_mode='model'`` this expert's ``_gather`` also calls
        ``fetch_estimator_inputs``, and that mode is a GA gene."""
        self._warm("past_earnings_quarterly", sym)
        self._warm_estimator_inputs(sym)

    def do_insider(self, sym: str) -> None:
        """Insider transactions, plus the price-target model's inputs.

        ``FMPInsiderClusterBuy._gather`` fetches ``estimator_inputs`` when
        ``expected_profit_mode == 'model'``. That is a GA gene here too, and the deployed live
        instance runs the model mode -- the 2026-09-10 readiness audit found it replaying
        against two namespaces prewarm never wrote.
        """
        self._warm("insider_v2", sym, lookback_days=INSIDER_PREWARM_LOOKBACK_DAYS)
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
        for namespace in ("income_statement_annual", "balance_sheet_annual",
                          "cashflow_statement_annual", "grades_historical",
                          "past_earnings_quarterly", "price_target"):
            self._warm(namespace, sym)

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
            # (SENATE_SCALPER_BOUNDS) contributes to NO GA trial's signal, so their (potentially
            # thousands of) buy-symbols are dead weight - skip their price-history warm.
            hold_info = s._calculate_trader_avg_hold_days(history)
            if (hold_info["avg_hold_days"] is not None
                    and hold_info["roundtrips"] >= SENATE_SCALPER_BOUNDS["hold_min_roundtrips"]
                    and hold_info["avg_hold_days"] < SENATE_SCALPER_BOUNDS["hold_floor_days"]):
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
        ``do_senate`` warms, and independent of any universe symbol, so it runs ONCE per run
        (including for a run that has no per-symbol work at all, e.g. FMPSenateTraderCopy alone).

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


def run_prewarm(fetchers: PrewarmFetchers, experts: List[str], symbols: List[str], workers: int,
                *, end: datetime) -> Dict[str, Any]:
    """Warm *experts* x *symbols*, and return what was written. THE prewarm run.

    Owns the freeze gate, the worker-thread initializer, the empty-result sentinel, the
    per-symbol error handling and the one-shot senate warm, so neither entry point can get any
    of them subtly wrong (the API handler did: see the module docstring).

    Raises ``PrewarmConfigError`` -- from ``validate`` up front, or out of a worker -- for a
    configuration gap the run cannot survive. A per-SYMBOL failure is counted instead: one
    instrument's data gap must not abort a 500-symbol warm.
    """
    from ba2_providers.fmp_common import (
        frozen_ttl_cache, persist_empty_sentinel, set_ttl_frozen,
    )

    t0 = time.time()
    skipped = fetchers.validate(experts)
    for name in skipped:
        fetchers.log(f">> skipping unknown expert '{name}' (no disk-cached history fetcher)")

    table = fetchers.table
    work = []  # list of (expert, symbol, fetch_callable)
    for expert in experts:
        if expert in skipped:
            continue
        for sym in symbols:
            work.append((expert, sym, table[expert]))

    counts: Dict[str, int] = {}
    failures: List[str] = []
    errors = 0
    senate_latest = False

    with frozen_ttl_cache(), persist_empty_sentinel():
        # BEFORE the per-symbol work, so a run whose experts have no per-symbol fetcher at all
        # (``--experts FMPSenateTraderCopy``) still warms the unscoped feed it needs.
        if any(e in experts for e in SENATE_EXPERTS):
            fetchers.do_senate_latest()
            senate_latest = True

        if work:
            with ThreadPoolExecutor(max_workers=max(1, workers),
                                    initializer=set_ttl_frozen, initargs=(True,)) as ex:
                futures = {ex.submit(fn, sym): (expert, sym) for (expert, sym, fn) in work}
                for fut in as_completed(futures):
                    expert, sym = futures[fut]
                    try:
                        fut.result()
                        counts[expert] = counts.get(expert, 0) + 1
                    except PrewarmConfigError:
                        # Not a per-symbol data gap: it would repeat for every remaining symbol.
                        raise
                    except Exception as e:  # noqa: BLE001 — one bad symbol must not abort
                        errors += 1
                        # Type + redacted text: an FMP/Finnhub error quotes the failing URL,
                        # api key and all.
                        detail = f"{type(e).__name__}: {redact(str(e))}"
                        failures.append(f"{expert}/{sym}: {detail}")
                        fetchers.log(f"!! prewarm {expert}/{sym} failed: {detail}")

    notes = []
    if not work:
        notes.append("no per-symbol disk-cached experts to pre-warm")
    # The trader-SKILL score prewarm is GA-grid-specific and driven by the CLI's --start; say so
    # rather than let a caller read its absence as "done".
    notes.append("senate trader-skill scores not prewarmed (ba2-test prewarm --start only)")

    return {
        "cached": counts,
        "errors": errors,
        "failures": failures,
        "skipped": skipped,
        "symbols": len(symbols),
        "senate_latest": senate_latest,
        "senate_skill_scores": False,
        "notes": notes,
        "end": end.isoformat(),
        "elapsed_seconds": round(time.time() - t0, 1),
    }
