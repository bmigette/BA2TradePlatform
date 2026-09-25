"""E2: a WHEEL member and a CSP member in ONE run -- each assignment follows its own member.

Plan 2026-09-25 option-stage2-build, "Revisions after the user's answers", Task E2. The
assigned-stock hold used to be ONE run-level switch (``hold_assigned_stock``), so a style run
holding the wheel would also have held -- and covered-called -- every CSP / jade-lizard /
ratio-spread assignment, turning each of them into a second wheel. Now the run names the
members that hold (``hold_assigned_stock_tags``) and the shared decider
(``ba2_common.core.assignment_hold``) answers per assignment from the ENTRY TAG of the option
position that was assigned:

* the wheel member's assigned shares are HELD and a covered call is written on them;
* the CSP member's assigned shares are LIQUIDATED at the next bar's open, as always, and no
  call is ever written on them -- the overlay is scoped to the wheel member by an
  ``entry_tag`` leaf, which works because the delivered stock inherits the put's tag;
* an assignment with no tag in such a run is refused and ends the run.

Every test runs the REAL ``DailyBacktestEngine.run()`` over a fixture options cache: two
symbols, the expert confident on one (the wheel member's entry rule fires there) and lukewarm
on the other (the CSP member's rule fires), both puts finishing ITM on the same expiry.

Run from the backend dir:
    python -m pytest tests/backtest/test_member_scoped_assignment_hold.py -q
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta

import pytest

from tests.backtest._no_atr import NoAtrIndicatorProvider
from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderDirection, OrderRecommendation, OrderStatus, Recommendation

#: Member tags (arbitrary positive ints; the real table is the launcher's _STYLE_MEMBER_TAGS).
TAG_WHEEL, TAG_CSP = 7, 3

START = datetime(2024, 1, 2)
END = datetime(2024, 3, 29)
PUT_EXPIRY = START.date() + timedelta(days=21)
CALL_EXPIRY = START.date() + timedelta(days=55)

WHL, CSP = "WHLX", "CSPX"
PUTS = {sym: f"{sym}{PUT_EXPIRY:%y%m%d}P00020000" for sym in (WHL, CSP)}
CALLS = {sym: f"{sym}{CALL_EXPIRY:%y%m%d}C00019000" for sym in (WHL, CSP)}
CONFIDENCE = {WHL: 90.0, CSP: 60.0}

BASE_CFG = {
    **_LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}


def _weekdays(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(START.date(), END.date())


def _spot_on(d: date) -> float:
    """20 until the puts' expiry week, then 18: both 20-strike puts finish ITM and are
    assigned; the 19-strike call written afterwards stays OTM."""
    return 20.0 if d < PUT_EXPIRY - timedelta(days=3) else 18.0


def _called_away_spot_on(d: date) -> float:
    """As ``_spot_on`` through the put phase and the call's writing, then 22 from the first of
    February: the 19-strike covered call finishes ITM and the shares are CALLED AWAY."""
    return 22.0 if d >= date(2024, 2, 1) else _spot_on(d)


def _bars(spot_on=_spot_on):
    return [{"Date": d, "Open": spot_on(d), "High": spot_on(d) + 0.1,
             "Low": spot_on(d) - 0.1, "Close": spot_on(d), "Volume": 1_000_000}
            for d in DAYS]


def _chain(sym):
    return [
        {"occ_symbol": PUTS[sym], "option_type": "put", "strike": 20.0,
         "expiry": PUT_EXPIRY.isoformat(), "bid": 0.50, "ask": 0.50, "last": 0.50,
         "iv": 0.30, "delta": -0.30, "open_interest": 5000},
        {"occ_symbol": CALLS[sym], "option_type": "call", "strike": 19.0,
         "expiry": CALL_EXPIRY.isoformat(), "bid": 0.60, "ask": 0.60, "last": 0.60,
         "iv": 0.30, "delta": 0.25, "open_interest": 5000},
    ]


def _option_bars():
    rows = []
    for sym in (WHL, CSP):
        # The put premium is FLAT, so nothing but its own expiry ends the put phase.
        for occ, ot, strike, expiry, px in ((PUTS[sym], "put", 20.0, PUT_EXPIRY, 0.50),
                                            (CALLS[sym], "call", 19.0, CALL_EXPIRY, 0.60)):
            for d in DAYS:
                if d > expiry:
                    continue
                rows.append({"occ_symbol": occ, "date": d.isoformat(), "open": px,
                             "high": px + 0.05, "low": px - 0.05, "close": px, "volume": 500,
                             "underlying": sym, "option_type": ot, "strike": strike,
                             "expiry": expiry.isoformat(), "iv": 0.30,
                             "delta": -0.30 if ot == "put" else 0.25})
    return rows


class _TwoMemberExpert(MarketExpertInterface):
    """BUY on both symbols with a per-symbol confidence, so each fires its own member's rule."""

    bypasses_classic_rm = False
    _gather_symbol = None

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub two-member BUY expert for the member-scoped assignment hold test."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        symbol = self._gather_symbol
        assert symbol in CONFIDENCE, f"unexpected symbol {symbol!r}"
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=CONFIDENCE[symbol],
            current_price=context.account.get_instrument_current_price(symbol),
            details="buy", expected_profit_percent=25.0, raw_outputs={})


