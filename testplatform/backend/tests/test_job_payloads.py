"""
Test job handler with various payload configurations.

Uses dry_run=True to test data preparation without actual training.
"""

import pytest
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Test database - MUST be set before any app imports
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_job_payloads.db")
TEST_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "AAPL_1h_test.csv")

if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ['DATABASE_URL'] = f"sqlite:///{TEST_DB_PATH}"

from app.services.job_handler import handle_training_job


@pytest.fixture(scope="module")
def test_db():
    """Set up test database with a real dataset."""
    from app.models.database import engine, Base, SessionLocal
    import app.models  # noqa: F401 - Register all models

    Base.metadata.create_all(bind=engine)

    db = SessionLocal()

    import pandas as pd
    from datetime import datetime
    df = pd.read_csv(TEST_DATA_PATH)

    from app.models.dataset import Dataset, DatasetStatus
    dataset = Dataset(
        name="AAPL_1h_test",
        ticker="AAPL",
        timeframe="1h",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 6, 30),
        rows_count=len(df),
        status=DatasetStatus.READY.value,
        file_path=TEST_DATA_PATH,
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)

    yield db, dataset.id

    db.close()
    engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass


# Sample payloads for testing
CLASSIFICATION_ZIGZAG_PAYLOAD = {
    "job_type": "classification",
    "dataset_ids": [1],  # Assumes dataset 1 exists
    "selected_models": ["lstm", "gru"],
    "parameter_ranges": {
        "layersMin": 2,
        "layersMax": 4,
        "layersStep": 1,
        "layerSizeMin": 32,
        "layerSizeMax": 128,
        "layerSizeStep": 32,
        "learningRateMin": 0.001,
        "learningRateMax": 0.01,
        "learningRateStep": 0.002,
        "dropoutMin": 0.0,
        "dropoutMax": 0.5,
        "dropoutStep": 0.1,
        "seqLen": 24
    },
    "prediction_targets": [
        {
            "type": "trend_reversal",
            "category": "binary_classification",
            "indicator": "zigzag",
            "indicatorParams": {
                "deviationPct": 2
            },
            "threshold": 30,
            "direction": "bullish"
        }
    ],
    "prediction_horizon": 3,
    "prediction_modes": ["shift"],
    "train_test_split": 70,
    "cross_validation": None,
    "genetic_config": {
        "populationSize": 20,
        "generations": 10,
        "elitismPercent": 10.0,
        "crossoverProb": 0.7,
        "mutationProb": 0.2,
        "earlyStoppingGenerations": 5,
        "trainingEpochs": 5
    },
    "metrics_config": {
        "optimizeMetric": "f1_score",
        "classificationMetric": "f1_score",
        "regressionMetric": "rmse",
        "lossFunction": "focal_loss"
    },
    "training_date_range": None
}

CLASSIFICATION_RSI_PAYLOAD = {
    "job_type": "classification",
    "dataset_ids": [1],
    "selected_models": ["tcn", "inception"],
    "parameter_ranges": {
        "layersMin": 1,
        "layersMax": 3,
        "layersStep": 1,
        "layerSizeMin": 64,
        "layerSizeMax": 256,
        "layerSizeStep": 64,
        "learningRateMin": 0.0001,
        "learningRateMax": 0.01,
        "learningRateStep": 0.001,
        "dropoutMin": 0.1,
        "dropoutMax": 0.4,
        "dropoutStep": 0.1,
        "seqLen": 32
    },
    "prediction_targets": [
        {
            "type": "trend_reversal",
            "category": "binary_classification",
            "indicator": "rsi",
            "indicatorParams": {
                "period": 14
            },
            "threshold": 30,
            "direction": "bullish"
        },
        {
            "type": "trend_reversal",
            "category": "binary_classification",
            "indicator": "rsi",
            "indicatorParams": {
                "period": 14
            },
            "threshold": 70,
            "direction": "bearish"
        }
    ],
    "prediction_horizon": 5,
    "prediction_modes": ["shift", "multistep"],
    "train_test_split": 80,
    "cross_validation": None,
    "genetic_config": {
        "populationSize": 30,
        "generations": 15,
        "elitismPercent": 15.0,
        "crossoverProb": 0.8,
        "mutationProb": 0.15,
        "earlyStoppingGenerations": 3,
        "trainingEpochs": 10
    },
    "metrics_config": {
        "optimizeMetric": "f1_score",
        "classificationMetric": "f1_score",
        "regressionMetric": "rmse",
        "lossFunction": "weighted_cross_entropy"
    },
    "training_date_range": None
}

