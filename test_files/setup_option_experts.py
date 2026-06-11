#!/usr/bin/env python
"""Seed 10 option-strategy expert instances onto the OptionsTest account (id 5).

Run with:
    .venv/Scripts/python.exe test_files/setup_option_experts.py

Creates one FMPRating-driven expert per option strategy (10 total), each
allocated 10% virtual equity, on the Alpaca PAPER options account (id 5). Each
expert clones the proven Nasdaq-30 FMPRating config from a template instance
(liquid large-caps with deep option chains), enables both buy and sell signals,
and points at a pair of freshly built option rulesets (entry + management).

The 10 strategies (one expert each):
   1. Long Call            (buy_call)              bullish directional
   2. Bull Call Spread     (open_bull_call_spread) bullish defined-risk
   3. Covered Call         (buy equity + overlay)  bullish income overlay
   4. Long Put             (buy_put)               bearish directional
   5. Bear Put Spread      (open_bear_put_spread)  bearish defined-risk debit
   6. Bear Call Spread     (open_bear_call_spread) bearish credit (short prem)
   7. Protective Put       (buy equity + hedge)    bullish + downside hedge
   8. Cash-Secured Put     (sell_cash_secured_put) neutral-bullish (short prem)
   9. Long Straddle        (open_straddle)         long volatility / catalyst
  10. Long Strangle        (open_strangle)         long volatility / catalyst

Re-running is idempotent: experts (by alias prefix "OPT-") and rulesets (by name
prefix "OPT:") from a previous run are deleted first, then recreated.
"""
import os
import sys

# Make the package importable when run as a script from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import select

from ba2_trade_platform.core.db import get_db, add_instance
from ba2_trade_platform.core.models import (
    Ruleset, EventAction, RulesetEventActionLink, ExpertInstance, ExpertSetting,
)
from ba2_trade_platform.core.types import (
    ExpertEventRuleType, ExpertActionType as A, AnalysisUseCase,
)

ACCOUNT_ID = 5                  # OptionsTest Alpaca paper account
TEMPLATE_EXPERT_ID = 5          # FMP-nas30-cons-hc (Nasdaq-30, consensus target)
EQUITY_PCT = 9.0                # virtual equity per expert (10 strategies + OPT-Wheel = 99%, never >100%)
ALIAS_PREFIX = "OPT-"
RULESET_PREFIX = "OPT:"
RULE = ExpertEventRuleType.TRADING_RECOMMENDATION_RULE

# --- Staggered, non-overlapping schedules -----------------------------------
# Existing experts (accounts 3/4) run on the :00/:15/:30/:45 grid (FMP at
# 09:30 enter / 15:30 manage; FactorRanker entries 10:00-12:15 and half-hour
# management). To guarantee NO two experts ever fire at the same wall-clock
# minute, the 10 option experts live entirely on the OFF-grid :05/:20/:35/:50
# slots: entries mid-morning (tighter option spreads than the open), management
# mid-afternoon (away from the 15:30 close crowd). Indexed to STRATEGIES order.
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
ALL_WEEKDAYS = {d: True for d in WEEKDAYS}
ALL_WEEKDAYS.update({"saturday": False, "sunday": False})

ENTER_TIMES = ["10:05", "10:20", "10:35", "10:50", "11:05",
               "11:20", "11:35", "11:50", "12:05", "12:20"]
MANAGE_TIMES = ["13:05", "13:20", "13:35", "13:50", "14:05",
                "14:20", "14:35", "14:50", "15:05", "15:20"]


def entry_days(i):
    """Exactly ONE weekday per expert for entries (runs once per week).

    Expert index -> weekday i % 5, so the 10 experts spread 2-per-weekday across
    Mon-Fri and each strategy opens new positions just once a week. MANAGEMENT/
    exits deliberately stay on ALL weekdays (see ALL_WEEKDAYS) so a held option's
    stop/target is always checked daily -- no off-day risk gap.
    """
    day = WEEKDAYS[i % 5]
    days = {d: (d == day) for d in WEEKDAYS}
    days.update({"saturday": False, "sunday": False})
    return days


_DAY_LETTER = {"monday": "Mon", "tuesday": "Tue", "wednesday": "Wed",
               "thursday": "Thu", "friday": "Fri"}


def _days_code(days):
    return ",".join(_DAY_LETTER[d] for d in WEEKDAYS if days.get(d))

