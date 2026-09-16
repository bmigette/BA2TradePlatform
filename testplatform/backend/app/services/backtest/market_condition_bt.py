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

Task 7: with ``manifest_digest`` set (the run config's ``market_condition_manifest``, pinned by
the launcher and carried into every trial), the reader is a thin wrapper over the host-shared
``MappedMarketConditionReader``: rows come from the published snapshot, ``window_before`` is never
called and no indicator is calculated in a trial. Without a digest it keeps computing on a miss --
research/dev only; a GA trial in that state is REFUSED by ``install_backtest_market_conditions``
rather than run on per-process numbers.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Dict, Optional

import numpy as np

from ba2_common.core.market_calendar import prior_regular_session, regular_session_close_utc
from ba2_common.core.market_condition_context import (
    TIMING_POLICY_PRIOR_SESSION_V1,
    MarketConditionContext,
)
from ba2_common.core.market_condition_readers import MEMO_SIZE, WindowMarketConditionReader
from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY
from ba2_common.core.market_conditions import STATUS_VALID, WINDOW

_log = logging.getLogger(__name__)

__all__ = ["BacktestMarketConditionReader", "BacktestMarketConditionResolver",
           "MarketConditionRunRecord", "attach_entry_states"]


class BacktestMarketConditionReader(WindowMarketConditionReader):
    """``MarketConditionReader`` over one run's ``AsOfPriceSource``."""

    def __init__(self, price_source: Any, profile: str, *, memo_size: int = MEMO_SIZE,
                 manifest_digest: Optional[str] = None, cache_root: Optional[str] = None):
        mapped = None
        if manifest_digest:
            from ba2_common.core.market_condition_reader import MappedMarketConditionReader

            root = cache_root
            if root is None:
                from ba2_common.config import CACHE_FOLDER
                root = CACHE_FOLDER
            # memo_size=0: the WindowMarketConditionReader base memoises the same
            # (symbol, session) key, so a second memo would only double the residency.
            mapped = MappedMarketConditionReader(root, manifest_digest, profile, memo_size=0)
        super().__init__(profile, memo_size=memo_size, retain_windows=False, mapped=mapped)
        self.manifest_digest = manifest_digest
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

