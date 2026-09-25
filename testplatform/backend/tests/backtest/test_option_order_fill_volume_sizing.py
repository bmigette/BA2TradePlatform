"""Task 11 (plan 2026-09-24 "Revisions"): size a backtest option order WITHIN the fill engine's
volume-participation cap at ORDER time -- behind ``option_size_within_fill_volume`` (default
OFF, so every older option run reproduces).

THE DEFECT. The fill engine refuses an option fill needing more than 10% of the premium bar's
volume (``_volume_cap_reject_reason``). An order sized above that did not trade smaller -- it
simply expired: about half of the expired O_LP entries in the 2026-09-24 diagnosis. With the
flag on, an OPENING order is cut to the same capacity (one shared helper,
``_option_fill_capacity``) read on the decision bar; a cap of 0 places no order and says why.
Live is untouched: the seam (``OptionsAccountInterface.option_order_quantity_limit``) is the
identity there.
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

import pytest

from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus

from tests.backtest.test_option_split_crossing import EXPIRY, OPEN_DAY, _bar, _sessions
from tests.backtest.test_option_split_rekey import CFG, PUT400, PUT410, _closes, _harness, _rows

ON = {**CFG, "option_size_within_fill_volume": True}


def _bars(volumes):
    """contract -> volume, a flat 10.00 premium on every pre-split session."""
    return {(c, d): _bar(10.0, volume=v) for c, v in volumes.items()
            for d in _sessions() if d < date(2020, 8, 31)}


def _leg(contract, strike, side, ratio=1, intent=None):
    return OptionLeg(contract_symbol=contract, side=side,
                     position_intent=intent or ("buy_to_open" if side == OrderDirection.BUY
                                                else "sell_to_open"),
                     option_type=OptionRight.PUT, strike=strike, expiry=EXPIRY,
                     underlying="AAPL", ratio_qty=ratio)


def test_an_order_above_the_cap_is_cut_to_the_cap_and_fills(caplog):
    """Volume 50 -> 10% = 5 contracts. An 8-lot is placed as 5 and fills (it used to expire)."""
    with _harness(_bars({PUT410: 50}), _closes(), cfg=ON) as (engine, acct, ps):
        with caplog.at_level(logging.INFO):
            order = acct.submit_option_order(legs=[_leg(PUT410, 410.0, OrderDirection.BUY)],
                                             quantity=8, order_type="market",
                                             option_strategy="long_put")
        assert order is not None and order.quantity == 5
        acct.refresh_orders()
        acct.refresh_transactions()
        assert order.status == OrderStatus.FILLED
        assert acct._option_positions[PUT410].qty == 5
        (txn,) = [p for p in acct.get_option_positions()]
        assert txn.quantity == 5
        assert any("sized 8 -> 5" in r.getMessage() for r in caplog.records)


def test_the_order_time_cap_is_the_fill_engines_number():
    """Same helper, same comparison: the capped size passes the fill check, one more fails."""
    from app.services.backtest.backtest_account import _max_units_within, _option_fill_capacity
    with _harness(_bars({PUT410: 50}), _closes(), cfg=ON) as (engine, acct, ps):
        for volume in (0, 9, 10, 29, 30, 31, 49, 50, 51, 99, 100, 1234, 70, 60, 3):
            n = _max_units_within(_option_fill_capacity(volume), 1.0)
            bar = {"volume": volume}
            assert acct._volume_cap_reject_reason(type("O", (), {"quantity": n})(), bar) is None \
                or n == 0
            assert acct._volume_cap_reject_reason(type("O", (), {"quantity": n + 1})(), bar)


def test_a_cap_of_zero_places_no_order_and_says_why(caplog):
    with _harness(_bars({PUT410: 5}), _closes(), cfg=ON) as (engine, acct, ps):
        with caplog.at_level(logging.WARNING):
            order = acct.submit_option_order(legs=[_leg(PUT410, 410.0, OrderDirection.BUY)],
                                             quantity=2, order_type="market",
                                             option_strategy="long_put")
        assert order is None
        assert _rows(acct, PUT410) == []
        msgs = [r.getMessage() for r in caplog.records if "NOT PLACED" in r.getMessage()]
        assert len(msgs) == 1 and PUT410 in msgs[0] and "volume 5" in msgs[0]


def test_flag_off_is_unchanged_the_order_keeps_its_size_and_expires_unfilled():
    with _harness(_bars({PUT410: 50}), _closes()) as (engine, acct, ps):
        order = acct.submit_option_order(legs=[_leg(PUT410, 410.0, OrderDirection.BUY)],
                                         quantity=8, order_type="market",
                                         option_strategy="long_put")
        assert order.quantity == 8
        acct.refresh_orders()
        assert order.status != OrderStatus.FILLED
        assert acct.rejected_illiquid_fills >= 1


def test_a_multi_leg_structure_is_capped_by_its_most_constrained_leg():
    """Short P410 volume 100 (cap 10), long P400 volume 30 (cap 3): 8 structures -> 3."""
    with _harness(_bars({PUT410: 100, PUT400: 30}), _closes(), cfg=ON) as (engine, acct, ps):
        parent = acct.submit_option_order(
            legs=[_leg(PUT410, 410.0, OrderDirection.SELL), _leg(PUT400, 400.0, OrderDirection.BUY)],
            quantity=8, order_type="market", option_strategy="bull_put_spread")
        assert parent.quantity == 3
        assert {o.contract_symbol: o.quantity for o in acct.get_orders() if o.contract_symbol} == {
            PUT410: 3, PUT400: 3}
        acct.refresh_orders()
        assert parent.status == OrderStatus.FILLED
        assert acct._option_positions[PUT410].qty == -3 and acct._option_positions[PUT400].qty == 3


def test_a_leg_ratio_divides_its_capacity():
    """A 2x leg on volume 50 (5 contracts) allows floor(5 / 2) = 2 structures."""
    with _harness(_bars({PUT410: 1000, PUT400: 50}), _closes(), cfg=ON) as (engine, acct, ps):
        parent = acct.submit_option_order(
            legs=[_leg(PUT410, 410.0, OrderDirection.SELL),
                  _leg(PUT400, 400.0, OrderDirection.BUY, ratio=2)],
            quantity=6, order_type="market", option_strategy="put_ratio_spread")
        assert parent.quantity == 2
        assert {o.contract_symbol: o.quantity for o in acct.get_orders() if o.contract_symbol} == {
            PUT410: 2, PUT400: 4}


def test_a_close_is_never_capped():
    with _harness(_bars({PUT410: 50}), _closes(), cfg=ON) as (engine, acct, ps):
        order = acct.submit_option_order(
            legs=[_leg(PUT410, 410.0, OrderDirection.SELL, intent="sell_to_close")],
            quantity=8, order_type="market", option_strategy="close")
        assert order.quantity == 8


def test_the_live_default_seam_is_the_identity():
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    assert OptionsAccountInterface.option_order_quantity_limit(
        object(), [_leg(PUT410, 410.0, OrderDirection.BUY)], 17, "long_put") == 17


# ------------------------------------------------------------------ the flag reaches the run
def test_the_trial_config_carries_the_flag():
    """A knob dropped by _build_daily_trial_config is silently inert (the whitelist trap)."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    backtest_cfg = {
        "backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
        "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0,
        "seed": 42, "account_settings": {"option_size_within_fill_volume": True},
        "experts": [{"class": "FMPRating", "settings": {}}],
    }
    decoded = {"expert_overrides": {}, "screener_overrides": {}, "schedule_days": None,
               "entry_rules": None, "exit_rules": None}
    cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
    assert cfg["account_settings"]["option_size_within_fill_volume"] is True


