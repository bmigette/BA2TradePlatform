"""
One-shot setup script for the fresh-experts rollout.

Phases:
  --rulesets    Create/improve rulesets (idempotent — safe to re-run)
  --wipe        Delete all expertinstance rows (CASCADE removes children)
  --create      Insert the 16 new expert instances
  --all         Run all three in order (with prompt before --wipe)

Run from project root:
  .venv/Scripts/python.exe test_files/setup_new_experts.py --rulesets
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

# -------- DB ---------------------------------------------------------------
DB_PATH = os.path.expanduser("~/Documents/ba2_trade_platform/db.sqlite")


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


# -------- Phase 1: rulesets -----------------------------------------------

def upsert_ruleset(c, name: str, type_: str, subtype: str, description: str) -> int:
    row = c.execute(
        "SELECT id FROM ruleset WHERE name=? AND type=? AND subtype=?",
        (name, type_, subtype),
    ).fetchone()
    if row:
        c.execute(
            "UPDATE ruleset SET description=? WHERE id=?",
            (description, row["id"]),
        )
        return row["id"]
    cur = c.execute(
        "INSERT INTO ruleset(name, type, subtype, description) VALUES (?,?,?,?)",
        (name, type_, subtype, description),
    )
    return cur.lastrowid


def replace_ruleset_eventactions(c, ruleset_id: int, eas: list[dict]) -> None:
    """Replace all event-action links for a ruleset with the given list."""
    # Capture eventaction IDs we are about to delete (so we can clean them up)
    old_ea_ids = [
        r["eventaction_id"]
        for r in c.execute(
            "SELECT eventaction_id FROM ruleset_eventaction_link WHERE ruleset_id=?",
            (ruleset_id,),
        ).fetchall()
    ]
    c.execute("DELETE FROM ruleset_eventaction_link WHERE ruleset_id=?", (ruleset_id,))
    # Best-effort delete of orphaned eventactions
    for ea_id in old_ea_ids:
        still_linked = c.execute(
            "SELECT 1 FROM ruleset_eventaction_link WHERE eventaction_id=? LIMIT 1",
            (ea_id,),
        ).fetchone()
        if not still_linked:
            c.execute("DELETE FROM eventaction WHERE id=?", (ea_id,))

    for idx, ea in enumerate(eas):
        cur = c.execute(
            """
            INSERT INTO eventaction(type, subtype, name, triggers, actions,
                                    extra_parameters, continue_processing)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                ea["type"],
                ea["subtype"],
                ea["name"],
                json.dumps(ea["triggers"]),
                json.dumps(ea["actions"]),
                json.dumps(ea.get("extra_parameters", {})),
                int(ea.get("continue_processing", 0)),
            ),
        )
        c.execute(
            "INSERT INTO ruleset_eventaction_link(ruleset_id, eventaction_id, order_index) VALUES (?,?,?)",
            (ruleset_id, cur.lastrowid, idx),
        )


# --- Event-action factories (reusable shapes) ------------------------------

def ea_buy_high_conviction(tp_delta_pct: float, sl_pct: float, name: str,
                            min_conf: float, min_profit: float,
                            extra_triggers: list[dict] | None = None,
                            risk_term_triggers: list[dict] | None = None) -> dict:
    triggers = {
        "trigger_0": {"event_type": "bullish"},
        "trigger_1": {"event_type": "confidence", "operator": ">=", "value": min_conf},
        "trigger_2": {"event_type": "expected_profit_target_percent", "operator": ">=", "value": min_profit},
    }
    extra = (risk_term_triggers or []) + (extra_triggers or [])
    for i, t in enumerate(extra, start=3):
        triggers[f"trigger_{i}"] = t
    triggers[f"trigger_{3 + len(extra)}"] = {"event_type": "has_no_position"}
    return {
        "type": "TRADING_RECOMMENDATION_RULE",
        "subtype": "ENTER_MARKET",
        "name": name,
        "triggers": triggers,
        "actions": {
            "action_0": {"action_type": "buy"},
            "action_1": {
                "action_type": "adjust_take_profit",
                "value": tp_delta_pct,
                "reference_value": "expert_target_price",
            },
            "action_2": {
                "action_type": "adjust_stop_loss",
                "value": sl_pct,
                "reference_value": "order_open_price",
            },
        },
        "continue_processing": 0,
    }


