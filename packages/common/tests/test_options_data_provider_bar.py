"""``OptionEodBar`` must be able to carry a VENDOR-SUPPLIED implied volatility.

The type was written on the assumption that no vendor serves IV, so the platform inverts
Black-Scholes off the contract's own close. dxfeed does serve it (``imp_volatility`` on every
candle), and a vendor print beats an inversion: the inversion needs a risk-free rate, a
dividend assumption and a clean close, and it cannot produce anything at all for the
1,083,571 rows whose bid/ask were fabricated as ``bid = ask = close``.

Adding the field is additive — it defaults to ``None`` and sits last, so every existing
construction (Alpaca's and ThetaData's, positional or keyword) is unchanged.
"""
from dataclasses import fields
from datetime import date

from ba2_common.core.interfaces import OptionEodBar


def test_the_bar_can_carry_a_vendor_supplied_iv():
    b = OptionEodBar(occ_symbol="AAPL230120C00150000", bar_date=date(2023, 1, 3),
                     open=7.0, high=7.5, low=6.8, close=7.25, volume=911,
                     open_interest=12345, iv=0.2841)
    assert b.iv == 0.2841
    assert b.open_interest == 12345


def test_iv_defaults_to_none_so_a_vendor_without_it_is_unchanged():
    """Absent must stay absent: a 0.0 default would read as a free option downstream."""
    b = OptionEodBar(occ_symbol="AAPL230120C00150000", bar_date=date(2023, 1, 3),
                     open=7.0, high=7.5, low=6.8, close=7.25)
    assert b.iv is None


def test_iv_is_the_last_field_so_positional_construction_is_backward_compatible():
    names = [f.name for f in fields(OptionEodBar)]
    assert names[-1] == "iv"
    assert names.index("open_interest") < names.index("iv")
    # The pre-existing positional order every current caller relies on.
    assert names[:9] == ["occ_symbol", "bar_date", "open", "high", "low", "close",
                         "volume", "bid", "ask"]


def test_the_bar_is_still_immutable():
    import pytest
    b = OptionEodBar(occ_symbol="X", bar_date=date(2023, 1, 3), open=1.0, high=1.0,
                     low=1.0, close=1.0)
    with pytest.raises(Exception):
        b.iv = 0.5  # type: ignore[misc]
