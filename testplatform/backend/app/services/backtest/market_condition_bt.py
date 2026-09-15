"""Backtest adapter for the market-condition entry gates.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` sections 4 and 4.1 --
"The BT adapter gets time from the engine and bars from its pinned offline input store."

* :class:`BacktestMarketConditionReader` reads the run's ``AsOfPriceSource`` columns through
  ``window_before`` (no DataFrame), assembles and computes through the shared
  ``WindowMarketConditionReader`` core (bounded memo, calc-version check).
* :class:`BacktestMarketConditionResolver` builds ONE frozen ``MarketConditionContext`` per
  simulated session and returns that same object for every leaf evaluated on the session:
  ``session_label = account._as_of_date()``, ``prior_session = prior_regular_session(label)``,
  ``decision_time`` = the label's regular close in UTC (deterministic, tz-aware).

Imported only when a run's config carries a profile other than ``none`` (see
``seam_wiring.install_backtest_market_conditions``); a profile-less run never loads this module.
Task 7 swaps the reader's compute path for the host-shared mapped feature store.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Optional

import numpy as np

from ba2_common.core.market_calendar import prior_regular_session, regular_session_close_utc
from ba2_common.core.market_condition_context import (
    TIMING_POLICY_PRIOR_SESSION_V1,
    MarketConditionContext,
)
from ba2_common.core.market_condition_readers import MEMO_SIZE, WindowMarketConditionReader
from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY
from ba2_common.core.market_conditions import WINDOW

_log = logging.getLogger(__name__)

__all__ = ["BacktestMarketConditionReader", "BacktestMarketConditionResolver"]


class BacktestMarketConditionReader(WindowMarketConditionReader):
    """``MarketConditionReader`` over one run's ``AsOfPriceSource``."""

    def __init__(self, price_source: Any, profile: str, *, memo_size: int = MEMO_SIZE):
        super().__init__(profile, memo_size=memo_size, retain_windows=False)
        self._ps = price_source

    def _bars(self, symbol: str, session: date):
        bars = self._ps.window_before(symbol, session, WINDOW)
        if bars is not None:
            return bars
        count = self._ps.count_through(symbol, session)
        if count:
            # Short: hand what exists so assemble_window can tell a young listing
            # (insufficient_history) from a hole (missing_session).
            return self._ps.window_before(symbol, session, count)
        if self._ps.has_symbol(symbol):
            # Bars exist, all after the session: the listing has not started yet. The first
            # later bar's date is enough for assemble_window to say insufficient_history.
            first = self._ps.next_bar_date(symbol, datetime.combine(session, time()))
            nan = np.array([np.nan])
            return (np.array([first], dtype="datetime64[D]"), nan, nan, nan, nan, nan)
        return None


class BacktestMarketConditionResolver:
    """The per-run resolver: one context per simulated session, same object on repeat calls."""

    def __init__(self, reader: Any, *, source_profile: str = SOURCE_PROFILE_FMP_DAILY):
        self.reader = reader
        self.source_profile = source_profile
        self._ctx: Optional[MarketConditionContext] = None
        self._not_a_session: Optional[date] = None

    def __call__(self, account: Any, instrument_name: str,
                 expert_recommendation: Any) -> Optional[MarketConditionContext]:
        label = account._as_of_date()
        ctx = self._ctx
        if ctx is not None and ctx.session_label == label:
            return ctx
        if self._not_a_session == label:
            return None
        try:
            decision_time = regular_session_close_utc(label)
        except ValueError:
            # A bar on a non-session date (a vendor glitch in the trading clock). The gate is
            # unknown for that date -- reported as no_context by the condition, never a pass.
            self._not_a_session = label
            _log.warning(f"market-condition gates: simulated date {label} is not a regular NYSE "
                         f"session; gates report no_context for it")
            return None
        ctx = MarketConditionContext(
            decision_time=decision_time,
            session_label=label,
            prior_session=prior_regular_session(label),
            source_profile=self.source_profile,
            timing_policy=TIMING_POLICY_PRIOR_SESSION_V1,
            calc_version=self.reader.calc_version,
            reader=self.reader,
            recorder=None,
        )
        self._ctx = ctx
        return ctx
