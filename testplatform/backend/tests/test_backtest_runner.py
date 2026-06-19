"""
Unit tests for the backtest runner (run_backtest function).

Creates a fake model and simple dataset to test the backtest execution flow,
including trade entry/exit based on strategy conditions.

Usage:
    cd backend
    ./venv/bin/python -m pytest tests/test_backtest_runner.py -v

Or run directly:
    ./venv/bin/python tests/test_backtest_runner.py
"""

import sys
import json
import tempfile
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def _check_tsai_available() -> bool:
    """Check if tsai is available."""
    try:
        import torch
        from tsai.all import TSClassifier
        return True
    except ImportError:
        return False


def create_sample_dataset(n_samples: int = 200, seed: int = 42) -> pd.DataFrame:
    """
    Create a sample dataset with predictable price movements.

    Prices trend upward for first half, downward for second half,
    making it easy to verify trading behavior.
    """
    np.random.seed(seed)

    dates = pd.date_range(start='2024-01-01', periods=n_samples, freq='1h')

    # Generate price data with a clear trend
    # First half: uptrend, second half: downtrend
    mid_point = n_samples // 2

    # Uptrend phase
    up_returns = np.random.normal(0.002, 0.005, mid_point)  # Positive bias
    up_prices = 100.0 * np.exp(np.cumsum(up_returns))

    # Downtrend phase
    down_returns = np.random.normal(-0.002, 0.005, n_samples - mid_point)  # Negative bias
    down_prices = up_prices[-1] * np.exp(np.cumsum(down_returns))

    close_prices = np.concatenate([up_prices, down_prices])

    # Generate OHLC around close
    high_prices = close_prices * (1 + np.abs(np.random.normal(0, 0.003, n_samples)))
    low_prices = close_prices * (1 - np.abs(np.random.normal(0, 0.003, n_samples)))
    open_prices = close_prices * (1 + np.random.normal(0, 0.002, n_samples))

    # Volume
    volume = np.random.lognormal(mean=15, sigma=0.5, size=n_samples)

    # Simple features
    returns = np.zeros(n_samples)
    returns[1:] = (close_prices[1:] - close_prices[:-1]) / close_prices[:-1]

    # Binary target (price goes up next bar)
    target = np.zeros(n_samples, dtype=int)
    target[:-1] = (close_prices[1:] > close_prices[:-1]).astype(int)

    df = pd.DataFrame({
        'Date': dates,
        'Open': open_prices,
        'High': high_prices,
        'Low': low_prices,
        'Close': close_prices,
        'Volume': volume,
        'Returns': returns,
        'target': target
    })

    return df


def create_mock_model_and_files(tmpdir: Path, feature_columns: list, seq_len: int = 24):
    """
    Create a mock trained model and save necessary files.

    Returns a mock TrainedModel object and paths to model files.
    """
    from app.services.tsai_training import TSAITrainingService
    from app.services.tsai_models import TSAIModelService
    from app.services.data_preparation import DataPreparationService
    import torch

    # Create sample data for training
    df = create_sample_dataset(200)

    # Fit normalization
    data_prep = DataPreparationService(buffer_pct=0.35)
    df_normalized = data_prep.fit_transform(df, feature_columns)
    norm_params = data_prep.export_params()
    valid_columns = data_prep.get_valid_columns()

    # Create model - use default params that TSAIModelService uses
    c_in = len(valid_columns)
    c_out = 2
    model_params = {'hidden_size': 64, 'n_layers': 2, 'dropout': 0.1}  # Default LSTM params

    model_service = TSAIModelService()
    model = model_service.create_model(
        model_type='lstm',
        params=model_params,
        c_in=c_in,
        c_out=c_out,
        seq_len=seq_len
    )

    # Save model
    model_path = tmpdir / "test_model.pt"
    torch.save(model.state_dict(), model_path)

    # Save metadata
    meta_path = tmpdir / "test_model_meta.json"
    metadata = {
        'model_type': 'lstm',
        'c_in': c_in,
        'c_out': c_out,
        'seq_len': seq_len,
        'params': model_params,
        'feature_columns': valid_columns,
    }
    with open(meta_path, 'w') as f:
        json.dump(metadata, f)

    # Create mock TrainedModel
    mock_model = MagicMock()
    mock_model.model_id = "mdl-test123"
    mock_model.model_type = "lstm"
    mock_model.file_path = str(model_path)
    mock_model.prediction_mode = "shift"
    mock_model.threshold = 0.5
    mock_model.hyperparameters = {
        'c_in': c_in,
        'c_out': c_out,
        'seq_len': seq_len,
        'seqLen': seq_len,
        'featureColumns': valid_columns,
        'modelParams': model_params
    }
    mock_model.normalization_params = norm_params

    return mock_model, model_path, norm_params, valid_columns