def ea_sell_high_conviction(tp_delta_pct: float, sl_pct: float, name: str,
                             min_conf: float, min_profit: float,
                             extra_triggers: list[dict] | None = None,
                             risk_term_triggers: list[dict] | None = None) -> dict:
    triggers = {
        "trigger_0": {"event_type": "bearish"},
        "trigger_1": {"event_type": "confidence", "operator": ">=", "value": min_conf},
        "trigger_2": {"event_type": "expected_profit_target_percent", "operator": ">=", "value": min_profit},
    }
    extra = (risk_term_triggers or []) + (extra_triggers or [])
    for i, t in enumerate(extra, start=3):
        triggers[f"trigger_{i}"] = t
    triggers[f"trigger_{3 + len(extra)}"] = {"event_type": "has_no_position"}
    return {
        "type": "TRADING_RECOMMENDATION_RULE",
        "subtype": "ENTER_MARKET",
        "name": name,
        "triggers": triggers,
        "actions": {
            "action_0": {"action_type": "sell"},
            "action_1": {
                "action_type": "adjust_take_profit",
                "value": tp_delta_pct,
                "reference_value": "expert_target_price",
            },
            "action_2": {
                "action_type": "adjust_stop_loss",
                "value": sl_pct,
                "reference_value": "order_open_price",
            },
        },
        "continue_processing": 0,
    }


