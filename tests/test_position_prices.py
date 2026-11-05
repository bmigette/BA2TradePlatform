"""Open-P/L views price a held position at the BROKER's position price (2026-10-29: FTNT fell ~25%
after hours; the Floating P/L card read the broker, Live Trades read FMP, and they disagreed)."""
from ba2_trade_platform.ui.utils.position_prices import broker_position_prices_from, held_position_prices


class _Account:
    def __init__(self, positions, feed, *, raises=False):
        self._positions = positions
        self._feed = feed
        self._raises = raises
        self.feed_asked = []

    def get_positions(self):
        if self._raises:
            raise RuntimeError("broker 503")
        return self._positions

    def get_instrument_current_price(self, symbols):
        self.feed_asked.append(list(symbols))
        return {s: self._feed.get(s) for s in symbols}


def test_held_symbol_takes_the_broker_price_not_the_feed():
    acct = _Account([{"symbol": "FTNT", "current_price": 130.02}], {"FTNT": 174.08, "MRK": 141.12})
    assert held_position_prices(acct, 1, ["FTNT", "MRK"]) == {"FTNT": 130.02, "MRK": 141.12}
    assert acct.feed_asked == [["MRK"]]


def test_failed_position_read_falls_to_the_feed_for_every_symbol():
    for acct in (_Account(None, {"FTNT": 174.08}), _Account([], {"FTNT": 174.08}, raises=True)):
        assert held_position_prices(acct, 1, ["FTNT"]) == {"FTNT": 174.08}


def test_unquoted_position_is_left_out_not_zeroed():
    assert broker_position_prices_from([{"symbol": "X", "current_price": None},
                                        {"symbol": "Y", "current_price": "12.5"}]) == {"Y": 12.5}


def test_feed_none_is_not_a_price():
    acct = _Account([], {"FTNT": None})
    assert held_position_prices(acct, 1, ["FTNT"]) == {}