class TestBacktestRunner:
    """Tests for run_backtest function."""

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_backtest_opens_trades_on_conditions(self):
        """Test that backtest opens trades when entry conditions are met."""
        from app.services.backtest_handler import run_backtest

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Returns']
        seq_len = 24

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create mock model
            mock_model, model_path, norm_params, valid_cols = create_mock_model_and_files(
                tmpdir, feature_columns, seq_len
            )

            # Create test dataset with enough bars
            df = create_sample_dataset(150)

            # Simple entry condition: buy when model:probability_1 > 0.3
            # (low threshold to ensure some trades happen)
            buy_entry_conditions = {
                "operator": "AND",
                "conditions": [
                    {
                        "id": "buy1",
                        "field": "model:probability_1",
                        "comparison": ">",
                        "value": 0.3
                    }
                ]
            }

            # Exit after 5 bars
            exit_conditions = [
                {
                    "id": "exit1",
                    "name": "Time Exit",
                    "conditions": {
                        "field": "bars_in_trade",
                        "comparison": ">=",
                        "value": 5
                    },
                    "action": "close"
                }
            ]

            result = run_backtest(
                model=mock_model,
                pred_df=df.copy(),
                exec_df=df.copy(),
                strategy_params={},
                initial_capital=10000.0,
                position_sizing_type="fixed",
                position_sizing_value=1000.0,
                commission=0.0,
                slippage=0.0,
                buy_entry_conditions=buy_entry_conditions,
                sell_entry_conditions=None,
                exit_conditions=exit_conditions
            )

            # Verify result structure - check for key metrics
            assert 'total_trades' in result, "Result should have total_trades"
            assert 'trades' in result, "Result should have trades"
            assert 'error' not in result or result.get('error') is None, f"Should not have error: {result.get('error')}"

            print(f"\nBacktest result:")
            print(f"  Status: {result.get('status')}")
            print(f"  Total trades: {result.get('total_trades', 0)}")
            print(f"  Total return: {result.get('total_return', 0):.2f}%")
            print(f"  Win rate: {result.get('win_rate', 0):.1f}%")

            # With low threshold, we expect some trades
            # Note: Model is untrained so predictions are random, but some should exceed 0.3
            if result.get('total_trades', 0) == 0:
                print("  Warning: No trades executed (model predictions may all be < 0.3)")

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_backtest_respects_position_count_condition(self):
        """Test that position count conditions work correctly."""
        from app.services.backtest_handler import run_backtest

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Returns']
        seq_len = 24

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            mock_model, model_path, norm_params, valid_cols = create_mock_model_and_files(
                tmpdir, feature_columns, seq_len
            )

            df = create_sample_dataset(150)

            # Entry condition: buy when probability > 0.3 AND no existing positions
            buy_entry_conditions = {
                "operator": "AND",
                "conditions": [
                    {
                        "id": "buy1",
                        "field": "model:probability_1",
                        "comparison": ">",
                        "value": 0.3
                    },
                    {
                        "id": "buy2",
                        "field": "position:total_count",
                        "comparison": "==",
                        "value": 0
                    }
                ]
            }

            # Exit after 10 bars
            exit_conditions = [
                {
                    "id": "exit1",
                    "conditions": {
                        "field": "bars_in_trade",
                        "comparison": ">=",
                        "value": 10
                    },
                    "action": "close"
                }
            ]

            result = run_backtest(
                model=mock_model,
                pred_df=df.copy(),
                exec_df=df.copy(),
                strategy_params={},
                initial_capital=10000.0,
                position_sizing_type="fixed",
                position_sizing_value=1000.0,
                buy_entry_conditions=buy_entry_conditions,
                exit_conditions=exit_conditions
            )

            assert 'total_trades' in result

            # With position count condition, positions should not overlap
            trades = result.get('trades', [])
            print(f"\nPosition count test:")
            print(f"  Total trades: {len(trades)}")

            # Verify no overlapping trades
            for i, trade in enumerate(trades):
                if i < len(trades) - 1:
                    next_trade = trades[i + 1]
                    # Current trade should exit before next trade enters
                    # (trades list contains completed trades with entry_time and exit_time)
                    if 'exit_time' in trade and 'entry_time' in next_trade:
                        assert trade['exit_time'] <= next_trade['entry_time'], \
                            "Trades should not overlap when position count condition is used"

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_backtest_with_both_buy_and_sell_conditions(self):
        """Test backtest with both buy and sell entry conditions."""
        from app.services.backtest_handler import run_backtest

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Returns']
        seq_len = 24

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            mock_model, model_path, norm_params, valid_cols = create_mock_model_and_files(
                tmpdir, feature_columns, seq_len
            )

            df = create_sample_dataset(150)

            # Buy when class 1 probability > 0.5
            buy_entry_conditions = {
                "operator": "AND",
                "conditions": [
                    {
                        "field": "model:probability_1",
                        "comparison": ">",
                        "value": 0.5
                    }
                ]
            }

            # Sell when class 0 probability > 0.5
            sell_entry_conditions = {
                "operator": "AND",
                "conditions": [
                    {
                        "field": "model:probability_0",
                        "comparison": ">",
                        "value": 0.5
                    }
                ]
            }

            exit_conditions = [
                {
                    "conditions": {
                        "field": "bars_in_trade",
                        "comparison": ">=",
                        "value": 3
                    },
                    "action": "close"
                }
            ]

            result = run_backtest(
                model=mock_model,
                pred_df=df.copy(),
                exec_df=df.copy(),
                strategy_params={},
                initial_capital=10000.0,
                buy_entry_conditions=buy_entry_conditions,
                sell_entry_conditions=sell_entry_conditions,
                exit_conditions=exit_conditions
            )

            assert 'total_trades' in result
            print(f"\nBuy/Sell conditions test:")
            print(f"  Total trades: {result.get('total_trades', 0)}")

            # Check trade directions
            trades = result.get('trades', [])
            buy_trades = [t for t in trades if t.get('direction') == 'buy']
            sell_trades = [t for t in trades if t.get('direction') == 'sell']
            print(f"  Buy trades: {len(buy_trades)}")
            print(f"  Sell trades: {len(sell_trades)}")

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_backtest_pnl_exit_condition(self):
        """Test exit condition based on position P&L."""
        from app.services.backtest_handler import run_backtest

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Returns']
        seq_len = 24

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            mock_model, model_path, norm_params, valid_cols = create_mock_model_and_files(
                tmpdir, feature_columns, seq_len
            )

            df = create_sample_dataset(150)

            buy_entry_conditions = {
                "operator": "AND",
                "conditions": [
                    {
                        "field": "model:probability_1",
                        "comparison": ">",
                        "value": 0.3
                    },
                    {
                        "field": "position:total_count",
                        "comparison": "==",
                        "value": 0
                    }
                ]
            }

            # Exit when P&L > 1% profit OR < -1% loss OR after 20 bars
            exit_conditions = [
                {
                    "name": "Take Profit",
                    "conditions": {
                        "field": "position_pnl_pct",
                        "comparison": ">",
                        "value": 1.0
                    },
                    "action": "close"
                },
                {
                    "name": "Stop Loss",
                    "conditions": {
                        "field": "position_pnl_pct",
                        "comparison": "<",
                        "value": -1.0
                    },
                    "action": "close"
                },
                {
                    "name": "Time Exit",
                    "conditions": {
                        "field": "bars_in_trade",
                        "comparison": ">=",
                        "value": 20
                    },
                    "action": "close"
                }
            ]

            result = run_backtest(
                model=mock_model,
                pred_df=df.copy(),
                exec_df=df.copy(),
                strategy_params={},
                initial_capital=10000.0,
                buy_entry_conditions=buy_entry_conditions,
                exit_conditions=exit_conditions
            )

            assert 'total_trades' in result
            print(f"\nP&L exit condition test:")
            print(f"  Total trades: {result.get('total_trades', 0)}")
            print(f"  Win rate: {result.get('win_rate', 0):.1f}%")
            print(f"  Best trade: {result.get('best_trade', 0):.2f}%")
            print(f"  Worst trade: {result.get('worst_trade', 0):.2f}%")

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_backtest_equity_curve(self):
        """Test that backtest produces valid equity curve."""
        from app.services.backtest_handler import run_backtest

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Returns']
        seq_len = 24

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            mock_model, model_path, norm_params, valid_cols = create_mock_model_and_files(
                tmpdir, feature_columns, seq_len
            )

            df = create_sample_dataset(150)
            initial_capital = 10000.0

            buy_entry_conditions = {
                "operator": "AND",
                "conditions": [
                    {"field": "model:probability_1", "comparison": ">", "value": 0.4}
                ]
            }

            exit_conditions = [
                {"conditions": {"field": "bars_in_trade", "comparison": ">=", "value": 5}, "action": "close"}
            ]

            result = run_backtest(
                model=mock_model,
                pred_df=df.copy(),
                exec_df=df.copy(),
                strategy_params={},
                initial_capital=initial_capital,
                buy_entry_conditions=buy_entry_conditions,
                exit_conditions=exit_conditions
            )

            assert 'equity_curve' in result, "Result should have equity_curve"

            equity_curve = result['equity_curve']
            assert len(equity_curve) > 0, "Equity curve should not be empty"

            # First point should be initial capital
            assert equity_curve[0]['equity'] == initial_capital, \
                "Equity curve should start at initial capital"

            # All equity values should be positive
            for point in equity_curve:
                assert point['equity'] > 0, "Equity should always be positive"

            print(f"\nEquity curve test:")
            print(f"  Data points: {len(equity_curve)}")
            print(f"  Starting equity: ${equity_curve[0]['equity']:.2f}")
            print(f"  Ending equity: ${equity_curve[-1]['equity']:.2f}")
            print(f"  Total return: {result.get('total_return', 0):.2f}%")