def _csp_action(tag):
    action = {"action_type": "sell_cash_secured_put", "option_strike_method": "percent_otm",
              "option_strike_param": 0.0, "option_dte_min": 14, "option_dte_max": 30,
              "option_sizing": 5.0}
    if tag is not None:
        action["option_entry_tag"] = tag
    return action


def _entry_rules(wheel_tag=TAG_WHEEL, csp_tag=TAG_CSP):
    def gate(op):
        return {"type": "AND", "conditions": [
            {"id": "conf", "field": "confidence", "op": op, "value": 80.0},
            {"id": "flat", "field": "has_no_position"}]}
    return [{"id": "wheel_entry", "conditions": gate(">="), "actions": [_csp_action(wheel_tag)]},
            {"id": "csp_entry", "conditions": gate("<"), "actions": [_csp_action(csp_tag)]}]


def _exit_rules():
    """The wheel member's stock-phase overlay, SCOPED to the wheel member by its tag (plus the
    two guards O_WHEEL emits). Without the scope the CSP member's assigned shares -- held for
    one bar before their liquidation -- would get a call written on them too."""
    return [
        {"id": "cc_guard", "conditions": {"type": "AND", "conditions": [
            {"id": "has_cc", "field": "has_covered_call"}]},
         "actions": [{"action_type": "stop_processing"}]},
        {"id": "cc_sell", "conditions": {"type": "AND", "conditions": [
            {"id": "assigned", "field": "has_assigned_shares"},
            {"id": "tag", "field": "entry_tag", "op": "==", "value": float(TAG_WHEEL)}]},
         "actions": [{"action_type": "sell_covered_call", "option_strike_method": "percent_otm",
                      "option_strike_param": 5.0, "option_dte_min": 20,
                      "option_dte_max": 60}],
         "continue_processing": True},
        {"id": "wheel_stock_guard", "conditions": {"type": "AND", "conditions": [
            {"id": "assigned2", "field": "has_assigned_shares"}]},
         "actions": [{"action_type": "stop_processing"}]},
    ]