CLASSIFICATION_DIRECTIONAL_PAYLOAD = {
    "job_type": "classification",
    "dataset_ids": [1],
    "selected_models": ["lstm"],
    "parameter_ranges": {
        "layersMin": 2,
        "layersMax": 3,
        "layersStep": 1,
        "layerSizeMin": 32,
        "layerSizeMax": 64,
        "layerSizeStep": 32,
        "learningRateMin": 0.001,
        "learningRateMax": 0.005,
        "learningRateStep": 0.001,
        "dropoutMin": 0.0,
        "dropoutMax": 0.3,
        "dropoutStep": 0.1,
        "seqLen": 16
    },
    "prediction_targets": [
        {
            "type": "directional",
            "category": "binary_classification",
            "config": {
                "horizon": 1
            }
        }
    ],
    "prediction_horizon": 1,
    "prediction_modes": ["shift"],
    "train_test_split": 75,
    "cross_validation": None,
    "genetic_config": {
        "populationSize": 10,
        "generations": 5,
        "elitismPercent": 10.0,
        "crossoverProb": 0.7,
        "mutationProb": 0.2,
        "earlyStoppingGenerations": 3,
        "trainingEpochs": 3
    },
    "metrics_config": {
        "optimizeMetric": "accuracy",
        "classificationMetric": "accuracy",
        "regressionMetric": "rmse",
        "lossFunction": "cross_entropy"
    },
    "training_date_range": None
}

# Payloads that should fail validation
MISSING_JOB_TYPE_PAYLOAD = {
    "dataset_ids": [1],
    "selected_models": ["lstm"],
    "parameter_ranges": {},
    "prediction_targets": [],
    "prediction_horizon": 3,
    "prediction_modes": ["shift"],
    "train_test_split": 80,
    "genetic_config": {},
    "metrics_config": {}
}

MISSING_GENETIC_CONFIG_PAYLOAD = {
    "job_type": "classification",
    "dataset_ids": [1],
    "selected_models": ["lstm"],
    "parameter_ranges": {"seqLen": 24, "layersMin": 1, "layersMax": 2, "layerSizeMin": 32, "layerSizeMax": 64,
                         "learningRateMin": 0.001, "learningRateMax": 0.01, "dropoutMin": 0.0, "dropoutMax": 0.3},
    "prediction_targets": [{"type": "directional", "config": {"horizon": 1}}],
    "prediction_horizon": 3,
    "prediction_modes": ["shift"],
    "train_test_split": 80,
    "genetic_config": {},  # Empty - should fail
    "metrics_config": {"classificationMetric": "f1_score", "lossFunction": "focal_loss"}
}


class TestJobPayloadValidation:
    """Test payload validation without database access."""

    def test_missing_job_type(self):
        """Should fail if job_type is missing."""
        result = handle_training_job("test-001", MISSING_JOB_TYPE_PAYLOAD, dry_run=True)
        assert result['status'] == 'failed'
        assert 'job_type' in result['error'].lower()

    def test_missing_genetic_config_fields(self):
        """Should fail if genetic_config is missing required fields."""
        result = handle_training_job("test-002", MISSING_GENETIC_CONFIG_PAYLOAD, dry_run=True)
        assert result['status'] == 'failed'
        assert 'genetic_config' in result['error'].lower() or 'required' in result['error'].lower()


class TestJobPayloadsDryRun:
    """Test actual job payloads with dry_run=True (requires database with dataset)."""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        """Use test database dataset."""
        _, dataset_id = test_db
        self.dataset_id = dataset_id

    def _update_payload_dataset(self, payload: dict) -> dict:
        """Update payload to use actual dataset ID."""
        payload = payload.copy()
        payload['dataset_ids'] = [self.dataset_id]
        return payload

    def test_classification_zigzag_dry_run(self):
        """Test classification with zigzag targets."""
        payload = self._update_payload_dataset(CLASSIFICATION_ZIGZAG_PAYLOAD)
        result = handle_training_job("test-zigzag", payload, dry_run=True)

        assert result['status'] == 'dry_run_success', f"Failed: {result.get('error', result)}"
        assert result['job_type'] == 'classification'
        assert result['dataset_rows'] > 0
        assert result['train_rows'] > 0
        assert result['test_rows'] > 0
        assert result['target_column'] is not None
        assert 'target_distribution' in result
        print(f"ZigZag test passed: {result}")

    def test_classification_rsi_dry_run(self):
        """Test classification with RSI targets and both prediction modes."""
        payload = self._update_payload_dataset(CLASSIFICATION_RSI_PAYLOAD)
        result = handle_training_job("test-rsi", payload, dry_run=True)

        assert result['status'] == 'dry_run_success', f"Failed: {result.get('error', result)}"
        assert result['job_type'] == 'classification'
        assert result['prediction_modes'] == ['shift', 'multistep']
        print(f"RSI test passed: {result}")

    def test_classification_directional_dry_run(self):
        """Test classification with simple directional target."""
        payload = self._update_payload_dataset(CLASSIFICATION_DIRECTIONAL_PAYLOAD)
        result = handle_training_job("test-directional", payload, dry_run=True)

        assert result['status'] == 'dry_run_success', f"Failed: {result.get('error', result)}"
        assert result['job_type'] == 'classification'
        assert 'direction' in result['target_column']
        print(f"Directional test passed: {result}")


