"""The PREMIUM style: the wheel, the overlays, assigned stock and concentration.

Plan docs/plans/2026-09-25-option-stage2-build.md, Task 9, as revised:
* "Added after Tasks 3-4" -- premium holds stock + options: O_CC is a member (E1), and O_JL /
  O_RS / O_CSP sit beside O_WHEEL because the hold is member-scoped (E2);
* "Tasks 5-6 as built" -- EVERY exit is scoped to its member, the wheel's stock-phase chain and
  O_CC's guard+overlay pair included (the delivered stock inherits the put's tag, an overlay the
  covered position's).

So the plan's original "the stock-phase chain stays UNSCOPED" and "no other premium member can
produce assigned stock" are both FLIPPED here: the chain is scoped to the wheel, and the members
that can deliver stock are named (``_STYLE_ASSIGNS_STOCK``, derived below from the settlement
path) and liquidate, while only the wheel's assignment is held.
"""
import inspect
from pathlib import Path

import pytest

from tests.test_option_grid_foundations import _launcher

_CAND = str(Path(__file__).resolve().parents[3] / "tools/option_styles.candidates.json")
_WHEEL_CHAIN = ["o_wheel-cc_dte", "o_wheel-cc_guard", "o_wheel-cc_sell",
                "o_wheel-wheel_stock_guard"]


def _premium(m):
    m._STYLE_ACTIVE = m._load_style_spec(_CAND, "premium")
    return m._build_strategy_style("O_STYLE")