# Settings overridden per new expert (everything else cloned from the template).
# Boolean settings are persisted in the value_json column (matching the template),
# NOT value_str — the settings loader reads booleans from value_json.
SETTING_OVERRIDES = {
    "enable_buy": ("value_json", "true"),
    "enable_sell": ("value_json", "true"),
}

# Buy-the-dip screener overrides for the BULLISH dip cohort (long call, bull call
# spread, cash-secured put). Tuned for OPTION liquidity: large-cap ($10B+), deep
# volume (2M+), $20+ price -> names with liquid option chains. The dip itself is a
# moderate -10% over 10 trading days, ranked biggest-drop-first. Column convention
# mirrors the existing FMP-scr-* experts exactly: int-typed settings -> value_str
# (string), float-typed settings -> value_float.
SCREENER_DIP_OVERRIDES = {
    "instrument_selection_method": ("value_str", "screener"),
    "screener_provider": ("value_str", "fmp"),
    "screener_market_cap_min": ("value_str", "10000000000"),  # $10B floor (liquid chains)
    "screener_market_cap_max": ("value_str", "0"),
    "screener_volume_min": ("value_str", "2000000"),          # 2M avg vol (deep options)
    "screener_volume_max": ("value_str", "0"),
    "screener_float_min": ("value_str", "10000000"),
    "screener_float_max": ("value_str", "0"),
    "screener_price_min": ("value_float", 20.0),              # no low-priced names
    "screener_price_max": ("value_float", 0.0),
    "screener_relative_volume_min": ("value_float", 0.0),     # disabled (avoids extra quotes)
    "screener_price_drop_pct": ("value_float", 10.0),         # the dip: -10% ...
    "screener_price_drop_days": ("value_str", "10"),          # ... over 10 trading days
    "screener_sort_metric": ("value_str", "price_drop_pct"),  # biggest dips first
    "screener_max_stocks": ("value_str", "15"),
}


# --- trigger / action JSON shape helpers ------------------------------------
def _triggers(*specs):
    return {f"trigger_{i}": s for i, s in enumerate(specs)}


def _actions(*specs):
    return {f"action_{i}": s for i, s in enumerate(specs)}


def flag(event_type: str):
    return {"event_type": event_type}


def num(event_type: str, operator: str, value):
    return {"event_type": event_type, "operator": operator, "value": float(value)}


def opt(action_type, strike_method=None, strike_param=None, dte=None,
        sizing=None, oi=100, spread=15.0):
    d = {"action_type": action_type.value}
    if strike_method is not None:
        d["strike_method"] = strike_method
    if strike_param is not None:
        d["strike_param"] = strike_param
    if dte is not None:
        d["dte_min"], d["dte_max"] = dte
    if sizing is not None:
        d["sizing"] = sizing
    d["min_open_interest"] = oi
    d["max_spread_pct"] = spread
    return d


# Reusable atomic actions
BUY_EQ = {"action_type": A.BUY.value}
TP = {"action_type": A.ADJUST_TAKE_PROFIT.value, "value": 25.0, "reference_value": "order_open_price"}
SL = {"action_type": A.ADJUST_STOP_LOSS.value, "value": -15.0, "reference_value": "order_open_price"}
CLOSE_OPT = {"action_type": A.CLOSE_OPTION.value}
CLOSE_EQ = {"action_type": A.CLOSE.value}
STOP = {"action_type": A.STOP_PROCESSING.value}

# Guard rules (placed first; halt the ruleset to prevent stacking).
GUARD_HAS_OPTION = ("Guard: Already Holds an Option",
                    _triggers(flag("has_option_position")), _actions(STOP), False)

# Shared management rules ----------------------------------------------------
LONG_PREMIUM_EXIT = [
    ("Take Profit: Option +75%",
     _triggers(flag("has_option_position"), num("profit_loss_percent", ">=", 75)),
     _actions(CLOSE_OPT), False),
    ("Stop Loss: Option -50%",
     _triggers(flag("has_option_position"), num("profit_loss_percent", "<=", -50)),
     _actions(CLOSE_OPT), False),
    ("Time Stop: 21 Days Held",
     _triggers(flag("has_option_position"), num("days_opened", ">=", 21)),
     _actions(CLOSE_OPT), False),
]

SHORT_PREMIUM_EXIT = [
    ("Buy-to-Close: 50% of Credit Captured",
     _triggers(flag("has_option_position"), num("profit_loss_percent", ">=", 50)),
     _actions(CLOSE_OPT), False),
    ("Stop Loss: Short Premium -100%",
     _triggers(flag("has_option_position"), num("profit_loss_percent", "<=", -100)),
     _actions(CLOSE_OPT), False),
    ("Time Stop: 21 Days Held",
     _triggers(flag("has_option_position"), num("days_opened", ">=", 21)),
     _actions(CLOSE_OPT), False),
]