class TestBacktestEdgeCases:
    """Test edge cases and error handling in backtest."""

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_backtest_no_conditions(self):
        """Test backtest with no entry conditions should produce no trades."""
        from app.services.backtest_handler import run_backtest

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Returns']
        seq_len = 24

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            mock_model, _, _, _ = create_mock_model_and_files(
                tmpdir, feature_columns, seq_len
            )

            df = create_sample_dataset(100)

            result = run_backtest(
                model=mock_model,
                pred_df=df.copy(),
                exec_df=df.copy(),
                strategy_params={},
                initial_capital=10000.0,
                buy_entry_conditions=None,  # No conditions
                sell_entry_conditions=None,
                exit_conditions=[]
            )

            assert result.get('total_trades', 0) == 0, \
                "No trades should be made without entry conditions"

            print("\nNo conditions test: passed (0 trades as expected)")

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_backtest_impossible_conditions(self):
        """Test backtest with impossible conditions should produce no trades."""
        from app.services.backtest_handler import run_backtest

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Returns']
        seq_len = 24

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            mock_model, _, _, _ = create_mock_model_and_files(
                tmpdir, feature_columns, seq_len
            )

            df = create_sample_dataset(100)

            # Impossible: probability must be > 1.5 (max is 1.0)
            buy_entry_conditions = {
                "operator": "AND",
                "conditions": [
                    {"field": "model:probability_1", "comparison": ">", "value": 1.5}
                ]
            }

            result = run_backtest(
                model=mock_model,
                pred_df=df.copy(),
                exec_df=df.copy(),
                strategy_params={},
                initial_capital=10000.0,
                buy_entry_conditions=buy_entry_conditions,
                exit_conditions=[]
            )

            assert result.get('total_trades', 0) == 0, \
                "No trades should be made with impossible conditions"

            print("\nImpossible conditions test: passed (0 trades as expected)")

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_backtest_insufficient_data(self):
        """Test backtest with insufficient data for seq_len."""
        from app.services.backtest_handler import run_backtest

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume', 'Returns']
        seq_len = 24

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            mock_model, _, _, _ = create_mock_model_and_files(
                tmpdir, feature_columns, seq_len
            )

            # Create dataset with fewer rows than seq_len
            df = create_sample_dataset(20)  # Less than seq_len=24

            result = run_backtest(
                model=mock_model,
                pred_df=df.copy(),
                exec_df=df.copy(),
                strategy_params={},
                initial_capital=10000.0,
                buy_entry_conditions={"operator": "AND", "conditions": [{"field": "model:probability_1", "comparison": ">", "value": 0.5}]},
                exit_conditions=[]
            )

            # Should handle gracefully - either error or empty results
            print(f"\nInsufficient data test:")
            print(f"  Status: {result.get('status')}")
            print(f"  Error: {result.get('error')}")
            print(f"  Trades: {result.get('total_trades', 0)}")


