"""Ruleset metadata API (Task A2).

Read-only single-source-of-truth endpoints so the exit-ruleset UI does NOT
hardcode or drift from the backend vocabulary:

  * ``GET /api/ruleset/vocabulary``    -> flags / numerics / operators / actions / reference_values
  * ``GET /api/ruleset/exit-presets``  -> the packaged default exit-rule presets

The vocabulary is derived entirely from ``ba2_common.core.types`` (no DB):
``ExpertEventType`` members split on the member-NAME prefix — ``F_*`` are boolean
flag conditions (no operator/value), ``N_*`` are numeric conditions (operator+value).
``ExpertActionType`` drives the action list; option actions are tagged via
``is_option_action`` and the two adjust actions are flagged ``needs_reference``.

``GET /api/experts/{expert_id}/open-positions-ruleset`` (Task A3, import-from-live) is
GRACEFUL/OPTIONAL: when the env var ``BA2_LIVE_DB`` points at the live BA2TradePlatform
sqlite, it reads that expert's ``open_positions`` ruleset READ-ONLY and converts each live
``EventAction`` into an ``ExitCondition``-shaped rule the UI can load (marked optimizable).
When ``BA2_LIVE_DB`` is unset or the DB is unreachable, it returns 503 so the UI falls back
to JSON-paste import. The read uses a dedicated ``mode=ro`` sqlite connection with raw SQL —
it never touches ba2_common's shared engine and can never write the live DB.
"""
import json
import logging
import os
import sqlite3

from fastapi import APIRouter, HTTPException

# All trigger/action <-> condition-tree conversion now lives in ba2_common (single source of
# truth). This file keeps only the backtester-specific glue: the live-DB SQL readers, the HTTP
# endpoints, and the vocabulary endpoint.
from ba2_common.core.rule_builders import (
    entry_action_side as _entry_action_side,
    eventaction_to_entry_group as _eventaction_to_entry_group,
    eventaction_to_exit_rule as _eventaction_to_exit_rule,
    groups_to_tree as _groups_to_tree,
    live_export_to_strategy,
)
from ba2_common.core.types import (
    ExpertActionType,
    ExpertEventType,
    get_reference_value_options,
    is_option_action,
)
from pydantic import BaseModel

from app.services.ruleset_presets import EXIT_PRESETS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

OPERATORS = [">", ">=", "<", "<=", "==", "!=", "between"]
_NEEDS_REFERENCE = ("adjust_take_profit", "adjust_stop_loss")


def _label(value: str) -> str:
    """Human label for an enum value, e.g. ``profit_loss_percent`` -> ``Profit Loss Percent``."""
    return value.replace("_", " ").title()


@router.get("/ruleset/vocabulary")
def get_vocabulary():
    """Condition vocabulary, operators, actions, and reference-value options."""
    flags = [
        {"value": m.value, "label": _label(m.value)}
        for m in ExpertEventType
        if m.name.startswith("F_")
    ]
    numerics = [
        {"value": m.value, "label": _label(m.value)}
        for m in ExpertEventType
        if m.name.startswith("N_")
    ]
    actions = [
        {
            "value": m.value,
            "label": _label(m.value),
            "is_option": is_option_action(m.value),
            "needs_reference": m.value in _NEEDS_REFERENCE,
        }
        for m in ExpertActionType
    ]
    return {
        "flags": flags,
        "numerics": numerics,
        "operators": OPERATORS,
        "actions": actions,
        "reference_values": get_reference_value_options(),
    }


@router.get("/ruleset/exit-presets")
def get_exit_presets():
    """The packaged default exit-rule presets (each ``rule`` validates against ExitCondition)."""
    return {"presets": EXIT_PRESETS}


