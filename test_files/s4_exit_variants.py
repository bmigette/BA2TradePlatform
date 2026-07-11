"""Manual (no-GA) exit-mechanic experiments off S4's TOP1 winner (backtest #324, opt #87,
large-cap, 177.41% / 2023-2026 / 90 trades / 32.2% win rate).

Motivation (user-driven): TOP1's exit structure has only two profit tiers (>12% -> SL to
+1%, >24% -> SL to +18% AND TP extended to 48%) plus a flat -4% floor stop. Every losing
trade round-trips the full -4% (58 of 90 trades), and every winner that clears the top tier
rides all the way to the fixed +48% target (25 of 90) -- there is no smooth trailing/stepped
behaviour and no partial profit-taking. The user wants to see whether tighter early protection
(earlier breakeven locks, true percent-trailing stops via reference_value=current_price,
partial scale-outs, more/finer tiers, or entry selectivity) improves win rate / consistency
while still keeping ~30%/yr, and whether the edge survives outside the very bullish
2023-2026 window (a few variants are reserved to re-run once the 2022 backfill lands).

Each variant is a (entry_rules, exit_rules) -> (entry_rules, exit_rules) transform applied to
a deep copy of TOP1's own concrete (already gene-decoded) rule lists, so every variant shares
the exact same entry logic/universe/dates/expert settings as the baseline unless the variant
explicitly changes that. Persists each run as its own tagged, saved Backtest row
(name="VAR-<variant>-S4TOP1[-<window>]") so results are comparable via the UI/DB like any
other backtest.

Run (test venv, from repo root):
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe test_files/s4_exit_variants.py --variant trail_only_3pct
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe test_files/s4_exit_variants.py --list
"""
import argparse
import copy
import json
import os
import sqlite3
import sys
from datetime import datetime as _dt

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND = os.path.join(_REPO, "testplatform", "backend")
sys.path.insert(0, _BACKEND)
os.chdir(_BACKEND)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BACKEND, ".env"))
    load_dotenv(os.path.join(_REPO, "testplatform", ".env"))
except Exception:  # noqa: BLE001
    pass

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"
from ba2_common.core import db as _ba2_db  # noqa: E402
_ba2_db.configure_db(DB)
if not os.getenv("FMP_API_KEY"):
    from ba2_common.config import get_app_setting
    _k = get_app_setting("FMP_API_KEY")
    if _k:
        os.environ["FMP_API_KEY"] = _k

_BASE_BACKTEST_ID = 324   # TOP1-scr-large-FMPRating-S4-goal5
_BASE_OPT_ID = 87


# ---------------------------------------------------------------------------
# Rule-building helpers
# ---------------------------------------------------------------------------
def _leaf(field, comparison, value=None, field_type="numeric", rid=None):
    d = {"id": rid or field, "field": field, "field_type": field_type,
         "fieldType": field_type, "comparison": comparison, "op": comparison}
    if value is not None:
        d["value"] = value
    return d


def _group(*conds, op="AND"):
    return {"id": "root", "operator": op, "type": op, "conditions": list(conds)}


def _action(action_type, action_value=None, reference_value=None, aid=None):
    d = {"id": aid or action_type, "action_type": action_type, "action": action_type}
    if action_value is not None:
        d["action_value"] = action_value
    if reference_value is not None:
        d["reference_value"] = reference_value
    return d


def _rule(rid, conditions, actions, continue_processing=False, name=None):
    return {"id": rid, "name": name or rid, "conditions": conditions,
            "actions": actions, "continue_processing": continue_processing}


EXIT_BEARISH = _rule("exit_bearish", _group(_leaf("bearish", "is_true", field_type="flag")),
                      [_action("close")])


def floor_stop(pct):
    """Catch-all floor stop-loss (fixed % below entry) -- gated on has_position so it's ALWAYS
    live; ratchet-only semantics mean tighter stops set by other rules always win over this."""
    return _rule("exit_stoploss", _group(_leaf("has_position", "is_true", field_type="flag")),
                  [_action("adjust_stop_loss", pct, "order_open_price")])