def _run(account_id, cfg, entry_rules, spot_on=_spot_on):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_entry_ruleset_from_rules, seed_exit_ruleset_from_rules)
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.rule_models import normalize_trade_rules
    from ba2_common.core.trade_store import orders_where
    from tests.backtest.fixtures.e2e_support import hermetic_providers

    tmpdir = tempfile.mkdtemp(prefix="member-hold-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    for sym in (WHL, CSP):
        cache.write_chain_rows(sym, START.date().isoformat(), _chain(sym))
    cache.write_bar_rows(_option_bars())
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"member-hold-{account_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, cfg)
        enter_id = seed_entry_ruleset_from_rules(normalize_trade_rules(list(entry_rules)),
                                                 name=f"mh-enter-{account_id}")
        open_id = seed_exit_ruleset_from_rules(normalize_trade_rules(_exit_rules()),
                                               name=f"mh-open-{account_id}")
        seed_expert_instance(account_id=account_id, expert_class_name="_MemberHoldExpert",
                             enter_market_ruleset_id=enter_id,
                             open_positions_ruleset_id=open_id, instance_id=account_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        for sym in (WHL, CSP):
            ps.load_bars(sym, _bars(spot_on))
        ps.set_clock(START)
        account = BacktestAccount(account_id, ps, cfg, options_provider=provider)
        resolver.register_account(account_id, account)
        expert = _TwoMemberExpert(account_id)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "allow_automated_trade_modification": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(account_id, expert)
        config = {"start_date": START, "end_date": END, "enabled_instruments": [WHL, CSP],
                  "seed": 42}
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, account_id, expert.settings, enter_id)],
            price_source=ps, config=config, indicator_provider=NoAtrIndicatorProvider())
        with hermetic_providers():
            engine.run()
        return list(orders_where(account_id=account_id)), account
    finally:
        ctx.__exit__(None, None, None)


def _executed(orders):
    return [o for o in orders if o.status in OrderStatus.get_executed_statuses()]


def _stock(orders, sym):
    return [o for o in _executed(orders) if o.symbol == sym
            and not getattr(o, "contract_symbol", None) and not getattr(o, "option_strategy", None)]


def _held(account, sym):
    rows = [p for p in account.get_positions() if p["symbol"] == sym]
    return float(rows[0]["qty"]) if rows else 0.0


def _delivered_buy(orders, sym):
    """Shares the PUT's assignment delivered on ``sym``."""
    (row,) = [o for o in _stock(orders, sym)
              if o.comment == "option_assignment" and o.side is OrderDirection.BUY]
    return float(row.filled_qty or row.quantity)


def _delivered(orders, sym):
    """Shares the assignment delivered on ``sym`` (100 x the contracts the put sold)."""
    (row,) = [o for o in _stock(orders, sym) if o.comment == "option_assignment"]
    return float(row.filled_qty or row.quantity)


def _written_calls(orders, sym):
    return [o for o in _executed(orders) if getattr(o, "contract_symbol", None) == CALLS[sym]
            and o.side is OrderDirection.SELL]


MEMBER_CFG = {**BASE_CFG, "hold_assigned_stock_tags": [TAG_WHEEL]}


@pytest.fixture(scope="module")
def member_run():
    return _run(9301, MEMBER_CFG, _entry_rules())


def test_both_members_sold_a_put_and_both_were_assigned(member_run):
    orders, _ = member_run
    for sym in (WHL, CSP):
        assert [o for o in _executed(orders) if getattr(o, "contract_symbol", None) == PUTS[sym]
                and o.side is OrderDirection.SELL], f"{sym}: no put was sold"
        delivered = [o for o in _stock(orders, sym) if o.comment == "option_assignment"]
        assert delivered, f"{sym}: the put was never assigned -- the fixture proves nothing"


def test_the_wheel_members_assignment_is_HELD(member_run):
    orders, account = member_run
    assert _delivered(orders, WHL) > 0
    assert _held(account, WHL) == pytest.approx(_delivered(orders, WHL)), (
        "the wheel member's assigned shares were not held to the end of the run")
    assert not [o for o in _stock(orders, WHL) if o.comment == "assignment_liquidation"], (
        "the wheel member's assigned shares were liquidated")


def test_the_csp_members_assignment_is_LIQUIDATED_at_the_next_open(member_run):
    orders, account = member_run
    sold = [o for o in _stock(orders, CSP) if o.comment == "assignment_liquidation"]
    assert len(sold) == 1 and sold[0].side is OrderDirection.SELL, (
        f"the CSP member's assigned shares were not liquidated: "
        f"{[(o.comment, o.side) for o in _stock(orders, CSP)]}")
    assert (sold[0].filled_qty or sold[0].quantity) == pytest.approx(_delivered(orders, CSP))
    assert _held(account, CSP) == pytest.approx(0.0)


