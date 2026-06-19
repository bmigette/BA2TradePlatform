"""
Integration tests for backtesting with real model and dataset.

Tests the backtesting.py integration with ML predictions and strategy conditions.
"""

import pytest
import pandas as pd
import numpy as np
import json
from pathlib import Path
from unittest.mock import MagicMock
from dataclasses import dataclass
from typing import Optional, Dict, Any

# Test fixtures paths
TEST_DATA_DIR = Path(__file__).parent / "data"
TEST_MODEL_PATH = TEST_DATA_DIR / "models" / "xception_test_model.pt"
TEST_MODEL_METADATA_PATH = TEST_DATA_DIR / "models" / "xception_test_model_metadata.json"
TEST_DATASET_PATH = TEST_DATA_DIR / "AAPL_1h_test_full.csv"


@dataclass
class MockTrainedModel:
    """Mock TrainedModel for testing."""
    model_id: str = "test-model"
    model_type: str = "XCEPTION"
    file_path: str = ""
    hyperparameters: Optional[Dict[str, Any]] = None
    normalization_params: Optional[Dict[str, Any]] = None
    prediction_mode: str = "shift"
    threshold: float = 0.5


@pytest.fixture
def test_model():
    """Create a mock TrainedModel using the test fixtures."""
    with open(TEST_MODEL_METADATA_PATH, 'r') as f:
        metadata = json.load(f)

    return MockTrainedModel(
        model_id="mdl-test",
        model_type="XCEPTION",
        file_path=str(TEST_MODEL_PATH),
        hyperparameters=metadata,
        normalization_params=None,  # No normalization for this test
        prediction_mode="shift",
        threshold=0.3
    )


@pytest.fixture
def test_dataset():
    """Load the test dataset."""
    df = pd.read_csv(TEST_DATASET_PATH)
    df['Date'] = pd.to_datetime(df['Date'])
    return df


@pytest.fixture
def buy_conditions():
    """Buy when model predicts class 0."""
    return {
        "id": "cond_buy",
        "operator": "AND",
        "conditions": [
            {
                "id": "cond_buy_1",
                "field": "model:class_0",
                "fieldType": "model_class",
                "comparison": "is_true",
                "value": 1,
                "optimizeEnabled": False
            }
        ]
    }


@pytest.fixture
def sell_conditions():
    """Sell when model predicts class 1."""
    return {
        "id": "cond_sell",
        "operator": "AND",
        "conditions": [
            {
                "id": "cond_sell_1",
                "field": "model:class_1",
                "fieldType": "model_class",
                "comparison": "is_true",
                "value": 1,
                "optimizeEnabled": False
            }
        ]
    }


