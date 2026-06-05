"""Seed two EXAMPLE option rulesets into the dev DB (run manually).

Run with:
    venv/bin/python test_files/setup_option_rulesets.py

This script is for local/dev exploration only — it builds two illustrative
option rulesets so you can see the trigger/action JSON shapes the
TradeActionEvaluator consumes and point an options-capable expert instance at
them by hand. It does NOT touch any real broker. Re-running is idempotent: it
deletes the previously seeded rulesets (by name) plus their event actions and
links, then recreates them fresh.

Ruleset 1 — "Example Options Dip Entry" (ENTER_MARKET):
  - Bullish dip in low IV  -> buy_call (delta ~0.40, 30-45 DTE, 2% sizing)
  - Same setup, defined-risk -> open_bull_call_spread (alternative variant)

Ruleset 2 — "Example Options Position Management" (OPEN_POSITIONS):
  - Take profit: option P/L >= +75%  -> close_option
  - Stop loss:   option P/L <= -50%  -> close_option
  - Covered-call overlay: held long AND no existing covered call -> sell_covered_call
    ("no existing covered call" is expressed with a stop_processing guard rule,
    matching the negation idiom used elsewhere in this codebase.)
"""
from sqlmodel import select

from ba2_trade_platform.core.db import get_db, add_instance
from ba2_trade_platform.core.models import Ruleset, EventAction, RulesetEventActionLink
from ba2_trade_platform.core.types import (
    ExpertEventRuleType, ExpertEventType, ExpertActionType, AnalysisUseCase,
)

ENTRY_NAME = "Example Options Dip Entry"
MANAGE_NAME = "Example Options Position Management"

RULE = ExpertEventRuleType.TRADING_RECOMMENDATION_RULE


# --- trigger / action JSON shape helpers ------------------------------------
def _triggers(*specs):
    return {f"trigger_{i}": s for i, s in enumerate(specs)}


def _actions(*specs):
    return {f"action_{i}": s for i, s in enumerate(specs)}


def flag(event_type: ExpertEventType):
    return {"event_type": event_type.value}


def num(event_type: ExpertEventType, operator: str, value):
    return {"event_type": event_type.value, "operator": operator, "value": float(value)}


def buy_call():
    return {
        "action_type": ExpertActionType.BUY_CALL.value,
        "strike_method": "delta", "strike_param": 0.40,
        "dte_min": 30, "dte_max": 45, "sizing": 2.0,
        "min_open_interest": 100, "max_spread_pct": 15.0,
    }


def bull_call_spread():
    return {
        "action_type": ExpertActionType.OPEN_BULL_CALL_SPREAD.value,
        # long ~0.40 delta, short ~0.20 delta -> defined-risk debit spread
        "strike_method": "delta", "strike_param": {"long": 0.40, "short": 0.20},
        "dte_min": 30, "dte_max": 45, "sizing": 2.0,
        "min_open_interest": 100, "max_spread_pct": 15.0,
    }


def sell_covered_call():
    return {
        "action_type": ExpertActionType.SELL_COVERED_CALL.value,
        "strike_method": "delta", "strike_param": 0.30,
        "dte_min": 30, "dte_max": 45, "sizing": 100.0,
        "min_open_interest": 100, "max_spread_pct": 15.0,
    }


CLOSE_OPTION = {"action_type": ExpertActionType.CLOSE_OPTION.value}
STOP = {"action_type": ExpertActionType.STOP_PROCESSING.value}


# (name, triggers, actions, continue_processing)
ENTRY_RULES = [
    ("Buy Call: Bullish Dip in Low IV",
     _triggers(flag(ExpertEventType.F_CURRENT_RATING_POSITIVE),
               num(ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, ">=", 10),
               num(ExpertEventType.N_IV_RANK, "<=", 40)),
     _actions(buy_call()), False),
    ("Bull Call Spread: Defined-Risk Alternative",
     _triggers(flag(ExpertEventType.F_CURRENT_RATING_POSITIVE),
               num(ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, ">=", 10),
               num(ExpertEventType.N_IV_RANK, "<=", 40)),
     _actions(bull_call_spread()), False),
]

