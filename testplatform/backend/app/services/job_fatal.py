"""The ONE classification of errors that end a whole optimization JOB (not one trial)."""
from typing import Any

#: ERRORS THAT END THE WHOLE JOB, not one trial. Each one means the DATA, the CONFIG or the CODE is wrong
#: for every genome (or that a look-ahead / basis guard was violated), so scoring the trial at the sentinel
#: and carrying on would finish the search on nothing, or on a winner shaped by the defect. Matched by NAME
#: (the exception object does not cross a process boundary, and this module must not import every
#: package just to classify). ONE set: the worker flags a failed trial ``fatal`` from it and the master
#: aborts the job (``_FatalTrialError`` -> ``_fail``) on the first such trial.
#:   AnalysisFailureRefusal -- more than 5% of an expert's analysis passes raised (owner decision
#:                             2026-10-07: "it should fail the job for analysis");
#:   StaleAnchorPrice       -- the intraday anchor-price (look-ahead) guard was violated;
#:   ScreenerUniverseRefusal -- the screener gate selected symbols outside the job's static universe
#:                             (the superset derivation is wrong: every genome's result omits tradable picks);
#:   StaleMarkToMarket / ComboSettlementRefused -- engine-level refusals that already end the run.
JOB_FATAL_ERROR_TYPES = frozenset({
    "BacktestCacheMiss", "FMPHistoryCacheMiss", "FMPHermeticViolation",
    "SharedArrayFdExhausted", "SplitBasisRefused", "OptionSpotBasisMismatch",
    "SpreadModelConfigError", "OptionTradeRecordsFlagMissing",
    "MarketCalendarUnavailable", "NotARegularSession", "RiskFreeRateUnavailable",
    "MacroAvailabilityUnknown",
    "AnalysisFailureRefusal", "StaleAnchorPrice", "StaleMarkToMarket", "ComboSettlementRefused",
    "ScreenerUniverseRefusal",
})


def job_fatal(exc_or_name: Any) -> bool:
    """True when this error (an exception, or its type name as carried by a worker result) ends the JOB."""
    name = exc_or_name if isinstance(exc_or_name, str) else type(exc_or_name).__name__
    return name in JOB_FATAL_ERROR_TYPES
