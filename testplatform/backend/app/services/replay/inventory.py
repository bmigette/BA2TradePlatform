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
from ba2_common.core.replay.decision_record import is_decision_pass

from app.services.replay.report import CAPABILITY_TITLES, STATUS_ORDER

__all__ = ["run", "to_markdown", "INVENTORY_NAME"]

INVENTORY_NAME = "inventory.json"

#: ``analyses_by_record_kind`` keys: an expert analysis vs a stock decision pass.
RECORD_KIND_EXPERT = "expert_analysis"
RECORD_KIND_DECISION_PASS = "decision_pass"

#: Every capability the report knows about, so one with no rows still appears as
#: ``not_run`` instead of dropping out of the table.
_CAPABILITIES = tuple(CAPABILITY_TITLES)


def run(bundle_dir, out_dir=None) -> Dict[str, Any]:
    """Count everything in ``bundle_dir``; write ``inventory.json`` when asked."""
    bundle = load_bundle(bundle_dir)
    analyses = bundle.analyses
    # A stock decision pass is NOT an expert analysis (it carries the owning expert's class only as
    # its owner): it is counted by record kind, never under ``analyses_by_expert``.
    decision_passes = [a for a in analyses if is_decision_pass(a)]
    expert_analyses = [a for a in analyses if not is_decision_pass(a)]

    by_record_kind = Counter(RECORD_KIND_DECISION_PASS if is_decision_pass(a) else RECORD_KIND_EXPERT
                             for a in analyses)
    by_expert = Counter(a.expert_class for a in expert_analyses)
    by_use_case = Counter(a.use_case for a in analyses)
    by_outcome = Counter(a.outcome for a in analyses)
    by_capture_status = Counter(a.bundle_capture_status for a in analyses)
    by_expert_outcome = Counter((a.expert_class, a.outcome) for a in expert_analyses)
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
        # The decision capability answers for decision passes only, the expert capabilities for expert
        # analyses only.
        applicable = (decision_passes if capability == ReplayStatus.CAPABILITY_DECISION
                      else expert_analyses)
        rows[ReplayStatus.COVERAGE_NOT_RUN] += sum(
            1 for a in applicable if a.analysis_id not in seen)
        coverage[capability] = rows

    capture_gaps = Counter(gap for a in analyses for gap in a.capture_gaps)
    capture_failures: Counter = Counter()
    for analysis in analyses:
        capture_failures.update(analysis.capture_failures)

    option_actions = _option_actions(bundle)

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
        "analyses_by_record_kind": dict(sorted(by_record_kind.items())),
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
        # Separate from the stock capabilities above, which keep their exact set.
        "option_actions": option_actions,
        "decision_passes": _decision_passes(bundle, decision_passes),
        "decision_coverage": _decision_coverage(bundle, decision_passes),
    }
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / INVENTORY_NAME).write_text(
            json.dumps(inventory, indent=2, allow_nan=False, ensure_ascii=False),
            encoding="utf-8")
        (out / "inventory.md").write_text(to_markdown(inventory), encoding="utf-8")
    return inventory


def _decision_passes(bundle, passes) -> Dict[str, Any]:
    """Recorded stock decision passes, counted by pass kind, outcome and recorded step kind.

    Replayed by the ``decision`` capability (``replay decision``); its coverage rows are in
    ``coverage_by_capability``.
    """
    by_pass: Counter = Counter()
    by_outcome: Counter = Counter()
    steps: Counter = Counter()
    for analysis in passes:
        by_pass[analysis.branch_flags["pass"] if "pass" in analysis.branch_flags else "unknown"] += 1
        by_outcome[analysis.outcome] += 1
        for observation in bundle.observations_for(analysis.analysis_id):
            if observation.provider == "decision_step":
                steps[observation.method] += 1
    return {
        "total": len(passes),
        "by_pass": dict(sorted(by_pass.items())),
        "by_outcome": dict(sorted(by_outcome.items())),
        "steps_by_kind": dict(sorted(steps.items())),
        "replayed_by": ReplayStatus.CAPABILITY_DECISION,
        "replay_command": "ba2-test replay decision --bundle <dir>",
    }


def _decision_coverage(bundle, passes) -> Dict[str, Any]:
    """Per expert: passes recorded / discarded / not recorded / failed, and the recorder's own health.

    * recorded -- the pass ended as a decision or as a recorded decision not to act.
    * not_recorded -- the pass RAN but its scope could not open (an error row carrying the
      ``pass_not_recorded`` failure); the live recorder counted it too.
    * failed -- the pass ended in any other error.
    * discarded -- from ``capabilities.capture_health`` (written at finalize): passes that ended before
      they were a decision (lock miss, pending analysis jobs). ``None`` when the session carries no
      health record (an older session), which is "unknown", never zero.
    """
    health = bundle.session.capabilities["capture_health"] if "capture_health" in bundle.session.capabilities else None
    discards_by_expert = dict(health["discards_by_expert"]) if health is not None else None
    experts: Dict[str, Dict[str, Any]] = {}

    def row(expert):
        return experts.setdefault(expert, {"recorded": 0, "discarded": None if health is None else 0,
                                           "not_recorded": 0, "failed": 0})

    for analysis in passes:
        entry = row(analysis.expert_class)
        if analysis.outcome in (ReplayStatus.OUTCOME_DECISION, ReplayStatus.OUTCOME_SKIP):
            entry["recorded"] += 1
        elif "pass_not_recorded" in analysis.capture_failures:
            entry["not_recorded"] += 1
        else:
            entry["failed"] += 1
    for expert, count in (discards_by_expert or {}).items():
        row(expert)["discarded"] = count

    totals = {key: sum(e[key] for e in experts.values() if e[key] is not None)
              for key in ("recorded", "discarded", "not_recorded", "failed")}
    counters = dict(health["counters"]) if health is not None else {}
    failures = {k: v for k, v in counters.items() if k != "pass_discarded" and v}
    return {
        "by_expert": dict(sorted(experts.items())),
        "totals": totals,
        "health_recorded": health is not None,
        "discards_by_reason": dict(sorted(health["discards"].items())) if health is not None else {},
        "capture_failures": sum(failures.values()),
        "capture_failure_kinds": dict(sorted(failures.items())),
    }


