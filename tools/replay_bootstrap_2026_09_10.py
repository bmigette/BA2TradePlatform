"""Turn the September 10 production read-out into a PARTIAL replay session.

Spec section 11: "Use September 10's saved recommendations, rules and selections
to bootstrap fixtures and known gaps. Label that session partial."

September 10 predates live capture, so what exists is a read-only export of the
trading database (`reports/trading/replay_2026-09-10/live_inputs.json`, NOT in
git -- it holds production rows). That file contains what live PRODUCED -- the
``MarketAnalysis`` rows, the ``ExpertRecommendation`` rows and the persisted
analysis outputs -- and, for FMPRating only, the two consensus payloads the
analysis actually consumed, because that expert persists them as outputs.

What it does NOT contain is a normalized ``_gather`` bundle for any analysis.
"Missing original observations cannot be recovered merely by fetching data after
the fact." (spec section 1) So every analysis this tool writes carries
``bundle_capture_status="not_attempted"``, and ``ba2-test replay experts`` on the
result reports every one of them as ``missing_capture`` -- which is the honest
answer, and exactly the thing this bootstrap exists to make visible.

The FMPRating payloads are attached as observations with ``provenance="unknown"``:
they were read out of a database row long after the fetch, so nothing here knows
whether the live call hit the network or a memo, and file/row timestamps are not
publication times.

Usage::

    python tools/replay_bootstrap_2026_09_10.py \
        --live-inputs .../reports/trading/replay_2026-09-10/live_inputs.json \
        --out .../replay_2026-09-10_bundle
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.replay import (
    AnalysisRecord,
    PendingObservation,
    ProviderObservation,
    ReplayStatus,
    ReplayStore,
    SessionRecord,
)
from ba2_common.core.replay.context import payload_kind_of, response_class_of
from ba2_common.core.types import OrderRecommendation, Recommendation

SESSION_ID = "sep10-bootstrap"

#: The analysis-output types FMPRating persists that ARE the provider payloads it
#: consumed, and the tapped method each one corresponds to. Nothing else in the
#: export is a provider return: the other rows are rendered text, not responses.
_OUTPUT_OBSERVATIONS: Tuple[Tuple[str, str, str], ...] = (
    ("fmp_consensus_response", "fmp", "price_target_consensus"),
    ("fmp_upgrade_downgrade", "fmp", "upgrade_downgrade_consensus"),
)


def build_bootstrap(live_inputs_path, export_dir, store_root=None) -> Path:
    """Write a partial session export for ``live_inputs.json``; return its directory."""
    payload = json.loads(Path(live_inputs_path).read_text(encoding="utf-8"))
    experts = {row["id"]: row["expert"] for row in payload["experts"]}
    recommendations: Dict[int, Dict[str, Any]] = {
        row["market_analysis_id"]: row for row in payload["recommendations"]}
    outputs_by_analysis: Dict[int, List[Dict[str, Any]]] = {}
    for row in payload["analysis_outputs"]:
        outputs_by_analysis.setdefault(row["market_analysis_id"], []).append(row)

    temporary = None
    if store_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="replay-bootstrap-")
        store_root = temporary.name
    store = ReplayStore(store_root, writer="sync")
    try:
        store.begin_session(_session(payload))
        for row in payload["analyses"]:
            record, objects, observations = _analysis(payload, row, experts,
                                                      recommendations, outputs_by_analysis)
            store.submit(record, objects=objects, observations=observations)
        exported = store.export_session(SESSION_ID, export_dir)
    finally:
        store.close(timeout=30.0)
        if temporary is not None:
            temporary.cleanup()
    return exported


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
def _session(payload: Dict[str, Any]) -> SessionRecord:
    return SessionRecord(
        session_id=SESSION_ID,
        instance_id=f"prod-{payload['day']}",
        started_at=_utc(payload["captured_at_utc"]),
        exchange_tz="America/New_York",
        # The read-out predates capture, so the deployed versions and the source
        # revision were never recorded. "Unknown timestamps remain unknown" --
        # and so do unknown versions: nothing here is invented.
        app_version="unrecorded",
        package_versions={},
        source_revision=None,
        dirty=False,
        status=ReplayStatus.SESSION_FINALIZED,
        capabilities={
            "source": "reports/trading/replay_2026-09-10/live_inputs.json",
            "partial": True,
            "normalized_bundles": False,
            "clock_reads": False,
            "provider_observations": "FMPRating consensus/upgrade payloads only",
        },
    )


def _analysis(payload: Dict[str, Any], row: Dict[str, Any], experts: Dict[int, str],
              recommendations: Dict[int, Dict[str, Any]],
              outputs_by_analysis: Dict[int, List[Dict[str, Any]]]):
    analysis_id = str(row["id"])
    instance_id = row["expert_instance_id"]
    expert_class = experts[instance_id]
    created_at = _utc(row["created_at"])
    recommendation_row = recommendations.get(row["id"])

    objects: Dict[str, Any] = {}
    if recommendation_row is not None:
        objects["recommendation"] = _recommendation(recommendation_row)
        outcome = ReplayStatus.OUTCOME_RECOMMENDATION
        skip_reason = None
        error = None
    elif row["status"] == "SKIPPED":
        outcome = ReplayStatus.OUTCOME_SKIP
        skip_reason = _skip_reason(row)
        error = None
    else:
        outcome = ReplayStatus.OUTCOME_ERROR
        skip_reason = None
        error = f"status={row['status']} with no persisted recommendation"

    observations = _observations(analysis_id, row, expert_class,
                                 outputs_by_analysis.get(row["id"], []))
    record = AnalysisRecord(
        analysis_id=analysis_id,
        attempt_id=analysis_id,
        session_id=SESSION_ID,
        expert_class=expert_class,
        expert_instance_id=instance_id,
        symbol=row["symbol"],
        use_case=str(row["subtype"]).lower(),
        scheduled_at=created_at,
        started_at=created_at,
        finished_at=None,
        # The whole point of the bootstrap: there is no normalized bundle, and no
        # later download can produce one.
        bundle_capture_status=ReplayStatus.CAPTURE_NOT_ATTEMPTED,
        clock_reads=(),
        outcome=outcome,
        skip_reason=skip_reason,
        error=error,
        observation_ids=[o.observation.observation_id for o in observations],
        branch_flags={"bootstrap": True,
                      "source": "live_inputs.json",
                      "day": payload["day"]},
    )
    return record, objects, observations


def _recommendation(row: Dict[str, Any]) -> Recommendation:
    """The recommendation live PRODUCED, rebuilt from its persisted row.

    Stored as an expected OUTPUT only: "Expected recommendations and order outputs
    are stored separately and must not be fed back as replay inputs." (spec s3)
    """
    return Recommendation(
        signal=OrderRecommendation[row["recommended_action"]],
        confidence=row["confidence"],
        current_price=row["price_at_date"],
        details=row["details"],
        expected_profit_percent=row["expected_profit_percent"],
        target_price=row["target_price"],
        raw_outputs=_json_or_empty(row["data"]),
        skip=False,
        skip_reason=None,
    )


def _observations(analysis_id: str, row: Dict[str, Any], expert_class: str,
                  outputs: List[Dict[str, Any]]) -> List[PendingObservation]:
    """The FMPRating payloads the analysis consumed, as recorded observations."""
    by_type = {output["type"]: output for output in outputs}
    out: List[PendingObservation] = []
    for output_type, provider, method in _OUTPUT_OBSERVATIONS:
        output = by_type.get(output_type)
        if output is None:
            continue
        payload = json.loads(output["text"])
        observation = ProviderObservation(
            observation_id=f"{SESSION_ID}:{analysis_id}#{len(out):06d}",
            session_id=SESSION_ID,
            analysis_ids=[analysis_id],
            provider=provider,
            method=method,
            request_identity={"symbol": row["symbol"]},
            invocation_seq=len(out),
            payload_kind=payload_kind_of(payload),
            response_class=response_class_of(payload),
            # Every time here is genuinely unknown: the payload was read out of a
            # database row long after the fetch. A row timestamp is not a fetch
            # time and never a publication time (spec section 3).
            fetched_at=None,
            observed_at=None,
            consumed_at=None,
            published_at=None,
            first_observed_at=None,
            provenance=ReplayStatus.PROVENANCE_UNKNOWN,
        )
        out.append(PendingObservation(observation=observation, payload=payload))
    return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _skip_reason(row: Dict[str, Any]) -> str:
    state = _json_or_empty(row["state"])
    reason = state.get("skip_reason") if isinstance(state, dict) else None
    # A skip with no recorded reason is a gap in the SOURCE, said out loud rather
    # than filled in with a guess.
    return str(reason) if reason else "skip reason not recorded on the analysis row"


def _json_or_empty(text: Any) -> Dict[str, Any]:
    if not text:
        return {}
    value = json.loads(text) if isinstance(text, str) else text
    return value if isinstance(value, dict) else {}


def _utc(text: str) -> datetime:
    """Parse a platform timestamp as UTC.

    The trading database stores UTC, naive; the replay schema refuses a naive
    datetime rather than guessing, so the assumption is stated here once instead
    of being made silently in five places.
    """
    value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay_bootstrap_2026_09_10",
        description="Convert the September 10 production read-out into a PARTIAL "
                    "replay session export (no normalized bundles).")
    parser.add_argument("--live-inputs", required=True,
                        help="Path to live_inputs.json (not in git; it holds production rows).")
    parser.add_argument("--out", required=True, help="Directory to write the export into.")
    parser.add_argument("--store", default=None,
                        help="Replay store root (default: a temporary directory).")
    args = parser.parse_args(argv)

    source = Path(args.live_inputs)
    if not source.is_file():
        print(f"replay-bootstrap: {source} does not exist", file=sys.stderr)
        return 2
    exported = build_bootstrap(source, args.out, args.store)
    print(f"replay-bootstrap: partial session written to {exported}")
    print("replay-bootstrap: every analysis is bundle_capture_status=not_attempted; "
          "`ba2-test replay experts` will report them all as missing_capture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
