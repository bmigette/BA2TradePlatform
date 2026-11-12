"""The replay report: per-analysis results, plus the spec section 8 stage table.

"Reports must state which capabilities ran. A successful normalized-bundle
replay must never be labelled a complete live/backtest match." (spec section 2)

So the header of every report names the capability that ran AND the ones that did
not, the stage table carries an explicit ``not_run`` row for Selection,
Rules/sizing and Execution, and the totals include every recorded analysis --
HOLD, skip and error alike. "Include HOLD/skipped/failed analyses in totals;
never report 100% by dropping unavailable rows." (spec section 8)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ba2_common.core.replay import CoverageEntry, ReplayStatus
from ba2_common.core.replay.schemas import SCHEMA_VERSION
from ba2_common.core.replay.service import COVERAGE_NAME

__all__ = [
    "AnalysisResult",
    "FieldDiff",
    "ReplayReport",
    "merge_coverage",
    "STAGE_ROWS",
    "CAPABILITY_CAVEATS",
    "CAPABILITY_TITLES",
    "CAPABILITY_OPTION_SELECTION",
    "OPTION_STAGE_ROWS",
    "DECISION_STAGE_ROWS",
    "STAGE_SELECTION",
    "STAGE_RULES_AND_SIZING",
    "STAGE_EXECUTION",
    "STAGE_DECISION_RULES",
    "STAGE_DECISION_SIZING",
    "STAGE_DECISION_TPSL",
    "STAGE_DECISION_ORDER",
    "STAGE_DECISION_SUBMIT",
    "STATUS_ORDER",
]

#: Every status the report can carry, in the order it is tabulated. Fixed rather
#: than derived from the data, so a status with zero rows shows as 0 instead of
#: vanishing from the table.
STATUS_ORDER: Tuple[str, ...] = (
    ReplayStatus.COVERAGE_MATCH,
    ReplayStatus.COVERAGE_DIFFERENCE,
    ReplayStatus.COVERAGE_CODE_DRIFT,
    ReplayStatus.COVERAGE_MISSING_CAPTURE,
    ReplayStatus.COVERAGE_MISSING_HISTORY,
    ReplayStatus.COVERAGE_REVISION_UNKNOWN,
    ReplayStatus.COVERAGE_UNSUPPORTED,
    ReplayStatus.COVERAGE_NOT_RUN,
)

CAPABILITY_TITLES: Dict[str, str] = {
    ReplayStatus.CAPABILITY_RECORDED_EXPERT:
        "recorded expert replay (_process on the recorded bundle)",
    ReplayStatus.CAPABILITY_GATHER_TAPE:
        "gather-tape replay (_gather against the recorded provider returns)",
    ReplayStatus.CAPABILITY_HISTORICAL: "historical expert comparison",
    ReplayStatus.CAPABILITY_DECISION: "decision and execution comparison",
}

#: The option selection replay (``option_selection``). A separate, additive capability: it is NOT
#: listed in :data:`CAPABILITY_TITLES`, so the four stock capabilities' "not run" lists (and the
#: inventory's per-capability coverage) keep their exact shape. Its own report states what it did
#: not run, including the four above.
CAPABILITY_OPTION_SELECTION = "option_selection"

OPTION_CAPABILITY_TITLE = ("option selection replay (the recorded option entry / close action "
                           "re-run on its tape of broker reads)")

#: The one comparison no tape can make: re-selecting the contract from the HISTORICAL data
#: provider (parquet) instead of the live broker chain. Named in every option report so a
#: ``match`` is never read as "the backtest would have chosen this contract".
HISTORICAL_PROVIDER_TITLE = ("historical_provider comparison (the same selection on the parquet "
                             "chain instead of the live broker chain)")

OPTION_CAVEAT = (
    "A match proves only that the shared option action code, given the broker reads (chain, "
    "quotes, IV, sizing figures) and database rows live actually consumed, reproduces the "
    "contracts, limit price, quantity, trade record and refusal live actually produced. "
    "`historical_provider` (parquet vs broker) is **NOT run**: it says nothing about whether the "
    "backtest data would have chosen the same contract.")

#: What a ``match`` in each capability does and does NOT establish. Per capability
#: because the four establish different things and one shared sentence is wrong for
#: three of them -- the recorded-expert caveat ("historical reconstruction is not
#: validated here") reads as a contradiction on a HISTORICAL report, which is
#: exactly the kind of quietly-misleading header spec section 2 forbids ("A
#: successful normalized-bundle replay must never be labelled a complete
#: live/backtest match").
CAPABILITY_CAVEATS: Dict[str, str] = {
    ReplayStatus.CAPABILITY_RECORDED_EXPERT:
        "A match proves only that the shared expert calculation, given the inputs live "
        "actually consumed, reproduces what live actually produced. It is **NOT** a "
        "live/backtest match: `_gather`, historical reconstruction, selection, rules, "
        "sizing and execution are not validated here.",
    ReplayStatus.CAPABILITY_GATHER_TAPE:
        "A match proves only that the live `_gather` maps the recorded provider returns "
        "into the recorded normalized bundle. It says nothing about whether those returns "
        "could be reconstructed historically, nor about selection, rules, sizing or "
        "execution.",
    ReplayStatus.CAPABILITY_HISTORICAL:
        "A match proves that the `analyze_as_of` reconstruction from the named cache root "
        "produced the same inputs and the same recommendation as live. It is a statement "
        "about THAT root: a different or later-warmed root can differ, and selection, "
        "rules, sizing and execution are not validated here. A **difference** is evidence "
        "of reconstruction, coverage, timing or revision drift -- it is not automatically "
        "a defect, and zero difference is not always attainable (spec section 8).",
    ReplayStatus.CAPABILITY_DECISION:
        "A match proves that the recorded rules, sizing and protection decisions recompute "
        "from the recorded account state. Broker fills and rejections are observations, "
        "not a promise that a fill model reproduces the market.",
}

#: The spec section 8 stage table. ``capabilities`` names EVERY capability whose
#: results can fill the row; an empty tuple means this delivery cannot fill it at
#: all, so the row is ``not_run`` by construction (spec step 6, a later delivery).
#: The Rules-and-sizing and Execution rows are what
#: ``app/services/backtest/parity_harness.py`` already compares for a BACKTEST;
#: the spec-step-6 decision trace is where that machinery meets a recorded LIVE
#: session, and these rows stay ``not_run`` until it does.
#:
#: A stage can be filled by MORE THAN ONE capability and they do not mean the same
#: thing. "Expert inputs" is a gather-tape row when the bundle was rebuilt from the
#: recorded provider returns, and a historical row when it was rebuilt from a pinned
#: cache root -- the first proves the mapping, the second measures reconstruction
#: drift. Only the capability that actually ran fills the row in ITS report.
STAGE_SELECTION = "Selection"
STAGE_RULES_AND_SIZING = "Rules and sizing"
STAGE_EXECUTION = "Execution"

STAGE_ROWS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (STAGE_SELECTION,
     "Candidate coverage, filters, ranked order, selected/held symbols",
     (ReplayStatus.CAPABILITY_DECISION,)),
    ("Expert inputs",
     "Normalized _gather bundle rebuilt from the recorded provider returns "
     "(gather tape) or from a pinned historical cache root (historical)",
     (ReplayStatus.CAPABILITY_GATHER_TAPE, ReplayStatus.CAPABILITY_HISTORICAL)),
    ("Recommendation",
     "Skip reason, signal, confidence, expected profit, current price, target",
     (ReplayStatus.CAPABILITY_RECORDED_EXPERT, ReplayStatus.CAPABILITY_HISTORICAL)),
    (STAGE_RULES_AND_SIZING,
     "Branch, eligibility, operands/budget, intended side/quantity, TP/SL",
     (ReplayStatus.CAPABILITY_DECISION,)),
    (STAGE_EXECUTION,
     "Submit attempts/rejections, partial/full fills, final active protection",
     (ReplayStatus.CAPABILITY_DECISION,)),
)

#: The stock decision-pass replay's finer stage rows (shown only on a ``decision`` report; the three
#: coarse rows above are their roll-up). Comparison is EXACT at every stage; a numeric difference carries
#: its delta. A row that is about another kind of step (a ``rules`` row has no order build) is
#: ``not_run`` there, so every row is represented at every stage.
STAGE_DECISION_RULES = "Decision rules"
STAGE_DECISION_SIZING = "Decision sizing"
STAGE_DECISION_TPSL = "Decision TP/SL"
STAGE_DECISION_ORDER = "Decision order build"
STAGE_DECISION_SUBMIT = "Decision submit / refusal"

DECISION_STAGE_ROWS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (STAGE_DECISION_RULES,
     "Action summaries, every condition evaluation (type, result, the values it read), rule verdicts, "
     "gate verdicts (open position, equity, route)",
     (ReplayStatus.CAPABILITY_DECISION,)),
    (STAGE_DECISION_SIZING,
     "Candidate orders, funded set (symbol, quantity, stop), the RM's decisions (outcome, reason, "
     "operands, binding constraint) and context figures",
     (ReplayStatus.CAPABILITY_DECISION,)),
    (STAGE_DECISION_TPSL,
     "Take-profit / stop-loss prices handed to the account (submit_order, adjust_tp/sl)",
     (ReplayStatus.CAPABILITY_DECISION,)),
    (STAGE_DECISION_ORDER,
     "The main order and transaction the execute built (fields, not ids or timestamps)",
     (ReplayStatus.CAPABILITY_DECISION,)),
    (STAGE_DECISION_SUBMIT,
     "Ordered account effect calls with arguments, action results, the refusal or error",
     (ReplayStatus.CAPABILITY_DECISION,)),
)

DECISION_NOT_RUN = (
    "Smart risk manager passes (LLM reasoning); option entries/exits (replayed by `option-selection`); "
    "broker fills, rejections and adapter-built protective legs (observations, not decisions); rules "
    "that read market-data providers (OHLCV / indicators / fundamentals) report `missing_capture` "
    "until those reads are served from the tape")


#: The option selection stage rows (shown only on an ``option_selection`` report). Comparison is
#: EXACT at every stage.
STAGE_OPTION_SELECTION = "Option contract selection"
STAGE_OPTION_PRICING = "Option limit price"
STAGE_OPTION_SIZING = "Option quantity"
STAGE_OPTION_RECORD = "Option trade record"
STAGE_OPTION_REFUSAL = "Option refusal"

OPTION_STAGE_ROWS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (STAGE_OPTION_SELECTION,
     "Per leg: contract symbol, side, ratio, expiry (close: the position closed)",
     (CAPABILITY_OPTION_SELECTION,)),
    (STAGE_OPTION_PRICING, "Limit price handed to the account",
     (CAPABILITY_OPTION_SELECTION,)),
    (STAGE_OPTION_SIZING, "Quantity handed to the account",
     (CAPABILITY_OPTION_SELECTION,)),
    (STAGE_OPTION_RECORD, "entry_record / exit_record, key by key",
     (CAPABILITY_OPTION_SELECTION,)),
    (STAGE_OPTION_REFUSAL, "Success flag, refusal message, error",
     (CAPABILITY_OPTION_SELECTION,)),
)


@dataclass(frozen=True)
class FieldDiff:
    """One differing field, and -- for a numeric one -- HOW FAR it moved.

    Spec section 8: "Historical comparison reports absolute/relative input
    differences and resulting decision differences." A pair of reprs answers
    "different?" but not "by how much", and a 0.3% price drift and a 30x one read
    identically in a table of reprs.

    The deltas are REPORTING only. Equality stays exact -- ``rel_delta`` is never
    compared against a tolerance, and a diff with a tiny delta is still a
    ``difference``. "No broad 'close enough' tolerance may hide a changed signal,
    threshold crossing, share quantity or stop tick."

    ``rel_delta`` is ``None`` when the recorded value is 0 (there is no relative
    change from nothing) and that case is rendered as such rather than as 0.0 or
    as an infinity, so a reader can tell "no relative change" from "not defined".
    """

    field: str
    recorded: str
    produced: str
    #: ``abs(produced - recorded)`` for a numeric leaf; ``None`` for anything else.
    abs_delta: Optional[float] = None
    #: ``abs_delta / abs(recorded)``; ``None`` when not numeric OR recorded == 0.
    rel_delta: Optional[float] = None
    #: True when this IS a numeric diff whose ``rel_delta`` is undefined (recorded
    #: is 0). Distinguishes "no relative delta because the baseline is zero" from
    #: "no relative delta because this is not a number".
    rel_delta_undefined: bool = False
    #: True when the two sides ARE numbers but the distance between them is not
    #: finite (a NaN on either side, an infinity). There is no honest answer to
    #: "how far did it move" then -- and a NaN in the payload would abort
    #: ``report.write`` (``allow_nan=False``) or, worse, serialize as ``NaN`` and
    #: be read back as a number. The fact is recorded instead of the non-number.
    delta_not_finite: bool = False

    @classmethod
    def coerce(cls, value: Any) -> "FieldDiff":
        """Accept a plain ``(field, recorded, produced)`` triple or a FieldDiff."""
        if isinstance(value, FieldDiff):
            return value
        field, recorded, produced = value
        return cls(field=field, recorded=recorded, produced=produced)

    def delta_text(self) -> str:
        """``abs 0.2 (16.7%)`` -- empty for a diff with no numeric distance.

        ASCII ONLY. ``ba2-test replay historical`` prints the markdown straight to
        the console, and a Windows console on cp1252 cannot encode a greek delta:
        the whole command would die with a UnicodeEncodeError after the work was
        done, on the one report that carries the most information.
        """
        if self.delta_not_finite:
            return "delta not finite (nan/inf)"
        if self.abs_delta is None:
            return ""
        if self.rel_delta is not None:
            return f"abs {self.abs_delta:.6g} ({self.rel_delta * 100:.3g}%)"
        if self.rel_delta_undefined:
            return f"abs {self.abs_delta:.6g} (relative undefined: recorded is 0)"
        return f"abs {self.abs_delta:.6g}"

    def to_mapping(self) -> Dict[str, Any]:
        """The JSON shape. Numeric keys appear ONLY on a numeric diff, so a
        capability that reports no deltas keeps its previous output exactly."""
        out: Dict[str, Any] = {"field": self.field, "recorded": self.recorded,
                               "produced": self.produced}
        if self.delta_not_finite:
            out["delta_not_finite"] = True
        if self.abs_delta is not None:
            out["abs_delta"] = self.abs_delta
            out["rel_delta"] = self.rel_delta
            if self.rel_delta is None:
                out["rel_delta_undefined"] = self.rel_delta_undefined
        return out


@dataclass(frozen=True)
class AnalysisResult:
    """One analysis, one capability: what happened and why."""

    analysis_id: str
    expert_class: str
    symbol: str
    use_case: str
    recorded_outcome: str
    status: str
    #: The recorded ATTEMPT this row is about. A re-run of one live analysis is a
    #: second attempt (the store keys analyses on ``(analysis_id, attempt_id)``),
    #: so the analysis id alone does not identify a row -- and anything that keys
    #: on it, per-stage counts included, silently collapses the retry.
    attempt_id: Optional[str] = None
    detail: str = ""
    #: Per-field differences. Plain ``(field, recorded, produced)`` triples are
    #: accepted and normalized to :class:`FieldDiff` here, so every caller --
    #: including the two that predate the deltas -- lands on one shape.
    field_diffs: Sequence[FieldDiff] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "field_diffs", tuple(FieldDiff.coerce(d) for d in self.field_diffs))

    @classmethod
    def for_analysis(cls, analysis: Any, status: str, detail: str = "",
                     field_diffs: Sequence[Any] = ()) -> "AnalysisResult":
        """Build a row from the recorded :class:`AnalysisRecord` it is about.

        ONE builder for all three capabilities. The three modules each had their
        own copy of this mapping, so a field added to the row (or a record field
        renamed) had to be found in three places -- and a row that named the wrong
        analysis is the one defect a coverage report cannot survive.
        """
        return cls(
            analysis_id=analysis.analysis_id,
            expert_class=analysis.expert_class,
            symbol=analysis.symbol,
            use_case=analysis.use_case,
            recorded_outcome=analysis.outcome,
            status=status,
            attempt_id=getattr(analysis, "attempt_id", None),
            detail=detail,
            field_diffs=tuple(field_diffs),
        )

    @property
    def row_id(self) -> str:
        """What identifies this ROW: the attempt when there is one, else the analysis."""
        return self.attempt_id or self.analysis_id

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "expert_class": self.expert_class,
            "symbol": self.symbol,
            "use_case": self.use_case,
            "recorded_outcome": self.recorded_outcome,
            "status": self.status,
            "attempt_id": self.attempt_id,
            "detail": self.detail,
            "field_diffs": [diff.to_mapping() for diff in self.field_diffs],
        }

    def coverage_entry(self, session_id: str, capability: str) -> CoverageEntry:
        return CoverageEntry(
            session_id=session_id,
            analysis_id=self.analysis_id,
            capability=capability,
            status=self.status,
            detail=self.detail or None,
        )


@dataclass
class ReplayReport:
    """The result of running ONE capability over ONE session bundle."""

    session_id: str
    bundle_dir: str
    capability: str
    results: List[AnalysisResult] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: Optional PER-STAGE status for each result, as ``{stage: {analysis_id: status}}``.
    #: A capability that fills more than one stage must say what happened at EACH of
    #: them: rolling one analysis-level status into both rows reports "difference" on
    #: Expert inputs for an analysis whose inputs matched and whose recommendation
    #: moved -- which points a reader at the wrong stage. Stages not listed here fall
    #: back to the analysis-level roll-up, which is exactly right for a capability
    #: that fills only one stage.
    stage_results: Dict[str, Dict[str, str]] = field(default_factory=dict)
    #: Capability-specific evidence merged into the JSON report, keyed by the
    #: capability name (e.g. ``{"historical": {"cache_root": ..., "isolation": ...}}``).
    #: A run's conditions ARE part of its result -- which cache root answered, and
    #: whether anything tried to leave the machine -- and a reader that has only the
    #: report must be able to see them. Refused if it would overwrite a standard key.
    extra: Dict[str, Any] = field(default_factory=dict)
    #: ``CodeComparison.to_mapping()`` of recorded vs running code, set by
    #: :meth:`apply_code_comparison`. ``None`` = the comparison was not made.
    code_provenance: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------- accessors
    @property
    def total(self) -> int:
        return len(self.results)

    def counts(self) -> Dict[str, int]:
        """Every status in :data:`STATUS_ORDER`, zeros included."""
        out = {status: 0 for status in STATUS_ORDER}
        for result in self.results:
            if result.status not in out:
                raise ValueError(f"unknown replay status {result.status!r}")
            out[result.status] += 1
        return out

    def apply_code_comparison(self, comparison: Any) -> None:
        """Record recorded-vs-running code, and relabel results when the code DIFFERS.

        When the running code is provably not the recorded code, a ``difference`` cannot be
        told from drift, so it is reported as ``code_drift`` -- a distinct status, never merged
        into ``difference`` (a real parity break on identical code).
        """
        self.code_provenance = comparison.to_mapping()
        if not comparison.drift:
            return
        drift = ReplayStatus.COVERAGE_CODE_DRIFT
        note = "code drift: " + "; ".join(comparison.reasons)
        self.results = [
            replace(r, status=drift, detail=(f"{r.detail} [{note}]" if r.detail else note))
            if r.status == ReplayStatus.COVERAGE_DIFFERENCE else r
            for r in self.results
        ]
        for per_stage in self.stage_results.values():
            for key, status in per_stage.items():
                if status == ReplayStatus.COVERAGE_DIFFERENCE:
                    per_stage[key] = drift

    def by_status(self, status: str) -> List[AnalysisResult]:
        return [r for r in self.results if r.status == status]

    def coverage_entries(self) -> List[CoverageEntry]:
        return [r.coverage_entry(self.session_id, self.capability) for r in self.results]

    # ------------------------------------------------------------- rendering
    def header_lines(self) -> List[str]:
        """What ran, what did not, and what a match here does NOT mean."""
        not_run = [
            CAPABILITY_TITLES[c]
            for c in (ReplayStatus.CAPABILITY_RECORDED_EXPERT,
                      ReplayStatus.CAPABILITY_GATHER_TAPE,
                      ReplayStatus.CAPABILITY_HISTORICAL,
                      ReplayStatus.CAPABILITY_DECISION)
            if c != self.capability
        ]
        if self.capability == CAPABILITY_OPTION_SELECTION:
            not_run.append(HISTORICAL_PROVIDER_TITLE)
        if self.capability == ReplayStatus.CAPABILITY_DECISION:
            not_run.append(DECISION_NOT_RUN)
        title = (OPTION_CAPABILITY_TITLE if self.capability == CAPABILITY_OPTION_SELECTION
                 else CAPABILITY_TITLES[self.capability])
        caveat = (OPTION_CAVEAT if self.capability == CAPABILITY_OPTION_SELECTION
                  else CAPABILITY_CAVEATS[self.capability])
        return [
            f"**Capability run:** {title}.",
            "",
            "**Capabilities NOT run:** " + "; ".join(not_run) + ".",
            "",
            caveat,
            "",
            "A `match` in one capability says nothing about another.",
        ]

    def stage_rows(self) -> List[Tuple[str, str, str, Tuple[str, ...]]]:
        """``(stage, compared fields, status, capabilities)`` -- ONE rendering.

        The markdown table and the JSON both read this, so the two cannot end up
        saying different things about the same stage.
        """
        out: List[Tuple[str, str, str, Tuple[str, ...]]] = []
        rows = STAGE_ROWS + (OPTION_STAGE_ROWS
                             if self.capability == CAPABILITY_OPTION_SELECTION else ())
        if self.capability == ReplayStatus.CAPABILITY_DECISION:
            rows = rows + DECISION_STAGE_ROWS
        for stage, fields_text, capabilities in rows:
            if self.capability not in capabilities:
                out.append((stage, fields_text, ReplayStatus.COVERAGE_NOT_RUN, capabilities))
                continue
            out.append((stage, fields_text, self._stage_summary(stage), capabilities))
        return out

    def _stage_summary(self, stage: str) -> str:
        """``match 3, difference 1`` for one stage, from its own counts when it has them."""
        per_stage = self.stage_results.get(stage)
        if per_stage is None:
            counts = self.counts()
        else:
            if len(per_stage) != self.total:
                # "Include HOLD/skipped/failed analyses in totals; never report 100%
                # by dropping unavailable rows." A stage that answers for fewer
                # analyses than the report holds is a shrunken total, not a stage.
                raise ValueError(
                    f"stage {stage!r} carries {len(per_stage)} rows for {self.total} "
                    f"analyses; every analysis must be represented at every stage the "
                    f"capability fills")
            counts = {status: 0 for status in STATUS_ORDER}
            for status in per_stage.values():
                if status not in counts:
                    raise ValueError(f"unknown replay status {status!r} for stage {stage!r}")
                counts[status] += 1
        summary = ", ".join(f"{name} {counts[name]}"
                            for name in STATUS_ORDER if counts[name])
        return summary or ReplayStatus.COVERAGE_NOT_RUN

    def to_markdown(self) -> str:
        counts = self.counts()
        lines: List[str] = [
            f"# Replay report -- {self.capability}",
            "",
            f"Session: `{self.session_id}`  ",
            f"Bundle: `{self.bundle_dir}`  ",
            f"Generated: {self.generated_at.isoformat()}",
            "",
        ]
        if self.code_provenance is not None:
            lines += [f"**Code:** {self.code_provenance['line']}", ""]
            if self.code_provenance["drift"]:
                lines += ["**CODE DRIFT:** " + "; ".join(self.code_provenance["reasons"])
                          + ". Results that differ are labelled `code_drift`, not "
                          "`difference`: they cannot be told from a parity break "
                          "until replayed on the recorded code "
                          "(`ba2-test replay checkout`).", ""]
        lines += self.header_lines()
        lines += ["", "## Totals", "", "| Status | Analyses |", "|---|---:|"]
        for status in STATUS_ORDER:
            lines.append(f"| {status} | {counts[status]} |")
        lines.append(f"| **total** | **{self.total}** |")

        lines += ["", "## Stages (spec section 8)", "",
                  "| Stage | Compared fields | Status |", "|---|---|---|"]
        for stage, fields_text, status, _capabilities in self.stage_rows():
            lines.append(f"| {stage} | {fields_text} | {status} |")

        lines += ["", "## Analyses", "",
                  "| Analysis | Expert | Symbol | Use case | Recorded outcome | Status | Detail |",
                  "|---|---|---|---|---|---|---|"]
        for result in self.results:
            lines.append(
                f"| {result.analysis_id} | {result.expert_class} | {result.symbol} | "
                f"{result.use_case} | {result.recorded_outcome} | {result.status} | "
                f"{_cell(result.detail)} |")

        differences = (self.by_status(ReplayStatus.COVERAGE_DIFFERENCE)
                       + self.by_status(ReplayStatus.COVERAGE_CODE_DRIFT))
        if differences:
            lines += ["", "## Field differences", ""]
            for result in differences:
                lines.append(
                    f"### {result.analysis_id} -- {result.expert_class}/{result.symbol}")
                lines += ["", "| Field | Recorded | Produced | Delta |",
                          "|---|---|---|---|"]
                for diff in result.field_diffs:
                    lines.append(f"| {diff.field} | {_cell(diff.recorded)} | "
                                 f"{_cell(diff.produced)} | {_cell(diff.delta_text())} |")
                lines.append("")
        return "\n".join(lines) + "\n"

    def to_mapping(self) -> Dict[str, Any]:
        payload = {
            # /2: a stage row carries ``capabilities`` (a list) where /1 carried a
            # single ``capability``, and a field diff carries the numeric deltas.
            # A consumer keys on this to know which shape it was handed.
            "schema": "ba2_replay_report/2",
            "capability": self.capability,
            "session_id": self.session_id,
            "bundle_dir": self.bundle_dir,
            "generated_at": self.generated_at.isoformat(),
            "total": self.total,
            "counts": self.counts(),
            "capabilities_not_run": [
                c for c in CAPABILITY_TITLES if c != self.capability
            ] + (["historical_provider"]
                 if self.capability == CAPABILITY_OPTION_SELECTION else []),
            "stages": [
                {
                    "stage": stage,
                    "compared_fields": fields_text,
                    "capabilities": list(capabilities),
                    "status": status,
                }
                for stage, fields_text, status, capabilities in self.stage_rows()
            ],
            "results": [r.to_mapping() for r in self.results],
        }
        if self.code_provenance is not None:
            payload["code"] = self.code_provenance
        overlap = sorted(set(self.extra) & set(payload))
        if overlap:
            raise ValueError(
                f"the {self.capability} report's extra evidence would overwrite the "
                f"standard report key(s) {overlap}")
        payload.update(self.extra)
        return payload

    def write(self, out_dir) -> Path:
        """Write ``<capability>.md`` and ``<capability>.json``; return the directory."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{self.capability}.md").write_text(self.to_markdown(), encoding="utf-8")
        (out / f"{self.capability}.json").write_text(
            json.dumps(self.to_mapping(), indent=2, allow_nan=False, ensure_ascii=False),
            encoding="utf-8")
        return out


