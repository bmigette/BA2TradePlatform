import pandas as pd
from datetime import date
import pytest

from ba2_trade_platform.core.TradeConditions import create_condition
from ba2_trade_platform.core.types import (
    ExpertEventType, OrderDirection, OrderType, OrderStatus, TransactionStatus,
    AssetClass, OptionRight,
)
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
import ba2_trade_platform.modules.dataproviders as dp


class _FakeOHLCV:
    def __init__(self, df): self._df = df
    def get_ohlcv_data(self, *a, **k): return self._df


def _patch_ohlcv(monkeypatch, df):
    monkeypatch.setattr(dp, "get_provider", lambda category, name, **k: _FakeOHLCV(df), raising=True)


def test_percent_below_recent_high(monkeypatch, mock_account, sample_recommendation):
    # recent high 200, current price 150 -> 25% below high
    df = pd.DataFrame({"Date": pd.date_range("2026-01-01", periods=20),
                       "Open": [180]*20, "High": [200]*20, "Low": [150]*20,
                       "Close": [160]*20, "Volume": [1e6]*20})
    _patch_ohlcv(monkeypatch, df)
    cond = create_condition(ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, mock_account, "AAPL",
                            sample_recommendation, operator_str=">=", value=20.0)
    assert cond.evaluate() is True          # 25% >= 20%
    cond2 = create_condition(ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, mock_account, "AAPL",
                             sample_recommendation, operator_str=">=", value=30.0)
    assert cond2.evaluate() is False        # 25% < 30%
    assert "%" in (cond.get_actual_value_display() or "")


def test_percent_above_recent_low(monkeypatch, mock_account, sample_recommendation):
    # recent low 100, current 150 -> 50% above low
    df = pd.DataFrame({"Date": pd.date_range("2026-01-01", periods=20),
                       "Open": [120]*20, "High": [160]*20, "Low": [100]*20,
                       "Close": [140]*20, "Volume": [1e6]*20})
    _patch_ohlcv(monkeypatch, df)
    cond = create_condition(ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW, mock_account, "AAPL",
                            sample_recommendation, operator_str=">=", value=40.0)
    assert cond.evaluate() is True


def test_iv_rank_condition(mock_account, sample_recommendation):
    for iv in (0.10, 0.20, 0.30, 0.40, 0.50):
        mock_account.record_atm_iv("AAPL", iv)
    # current ATM IV (mock) = 0.30 -> rank 40 (2 of 5 below)
    cond = create_condition(ExpertEventType.N_IV_RANK, mock_account, "AAPL",
                            sample_recommendation, operator_str="<=", value=50.0)
    assert cond.evaluate() is True          # rank 40 <= 50
    cond2 = create_condition(ExpertEventType.N_IV_RANK, mock_account, "AAPL",
                             sample_recommendation, operator_str=">=", value=60.0)
    assert cond2.evaluate() is False


def test_has_option_position(mock_account, mock_expert_instance, sample_recommendation):
    txn_id = add_instance(Transaction(symbol="AAPL", quantity=2, side=OrderDirection.BUY,
                                      status=TransactionStatus.OPENED, open_price=5.2,
                                      expert_id=mock_expert_instance.id))
    add_instance(TradingOrder(account_id=mock_account.id, symbol="AAPL", quantity=2,
                              side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
                              status=OrderStatus.FILLED, filled_qty=2, transaction_id=txn_id,
                              asset_class=AssetClass.OPTION, option_type=OptionRight.CALL,
                              underlying_symbol="AAPL", option_strategy="long_call"))
    cond = create_condition(ExpertEventType.F_HAS_OPTION_POSITION, mock_account, "AAPL",
                            sample_recommendation)
    assert cond.evaluate() is True
    # different symbol -> False
    cond2 = create_condition(ExpertEventType.F_HAS_OPTION_POSITION, mock_account, "MSFT",
                             sample_recommendation)
    assert cond2.evaluate() is False


def test_has_covered_call(mock_account, mock_expert_instance, sample_recommendation):
    txn_id = add_instance(Transaction(symbol="AAPL", quantity=1, side=OrderDirection.SELL,
                                      status=TransactionStatus.OPENED, open_price=2.0,
                                      expert_id=mock_expert_instance.id))
    add_instance(TradingOrder(account_id=mock_account.id, symbol="AAPL", quantity=1,
                              side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
                              status=OrderStatus.FILLED, filled_qty=1, transaction_id=txn_id,
                              asset_class=AssetClass.OPTION, option_type=OptionRight.CALL,
                              underlying_symbol="AAPL", option_strategy="covered_call"))
    cond = create_condition(ExpertEventType.F_HAS_COVERED_CALL, mock_account, "AAPL",
                            sample_recommendation)
    assert cond.evaluate() is True