def trail(pct, rid="trail"):
    """A TRUE percent-trailing stop: fires every bar while in position, proposing
    stop = current_price * (1 - pct/100). Ratchet-only means this only ever tightens the
    stop as price rises -- it silently no-ops on a pullback.

    IMPORTANT (found via smoke test): continue_processing=True here is a bug trap. If a
    LATER rule (e.g. floor_stop) ALSO fires on the same "has_position" gate in the same
    bar, the evaluator's multi-action handling is "keep only the LAST proposed SL action"
    (list order), NOT "keep the tighter one" -- ratchet-only tightening only applies across
    BARS (new proposal vs. the persisted order from a prior bar), not across multiple
    same-bar proposals. A trail listed before an always-firing floor_stop got silently
    overridden by floor_stop's fixed value every single bar, so the position was pinned to
    an unmoving synthetic stop that (empirically) never triggered a real fill for 3 straight
    years across all 3 test positions. Fix: trail must be the ONLY always-firing SL rule in
    its list (no separate floor_stop alongside it) and keeps continue_processing=False (the
    default) -- first-bar-after-entry naturally sets an initial floor since there is no
    prior ruleset-driven stop yet to ratchet against.
    """
    return _rule(rid, _group(_leaf("has_position", "is_true", field_type="flag")),
                 [_action("adjust_stop_loss", pct, "current_price")])


def breakeven_lock(gate_pct, lock_pct=0.5, rid="belock"):
    return _rule(rid, _group(_leaf("profit_loss_percent", ">", gate_pct)),
                 [_action("adjust_stop_loss", lock_pct, "order_open_price")])


def tier(rid, gate_pct, sl_pct, tp_pct=None):
    acts = [_action("adjust_stop_loss", sl_pct, "order_open_price", aid=f"{rid}_sl")]
    if tp_pct is not None:
        acts.append(_action("adjust_take_profit", tp_pct, "order_open_price", aid=f"{rid}_tp"))
    return _rule(rid, _group(_leaf("profit_loss_percent", ">", gate_pct)), acts)


def scaleout(gate_pct, target_pct, rid="scaleout"):
    """target_pct is a TARGET percent of virtual equity to reduce the position down to
    (DecreaseInstrumentShareAction.target_percent), NOT a relative "cut by X%" delta --
    confirmed in TradeActions.py: it computes target_value = equity * target_pct/100 and
    only submits a reduce order while current_value > target_value, else no-ops with
    "already at target". That makes it safe/idempotent to fire on EVERY bar past the gate
    (continue_processing=True), unlike adjust_stop_loss: it won't repeatedly shrink the
    position, and it's a different action type than the SL/TP actions on tier/floor rules
    so it doesn't collide with those in the same-bar action dedup.

    NOTE: S4's baseline caps any single position at 20% of virtual equity
    (model:max_virtual_equity_per_instrument_percent=20.0), so target_pct must be set well
    below 20 to actually trim anything -- a target like 50 would never fire (position value
    never reaches it)."""
    return _rule(rid, _group(_leaf("profit_loss_percent", ">", gate_pct)),
                 [_action("decrease_instrument_share", target_pct)], continue_processing=True)


def confidence_gate(min_value, rid="gate_confidence_added"):
    return _leaf("confidence", ">=", min_value, rid=rid)


# ---------------------------------------------------------------------------
# Variants: name -> (entry_rules, exit_rules) transform
# Each receives DEEP COPIES of the baseline entry_rules/exit_rules lists and returns the
# (possibly entirely replaced) lists to actually run.
# ---------------------------------------------------------------------------
def _v_trail_only(pct):
    def fn(entry, exit_):
        # trail is exclusive (has_position gate) and is listed last, so it doubles as both
        # the initial floor stop AND the ongoing trail -- no separate floor_stop needed
        # (one would never be reached: trail's gate matches on every bar first).
        return entry, [EXIT_BEARISH, trail(-pct)]
    return fn


def _v_trail_after_breakeven(trail_pct, gate_pct=5.0):
    def fn(entry, exit_):
        return entry, [EXIT_BEARISH, breakeven_lock(gate_pct), trail(-trail_pct)]
    return fn


def _v_trail_with_cap(trail_pct, cap_pct):
    def fn(entry, exit_):
        return entry, [EXIT_BEARISH,
                        tier("cap", cap_pct - 5.0, sl_pct=cap_pct - 15.0, tp_pct=cap_pct),
                        trail(-trail_pct)]
    return fn


def _v_steps_5tier():
    def fn(entry, exit_):
        return entry, [
            EXIT_BEARISH,
            tier("t5", 32.0, sl_pct=20.0, tp_pct=48.0),
            tier("t4", 24.0, sl_pct=14.0),
            tier("t3", 18.0, sl_pct=8.0),
            tier("t2", 12.0, sl_pct=3.0),
            tier("t1", 6.0, sl_pct=0.5),
            floor_stop(-4.0),
        ]
    return fn


def _v_steps_3tier_earlier():
    def fn(entry, exit_):
        return entry, [
            EXIT_BEARISH,
            tier("t3", 20.0, sl_pct=15.0, tp_pct=40.0),
            tier("t2", 12.0, sl_pct=5.0),
            tier("t1", 5.0, sl_pct=0.5),
            floor_stop(-4.0),
        ]
    return fn