def setup_rulesets(c) -> dict[str, int]:
    """Idempotent: create/update the 4 rulesets we need."""
    ids = {}

    # ---- 1. Improve Ruleset 11 (Profit Protection Exit) ----
    # Find existing
    r = c.execute("SELECT id FROM ruleset WHERE name='Optimized Exit - Profit Protection'").fetchone()
    if r:
        rs11_id = r["id"]
    else:
        rs11_id = upsert_ruleset(
            c,
            "Optimized Exit - Profit Protection",
            "TRADING_RECOMMENDATION_RULE",
            "OPEN_POSITIONS",
            "Smart exit rules: trail stops, lock-in profits, cut losses early, hard floors.",
        )
    ids["high_conviction_exit"] = rs11_id

    # New rule set for improved exit — preserves the original logic and adds:
    #   - Tighter break-even trigger (4% profit instead of 5% + 7 days)
    #   - Hard stop loss at -12% regardless of rating (catches catastrophic moves)
    #   - Aggressive trail at 25% profit (lock in +15% gain)
    exit_eas = [
        # Trail stops by profit bucket — locks in increasing gains
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Trail_Stop_When_Profit_10pct",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "profit_loss_percent", "operator": ">=", "value": 10.0}},
            "actions": {"action_0": {"action_type": "adjust_stop_loss", "value": 5.0,
                                       "reference_value": "order_open_price"}},
            "continue_processing": 1,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Trail_Stop_When_Profit_15pct",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "profit_loss_percent", "operator": ">=", "value": 15.0}},
            "actions": {"action_0": {"action_type": "adjust_stop_loss", "value": 8.0,
                                       "reference_value": "order_open_price"}},
            "continue_processing": 1,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Trail_Stop_When_Profit_20pct",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "profit_loss_percent", "operator": ">=", "value": 20.0}},
            "actions": {"action_0": {"action_type": "adjust_stop_loss", "value": 12.0,
                                       "reference_value": "order_open_price"}},
            "continue_processing": 1,
        },
        # NEW: aggressive trail at 25%+ — lock in +15%
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Trail_Stop_When_Profit_25pct",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "profit_loss_percent", "operator": ">=", "value": 25.0}},
            "actions": {"action_0": {"action_type": "adjust_stop_loss", "value": 15.0,
                                       "reference_value": "order_open_price"}},
            "continue_processing": 1,
        },
        # Raise TP on positive target revisions
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Raise_TP_When_Target_Higher",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "new_target_higher"},
                          "trigger_2": {"event_type": "current_rating_positive"}},
            "actions": {"action_0": {"action_type": "adjust_take_profit", "value": -5.0,
                                       "reference_value": "expert_target_price"}},
            "continue_processing": 1,
        },
        # Sentiment reversal early exits
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Early_Exit_Sentiment_Reversal_Profitable",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "rating_positive_to_negative"},
                          "trigger_2": {"event_type": "profit_loss_percent", "operator": ">=", "value": 3.0}},
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Early_Exit_Sentiment_Reversal_Small_Loss",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "rating_positive_to_negative"},
                          "trigger_2": {"event_type": "profit_loss_percent", "operator": ">=", "value": -3.0},
                          "trigger_3": {"event_type": "days_opened", "operator": "<", "value": 14.0}},
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Exit_Neutral_Rating_With_Profit",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "rating_positive_to_neutral"},
                          "trigger_2": {"event_type": "profit_loss_percent", "operator": ">=", "value": 5.0}},
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
        # IMPROVED: break-even SL after 4% profit (was 5% + 7 days; too slow on fast moves)
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Move_SL_BreakEven_After_4pct_Profit",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "profit_loss_percent", "operator": ">=", "value": 4.0}},
            "actions": {"action_0": {"action_type": "adjust_stop_loss", "value": 0.5,
                                       "reference_value": "order_open_price"}},
            "continue_processing": 1,
        },
        # NEW: hard SL — close any position down 12%+ regardless of rating
        # Critical safety net for cases where rating stays positive but stock keeps falling.
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Hard_Stop_Loss_12pct",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "profit_loss_percent", "operator": "<=", "value": -12.0}},
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
        # Time decay
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Time_Decay_30Days_Tighten_TP",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "days_opened", "operator": ">=", "value": 30.0},
                          "trigger_2": {"event_type": "days_opened", "operator": "<", "value": 60.0},
                          "trigger_3": {"event_type": "profit_loss_percent", "operator": "<", "value": 5.0}},
            "actions": {"action_0": {"action_type": "adjust_take_profit", "value": -7.0,
                                       "reference_value": "expert_target_price"}},
            "continue_processing": 1,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Time_Decay_60Days_BreakEven_TP",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "days_opened", "operator": ">=", "value": 60.0},
                          "trigger_2": {"event_type": "profit_loss_percent", "operator": "<", "value": 5.0}},
            "actions": {"action_0": {"action_type": "adjust_take_profit", "value": 1.0,
                                       "reference_value": "order_open_price"}},
            "continue_processing": 1,
        },
        # Long-stale, no-progress positions
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Long_Term_Exit_90Days_No_Progress",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "days_opened", "operator": ">=", "value": 90.0},
                          "trigger_2": {"event_type": "profit_loss_percent", "operator": ">=", "value": 0.0},
                          "trigger_3": {"event_type": "profit_loss_percent", "operator": "<", "value": 3.0},
                          "trigger_4": {"event_type": "current_rating_neutral"}},
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Cut_Loss_Negative_Rating_Under_10pct",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "current_rating_negative"},
                          "trigger_2": {"event_type": "profit_loss_percent", "operator": ">=", "value": -10.0},
                          "trigger_3": {"event_type": "profit_loss_percent", "operator": "<", "value": 0.0}},
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Exit_120Days_Still_Negative",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "days_opened", "operator": ">=", "value": 120.0},
                          "trigger_2": {"event_type": "current_rating_negative"}},
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
        # Allocation drift
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Lower_Target_Reduce_Exposure",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "new_target_lower"},
                          "trigger_2": {"event_type": "instrument_account_share", "operator": ">", "value": 10.0}},
            "actions": {"action_0": {"action_type": "decrease_instrument_share", "target_percent": 8.0}},
            "continue_processing": 1,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Scale_Into_Winner",
            "triggers": {"trigger_0": {"event_type": "has_position"},
                          "trigger_1": {"event_type": "new_target_higher"},
                          "trigger_2": {"event_type": "profit_loss_percent", "operator": ">=", "value": 8.0},
                          "trigger_3": {"event_type": "confidence", "operator": ">=", "value": 80.0},
                          "trigger_4": {"event_type": "instrument_account_share", "operator": "<", "value": 12.0}},
            "actions": {"action_0": {"action_type": "increase_instrument_share", "target_percent": 12.0}},
            "continue_processing": 1,
        },
    ]
    replace_ruleset_eventactions(c, rs11_id, exit_eas)

    # ---- 2. High Conviction Entry (Low Target) — clone of #10 without -5% on TP ----
    rs_low_id = upsert_ruleset(
        c,
        "Optimized Entry - High Conviction (Low Target)",
        "TRADING_RECOMMENDATION_RULE",
        "ENTER_MARKET",
        "High-conviction entry for FMP experts using 'low' target_price_type — "
        "TP set EXACTLY at analyst low target (no -5% buffer applied).",
    )
    risk_long  = [{"event_type": "long_term"}, {"event_type": "lowrisk"}]
    risk_med   = [{"event_type": "medium_term"}, {"event_type": "mediumrisk"}]
    risk_short = [{"event_type": "short_term"}]
    low_entry_eas = [
        ea_buy_high_conviction(0.0, -8.0, "BUY_HighConfidence_LowRisk_LongTerm",  80.0, 10.0, risk_term_triggers=risk_long),
        ea_buy_high_conviction(0.0, -6.0, "BUY_HighConfidence_MedRisk_MedTerm",   78.0,  8.0, risk_term_triggers=risk_med),
        ea_buy_high_conviction(0.0, -4.0, "BUY_VeryHighConfidence_ShortTerm",     85.0,  5.0, risk_term_triggers=risk_short),
        ea_buy_high_conviction(0.0, -7.0, "BUY_Fallback_StrongSignal",            75.0, 12.0),
        ea_sell_high_conviction(0.0, -8.0, "SELL_HighConfidence_LowRisk_LongTerm", 80.0, 10.0, risk_term_triggers=risk_long),
        ea_sell_high_conviction(0.0, -6.0, "SELL_HighConfidence_MedRisk_MedTerm",  78.0,  8.0, risk_term_triggers=risk_med),
        ea_sell_high_conviction(0.0, -4.0, "SELL_VeryHighConfidence_ShortTerm",    85.0,  5.0, risk_term_triggers=risk_short),
        ea_sell_high_conviction(0.0, -7.0, "SELL_Fallback_StrongSignal",           75.0, 12.0),
    ]
    replace_ruleset_eventactions(c, rs_low_id, low_entry_eas)
    ids["high_conviction_entry_low"] = rs_low_id

    # ---- 3. Static TP/SL +/-15% Entry ----
    rs_static_in = upsert_ruleset(
        c,
        "Static TP/SL +-15% Entry",
        "TRADING_RECOMMENDATION_RULE",
        "ENTER_MARKET",
        "Simple entry: when expert is sufficiently bullish/bearish, open with TP=+15% / SL=-15% "
        "from entry price. Independent of expert target.",
    )
    # Single BUY + single SELL rule. Lower confidence floor since static TP/SL is more forgiving.
    static_entry_eas = [
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "ENTER_MARKET",
            "name": "BUY_Static_TPSL_15pct",
            "triggers": {
                "trigger_0": {"event_type": "bullish"},
                "trigger_1": {"event_type": "confidence", "operator": ">=", "value": 65.0},
                "trigger_2": {"event_type": "expected_profit_target_percent", "operator": ">=", "value": 3.0},
                "trigger_3": {"event_type": "has_no_position"},
            },
            "actions": {
                "action_0": {"action_type": "buy"},
                "action_1": {"action_type": "adjust_take_profit", "value": 15.0,
                              "reference_value": "order_open_price"},
                "action_2": {"action_type": "adjust_stop_loss", "value": -15.0,
                              "reference_value": "order_open_price"},
            },
            "continue_processing": 0,
        },
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "ENTER_MARKET",
            "name": "SELL_Static_TPSL_15pct",
            "triggers": {
                "trigger_0": {"event_type": "bearish"},
                "trigger_1": {"event_type": "confidence", "operator": ">=", "value": 65.0},
                "trigger_2": {"event_type": "expected_profit_target_percent", "operator": ">=", "value": 3.0},
                "trigger_3": {"event_type": "has_no_position"},
            },
            "actions": {
                "action_0": {"action_type": "sell"},
                "action_1": {"action_type": "adjust_take_profit", "value": -15.0,
                              "reference_value": "order_open_price"},
                "action_2": {"action_type": "adjust_stop_loss", "value": 15.0,
                              "reference_value": "order_open_price"},
            },
            "continue_processing": 0,
        },
    ]
    replace_ruleset_eventactions(c, rs_static_in, static_entry_eas)
    ids["static_tpsl_entry"] = rs_static_in

    # ---- 4. Static TP/SL +/-15% Exit ----
    rs_static_out = upsert_ruleset(
        c,
        "Static TP/SL +-15% Exit",
        "TRADING_RECOMMENDATION_RULE",
        "OPEN_POSITIONS",
        "Minimal exit logic: let the +-15% broker TP/SL run, but cut on hard sentiment reversal "
        "and force-close stale positions after 60 days.",
    )
    static_exit_eas = [
        # Cut on sentiment flip if down >5%
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Cut_On_Rating_Reversal_To_Negative",
            "triggers": {
                "trigger_0": {"event_type": "has_position"},
                "trigger_1": {"event_type": "rating_positive_to_negative"},
            },
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
        # Force-close after 60 days (let the broker TP/SL do the work otherwise)
        {
            "type": "TRADING_RECOMMENDATION_RULE", "subtype": "OPEN_POSITIONS",
            "name": "Force_Close_After_60_Days",
            "triggers": {
                "trigger_0": {"event_type": "has_position"},
                "trigger_1": {"event_type": "days_opened", "operator": ">=", "value": 60.0},
            },
            "actions": {"action_0": {"action_type": "close"}},
            "continue_processing": 0,
        },
    ]
    replace_ruleset_eventactions(c, rs_static_out, static_exit_eas)
    ids["static_tpsl_exit"] = rs_static_out

    # Lookup the existing high-conviction entry id=10 (for FMP "consensus" variants)
    r = c.execute(
        "SELECT id FROM ruleset WHERE name='Optimized Entry - High Conviction'"
    ).fetchone()
    ids["high_conviction_entry_consensus"] = r["id"] if r else None
    return ids


