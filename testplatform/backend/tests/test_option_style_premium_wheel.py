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
from datetime import date
from pathlib import Path
from types import SimpleNamespace

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
#: A covered call's short leg is covered by the member's own SHARES: ITM at expiry it is
#: called away -- a CLOSING delivery of stock already held, never new stock (pinned end to end
#: by test_member_scoped_assignment_hold's C1 tests). Every other short leg outside a
#: defined-risk combo is assigned per leg INTO stock.
_SHARE_COVERED = {"covered_call"}
#: An opening action that does not open a structure of its own: it re-opens a leg ON another
#: action's transaction, so its settlement path is that transaction's. ``roll_pmcc_short``
#: writes the next short call on the PMCC's transaction, and the backtest decides per-leg vs
#: unit settlement from the transaction's ENTRY order (``defined_risk_combo_strategy`` ->
#: ``_entry_order_for_transaction``: the ``pmcc`` parent), never from the roll's own row.
_RIDES_ON = {"roll_pmcc_short": "open_pmcc"}

_TODAY = date(2024, 6, 3)
_NEAR, _LEAP = date(2024, 7, 3), date(2025, 7, 7)            # 30 and 399 DTE
#: Builder kwargs that make every opening action resolve on ``_ChainAccount``'s chain. The
#: STRIKES and WINDOWS are irrelevant to the question (which legs are SHORT, and what the
#: structure is booked as); they only have to select something.
_GENERIC_KW = dict(strike_method="percent_otm", strike_param=5.0, wing_width_pct=5.0,
                   dte_min=10, dte_max=45, sizing=1.0)
_BUILD_KW = {"open_pmcc": dict(_GENERIC_KW, strike_param={"long": -10.0, "short": 5.0},
                               dte_min=365, dte_max=450,
                               short_dte_min=10, short_dte_max=45)}


def _chain_account():
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    from ba2_common.core.option_types import OptionContract
    from ba2_common.core.types import OptionRight

    class _ChainAccount(OptionsAccountInterface):
        """A chain for both rights at a near and a LEAPS expiry, a deep balance, no book.
        Premium = intrinsic + a time value that decays away from the money (and is larger on
        the LEAPS), so credit structures earn a credit and debit ones cost a debit."""

        SPOT = 100.0

        def __init__(self):
            self.id = 1

        def _as_of_date(self):
            return _TODAY

        def decision_label(self):
            return _TODAY

        def get_balance(self):
            return 50_000_000.0

        def get_tradable_balance(self):
            return self.get_balance()

        def get_option_tradable_balance(self):
            return self.get_balance()

        def available_option_buying_power(self):
            return self.get_balance()

        def get_account_snapshot(self):
            from ba2_common.core.account_types import AccountSnapshot
            b = self.get_balance()
            return AccountSnapshot(cash=b, equity=b, net_liquidation=b)

        def get_instrument_current_price(self, symbol, price_type=None):
            return self.SPOT

        def get_current_price(self, symbol=None):
            return self.SPOT

        def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                             strike_min=None, strike_max=None):
            out = []
            for expiry in (_NEAR, _LEAP):
                if (expiry_min and expiry < expiry_min) or (expiry_max and expiry > expiry_max):
                    continue
                for k in range(50, 151, 5):
                    call = option_type == OptionRight.CALL
                    otm = max((k - self.SPOT) if call else (self.SPOT - k), 0.0)
                    intrinsic = max((self.SPOT - k) if call else (k - self.SPOT), 0.0)
                    tv = (5.0 if expiry == _NEAR else 14.0) * (0.9 ** (otm / 5.0))
                    bid = round(tv + intrinsic, 4)
                    out.append(OptionContract(
                        symbol=f"{underlying}{expiry:%y%m%d}{'C' if call else 'P'}{k:05d}",
                        underlying=underlying, option_type=option_type, strike=float(k),
                        expiry=expiry, bid=bid, ask=round(bid + 0.05, 4), last=bid,
                        open_interest=10_000, volume=10_000))
            return out

        def open_option_orders_book_wide(self):
            return []

        def get_option_positions(self):
            return []

        def get_option_quote(self, contract_symbol):
            return None

        def get_atm_implied_volatility(self, underlying):
            return 0.3

        def check_option_buying_power(self, required):
            return True

        def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
            return trading_order

        def close_option_position(self, position, order_type="limit", limit_price=None):
            return None

    return _ChainAccount()


