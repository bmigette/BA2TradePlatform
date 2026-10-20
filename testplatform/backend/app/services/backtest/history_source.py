"""Which OHLCV history a backtest of THIS platform reads (``fmp`` | ``alpaca``), at the host.

The pure mapping and the stamp rules live in ``ba2_common.core.ohlcv_history``; this module is
the testplatform's thin side of it, so every switch point asks ONE place:

* ``job_history_provider(config)``  -- the raw OHLCV provider of a job / trial config
  (``get_provider("ohlcv", history_registry_key(resolve_job_history_source(config)))``);
* ``job_history_cache_dir(config)`` -- the cache folder that provider reads;
* ``stored_history_source(backtest, session)`` -- the source of a STORED row (re-run, what-if,
  chart): the row's own stamp, else its optimization's, else the legacy ``fmp`` (a row from
  before the switch has no stamp);
* ``refuse_option_job_on_unsupported_source`` -- an OPTION job stamped ``alpaca`` raises
  ``OptionHistorySourceUnsupported`` (ThetaData strikes are as-traded and the split-basis
  converter still reads FMP's calendar; plan Task 12 lifts this).
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional

from ba2_common.core.ohlcv_history import (
    LEGACY_HISTORY_SOURCE,
    OHLCV_HISTORY_SOURCE_KEY,
    check_history_source,
    history_cache_dirname,
    history_registry_key,
    resolve_job_history_source,
)

#: The only source an option job may run on until the split-basis converter reads the verified table.
OPTION_SUPPORTED_SOURCE = "fmp"


class OptionHistorySourceUnsupported(ValueError):
    """An option job was stamped with a history source its option data cannot be joined to."""


def job_history_provider(config: Mapping) -> Any:
    """The raw OHLCV provider of ``config``'s history source (an unstamped config reads ``fmp``)."""
    from ba2_providers import get_provider

    return get_provider("ohlcv", history_registry_key(resolve_job_history_source(config)))


def job_history_cache_dir(config: Mapping) -> str:
    """``CACHE_FOLDER/<provider dir>`` of ``config``'s history source."""
    from ba2_common.config import CACHE_FOLDER

    return os.path.join(CACHE_FOLDER, history_cache_dirname(resolve_job_history_source(config)))


def refuse_option_job_on_unsupported_source(config: Mapping, *, uses_options: bool, where: str) -> None:
    """Raise ``OptionHistorySourceUnsupported`` for an option job whose source is not ``fmp``."""
    if not uses_options:
        return
    source = resolve_job_history_source(config)
    if source != OPTION_SUPPORTED_SOURCE:
        raise OptionHistorySourceUnsupported(
            f"{where}: an option job cannot run on OHLCV history source {source!r}: ThetaData strikes "
            f"are as-traded and the split-basis converter still reads FMP's split calendar. Use "
            f"{OHLCV_HISTORY_SOURCE_KEY}={OPTION_SUPPORTED_SOURCE!r} for option jobs.")


def _blob(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def find_history_stamp(node: Any) -> Optional[str]:
    """The first ``ohlcv_history_source`` value in a persisted config blob (any depth), else None."""
    if isinstance(node, dict):
        if OHLCV_HISTORY_SOURCE_KEY in node:
            return node[OHLCV_HISTORY_SOURCE_KEY]
        for value in node.values():
            found = find_history_stamp(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_history_stamp(value)
            if found is not None:
                return found
    return None


def stored_history_source(backtest: Any, session: Any = None) -> str:
    """The history source a STORED backtest row ran on.

    Order: the row's ``strategy_params`` blob, then the ``optimization_config`` of its
    ``optimization_id`` (needs ``session``), else ``LEGACY_HISTORY_SOURCE`` (the row predates
    the stamp). A stamp that is present but unknown refuses (``UnknownHistorySource``)."""
    stamp = find_history_stamp(_blob(getattr(backtest, "strategy_params", None)))
    optimization_id = getattr(backtest, "optimization_id", None)
    if stamp is None and optimization_id is not None:
        if session is None:
            raise ValueError(
                f"backtest row belongs to optimization {optimization_id} but no session was given to "
                f"read its history-source stamp; refusing to assume {LEGACY_HISTORY_SOURCE!r}")
        from app.models.strategy_optimization import StrategyOptimization

        optimization = session.get(StrategyOptimization, optimization_id)
        if optimization is not None:
            stamp = find_history_stamp(_blob(getattr(optimization, "optimization_config", None)))
    return LEGACY_HISTORY_SOURCE if stamp is None else check_history_source(stamp)
