"""FactorRanker on the shared share grid (opt-in via ``allow_fractional_shares``).

An expert that did not opt in must compute byte-identical deltas; one that did gets its
targets floored onto the fractional grid for symbols the broker marks fractionable.
"""
from types import SimpleNamespace

import pytest

from ba2_experts.FactorRanker.portfolio import FactorPortfolioManager, rebalance_deltas


def test_no_units_is_the_whole_share_behaviour_byte_for_byte():
    assert rebalance_deltas({"AAA": 0.5}, {"AAA": 0.0}, {"AAA": 333.0}, 10_000.0) == {"AAA": 15.0}


def test_a_fractional_unit_buys_the_fraction():
    out = rebalance_deltas({"AAA": 0.5}, {"AAA": 0.0}, {"AAA": 333.0}, 10_000.0,
                           quantity_units={"AAA": 0.0001})

    assert out == {"AAA": 15.015}


def test_a_symbol_without_a_unit_stays_whole_in_a_mixed_basket():
    out = rebalance_deltas({"AAA": 0.5, "BBB": 0.5}, {}, {"AAA": 333.0, "BBB": 333.0},
                           10_000.0, quantity_units={"AAA": 0.0001})

    assert out == {"AAA": 15.015, "BBB": 15.0}


def test_ledger_float_noise_is_not_a_trade():
    """A ledger that accrues fills with += drifts ~1e-11 from the target. Submitting that
    would be a broker rejection for a trade nobody decided."""
    out = rebalance_deltas({"AAA": 0.5}, {"AAA": 15.015 + 1e-12}, {"AAA": 333.0}, 10_000.0,
                           quantity_units={"AAA": 0.0001})

    assert out == {}


def test_a_buy_is_floored_onto_the_grid_even_from_an_off_grid_holding():
    """DRIP pays odd fractions; target-minus-held would otherwise carry that residue onto
    the wire."""
    out = rebalance_deltas({"AAA": 0.5}, {"AAA": 10.00003}, {"AAA": 333.0}, 10_000.0,
                           quantity_units={"AAA": 0.0001})

    assert out == {"AAA": 5.0149}


def test_a_sell_is_left_exact():
    """Selling precisely what is held is always acceptable; rounding a sell could strand an
    unsellable crumb."""
    out = rebalance_deltas({}, {"AAA": 3.14159}, {"AAA": 100.0}, 10_000.0,
                           quantity_units={"AAA": 0.0001})

    assert out == {"AAA": -3.14159}


def _manager(allowed, flags, risk_pct=0.0):
    mgr = object.__new__(FactorPortfolioManager)
    mgr.expert_instance_id = 7
    settings = {"allow_fractional_shares": allowed, "risk_per_trade_pct": risk_pct}
    mgr.expert = SimpleNamespace(
        get_setting_with_interface_default=lambda key, log_warning=False: settings.get(key))
    calls = []
    mgr.account = SimpleNamespace(
        get_fractionable=lambda symbols: calls.append(list(symbols)) or
        {s: flags.get(s) for s in symbols})
    return mgr, calls


def test_not_opted_in_asks_the_broker_nothing():
    mgr, calls = _manager(False, {"AAA": True})

    assert mgr._quantity_units(["AAA"]) == {}
    assert calls == []


def test_opted_in_gets_a_unit_only_for_broker_true():
    mgr, calls = _manager(True, {"AAA": True, "BBB": False, "CCC": None})

    assert mgr._quantity_units(["AAA", "BBB", "CCC"]) == {"AAA": 0.0001}
    assert len(calls) == 1, "one batch lookup, not one per symbol"


def test_a_lookup_failure_rebalances_in_whole_shares():
    mgr, _ = _manager(True, {})

    def _boom(symbols):
        raise RuntimeError("down")

    mgr.account.get_fractionable = _boom

    assert mgr._quantity_units(["AAA"]) == {}


def test_the_resting_stop_wins_over_the_setting():
    """With risk_per_trade_pct > 0 every held name rests a protective stop, which a
    broker cannot place on a fraction -- so the rebalance stays in whole shares and
    never asks the broker."""
    mgr, calls = _manager(True, {"AAA": True}, risk_pct=1.0)

    assert mgr._quantity_units(["AAA"]) == {}
    assert calls == []


def test_with_the_stop_off_the_setting_applies():
    mgr, _ = _manager(True, {"AAA": True}, risk_pct=0.0)

    assert mgr._quantity_units(["AAA"]) == {"AAA": 0.0001}