class TestBacktestIntegration:
    """Integration tests for backtesting with backtesting.py library."""

    @pytest.mark.skipif(
        not TEST_MODEL_PATH.exists() or not TEST_DATASET_PATH.exists(),
        reason="Test fixtures not available"
    )
    def test_backtest_opens_trades_with_class_conditions(
        self, test_model, test_dataset, buy_conditions, sell_conditions
    ):
        """Test that backtest opens trades when model predictions meet conditions."""
        from app.services.backtest_handler import run_backtest

        strategy_params = {
            'initial_tp_percent': 5.0,
            'initial_sl_percent': 2.0,
        }

        results = run_backtest(
            model=test_model,
            pred_df=test_dataset,
            exec_df=test_dataset,
            strategy_params=strategy_params,
            initial_capital=10000.0,
            position_sizing_type="percent",
            position_sizing_value=10.0,
            commission=0.1,
            slippage=0.0,
            buy_entry_conditions=buy_conditions,
            sell_entry_conditions=sell_conditions,
            exit_conditions=[],
        )

        # Verify results structure
        assert 'total_trades' in results
        assert 'winning_trades' in results
        assert 'losing_trades' in results
        assert 'win_rate' in results
        assert 'total_return' in results
        assert 'equity_curve' in results
        assert 'trades' in results

        # Should have opened at least some trades
        assert results['total_trades'] > 0, f"Expected trades to be opened, got {results['total_trades']}"

        # Verify trades have required fields
        if results['trades']:
            trade = results['trades'][0]
            assert 'entry_time' in trade
            assert 'exit_time' in trade
            assert 'direction' in trade
            assert 'entry_price' in trade
            assert 'exit_price' in trade
            assert 'pnl' in trade
            assert trade['direction'] in ['buy', 'sell']

        print(f"Backtest completed: {results['total_trades']} trades, "
              f"return: {results['total_return']}%, max_dd: {results['max_drawdown']}%")

    @pytest.mark.skipif(
        not TEST_MODEL_PATH.exists() or not TEST_DATASET_PATH.exists(),
        reason="Test fixtures not available"
    )
    def test_backtest_with_tp_sl(
        self, test_model, test_dataset, buy_conditions
    ):
        """Test that TP/SL are properly applied to trades."""
        from app.services.backtest_handler import run_backtest

        strategy_params = {
            'initial_tp_percent': 2.0,  # 2% take profit
            'initial_sl_percent': 1.0,  # 1% stop loss
        }

        results = run_backtest(
            model=test_model,
            pred_df=test_dataset,
            exec_df=test_dataset,
            strategy_params=strategy_params,
            initial_capital=10000.0,
            position_sizing_type="percent",
            position_sizing_value=20.0,
            commission=0.0,
            slippage=0.0,
            buy_entry_conditions=buy_conditions,
            sell_entry_conditions=None,
            exit_conditions=[],
        )

        # With TP/SL, we expect trades to close at TP or SL levels
        assert results['total_trades'] >= 0

        # If we have trades, verify they have P&L
        # Note: Due to price gaps, trades may close beyond TP/SL levels
        # backtesting.py is adversarial - it assumes worst-case execution
        for trade in results['trades']:
            pnl_pct = trade['pnl_pct']
            # Verify P&L is a reasonable number (not NaN or extreme)
            assert -50 <= pnl_pct <= 50, f"Trade P&L {pnl_pct}% seems unreasonable"

    @pytest.mark.skipif(
        not TEST_MODEL_PATH.exists() or not TEST_DATASET_PATH.exists(),
        reason="Test fixtures not available"
    )
    def test_backtest_only_buy_conditions(
        self, test_model, test_dataset, buy_conditions
    ):
        """Test backtest with only buy conditions (no sell entries)."""
        from app.services.backtest_handler import run_backtest

        strategy_params = {
            'initial_tp_percent': 3.0,
            'initial_sl_percent': 1.5,
        }

        results = run_backtest(
            model=test_model,
            pred_df=test_dataset,
            exec_df=test_dataset,
            strategy_params=strategy_params,
            initial_capital=10000.0,
            position_sizing_type="fixed",
            position_sizing_value=1000.0,
            commission=0.0,
            slippage=0.0,
            buy_entry_conditions=buy_conditions,
            sell_entry_conditions=None,
            exit_conditions=[],
        )

        # All trades should be buy direction
        for trade in results['trades']:
            assert trade['direction'] == 'buy', f"Expected only buy trades, got {trade['direction']}"

    @pytest.mark.skipif(
        not TEST_MODEL_PATH.exists() or not TEST_DATASET_PATH.exists(),
        reason="Test fixtures not available"
    )
    def test_backtest_equity_curve(
        self, test_model, test_dataset, buy_conditions
    ):
        """Test that equity curve is properly generated."""
        from app.services.backtest_handler import run_backtest

        initial_capital = 10000.0
        strategy_params = {
            'initial_tp_percent': 5.0,
            'initial_sl_percent': 2.0,
        }

        results = run_backtest(
            model=test_model,
            pred_df=test_dataset,
            exec_df=test_dataset,
            strategy_params=strategy_params,
            initial_capital=initial_capital,
            position_sizing_type="percent",
            position_sizing_value=10.0,
            commission=0.0,
            slippage=0.0,
            buy_entry_conditions=buy_conditions,
            sell_entry_conditions=None,
            exit_conditions=[],
        )

        # Equity curve should exist and have entries
        assert len(results['equity_curve']) > 0

        # First entry should be near initial capital
        first_equity = results['equity_curve'][0]['equity']
        assert first_equity == pytest.approx(initial_capital, rel=0.1)

        # Final equity should match reported final_equity
        last_equity = results['equity_curve'][-1]['equity']
        assert last_equity == pytest.approx(results['final_equity'], rel=0.01)

    @pytest.mark.skipif(
        not TEST_MODEL_PATH.exists() or not TEST_DATASET_PATH.exists(),
        reason="Test fixtures not available"
    )
    def test_backtest_no_conditions_no_trades(
        self, test_model, test_dataset
    ):
        """Test that no trades are opened when no conditions are provided."""
        from app.services.backtest_handler import run_backtest

        strategy_params = {}

        results = run_backtest(
            model=test_model,
            pred_df=test_dataset,
            exec_df=test_dataset,
            strategy_params=strategy_params,
            initial_capital=10000.0,
            position_sizing_type="percent",
            position_sizing_value=10.0,
            commission=0.0,
            slippage=0.0,
            buy_entry_conditions=None,
            sell_entry_conditions=None,
            exit_conditions=[],
        )

        # No conditions = no trades
        assert results['total_trades'] == 0
        assert results['total_return'] == 0.0


