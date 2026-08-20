"""The Settings > Instruments import path must normalise the symbols it stores.

`parse_instrument_symbol_list` is the pure helper behind the .txt upload in
ui/pages/settings.py. The upload handler itself is a closure inside a NiceGUI
dialog and is unreachable from a test, so the parsing contract is pinned here and
the UI simply calls it.
"""
from ba2_trade_platform.core.utils import parse_instrument_symbol_list


def test_parse_symbol_list_uppercases_and_strips_each_line():
    assert parse_instrument_symbol_list("aapl\n  msft  \nNvDa") == ['AAPL', 'MSFT', 'NVDA']


def test_parse_symbol_list_drops_blank_and_whitespace_only_lines():
    assert parse_instrument_symbol_list("AAPL\n\n   \nMSFT\n") == ['AAPL', 'MSFT']


def test_parse_symbol_list_dedupes_case_variants_preserving_first_seen_order():
    assert parse_instrument_symbol_list("msft\nAAPL\nMSFT\n aapl ") == ['MSFT', 'AAPL']


def test_parse_symbol_list_empty_input_returns_empty_list():
    assert parse_instrument_symbol_list("") == []
    assert parse_instrument_symbol_list(None) == []