def test_the_api_payload_writes_the_key_only_when_on():
    from app.services.backtest import daily_backtest_handler as H
    from tests.backtest.test_daily_backtest_handler import _payload
    off = H._build_config(_payload(1))
    on = H._build_config(_payload(1, option_size_within_fill_volume="true"))
    assert "option_size_within_fill_volume" not in off["account_settings"]
    assert on["account_settings"]["option_size_within_fill_volume"] is True


def test_the_launcher_writes_the_key_only_when_on():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    import ba2test_launcher as L
    from types import SimpleNamespace
    assert L._option_sizing_account_settings(SimpleNamespace()) == {}
    assert L._option_sizing_account_settings(
        SimpleNamespace(option_size_within_fill_volume=False)) == {}
    assert L._option_sizing_account_settings(
        SimpleNamespace(option_size_within_fill_volume=True)) == {
            "option_size_within_fill_volume": True}


def test_stage1_turns_it_on_and_the_matrix_forwards_it_as_an_identity_token():
    repo = Path(__file__).resolve().parents[4]
    assert "--option-size-within-fill-volume" in (repo / "tools" / "stage1_run.sh").read_text(
        encoding="utf-8")
    sys.path.insert(0, str(repo / "tools"))
    import run_options_matrix as M
    src = (repo / "tools" / "run_options_matrix.py").read_text(encoding="utf-8")
    assert 'cmd += ["--option-size-within-fill-volume"]' in src
    assert '"--no-robust-fitness", "--option-size-within-fill-volume"' in src


