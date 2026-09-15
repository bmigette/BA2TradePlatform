"""Per-expert replay-dependency adapters (spec step 4, section 5 table).

One adapter per expert class the live capture records. Each answers, for ONE
configuration, exactly what that configuration reads -- the same branches its
``_gather`` takes, read from the same settings.

**Why the adapters live beside the experts and not beside the warm service.** The
declaration has to track the fetch. ``prewarm_fetchers`` already learned this the
expensive way and calls the SAME data-layer function the expert calls, so the
warmed surface cannot drift from the read surface; an adapter does the declarative
half of that and therefore belongs in the same package, where a change to a
``_gather`` branch is one file away from the declaration it invalidates. Constants
that both sides need (``MACRO_SERIES_IDS``, ``OHLCV_LOOKBACK_DAYS``, the estimator
namespaces) are IMPORTED from the fetchers rather than repeated here.

**Per-INSTANCE, not union, semantics.** This is the opposite of
``prewarm_fetchers``' rule and the difference matters. A GA prewarm cannot steer on
one instance's settings (every trial has different genes), so it warms the union.
A replay answers for ONE recorded analysis with ONE recorded settings dict, and a
declared-but-unused optional dependency is explicitly NOT a proven live input
(spec section 5) -- so ``w_analyst=0`` here means the analyst history is not
declared at all.

Experts without an adapter (FactorRanker, the Senate pair, FinnHubRating, the
ETF/basket and penny strategies) resolve to ``unsupported`` through
``required_replay_inputs``; they join later through adapters of their own and are
never silently covered by this scope.
"""
from __future__ import annotations

from typing import Any, List, Mapping, Sequence

from ba2_common.core.replay.dependencies import (
    FMP_PROVIDER,
    FRED_PROVIDER,
    KIND_HISTORY,
    KIND_SERIES,
    KIND_TIMESERIES,
    OHLCV_PROVIDER,
    Requirement,
    Window,
    as_bool,
    register_adapter,
    require_setting,
)

from ba2_experts.DeterministicScorer import data as ds_data

#: The interval every expert's price read asks for. Intraday bars belong to the
#: execution comparison only (spec section 6).
DAILY = "1d"

#: The two namespaces ``analyst_target_model.fetch_estimator_inputs`` reads when an
#: expert runs in ``expected_profit_mode='model'`` / ``use_model_target``. Spelled
#: exactly as the fetcher spells them -- "quarterly" is the cache NAMESPACE of the
#: estimates endpoint, which always returns annual rows (see
#: ``FMPCompanyDetailsProvider.get_earnings_estimates``).
ESTIMATOR_EARNINGS_NAMESPACE = "past_earnings_quarterly"
ESTIMATOR_ESTIMATES_NAMESPACE = "earnings_estimates_quarterly"

#: The three annual statement namespaces ``DeterministicScorer.data.fetch_statements``
#: reads (``FMPCompanyDetailsProvider`` keys them ``<statement>_<frequency>``).
STATEMENT_NAMESPACES = (
    "income_statement_annual",
    "balance_sheet_annual",
    "cashflow_statement_annual",
)

#: The estimates payload cannot be replayed as a vintage: the provider filters
#: FISCAL PERIODS, not historical revisions, so a warm performed today returns
#: today's revision of an estimate live consumed months ago. Carried on every
#: estimates requirement so the plan and the historical report can say so instead
#: of comparing the two as if they were the same observation (spec section 5).
ESTIMATES_PROVENANCE_NOTE = (
    "the analyst-estimates endpoint filters fiscal periods, NOT historical revisions: a "
    "warmed payload is today's revision and cannot reconstruct the vintage live consumed"
)

#: The expert classes this module adapts, in the order the spec table lists them.
ADAPTED_EXPERTS = ("FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy",
                   "DeterministicScorer")


# --------------------------------------------------------------------------- #
# Shared pieces
# --------------------------------------------------------------------------- #
def _history(namespace: str, symbol: str, window: Window, reason: str, *,
             optional: bool = False) -> Requirement:
    """A per-symbol ``fmp_history`` payload."""
    return Requirement(provider=FMP_PROVIDER, namespace=namespace, symbol=symbol,
                       window=window, interval=None, kind=KIND_HISTORY, optional=optional,
                       reason=reason)


def _price_series(symbol: str, window: Window, reason: str) -> Requirement:
    """The daily parquet series an expert's OHLCV / price-at-date read uses."""
    return Requirement(provider=OHLCV_PROVIDER, namespace="ohlcv", symbol=symbol,
                       window=window, interval=DAILY, kind=KIND_TIMESERIES, optional=False,
                       reason=reason)


