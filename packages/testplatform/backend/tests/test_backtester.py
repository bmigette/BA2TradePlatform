"""
Comprehensive unit tests for the backtester system.

Tests Strategy API, Backtest API, model serialization, and integration workflows.
"""

import pytest
import os
import sys
from datetime import datetime, timedelta

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Test database configuration - MUST be set before any app imports
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_backtester.db")

# Remove existing test db and set environment variable BEFORE any app imports
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ['DATABASE_URL'] = f"sqlite:///{TEST_DB_PATH}"


@pytest.fixture(scope="module")
def test_db():
    """Set up test database for the module."""
    # Environment variable already set at module level before any imports
    # Import database components
    from app.models.database import engine, Base, SessionLocal
    # Import all models to register them with Base before create_all
    import app.models  # noqa: F401

    # Create tables (includes new buy/sell columns)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    yield db

    # Cleanup - close session and dispose engine to release file locks (Windows)
    db.close()
    engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass  # Windows may still hold lock briefly


@pytest.fixture(scope="module")
def test_client(test_db):
    """Create FastAPI test client."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def sample_dataset(test_db):
    """Create a sample dataset for testing."""
    from app.models import Dataset

    # Use the actual test data file path
    test_data_path = os.path.join(os.path.dirname(__file__), "data", "AAPL_1h_test.csv")

    dataset = Dataset(
        name="Test Dataset AAPL",
        ticker="AAPL",
        timeframe="1h",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 6, 30),
        rows_count=4320,
        status="ready",
        file_path=test_data_path
    )
    test_db.add(dataset)
    test_db.commit()
    test_db.refresh(dataset)
    return dataset


@pytest.fixture(scope="module")
def sample_model(test_db, sample_dataset):
    """Create a sample trained model for testing."""
    from app.models import TrainedModel
    import uuid

    model = TrainedModel(
        model_id=f"mdl-{uuid.uuid4().hex[:8]}",
        name="Test LSTM Model",
        model_type="lstm",
        dataset_id=sample_dataset.id,
        job_id="job-test-001",
        status="trained",
        hyperparameters={"hidden_size": 64, "n_layers": 2},
        performance_metrics={"f1_score": 0.72, "accuracy": 0.78},
        prediction_targets=[
            {"type": "price_up_10pct", "category": "binary_classification"}
        ],
        prediction_horizon=5,
        prediction_mode="shift",
        generations=50,
        best_generation=35,
        fitness=0.72
    )
    test_db.add(model)
    test_db.commit()
    test_db.refresh(model)
    return model


@pytest.fixture(scope="module")
def sample_strategy(test_db):
    """Create a sample strategy for testing."""
    from app.models import Strategy

    strategy = Strategy(
        name="Test Strategy",
        description="A test trading strategy",
        required_fields=["price_up_10pct"],
        entry_conditions={
            "operator": "AND",
            "conditions": [
                {
                    "id": "cond1",
                    "field": "price_up_10pct",
                    "field_type": "model_probability",
                    "comparison": ">",
                    "value": 0.7
                }
            ]
        },
        exit_conditions=[
            {
                "id": "exit1",
                "name": "Time Exit",
                "conditions": {
                    "field": "bars_in_trade",
                    "field_type": "position",
                    "comparison": ">",
                    "value": 50
                },
                "action": "close"
            }
        ],
        initial_tp_percent=5.0,
        initial_sl_percent=2.0
    )
    test_db.add(strategy)
    test_db.commit()
    test_db.refresh(strategy)
    return strategy


# =============================================================================
# Test extract_required_fields helper function
# =============================================================================

class TestExtractRequiredFields:
    """Test the extract_required_fields helper function."""

    def test_extract_from_entry_conditions(self):
        """Should extract model fields from entry conditions."""
        from app.api.strategies import extract_required_fields

        entry = {
            "operator": "AND",
            "conditions": [
                {"field": "price_up_10pct", "field_type": "model_probability", "comparison": ">", "value": 0.7},
                {"field": "volatility_high", "field_type": "model_class", "comparison": "==", "value": 1}
            ]
        }
        exit_conds = []

        result = extract_required_fields(entry, exit_conds)
        assert sorted(result) == ["price_up_10pct", "volatility_high"]

    def test_extract_from_exit_conditions(self):
        """Should extract model fields from exit conditions."""
        from app.api.strategies import extract_required_fields

        entry = {}
        exit_conds = [
            {
                "id": "exit1",
                "conditions": {
                    "field": "bearish_signal",
                    "field_type": "model_probability",
                    "comparison": ">",
                    "value": 0.8
                },
                "action": "close"
            }
        ]

        result = extract_required_fields(entry, exit_conds)
        assert result == ["bearish_signal"]

    def test_extract_ignores_position_fields(self):
        """Should ignore position/time fields, only extract model fields."""
        from app.api.strategies import extract_required_fields

        entry = {
            "conditions": [
                {"field": "bars_in_trade", "field_type": "position", "comparison": ">", "value": 10},
                {"field": "hour", "field_type": "time", "comparison": ">=", "value": 9}
            ]
        }
        exit_conds = []

        result = extract_required_fields(entry, exit_conds)
        assert result == []

    def test_extract_nested_conditions(self):
        """Should extract fields from nested condition trees."""
        from app.api.strategies import extract_required_fields

        entry = {
            "operator": "OR",
            "conditions": [
                {
                    "operator": "AND",
                    "conditions": [
                        {"field": "bull_signal", "field_type": "model_probability", "comparison": ">", "value": 0.7},
                        {"field": "bear_signal", "field_type": "model_probability", "comparison": "<", "value": 0.3}
                    ]
                },
                {"field": "momentum", "field_type": "model_class", "comparison": "==", "value": 1}
            ]
        }
        exit_conds = []

        result = extract_required_fields(entry, exit_conds)
        assert sorted(result) == ["bear_signal", "bull_signal", "momentum"]

    def test_empty_conditions(self):
        """Should handle empty conditions gracefully."""
        from app.api.strategies import extract_required_fields

        result = extract_required_fields({}, [])
        assert result == []

        result = extract_required_fields(None, None)
        assert result == []


# =============================================================================
# Test Strategy Model Serialization
# =============================================================================

class TestStrategyModelSerialization:
    """Test Strategy model to_dict() serialization."""

    def test_strategy_to_dict_basic(self, sample_strategy):
        """Test basic serialization of Strategy model."""
        result = sample_strategy.to_dict()

        assert result["id"] == sample_strategy.id
        assert result["name"] == "Test Strategy"
        assert result["description"] == "A test trading strategy"
        assert result["requiredFields"] == ["price_up_10pct"]

    def test_strategy_to_dict_conditions(self, sample_strategy):
        """Test that conditions are properly serialized."""
        result = sample_strategy.to_dict()

        assert "entryConditions" in result
        assert result["entryConditions"]["operator"] == "AND"
        assert len(result["entryConditions"]["conditions"]) == 1

        assert "exitConditions" in result
        assert len(result["exitConditions"]) == 1
        assert result["exitConditions"][0]["action"] == "close"

    def test_strategy_to_dict_tp_sl(self, sample_strategy):
        """Test TP/SL fields are properly serialized."""
        result = sample_strategy.to_dict()

        assert result["initialTpPercent"] == 5.0
        assert result["initialSlPercent"] == 2.0
        assert result["initialTpOptimize"] is False
        assert result["initialSlOptimize"] is False

    def test_strategy_to_dict_timestamps(self, sample_strategy):
        """Test timestamp serialization."""
        result = sample_strategy.to_dict()

        assert "createdAt" in result
        assert result["createdAt"] is not None
        # updatedAt might be None if not updated
        assert "updatedAt" in result

    def test_strategy_to_dict_empty_conditions(self, test_db):
        """Test serialization with empty conditions."""
        from app.models import Strategy

        strategy = Strategy(
            name="Empty Strategy",
            entry_conditions={},
            exit_conditions=[]
        )
        test_db.add(strategy)
        test_db.commit()
        test_db.refresh(strategy)

        result = strategy.to_dict()
        assert result["entryConditions"] == {}
        assert result["exitConditions"] == []
        assert result["requiredFields"] == []

        # Cleanup
        test_db.delete(strategy)
        test_db.commit()


# =============================================================================
# Test Backtest Model Serialization
# =============================================================================

class TestBacktestModelSerialization:
    """Test Backtest model to_dict() serialization."""

    def test_backtest_to_dict_basic(self, test_db, sample_model, sample_dataset, sample_strategy):
        """Test basic serialization of Backtest model."""
        from app.models import Backtest

        backtest = Backtest(
            name="Test Backtest",
            model_id=sample_model.id,
            prediction_dataset_id=sample_dataset.id,
            execution_dataset_id=sample_dataset.id,
            strategy_id=sample_strategy.id,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 6, 30),
            initial_capital=10000.0,
            position_sizing_type="fixed",
            position_sizing_value=1000.0,
            commission=0.1,
            slippage=0.05,
            status="pending"
        )
        test_db.add(backtest)
        test_db.commit()
        test_db.refresh(backtest)

        result = backtest.to_dict()

        assert result["id"] == backtest.id
        assert result["name"] == "Test Backtest"
        assert result["modelId"] == sample_model.id
        assert result["strategyId"] == sample_strategy.id
        assert result["status"] == "pending"

        # Cleanup
        test_db.delete(backtest)
        test_db.commit()

    def test_backtest_to_dict_configuration(self, test_db, sample_model, sample_dataset):
        """Test configuration fields serialization."""
        from app.models import Backtest

        backtest = Backtest(
            name="Config Test",
            model_id=sample_model.id,
            prediction_dataset_id=sample_dataset.id,
            execution_dataset_id=sample_dataset.id,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 6, 30),
            initial_capital=50000.0,
            position_sizing_type="percent",
            position_sizing_value=5.0,
            commission=0.2,
            slippage=0.1,
            fitness_metric="sharpe_ratio",
            status="pending"
        )
        test_db.add(backtest)
        test_db.commit()
        test_db.refresh(backtest)

        result = backtest.to_dict()

        assert result["initialCapital"] == 50000.0
        assert result["positionSizingType"] == "percent"
        assert result["positionSizingValue"] == 5.0
        assert result["commission"] == 0.2
        assert result["slippage"] == 0.1
        assert result["fitnessMetric"] == "sharpe_ratio"

        # Cleanup
        test_db.delete(backtest)
        test_db.commit()

    def test_backtest_to_dict_with_results(self, test_db, sample_model, sample_dataset):
        """Test serialization of completed backtest with results."""
        from app.models import Backtest

        backtest = Backtest(
            name="Completed Backtest",
            model_id=sample_model.id,
            prediction_dataset_id=sample_dataset.id,
            execution_dataset_id=sample_dataset.id,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 6, 30),
            initial_capital=10000.0,
            status="completed",
            total_return=15.5,
            sharpe_ratio=1.2,
            max_drawdown=8.3,
            win_rate=62.5,
            profit_factor=1.8,
            total_trades=50,
            avg_trade_duration=12.5,
            best_trade=5.2,
            worst_trade=-3.1,
            completed_at=datetime.now()
        )
        test_db.add(backtest)
        test_db.commit()
        test_db.refresh(backtest)

        result = backtest.to_dict()

        assert result["status"] == "completed"
        assert result["totalReturn"] == 15.5
        assert result["sharpeRatio"] == 1.2
        assert result["maxDrawdown"] == 8.3
        assert result["winRate"] == 62.5
        assert result["profitFactor"] == 1.8
        assert result["totalTrades"] == 50
        assert result["avgTradeDuration"] == 12.5
        assert result["bestTrade"] == 5.2
        assert result["worstTrade"] == -3.1
        assert result["completedAt"] is not None

        # Cleanup
        test_db.delete(backtest)
        test_db.commit()

    def test_backtest_to_dict_dates(self, test_db, sample_model, sample_dataset):
        """Test date serialization (ISO format)."""
        from app.models import Backtest

        start = datetime(2024, 3, 15, 10, 30, 0)
        end = datetime(2024, 6, 15, 16, 0, 0)

        backtest = Backtest(
            name="Date Test",
            model_id=sample_model.id,
            prediction_dataset_id=sample_dataset.id,
            execution_dataset_id=sample_dataset.id,
            start_date=start,
            end_date=end,
            initial_capital=10000.0,
            status="pending"
        )
        test_db.add(backtest)
        test_db.commit()
        test_db.refresh(backtest)

        result = backtest.to_dict()

        assert "2024-03-15" in result["startDate"]
        assert "2024-06-15" in result["endDate"]

        # Cleanup
        test_db.delete(backtest)
        test_db.commit()


# =============================================================================
# Test Strategy API Endpoints
# =============================================================================

class TestStrategyAPIEndpoints:
    """Test Strategy API CRUD operations."""

    def test_list_strategies_empty(self, test_client):
        """Test listing strategies when empty."""
        response = test_client.get("/api/strategies")
        assert response.status_code == 200
        data = response.json()
        assert "strategies" in data
        assert "total" in data
        assert isinstance(data["strategies"], list)

    def test_create_strategy(self, test_client):
        """Test creating a new strategy."""
        strategy_data = {
            "name": "API Test Strategy",
            "description": "Created via API",
            "entry_conditions": {
                "operator": "AND",
                "conditions": [
                    {
                        "id": "cond1",
                        "field": "bull_signal",
                        "field_type": "model_probability",
                        "comparison": ">",
                        "value": 0.75
                    }
                ]
            },
            "exit_conditions": [
                {
                    "id": "exit1",
                    "name": "Stop Loss Time",
                    "conditions": {
                        "field": "bars_in_trade",
                        "field_type": "position",
                        "comparison": ">",
                        "value": 100
                    },
                    "action": "close"
                }
            ],
            "initial_tp_percent": 10.0,
            "initial_sl_percent": 5.0
        }

        response = test_client.post("/api/strategies", json=strategy_data)
        assert response.status_code == 200

        data = response.json()
        assert data["name"] == "API Test Strategy"
        assert data["description"] == "Created via API"
        assert data["initialTpPercent"] == 10.0
        assert data["initialSlPercent"] == 5.0
        assert "bull_signal" in data["requiredFields"]

        # Store ID for later tests
        TestStrategyAPIEndpoints.created_strategy_id = data["id"]

    def test_get_strategy(self, test_client):
        """Test getting a strategy by ID."""
        strategy_id = TestStrategyAPIEndpoints.created_strategy_id
        response = test_client.get(f"/api/strategies/{strategy_id}")
        assert response.status_code == 200

        data = response.json()
        assert data["id"] == strategy_id
        assert data["name"] == "API Test Strategy"

    def test_get_strategy_not_found(self, test_client):
        """Test getting a non-existent strategy."""
        response = test_client.get("/api/strategies/99999")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_update_strategy(self, test_client):
        """Test updating a strategy."""
        strategy_id = TestStrategyAPIEndpoints.created_strategy_id
        update_data = {
            "name": "Updated Strategy Name",
            "initial_tp_percent": 15.0
        }

        response = test_client.put(f"/api/strategies/{strategy_id}", json=update_data)
        assert response.status_code == 200

        data = response.json()
        assert data["name"] == "Updated Strategy Name"
        assert data["initialTpPercent"] == 15.0
        # Original values should remain
        assert data["initialSlPercent"] == 5.0

    def test_update_strategy_conditions(self, test_client):
        """Test updating strategy conditions recalculates required_fields."""
        strategy_id = TestStrategyAPIEndpoints.created_strategy_id
        update_data = {
            "entry_conditions": {
                "operator": "AND",
                "conditions": [
                    {
                        "id": "cond1",
                        "field": "new_signal",
                        "field_type": "model_probability",
                        "comparison": ">",
                        "value": 0.8
                    },
                    {
                        "id": "cond2",
                        "field": "trend_strength",
                        "field_type": "model_class",
                        "comparison": "==",
                        "value": 1
                    }
                ]
            }
        }

        response = test_client.put(f"/api/strategies/{strategy_id}", json=update_data)
        assert response.status_code == 200

        data = response.json()
        assert sorted(data["requiredFields"]) == ["new_signal", "trend_strength"]

    def test_list_strategies_with_search(self, test_client):
        """Test listing strategies with search filter."""
        response = test_client.get("/api/strategies?search=Updated")
        assert response.status_code == 200

        data = response.json()
        assert data["total"] >= 1
        assert any("Updated" in s["name"] for s in data["strategies"])

    def test_compatible_strategies_model_not_found(self, test_client):
        """Test compatible strategies endpoint with non-existent model."""
        response = test_client.get("/api/strategies/compatible/99999")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_compatible_strategies(self, test_client, sample_model):
        """Test getting strategies compatible with a model."""
        response = test_client.get(f"/api/strategies/compatible/{sample_model.id}")
        assert response.status_code == 200

        data = response.json()
        assert "strategies" in data
        assert "total" in data
        assert "modelFields" in data

    def test_delete_strategy(self, test_client):
        """Test deleting a strategy."""
        strategy_id = TestStrategyAPIEndpoints.created_strategy_id
        response = test_client.delete(f"/api/strategies/{strategy_id}")
        assert response.status_code == 200
        assert "deleted" in response.json()["message"].lower()

        # Verify deleted
        response = test_client.get(f"/api/strategies/{strategy_id}")
        assert response.status_code == 404

    def test_delete_strategy_not_found(self, test_client):
        """Test deleting a non-existent strategy."""
        response = test_client.delete("/api/strategies/99999")
        assert response.status_code == 404


# =============================================================================
# Test Backtest API Endpoints
# =============================================================================

class TestBacktestAPIEndpoints:
    """Test Backtest API CRUD operations."""

    def test_list_backtests_empty(self, test_client):
        """Test listing backtests when empty."""
        response = test_client.get("/api/backtests")
        assert response.status_code == 200
        data = response.json()
        assert "backtests" in data
        assert "total" in data

    def test_create_backtest(self, test_client, sample_model, sample_dataset, sample_strategy):
        """Test creating a new backtest."""
        backtest_data = {
            "name": "API Test Backtest",
            "model_id": sample_model.model_id,
            "prediction_dataset_id": sample_dataset.id,
            "execution_dataset_id": sample_dataset.id,
            "strategy_id": sample_strategy.id,
            "start_date": "2024-01-01T00:00:00",
            "end_date": "2024-06-30T00:00:00",
            "initial_capital": 25000.0,
            "position_sizing_type": "percent",
            "position_sizing_value": 10.0,
            "commission": 0.15,
            "slippage": 0.08
        }

        response = test_client.post("/api/backtests", json=backtest_data)
        assert response.status_code == 200

        data = response.json()
        assert data["name"] == "API Test Backtest"
        assert data["modelId"] == sample_model.id
        assert data["strategyId"] == sample_strategy.id
        assert data["initialCapital"] == 25000.0
        assert data["status"] == "pending"

        TestBacktestAPIEndpoints.created_backtest_id = data["id"]

    def test_create_backtest_model_not_found(self, test_client, sample_dataset):
        """Test creating backtest with non-existent model."""
        backtest_data = {
            "name": "Invalid Backtest",
            "model_id": "mdl-nonexistent",
            "prediction_dataset_id": sample_dataset.id,
            "execution_dataset_id": sample_dataset.id,
            "start_date": "2024-01-01T00:00:00",
            "end_date": "2024-06-30T00:00:00"
        }

        response = test_client.post("/api/backtests", json=backtest_data)
        assert response.status_code == 404
        assert "model" in response.json()["detail"].lower()

    def test_create_backtest_dataset_not_found(self, test_client, sample_model):
        """Test creating backtest with non-existent dataset."""
        backtest_data = {
            "name": "Invalid Backtest",
            "model_id": sample_model.model_id,
            "prediction_dataset_id": 99999,
            "execution_dataset_id": 99999,
            "start_date": "2024-01-01T00:00:00",
            "end_date": "2024-06-30T00:00:00"
        }

        response = test_client.post("/api/backtests", json=backtest_data)
        assert response.status_code == 404
        assert "dataset" in response.json()["detail"].lower()

    def test_create_backtest_strategy_not_found(self, test_client, sample_model, sample_dataset):
        """Test creating backtest with non-existent strategy."""
        backtest_data = {
            "name": "Invalid Backtest",
            "model_id": sample_model.model_id,
            "prediction_dataset_id": sample_dataset.id,
            "execution_dataset_id": sample_dataset.id,
            "strategy_id": 99999,
            "start_date": "2024-01-01T00:00:00",
            "end_date": "2024-06-30T00:00:00"
        }

        response = test_client.post("/api/backtests", json=backtest_data)
        assert response.status_code == 404
        assert "strategy" in response.json()["detail"].lower()

    def test_create_backtest_invalid_date(self, test_client, sample_model, sample_dataset):
        """Test creating backtest with invalid date format."""
        backtest_data = {
            "name": "Invalid Date Backtest",
            "model_id": sample_model.model_id,
            "prediction_dataset_id": sample_dataset.id,
            "execution_dataset_id": sample_dataset.id,
            "start_date": "not-a-date",
            "end_date": "2024-06-30T00:00:00"
        }

        response = test_client.post("/api/backtests", json=backtest_data)
        assert response.status_code == 400
        assert "date" in response.json()["detail"].lower()

    def test_get_backtest(self, test_client):
        """Test getting a backtest by ID."""
        backtest_id = TestBacktestAPIEndpoints.created_backtest_id
        response = test_client.get(f"/api/backtests/{backtest_id}")
        assert response.status_code == 200

        data = response.json()
        assert data["id"] == backtest_id
        assert data["name"] == "API Test Backtest"

    def test_get_backtest_not_found(self, test_client):
        """Test getting a non-existent backtest."""
        response = test_client.get("/api/backtests/99999")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_export_backtest_not_completed(self, test_client):
        """Test exporting a non-completed backtest should fail."""
        backtest_id = TestBacktestAPIEndpoints.created_backtest_id
        response = test_client.post(f"/api/backtests/{backtest_id}/export")
        assert response.status_code == 400
        assert "incomplete" in response.json()["detail"].lower()

    def test_export_backtest_not_found(self, test_client):
        """Test exporting a non-existent backtest."""
        response = test_client.post("/api/backtests/99999/export")
        assert response.status_code == 404

    def test_compare_backtests_insufficient(self, test_client):
        """Test comparing fewer than 2 backtests should fail."""
        response = test_client.post(
            "/api/backtests/compare",
            json=[TestBacktestAPIEndpoints.created_backtest_id]
        )
        assert response.status_code == 400
        assert "at least 2" in response.json()["detail"].lower()

    def test_compare_backtests_not_found(self, test_client):
        """Test comparing with non-existent backtest."""
        response = test_client.post(
            "/api/backtests/compare",
            json=[TestBacktestAPIEndpoints.created_backtest_id, 99999]
        )
        assert response.status_code == 404

    def test_compare_backtests(self, test_client, test_db, sample_model, sample_dataset):
        """Test comparing multiple backtests."""
        from app.models import Backtest

        # Create a second backtest for comparison
        backtest2 = Backtest(
            name="Comparison Backtest",
            model_id=sample_model.id,
            prediction_dataset_id=sample_dataset.id,
            execution_dataset_id=sample_dataset.id,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 6, 30),
            initial_capital=10000.0,
            status="completed",
            total_return=20.0,
            sharpe_ratio=1.5,
            max_drawdown=5.0,
            win_rate=70.0
        )
        test_db.add(backtest2)
        test_db.commit()
        test_db.refresh(backtest2)

        response = test_client.post(
            "/api/backtests/compare",
            json=[TestBacktestAPIEndpoints.created_backtest_id, backtest2.id]
        )
        assert response.status_code == 200

        data = response.json()
        assert "backtests" in data
        assert "comparison" in data
        assert len(data["backtests"]) == 2

        # Cleanup
        test_db.delete(backtest2)
        test_db.commit()

    def test_delete_backtest(self, test_client):
        """Test deleting a backtest."""
        backtest_id = TestBacktestAPIEndpoints.created_backtest_id
        response = test_client.delete(f"/api/backtests/{backtest_id}")
        assert response.status_code == 200
        assert "deleted" in response.json()["message"].lower()

        # Verify deleted
        response = test_client.get(f"/api/backtests/{backtest_id}")
        assert response.status_code == 404

    def test_delete_backtest_not_found(self, test_client):
        """Test deleting a non-existent backtest."""
        response = test_client.delete("/api/backtests/99999")
        assert response.status_code == 404


# =============================================================================
# Integration Test: Full Workflow
# =============================================================================

class TestBacktesterIntegration:
    """Integration tests for the complete backtester workflow."""

    def test_full_workflow_create_strategy_and_backtest(
        self, test_client, sample_model, sample_dataset
    ):
        """Test complete workflow: create strategy, then create backtest using it."""
        # Step 1: Create a strategy
        strategy_data = {
            "name": "Integration Test Strategy",
            "description": "Strategy for integration testing",
            "entry_conditions": {
                "operator": "AND",
                "conditions": [
                    {
                        "id": "entry1",
                        "field": "price_up_10pct",
                        "field_type": "model_probability",
                        "comparison": ">",
                        "value": 0.65
                    }
                ]
            },
            "exit_conditions": [
                {
                    "id": "exit1",
                    "name": "Max Duration",
                    "conditions": {
                        "field": "bars_in_trade",
                        "field_type": "position",
                        "comparison": ">",
                        "value": 48
                    },
                    "action": "close"
                },
                {
                    "id": "exit2",
                    "name": "Trailing Stop",
                    "conditions": {
                        "field": "position_pnl_pct",
                        "field_type": "position",
                        "comparison": ">",
                        "value": 3.0
                    },
                    "action": "adjust_sl",
                    "action_value": 0
                }
            ],
            "initial_tp_percent": 8.0,
            "initial_sl_percent": 3.0,
            "initial_tp_optimize": True,
            "initial_tp_min": 5.0,
            "initial_tp_max": 15.0,
            "initial_tp_step": 1.0
        }

        response = test_client.post("/api/strategies", json=strategy_data)
        assert response.status_code == 200
        strategy = response.json()
        assert strategy["name"] == "Integration Test Strategy"
        assert strategy["requiredFields"] == ["price_up_10pct"]

        # Step 2: Create a backtest using the strategy
        backtest_data = {
            "name": "Integration Test Backtest",
            "model_id": sample_model.model_id,
            "prediction_dataset_id": sample_dataset.id,
            "execution_dataset_id": sample_dataset.id,
            "strategy_id": strategy["id"],
            "strategy_params": {
                "initial_tp_percent": 10.0  # Overridden from strategy default
            },
            "start_date": "2024-02-01T00:00:00",
            "end_date": "2024-05-31T00:00:00",
            "initial_capital": 50000.0,
            "position_sizing_type": "percent",
            "position_sizing_value": 5.0,
            "commission": 0.1,
            "slippage": 0.05,
            "fitness_metric": "sharpe_ratio"
        }

        response = test_client.post("/api/backtests", json=backtest_data)
        assert response.status_code == 200
        backtest = response.json()

        assert backtest["name"] == "Integration Test Backtest"
        assert backtest["strategyId"] == strategy["id"]
        assert backtest["strategyParams"]["initial_tp_percent"] == 10.0
        assert backtest["status"] == "pending"
        assert backtest["fitnessMetric"] == "sharpe_ratio"

        # Step 3: Verify the backtest can be retrieved
        response = test_client.get(f"/api/backtests/{backtest['id']}")
        assert response.status_code == 200
        retrieved = response.json()
        assert retrieved["id"] == backtest["id"]
        assert retrieved["strategyId"] == strategy["id"]

        # Step 4: Verify the strategy can be retrieved
        response = test_client.get(f"/api/strategies/{strategy['id']}")
        assert response.status_code == 200
        retrieved_strategy = response.json()
        assert retrieved_strategy["id"] == strategy["id"]

        # Cleanup
        test_client.delete(f"/api/backtests/{backtest['id']}")
        test_client.delete(f"/api/strategies/{strategy['id']}")

    def test_backtest_without_strategy(self, test_client, sample_model, sample_dataset):
        """Test creating a backtest without a strategy (using strategy_params directly)."""
        backtest_data = {
            "name": "No Strategy Backtest",
            "model_id": sample_model.model_id,
            "prediction_dataset_id": sample_dataset.id,
            "execution_dataset_id": sample_dataset.id,
            "strategy_params": {
                "entry_threshold": 0.8,
                "exit_bars": 20
            },
            "start_date": "2024-01-01T00:00:00",
            "end_date": "2024-03-31T00:00:00",
            "initial_capital": 10000.0
        }

        response = test_client.post("/api/backtests", json=backtest_data)
        assert response.status_code == 200

        data = response.json()
        assert data["strategyId"] is None
        assert data["strategyParams"]["entry_threshold"] == 0.8

        # Cleanup
        test_client.delete(f"/api/backtests/{data['id']}")

    def test_list_backtests_shows_created(self, test_client, sample_model, sample_dataset):
        """Test that created backtests appear in list."""
        # Create a backtest
        backtest_data = {
            "name": "List Test Backtest",
            "model_id": sample_model.model_id,
            "prediction_dataset_id": sample_dataset.id,
            "execution_dataset_id": sample_dataset.id,
            "start_date": "2024-01-01T00:00:00",
            "end_date": "2024-06-30T00:00:00"
        }

        response = test_client.post("/api/backtests", json=backtest_data)
        assert response.status_code == 200
        backtest = response.json()

        # List backtests
        response = test_client.get("/api/backtests")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] >= 1
        backtest_ids = [b["id"] for b in data["backtests"]]
        assert backtest["id"] in backtest_ids

        # Cleanup
        test_client.delete(f"/api/backtests/{backtest['id']}")


# =============================================================================
# Main entry point
# =============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