# ------------------------------------------------------------------ C1: the ACTION sees the cut
def _through_the_action(monkeypatch, acct, action_cls, legs, quantity, strategy, reserve):
    """Run ``_OptionEntryAction._submit_option_order`` -- the choke point every builder reaches
    -- with an option RM engaged (its admission and charge captured), exactly as a builder
    calls it: with the reserve it computed for the UNCAPPED quantity."""
    from types import SimpleNamespace
    import ba2_common.core.TradeActions as TA
    from ba2_common.core.types import OrderRecommendation
    seen = {}

    def _admit(**kw):
        seen["admit_qty"] = kw["quantity"]
        return SimpleNamespace(allowed=True, candidate={"quantity": kw["quantity"]},
                               reason=None, message="ok")

    monkeypatch.setattr(TA, "admit_option_entry", _admit)
    monkeypatch.setattr(TA, "record_submitted",
                        lambda iid, tid, cand: seen.setdefault("charged", cand))
    action = action_cls(instrument_name="AAPL", account=acct,
                        order_recommendation=OrderRecommendation.BUY)
    action.create_and_save_action_result = lambda **kw: SimpleNamespace(**kw)
    monkeypatch.setattr(action, "_option_risk_manager", lambda: (object(), 7))
    result = action._submit_option_order(legs, quantity, 10.0, strategy, option_reserve=reserve)
    return result, seen


def test_a_capped_csp_carries_the_capped_reserve_rm_charge_and_record(monkeypatch):
    """CSP P410, 8 requested, decision-bar volume 50 -> 5. The reserve on the order row (what
    reserved_option_buying_power_detail reads for the position's life), the RM admission and
    its submitted charge, data['quantity'] and the entry record are all the 5-lot's."""
    from ba2_common.core.TradeActions import SellCashSecuredPutAction
    with _harness(_bars({PUT410: 50}), _closes(), cfg=ON) as (engine, acct, ps):
        leg = _leg(PUT410, 410.0, OrderDirection.SELL)
        reserve8 = acct.option_reserve_required("cash_secured_put", 8, strike=410.0)
        result, seen = _through_the_action(monkeypatch, acct, SellCashSecuredPutAction,
                                           [leg], 8, "cash_secured_put", reserve8)
        assert result.success, result.message
        (order,) = _rows(acct, PUT410)
        assert order.quantity == 5
        assert order.data["option_reserve"] == pytest.approx(410.0 * 100 * 5)
        assert result.data["option_reserve"] == pytest.approx(410.0 * 100 * 5)
        assert result.data["quantity"] == 5
        assert seen == {"admit_qty": 5, "charged": {"quantity": 5}}
        assert order.data["entry_record"]["structure"]["quantity"] == 5
        detail = acct.reserved_option_buying_power()
        assert detail == pytest.approx(410.0 * 100 * 5)


