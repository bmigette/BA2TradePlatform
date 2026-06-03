"""Create the FinnHub-tailored entry/exit rulesets and point the FinnHub expert
instances (ids 1 and 2) at them.

Idempotent: re-running deletes the previously created rulesets (by name) and
their event actions, then recreates them fresh.

Entry "FinnHub Rating Entry" (ENTER_MARKET): buy on a rating upgrade into bullish
territory, or on a strong steady-state rating, with conviction-tiered TP/SL
referenced to the order open price (FinnHub has no price target).

Exit "FinnHub Rating Exit" (OPEN_POSITIONS): cut on any rating downgrade, except
hold a *losing* position whose downgrade only dropped it to OVERWEIGHT (still
mildly bullish) — a guard rule using STOP_PROCESSING.
"""
from sqlmodel import select

from ba2_trade_platform.core.db import get_db, add_instance, get_instance, update_instance
from ba2_trade_platform.core.models import (
    Ruleset, EventAction, RulesetEventActionLink, ExpertInstance,
)
from ba2_trade_platform.core.types import ExpertEventRuleType, AnalysisUseCase

ENTRY_NAME = "FinnHub Rating Entry"
EXIT_NAME = "FinnHub Rating Exit"
FINNHUB_INSTANCE_IDS = [1, 2]

RULE = ExpertEventRuleType.TRADING_RECOMMENDATION_RULE


def _triggers(*specs):
    return {f"trigger_{i}": s for i, s in enumerate(specs)}


def _actions(*specs):
    return {f"action_{i}": s for i, s in enumerate(specs)}


def conf(v):
    return {"event_type": "confidence", "operator": ">=", "value": float(v)}


def pl(op, v):
    return {"event_type": "profit_loss_percent", "operator": op, "value": float(v)}


def tp(v):
    return {"action_type": "adjust_take_profit", "value": float(v), "reference_value": "order_open_price"}


def sl(v):
    return {"action_type": "adjust_stop_loss", "value": float(v), "reference_value": "order_open_price"}


BUY = {"action_type": "buy"}
CLOSE = {"action_type": "close"}
STOP = {"action_type": "stop_processing"}

# (name, triggers, actions, continue_processing)
ENTRY_RULES = [
    ("FH Buy: Upgraded to Strong Buy",
     _triggers({"event_type": "has_no_position"}, {"event_type": "rating_upgraded"},
               {"event_type": "current_rating_positive"}, conf(70)),
     _actions(BUY, tp(25), sl(-15)), False),
    ("FH Buy: Steady Strong Buy",
     _triggers({"event_type": "has_no_position"}, {"event_type": "current_rating_positive"}, conf(80)),
     _actions(BUY, tp(25), sl(-15)), False),
    ("FH Buy: Upgraded to Overweight",
     _triggers({"event_type": "has_no_position"}, {"event_type": "rating_upgraded"},
               {"event_type": "current_rating_overweight"}, conf(65)),
     _actions(BUY, tp(15), sl(-10)), False),
    ("FH Buy: Steady Overweight (High Conf)",
     _triggers({"event_type": "has_no_position"}, {"event_type": "current_rating_overweight"}, conf(80)),
     _actions(BUY, tp(15), sl(-10)), False),
]

EXIT_RULES = [
    # Guard: hold a losing position when the downgrade only dropped it to OVERWEIGHT.
    ("FH Hold: Soft Downgrade While Losing",
     _triggers({"event_type": "has_position"}, {"event_type": "rating_downgraded"},
               {"event_type": "current_rating_overweight"}, pl("<", 0)),
     _actions(STOP), False),
    ("FH Cut: On Downgrade",
     _triggers({"event_type": "has_position"}, {"event_type": "rating_downgraded"}),
     _actions(CLOSE), False),
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


def main():
    print("Setting up FinnHub rulesets...")
    _delete_existing(ENTRY_NAME)
    _delete_existing(EXIT_NAME)

    entry_id = _create_ruleset(
        ENTRY_NAME,
        "FinnHub: buy on rating upgrade into bullish territory or strong steady rating; "
        "conviction-tiered TP/SL from open price (strong BUY +25/-15, OVERWEIGHT +15/-10).",
        AnalysisUseCase.ENTER_MARKET, ENTRY_RULES,
    )
    exit_id = _create_ruleset(
        EXIT_NAME,
        "FinnHub: cut on any rating downgrade; hold a losing position when the downgrade "
        "only dropped it to OVERWEIGHT (guard via stop_processing).",
        AnalysisUseCase.OPEN_POSITIONS, EXIT_RULES,
    )

    for inst_id in FINNHUB_INSTANCE_IDS:
        inst = get_instance(ExpertInstance, inst_id)
        if not inst:
            print(f"  WARNING: expert instance {inst_id} not found - skipping")
            continue
        inst.enter_market_ruleset_id = entry_id
        inst.open_positions_ruleset_id = exit_id
        update_instance(inst)
        print(f"  pointed instance {inst_id} ({inst.expert}/{inst.alias}) -> entry {entry_id}, exit {exit_id}")

    print("Done.")


if __name__ == "__main__":
    main()