def merge_coverage(bundle_dir, report: "ReplayReport") -> Path:
    """Write this run's coverage rows back into the bundle's ``coverage.json``.

    Running a capability IS a coverage fact about the session, so it belongs with
    the session and not only in a report directory the next command will not
    read. Without this, ``replay inventory`` still says ``not_run`` for a
    capability that has just been run, and the two commands disagree about the
    same bundle.

    Rows are replaced per ``(capability, analysis_id)``, never wholesale: the
    capture-time rows (the ``missing_capture`` a degraded recording already
    wrote) survive for any analysis this run did not cover.
    """
    path = Path(bundle_dir) / COVERAGE_NAME
    existing: List[Dict[str, Any]] = []
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing = list(payload["entries"])
    replaced = {(report.capability, result.analysis_id) for result in report.results}
    kept = [row for row in existing
            if (row["capability"], row["analysis_id"]) not in replaced]
    merged = kept + [entry.to_mapping() for entry in report.coverage_entries()]
    path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION,
                    "session_id": report.session_id,
                    "entries": merged},
                   indent=2, allow_nan=False, ensure_ascii=False),
        encoding="utf-8")
    return path


#: Longest cell the markdown table renders before saying so.
_CELL_LIMIT = 300


def _cell(text: Any) -> str:
    """One markdown table cell: no pipes, no newlines, bounded length.

    A truncated cell SAYS it was truncated. A silently clipped diff reads like a
    complete value that happens to end oddly; the JSON beside it always carries
    the whole thing.
    """
    value = "" if text is None else str(text)
    value = value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    if len(value) <= _CELL_LIMIT:
        return value
    return (f"{value[:_CELL_LIMIT]}... (truncated, {len(value)} chars; "
            f"the full value is in the JSON report)")