def test_a_covered_call_is_written_ONLY_on_the_wheel_members_shares(member_run):
    orders, _ = member_run
    assert len(_written_calls(orders, WHL)) == 1, (
        "the wheel member's overlay wrote no call on its held shares")
    assert _written_calls(orders, CSP) == [], (
        "a covered call was written on the CSP member's shares")


def test_the_delivered_stock_carries_the_assigned_puts_member_tag(member_run):
    from ba2_common.core.entry_tag import order_entry_tag

    orders, _ = member_run
    for sym, tag in ((WHL, TAG_WHEEL), (CSP, TAG_CSP)):
        (delivered,) = [o for o in _stock(orders, sym) if o.comment == "option_assignment"]
        assert order_entry_tag(delivered) == tag, (sym, delivered.data)


def test_an_UNTAGGED_assignment_in_a_member_scoped_run_ends_the_run():
    from ba2_common.core.assignment_hold import UntaggedAssignmentRefused

    with pytest.raises(UntaggedAssignmentRefused, match="no entry tag"):
        _run(9302, MEMBER_CFG, _entry_rules(csp_tag=None))


def test_without_a_tag_set_the_run_level_flag_still_decides_for_everyone():
    """No ``hold_assigned_stock_tags``: the old switch, applied to every assignment -- here ON,
    so BOTH members' shares are held, tagged or not (the pre-E2 wheel run)."""
    orders, account = _run(9303, {**BASE_CFG, "hold_assigned_stock": True},
                           _entry_rules(wheel_tag=None, csp_tag=None))
    for sym in (WHL, CSP):
        assert _delivered(orders, sym) > 0
        assert _held(account, sym) == pytest.approx(_delivered(orders, sym)), sym
        (delivered,) = [o for o in _stock(orders, sym) if o.comment == "option_assignment"]
        assert delivered.data is None, "an untagged assignment wrote order data"


# =========================================================================================== #
# the borrow charge follows the SAME per-assignment decision (account level)
#
# The short-selling merge charges borrow on short stock a run KEEPS from an assignment, and
# exempts short stock queued for next-open liquidation. A naked short call assigned ITM
# delivers SHORT stock: a member whose stock is held pays borrow on it, a member whose stock
# is liquidated does not -- in the same member-scoped configuration.
# =========================================================================================== #
def _naked_call_assigned(tmp_path, name, account_id, cfg, tag):
    """Write a naked 170 call (tagged ``tag``) and drive it to its ITM expiry (close 190)."""
    from ba2_common.core.db import update_instance
    from ba2_common.core.types import OptionRight
    from tests.backtest.test_wheel_assignment import (
        _CALL170, _EXP_CALL, _build, _engine, _leg)

    acct, ps, ctx = _build(tmp_path, name, account_id, cfg)
    ps.set_clock(datetime(2024, 3, 11))
    acct.submit_option_order(
        legs=[_leg(_CALL170, OrderDirection.SELL, "sell_to_open", OptionRight.CALL, 170.0,
                   _EXP_CALL)],
        quantity=1, order_type="market", option_strategy="naked_call")
    acct.refresh_orders()                          # fills at the call's 3/12 premium bar
    acct.refresh_transactions()
    # The option entry action stamps the tag on its entry row (entry_tag Task 1); this harness
    # submits directly, so stamp it the same way.
    (entry,) = [o for o in acct.get_orders() if o.contract_symbol == _CALL170]
    entry.data = {**(entry.data or {}), "entry_tag": tag}
    update_instance(entry)

    as_of = datetime(2024, 3, 28)
    ps.set_clock(as_of)
    _engine(acct, ps, cfg)._apply_option_expiry(as_of)
    acct.refresh_transactions()
    return acct, ctx, as_of


