"""EX-2: shared FMP congressional-trade amount parser."""
from ba2_trade_platform.core.utils import parse_fmp_amount_range as parse


class TestParseFmpAmountRange:
    def test_range_returns_midpoint(self):
        assert parse("$15,001 - $50,000") == (15001 + 50000) / 2

    def test_single_value(self):
        assert parse("$1,000") == 1000.0

    def test_large_range(self):
        assert parse("$1,000,001 - $5,000,000") == (1000001 + 5000000) / 2

    def test_empty_and_none(self):
        assert parse("") == 0.0
        assert parse(None) == 0.0
        assert parse("0") == 0.0

    def test_non_numeric(self):
        assert parse("N/A") == 0.0

    def test_numeric_input(self):
        assert parse(5000) == 5000.0
