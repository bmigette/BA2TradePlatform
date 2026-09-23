"""Pairing and diffing of live option structures against their backtest twins (plan Part C5).

Synthetic rows only: live ``TradingOrder`` / ``Transaction``-like dicts (enums stored by NAME,
as SQLModel writes them) and backtest trades-JSON rows (the shape
``BacktestAccount._attach_option_records`` + ``results._trade_row`` persist).
"""
from __future__ import annotations

import copy

import pytest

from ba2_common.core.option_trade_compare import (
    KEY_FROM_FILL, KEY_FROM_RECORD, Tolerances, backtest_structures, compare,
    live_structures, pair_structures, summarize,
)
from ba2_common.core.option_trade_record import (
    LEG_SNAPSHOT_FIELDS, OPTION_TRADE_RECORD_VERSION, STRUCTURE_SNAPSHOT_FIELDS,
)

LONG_C = "AAPL240621C00170000"
SHORT_C = "AAPL240621C00180000"


def snap(contract, *, side, right="call", strike, data_session="2024-05-01",
         greeks_source="broker", **over):
    leg = {k: None for k in LEG_SNAPSHOT_FIELDS}
    leg.update({
        "contract_symbol": contract, "side": side, "ratio_qty": 1,
        "position_intent": f"{side}_to_open", "right": right, "strike": strike,
        "expiry": "2024-06-21", "dte": 50, "data_session": data_session, "spot": 169.3,
        "moneyness_pct": (strike / 169.3 - 1) * 100, "bid": 5.0, "ask": 5.2, "mid": 5.1,
        "spread_pct": 0.2 / 5.1 * 100, "last": 5.1, "iv": 0.25, "delta": 0.5,
        "gamma": 0.02, "theta": -0.05, "vega": 0.3, "rho": 0.1, "open_interest": 1200,
        "volume": 340, "greeks_source": greeks_source, "quote_time": None,
    })
    leg.update(over)
    return leg


def structure(**over):
    s = {k: None for k in STRUCTURE_SNAPSHOT_FIELDS}
    s.update({"strategy": "bull_call_spread", "quantity": 2, "multiplier": 100,
              "net_price": 2.4, "leg_count": 2, "max_loss": 240.0, "max_loss_state": "MEASURED",
              "max_profit": 760.0, "max_profit_state": "MEASURED", "breakevens": [172.4]})
    s.update(over)
    return s


def entry_rec(legs, **struct_over):
    return {"version": OPTION_TRADE_RECORD_VERSION, "legs": legs, "legs_without_quote": [],
            "structure": structure(**struct_over)}


def exit_rec(trigger, legs):
    return {"version": OPTION_TRADE_RECORD_VERSION, "trigger": trigger, "rule_id": 7,
            "rule_name": "tp", "legs": legs, "legs_without_quote": []}


# ---------------------------------------------------------------- live fixture builders

