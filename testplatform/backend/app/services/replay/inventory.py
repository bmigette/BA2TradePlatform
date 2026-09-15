"""What a recorded session actually contains -- before anything is replayed.

"Include HOLD/skipped/failed analyses in totals; never report 100% by dropping
unavailable rows." (spec section 8) So this counts EVERY analysis in the bundle:
by expert, by use case, by outcome (recommendation / skip / error), by
bundle-capture status, plus the provider observations behind them, the
evaluation-clock reads and the per-capability coverage the bundle already
carries.

This is the command that answers "what can this session support?" -- which is
exactly what the September 10 bootstrap needs, where the honest answer is "the
recommendations, and nothing that requires a normalized bundle".
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from ba2_common.core.replay import ReplayStatus, load_bundle

from app.services.replay.report import CAPABILITY_TITLES, STATUS_ORDER

__all__ = ["run", "to_markdown", "INVENTORY_NAME"]

INVENTORY_NAME = "inventory.json"

#: Every capability the report knows about, so one with no rows still appears as
#: ``not_run`` instead of dropping out of the table.
_CAPABILITIES = tuple(CAPABILITY_TITLES)


def run(bundle_dir, out_dir=None) -> Dict[str, Any]:
    """Count everything in ``bundle_dir``; write ``inventory.json`` when asked."""
    bundle = load_bundle(bundle_dir)
    analyses = bundle.analyses

    by_expert = Counter(a.expert_class for a in analyses)
    by_use_case = Counter(a.use_case for a in analyses)
    by_outcome = Counter(a.outcome for a in analyses)
    by_capture_status = Counter(a.bundle_capture_status for a in analyses)
    by_expert_outcome = Counter((a.expert_class, a.outcome) for a in analyses)
    skip_reasons = Counter(a.skip_reason for a in analyses
                           if a.outcome == ReplayStatus.OUTCOME_SKIP)

    observations = Counter(f"{o.provider}.{o.method}" for o in bundle.observations)
    provenance = Counter(o.provenance for o in bundle.observations)
    response_class = Counter(o.response_class for o in bundle.observations)

    clock_reads = [len(a.clock_reads) for a in analyses]
    without_clock = sum(1 for count in clock_reads if count == 0)

    coverage: Dict[str, Dict[str, int]] = {}
    for capability in _CAPABILITIES:
        rows = {status: 0 for status in STATUS_ORDER}
        seen = set()
        for entry in bundle.coverage:
            if entry.capability != capability:
                continue
            rows[entry.status] += 1
            seen.add(entry.analysis_id)
        # An analysis with no coverage row for a capability has not been run for
        # it. Saying so explicitly is what stops a partial run reading as a full
        # one when the totals are divided.
        rows[ReplayStatus.COVERAGE_NOT_RUN] += sum(
            1 for a in analyses if a.analysis_id not in seen)
        coverage[capability] = rows

    capture_gaps = Counter(gap for a in analyses for gap in a.capture_gaps)
    capture_failures: Counter = Counter()
    for analysis in analyses:
        capture_failures.update(analysis.capture_failures)

    inventory: Dict[str, Any] = {
        "schema": "ba2_replay_inventory/1",
        "bundle_dir": str(Path(bundle_dir)),
        "session": {
            "session_id": bundle.session.session_id,
            "instance_id": bundle.session.instance_id,
            "status": bundle.session.status,
            "started_at": bundle.session.started_at.isoformat(),
            "ended_at": (None if bundle.session.ended_at is None
                         else bundle.session.ended_at.isoformat()),
            "app_version": bundle.session.app_version,
            "package_versions": dict(bundle.session.package_versions),
            "source_revision": bundle.session.source_revision,
            "dirty": bundle.session.dirty,
            "exchange_tz": bundle.session.exchange_tz,
        },
        "totals": {
            "analyses": len(analyses),
            "observations": len(bundle.observations),
            "coverage_rows": len(bundle.coverage),
        },
        "analyses_by_expert": dict(sorted(by_expert.items())),
        "analyses_by_use_case": dict(sorted(by_use_case.items())),
        "analyses_by_outcome": dict(sorted(by_outcome.items())),
        "analyses_by_expert_and_outcome": {
            f"{expert}/{outcome}": count
            for (expert, outcome), count in sorted(by_expert_outcome.items())
        },
        "bundle_capture_status": dict(sorted(by_capture_status.items())),
        "skip_reasons": dict(sorted((str(k), v) for k, v in skip_reasons.items())),
        "capture_gaps": dict(sorted(capture_gaps.items())),
        "capture_failures": dict(sorted(capture_failures.items())),
        "observations_by_provider_method": dict(sorted(observations.items())),
        "observations_by_provenance": dict(sorted(provenance.items())),
        "observations_by_response_class": dict(sorted(response_class.items())),
        "clock_reads": {
            "total": sum(clock_reads),
            "analyses_with_reads": len(clock_reads) - without_clock,
            "analyses_without_reads": without_clock,
            "max_per_analysis": max(clock_reads) if clock_reads else 0,
        },
        "coverage_by_capability": coverage,
    }
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / INVENTORY_NAME).write_text(
            json.dumps(inventory, indent=2, allow_nan=False, ensure_ascii=False),
            encoding="utf-8")
        (out / "inventory.md").write_text(to_markdown(inventory), encoding="utf-8")
    return inventory


def to_markdown(inventory: Dict[str, Any]) -> str:
    session = inventory["session"]
    lines: List[str] = [
        "# Replay session inventory",
        "",
        f"Session: `{session['session_id']}`  ",
        f"Instance: `{session['instance_id']}`  ",
        f"Status: **{session['status']}**  ",
        f"App version: {session['app_version']}  ",
        f"Source revision: {session['source_revision']}"
        + (" (dirty)" if session["dirty"] else ""),
        "",
        f"Bundle: `{inventory['bundle_dir']}`",
        "",
        "Every recorded analysis is counted below -- recommendation, skip and error "
        "alike. A capability with no coverage rows is `not_run`, not absent.",
        "",
        f"**{inventory['totals']['analyses']} analyses**, "
        f"{inventory['totals']['observations']} provider observations, "
        f"{inventory['totals']['coverage_rows']} coverage rows.",
    ]
    lines += _table("Analyses by expert", "Expert", inventory["analyses_by_expert"])
    lines += _table("Analyses by use case", "Use case", inventory["analyses_by_use_case"])
    lines += _table("Analyses by outcome", "Outcome", inventory["analyses_by_outcome"])
    lines += _table("Analyses by expert and outcome", "Expert / outcome",
                    inventory["analyses_by_expert_and_outcome"])
    lines += _table("Bundle capture status", "Status", inventory["bundle_capture_status"])
    lines += _table("Skip reasons", "Reason", inventory["skip_reasons"])
    lines += _table("Capture gaps", "Role", inventory["capture_gaps"])
    lines += _table("Capture failures", "Kind", inventory["capture_failures"])
    lines += _table("Observations by provider.method", "Provider.method",
                    inventory["observations_by_provider_method"])
    lines += _table("Observations by provenance", "Provenance",
                    inventory["observations_by_provenance"])
    lines += _table("Observations by response class", "Response class",
                    inventory["observations_by_response_class"])

    clock = inventory["clock_reads"]
    lines += [
        "", "## Evaluation-clock reads", "",
        f"- recorded reads: {clock['total']}",
        f"- analyses that read a clock: {clock['analyses_with_reads']}",
        f"- analyses that read none: {clock['analyses_without_reads']}",
        f"- most reads in one analysis: {clock['max_per_analysis']}",
    ]

    lines += ["", "## Coverage by capability", "",
              "| Capability | " + " | ".join(STATUS_ORDER) + " |",
              "|---" * (len(STATUS_ORDER) + 1) + "|"]
    for capability, rows in inventory["coverage_by_capability"].items():
        counts = " | ".join(str(rows[status]) for status in STATUS_ORDER)
        lines.append(f"| {capability} | {counts} |")
    return "\n".join(lines) + "\n"


def _table(title: str, key_header: str, rows: Dict[str, int]) -> List[str]:
    if not rows:
        return ["", f"## {title}", "", "_none recorded_"]
    out = ["", f"## {title}", "", f"| {key_header} | Count |", "|---|---:|"]
    out += [f"| {key} | {value} |" for key, value in rows.items()]
    return out