MANAGE_RULES = [
    ("Take Profit: Option P/L >= +75%",
     _triggers(flag(ExpertEventType.F_HAS_OPTION_POSITION),
               num(ExpertEventType.N_PROFIT_LOSS_PERCENT, ">=", 75)),
     _actions(CLOSE_OPTION), False),
    ("Stop Loss: Option P/L <= -50%",
     _triggers(flag(ExpertEventType.F_HAS_OPTION_POSITION),
               num(ExpertEventType.N_PROFIT_LOSS_PERCENT, "<=", -50)),
     _actions(CLOSE_OPTION), False),
    # Guard: skip the covered-call overlay if one already exists (NOT has_covered_call).
    ("Guard: Already Has Covered Call",
     _triggers(flag(ExpertEventType.F_HAS_COVERED_CALL)),
     _actions(STOP), False),
    ("Covered Call Overlay: Sell Against Held Long",
     _triggers(flag(ExpertEventType.F_HAS_BUY_POSITION)),
     _actions(sell_covered_call()), False),
]


def _delete_existing(name: str):
    """Delete a ruleset by name plus its event actions and links (idempotency)."""
    with get_db() as session:
        ruleset = session.exec(select(Ruleset).where(Ruleset.name == name)).first()
        if not ruleset:
            return
        links = session.exec(
            select(RulesetEventActionLink).where(RulesetEventActionLink.ruleset_id == ruleset.id)
        ).all()
        ea_ids = [l.eventaction_id for l in links]
        for l in links:
            session.delete(l)
        for ea_id in ea_ids:
            ea = session.get(EventAction, ea_id)
            if ea:
                session.delete(ea)
        session.delete(ruleset)
        session.commit()
        print(f"  deleted existing ruleset '{name}' (id {ruleset.id}) + {len(ea_ids)} event actions")


def _create_ruleset(name: str, description: str, subtype: AnalysisUseCase, rules) -> int:
    ruleset_id = add_instance(
        Ruleset(name=name, description=description, type=RULE, subtype=subtype)
    )
    for order_index, (rule_name, trigs, acts, cont) in enumerate(rules):
        ea_id = add_instance(EventAction(
            name=rule_name, type=RULE, subtype=subtype,
            triggers=trigs, actions=acts, continue_processing=cont,
        ))
        with get_db() as session:
            session.add(RulesetEventActionLink(
                ruleset_id=ruleset_id, eventaction_id=ea_id, order_index=order_index,
            ))
            session.commit()
    print(f"  created '{name}' (id {ruleset_id}) with {len(rules)} rules")
    return ruleset_id


def run_setup():
    print("Seeding EXAMPLE option rulesets...")
    _delete_existing(ENTRY_NAME)
    _delete_existing(MANAGE_NAME)

    entry_id = _create_ruleset(
        ENTRY_NAME,
        "EXAMPLE: enter via a long call (delta ~0.40, 30-45 DTE, 2% sizing) on a "
        "bullish dip (>=10% below recent high) in low IV (IV rank <=40); a "
        "defined-risk bull call spread is offered as an alternative variant.",
        AnalysisUseCase.ENTER_MARKET, ENTRY_RULES,
    )
    manage_id = _create_ruleset(
        MANAGE_NAME,
        "EXAMPLE: take profit at +75% / stop out at -50% on option P/L (close_option); "
        "and sell a covered call against a held equity long when none exists yet "
        "(guarded by stop_processing so it does not stack).",
        AnalysisUseCase.OPEN_POSITIONS, MANAGE_RULES,
    )

    print(f"Done. Created example rulesets: entry={entry_id}, manage={manage_id}")
    print("Point an options-capable expert instance at them via the Settings UI "
          "(enter_market_ruleset_id / open_positions_ruleset_id).")


if __name__ == "__main__":
    run_setup()
