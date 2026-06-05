from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface


def test_iv_rank_percentile_math():
    series = [0.10, 0.20, 0.30, 0.40, 0.50]
    # 2 of 5 strictly below 0.30 => 40.0
    assert OptionsAccountInterface._iv_rank_from_series(series, current=0.30, min_samples=5) == 40.0
    # fewer than min_samples => None (default min_samples=20)
    assert OptionsAccountInterface._iv_rank_from_series([0.2], current=0.2) is None
    # current None => None
    assert OptionsAccountInterface._iv_rank_from_series(series, current=None, min_samples=1) is None
    # empty => None
    assert OptionsAccountInterface._iv_rank_from_series([], current=0.3, min_samples=1) is None
    # None values in series are ignored for the count/threshold
    assert OptionsAccountInterface._iv_rank_from_series([0.1, None, 0.5], current=0.2, min_samples=2) == 50.0


def test_record_and_rank_roundtrip(mock_account):
    # mock_account.get_atm_implied_volatility("AAPL") returns 0.30
    for iv in (0.10, 0.20, 0.30, 0.40, 0.50):
        mock_account.record_atm_iv("AAPL", iv)
    rank = mock_account.get_iv_rank("AAPL", min_samples=3)
    assert rank == 40.0  # 2 of 5 below current 0.30


def test_record_atm_iv_uses_current_when_iv_none(mock_account):
    # When iv arg omitted, record uses get_atm_implied_volatility (0.30 for AAPL)
    sid = mock_account.record_atm_iv("AAPL")
    assert sid is not None
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import OptionIVSnapshot
    row = get_instance(OptionIVSnapshot, sid)
    assert row.atm_iv == 0.30 and row.underlying == "AAPL"
