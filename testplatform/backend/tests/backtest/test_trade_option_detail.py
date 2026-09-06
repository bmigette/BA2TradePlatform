"""The trade list's OPTION detail: what a leg IS, and which legs are one bet.

Asked for from live use on 2026-09-04: "the backtest result in the test platform
do not show option detail in trade list. It should show option types and maybe
expand lines to show legs... Also show premium / strike."

Everything needed was already recorded and then dropped on the way out. The
round-trip recorder publishes ``contract_symbol`` / ``underlying_symbol`` /
``transaction_id`` / ``multiplier``; ``Backtest._transform_trades_for_frontend``
carried none of them, so an option leg reached the UI as a bare symbol and a
price -- and a price that is a PREMIUM PER SHARE reads exactly like a share
price, which is the part that actively misleads.

These pin the three hops the data now makes: the recorder emits the contract's
own terms, ``_trade_row`` passes them through the persisted blob, and the
frontend transform hands them over in the shape the table consumes.

Run from the backend dir:
    python -m pytest tests/backtest/test_trade_option_detail.py -v
"""
from __future__ import annotations

from app.models.backtest import Backtest
from app.services.backtest.results import _trade_row


# ---------------------------------------------------------------------------
# _trade_row: the persisted blob
# ---------------------------------------------------------------------------

def test_trade_row_carries_the_contracts_own_terms():
    row = _trade_row({
        "symbol": "AAPL240119C00150000",
        "entry_time": "2024-01-02T09:30:00",
        "exit_time": "2024-01-05T16:00:00",
        "direction": "buy",
        "entry_price": 4.20,
        "exit_price": 6.10,
        "size": 2,
        "pnl": 380.0,
        "pnl_pct": 3.8,
        "contract_symbol": "AAPL240119C00150000",
        "underlying_symbol": "AAPL",
        "option_type": "call",
        "strike": 150.0,
        "expiry": "2024-01-19",
        "multiplier": 100,
        "transaction_id": 77,
    })

    assert row["option_type"] == "call"
    assert row["strike"] == 150.0
    assert row["expiry"] == "2024-01-19"
    assert row["underlying_symbol"] == "AAPL"
    assert row["contract_symbol"] == "AAPL240119C00150000"
    assert row["multiplier"] == 100.0
    assert row["transaction_id"] == 77


def test_trade_row_leaves_an_equity_trade_with_no_option_terms():
    """The discriminator: no ``option_type`` is what tells the UI a row is not an
    option. It must be absent-as-None on equity, never a fabricated 'stock'."""
    row = _trade_row({
        "symbol": "AAPL", "entry_time": "2024-01-02T09:30:00",
        "exit_time": "2024-01-05T16:00:00", "direction": "buy",
        "entry_price": 150.0, "exit_price": 155.0, "size": 10, "pnl": 50.0,
    })

    assert row["option_type"] is None
    assert row["strike"] is None
    assert row["expiry"] is None
    assert row["contract_symbol"] is None
    # The equity no-op, and the reason ``multiplier`` defaults to 1.0 rather than
    # None: every consumer multiplies by it.
    assert row["multiplier"] == 1.0


# ---------------------------------------------------------------------------
# _transform_trades_for_frontend: the shape the table consumes
# ---------------------------------------------------------------------------

def _transform(trades):
    bt = Backtest()
    bt.trades = trades
    return bt._transform_trades_for_frontend()


def test_the_frontend_transform_hands_over_the_option_detail():
    out = _transform([{
        "symbol": "AAPL240119C00150000",
        "entry_time": "2024-01-02T09:30:00", "exit_time": "2024-01-05T16:00:00",
        "direction": "buy", "entry_price": 4.20, "exit_price": 6.10, "size": 2,
        "pnl": 380.0, "pnl_pct": 3.8, "bars_held": 3, "exit_reason": "take_profit",
        "contract_symbol": "AAPL240119C00150000", "underlying_symbol": "AAPL",
        "option_type": "call", "strike": 150.0, "expiry": "2024-01-19",
        "multiplier": 100, "transaction_id": 77,
    }])

    assert len(out) == 1
    row = out[0]
    assert row["optionType"] == "call"
    assert row["strike"] == 150.0
    assert row["expiry"] == "2024-01-19"
    assert row["underlyingSymbol"] == "AAPL"
    assert row["contractSymbol"] == "AAPL240119C00150000"
    assert row["multiplier"] == 100
    assert row["transactionId"] == 77
    # ...and the fields that were already there are untouched.
    assert row["entryPrice"] == 4.20
    assert row["direction"] == "long"


def test_the_frontend_transform_leaves_an_equity_row_option_free():
    out = _transform([{
        "symbol": "AAPL", "entry_time": "2024-01-02T09:30:00",
        "exit_time": "2024-01-05T16:00:00", "direction": "sell",
        "entry_price": 150.0, "exit_price": 145.0, "size": 10, "pnl": 50.0,
        "pnl_pct": 0.5, "bars_held": 3, "exit_reason": "stop_loss",
    }])

    row = out[0]
    assert row["optionType"] is None
    assert row["strike"] is None
    assert row["expiry"] is None
    assert row["contractSymbol"] is None
    assert row["transactionId"] is None
    # An equity row's multiplier is 1, so premium x size x multiplier is just the
    # notional -- the UI needs no special case for it.
    assert row["multiplier"] == 1
    assert row["direction"] == "short"


def test_the_legs_of_one_structure_carry_the_same_transaction_id():
    """What the trade list folds on. Four rows, one bet: without a shared id the
    table cannot tell a condor from four unrelated trades on the same underlying."""
    legs = [{
        "symbol": f"AAPL240119{right}{strike:08.0f}",
        "entry_time": "2024-01-02T09:30:00", "exit_time": "2024-01-19T16:00:00",
        "direction": direction, "entry_price": premium, "exit_price": 0.0,
        "size": 1, "pnl": 0.0, "pnl_pct": 0.0, "bars_held": 13,
        "exit_reason": "expired", "contract_symbol": f"AAPL240119{right}",
        "underlying_symbol": "AAPL", "option_type": "call" if right == "C" else "put",
        "strike": strike, "expiry": "2024-01-19", "multiplier": 100,
        "transaction_id": 42,
    } for right, strike, direction, premium in (
        ("P", 140.0, "sell", 1.10), ("P", 135.0, "buy", 0.60),
        ("C", 160.0, "sell", 1.20), ("C", 165.0, "buy", 0.55))]

    out = _transform(legs)

    assert {row["transactionId"] for row in out} == {42}
    assert len(out) == 4
    assert {row["optionType"] for row in out} == {"call", "put"}
    # Every leg keeps its OWN strike -- the structure is the group, not a merge.
    assert sorted(row["strike"] for row in out) == [135.0, 140.0, 160.0, 165.0]


def test_the_transform_survives_a_blob_written_before_the_option_columns_existed():
    """Old persisted runs carry none of these keys. They must come back None
    rather than raising -- the results blob is durable and is re-read for years."""
    out = _transform([{
        "symbol": "AAPL", "entry_time": "2023-01-02T09:30:00",
        "exit_time": "2023-01-05T16:00:00", "direction": "buy",
        "entry_price": 150.0, "exit_price": 155.0, "size": 10,
        "pnl": 50.0, "pnl_pct": 0.5, "bars_held": 3, "exit_reason": "exit",
    }])

    row = out[0]
    assert row["optionType"] is None
    assert row["multiplier"] == 1        # the documented default, not a KeyError
    assert row["transactionId"] is None