class MarketConditionRunRecord:
    """Per-run market-condition telemetry: the D§6 counters and the per-entry state.

    Built by ``install_backtest_market_conditions`` and handed to the engine ONLY when a profile
    is on; with the profile off the engine holds ``None``, every call site below is one ``is
    None`` test, and the results blob is the one a profile-less run has always produced (pinned
    by ``test_market_condition_all_off_matches_baseline``).

    TWO SEPARATE QUESTIONS, deliberately not merged (design section 6, "report eligible
    recommendations separately from condition rejections", and section 7, "report configuration
    failures distinctly from normal short-history cases"):

    * how many recommendations the run had to decide on at all (``eligible_recommendations``);
    * of those, how many a market gate refused on a MEASURED value (``market_gate_rejected``)
      versus refused because the measurement was UNKNOWN, by reason
      (``market_unknown_input_by_reason``).

    One merged number would make "the regime filter is working" and "the feature store does not
    cover these symbols" the same observation -- the exact confusion this report exists to
    prevent.

    THE ENTRY STATE is read from the run's own reader at the moment an entry rule fires: one
    memoised lookup of a row the gate has just read. It is recorded here rather than scraped out
    of the condition evaluations because attribution needs the measurement of the fields whose
    gate was OFF too (with every mode off there are no market leaves at all, and the run still
    has an entry state worth attributing to). Keyed ``(symbol, session_label)``;
    :func:`attach_entry_states` matches each executed trade to the LATEST recorded session at or
    before its entry, so a resting entry that fills after the decision bar still carries the
    state its decision was made on.
    """

    def __init__(self, resolver: Any, *, profile: str, manifest_digest: Optional[str] = None):
        self.resolver = resolver
        self.profile = profile
        self.manifest_digest = manifest_digest
        self.eligible_recommendations = 0
        self.market_evaluated = 0
        self.market_gate_passed = 0
        self.market_gate_rejected = 0
        self.market_unknown_recommendations = 0
        self.market_leaf_evaluations = 0
        self.market_unknown_input_by_reason: Dict[str, int] = {}
        self.entries_staged = 0
        self.entry_read_failures = 0
        self._entry_states: Dict[Any, Dict[str, Any]] = {}

    # -- counters ---------------------------------------------------------------
    def note_eligible(self) -> None:
        """One recommendation reached the entry ruleset (design section 6's "eligible")."""
        self.eligible_recommendations += 1

    def note_conditions(self, condition_evaluations: Any) -> None:
        """Classify ONE recommendation's condition evaluations.

        Only market-condition records carry ``market_condition_status`` (set by
        ``TradeActionEvaluator._evaluate_conditions`` for conditions that define
        ``last_status``), so a ruleset with no market leaf is counted nowhere here and the run's
        other rejections stay the run's other rejections.
        """
        seen = rejected = unknown = False
        for record in condition_evaluations or ():
            status = record.get("market_condition_status")
            if status is None:
                continue
            seen = True
            self.market_leaf_evaluations += 1
            if status == STATUS_VALID:
                if not record.get("condition_result"):
                    rejected = True
            else:
                unknown = True
                self.market_unknown_input_by_reason[status] = \
                    self.market_unknown_input_by_reason.get(status, 0) + 1
        if not seen:
            return
        self.market_evaluated += 1
        if rejected:
            self.market_gate_rejected += 1
        if unknown:
            self.market_unknown_recommendations += 1
        if not rejected and not unknown:
            self.market_gate_passed += 1

    # -- entry state ------------------------------------------------------------
    def note_entry(self, account: Any, symbol: str, recommendation: Any) -> None:
        """Record the market-condition measurement behind ONE fired entry rule."""
        self.entries_staged += 1
        try:
            ctx = self.resolver(account, symbol, recommendation)
            if ctx is None:
                return
            key = (symbol, ctx.session_label)
            if key in self._entry_states:
                return                      # same symbol, same session: the same measurement
            row = ctx.reader.observe(symbol, ctx.prior_session)
            values: Dict[str, Any] = {}
            if row is not None:
                for name, obs in row.by_field().items():
                    values[name] = {"value": obs.value, "status": obs.status}
            self._entry_states[key] = {
                "symbol": symbol,
                "session": ctx.session_label.isoformat(),
                "prior_session": ctx.prior_session.isoformat(),
                "values": values,
            }
        except Exception as e:  # noqa: BLE001 -- telemetry never fails a run
            self.entry_read_failures += 1
            _log.warning(f"market-condition entry state not recorded for {symbol}: {e}")

    # -- output -----------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {
            "eligible_recommendations": self.eligible_recommendations,
            "market_evaluated": self.market_evaluated,
            "market_gate_passed": self.market_gate_passed,
            "market_gate_rejected": self.market_gate_rejected,
            "market_unknown_recommendations": self.market_unknown_recommendations,
            "market_unknown_input_by_reason": dict(
                sorted(self.market_unknown_input_by_reason.items())),
            "market_leaf_evaluations": self.market_leaf_evaluations,
            "entries_staged": self.entries_staged,
            "entry_read_failures": self.entry_read_failures,
        }

    def entry_states(self) -> list:
        """The recorded states, ordered by (session, symbol) so a run is byte-reproducible.

        NOT part of :meth:`as_dict`: the states are attached to the trades they explain (see
        :func:`attach_entry_states`) rather than persisted a second time as a standalone list.
        A gated option genome enters ~1k times over the goal2020 window, and a second copy of
        that in every trial's result dict is bytes over the wire for every individual of every
        generation, for data the report reads off the trades anyway.
        """
        return [self._entry_states[k] for k in sorted(
            self._entry_states, key=lambda k: (str(k[1]), str(k[0])))]

    def as_dict(self) -> Dict[str, Any]:
        reader = getattr(self.resolver, "reader", None)
        return {
            "profile": self.profile,
            "manifest": self.manifest_digest,
            "calc_version": getattr(reader, "calc_version", None),
            "source_profile": getattr(self.resolver, "source_profile", None),
            "timing_policy": TIMING_POLICY_PRIOR_SESSION_V1,
            "stats": self.stats(),
        }


def attach_entry_states(trades: Any, entry_states: Any) -> int:
    """Attach ``entry_state`` to every trade an entry-state record covers; return how many.

    A trade is matched on its UNDERLYING (an option leg's ``underlying_symbol``, else
    ``symbol``) and on the LATEST recorded session at or before its entry date: a resting entry
    that fills days after the decision still carries the state the decision was made on, and a
    symbol entered repeatedly gets each entry's own state rather than the first or the last. A
    trade with no record at or before it (a position opened by something other than a gated
    entry rule -- an assignment, say) is left untouched: an absent key is honest, an empty one
    would let the report bin it as though it had been measured.
    """
    from bisect import bisect_right

    by_symbol: Dict[str, Any] = {}
    for record in entry_states or ():
        by_symbol.setdefault(str(record["symbol"]).upper(), []).append(record)
    index = {sym: ([r["session"] for r in sorted(recs, key=lambda r: r["session"])],
                   sorted(recs, key=lambda r: r["session"]))
             for sym, recs in by_symbol.items()}
    attached = 0
    for trade in trades or ():
        symbol = trade.get("underlying_symbol") or trade.get("symbol")
        entry = str(trade.get("entry_time") or "")[:10]
        if not symbol or not entry:
            continue
        found = index.get(str(symbol).upper())
        if not found:
            continue
        sessions, records = found
        pos = bisect_right(sessions, entry)
        if pos == 0:
            continue
        chosen = records[pos - 1]
        trade["entry_state"] = {"session": chosen["session"],
                                "prior_session": chosen["prior_session"],
                                "values": chosen["values"]}
        attached += 1
    return attached
