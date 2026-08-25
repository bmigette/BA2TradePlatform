from datetime import datetime, timedelta, timezone

from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface


def _seed_daily(account, underlying, ivs):
    """Seed one ATM-IV sample per past day, oldest first.

    ``record_atm_iv`` is deliberately one-row-per-(account, underlying)-per-UTC-day
    (see its docstring and tests/test_iv_rank_wiring.py), so a multi-sample series can
    only be built across distinct days — which is exactly what the daily recorder
    produces in production.
    """
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import OptionIVSnapshot
    now = datetime.now(timezone.utc)
    for i, iv in enumerate(ivs):
        add_instance(OptionIVSnapshot(
            account_id=account.id, underlying=underlying, atm_iv=iv,
            recorded_at=now - timedelta(days=len(ivs) - i),
        ))


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
    _seed_daily(mock_account, "AAPL", (0.10, 0.20, 0.30, 0.40, 0.50))
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


def test_get_iv_rank_excludes_samples_beyond_lookback(mock_account):
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import OptionIVSnapshot

    # 3 in-window samples (one per recent day) + 1 ancient sample that must be excluded.
    _seed_daily(mock_account, "AAPL", (0.10, 0.20, 0.50))
    old = OptionIVSnapshot(
        account_id=mock_account.id, underlying="AAPL", atm_iv=0.99,
        recorded_at=datetime.now(timezone.utc) - timedelta(days=400),
    )
    add_instance(old)
    # current = 0.30 (mock). With lookback 252d, only [0.10,0.20,0.50] count.
    # 2 of 3 strictly below 0.30 -> 66.67. The excluded 0.99 would have changed this.
    rank = mock_account.get_iv_rank("AAPL", lookback_days=252, min_samples=3)
    assert rank == round(2 / 3 * 100, 2)  # 66.67

    # Sanity: a huge lookback that INCLUDES the old 0.99 sample changes the result
    rank_all = mock_account.get_iv_rank("AAPL", lookback_days=100000, min_samples=3)
    assert rank_all == round(2 / 4 * 100, 2)  # 50.0 (2 of 4 below 0.30)
