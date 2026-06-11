#!/usr/bin/env python
"""Seed the three new strategy experts (2026-06-11 session):

  1. OPT-Wheel        (account 5, FMPRating)            cash-secured put ->
                       assignment -> covered call income cycle on the verified
                       cheap-optionable universe.
  2. InsiderCluster   (account 3, FMPInsiderClusterBuy) BUY when >=3 distinct
                       insiders bought >=$200k combined within 30 days; screener
                       feeds it recent decliners (insiders buy dips).
  3. EarningsDrift    (account 3, FMPEarningsDrift)     BUY fresh >=5% EPS beats
                       (PEAD), time-boxed exit after the ~3-week drift window.

Idempotent: deletes experts by alias and rulesets by name before recreating.
Run:  .venv/Scripts/python.exe test_files/setup_strategy_experts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import select

from ba2_trade_platform.core.db import get_db, add_instance
from ba2_trade_platform.core.models import (
    Ruleset, EventAction, RulesetEventActionLink, ExpertInstance, ExpertSetting,
)
from ba2_trade_platform.core.types import ExpertEventRuleType, AnalysisUseCase

RULE = ExpertEventRuleType.TRADING_RECOMMENDATION_RULE
WEEKDAYS = {"monday": True, "tuesday": True, "wednesday": True, "thursday": True,
            "friday": True, "saturday": False, "sunday": False}

# Verified cheap-optionable universe (Alpaca chains checked 2026-06-11).
# Names whose CSP reserve (strike*100) exceeds the wheel's per-action budget
# are skipped automatically by the sizing check at run time.
WHEEL_UNIVERSE = ["GRAB", "NXE", "IBRX", "OWL", "MBLY", "NIO", "AUR", "ITUB", "LYG",
                  "ET", "TAK", "SONY", "NOK", "CMCSA", "NU", "SAN", "PBR"]

WHEEL_ACCOUNT_ID = 5     # OptionsTest
# 11 experts on account 5 at 9% each = 99% — never allocate beyond 100%.
WHEEL_EQUITY_PCT = 9.0
WHEEL_TEMPLATE_ID = 28   # OPT-CoveredCall: cloned FMPRating config (caps overridden)


def _triggers(*specs):
    return {f"trigger_{i}": s for i, s in enumerate(specs)}


def _actions(*specs):
    return {f"action_{i}": s for i, s in enumerate(specs)}


def flag(event_type):
    return {"event_type": event_type}


def num(event_type, operator, value):
    return {"event_type": event_type, "operator": operator, "value": value}


def _delete_ruleset_by_name(name):
    with get_db() as session:
        rulesets = session.exec(select(Ruleset).where(Ruleset.name == name)).all()
        for rs in rulesets:
            links = session.exec(select(RulesetEventActionLink).where(
                RulesetEventActionLink.ruleset_id == rs.id)).all()
            ea_ids = [l.eventaction_id for l in links]
            for l in links:
                session.delete(l)
            for ea in session.exec(select(EventAction).where(EventAction.id.in_(ea_ids))).all() if ea_ids else []:
                session.delete(ea)
            session.delete(rs)
        session.commit()


def _create_ruleset(name, description, subtype, rules):
    ruleset_id = add_instance(Ruleset(name=name, description=description, type=RULE, subtype=subtype))
    for order_index, (rule_name, trigs, acts, cont) in enumerate(rules):
        ea_id = add_instance(EventAction(
            name=rule_name, type=RULE, subtype=subtype,
            triggers=trigs, actions=acts, continue_processing=cont,
        ))
        with get_db() as session:
            session.add(RulesetEventActionLink(
                ruleset_id=ruleset_id, eventaction_id=ea_id, order_index=order_index))
            session.commit()
    return ruleset_id


def _delete_expert_by_alias(alias):
    with get_db() as session:
        experts = session.exec(select(ExpertInstance).where(ExpertInstance.alias == alias)).all()
        for e in experts:
            for s in session.exec(select(ExpertSetting).where(ExpertSetting.instance_id == e.id)).all():
                session.delete(s)
            session.delete(e)
        session.commit()
        if experts:
            print(f"  deleted existing expert '{alias}'")


def _set_settings(instance_id, settings):
    """settings: {key: (field, value)} with field in value_str/value_float/value_json."""
    with get_db() as session:
        for key, (field, val) in settings.items():
            row = ExpertSetting(instance_id=instance_id, key=key)
            setattr(row, field, val)
            session.add(row)
        session.commit()


def _clone_settings(template_id, new_id, overrides):
    with get_db() as session:
        template = session.exec(select(ExpertSetting).where(
            ExpertSetting.instance_id == template_id)).all()
        seen = set()
        for s in template:
            seen.add(s.key)
            row = ExpertSetting(instance_id=new_id, key=s.key, value_str=s.value_str,
                                value_json=s.value_json, value_float=s.value_float)
            if s.key in overrides:
                field, val = overrides[s.key]
                row.value_str = row.value_json = row.value_float = None
                setattr(row, field, val)
            session.add(row)
        for key, (field, val) in overrides.items():
            if key not in seen:
                row = ExpertSetting(instance_id=new_id, key=key)
                setattr(row, field, val)
                session.add(row)
        session.commit()


# =============================================================================
# 1. OPT-Wheel (account 5, FMPRating engine)
# =============================================================================
def setup_wheel():
    alias = "OPT-Wheel"
    entry_name, manage_name = "OPT: Wheel Entry", "OPT: Wheel Manage"
    _delete_expert_by_alias(alias)
    _delete_ruleset_by_name(entry_name)
    _delete_ruleset_by_name(manage_name)

    entry_rules = [
        ("Guard: Premium Already Working",
         _triggers(flag("has_option_position")),
         _actions({"action_type": "stop_processing"}), False),
        ("Guard: Holding Assigned Shares",
         _triggers(flag("has_buy_position")),
         _actions({"action_type": "stop_processing"}), False),
        ("Wheel: Sell Cash-Secured Put",
         _triggers(flag("has_no_position"),
                   flag("current_rating_positive"),
                   num("confidence", ">=", 60.0)),
         _actions({"action_type": "sell_cash_secured_put", "strike_method": "delta",
                   "strike_param": 0.3, "dte_min": 30, "dte_max": 45, "sizing": 25.0,
                   "min_open_interest": 100, "max_spread_pct": 15.0}), False),
    ]
    manage_rules = [
        ("Guard: Premium Already Working",
         _triggers(flag("has_option_position")),
         _actions({"action_type": "stop_processing"}), False),
        ("Wheel: Cut Assigned Shares on Downgrade",
         _triggers(flag("has_buy_position"), flag("rating_downgraded")),
         _actions({"action_type": "close"}), False),
        ("Wheel: Write Covered Call on Assigned Shares",
         _triggers(flag("has_buy_position")),
         _actions({"action_type": "sell_covered_call", "strike_method": "delta",
                   "strike_param": 0.3, "dte_min": 30, "dte_max": 45, "sizing": 100.0,
                   "min_open_interest": 100, "max_spread_pct": 15.0}), False),
    ]
    entry_id = _create_ruleset(entry_name, "Wheel: CSP entry leg", AnalysisUseCase.ENTER_MARKET, entry_rules)
    manage_id = _create_ruleset(manage_name, "Wheel: assignment -> covered call cycle",
                                AnalysisUseCase.OPEN_POSITIONS, manage_rules)

    instance_id = add_instance(ExpertInstance(
        alias=alias, enabled=True, account_id=WHEEL_ACCOUNT_ID, virtual_equity_pct=WHEEL_EQUITY_PCT,
        expert="FMPRating",
        user_description="Wheel: sell cash-secured puts; if assigned, write covered calls until called away",
        enter_market_ruleset_id=entry_id, open_positions_ruleset_id=manage_id,
    ))
    instruments = {s: {"enabled": True, "weight": 100.0} for s in WHEEL_UNIVERSE}
    _clone_settings(WHEEL_TEMPLATE_ID, instance_id, {
        "enabled_instruments": ("value_json", instruments),
        "max_virtual_equity_per_instrument_percent": ("value_float", 25.0),
        # Weekly entries (Tuesday), daily management - off-grid minutes
        "execution_schedule_enter_market": ("value_json", {
            "days": {**{d: False for d in WEEKDAYS}, "tuesday": True,
                     "saturday": False, "sunday": False},
            "times": ["12:35"]}),
        "execution_schedule_open_positions": ("value_json", {
            "days": dict(WEEKDAYS), "times": ["12:50"]}),
    })
    print(f"  created {alias} (instance {instance_id}, rulesets {entry_id}/{manage_id}, "
          f"{len(WHEEL_UNIVERSE)} instruments)")


# =============================================================================
# 2. InsiderCluster (account 3, FMPInsiderClusterBuy)
# =============================================================================
def setup_insider_cluster():
    alias = "InsiderCluster"
    entry_name, exit_name = "ICB: Entry", "ICB: Exit"
    _delete_expert_by_alias(alias)
    _delete_ruleset_by_name(entry_name)
    _delete_ruleset_by_name(exit_name)

    entry_rules = [
        ("Insider Cluster: Buy",
         _triggers(flag("has_no_position"),
                   flag("current_rating_positive"),
                   num("confidence", ">=", 60.0)),
         _actions({"action_type": "buy"},
                  {"action_type": "adjust_take_profit", "value": 15.0,
                   "reference_value": "order_open_price"},
                  {"action_type": "adjust_stop_loss", "value": -10.0,
                   "reference_value": "order_open_price"}), False),
    ]
    exit_rules = [
        ("Cluster Faded: Close",
         _triggers(flag("has_buy_position"), flag("rating_positive_to_neutral")),
         _actions({"action_type": "close"}), False),
        ("Max Holding Period: Close",
         _triggers(flag("has_buy_position"), num("days_opened", ">=", 40.0)),
         _actions({"action_type": "close"}), False),
    ]
    entry_id = _create_ruleset(entry_name, "Insider cluster-buy entry", AnalysisUseCase.ENTER_MARKET, entry_rules)
    exit_id = _create_ruleset(exit_name, "Insider cluster-buy exit", AnalysisUseCase.OPEN_POSITIONS, exit_rules)

    instance_id = add_instance(ExpertInstance(
        alias=alias, enabled=True, account_id=3, virtual_equity_pct=10.0,
        expert="FMPInsiderClusterBuy",
        user_description="BUY when >=3 insiders bought >=$200k combined within 30 days (screener: recent decliners)",
        enter_market_ruleset_id=entry_id, open_positions_ruleset_id=exit_id,
    ))
    _set_settings(instance_id, {
        "enable_buy": ("value_json", "true"),
        "enable_sell": ("value_json", "false"),
        "allow_automated_trade_opening": ("value_json", "true"),
        "allow_automated_trade_modification": ("value_json", "true"),
        "max_virtual_equity_per_instrument_percent": ("value_float", 20.0),
        # Insiders buy dips: feed the expert recent decliners
        "instrument_selection_method": ("value_str", "screener"),
        "screener_provider": ("value_str", "fmp"),
        "screener_market_cap_min": ("value_float", 500_000_000.0),
        "screener_market_cap_max": ("value_float", 0.0),
        "screener_volume_min": ("value_float", 500_000.0),
        "screener_volume_max": ("value_float", 0.0),
        "screener_float_min": ("value_float", 0.0),
        "screener_float_max": ("value_float", 0.0),
        "screener_price_min": ("value_float", 5.0),
        "screener_price_max": ("value_float", 0.0),
        "screener_relative_volume_min": ("value_float", 0.0),
        "screener_price_drop_pct": ("value_float", 10.0),
        "screener_price_drop_days": ("value_float", 30.0),
        "screener_sort_metric": ("value_str", "price_drop_pct"),
        "screener_max_stocks": ("value_float", 20.0),
        # Expert-specific
        "lookback_days": ("value_float", 30.0),
        "min_insiders": ("value_float", 3.0),
        "min_total_value": ("value_float", 200_000.0),
        "expected_profit_percent": ("value_float", 10.0),
        "execution_schedule_enter_market": ("value_json", {
            "days": dict(WEEKDAYS), "times": ["11:10"]}),
        "execution_schedule_open_positions": ("value_json", {
            "days": dict(WEEKDAYS), "times": ["14:10"]}),
    })
    print(f"  created {alias} (instance {instance_id}, rulesets {entry_id}/{exit_id})")


# =============================================================================
# 3. EarningsDrift (account 3, FMPEarningsDrift)
# =============================================================================
def setup_earnings_drift():
    alias = "EarningsDrift"
    entry_name, exit_name = "PEAD: Entry", "PEAD: Exit"
    _delete_expert_by_alias(alias)
    _delete_ruleset_by_name(entry_name)
    _delete_ruleset_by_name(exit_name)

    entry_rules = [
        ("Fresh EPS Beat: Buy",
         _triggers(flag("has_no_position"),
                   flag("current_rating_positive"),
                   num("confidence", ">=", 60.0)),
         _actions({"action_type": "buy"},
                  {"action_type": "adjust_take_profit", "value": 12.0,
                   "reference_value": "order_open_price"},
                  {"action_type": "adjust_stop_loss", "value": -8.0,
                   "reference_value": "order_open_price"}), False),
    ]
    exit_rules = [
        ("Drift Window Over: Close",
         _triggers(flag("has_buy_position"), num("days_opened", ">=", 15.0)),
         _actions({"action_type": "close"}), False),
    ]
    entry_id = _create_ruleset(entry_name, "Post-earnings-drift entry", AnalysisUseCase.ENTER_MARKET, entry_rules)
    exit_id = _create_ruleset(exit_name, "Post-earnings-drift time-boxed exit", AnalysisUseCase.OPEN_POSITIONS, exit_rules)

    instance_id = add_instance(ExpertInstance(
        alias=alias, enabled=True, account_id=3, virtual_equity_pct=10.0,
        expert="FMPEarningsDrift",
        user_description="PEAD: BUY fresh >=5% EPS beats, hold ~3 weeks (liquid mid/large caps)",
        enter_market_ruleset_id=entry_id, open_positions_ruleset_id=exit_id,
    ))
    _set_settings(instance_id, {
        "enable_buy": ("value_json", "true"),
        "enable_sell": ("value_json", "false"),
        "allow_automated_trade_opening": ("value_json", "true"),
        "allow_automated_trade_modification": ("value_json", "true"),
        "max_virtual_equity_per_instrument_percent": ("value_float", 20.0),
        "instrument_selection_method": ("value_str", "screener"),
        "screener_provider": ("value_str", "fmp"),
        "screener_market_cap_min": ("value_float", 2_000_000_000.0),
        "screener_market_cap_max": ("value_float", 0.0),
        "screener_volume_min": ("value_float", 1_000_000.0),
        "screener_volume_max": ("value_float", 0.0),
        "screener_float_min": ("value_float", 0.0),
        "screener_float_max": ("value_float", 0.0),
        "screener_price_min": ("value_float", 10.0),
        "screener_price_max": ("value_float", 0.0),
        "screener_relative_volume_min": ("value_float", 0.0),
        "screener_price_drop_pct": ("value_float", 0.0),   # no drop filter
        "screener_sort_metric": ("value_str", "volume"),
        "screener_max_stocks": ("value_float", 25.0),
        # Expert-specific
        "surprise_min_pct": ("value_float", 5.0),
        "max_days_since_report": ("value_float", 10.0),
        "expected_profit_percent": ("value_float", 8.0),
        "execution_schedule_enter_market": ("value_json", {
            "days": dict(WEEKDAYS), "times": ["10:40"]}),
        "execution_schedule_open_positions": ("value_json", {
            "days": dict(WEEKDAYS), "times": ["15:10"]}),
    })
    print(f"  created {alias} (instance {instance_id}, rulesets {entry_id}/{exit_id})")


if __name__ == "__main__":
    print("Seeding strategy experts (Wheel / InsiderCluster / EarningsDrift)...")
    setup_wheel()
    setup_insider_cluster()
    setup_earnings_drift()
    print("Done.")