def live_spread(*, txn=10, underlying="AAPL", created="2024-05-02 13:35:00",
                closed="2024-05-10 14:00:00", entry_record=None, exit_record=None,
                entry_fills=(5.3, 2.8), exit_fills=(7.0, 3.5), with_exit=True,
                close_reason="take_profit", base_id=100):
    """A 2-leg bull call spread: parent + 2 children, then a close parent + 2 children."""
    long_s = snap(LONG_C, side="buy", strike=170.0)
    short_s = snap(SHORT_C, side="sell", strike=180.0, bid=2.6, ask=2.8, mid=2.7,
                   delta=0.3)
    er = entry_rec([long_s, short_s]) if entry_record is None else entry_record
    orders = [
        dict(id=base_id, account_id=1, symbol=underlying, quantity=2, side="BUY",
             status="FILLED", filled_qty=2, open_price=2.5, created_at=created,
             transaction_id=txn, data={"entry_record": er} if er != "absent" else {},
             parent_order_id=None, asset_class="OPTION", contract_symbol=None,
             option_type=None, strike=None, expiry="2024-06-21",
             underlying_symbol=underlying, multiplier=100, position_intent=None,
             option_strategy="bull_call_spread"),
        dict(id=base_id + 1, account_id=1, symbol=LONG_C, quantity=2, side="BUY",
             status="FILLED", filled_qty=2, open_price=entry_fills[0], created_at=created,
             transaction_id=txn, data=None, parent_order_id=base_id, asset_class="OPTION",
             contract_symbol=LONG_C, option_type="CALL", strike=170.0, expiry="2024-06-21",
             underlying_symbol=underlying, multiplier=100, position_intent="buy_to_open",
             option_strategy=None),
        dict(id=base_id + 2, account_id=1, symbol=SHORT_C, quantity=2, side="SELL",
             status="FILLED", filled_qty=2, open_price=entry_fills[1], created_at=created,
             transaction_id=txn, data=None, parent_order_id=base_id, asset_class="OPTION",
             contract_symbol=SHORT_C, option_type="CALL", strike=180.0, expiry="2024-06-21",
             underlying_symbol=underlying, multiplier=100, position_intent="sell_to_open",
             option_strategy=None),
    ]
    if with_exit:
        xr = exit_rec("take_profit", [
            snap(LONG_C, side="sell", strike=170.0, data_session="2024-05-09",
                 position_intent="sell_to_close", bid=6.9, ask=7.1, mid=7.0),
            snap(SHORT_C, side="buy", strike=180.0, data_session="2024-05-09",
                 position_intent="buy_to_close", bid=3.4, ask=3.6, mid=3.5),
        ]) if exit_record is None else exit_record
        orders += [
            dict(id=base_id + 3, account_id=1, symbol=underlying, quantity=2, side="SELL",
                 status="FILLED", filled_qty=2, open_price=3.5, created_at=closed,
                 transaction_id=txn, data={"exit_record": xr}, parent_order_id=None,
                 asset_class="OPTION", contract_symbol=None, option_type=None, strike=None,
                 expiry="2024-06-21", underlying_symbol=underlying, multiplier=100,
                 position_intent=None, option_strategy="close"),
            dict(id=base_id + 4, account_id=1, symbol=LONG_C, quantity=2, side="SELL",
                 status="FILLED", filled_qty=2, open_price=exit_fills[0], created_at=closed,
                 transaction_id=txn, data=None, parent_order_id=base_id + 3,
                 asset_class="OPTION", contract_symbol=LONG_C, option_type="CALL",
                 strike=170.0, expiry="2024-06-21", underlying_symbol=underlying,
                 multiplier=100, position_intent="sell_to_close", option_strategy=None),
            dict(id=base_id + 5, account_id=1, symbol=SHORT_C, quantity=2, side="BUY",
                 status="FILLED", filled_qty=2, open_price=exit_fills[1], created_at=closed,
                 transaction_id=txn, data=None, parent_order_id=base_id + 3,
                 asset_class="OPTION", contract_symbol=SHORT_C, option_type="CALL",
                 strike=180.0, expiry="2024-06-21", underlying_symbol=underlying,
                 multiplier=100, position_intent="buy_to_close", option_strategy=None),
        ]
    txns = {txn: {"id": txn, "symbol": underlying, "close_reason": close_reason,
                  "status": "CLOSED", "option_strategy": "bull_call_spread"}}
    return orders, txns


# ---------------------------------------------------------------- backtest fixture builders