def _v_breakeven_at_3pct(trail_pct=None):
    """Evidence-driven: MFE analysis of TOP1's own 58 stop_loss trades showed 58.6% had
    already reached >= +3% unrealized gain before reversing all the way to the -4% floor
    (mean MFE of losers 7.0%, median 4.1%) -- the existing tier1 gate (>12%) is too late to
    catch most of them. Lock breakeven right where the data says the majority of eventual
    losers already were."""
    def fn(entry, exit_):
        rules = [EXIT_BEARISH, breakeven_lock(3.0, lock_pct=0.3)]
        if trail_pct is not None:
            rules.append(trail(-trail_pct))  # exclusive + gates on has_position -> no floor_stop needed
        else:
            rules.append(tier("tier3", 24.0, sl_pct=18.0, tp_pct=48.0))
            rules.append(floor_stop(-4.0))
        return entry, rules
    return fn


def _v_floor(pct):
    def fn(entry, exit_):
        new_exit = copy.deepcopy(exit_)
        for r in new_exit:
            if r["id"] == "exit_stoploss":
                r["actions"][0]["action_value"] = pct
        return entry, new_exit
    return fn


def _v_partial_scaleout(gate_pct, target_pct):
    def fn(entry, exit_):
        new_exit = copy.deepcopy(exit_)
        # Insert scale-out BEFORE the existing tiers so it fires first (continue_processing=True
        # lets evaluation still reach the tier/floor rules the same bar -- safe because
        # decrease_instrument_share is a different action type than the tiers' SL/TP actions).
        new_exit.insert(1, scaleout(gate_pct, target_pct))
        return entry, new_exit
    return fn


def _v_partial_scaleout_and_trail(gate_pct, target_pct, trail_pct):
    def fn(entry, exit_):
        return entry, [EXIT_BEARISH, scaleout(gate_pct, target_pct), trail(-trail_pct)]
    return fn


def _v_entry_confidence(min_value):
    def fn(entry, exit_):
        new_entry = copy.deepcopy(entry)
        new_entry[0]["conditions"]["conditions"].append(confidence_gate(min_value))
        return new_entry, exit_
    return fn


def _v_entry_confidence_plus_trail(min_value, trail_pct):
    def fn(entry, exit_):
        new_entry = copy.deepcopy(entry)
        new_entry[0]["conditions"]["conditions"].append(confidence_gate(min_value))
        return new_entry, [EXIT_BEARISH, trail(-trail_pct)]
    return fn


def _v_lower_cap(cap_pct, trail_pct=None):
    def fn(entry, exit_):
        rules = [EXIT_BEARISH, tier("tier1", 12.0, sl_pct=1.0, tp_pct=cap_pct)]
        if trail_pct is not None:
            rules.append(trail(-trail_pct))  # exclusive + has_position gate -> no floor_stop needed
        else:
            rules.append(floor_stop(-4.0))
        return entry, rules
    return fn