@pytest.mark.parametrize("tag, held", [(TAG_WHEEL, True), (TAG_CSP, False)])
def test_borrow_is_charged_exactly_on_the_assigned_short_stock_a_member_HOLDS(
        tmp_path, tag, held):
    cfg = {**BASE_CFG, "hold_assigned_stock_tags": [TAG_WHEEL], "short_borrow_rate_pa": 0.252}
    acct, ctx, as_of = _naked_call_assigned(tmp_path, f"borrow{tag}", 9310 + tag, cfg, tag)
    try:
        pos = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
        assert pos and float(pos[0]["qty"]) == pytest.approx(-100.0), (
            "the naked call was not assigned into short stock -- the fixture proves nothing")
        assert (acct._pending_assignment_sells == {}) is held
        charge = acct.accrue_short_borrow(as_of)
        if held:
            # 100 shares x the 190 mark x 0.252 / 252 sessions
            assert charge == pytest.approx(100 * 190.0 * 0.252 / 252)
        else:
            assert charge == 0.0, "short stock queued for liquidation was charged borrow"
    finally:
        ctx.__exit__(None, None, None)


# =========================================================================================== #
# C1: the wheel's covered call finishing ITM is a closing delivery, not an assignment to decide
# =========================================================================================== #
@pytest.fixture(scope="module")
def called_away_run():
    return _run(9304, MEMBER_CFG, _entry_rules(), spot_on=_called_away_spot_on)


def test_a_wheel_call_finishing_ITM_is_called_away_and_the_run_completes(called_away_run):
    """The written call carries no entry tag of its own unless it inherits one, and the
    delivery it causes OPENS nothing (it closes the held lot), so there is no hold/liquidate
    decision to make: the run must not refuse it as an untagged assignment."""
    orders, account = called_away_run
    (call,) = _written_calls(orders, WHL)
    away = [o for o in _stock(orders, WHL)
            if o.comment == "option_assignment" and o.side is OrderDirection.SELL]
    assert away, "the ITM covered call did not call the shares away"
    assert sum(float(o.filled_qty or o.quantity) for o in away) == pytest.approx(
        _delivered_buy(orders, WHL))
    assert _held(account, WHL) == pytest.approx(0.0), "shares remain after being called away"
    assert not [o for o in _stock(orders, WHL) if o.comment == "assignment_liquidation"]


def test_the_written_covered_call_inherits_the_wheel_members_tag(called_away_run):
    """The overlay entry (``sell_covered_call`` from an OPEN_POSITIONS rule) authors no tag;
    it inherits the covered stock's, so the wheel member's own tag-scoped exits see it."""
    from ba2_common.core.entry_tag import order_entry_tag

    orders, _ = called_away_run
    (call,) = _written_calls(orders, WHL)
    assert order_entry_tag(call) == TAG_WHEEL, call.data


def test_an_UNTAGGED_call_called_away_in_a_member_scoped_run_decides_nothing(tmp_path):
    """C1 on its own, without the overlay's tag inheritance: a call delivered against shares
    already held only CLOSES the lot, so no hold/liquidate decision -- and so no untagged
    refusal -- is made for it. Account level, through the settlement the engine drives."""
    from ba2_common.core.db import update_instance
    from ba2_common.core.types import OptionRight
    from tests.backtest.test_wheel_assignment import (
        _CALL170, _assign_the_put, _build, _engine, _sell_the_put, _shares, _write_the_call)

    cfg = {**BASE_CFG, "hold_assigned_stock_tags": [TAG_WHEEL]}
    acct, ps, ctx = _build(tmp_path, "c1untagged", 9320, cfg)
    try:
        _sell_the_put(acct)
        (put_entry,) = [o for o in acct.get_orders() if o.contract_symbol]
        put_entry.data = {**(put_entry.data or {}), "entry_tag": TAG_WHEEL}
        update_instance(put_entry)
        _assign_the_put(acct, ps, cfg)
        assert _shares(acct) == pytest.approx(100.0) and acct._pending_assignment_sells == {}

        ps.set_clock(datetime(2024, 3, 11))
        _write_the_call(acct, occ=_CALL170, strike=170.0)       # NO tag on the call
        as_of = datetime(2024, 3, 28)                              # closes 190: ITM
        ps.set_clock(as_of)
        _engine(acct, ps, cfg)._apply_option_expiry(as_of)
        acct.refresh_transactions()

        assert _shares(acct) == pytest.approx(0.0), "the shares were not called away"
        assert acct._pending_assignment_sells == {}
    finally:
        ctx.__exit__(None, None, None)