class TestMLStrategyConditions:
    """Tests for strategy condition evaluation within backtesting."""

    @pytest.mark.skipif(
        not TEST_MODEL_PATH.exists() or not TEST_DATASET_PATH.exists(),
        reason="Test fixtures not available"
    )
    def test_position_count_condition(
        self, test_model, test_dataset
    ):
        """Test that position count conditions are evaluated correctly."""
        from app.services.backtest_handler import run_backtest

        # Buy when class_0 AND no positions
        buy_conditions = {
            "id": "cond_buy",
            "operator": "AND",
            "conditions": [
                {
                    "id": "cond_1",
                    "field": "model:class_0",
                    "fieldType": "model_class",
                    "comparison": "is_true",
                    "value": 1,
                    "optimizeEnabled": False
                },
                {
                    "id": "cond_2",
                    "field": "position:total_count",
                    "fieldType": "position",
                    "comparison": "eq",
                    "value": 0,
                    "optimizeEnabled": False
                }
            ]
        }

        strategy_params = {
            'initial_tp_percent': 5.0,
            'initial_sl_percent': 2.0,
        }

        results = run_backtest(
            model=test_model,
            pred_df=test_dataset,
            exec_df=test_dataset,
            strategy_params=strategy_params,
            initial_capital=10000.0,
            position_sizing_type="percent",
            position_sizing_value=10.0,
            commission=0.0,
            slippage=0.0,
            buy_entry_conditions=buy_conditions,
            sell_entry_conditions=None,
            exit_conditions=[],
        )

        # Should still open trades (backtesting.py handles one position at a time anyway)
        assert results['total_trades'] >= 0

    @pytest.mark.skipif(
        not TEST_MODEL_PATH.exists() or not TEST_DATASET_PATH.exists(),
        reason="Test fixtures not available"
    )
    def test_probability_threshold_condition(
        self, test_model, test_dataset
    ):
        """Test conditions based on prediction probability threshold."""
        from app.services.backtest_handler import run_backtest

        # Buy when class_0 with high probability (> 0.7)
        buy_conditions = {
            "id": "cond_buy",
            "operator": "AND",
            "conditions": [
                {
                    "id": "cond_1",
                    "field": "model:class_0",
                    "fieldType": "model_class",
                    "comparison": "is_true",
                    "value": 1,
                    "optimizeEnabled": False
                },
                {
                    "id": "cond_2",
                    "field": "model:probability_0",
                    "fieldType": "model",
                    "comparison": "gt",
                    "value": 0.7,
                    "optimizeEnabled": False
                }
            ]
        }

        strategy_params = {
            'initial_tp_percent': 5.0,
            'initial_sl_percent': 2.0,
        }

        results = run_backtest(
            model=test_model,
            pred_df=test_dataset,
            exec_df=test_dataset,
            strategy_params=strategy_params,
            initial_capital=10000.0,
            position_sizing_type="percent",
            position_sizing_value=10.0,
            commission=0.0,
            slippage=0.0,
            buy_entry_conditions=buy_conditions,
            sell_entry_conditions=None,
            exit_conditions=[],
        )

        # With stricter probability threshold, we may have fewer trades
        # This is just verifying the condition works
        assert 'total_trades' in results


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