def _expert_ruleset_eas(db_path: str, expert_id: int, ruleset_id_column: str) -> list[dict]:
    """READ-ONLY raw-SQL read of one expert ruleset's ordered EventActions from the live sqlite.

    Opens the DB via a ``mode=ro`` URI (never writes, never touches ba2_common's engine),
    resolves ``expertinstance.<ruleset_id_column>`` -> ordered ``eventaction`` rows, and returns
    each as ``{"id", "name", "triggers": <dict>, "actions": <dict>}`` (JSON already parsed).
    Raises ``HTTPException(404)`` if the expert is absent; returns ``[]`` when the expert has no
    such ruleset configured. Connection errors propagate to the caller (mapped to 503 there).

    ``ruleset_id_column`` is a fixed internal identifier (``open_positions_ruleset_id`` /
    ``enter_market_ruleset_id``), NOT user input, so interpolating it into the SQL is safe.
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {ruleset_id_column} AS ruleset_id FROM expertinstance WHERE id = ?",
            (expert_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"expert {expert_id} not found in live DB")
        ruleset_id = row["ruleset_id"]
        if ruleset_id is None:
            return []  # expert exists but has no such ruleset configured
        ea_rows = conn.execute(
            "SELECT ea.id AS id, ea.name AS name, ea.triggers AS triggers, ea.actions AS actions "
            "FROM eventaction ea "
            "JOIN ruleset_eventaction_link link ON link.eventaction_id = ea.id "
            "WHERE link.ruleset_id = ? "
            "ORDER BY link.order_index",
            (ruleset_id,),
        ).fetchall()
    finally:
        conn.close()

    eas: list[dict] = []
    for ea in ea_rows:
        eas.append(
            {
                "id": ea["id"],
                "name": ea["name"],
                "triggers": json.loads(ea["triggers"]) if ea["triggers"] else {},
                "actions": json.loads(ea["actions"]) if ea["actions"] else {},
            }
        )
    return eas


def _read_live_open_positions_rules(db_path: str, expert_id: int) -> list[dict]:
    """READ-ONLY read of an expert's open_positions ruleset as ExitCondition-shaped dicts.

    Resolves ``expertinstance.open_positions_ruleset_id`` -> ordered EventActions (via the shared
    ``_expert_ruleset_eas`` reader) and converts each to an ExitCondition-shaped dict.
    """
    rules: list[dict] = []
    for ea in _expert_ruleset_eas(db_path, expert_id, "open_positions_ruleset_id"):
        rule = _eventaction_to_exit_rule(ea["id"], ea["name"], ea["triggers"], ea["actions"])
        if rule is not None:
            rules.append(rule)
    return rules


@router.get("/experts/{expert_id}/open-positions-ruleset")
def get_open_positions_ruleset(expert_id: int):
    """Import a LIVE expert's open_positions ruleset as ExitCondition-shaped rules (optional).

    Graceful/optional: when ``BA2_LIVE_DB`` is unset the UI falls back to JSON-paste import
    (503). When set, the live ruleset is read READ-ONLY and converted. DB/connection errors
    map to 503 (never 500) so the UI degrades gracefully; a missing expert is a 404.
    """
    db_path = os.environ.get("BA2_LIVE_DB")
    if not db_path:
        raise HTTPException(
            status_code=503,
            detail="live DB not configured; paste the ruleset JSON instead",
        )
    try:
        rules = _read_live_open_positions_rules(db_path, expert_id)
    except HTTPException:
        raise  # 404 (expert not found) passes through unchanged
    except Exception as exc:  # noqa: BLE001 — any DB/parse failure degrades to a graceful 503
        logger.warning("live open_positions import failed for expert %s: %s", expert_id, exc)
        raise HTTPException(
            status_code=503,
            detail="could not read live DB; paste the ruleset JSON instead",
        )
    return {"rules": rules}


# --- enter_market import (uses the shared ba2_common converters) ---------------------------

def _read_live_enter_market_trees(db_path: str, expert_id: int) -> dict:
    """READ-ONLY read of an expert's enter_market ruleset -> buy/sell entry condition trees.

    Resolves ``expertinstance.enter_market_ruleset_id`` -> ordered EventActions (shared reader),
    converts each EventAction's triggers to an AND-group, routes it to buy/sell by the action's
    ``action_type``, and combines per-side groups (single -> as-is, multiple -> OR). Returns
    ``{"buy_entry_conditions": <tree|None>, "sell_entry_conditions": <tree|None>}``.
    """
    buy_groups: list[dict] = []
    sell_groups: list[dict] = []
    for ea in _expert_ruleset_eas(db_path, expert_id, "enter_market_ruleset_id"):
        side = _entry_action_side(ea["actions"])
        if side is None:
            continue
        converted = _eventaction_to_entry_group(ea["id"], ea["triggers"])
        if converted is None:
            continue
        group, _leaves = converted
        (buy_groups if side == "buy" else sell_groups).append(group)
    return {
        "buy_entry_conditions": _groups_to_tree(buy_groups),
        "sell_entry_conditions": _groups_to_tree(sell_groups),
    }


@router.get("/experts/{expert_id}/enter-market-ruleset")
def get_enter_market_ruleset(expert_id: int):
    """Import a LIVE expert's enter_market ruleset as buy/sell entry condition TREES (optional).

    The INVERSE of ``triggers_from_condition_tree``: each enter_market EventAction's triggers
    become a condition-tree AND-group (numeric leaves marked optimizable with default ranges,
    flag leaves value-less), routed to ``buy_entry_conditions`` / ``sell_entry_conditions`` by the
    action's ``action_type`` (buy/sell). Graceful/optional: 503 when ``BA2_LIVE_DB`` is unset or
    the DB is unreadable (UI falls back to JSON-paste), 404 for a missing expert; never 500.
    """
    db_path = os.environ.get("BA2_LIVE_DB")
    if not db_path:
        raise HTTPException(
            status_code=503,
            detail="live DB not configured; paste the ruleset JSON instead",
        )
    try:
        trees = _read_live_enter_market_trees(db_path, expert_id)
    except HTTPException:
        raise  # 404 (expert not found) passes through unchanged
    except Exception as exc:  # noqa: BLE001 — any DB/parse failure degrades to a graceful 503
        logger.warning("live enter_market import failed for expert %s: %s", expert_id, exc)
        raise HTTPException(
            status_code=503,
            detail="could not read live DB; paste the ruleset JSON instead",
        )
    return trees


# --- live ruleset EXPORT FILE import (DB-free, pure transform) ------------------------------

class ConvertLiveRequest(BaseModel):
    payload: dict  # the raw live export-file JSON (export_type rulesets/ruleset/rule)


@router.post("/ruleset/convert-live")
def convert_live_ruleset(req: ConvertLiveRequest):
    """Convert a LIVE-platform ruleset EXPORT FILE into backtester strategy shapes.

    Unlike the ``/experts/{id}/*`` live-import endpoints this needs NO DB — it is a pure
    transform of the uploaded JSON (``export_type`` rulesets/ruleset/rule), so the UI can
    import rules exported from the live platform without a live-DB connection. Returns
    ``{buy_entry_conditions, sell_entry_conditions, exit_conditions, summary}``. 200 even for a
    partial file (unknown triggers/actions are skipped); 422 only on a structurally broken body.
    """
    try:
        return live_export_to_strategy(req.payload or {})
    except Exception as exc:  # noqa: BLE001 — never 500 on a malformed upload
        logger.warning("convert-live failed: %s", exc)
        raise HTTPException(status_code=422, detail=f"could not convert ruleset export: {exc}")