_BUILT: dict = {}


def _built_structure(action_type):
    """``(booked option_strategy, [OptionLeg])`` of the structure ``action_type`` OPENS, read off
    a real build: the action runs against ``_ChainAccount`` and the legs it hands to the shared
    ``_submit_option_order`` choke point are captured there. A leg is short because the builder
    made it ``side=SELL, position_intent="sell_to_open"`` -- nothing reads source text, so no
    comment or docstring can move a structure between the settlement paths."""
    if action_type in _BUILT:
        return _BUILT[action_type]
    from app.services.backtest.backtest_db import backtest_trading_db
    from ba2_common.core import TradeActions as TA
    from ba2_common.core.types import ExpertActionType

    captured = []

    def capture(self, legs, quantity, limit_price, option_strategy, *a, **k):
        captured.append((option_strategy, list(legs)))
        return {"success": True, "message": "captured", "data": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(TA._OptionEntryAction, "_submit_option_order", capture)
        # the share overlays (covered call / protective put) need 100 held, uncommitted shares
        mp.setattr(TA._OptionEntryAction, "_held_equity_shares", lambda self: 100.0)
        mp.setattr(TA._OptionEntryAction, "_refuse_if_cover_is_short",
                   lambda self, *a, **k: None)
        rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                              expected_profit_percent=None, recommended_action=None)
        with backtest_trading_db(f"m4-build-{action_type}"):
            action = TA.create_action(ExpertActionType(action_type), "XYZ", _chain_account(),
                                      SimpleNamespace(), None, rec,
                                      **_BUILD_KW.get(action_type, _GENERIC_KW))
            action.submit_to_broker = True
            result = action.execute()
    assert len(captured) == 1, (
        f"{action_type} built {len(captured)} structures on the test chain: "
        f"{getattr(result, 'message', result)}")
    _BUILT[action_type] = captured[0]
    return captured[0]


def _opens_a_short_leg(legs):
    from ba2_common.core.types import OrderDirection
    return any(leg.side == OrderDirection.SELL and leg.position_intent == "sell_to_open"
               for leg in legs)


def _assigns_stock_per_settlement_path(m, key):
    """Does member ``key`` open a short leg that the backtest settles PER LEG (so an ITM expiry
    is physically assigned into stock -- ``settle_single_leg_expiry``: "SHORT ITM: ALWAYS
    physical assignment at the strike, covered or naked"), rather than as a unit-settled
    defined-risk combo (``BacktestAccount.defined_risk_combo_strategy``)? Derived from the
    structures the member's opening actions actually BUILD."""
    from app.services.backtest.backtest_account import BacktestAccount
    from ba2_common.core.types import get_option_entry_action_values

    combos = (BacktestAccount.DEFINED_RISK_LONG_STRATEGIES
              | BacktestAccount.DEFINED_RISK_SHORT_STRATEGIES)
    opening = set(get_option_entry_action_values()) | set(_RIDES_ON)
    built = m._STRATEGY_BUILDERS[key](key)
    types = {str(a.get("action_type") or a.get("action") or "")
             for r in (built.entry_rules or []) + (built.exit_rules or [])
             for a in (r.get("actions") or [])}
    types |= {str(r["action_type"]) for r in (built.exit_rules or []) if r.get("action_type")}
    out = False
    for at in sorted(types & opening):
        if at in _RIDES_ON:
            assert _RIDES_ON[at] in types, (
                f"{key}: {at} rides on {_RIDES_ON[at]}'s transaction, which {key} never opens")
            continue
        strategy, legs = _built_structure(at)
        short = _opens_a_short_leg(legs)
        if short and strategy not in combos and strategy not in _SHARE_COVERED:
            out = True
    return out


def test_the_style_assigns_stock_set_matches_the_settlement_path():
    """``_STYLE_ASSIGNS_STOCK`` is exactly the admissible members whose structures the backtest
    settles per leg with a short leg -- jade_lizard / put_ratio_spread / the backspreads / the
    PMCC short call are NOT defined-risk combos, cash_secured_put is single-leg -- derived here
    from the legs each member's opening actions BUILD (``_built_structure``) and the account's
    combo sets, never restated and never read off source text."""
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