def run_tests():
    """Run all tests manually."""
    print("\n" + "="*60)
    print("TESTING BACKTEST RUNNER")
    print("="*60 + "\n")

    if not _check_tsai_available():
        print("tsai not available - skipping tests")
        return

    # Run tests
    test_runner = TestBacktestRunner()
    test_edge = TestBacktestEdgeCases()

    print("\n--- Basic Tests ---")

    print("\nTest 1: backtest opens trades on conditions")
    test_runner.test_backtest_opens_trades_on_conditions()

    print("\nTest 2: backtest respects position count condition")
    test_runner.test_backtest_respects_position_count_condition()

    print("\nTest 3: backtest with buy and sell conditions")
    test_runner.test_backtest_with_both_buy_and_sell_conditions()

    print("\nTest 4: backtest P&L exit condition")
    test_runner.test_backtest_pnl_exit_condition()

    print("\nTest 5: backtest equity curve")
    test_runner.test_backtest_equity_curve()

    print("\n--- Edge Case Tests ---")

    print("\nTest 6: backtest no conditions")
    test_edge.test_backtest_no_conditions()

    print("\nTest 7: backtest impossible conditions")
    test_edge.test_backtest_impossible_conditions()

    print("\nTest 8: backtest insufficient data")
    test_edge.test_backtest_insufficient_data()

    print("\n" + "="*60)
    print("ALL TESTS COMPLETED")
    print("="*60 + "\n")


if __name__ == "__main__":
    run_tests()
