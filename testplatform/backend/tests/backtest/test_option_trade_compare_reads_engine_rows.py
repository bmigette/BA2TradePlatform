"""The C5 comparison reads the trade rows the REAL engine persists (plan Part C5).

``ba2_common.core.option_trade_compare`` is tested on synthetic rows; this pins that its
backtest loader understands the actual ``build_results`` output (JSON round-tripped, as it sits
in ``Backtest.trades``): one O_LEAP round trip from the golden fixture must read as ONE
structure with an ``ok`` entry record, a pairing key taken from the record and equal to the
entry bar D, and the recorded ``dte_exit`` trigger -- with no issue raised.
"""
from __future__ import annotations

import json

from ba2_common.core.option_trade_compare import (
    KEY_FROM_RECORD, Tolerances, backtest_structures, compare,
)
from tests.backtest.test_option_close_trigger_recorded import _golden_leap_account


def test_engine_rows_read_as_one_recorded_structure():
    from app.services.backtest.results import build_results

    account, ctx = _golden_leap_account(True)
    try:
        cfg = {"initial_capital": 100_000.0, "start_date": "2024-01-02",
               "end_date": "2024-07-26", "account_settings": dict(account._cfg),
               "option_trade_records": True}
        trades = json.loads(json.dumps(build_results(account, cfg)["trades"], default=str))
    finally:
        ctx.__exit__(None, None, None)
    issues: list = []
    structs = backtest_structures(trades, issues)
    assert len(structs) == 1, structs
    s = structs[0]
    assert issues == [] and s["issues"] == [], s["issues"]
    assert s["entry_record_status"] == "ok"
    assert s["key_source"] == KEY_FROM_RECORD
    assert s["data_session"] == str(trades[0]["entry_time"])[:10]
    assert s["strategy"]
    assert s["structure"]["strategy"] == s["strategy"]
    assert s["exit"]["status"] == "ok" and s["exit"]["trigger"] == "dte_exit"
    assert all(leg["entry_snapshot"] is not None for leg in s["legs"])
    assert all(leg["gross_pnl"] is not None for leg in s["legs"])
    # the structure compared against itself (as if live had done exactly the same) is clean
    report = compare(structs, structs, Tolerances())
    assert report["summary"]["paired"] == 1
    assert [d for d in report["diffs"] if d["within_tolerance"] is False] == []


# ---------------------------------------------------------------- the real records, both paths

import pytest  # noqa: E402

from ba2_common.core.option_trade_compare import live_structures  # noqa: E402
from tests.backtest.test_option_entry_record_parity import (  # noqa: E402,F401
    _D, _bull_call_spread, _long_call, _record, bt_account, live_account,
)


def _as_live_rows(rec, strategy):
    """The live DB shape of one submitted entry: parent (or single ticket) + leg children,
    enums by NAME, as SQLModel stores them. A preview has no fill PRICE: both sides read None."""
    legs = rec["legs"]
    head = dict(id=1, account_id=1, symbol="X", quantity=2, side="BUY", status="FILLED",
                filled_qty=2, open_price=None, created_at="2023-01-11 14:35:00",
                transaction_id=1, data={"entry_record": rec}, parent_order_id=None,
                asset_class="OPTION", contract_symbol=None, option_type=None, strike=None,
                expiry=None, underlying_symbol=legs[0]["contract_symbol"][:-15].strip(),
                multiplier=100, position_intent=None, option_strategy=strategy)
    if len(legs) == 1:
        leg = legs[0]
        head.update(contract_symbol=leg["contract_symbol"], option_type=leg["right"].upper(),
                    strike=leg["strike"], expiry=leg["expiry"],
                    position_intent=leg["position_intent"])
        return [head]
    kids = [dict(head, id=2 + i, data=None, parent_order_id=1, option_strategy=None,
                 side=leg["side"].upper(), contract_symbol=leg["contract_symbol"],
                 option_type=leg["right"].upper(), strike=leg["strike"], expiry=leg["expiry"],
                 position_intent=leg["position_intent"])
            for i, leg in enumerate(legs)]
    return [head] + kids


def _as_bt_rows(rec, strategy):
    rows = []
    for i, leg in enumerate(rec["legs"]):
        row = {"symbol": "X", "entry_time": f"{_D.isoformat()}T00:00:00", "exit_time": None,
               "direction": leg["side"], "entry_price": None, "exit_price": None,
               "size": 2.0, "pnl": 0.0, "exit_reason": "open_at_end",
               "contract_symbol": leg["contract_symbol"],
               "underlying_symbol": leg["contract_symbol"][:-15].strip(),
               "option_type": leg["right"], "strike": leg["strike"], "expiry": leg["expiry"],
               "transaction_id": 1, "multiplier": 100.0, "exit_record": None,
               "entry_record": {"leg": leg}}
        if i == 0:
            row["option_strategy"] = strategy
            row["entry_record"] = {"version": rec["version"], "structure": rec["structure"],
                                   "legs_without_quote": rec["legs_without_quote"], "leg": leg}
        rows.append(row)
    return rows


@pytest.mark.parametrize("builder", [_long_call, _bull_call_spread],
                         ids=["long_call", "bull_call_spread"])
def test_the_two_real_entry_records_pair_and_differ_only_in_provenance(
        bt_account, live_account, builder):
    """Live (Alpaca snapshot at N(D) 09:35 ET) and backtest (bar D) records of the SAME
    decision pair on data_session D, and the only out-of-tolerance rows are the greeks-source
    rows -- which the report tags, never hides."""
    bt_rec, live_rec = _record(builder(bt_account)), _record(builder(live_account))
    strategy = bt_rec["structure"]["strategy"]
    live = live_structures(_as_live_rows(live_rec, strategy), {})
    bt = backtest_structures(_as_bt_rows(bt_rec, strategy))
    assert live[0]["data_session"] == bt[0]["data_session"] == _D.isoformat()
    report = compare(live, bt, Tolerances())
    assert report["summary"]["paired"] == 1, report["unmatched_live"]
    bad = {(d["phase"], d["field"]) for d in report["diffs"] if d["within_tolerance"] is False}
    assert bad == {("entry", "greeks_source")}, bad