def bt_spread(*, txn=1, underlying="AAPL", entry_day="2024-05-01", exit_day="2024-05-09",
              long_snap=None, short_snap=None, entry_fills=(5.3, 2.8), exit_fills=(7.0, 3.5),
              greeks_source="broker", head_error=None, records=True, strategy="bull_call_spread"):
    long_s = long_snap or snap(LONG_C, side="buy", strike=170.0, data_session=entry_day,
                               greeks_source=greeks_source)
    short_s = short_snap or snap(SHORT_C, side="sell", strike=180.0, bid=2.6, ask=2.8,
                                 mid=2.7, delta=0.3, data_session=entry_day,
                                 greeks_source=greeks_source)
    x_long = snap(LONG_C, side="sell", strike=170.0, data_session=exit_day,
                  position_intent="sell_to_close", bid=6.9, ask=7.1, mid=7.0,
                  greeks_source=greeks_source)
    x_short = snap(SHORT_C, side="buy", strike=180.0, data_session=exit_day,
                   position_intent="buy_to_close", bid=3.4, ask=3.6, mid=3.5,
                   greeks_source=greeks_source)

    def row(contract, direction, strike, e, x, size=2.0):
        d = 1.0 if direction == "buy" else -1.0
        return {"symbol": underlying, "entry_time": f"{entry_day}T00:00:00",
                "exit_time": f"{exit_day}T00:00:00", "direction": direction,
                "entry_price": e, "exit_price": x, "size": size,
                "pnl": (x - e) * size * d * 100 - 2.0, "pnl_pct": 0.1, "bars_held": 6,
                "exit_reason": "take_profit", "contract_symbol": contract,
                "underlying_symbol": underlying, "option_type": "call", "strike": strike,
                "expiry": "2024-06-21", "transaction_id": txn, "multiplier": 100.0}

    r1 = row(LONG_C, "buy", 170.0, entry_fills[0], exit_fills[0])
    r2 = row(SHORT_C, "sell", 180.0, entry_fills[1], exit_fills[1])
    if records:
        head = {"version": OPTION_TRADE_RECORD_VERSION,
                "structure": structure(strategy=strategy), "legs_without_quote": [],
                "leg": long_s}
        if head_error:
            head = {"version": OPTION_TRADE_RECORD_VERSION, "structure": None,
                    "legs_without_quote": None, "error": head_error, "leg": None}
        r1.update(option_strategy=strategy, recommendation_confidence=71.0,
                  entry_record=head,
                  exit_record={"trigger": "take_profit", "rule_id": 3, "rule_name": "r-1",
                               "leg": x_long})
        r2.update(entry_record={"leg": None if head_error else short_s},
                  exit_record={"trigger": "take_profit", "rule_id": 3, "rule_name": "r-1",
                               "leg": x_short})
    return [r1, r2]


# ================================================================= tests

def test_exact_match_has_no_out_of_tolerance_diffs():
    orders, txns = live_spread()
    live = live_structures(orders, txns)
    bt = backtest_structures(bt_spread())
    report = compare(live, bt, Tolerances())
    assert report["summary"]["paired"] == 1
    assert report["unmatched_live"] == [] and report["unmatched_backtest"] == []
    bad = [d for d in report["diffs"] if not d["within_tolerance"]]
    assert bad == [], bad
    # the fields the plan names are all compared
    fields = {d["field"] for d in report["diffs"]}
    for f in ("data_session", "strike", "expiry", "right", "side", "dte", "spot",
              "moneyness_pct", "bid", "ask", "mid", "spread_pct", "iv", "delta", "volume",
              "open_interest", "entry_fill", "entry_slippage_vs_mid", "exit_trigger",
              "exit_data_session", "exit_fill", "gross_pnl", "net_gross_pnl", "net_price"):
        assert f in fields, f


def test_pairing_is_by_data_session_not_fill_date():
    """Live fills in session N(D) (the Thursday), the backtest stamps D (the Wednesday):
    the key is the entry record's data_session, identical on both sides."""
    orders, txns = live_spread(created="2024-05-02 13:35:00")
    live = live_structures(orders, txns)
    bt = backtest_structures(bt_spread(entry_day="2024-05-01"))
    assert live[0]["data_session"] == "2024-05-01"
    assert live[0]["key_source"] == KEY_FROM_RECORD
    assert bt[0]["data_session"] == "2024-05-01"
    pairs, lu, bu = pair_structures(live, bt)
    assert len(pairs) == 1 and lu == [] and bu == []