VARIANTS = {
    # -- Control: baseline TOP1 rules unmodified, run through the SAME pipeline as every
    # other variant -- used to check whether the min_tp_sl_percent sign-bug fix (TradeActions.py
    # AdjustStopLossAction._enforce_minimum_distance) actually moves baseline's own numbers
    # relative to the originally-persisted backtest 324 (177.41% / 32.22% win / -13.54% DD /
    # calmar 3.00 / sharpe 2.28 / 90 trades). --
    "baseline_control": lambda entry, exit_: (entry, exit_),

    # -- Group A: true percent-trailing stop instead of the 2-tier fixed staircase --
    "trail_only_2pct":        _v_trail_only(2.0),
    "trail_only_3pct":        _v_trail_only(3.0),
    "trail_only_5pct":        _v_trail_only(5.0),
    "trail_after_breakeven":  _v_trail_after_breakeven(trail_pct=3.0, gate_pct=5.0),
    "trail_with_cap_60":      _v_trail_with_cap(trail_pct=4.0, cap_pct=60.0),
    "breakeven_3pct_keep_tier3": _v_breakeven_at_3pct(trail_pct=None),
    "breakeven_3pct_trail":      _v_breakeven_at_3pct(trail_pct=3.0),

    # -- Group B: finer step tiers (smoother staircase than baseline's 12/24) --
    "steps_5tier":            _v_steps_5tier(),
    "steps_3tier_earlier":    _v_steps_3tier_earlier(),
    "steps_tighten_floor":    _v_floor(-2.0),
    "steps_loosen_floor":     _v_floor(-6.0),

    # -- Group C: partial profit-taking (lock in some gain, let remainder ride).
    # target_pct is a TARGET percent of virtual equity (not a relative cut) -- S4 caps any
    # single position at 20% of equity (max_virtual_equity_per_instrument_percent), so these
    # must sit well below 20 to actually trim anything (e.g. 12% trims a full-size 20%
    # position down by 40%; a smaller position no-ops until it grows past 12%). --
    "partial_scaleout_15":         _v_partial_scaleout(15.0, 12.0),
    "partial_scaleout_10_trail":   _v_partial_scaleout_and_trail(10.0, 12.0, 4.0),

    # -- Group D: entry selectivity (re-add the confidence gate this genome dropped) --
    "entry_confidence_60":         _v_entry_confidence(60.0),
    "entry_confidence_70":         _v_entry_confidence(70.0),
    "entry_confidence_plus_trail": _v_entry_confidence_plus_trail(60.0, 3.0),

    # -- Group E: lower/earlier profit cap (less "all-or-nothing at 48%") --
    "lower_cap_30":            _v_lower_cap(30.0),
    "lower_cap_20_tight_trail": _v_lower_cap(20.0, trail_pct=2.5),
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=False)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--start", default=None, help="Override bt_block start date (ISO).")
    ap.add_argument("--end", default=None, help="Override bt_block end date (ISO).")
    ap.add_argument("--suffix", default="", help="Appended to the persisted run name.")
    args = ap.parse_args()

    if args.list or not args.variant:
        print("Available variants:")
        for name in VARIANTS:
            print(" -", name)
        return 0

    if args.variant not in VARIANTS:
        print(f"Unknown variant {args.variant!r}. Use --list.")
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    bt = conn.execute("SELECT * FROM backtests WHERE id=?", (_BASE_BACKTEST_ID,)).fetchone()
    opt = conn.execute("SELECT optimization_config FROM strategy_optimizations WHERE id=?",
                       (_BASE_OPT_ID,)).fetchone()
    conn.close()

    sp = json.loads(bt["strategy_params"])
    cfg = json.loads(opt["optimization_config"])
    bt_block = dict(cfg["backtest"])
    if args.start:
        bt_block["start_date"] = args.start
    if args.end:
        bt_block["end_date"] = args.end

    base_entry = copy.deepcopy(sp["entryRules"])
    base_exit = copy.deepcopy(sp["exitRules"])
    entry_rules, exit_rules = VARIANTS[args.variant](base_entry, base_exit)

    overrides = {k[len("model:"):]: v for k, v in sp.items() if k.startswith("model:")}
    schedule = {k[len("schedule:"):]: bool(v) for k, v in sp.items() if k.startswith("schedule:")}
    screener_overrides = {k[len("screener:"):]: v for k, v in sp.items() if k.startswith("screener:")}

    from app.services.strategy_optimization_handler import (
        _build_daily_trial_config, _build_hoisted_state,
    )
    from app.services.backtest.daily_backtest_handler import run_daily_backtest, _persist_results
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal

    hoisted = _build_hoisted_state(bt_block) if bt_block.get("screener_opt") else None
    decoded = {
        "expert_overrides": overrides,
        "screener_overrides": screener_overrides,
        "schedule_days": schedule or None,
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
    }
    trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
    run_name = f"VAR-{args.variant}-S4TOP1{args.suffix}"
    trial_cfg["name"] = run_name

    print(f"=== running {run_name} ({bt_block['start_date']}..{bt_block['end_date']}) ===")
    results = run_daily_backtest(trial_cfg)

    db = SessionLocal()
    try:
        row = Backtest(
            name=run_name, model_id=None, engine_type="daily_expert",
            expert_name="FMPRating", optimization_id=None,
            strategy_params={**{k: v for k, v in sp.items() if not k.startswith(("entry", "exit"))},
                             "entryRules": entry_rules, "exitRules": exit_rules,
                             "variant": args.variant, "baseBacktestId": _BASE_BACKTEST_ID},
            start_date=_dt.fromisoformat(str(bt_block["start_date"])),
            end_date=_dt.fromisoformat(str(bt_block["end_date"])),
            initial_capital=float(bt_block["initial_capital"]),
            status="running", started_at=_dt.now(),
        )
        db.add(row); db.commit(); db.refresh(row)
        _persist_results(db, row, results)
        row.status = "completed"; row.completed_at = _dt.now(); row.is_saved = True
        db.commit()
        print(f"persisted backtest id={row.id}")
    finally:
        db.close()

    print(f"total_return:   {results.get('total_return')}%")
    print(f"annualized:     {results.get('annualized_return')}%")
    print(f"max_drawdown:   {results.get('max_drawdown')}%")
    print(f"calmar_ratio:   {results.get('calmar_ratio')}")
    print(f"sharpe_ratio:   {results.get('sharpe_ratio')}")
    print(f"total_trades:   {results.get('total_trades')}")
    print(f"win_rate:       {results.get('win_rate')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