def _option_actions(bundle) -> Dict[str, Any]:
    """Recorded option entry / close actions, counted per action class and outcome.

    Replayed by ``option_selection`` (``replay option-selection``); its coverage rows are counted
    here because the stock capability table above deliberately does not list it.
    """
    from ba2_common.core.replay import option_action_record as oar

    from app.services.replay.report import CAPABILITY_OPTION_SELECTION

    by_class_outcome: Counter = Counter()
    for observation in bundle.observations:
        if observation.provider != oar.OPTION_ACTION_PROVIDER:
            continue
        if observation.payload_object is None:
            label = "payload_missing"
        else:
            try:
                outcome = bundle.decode(observation.payload_object)["outcome"]
            except Exception:  # noqa: BLE001 -- one bad payload is a counted gap, not an abort
                label = "undecodable"
            else:
                if outcome["status"] == "error":
                    label = "error"
                else:
                    label = "submitted" if outcome["success"] else "refused"
        by_class_outcome[f"{observation.method}/{label}"] += 1
    rows = {status: 0 for status in STATUS_ORDER}
    for entry in bundle.coverage:
        if entry.capability == CAPABILITY_OPTION_SELECTION:
            rows[entry.status] += 1
    return {
        "total": sum(by_class_outcome.values()),
        "by_class_and_outcome": dict(sorted(by_class_outcome.items())),
        "replayed_by": CAPABILITY_OPTION_SELECTION,
        "replay_command": "ba2-test replay option-selection --bundle <dir>",
        "coverage": rows,
    }


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
    banner = _banner(inventory["decision_coverage"])
    if banner:
        lines = [banner, ""] + lines
    lines += _decision_coverage_markdown(inventory["decision_coverage"])
    lines += _table("Analyses by record kind", "Record kind", inventory["analyses_by_record_kind"])
    lines += _table("Analyses by expert (expert analyses only)", "Expert", inventory["analyses_by_expert"])
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
    decisions = inventory["decision_passes"]
    lines += ["", "## Decision passes", "",
              f"{decisions['total']} recorded stock decision pass(es), replayed by the "
              f"`{decisions['replayed_by']}` capability (`{decisions['replay_command']}`)."]
    lines += _table("Decision passes by kind", "Pass", decisions["by_pass"])
    lines += _table("Decision passes by outcome", "Outcome", decisions["by_outcome"])
    lines += _table("Recorded decision steps", "Step", decisions["steps_by_kind"])
    options = inventory["option_actions"]
    lines += ["", "## Option actions", "",
              f"{options['total']} recorded option entry / close action(s), replayed by the "
              f"`{options['replayed_by']}` capability (`{options['replay_command']}`)."]
    lines += _table("Option actions by class and outcome", "Action / outcome",
                    options["by_class_and_outcome"])
    lines += ["", "| " + " | ".join(STATUS_ORDER) + " |",
              "|---" * len(STATUS_ORDER) + "|",
              "| " + " | ".join(str(options["coverage"][s]) for s in STATUS_ORDER) + " |"]
    return "\n".join(lines) + "\n"


def _banner(coverage: Dict[str, Any]) -> str:
    if not coverage["capture_failures"]:
        return ""
    kinds = ", ".join(f"{k}={v}" for k, v in coverage["capture_failure_kinds"].items())
    return (f"**CAPTURE FAILURES: {coverage['capture_failures']} recording failure(s) in this session "
            f"({kinds}). Coverage below is incomplete.**")


def _decision_coverage_markdown(coverage: Dict[str, Any]) -> List[str]:
    lines = ["", "## Decision coverage", ""]
    if not coverage["health_recorded"]:
        lines += ["_no capture_health in this session: discarded passes and recorder failures are "
                  "unknown (shown as `-`), not zero._", ""]
    lines += ["| Expert | Recorded | Discarded | Not recorded | Failed |", "|---|---:|---:|---:|---:|"]
    for expert, e in coverage["by_expert"].items():
        discarded = "-" if e["discarded"] is None else e["discarded"]
        lines.append(f"| {expert} | {e['recorded']} | {discarded} | {e['not_recorded']} | {e['failed']} |")
    t = coverage["totals"]
    lines.append(f"| **all** | {t['recorded']} | {t['discarded']} | {t['not_recorded']} | {t['failed']} |")
    if coverage["discards_by_reason"]:
        lines += ["", "Discards by reason: " + ", ".join(
            f"{k}={v}" for k, v in coverage["discards_by_reason"].items())]
    return lines


def _table(title: str, key_header: str, rows: Dict[str, int]) -> List[str]:
    if not rows:
        return ["", f"## {title}", "", "_none recorded_"]
    out = ["", f"## {title}", "", f"| {key_header} | Count |", "|---|---:|"]
    out += [f"| {key} | {value} |" for key, value in rows.items()]
    return out
