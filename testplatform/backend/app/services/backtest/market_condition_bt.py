"""Backtest adapter for the market-condition entry gates.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` sections 4 and 4.1 --
"The BT adapter gets time from the engine and bars from its pinned offline input store."

* :class:`BacktestMarketConditionReader` reads the run's ``AsOfPriceSource`` columns through
  ``window_before`` (no DataFrame), assembles and computes through the shared
  ``WindowMarketConditionReader`` core (bounded memo, calc-version check).
* :class:`BacktestMarketConditionResolver` builds ONE frozen ``MarketConditionContext`` per
  simulated bar and returns that same object for every leaf evaluated on the bar. THE SESSION
  CLOCK IS THE LIVE ONE (BT/live parity, ``docs/plans/2026-09-22-bt-live-option-parity.md`` §0):
  bar D decides with data through D's close and fills on the next bar, so it is the live
  decision made during the next regular session N(D). Hence
  ``session_label = backtest_decision_label(D)`` (= N(D)),
  ``prior_session = decision_data_session(session_label)`` (= D itself), and
  ``decision_time`` = D's regular close in UTC -- the instant the backtest actually decides
  (deterministic, tz-aware).

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
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np

from ba2_common.core.market_calendar import (
    backtest_decision_label,
    decision_data_session,
    regular_session_close_utc,
    is_regular_session,
)
from ba2_common.core.market_condition_context import (
    TIMING_POLICY_PRIOR_SESSION_V1,
    MarketConditionContext,
)
from ba2_common.core.market_condition_readers import MEMO_SIZE, WindowMarketConditionReader
from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY
from ba2_common.core.market_conditions import (
    PROFILES, STATUS_MISSING_SESSION, STATUS_VALID, WINDOW,
)

_log = logging.getLogger(__name__)

__all__ = ["BacktestMarketConditionReader", "BacktestMarketConditionResolver",
           "MarketConditionRunRecord", "apply_market_condition_block",
           "attach_entry_states"]


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
    """The per-run resolver: one context per simulated bar, same object on repeat calls.

    The context is the one a LIVE decision during ``backtest_decision_label(bar)`` builds (see
    the module docstring): same ``prior_session``, same row, same gate answer. Pinned against
    the live ``DecisionState`` by ``test_market_condition_bt_live_session_parity``.
    """

    def __init__(self, reader: Any, *, source_profile: str = SOURCE_PROFILE_FMP_DAILY):
        self.reader = reader
        self.source_profile = source_profile
        self._ctx: Optional[MarketConditionContext] = None
        #: The BAR the cached context was built for. Compared instead of ``ctx.session_label``,
        #: which is the NEXT session and never equals the bar.
        self._ctx_bar: Optional[date] = None
        self._not_a_session: Optional[date] = None

    def __call__(self, account: Any, instrument_name: str,
                 expert_recommendation: Any) -> Optional[MarketConditionContext]:
        bar = account._as_of_date()
        ctx = self._ctx
        if ctx is not None and self._ctx_bar == bar:
            return ctx
        if self._not_a_session == bar:
            return None
        if not is_regular_session(bar):
            # A bar on a non-session date (a vendor glitch in the trading clock). The gate is
            # unknown for that date -- reported as no_context by the condition, never a pass.
            self._not_a_session = bar
            _log.warning(f"market-condition gates: simulated date {bar} is not a regular NYSE "
                         f"session; gates report no_context for it")
            return None
        # Outside any handler: on a session bar, a calendar-edge failure (no later session, a
        # table that does not reach) is a fault to surface, not a "not a session" no_context.
        session_label = backtest_decision_label(bar)
        ctx = MarketConditionContext(
            decision_time=regular_session_close_utc(bar),
            session_label=session_label,
            prior_session=decision_data_session(session_label),
            source_profile=self.source_profile,
            timing_policy=TIMING_POLICY_PRIOR_SESSION_V1,
            calc_version=self.reader.calc_version,
            reader=self.reader,
            recorder=None,
        )
        self._ctx, self._ctx_bar = ctx, bar
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
    has an entry state worth attributing to). Keyed ``(symbol, session_label)``: the decision
    LABEL, which for bar D is N(D), the next regular session (``backtest_decision_label``) and
    the session a live run of the same decision records. ``prior_session`` is the bar D itself.
    :func:`attach_entry_states` matches each executed STRUCTURE to the LATEST recorded decision
    BAR at or before its entry (see there for why the bar and not the label), so a next-bar fill
    binds with gap 0 and a resting entry that fills later still carries the state its decision
    was made on -- and records the gap, and whether the choice was ambiguous, because this is
    date proximity and not identity.
    """

    def __init__(self, resolver: Any, *, profiles: Sequence[str],
                 manifests: Optional[Mapping[str, Optional[str]]] = None):
        #: The run's profiles, in the order the config pinned them, and the digest each one's
        #: snapshot was pinned to. PLURAL since Task 10: a run can gate on ``ohlcv-v1`` and
        #: ``ta-structure-v1`` at once, every profile is its own warmed snapshot with its own
        #: digest and coverage, and there is no single "the manifest" to report.
        self.resolver = resolver
        self.profiles = [str(p) for p in profiles]
        if not self.profiles:
            raise ValueError("a market-condition run record needs at least one profile")
        self.manifests: Dict[str, Optional[str]] = {p: (manifests or {}).get(p) for p in self.profiles}
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
        #: Set by :func:`apply_market_condition_block` from the BINDING, not the capture: how
        #: many structures took a same-session state, how many took an earlier one, and how many
        #: had more than one candidate decision to choose from (:func:`attach_entry_states`).
        #:
        #: ``None`` until the binding has actually run, and the counters are then OMITTED from
        #: ``stats()`` rather than reported as zeros. "No trade was bound across a gap" and "the
        #: binding has not happened yet" are different facts, and a run whose blob was assembled
        #: without it -- an engine-level test, a caller that never reaches the handler -- must
        #: not read as a run with a perfect same-session binding.
        self.binding: Optional[Dict[str, int]] = None

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

        SCOPE: ``evaluator.condition_evaluations`` spans EVERY rule the evaluator walked, not
        only the one that fired (the evaluator is first-match over the ruleset's ordered
        rules). With more than one entry rule carrying market leaves, a recommendation a later
        rule admitted would still be counted as rejected by the earlier rule's leaf. The
        launcher emits the market gates onto ONE entry rule per structure, so that case does
        not arise today; if it ever does, the classification has to move inside the winning
        rule's ``rule_evaluations`` entry instead of reading the flat list.
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
        """Record the market-condition measurement behind ONE fired entry rule.

        ``session`` is the decision label N(bar) (the session the fill executes in) and
        ``prior_session`` the bar itself, whose row the gate read and which the binding keys on.
        """
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
            else:
                # NO ROW IS ITSELF A MEASUREMENT OUTCOME, and it is the one an operator most
                # needs to see: it is what an uncovered symbol produces for a whole run. Record
                # it as the status the gate would have reported (``missing_session``) rather
                # than an empty dict, which the report cannot tell from "this run predates the
                # entry-state capture".
                for profile in self.profiles:
                    for field in PROFILES[profile].fields:
                        values[field.name] = {"value": None, "status": STATUS_MISSING_SESSION}
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
        out = {
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
        if self.binding is not None:
            # HOW GOOD THE BINDING WAS, not just how much of it there was. A structure bound on
            # the decision's own session is as certain as this scheme gets; one bound across a
            # gap, and above all one whose window held MORE THAN ONE decision, is an inference.
            # The report prints these next to the attribution table so a reader can see how much
            # of it rests on date proximity.
            out.update({
                "bound_same_session": self.binding.get("same_session", 0),
                "bound_with_gap": self.binding.get("with_gap", 0),
                "ambiguous": self.binding.get("ambiguous", 0),
            })
        return out

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
        """The persisted ``market_condition`` block. PLURAL since Task 10 -- ``profiles``,
        ``manifests`` and ``calc_versions`` are keyed by profile even for a one-profile run.

        The shape changed ONCE, together with the seam, deliberately: a block that reported
        ``"profile"`` for the first profile and hid the second would be worse than a shape
        change, and there is no reading of a single ``manifest`` that is true of two snapshots.
        A run with the gates OFF still produces NO block at all, which is what keeps every
        existing backtest byte-identical (``test_market_condition_all_off_matches_baseline``).
        """
        reader = getattr(self.resolver, "reader", None)
        versions = getattr(reader, "calc_versions", None)
        if not versions:
            versions = {self.profiles[0]: getattr(reader, "calc_version", None)}
        return {
            "profiles": list(self.profiles),
            "manifests": dict(self.manifests),
            "calc_versions": {p: versions.get(p) for p in self.profiles},
            "source_profile": getattr(self.resolver, "source_profile", None),
            "timing_policy": TIMING_POLICY_PRIOR_SESSION_V1,
            "stats": self.stats(),
        }


#: How far after a recorded decision a fill may still be attributed to it, in CALENDAR days.
#:
#: WHY A BOUND AT ALL. The match is by date, not by identity: the trade blob carries no
#: recommendation id, so the only link between "the entry rule fired for AAA on the 5th" and
#: "an AAA position opened on the 11th" is their order in time. Without a bound, ANY later
#: position on that underlying inherits the last rule-fired state -- an assignment, a lifecycle
#: roll, a position opened months afterwards -- and the attribution table would report a
#: measured regime for a trade that no measurement produced.
#:
#: WHY SEVEN. An entry order in this engine is DAY time-in-force (``backtest_account``
#: re-submits or expires it; live forces the same), so a fill is stamped with the decision bar
#: or the next one (gap 0 or one session), and a re-submission on a later bar fires the rule
#: again and records its own state.
#: A week is generous for a long holiday weekend and still far short of the interval at which
#: a stale attribution could look plausible.
ENTRY_STATE_MAX_GAP_DAYS = 7


def _structure_groups(trades: Any) -> Any:
    """Trade rows grouped into the units a reader means by "a structure", in first-appearance
    order: option legs sharing a ``transaction_id`` are ONE bet, everything else is its own.

    The same rule as ``results._cap_groups`` and the report tool's ``structures`` -- stated a
    third time rather than imported because those two work on a live account and on a persisted
    blob respectively, and this one runs between them, inside the handler. If it ever needs a
    fourth copy, that is the moment to lift it into ``results``.
    """
    groups: "OrderedDict[Any, list]" = OrderedDict()
    loose: list = []
    for trade in trades or ():
        txn = trade.get("transaction_id")
        if txn is not None and trade.get("contract_symbol"):
            groups.setdefault(txn, []).append(trade)
        else:
            loose.append([trade])
    return list(groups.values()) + loose


def attach_entry_states(trades: Any, entry_states: Any,
                        max_gap_days: int = ENTRY_STATE_MAX_GAP_DAYS) -> Dict[str, int]:
    """Attach ``entry_state`` to the structures an entry-state record covers.

    Returns ``{"attached", "same_session", "with_gap", "ambiguous"}`` -- how many STRUCTURES
    were bound, and how certain each binding was. The counts are of structures throughout:
    since the state is written once per structure (below), a separate count of trade ROWS
    carrying the key would always equal ``attached`` and could only ever disagree with it by
    being wrong.

    A structure is matched on its UNDERLYING (an option leg's ``underlying_symbol``, else
    ``symbol``) and on the LATEST recorded decision BAR at or before its entry date, provided
    that bar is within ``max_gap_days`` of it (see ``ENTRY_STATE_MAX_GAP_DAYS``).

    THE KEY IS THE BAR (``prior_session``), NOT THE LABEL (``session``): a record's ``session``
    is N(D) and its ``prior_session`` is the bar D, and the engine stamps a ``next_bar_open``
    fill with D (``BacktestAccount._apply_fill``/``_apply_option_fill`` record ``as_of``), so
    ``entry_time`` is D. Keyed on the label, every such fill would precede its own decision.

    A resting entry that fills a day or two after the decision still carries the state the
    decision was made on; a position opened by something other than a gated entry rule is left
    UNTOUCHED. An absent key is honest; an inherited one would be a fabricated observation.

    THE BINDING IS DATE PROXIMITY, NOT IDENTITY, AND IT SAYS SO IN THE DATA. The trade blob
    carries no recommendation id, and ``note_entry`` records a state whenever an entry RULE
    fires -- including for decisions the dup-position or equity gate then stopped, which produce
    no order at all. So a fill can sit within the window of more than one recorded decision, and
    picking the latest is a choice, not a fact. Every attached state therefore carries
    ``gap_days`` and, when the window held more than one candidate, ``ambiguous: True``. A
    reader who never looks still gets the best available answer; a reader checking a surprising
    bin can see exactly which rows were inferred.

    THE STATE IS WRITTEN ONCE PER STRUCTURE, on its first leg. A four-leg condor is one
    decision and one measurement, and four copies of the same ~200-byte dict in the persisted
    trades blob is three copies of nothing -- on every individual of every generation.
    ``attribution`` reads the first leg of the group that carries one.
    """
    from bisect import bisect_left, bisect_right
    from datetime import date as _date

    by_symbol: Dict[str, Any] = {}
    for record in entry_states or ():
        by_symbol.setdefault(str(record["symbol"]).upper(), []).append(record)
    index = {}
    for sym, recs in by_symbol.items():
        recs.sort(key=lambda r: r["prior_session"])    # sorted ONCE, in place, on the BAR
        index[sym] = ([r["prior_session"] for r in recs], recs)

    out = {"attached": 0, "same_session": 0, "with_gap": 0, "ambiguous": 0}
    for group in _structure_groups(trades):
        head = group[0]
        symbol = head.get("underlying_symbol") or head.get("symbol")
        entry = str(head.get("entry_time") or "")[:10]
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
        try:
            gap = (_date.fromisoformat(entry) - _date.fromisoformat(chosen["prior_session"])).days
        except ValueError:
            continue                       # an unparseable date is not a match, and not a guess
        if gap > max_gap_days:
            continue
        # How many recorded decisions for this symbol fall inside the same window? More than
        # one and the latest is an inference, not the answer.
        lo = (_date.fromisoformat(entry) - timedelta(days=max_gap_days)).isoformat()
        candidates = pos - bisect_left(sessions, lo)
        state = {"session": chosen["session"], "prior_session": chosen["prior_session"],
                 "values": chosen["values"], "gap_days": gap}
        if candidates > 1:
            state["ambiguous"] = True
            out["ambiguous"] += 1
        head["entry_state"] = state
        out["attached"] += 1
        out["same_session" if gap == 0 else "with_gap"] += 1
    return out


def apply_market_condition_block(results: Dict[str, Any], record: Any) -> Dict[str, Any]:
    """Add the run's research metadata to a finished ``build_results`` blob, in place.

    Called by ``run_daily_backtest`` AFTER every metric has been computed, so nothing here can
    reach one: the counters and the per-trade entry state are evidence about a run, never an
    input to scoring it. ``record is None`` (every profile-less run) returns the blob untouched
    and adds no key -- which is what the all-off compatibility gate compares.
    """
    if record is None:
        return results
    record.binding = attach_entry_states(results.get("trades"), record.entry_states())
    block = record.as_dict()
    block["stats"]["structures_with_entry_state"] = record.binding.get("attached", 0)
    results["market_condition"] = block
    return results
