"""Overview quantity-mismatch alert: shares are compared with shares.

The check used to add up EVERY open transaction on a ticker, so a buy-write (100 shares plus one
short covered call, both filed under the underlying's ticker) read as 101 against the broker's
100 shares and raised a false alert on every covered-call position.
"""
from types import SimpleNamespace

from ba2_common.core.types import AssetClass

from ba2_trade_platform.ui.utils.quantity_mismatch import find_quantity_mismatches


def _txn(tid, symbol, quantity, asset_class=AssetClass.EQUITY):
    return SimpleNamespace(id=tid, symbol=symbol, quantity=quantity, asset_class=asset_class)


def _pos(symbol, qty, asset_class="us_equity"):
    return {"symbol": symbol, "qty": qty, "asset_class": asset_class}


def test_a_covered_call_is_not_a_share_mismatch():
    positions = [_pos("B", "100"), _pos("B261218C00045000", "-1", "us_option")]
    transactions = [_txn(28, "B", 100.0), _txn(29, "B", 1.0, AssetClass.OPTION)]
    assert find_quantity_mismatches(positions, transactions) == []


def test_fewer_shares_at_the_broker_than_in_open_transactions_is_reported():
    shares = _txn(1, "INTC", 100.0)
    call = _txn(2, "INTC", 1.0, AssetClass.OPTION)
    got = find_quantity_mismatches([_pos("INTC", "60")], [shares, call])
    assert got == [{"symbol": "INTC", "broker_qty": 60.0, "transaction_qty": 100.0,
                    "difference": 40.0, "transactions": [shares]}]


def test_several_share_transactions_on_one_ticker_are_summed():
    a, b = _txn(1, "KO", 30.0), _txn(2, "KO", 20.0)
    assert find_quantity_mismatches([_pos("KO", "50")], [a, b]) == []
    got = find_quantity_mismatches([_pos("KO", "40")], [a, b])
    assert [(m["symbol"], m["transaction_qty"], m["transactions"]) for m in got] == [("KO", 50.0, [a, b])]


def test_more_shares_at_the_broker_and_float_dust_are_not_reported():
    assert find_quantity_mismatches([_pos("KO", "120")], [_txn(1, "KO", 100.0)]) == []
    assert find_quantity_mismatches([_pos("KO", "99.995")], [_txn(1, "KO", 100.0)]) == []


def test_an_option_position_never_counts_as_shares_even_under_a_ticker_symbol():
    # A broker that files an option position under the underlying's ticker must not be read as shares.
    got = find_quantity_mismatches([_pos("KO", "-1", "us_option")], [_txn(1, "KO", 100.0)])
    assert got == []


def test_the_asset_class_may_be_an_enum_a_string_or_absent():
    enum_like = SimpleNamespace(value="us_option")
    assert find_quantity_mismatches([{"symbol": "KO", "qty": "-1", "asset_class": enum_like}],
                                    [_txn(1, "KO", 100.0)]) == []
    legacy = SimpleNamespace(id=7, symbol="KO", quantity=100.0, asset_class=None)   # pre-option rows
    got = find_quantity_mismatches([{"symbol": "KO", "qty": "40"}], [legacy])
    assert [(m["symbol"], m["difference"]) for m in got] == [("KO", 60.0)]
    assert find_quantity_mismatches([_pos("KO", "100")], [_txn(1, "KO", 1.0, "option")]) == []
    # The database stores the enum NAME ("OPTION"), which is what a raw row carries.
    assert find_quantity_mismatches([_pos("KO", "100")], [_txn(1, "KO", 100.0, "EQUITY"),
                                                          _txn(2, "KO", 1.0, "OPTION")]) == []
