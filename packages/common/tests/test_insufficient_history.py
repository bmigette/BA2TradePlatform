"""A symbol too new for an indicator window is insufficient, not computed (owner 2026-10-24)."""
from ba2_common.core.ohlcv_history import InsufficientHistory
from ba2_common.core.position_sizing import get_latest_atr


class Short:
    def get_indicator(self, *a, **k):
        raise InsufficientHistory("IPO", "atr", 9, 14)


def test_message_names_symbol_indicator_and_counts():
    e = InsufficientHistory("IPO", "close_200_sma", 120, 200)
    assert "IPO" in str(e) and "close_200_sma" in str(e) and "200" in str(e) and "120" in str(e)
    assert (e.symbol, e.have, e.need) == ("IPO", 120, 200)


def test_atr_of_a_too_new_symbol_is_none_not_a_crash():
    assert get_latest_atr("IPO", Short(), period=14) is None
