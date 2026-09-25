"""Review item I1 (plan 2026-09-24 Tasks 1b / 11 / 13): the option-integrity events are COUNTED on
the account and published in the results, because a GA trial child runs under
``logging.disable(logging.ERROR)`` (price_source._worker_init) -- the WARNING/ERROR lines these
tasks added never reach a log in stage 1, and ``check_option_ledger``'s return was discarded.

Keys (options runs only; an equity run's results are unchanged):
``option_ledger_mismatches`` {count, examples[<=3]}, ``option_orders_volume_sized``,
``option_orders_volume_refused``, ``option_split_rekeys``, ``option_split_rekey_refusals``.
"""
from __future__ import annotations

import logging
from datetime import date

import pytest

from ba2_common.core.types import OptionRight, OrderDirection

from tests.backtest.test_option_split_crossing import _dt
from tests.backtest.test_option_split_rekey import (
    ADJ_PUT100, CFG, D0901, PUT400, PUT410, SPLIT, _closes, _harness, _long_put_bars, _open,
    _series, _step,
)
# The equity-only account fixture, imported so pytest can inject it here.
from tests.backtest.test_backtest_account_options import backtest_account_no_options  # noqa: F401

KEYS = ("option_ledger_mismatches", "option_orders_volume_sized",
        "option_orders_volume_refused", "option_split_rekeys", "option_split_rekey_refusals")


def _results(acct):
    from app.services.backtest.results import build_results
    acct.snapshot_equity(_dt(date(2020, 9, 2)))
    return build_results(acct, {"initial_capital": CFG["starting_cash"],
                                "account_settings": dict(CFG), "option_trade_records": True})


def test_a_clean_options_run_publishes_zeroed_counters():
    with _harness(_long_put_bars(), _closes()) as (engine, acct, ps):
        out = _results(acct)
        assert {k: out[k] for k in KEYS} == {
            "option_ledger_mismatches": {"count": 0, "examples": []},
            "option_orders_volume_sized": 0, "option_orders_volume_refused": 0,
            "option_split_rekeys": 0, "option_split_rekey_refusals": 0}


def test_rekeys_and_refusals_are_counted_even_with_logging_disabled():
    """One re-key (P400 -> P100) and one deferral (P410's adjusted P102.5 not listed), with the
    GA child's logging.disable(ERROR) in force."""
    bars = _long_put_bars()
    bars.update(_series(PUT410, {date(2020, 8, 24): 15.0}))
    logging.disable(logging.ERROR)
    try:
        with _harness(bars, _closes()) as (engine, acct, ps):
            _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put")
            _open(acct, PUT410, OptionRight.PUT, 410.0, OrderDirection.BUY, "long_put")
            for d in (SPLIT, D0901):
                _step(acct, ps, d)
            out = _results(acct)
    finally:
        logging.disable(logging.NOTSET)
    assert out["option_split_rekeys"] == 1
    assert out["option_split_rekey_refusals"] == 1           # explained once, counted once


def test_a_ledger_mismatch_is_counted_with_examples():
    with _harness(_long_put_bars(), _closes()) as (engine, acct, ps):
        _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put")
        acct._option_positions[PUT400].qty = 3.0
        for _ in range(3):
            acct.check_option_ledger(context="expiry pass")   # a standing orphan: counted once
        out = _results(acct)
    lm = out["option_ledger_mismatches"]
    assert lm["count"] == 1
    assert lm["examples"][0]["contract"] == PUT400
    assert (lm["examples"][0]["lot_qty"], lm["examples"][0]["view_qty"]) == (3.0, 1.0)
    assert len(lm["examples"]) <= 3


def test_volume_sized_and_refused_orders_are_counted():
    from tests.backtest.test_option_order_fill_volume_sizing import ON, _bars, _leg
    with _harness(_bars({PUT410: 50, PUT400: 5}), _closes(), cfg=ON) as (engine, acct, ps):
        acct.submit_option_order(legs=[_leg(PUT410, 410.0, OrderDirection.BUY)], quantity=8,
                                 order_type="market", option_strategy="long_put")
        acct.submit_option_order(legs=[_leg(PUT400, 400.0, OrderDirection.BUY)], quantity=2,
                                 order_type="market", option_strategy="long_put")
        out = _results(acct)
    assert (out["option_orders_volume_sized"], out["option_orders_volume_refused"]) == (1, 1)


def test_an_equity_run_gains_no_key(backtest_account_no_options):  # noqa: F811
    from datetime import datetime
    from app.services.backtest.results import build_results
    acct = backtest_account_no_options
    acct.snapshot_equity(datetime(2024, 3, 5))
    out = build_results(acct, {"initial_capital": 100_000.0, "account_settings": dict(CFG)})
    assert not any(k in out for k in KEYS)