def test_a_capped_bull_put_spread_carries_the_capped_reserve_and_rm_charge(monkeypatch):
    from ba2_common.core.TradeActions import OpenBullPutSpreadAction
    with _harness(_bars({PUT410: 100, PUT400: 30}), _closes(), cfg=ON) as (engine, acct, ps):
        legs = [_leg(PUT410, 410.0, OrderDirection.SELL), _leg(PUT400, 400.0, OrderDirection.BUY)]
        reserve8 = acct.option_reserve_required("bull_put_spread", 8, spread_width=10.0,
                                                net_credit=2.0)
        assert reserve8 == pytest.approx(8.0 * 100 * 8)
        result, seen = _through_the_action(monkeypatch, acct, OpenBullPutSpreadAction,
                                           legs, 8, "bull_put_spread", reserve8)
        assert result.success, result.message
        parent = [o for o in acct.get_orders() if o.contract_symbol is None][0]
        assert parent.quantity == 3
        assert {o.contract_symbol: o.quantity for o in acct.get_orders() if o.contract_symbol} == {
            PUT410: 3, PUT400: 3}
        assert parent.data["option_reserve"] == pytest.approx(8.0 * 100 * 3)
        assert result.data["quantity"] == 3
        assert seen == {"admit_qty": 3, "charged": {"quantity": 3}}


def test_a_cap_of_zero_is_refused_by_the_action_naming_the_volume_cap(monkeypatch):
    from ba2_common.core.TradeActions import SellCashSecuredPutAction
    with _harness(_bars({PUT410: 5}), _closes(), cfg=ON) as (engine, acct, ps):
        leg = _leg(PUT410, 410.0, OrderDirection.SELL)
        result, seen = _through_the_action(
            monkeypatch, acct, SellCashSecuredPutAction, [leg], 2, "cash_secured_put",
            acct.option_reserve_required("cash_secured_put", 2, strike=410.0))
        assert not result.success
        assert "fill-volume cap" in result.message
        assert seen == {}                                  # never admitted, never charged
        assert _rows(acct, PUT410) == []


def test_flag_off_the_action_path_is_unchanged(monkeypatch):
    from ba2_common.core.TradeActions import SellCashSecuredPutAction
    with _harness(_bars({PUT410: 50}), _closes()) as (engine, acct, ps):
        leg = _leg(PUT410, 410.0, OrderDirection.SELL)
        reserve8 = acct.option_reserve_required("cash_secured_put", 8, strike=410.0)
        result, seen = _through_the_action(monkeypatch, acct, SellCashSecuredPutAction,
                                           [leg], 8, "cash_secured_put", reserve8)
        (order,) = _rows(acct, PUT410)
        assert order.quantity == 8 and order.data["option_reserve"] == pytest.approx(reserve8)
        assert seen["admit_qty"] == 8


# ---------------------------------------------- the fill reads the DECISION bar under the flag
D0824, D0825 = date(2020, 8, 24), date(2020, 8, 25)


def _two_day_bars(decision_volume, fill_volume):
    return {(PUT410, D0824): _bar(10.0, volume=decision_volume),
            (PUT410, D0825): _bar(10.0, volume=fill_volume)}


@pytest.mark.parametrize("flag,filled", [(True, True), (False, False)])
def test_under_the_flag_the_fill_cap_reads_the_decision_bar_not_the_next_one(flag, filled):
    """next_bar_open, decided 08-24 (volume 50 -> 5 fillable), filling on 08-25 (volume 10 -> 1).
    Flag on: the fill engine reads the DECISION bar, so the 5-lot the order was sized to fills --
    no look-ahead into 08-25's volume. Flag off: unchanged, the fill bar's 10 refuses it."""
    from tests.backtest.test_option_split_crossing import _dt
    cfg = {**CFG, "fill_model": "next_bar_open",
           **({"option_size_within_fill_volume": True} if flag else {})}
    with _harness(_two_day_bars(50, 10), _closes(), cfg=cfg) as (engine, acct, ps):
        ps.set_clock(_dt(D0824))
        order = acct.submit_option_order(legs=[_leg(PUT410, 410.0, OrderDirection.BUY)],
                                         quantity=5, order_type="market",
                                         option_strategy="long_put")
        acct.refresh_orders()
        assert (order.status == OrderStatus.FILLED) is filled
        assert acct.rejected_illiquid_fills == (0 if filled else 1)