VOL_EXIT = [
    ("Take Profit: Vol +50%",
     _triggers(flag("has_option_position"), num("profit_loss_percent", ">=", 50)),
     _actions(CLOSE_OPT), False),
    ("Stop Loss: Vol -50%",
     _triggers(flag("has_option_position"), num("profit_loss_percent", "<=", -50)),
     _actions(CLOSE_OPT), False),
    ("Time Stop: 10 Days (Close Near Catalyst)",
     _triggers(flag("has_option_position"), num("days_opened", ">=", 10)),
     _actions(CLOSE_OPT), False),
]


# --- Per-strategy specifications --------------------------------------------
# Each: alias, entry_name, entry_rules, manage_name, manage_rules
STRATEGIES = []


def _add(alias, desc, entry_rules, manage_rules, use_screener=False):
    STRATEGIES.append({
        "alias": ALIAS_PREFIX + alias,
        "desc": desc,
        "entry_name": f"{RULESET_PREFIX} {alias} Entry",
        "entry_rules": entry_rules,
        "manage_name": f"{RULESET_PREFIX} {alias} Manage",
        "manage_rules": manage_rules,
        "use_screener": use_screener,
    })


# 1. Long Call --------------------------------------------------------------
_add(
    "LongCall",
    "Long call on a bullish dip in low IV (delta ~0.45, 30-60 DTE, 2% premium).",
    [GUARD_HAS_OPTION,
     ("Buy Call: Bullish Dip, Low IV",
      _triggers(flag("current_rating_positive"),
                num("percent_below_recent_high", ">=", 8),
                num("iv_rank", "<=", 40)),
      _actions(opt(A.BUY_CALL, "delta", 0.45, (30, 60), 2.0)), False),
     ("Buy Call: High-Conviction Bullish",
      _triggers(flag("current_rating_positive"),
                num("confidence", ">=", 80),
                num("iv_rank", "<=", 50)),
      _actions(opt(A.BUY_CALL, "delta", 0.45, (30, 60), 2.0)), False)],
    LONG_PREMIUM_EXIT,
    use_screener=True,
)

# 2. Bull Call Spread -------------------------------------------------------
_add(
    "BullCallSpread",
    "Defined-risk bull call debit spread (long ~0.45 / short ~0.25 delta, 3% debit).",
    [GUARD_HAS_OPTION,
     ("Bull Call Spread: Bullish Dip",
      _triggers(flag("current_rating_positive"),
                num("percent_below_recent_high", ">=", 5)),
      _actions(opt(A.OPEN_BULL_CALL_SPREAD, "delta",
                   {"long": 0.45, "short": 0.25}, (30, 60), 3.0)), False)],
    LONG_PREMIUM_EXIT,
    use_screener=True,
)

# 3. Covered Call (buy equity, then write a call overlay) --------------------
_add(
    "CoveredCall",
    "Buy equity on a strong bullish signal, then write a 0.30-delta covered call "
    "overlay when IV is rich. Income overlay on a held long.",
    [("Buy Equity: High-Conviction Bullish",
      _triggers(flag("has_no_position"),
                flag("current_rating_positive"),
                num("confidence", ">=", 70)),
      _actions(BUY_EQ, TP, SL), False)],
    [("Guard: Already Has Covered Call",
      _triggers(flag("has_covered_call")), _actions(STOP), False),
     ("Cut Equity: Rating Downgraded",
      _triggers(flag("has_buy_position"), flag("rating_downgraded")),
      _actions(CLOSE_EQ), False),
     ("Write Covered Call: Held Long, Rich IV",
      _triggers(flag("has_buy_position"), num("iv_rank", ">=", 50)),
      _actions(opt(A.SELL_COVERED_CALL, "delta", 0.30, (30, 45), 100.0)), False)],
)

# 4. Long Put ---------------------------------------------------------------
_add(
    "LongPut",
    "Long put on a bearish rally in low IV (delta ~0.45, 30-60 DTE, 2% premium).",
    [GUARD_HAS_OPTION,
     ("Buy Put: Bearish Rally, Low IV",
      _triggers(flag("current_rating_negative"),
                num("percent_above_recent_low", ">=", 8),
                num("iv_rank", "<=", 40)),
      _actions(opt(A.BUY_PUT, "delta", 0.45, (30, 60), 2.0)), False),
     ("Buy Put: High-Conviction Bearish",
      _triggers(flag("current_rating_negative"),
                num("confidence", ">=", 80),
                num("iv_rank", "<=", 50)),
      _actions(opt(A.BUY_PUT, "delta", 0.45, (30, 60), 2.0)), False)],
    LONG_PREMIUM_EXIT,
)

