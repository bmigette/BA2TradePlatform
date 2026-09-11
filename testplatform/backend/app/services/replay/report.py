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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from ba2_common.core.replay import CoverageEntry, ReplayStatus
from ba2_common.core.replay.schemas import SCHEMA_VERSION
from ba2_common.core.replay.service import COVERAGE_NAME

__all__ = [
    "AnalysisResult",
    "ReplayReport",
    "merge_coverage",
    "STAGE_ROWS",
    "CAPABILITY_CAVEATS",
    "CAPABILITY_TITLES",
    "STATUS_ORDER",
]

#: Every status the report can carry, in the order it is tabulated. Fixed rather
#: than derived from the data, so a status with zero rows shows as 0 instead of
#: vanishing from the table.
STATUS_ORDER: Tuple[str, ...] = (
    ReplayStatus.COVERAGE_MATCH,
    ReplayStatus.COVERAGE_DIFFERENCE,
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
STAGE_ROWS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("Selection",
     "Candidate coverage, filters, ranked order, selected/held symbols",
     ()),
    ("Expert inputs",
     "Normalized _gather bundle rebuilt from the recorded provider returns "
     "(gather tape) or from a pinned historical cache root (historical)",
     (ReplayStatus.CAPABILITY_GATHER_TAPE, ReplayStatus.CAPABILITY_HISTORICAL)),
    ("Recommendation",
     "Skip reason, signal, confidence, expected profit, current price, target",
     (ReplayStatus.CAPABILITY_RECORDED_EXPERT, ReplayStatus.CAPABILITY_HISTORICAL)),
    ("Rules and sizing",
     "Branch, eligibility, operands/budget, intended side/quantity, TP/SL",
     ()),
    ("Execution",
     "Submit attempts/rejections, partial/full fills, final active protection",
     ()),
)


@dataclass(frozen=True)
class AnalysisResult:
    """One analysis, one capability: what happened and why."""

    analysis_id: str
    expert_class: str
    symbol: str
    use_case: str
    recorded_outcome: str
    status: str
    detail: str = ""
    #: Per-field differences as ``(field, recorded, produced)`` rendered text.
    field_diffs: Sequence[Tuple[str, str, str]] = ()

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "expert_class": self.expert_class,
            "symbol": self.symbol,
            "use_case": self.use_case,
            "recorded_outcome": self.recorded_outcome,
            "status": self.status,
            "detail": self.detail,
            "field_diffs": [
                {"field": name, "recorded": recorded, "produced": produced}
                for name, recorded, produced in self.field_diffs
            ],
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
    #: Capability-specific evidence merged into the JSON report, keyed by the
    #: capability name (e.g. ``{"historical": {"cache_root": ..., "isolation": ...}}``).
    #: A run's conditions ARE part of its result -- which cache root answered, and
    #: whether anything tried to leave the machine -- and a reader that has only the
    #: report must be able to see them. Refused if it would overwrite a standard key.
    extra: Dict[str, Any] = field(default_factory=dict)

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
        return [
            f"**Capability run:** {CAPABILITY_TITLES[self.capability]}.",
            "",
            "**Capabilities NOT run:** " + "; ".join(not_run) + ".",
            "",
            CAPABILITY_CAVEATS[self.capability],
            "",
            "A `match` in one capability says nothing about another.",
        ]

    def stage_rows(self) -> List[Tuple[str, str, str, Tuple[str, ...]]]:
        """``(stage, compared fields, status, capabilities)`` -- ONE rendering.

        The markdown table and the JSON both read this, so the two cannot end up
        saying different things about the same stage.
        """
        counts = self.counts()
        summary = ", ".join(f"{name} {counts[name]}"
                            for name in STATUS_ORDER if counts[name])
        out: List[Tuple[str, str, str, Tuple[str, ...]]] = []
        for stage, fields_text, capabilities in STAGE_ROWS:
            status = ((summary or ReplayStatus.COVERAGE_NOT_RUN)
                      if self.capability in capabilities
                      else ReplayStatus.COVERAGE_NOT_RUN)
            out.append((stage, fields_text, status, capabilities))
        return out

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

        differences = self.by_status(ReplayStatus.COVERAGE_DIFFERENCE)
        if differences:
            lines += ["", "## Field differences", ""]
            for result in differences:
                lines.append(
                    f"### {result.analysis_id} -- {result.expert_class}/{result.symbol}")
                lines += ["", "| Field | Recorded | Produced |", "|---|---|---|"]
                for name, recorded, produced in result.field_diffs:
                    lines.append(f"| {name} | {_cell(recorded)} | {_cell(produced)} |")
                lines.append("")
        return "\n".join(lines) + "\n"

    def to_mapping(self) -> Dict[str, Any]:
        payload = {
            "schema": "ba2_replay_report/1",
            "capability": self.capability,
            "session_id": self.session_id,
            "bundle_dir": self.bundle_dir,
            "generated_at": self.generated_at.isoformat(),
            "total": self.total,
            "counts": self.counts(),
            "capabilities_not_run": [
                c for c in CAPABILITY_TITLES if c != self.capability
            ],
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