def _estimator_inputs(symbol: str, window: Window, mode_reason: str) -> List[Requirement]:
    """``fetch_estimator_inputs``' two namespaces, with the revision caveat attached."""
    return [
        _history(ESTIMATOR_EARNINGS_NAMESPACE, symbol, window,
                 f"{mode_reason}: the price-target model reads trailing quarterly earnings"),
        _history(ESTIMATOR_ESTIMATES_NAMESPACE, symbol, window,
                 f"{mode_reason}: the price-target model reads forward EPS estimates -- "
                 f"{ESTIMATES_PROVENANCE_NOTE}"),
    ]


def _model_mode(settings: Mapping[str, Any], key: str, expert: str) -> bool:
    """Whether this configuration runs the fundamentals price-target model.

    ``FMPEarningsDrift`` / ``FMPInsiderClusterBuy`` spell it
    ``expected_profit_mode='model'``; ``DeterministicScorer`` spells it
    ``use_model_target``. Both are read explicitly.
    """
    value = require_setting(settings, key, needed_by=f"{expert}'s price-target model inputs")
    if key == "use_model_target":
        return as_bool(value)
    return str(value) == "model"


# --------------------------------------------------------------------------- #
# FMPRating
# --------------------------------------------------------------------------- #
def fmp_rating_requirements(settings: Mapping[str, Any], universe: Sequence[str],
                            window: Window) -> List[Requirement]:
    """Dated price targets and grades; the individual grades only when recency is on.

    ``_gather`` fetches the DATED individual grades (``analyst_grades``) only while
    ``max_analyst_age_months > 0`` -- the whole point of that gate is that a run
    which leaves the recency filter off needs no extra fetch, so declaring it
    unconditionally would report coverage for an input live never read.

    Both branches (the live consensus snapshot and the as_of reconstruction) end up
    consuming the same two dated histories: the snapshot path reads the
    price-target history to count the targets behind FMP's windowed consensus, and
    the reconstruction path builds the consensus from it.
    """
    out: List[Requirement] = []
    max_age = int(require_setting(settings, "max_analyst_age_months",
                                  needed_by="FMPRating's dated individual analyst grades"))
    for symbol in universe:
        out.append(_history("price_target", symbol, window,
                            "FMPRating reads the dated price-target history (consensus "
                            "reconstruction live and as_of, plus the target count)"))
        out.append(_history("grades_historical", symbol, window,
                            "FMPRating reads the dated upgrade/downgrade grade history"))
        if max_age > 0:
            out.append(_history("analyst_grades", symbol, window,
                                f"max_analyst_age_months={max_age}: the rating-recency filter "
                                f"reads the DATED individual analyst grades"))
        out.append(_price_series(symbol, window,
                                 "FMPRating reads the as_of close through the OHLCV provider"))
    return out


# --------------------------------------------------------------------------- #
# FMPEarningsDrift
# --------------------------------------------------------------------------- #
def earnings_drift_requirements(settings: Mapping[str, Any], universe: Sequence[str],
                                window: Window) -> List[Requirement]:
    """The quarterly earnings calendar, plus the model's inputs in model mode.

    The live calendar SHORTCUT (``_fetch_earnings_calendar_by_symbol``) is a
    market-wide date-ranged endpoint, not a per-symbol history, and it is not
    cached as a warmable artifact -- the historical path always reads the
    per-symbol ``past_earnings_quarterly`` payload declared here, which is what a
    historical comparison has to reconstruct from.
    """
    out: List[Requirement] = []
    model = _model_mode(settings, "expected_profit_mode", "FMPEarningsDrift")
    for symbol in universe:
        out.append(_history("past_earnings_quarterly", symbol, window,
                            "FMPEarningsDrift reads the reported-vs-estimated quarterly "
                            "earnings rows"))
        if model:
            out.extend(_estimator_inputs(symbol, window, "expected_profit_mode='model'"))
        out.append(_price_series(symbol, window,
                                 "FMPEarningsDrift reads the as_of close through the OHLCV "
                                 "provider (live reads the broker quote)"))
    return out


# --------------------------------------------------------------------------- #
# FMPInsiderClusterBuy
# --------------------------------------------------------------------------- #
def insider_requirements(settings: Mapping[str, Any], universe: Sequence[str],
                         window: Window) -> List[Requirement]:
    """Insider transactions over the configured lookback, plus the model's inputs."""
    out: List[Requirement] = []
    lookback_days = int(require_setting(settings, "lookback_days",
                                        needed_by="FMPInsiderClusterBuy's insider history"))
    model = _model_mode(settings, "expected_profit_mode", "FMPInsiderClusterBuy")
    insider_window = window.trailing(lookback_days)
    for symbol in universe:
        out.append(_history("insider_v2", symbol, insider_window,
                            f"lookback_days={lookback_days}: the cluster-buy count reads the "
                            f"insider transactions filed in that window"))
        if model:
            out.extend(_estimator_inputs(symbol, window, "expected_profit_mode='model'"))
        out.append(_price_series(symbol, window,
                                 "FMPInsiderClusterBuy reads the as_of close through the OHLCV "
                                 "provider (live reads the broker quote)"))
    return out