# 5. Bear Put Spread --------------------------------------------------------
_add(
    "BearPutSpread",
    "Defined-risk bear put debit spread (long ~0.45 / short ~0.25 delta, 3% debit).",
    [GUARD_HAS_OPTION,
     ("Bear Put Spread: Bearish Rally",
      _triggers(flag("current_rating_negative"),
                num("percent_above_recent_low", ">=", 5)),
      _actions(opt(A.OPEN_BEAR_PUT_SPREAD, "delta",
                   {"long": 0.45, "short": 0.25}, (30, 60), 3.0)), False)],
    LONG_PREMIUM_EXIT,
)

# 6. Bear Call Spread (credit / short premium) ------------------------------
_add(
    "BearCallSpread",
    "Defined-risk bear call CREDIT spread (short ~0.30 / long ~0.15 delta) when IV "
    "is rich; max loss (width - credit) reserved. Short premium.",
    [GUARD_HAS_OPTION,
     ("Bear Call Spread: Bearish, Rich IV",
      _triggers(flag("current_rating_negative"),
                num("iv_rank", ">=", 50)),
      _actions(opt(A.OPEN_BEAR_CALL_SPREAD, "delta",
                   {"short": 0.30, "long": 0.15}, (30, 45), 3.0)), False)],
    SHORT_PREMIUM_EXIT,
)

# 7. Protective Put (buy equity, then hedge with a put) ----------------------
_add(
    "ProtectivePut",
    "Buy equity on a strong bullish signal, then buy a 5% OTM protective put when "
    "the underlying pulls back. Downside insurance on a held long.",
    [("Buy Equity: High-Conviction Bullish",
      _triggers(flag("has_no_position"),
                flag("current_rating_positive"),
                num("confidence", ">=", 70)),
      _actions(BUY_EQ, TP, SL), False)],
    [("Guard: Already Has Protective Put",
      _triggers(flag("has_protective_put")), _actions(STOP), False),
     ("Buy Protective Put: Held Long Pulling Back",
      _triggers(flag("has_buy_position"),
                num("percent_below_recent_high", ">=", 5)),
      _actions(opt(A.BUY_PROTECTIVE_PUT, "percent_otm", 5.0, (30, 45), 100.0)), False)],
)

# 8. Cash-Secured Put (short premium / bullish entry) -----------------------
_add(
    "CashSecuredPut",
    "Sell a 0.30-delta cash-secured put when bullish and IV is rich; reserve "
    "strike*100 cash. Income now, willing to be assigned the long. Short premium.",
    [GUARD_HAS_OPTION,
     ("Sell Cash-Secured Put: Bullish, Rich IV",
      _triggers(flag("current_rating_positive"),
                num("iv_rank", ">=", 50)),
      _actions(opt(A.SELL_CASH_SECURED_PUT, "delta", 0.30, (30, 45), 10.0)), False)],
    SHORT_PREMIUM_EXIT,
    use_screener=True,
)

# 9. Long Straddle (long volatility, catalyst) ------------------------------
_add(
    "Straddle",
    "Buy an ATM straddle (call + put, same strike) into a near-term catalyst when "
    "IV is cheap. Long volatility; close around the event.",
    [GUARD_HAS_OPTION,
     ("Open Straddle: Cheap IV, Earnings Imminent",
      _triggers(num("iv_rank", "<=", 35),
                num("days_to_earnings", "<=", 7)),
      _actions(opt(A.OPEN_STRADDLE, dte=(20, 45), sizing=5.0)), False)],
    VOL_EXIT,
)

# 10. Long Strangle (long volatility, catalyst) -----------------------------
_add(
    "Strangle",
    "Buy a 5% OTM strangle (OTM call + OTM put) into a near-term catalyst when IV "
    "is cheap. Cheaper than a straddle; needs a larger move. Long volatility.",
    [GUARD_HAS_OPTION,
     ("Open Strangle: Cheap IV, Earnings Imminent",
      _triggers(num("iv_rank", "<=", 35),
                num("days_to_earnings", "<=", 7)),
      _actions(opt(A.OPEN_STRANGLE, strike_param=5.0, dte=(20, 45), sizing=5.0)), False)],
    VOL_EXIT,
)