def _leaves(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if "field" in node:
            out.append(node)
        for c in node.get("conditions") or []:
            _leaves(c, out)
    return out


def _tags(rule):
    return [x for x in _leaves(rule["conditions"]) if x["field"] == "entry_tag"]


# --- the wheel's stock phase and O_CC's overlay -------------------------------------------------
def test_the_wheel_overlay_chain_is_intact_scoped_to_the_wheel_and_in_its_order():
    m = _launcher()
    s = _premium(m)
    ids = [r["id"] for r in s.exit_rules]
    pos = [ids.index(i) for i in _WHEEL_CHAIN]
    assert pos == sorted(pos) and pos == list(range(pos[0], pos[0] + len(pos))), pos
    wheel = float(m._STYLE_MEMBER_TAGS["O_WHEEL"])
    for i in _WHEEL_CHAIN:
        (tag,) = _tags(s.exit_rules[ids.index(i)])
        assert (tag["op"], tag["value"]) == ("==", wheel), i
    # the wheel's CSP-phase closes come AFTER its stock guard (its own order) and are scoped too
    assert ids.index("o_wheel-opt_tp") > ids.index("o_wheel-wheel_stock_guard")
    assert [t["value"] for t in _tags(s.exit_rules[ids.index("o_wheel-opt_tp")])] == [wheel]


def test_the_covered_call_is_written_only_against_the_WHEELS_assigned_shares():
    """``has_assigned_shares`` AND ``entry_tag == wheel``: the delivered lot carries the put's
    tag, so a CSP / JL / RS member's lot -- held one bar before its liquidation -- never gets a
    call written on it."""
    m = _launcher()
    s = _premium(m)
    cc_sell = next(r for r in s.exit_rules if r["id"] == "o_wheel-cc_sell")
    assert [x["field"] for x in _leaves(cc_sell["conditions"])] == ["has_assigned_shares",
                                                                    "entry_tag"]
    assert _tags(cc_sell)[0]["value"] == float(m._STYLE_MEMBER_TAGS["O_WHEEL"])


def test_O_CCs_guard_and_overlay_are_scoped_to_O_CC():
    """O_CC's ``cc_sell`` reads ``has_position``, which is true for ANY member's position on the
    symbol; its tag is what keeps it off the wheel's (or anyone else's) shares."""
    m = _launcher()
    s = _premium(m)
    cc = float(m._STYLE_MEMBER_TAGS["O_CC"])
    for rid in ("o_cc-cc_guard", "o_cc-cc_sell", "o_cc-cc_dte"):
        rule = next(r for r in s.exit_rules if r["id"] == rid)
        assert [t["value"] for t in _tags(rule)] == [cc], rid


# --- who can put stock on the book, and who keeps it -------------------------------------------
#: action type -> (the option_strategy its builder BOOKS, the action class). Each strategy is
#: checked against the class source below, so a renamed booking fails here rather than silently
#: moving a structure between the unit-settled and the per-leg path.
_BOOKED = {
    "buy_call": ("long_call", "BuyCallAction"),
    "buy_put": ("long_put", "BuyPutAction"),
    "open_bull_call_spread": ("bull_call_spread", "OpenBullCallSpreadAction"),
    "open_bear_put_spread": ("bear_put_spread", "OpenBearPutSpreadAction"),
    "sell_cash_secured_put": ("cash_secured_put", "SellCashSecuredPutAction"),
    "open_bear_call_spread": ("bear_call_spread", "OpenBearCallSpreadAction"),
    "open_bull_put_spread": ("bull_put_spread", "OpenBullPutSpreadAction"),
    "open_straddle": ("straddle", "OpenStraddleAction"),
    "open_strangle": ("strangle", "OpenStrangleAction"),
    "open_iron_condor": ("iron_condor", "OpenIronCondorAction"),
    "open_jade_lizard": ("jade_lizard", "OpenJadeLizardAction"),
    "open_call_butterfly": ("call_butterfly", "OpenCallButterflyAction"),
    "open_put_ratio_spread": ("put_ratio_spread", "OpenPutRatioSpreadAction"),
    "open_call_backspread": ("call_backspread", "OpenCallBackspreadAction"),
    "open_put_backspread": ("put_backspread", "OpenPutBackspreadAction"),
    "open_pmcc": ("pmcc", "OpenPMCCAction"),
    "roll_pmcc_short": ("pmcc_roll", "RollPMCCShortAction"),
    "sell_covered_call": ("covered_call", "SellCoveredCallAction"),
    "buy_protective_put": ("protective_put", "BuyProtectivePutAction"),
}
#: A covered call's short leg is covered by the member's own SHARES: ITM at expiry it is
#: called away -- a CLOSING delivery of stock already held, never new stock (pinned end to end
#: by test_member_scoped_assignment_hold's C1 tests). Every other short leg outside a
#: defined-risk combo is assigned per leg INTO stock.
_SHARE_COVERED = {"covered_call"}


def _class_source(cls):
    from ba2_common.core import TradeActions as TA
    return "".join(inspect.getsource(c) for c in cls.__mro__
                   if c.__module__ == TA.__name__ and c is not TA._OptionEntryAction
                   and c is not TA.TradeAction and issubclass(c, TA.TradeAction))


def _opening_short_leg(action_type):
    """(booked strategy, opens a SHORT leg?) -- the short leg read off the class SOURCE
    (``sell_to_open``), not restated."""
    from ba2_common.core import TradeActions as TA
    strategy, cls_name = _BOOKED[action_type]
    cls = getattr(TA, cls_name)
    src = _class_source(cls)
    booked = getattr(cls, "OPTION_STRATEGY", None)
    assert booked == strategy or f'"{strategy}"' in src or (
        strategy == "pmcc_roll" and "PMCC_ROLL_STRATEGY" in src), (action_type, strategy)
    return strategy, "sell_to_open" in src


def _assigns_stock_per_settlement_path(m, key):
    """Does member ``key`` open a short leg that the backtest settles PER LEG (so an ITM expiry
    is physically assigned into stock -- ``settle_single_leg_expiry``: "SHORT ITM: ALWAYS
    physical assignment at the strike, covered or naked"), rather than as a unit-settled
    defined-risk combo (``BacktestAccount.defined_risk_combo_strategy``)?"""
    from app.services.backtest.backtest_account import BacktestAccount
    from ba2_common.core.types import get_option_entry_action_values

    combos = (BacktestAccount.DEFINED_RISK_LONG_STRATEGIES
              | BacktestAccount.DEFINED_RISK_SHORT_STRATEGIES)
    opening = set(get_option_entry_action_values()) | {"roll_pmcc_short"}
    built = m._STRATEGY_BUILDERS[key](key)
    types = {str(a.get("action_type") or a.get("action") or "")
             for r in (built.entry_rules or []) + (built.exit_rules or [])
             for a in (r.get("actions") or [])}
    types |= {str(r["action_type"]) for r in (built.exit_rules or []) if r.get("action_type")}
    out = False
    for at in sorted(types & opening):
        assert at in _BOOKED, f"{key}: unmapped option-opening action {at!r}"
        strategy, short = _opening_short_leg(at)
        if short and strategy not in combos and strategy not in _SHARE_COVERED:
            out = True
    return out


def test_the_style_assigns_stock_set_matches_the_settlement_path():
    """``_STYLE_ASSIGNS_STOCK`` is exactly the admissible members whose structures the backtest
    settles per leg with a short leg -- jade_lizard / put_ratio_spread / the backspreads / the
    PMCC short call are NOT defined-risk combos, cash_secured_put is single-leg -- derived here
    from the action classes and the account's combo sets, never restated."""
    m = _launcher()
    derived = {k for k in m._style_admissible_members()
               if _assigns_stock_per_settlement_path(m, k)}
    assert derived == m._STYLE_ASSIGNS_STOCK, (
        f"missing {sorted(derived - m._STYLE_ASSIGNS_STOCK)}, "
        f"extra {sorted(m._STYLE_ASSIGNS_STOCK - derived)}")


def test_premium_holds_ONLY_the_wheels_assignments_and_liquidates_the_others():
    """The E2 mechanism (pinned end to end in test_member_scoped_assignment_hold) applied to the
    BUILT premium style: its run holds the wheel's tag alone, so the CSP / JL / RS members'
    assignments are liquidated at the next open, while the other members cannot deliver stock."""
    from ba2_common.core.assignment_hold import (
        holds_assigned_stock, resolve_assigned_stock_hold_policy)
    m = _launcher()
    m._STYLE_ACTIVE = m._load_style_spec(_CAND, "premium")
    members = [x["key"] for x in m._STYLE_ACTIVE["members"]]
    acct = m._hold_assigned_stock_account_settings("O_STYLE")
    policy = resolve_assigned_stock_hold_policy(acct)
    assigning = [k for k in members if k in m._STYLE_ASSIGNS_STOCK]
    assert sorted(assigning) == ["O_CSP", "O_JL", "O_RS", "O_WHEEL"]
    for k in assigning:
        held = holds_assigned_stock(policy, m._STYLE_MEMBER_TAGS[k], where=k)
        assert held is (k == "O_WHEEL"), k
    for k in set(members) - set(assigning):
        assert not _assigns_stock_per_settlement_path(m, k), k


# --- concentration ------------------------------------------------------------------------------
def _leg(txn, sym, t_in, t_out, pnl, kind="P", strike=100.0, reason="closed", k=0):
    return {"symbol": sym, "underlying_symbol": sym, "contract_symbol": f"{sym}{kind}{txn}{k}",
            "transaction_id": txn, "pnl": pnl, "pnl_pct": pnl / 1000.0, "strike": strike,
            "entry_time": t_in, "exit_time": t_out, "entry_price": 2.0, "size": 1.0,
            "exit_reason": reason}


def _lot(txn, sym, t_in, t_out, pnl, px):
    return {"symbol": sym, "transaction_id": txn, "pnl": pnl, "pnl_pct": pnl / 1000.0,
            "entry_time": t_in, "exit_time": t_out, "entry_price": px, "size": 100.0,
            "exit_reason": "assignment"}


def test_concentration_groups_the_wheel_cycle_as_one_bet_and_other_members_apart():
    """``strategy_fitness._option_structure_groups`` over a synthetic premium book:
      * the wheel: CSP assigned at the strike -> the stock lot -> a covered call overlapping the
        lot -> called away == ONE bet;
      * an O_BULLPS spread (two legs, one transaction) on another symbol == its own bet;
      * an O_IC on the SAME symbol as the wheel, opened after the lot closed == its own bet;
      * an O_CSP member's put assigned on a third symbol and liquidated at the next open == one
        bet (the put settled INTO the lot at its strike)."""
    from app.services import strategy_fitness as F
    rows = [
        # wheel on AAA: 0 put, 1 lot, 2 covered call
        _leg(1, "AAA", "2021-01-04T14:30:00", "2021-01-15T21:00:00", 150.0, strike=100.0,
             reason="assigned"),
        _lot(2, "AAA", "2021-01-15T21:00:00", "2021-02-19T21:00:00", 300.0, px=100.0),
        _leg(3, "AAA", "2021-01-19T14:30:00", "2021-02-19T21:00:00", -80.0, kind="C",
             strike=104.0, reason="assigned"),
        # O_BULLPS on BBB: 3, 4 (one transaction)
        _leg(4, "BBB", "2021-01-05T14:30:00", "2021-01-29T21:00:00", 90.0, strike=50.0, k=0),
        _leg(4, "BBB", "2021-01-05T14:30:00", "2021-01-29T21:00:00", -30.0, strike=45.0, k=1),
        # O_IC on AAA after the lot closed: 5..8 (one transaction)
        *[_leg(5, "AAA", "2021-02-22T14:30:00", "2021-03-19T21:00:00", p, kind=kd, strike=s, k=i)
          for i, (p, kd, s) in enumerate([(60.0, "P", 95.0), (-20.0, "P", 90.0),
                                          (50.0, "C", 110.0), (-15.0, "C", 115.0)])],
        # O_CSP on CCC: 9 put assigned at 40, 10 lot liquidated at the next open
        _leg(6, "CCC", "2021-01-04T14:30:00", "2021-01-15T21:00:00", 70.0, strike=40.0,
             reason="assigned"),
        _lot(7, "CCC", "2021-01-15T21:00:00", "2021-01-19T14:30:00", -110.0, px=40.0),
    ]
    groups = F._option_structure_groups(rows)
    assert groups == [[0, 1, 2], [3, 4], [5, 6, 7, 8], [9, 10]], groups
    assert F._structure_count(rows, option_structures=True) == 4