def test_missing_records_pair_on_the_session_derived_from_the_fill():
    """No record on either side: live N(D) Monday 2024-05-06 09:35 ET reads Friday
    2024-05-03, and the backtest's D is that Friday."""
    orders, txns = live_spread(created="2024-05-06 13:35:00", entry_record="absent",
                               with_exit=False)
    live = live_structures(orders, txns)
    assert live[0]["data_session"] == "2024-05-03"
    assert live[0]["key_source"] == KEY_FROM_FILL
    assert live[0]["entry_record_status"] == "missing"
    rows = bt_spread(entry_day="2024-05-03", records=False)
    for r in rows:
        r["option_strategy"] = None
    rows[0]["option_strategy"] = None
    bt = backtest_structures(rows)
    assert bt[0]["data_session"] == "2024-05-03"
    assert bt[0]["entry_record_status"] == "missing"
    assert bt[0]["strategy"] is None
    # the BT row carries no strategy without its record: it pairs on (underlying, session)
    # in a second pass, and the pair SAYS so -- never a silent wildcard
    report = compare(live, bt, Tolerances())
    assert report["summary"]["paired"] == 1
    assert "strategy unknown" in report["pairs"][0]["note"]
    strat = [d for d in report["diffs"] if d["field"] == "strategy"][0]
    assert strat["within_tolerance"] is False
    issues = " ".join(report["issues"])
    assert "entry record missing" in issues


def test_a_known_different_strategy_does_not_pair():
    orders, txns = live_spread()
    live = live_structures(orders, txns)
    bt = backtest_structures(bt_spread(strategy="long_call"))
    report = compare(live, bt, Tolerances())
    assert report["summary"]["paired"] == 0
    assert "long_call" in report["unmatched_live"][0]["reason"]


def test_greek_source_difference_is_shown_not_silently_compared():
    orders, txns = live_spread()
    live = live_structures(orders, txns)
    long_bt = snap(LONG_C, side="buy", strike=170.0, greeks_source="bs_from_close",
                   delta=0.47, spot=169.9, moneyness_pct=(170 / 169.9 - 1) * 100)
    bt = backtest_structures(bt_spread(long_snap=long_bt, greeks_source="bs_from_close"))
    report = compare(live, bt, Tolerances())
    delta_rows = [d for d in report["diffs"] if d["field"] == "delta" and d["phase"] == "entry"
                  and d["leg"] == LONG_C]
    assert len(delta_rows) == 1
    d = delta_rows[0]
    assert d["live"] == 0.5 and d["backtest"] == 0.47
    assert d["live_source"] == "broker" and d["backtest_source"] == "bs_from_close"
    assert d["source_mismatch"] is True
    assert d["within_tolerance"] is False
    spot = [d for d in report["diffs"] if d["field"] == "spot" and d["phase"] == "entry"
            and d["leg"] == LONG_C][0]
    assert spot["abs_delta"] == pytest.approx(0.6)
    assert spot["rel_delta"] == pytest.approx(0.6 / 169.3)
    assert spot["within_tolerance"] is False
    # the summary lists greeks under their own "sources differ" heading
    text = summarize(report)
    assert "sources differ" in text
    # a generous tolerance for spot makes that one row pass
    report2 = compare(live, bt, Tolerances(fields={"spot": (1.0, 0.0)}))
    spot2 = [d for d in report2["diffs"] if d["field"] == "spot" and d["phase"] == "entry"
             and d["leg"] == LONG_C][0]
    assert spot2["within_tolerance"] is True


def test_unmatched_on_each_side_with_reasons():
    o1, t1 = live_spread(txn=10, underlying="AAPL", base_id=100)
    o2, t2 = live_spread(txn=11, underlying="MSFT", base_id=200)
    live = live_structures(o1 + o2, {**t1, **t2})
    rows = bt_spread(txn=1, underlying="AAPL")
    rows += bt_spread(txn=2, underlying="NVDA")
    rows += bt_spread(txn=3, underlying="MSFT", entry_day="2024-05-02",
                      exit_day="2024-05-09")
    bt = backtest_structures(rows)
    report = compare(live, bt, Tolerances())
    assert report["summary"]["paired"] == 1
    lu = {u["underlying"]: u for u in report["unmatched_live"]}
    bu = {u["underlying"]: u for u in report["unmatched_backtest"]}
    assert set(lu) == {"MSFT"} and set(bu) == {"NVDA", "MSFT"}
    assert "2024-05-02" in lu["MSFT"]["reason"]          # BT entered it a session later
    assert "live did not" in bu["NVDA"]["reason"]