# --------------------------------------------------------------------------- #
# DeterministicScorer
# --------------------------------------------------------------------------- #
def deterministic_scorer_requirements(settings: Mapping[str, Any], universe: Sequence[str],
                                      window: Window) -> List[Requirement]:
    """Long daily history + benchmark, annual statements, macro, and the weighted sections.

    ``fetch_macro_series`` runs on EVERY analysis regardless of the macro weight
    (the regime composite consumes it through the technical section too), so the
    four FRED series are declared unconditionally -- spec section 5: "Account for
    currently executed macro reads even when their score weight is off."

    The analyst and earnings sections ARE weight-gated in ``_gather``: at
    ``w_analyst = 0`` it never touches the grade/target histories, and at
    ``w_earnings = 0`` never the earnings rows.
    """
    out: List[Requirement] = []
    w_analyst = float(require_setting(settings, "w_analyst",
                                      needed_by="DeterministicScorer's analyst section inputs"))
    w_earnings = float(require_setting(settings, "w_earnings",
                                       needed_by="DeterministicScorer's earnings section inputs"))
    index_symbol = str(require_setting(settings, "index_symbol",
                                       needed_by="DeterministicScorer's benchmark price series"))
    model = _model_mode(settings, "use_model_target", "DeterministicScorer")
    price_window = window.trailing(ds_data.OHLCV_LOOKBACK_DAYS)

    for symbol in universe:
        out.append(_price_series(
            symbol, price_window,
            f"DeterministicScorer reads {ds_data.OHLCV_LOOKBACK_DAYS}d of daily bars "
            f"(252d momentum + 200d SMA + indicator buffers)"))
        for namespace in STATEMENT_NAMESPACES:
            out.append(_history(namespace, symbol, window,
                                "DeterministicScorer's FUNDAMENTAL section reads the "
                                "point-in-time annual statements"))
        if w_analyst > 0:
            out.append(_history("grades_historical", symbol, window,
                                f"w_analyst={w_analyst:g}: the ANALYST section reads the dated "
                                f"grade history"))
            out.append(_history("price_target", symbol, window,
                                f"w_analyst={w_analyst:g}: the ANALYST section reads the dated "
                                f"individual price targets"))
        if w_earnings > 0:
            out.append(_history("past_earnings_quarterly", symbol, window,
                                f"w_earnings={w_earnings:g}: the EARNINGS/PEAD section reads the "
                                f"quarterly earnings rows"))
        if model:
            out.extend(_estimator_inputs(symbol, window, "use_model_target=true"))

    out.append(_price_series(
        index_symbol, price_window,
        f"index_symbol={index_symbol}: the macro trend input reads the benchmark's closes"))
    for series_id in ds_data.MACRO_SERIES_IDS:
        out.append(Requirement(
            provider=FRED_PROVIDER, namespace=series_id, symbol=None, window=window,
            interval=None, kind=KIND_SERIES, optional=False,
            reason=("fetch_macro_series reads this FRED series on EVERY analysis, whatever the "
                    "macro weight is set to"),
        ))
    return out


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
_ADAPTERS = {
    "FMPRating": fmp_rating_requirements,
    "FMPEarningsDrift": earnings_drift_requirements,
    "FMPInsiderClusterBuy": insider_requirements,
    "DeterministicScorer": deterministic_scorer_requirements,
}


def register_all() -> None:
    """Install every adapter in this module. Idempotent; called on import.

    Importing this module is what makes ``required_replay_inputs`` able to answer
    for these experts at all -- ``ba2_common`` may not import ``ba2_experts``, so
    the host, the backend warm service and the CLI each import it explicitly.
    """
    for expert_class, adapter in _ADAPTERS.items():
        register_adapter(expert_class, adapter)


register_all()

__all__ = [
    "ADAPTED_EXPERTS",
    "DAILY",
    "ESTIMATES_PROVENANCE_NOTE",
    "ESTIMATOR_EARNINGS_NAMESPACE",
    "ESTIMATOR_ESTIMATES_NAMESPACE",
    "STATEMENT_NAMESPACES",
    "deterministic_scorer_requirements",
    "earnings_drift_requirements",
    "fmp_rating_requirements",
    "insider_requirements",
    "register_all",
]