# --- DB helpers -------------------------------------------------------------
def _delete_ruleset_by_name(name: str):
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


def _create_ruleset(name, description, subtype, rules) -> int:
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
    return ruleset_id


def _delete_existing_experts():
    """Delete OPT- experts (by alias prefix) plus their settings."""
    with get_db() as session:
        experts = session.exec(
            select(ExpertInstance).where(ExpertInstance.alias.startswith(ALIAS_PREFIX))
        ).all()
        for e in experts:
            settings = session.exec(
                select(ExpertSetting).where(ExpertSetting.instance_id == e.id)
            ).all()
            for s in settings:
                session.delete(s)
            session.delete(e)
        session.commit()
        if experts:
            print(f"  deleted {len(experts)} existing OPT- expert(s) + settings")


def _clone_template_settings(new_instance_id: int, overrides: dict):
    """Copy every ExpertSetting from the template expert, applying overrides."""
    with get_db() as session:
        template = session.exec(
            select(ExpertSetting).where(ExpertSetting.instance_id == TEMPLATE_EXPERT_ID)
        ).all()
        template_keys = {s.key for s in template}
        for s in template:
            row = ExpertSetting(
                instance_id=new_instance_id, key=s.key,
                value_str=s.value_str, value_json=s.value_json, value_float=s.value_float,
            )
            if s.key in overrides:
                field, val = overrides[s.key]
                row.value_str = None
                row.value_json = None
                row.value_float = None
                setattr(row, field, val)
            session.add(row)
        # Add any override keys not present in the template.
        for key, (field, val) in overrides.items():
            if key not in template_keys:
                row = ExpertSetting(instance_id=new_instance_id, key=key)
                setattr(row, field, val)
                session.add(row)
        session.commit()


def run_setup():
    print(f"Seeding {len(STRATEGIES)} option-strategy experts onto account {ACCOUNT_ID} "
          f"({EQUITY_PCT}% equity each)...")

    # Clean slate (idempotent).
    _delete_existing_experts()
    for spec in STRATEGIES:
        _delete_ruleset_by_name(spec["entry_name"])
        _delete_ruleset_by_name(spec["manage_name"])

    created = []
    for i, spec in enumerate(STRATEGIES):
        entry_id = _create_ruleset(
            spec["entry_name"], spec["desc"],
            AnalysisUseCase.ENTER_MARKET, spec["entry_rules"],
        )
        manage_id = _create_ruleset(
            spec["manage_name"], spec["desc"],
            AnalysisUseCase.OPEN_POSITIONS, spec["manage_rules"],
        )
        instance_id = add_instance(ExpertInstance(
            alias=spec["alias"], enabled=True, account_id=ACCOUNT_ID,
            virtual_equity_pct=EQUITY_PCT, expert="FMPRating",
            user_description=spec["desc"],
            enter_market_ruleset_id=entry_id,
            open_positions_ruleset_id=manage_id,
        ))
        overrides = dict(SETTING_OVERRIDES)
        if spec.get("use_screener"):
            overrides.update(SCREENER_DIP_OVERRIDES)
        # Staggered, non-overlapping schedule (value_json dicts, matching template).
        # Entries: once a week (one weekday), distinct time. Management: every
        # weekday at a distinct time so exits are always checked.
        e_days = entry_days(i)
        overrides["execution_schedule_enter_market"] = (
            "value_json", {"days": e_days, "times": [ENTER_TIMES[i]]})
        overrides["execution_schedule_open_positions"] = (
            "value_json", {"days": dict(ALL_WEEKDAYS), "times": [MANAGE_TIMES[i]]})
        _clone_template_settings(instance_id, overrides)
        method = "screener(dip)" if spec.get("use_screener") else "static(nas30)"
        created.append((instance_id, spec["alias"], entry_id, manage_id))
        print(f"  created expert id={instance_id} {spec['alias']!r} [{method}] "
              f"enter={_days_code(e_days)}@{ENTER_TIMES[i]} "
              f"manage=MTWTF@{MANAGE_TIMES[i]} "
              f"(rulesets {entry_id}/{manage_id})")

    total_pct = EQUITY_PCT * len(created)
    print(f"\nDone. Created {len(created)} experts on account {ACCOUNT_ID}, "
          f"{EQUITY_PCT}% each ({total_pct:.0f}% total virtual equity).")
    return created


if __name__ == "__main__":
    run_setup()