def test_error_records_are_reported_never_skipped():
    err_entry = {"version": OPTION_TRADE_RECORD_VERSION, "error": "KeyError: boom"}
    err_exit = {"version": OPTION_TRADE_RECORD_VERSION, "trigger": "stop_loss",
                "rule_id": None, "rule_name": None, "error": "ValueError: no quote"}
    orders, txns = live_spread(entry_record=err_entry, exit_record=err_exit)
    live = live_structures(orders, txns)
    assert live[0]["entry_record_status"] == "error"
    assert live[0]["key_source"] == KEY_FROM_FILL      # still pairable
    bt = backtest_structures(bt_spread(head_error="TypeError: bt boom"))
    assert bt[0]["entry_record_status"] == "error"
    report = compare(live, bt, Tolerances())
    assert report["summary"]["paired"] == 1
    rec_rows = [d for d in report["diffs"] if d["field"] in ("entry_record", "exit_record")]
    assert any("KeyError: boom" in str(d["live"]) for d in rec_rows)
    assert any("TypeError: bt boom" in str(d["backtest"]) for d in rec_rows)
    assert any("ValueError: no quote" in str(d["live"]) for d in rec_rows)
    trig = [d for d in report["diffs"] if d["field"] == "exit_trigger"][0]
    assert trig["live"] == "stop_loss" and trig["backtest"] == "take_profit"
    assert trig["within_tolerance"] is False
    assert report["summary"]["record_errors"]["live"] >= 2
    assert report["summary"]["record_errors"]["backtest"] >= 1


def test_fill_and_pnl_diffs_are_computed_per_leg_and_net():
    orders, txns = live_spread(entry_fills=(5.25, 2.8), exit_fills=(7.0, 3.5))
    live = live_structures(orders, txns)
    bt = backtest_structures(bt_spread(entry_fills=(5.3, 2.8), exit_fills=(7.0, 3.5)))
    report = compare(live, bt, Tolerances())
    fill = [d for d in report["diffs"] if d["field"] == "entry_fill" and d["leg"] == LONG_C][0]
    assert fill["abs_delta"] == pytest.approx(0.05)
    slip = [d for d in report["diffs"] if d["field"] == "entry_slippage_vs_mid"
            and d["leg"] == LONG_C][0]
    assert slip["live"] == pytest.approx(0.15) and slip["backtest"] == pytest.approx(0.2)
    net = [d for d in report["diffs"] if d["field"] == "net_gross_pnl"][0]
    # long leg: (7.0-5.25)*2*100 = 350 live vs 340 bt; short leg identical
    assert net["abs_delta"] == pytest.approx(-10.0)


def test_window_and_symbol_filters():
    o1, t1 = live_spread(txn=10, underlying="AAPL", base_id=100)
    live = live_structures(o1, t1)
    bt = backtest_structures(bt_spread(txn=1, underlying="AAPL"))
    r = compare(live, bt, Tolerances(), start="2024-05-02")
    assert r["summary"]["live_structures"] == 0 and r["summary"]["backtest_structures"] == 0
    r = compare(live, bt, Tolerances(), symbols=["MSFT"])
    assert r["summary"]["live_structures"] == 0
    r = compare(live, bt, Tolerances(), start="2024-05-01", end="2024-05-01",
                symbols=["aapl"])
    assert r["summary"]["paired"] == 1


def test_open_structure_is_reported_as_open_not_as_a_missing_exit():
    orders, txns = live_spread(with_exit=False, close_reason=None)
    live = live_structures(orders, txns)
    assert live[0]["exit"]["status"] == "open"
    rows = bt_spread()
    for r in rows:
        r["exit_reason"] = "open_at_end"
        r["exit_record"] = None
    bt = backtest_structures(rows)
    assert bt[0]["exit"]["status"] == "open"
    report = compare(live, bt, Tolerances())
    # "open_at_end" is the run ending, not a close reason: no false close_reason diff
    assert bt[0]["exit"]["close_reason"] is None
    assert not [d for d in report["diffs"]
                if d["field"] in ("exit_record", "close_reason", "exit_trigger",
                                  "exit_data_session") and not d["within_tolerance"]]


def test_live_input_is_not_mutated():
    orders, txns = live_spread()
    snapshot = copy.deepcopy(orders)
    live_structures(orders, txns)
    assert orders == snapshot
