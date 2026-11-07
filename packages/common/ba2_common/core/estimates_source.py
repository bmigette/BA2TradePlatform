"""Which provider answers the ESTIMATE methods of ``fundamentals_details()``.

Mirrors ``ohlcv_history`` (the per-run OHLCV history source):

* LIVE reads the app setting ``data_provider_estimates`` (``fmp`` | ``alphavantage``, declared default
  ``fmp``). The setting lives with the host (``ba2_trade_platform.core.app_settings``); ba2_common reads it
  through the resolver seam below, wired at startup. Unwired (packages alone, tests) means ``fmp``.
* A BACKTEST names its source explicitly on the run config (``estimates_source``). There is NO default: a
  run that does not say where its estimates come from refuses (``EstimatesSourceMissing``), because the
  two sources differ in whether they can be point-in-time at all.

The pattern ``data_provider_<kind>`` is reserved for later kinds (earnings, statements, insider); only
``estimates`` is implemented.
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional, Tuple

#: The values of the setting / the run-config key / the ``--estimates-source`` flag.
ESTIMATES_SOURCES: Tuple[str, ...] = ("fmp", "alphavantage")

#: The run-config key (like ``OHLCV_HISTORY_SOURCE_KEY``).
ESTIMATES_SOURCE_KEY = "estimates_source"

#: The AppSetting key of the LIVE choice.
ESTIMATES_SETTING_KEY = "data_provider_estimates"

#: What live uses when no host resolver is wired.
UNWIRED_LIVE_ESTIMATES_SOURCE = "fmp"


class UnknownEstimatesSource(ValueError):
    """An estimates source name that is not one of ``ESTIMATES_SOURCES``."""


class EstimatesSourceMissing(ValueError):
    """A backtest run config without ``estimates_source``. Never defaulted."""


def check_estimates_source(value) -> str:
    if value not in ESTIMATES_SOURCES:
        raise UnknownEstimatesSource(
            f"unknown estimates source {value!r}; expected one of {', '.join(ESTIMATES_SOURCES)}")
    return value


def resolve_job_estimates_source(job_config: Mapping) -> str:
    """The estimates source a backtest run is stamped with. Absent -> ``EstimatesSourceMissing``."""
    if ESTIMATES_SOURCE_KEY not in job_config or job_config[ESTIMATES_SOURCE_KEY] in (None, ""):
        raise EstimatesSourceMissing(
            f"the run config has no {ESTIMATES_SOURCE_KEY!r}; a backtest must name its estimates source "
            f"({', '.join(ESTIMATES_SOURCES)}) -- there is no default")
    return check_estimates_source(job_config[ESTIMATES_SOURCE_KEY])


def with_estimates_source_name(name: str, source: str) -> str:
    """``name`` carrying the estimates source (a job name is the job's identity; two sources under one name
    would skip-as-done or compare unlike runs). Idempotent."""
    check_estimates_source(source)
    tag = f"-est{source}"
    return name if name.endswith(tag) else f"{name}{tag}"


_live_resolver: Optional[Callable[[], str]] = None


def set_live_estimates_source_resolver(fn: Optional[Callable[[], str]]) -> None:
    global _live_resolver
    _live_resolver = fn


def live_estimates_source() -> str:
    """The LIVE estimates source (the app setting through the host resolver; ``fmp`` when unwired)."""
    if _live_resolver is None:
        return UNWIRED_LIVE_ESTIMATES_SOURCE
    return check_estimates_source(_live_resolver())