# -------- Phase 2 + 3: wipe + create --------------------------------------

NEW_ACCOUNT_ID = 3  # ba2New

COMMON_RISK_OVERRIDES = {
    # (key, value_str, value_float, value_json)
    "virtual_equity_pct_via_instance_col": None,  # set on the row itself
    "max_virtual_equity_per_instrument_percent": ("max_virtual_equity_per_instrument_percent", None, 15.0, None),
    "min_available_balance_pct": ("min_available_balance_pct", None, 10.0, None),
    "allow_automated_trade_opening": ("allow_automated_trade_opening", None, None, '"true"'),
    "allow_automated_trade_modification": ("allow_automated_trade_modification", None, None, '"true"'),
    "enable_buy": ("enable_buy", None, None, '"true"'),
    "enable_sell": ("enable_sell", None, None, '"false"'),
}


def get_template_settings(c, instance_id: int) -> list[dict]:
    rows = c.execute(
        "SELECT key, value_str, value_float, value_json FROM expertsetting WHERE instance_id=?",
        (instance_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def apply_common_overrides(settings_by_key: dict[str, dict]) -> None:
    """Force common risk-settings to the standard values requested for v2."""
    for k, ovr in COMMON_RISK_OVERRIDES.items():
        if ovr is None:
            continue
        key, vs, vf, vj = ovr
        settings_by_key[key] = {
            "key": key,
            "value_str": vs,
            "value_float": vf,
            "value_json": vj or "{}",
        }


def insert_expert(c, alias: str, expert_class: str, entry_rs_id: int | None,
                  exit_rs_id: int | None, source_settings: list[dict],
                  overrides: dict[str, dict] | None = None,
                  user_description: str = "") -> int:
    """Insert one expertinstance + its settings. Returns the new id."""
    cur = c.execute(
        """
        INSERT INTO expertinstance(account_id, expert, user_description, enabled,
                                    virtual_equity_pct, enter_market_ruleset_id,
                                    open_positions_ruleset_id, alias)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            NEW_ACCOUNT_ID,
            expert_class,
            user_description,
            1,
            5.0,
            entry_rs_id,
            exit_rs_id,
            alias,
        ),
    )
    new_id = cur.lastrowid

    # Build merged settings: template + common risk overrides + per-variant overrides
    merged: dict[str, dict] = {}
    for s in source_settings:
        merged[s["key"]] = dict(s)
    apply_common_overrides(merged)
    if overrides:
        for k, v in overrides.items():
            merged[k] = {"key": k, **v}

    # Skip stale/auto-managed keys we shouldn't copy
    SKIP = {"virtual_equity_pct", "id", "instance_id"}
    for key, s in merged.items():
        if key in SKIP:
            continue
        c.execute(
            """
            INSERT INTO expertsetting(instance_id, key, value_str, value_float, value_json)
            VALUES (?,?,?,?,?)
            """,
            (
                new_id,
                key,
                s.get("value_str"),
                s.get("value_float"),
                s.get("value_json") if s.get("value_json") is not None else "{}",
            ),
        )
    return new_id


def fetch_label_symbols_dict(c, label: str) -> str:
    """Return JSON enabled_instruments dict for symbols tagged with the given label."""
    syms = [r[0] for r in c.execute(
        "SELECT name FROM instrument WHERE labels LIKE ? ORDER BY name",
        (f'%"{label}"%',),
    ).fetchall()]
    return json.dumps({s: {"enabled": True, "weight": 100.0} for s in syms})


def wipe_experts(c) -> int:
    """Delete all expert-related data, biggest leaves first to avoid CASCADE
    fanout on a 1.7GB DB. Uses one transaction per table for progress."""
    import time
    count = c.execute("SELECT COUNT(*) FROM expertinstance").fetchone()[0]

    # Disable FK enforcement during the wipe — we'll delete in a safe order
    # so children go first; this avoids cascade traversal blowing the WAL.
    c.execute("PRAGMA foreign_keys = OFF")

    plan = [
        ("analysisoutput",        "DELETE FROM analysisoutput WHERE market_analysis_id IS NOT NULL"),
        ("trade_action_result",   "DELETE FROM trade_action_result"),
        ("llmusagelog",           "DELETE FROM llmusagelog"),
        ("smartriskmanagerjob",   "DELETE FROM smartriskmanagerjob"),
        ("tradingorder",          "DELETE FROM tradingorder"),
        ("transaction",           "DELETE FROM \"transaction\""),
        ("expertrecommendation",  "DELETE FROM expertrecommendation"),
        ("marketanalysis",        "DELETE FROM marketanalysis"),
        ("persistedqueuetask",    "DELETE FROM persistedqueuetask WHERE expert_instance_id IS NOT NULL"),
        ("activitylog",           "DELETE FROM activitylog WHERE source_expert_id IS NOT NULL"),
        ("expertsetting",         "DELETE FROM expertsetting"),
        ("expertinstance",        "DELETE FROM expertinstance"),
    ]
    for name, sql in plan:
        t0 = time.time()
        before = c.execute(f"SELECT COUNT(*) FROM \"{name}\"").fetchone()[0]
        c.execute(sql)
        c.commit()
        after = c.execute(f"SELECT COUNT(*) FROM \"{name}\"").fetchone()[0]
        dt = time.time() - t0
        print(f"  wiped {name:24s}: {before:>7d} -> {after:>7d}  ({dt:.1f}s)", flush=True)

    c.execute("PRAGMA foreign_keys = ON")
    return count


# -------- create configurations --------------------------------------------

def create_all_experts(c, rs_ids: dict[str, int]) -> list[int]:
    """Create the 16 experts on the ba2New account."""
    new_ids = []

    # Capture template settings before wipe... but wipe already happened.
    # So we have to read them BEFORE wipe. The orchestration in main() handles that.
    return new_ids


def capture_templates(c) -> dict[str, list[dict]]:
    """Snapshot template expert settings before they're wiped."""
    templates = {
        "FH_short_med":   get_template_settings(c, 3),
        "FH_risky_long":  get_template_settings(c, 5),
        "senate_weight":  get_template_settings(c, 7),
        "penny":          get_template_settings(c, 17),
        "fmp_consensus":  get_template_settings(c, 6),
        "fmp_screener":   get_template_settings(c, 18),
    }
    return templates


def materialize_experts(c, templates: dict[str, list[dict]],
                        rs_ids: dict[str, int]) -> list[tuple[int, str]]:
    """Insert 4 baseline + 12 FMP variants."""
    created: list[tuple[int, str]] = []

    rs_hc_entry_cons = rs_ids["high_conviction_entry_consensus"]  # 10
    rs_hc_exit       = rs_ids["high_conviction_exit"]             # 11
    rs_hc_entry_low  = rs_ids["high_conviction_entry_low"]        # 12
    rs_static_entry  = rs_ids["static_tpsl_entry"]                # 13
    rs_static_exit   = rs_ids["static_tpsl_exit"]                 # 14

    # ---- Baseline 4 ----
    new_id = insert_expert(c, "FH Short/Med", "FinnHubRating", rs_hc_entry_cons, rs_hc_exit,
                            templates["FH_short_med"])
    created.append((new_id, "FH Short/Med"))

    new_id = insert_expert(c, "RiskyLongFH", "FinnHubRating", rs_hc_entry_cons, rs_hc_exit,
                            templates["FH_risky_long"])
    created.append((new_id, "RiskyLongFH"))

    new_id = insert_expert(c, "FMPSenateWeight", "FMPSenateTraderWeight", rs_hc_entry_cons, rs_hc_exit,
                            templates["senate_weight"])
    created.append((new_id, "FMPSenateWeight"))

    # Penny has its own internal exit logic — leave ruleset ids None
    new_id = insert_expert(c, "TestPenny", "PennyMomentumTrader", None, None,
                            templates["penny"])
    created.append((new_id, "TestPenny"))

    # ---- 12 FMP variants ----
    nas30_json = fetch_label_symbols_dict(c, "NASDAQ30")
    ark26_json = fetch_label_symbols_dict(c, "ARK26")

    def fmp_overrides(method: str, target_type: str,
                       instruments_json: str | None) -> dict[str, dict]:
        ovr = {
            "instrument_selection_method": {"value_str": method, "value_float": None, "value_json": "{}"},
            "target_price_type": {"value_str": target_type, "value_float": None, "value_json": "{}"},
        }
        if instruments_json is not None:
            ovr["enabled_instruments"] = {"value_str": None, "value_float": None, "value_json": instruments_json}
        return ovr

    # Pick the right template per selection method
    def template_for(method: str) -> list[dict]:
        return templates["fmp_screener"] if method == "screener" else templates["fmp_consensus"]

    combos = [
        # (alias_suffix, method, instruments_json, target_type, entry_rs, exit_rs)
        ("nas30-cons-hc",    "static",   nas30_json, "consensus", rs_hc_entry_cons, rs_hc_exit),
        ("nas30-cons-stat",  "static",   nas30_json, "consensus", rs_static_entry, rs_static_exit),
        ("nas30-low-hc",     "static",   nas30_json, "low",       rs_hc_entry_low, rs_hc_exit),
        ("nas30-low-stat",   "static",   nas30_json, "low",       rs_static_entry, rs_static_exit),
        ("ark26-cons-hc",    "static",   ark26_json, "consensus", rs_hc_entry_cons, rs_hc_exit),
        ("ark26-cons-stat",  "static",   ark26_json, "consensus", rs_static_entry, rs_static_exit),
        ("ark26-low-hc",     "static",   ark26_json, "low",       rs_hc_entry_low, rs_hc_exit),
        ("ark26-low-stat",   "static",   ark26_json, "low",       rs_static_entry, rs_static_exit),
        ("scr-cons-hc",      "screener", None,       "consensus", rs_hc_entry_cons, rs_hc_exit),
        ("scr-cons-stat",    "screener", None,       "consensus", rs_static_entry, rs_static_exit),
        ("scr-low-hc",       "screener", None,       "low",       rs_hc_entry_low, rs_hc_exit),
        ("scr-low-stat",     "screener", None,       "low",       rs_static_entry, rs_static_exit),
    ]
    for suffix, method, instruments_json, target_type, entry_rs, exit_rs in combos:
        alias = f"FMP-{suffix}"
        ovr = fmp_overrides(method, target_type, instruments_json)
        new_id = insert_expert(
            c, alias, "FMPRating", entry_rs, exit_rs,
            template_for(method),
            overrides=ovr,
        )
        created.append((new_id, alias))

    return created


# -------- main -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rulesets", action="store_true")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--create", action="store_true")
    args = ap.parse_args()

    if not (args.rulesets or args.wipe or args.create):
        ap.print_help()
        sys.exit(1)

    with conn() as c:
        if args.rulesets:
            ids = setup_rulesets(c)
            c.commit()
            print("Rulesets configured:")
            for k, v in ids.items():
                print(f"  {k} -> ruleset id {v}")

        if args.wipe or args.create:
            # We need ruleset ids for create, and templates BEFORE wipe.
            rs_ids = {
                "high_conviction_entry_consensus": c.execute(
                    "SELECT id FROM ruleset WHERE name='Optimized Entry - High Conviction'"
                ).fetchone()["id"],
                "high_conviction_exit": c.execute(
                    "SELECT id FROM ruleset WHERE name='Optimized Exit - Profit Protection'"
                ).fetchone()["id"],
                "high_conviction_entry_low": c.execute(
                    "SELECT id FROM ruleset WHERE name='Optimized Entry - High Conviction (Low Target)'"
                ).fetchone()["id"],
                "static_tpsl_entry": c.execute(
                    "SELECT id FROM ruleset WHERE name='Static TP/SL +-15% Entry'"
                ).fetchone()["id"],
                "static_tpsl_exit": c.execute(
                    "SELECT id FROM ruleset WHERE name='Static TP/SL +-15% Exit'"
                ).fetchone()["id"],
            }

            templates = capture_templates(c)
            print("Captured template settings:")
            for k, v in templates.items():
                print(f"  {k}: {len(v)} settings")

            if args.wipe:
                n = wipe_experts(c)
                c.commit()
                print(f"Wiped {n} expertinstance rows (and dependent rows)")

            if args.create:
                created = materialize_experts(c, templates, rs_ids)
                c.commit()
                print(f"Created {len(created)} new experts on account {NEW_ACCOUNT_ID}:")
                for nid, alias in created:
                    print(f"  id={nid:3d}  {alias}")


if __name__ == "__main__":
    main()