def run_quick_tests():
    """Run quick validation tests without pytest."""
    print("=" * 60)
    print("Running quick job payload validation tests...")
    print("=" * 60)

    # Test missing job_type
    print("\n1. Testing missing job_type...")
    result = handle_training_job("test-001", MISSING_JOB_TYPE_PAYLOAD, dry_run=True)
    assert result['status'] == 'failed', f"Expected failure, got: {result}"
    print(f"   PASS: {result['error']}")

    # Test missing genetic_config fields
    print("\n2. Testing missing genetic_config fields...")
    result = handle_training_job("test-002", MISSING_GENETIC_CONFIG_PAYLOAD, dry_run=True)
    assert result['status'] == 'failed', f"Expected failure, got: {result}"
    print(f"   PASS: {result['error']}")

    print("\n" + "=" * 60)
    print("Quick validation tests passed!")
    print("=" * 60)


def run_full_tests():
    """Run full tests with database access."""
    print("=" * 60)
    print("Running full job payload tests with dry_run...")
    print("=" * 60)

    try:
        from app.models.database import get_db
        from app.models.dataset import Dataset
        db = next(get_db())
        dataset = db.query(Dataset).first()
        if dataset is None:
            print("ERROR: No datasets in database. Please create a dataset first.")
            return False
        dataset_id = dataset.id
        print(f"Using dataset ID: {dataset_id}")
    except Exception as e:
        print(f"ERROR: Cannot access database: {e}")
        return False

    def update_dataset(payload):
        p = payload.copy()
        p['dataset_ids'] = [dataset_id]
        return p

    # Test 1: ZigZag targets
    print("\n1. Testing classification with ZigZag targets...")
    payload = update_dataset(CLASSIFICATION_ZIGZAG_PAYLOAD)
    result = handle_training_job("test-zigzag", payload, dry_run=True)
    if result['status'] != 'dry_run_success':
        print(f"   FAIL: {result.get('error', result)}")
        return False
    print(f"   PASS: {result['dataset_rows']} rows, target: {result['target_column']}")
    print(f"         Distribution: {result['target_distribution']}")

    # Test 2: RSI targets with both modes
    print("\n2. Testing classification with RSI targets (both modes)...")
    payload = update_dataset(CLASSIFICATION_RSI_PAYLOAD)
    result = handle_training_job("test-rsi", payload, dry_run=True)
    if result['status'] != 'dry_run_success':
        print(f"   FAIL: {result.get('error', result)}")
        return False
    print(f"   PASS: {result['dataset_rows']} rows, target: {result['target_column']}")
    print(f"         Modes: {result['prediction_modes']}")

    # Test 3: Directional target
    print("\n3. Testing classification with directional target...")
    payload = update_dataset(CLASSIFICATION_DIRECTIONAL_PAYLOAD)
    result = handle_training_job("test-directional", payload, dry_run=True)
    if result['status'] != 'dry_run_success':
        print(f"   FAIL: {result.get('error', result)}")
        return False
    print(f"   PASS: {result['dataset_rows']} rows, target: {result['target_column']}")

    print("\n" + "=" * 60)
    print("All full tests passed!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test job handler payloads")
    parser.add_argument("--quick", action="store_true", help="Run quick validation tests only")
    parser.add_argument("--full", action="store_true", help="Run full tests with database")
    args = parser.parse_args()

    if args.quick:
        run_quick_tests()
    elif args.full:
        run_full_tests()
    else:
        # Run both
        run_quick_tests()
        print("\n")
        run_full_tests()
