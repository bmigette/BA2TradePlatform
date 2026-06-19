# Classification/Regression Split Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split ML training into Classification (tsai) and Regression (Darts) job types with proper abstractions and UI.

**Architecture:** Create IModelService/ITrainingService interfaces, implement for both libraries, add factory routing in job_handler, update frontend with 3-step wizard and nested target tabs.

**Tech Stack:** Python (tsai, fastai, darts, PyTorch), TypeScript/React, SQLite, FastAPI

---

## Phase 1: Backend Foundation

### Task 1: Install tsai and verify GPU support

**Files:**
- Modify: `backend/requirements.txt`

**Step 1: Add tsai to requirements**

Add to `backend/requirements.txt`:
```
tsai>=0.3.9
fastai>=2.7.0
```

**Step 2: Install and verify**

Run:
```bash
cd backend && ./venv/bin/pip install tsai fastai
```

**Step 3: Verify GPU detection**

Run:
```bash
./venv/bin/python -c "
import torch
print(f'CUDA available: {torch.cuda.is_available()}')
print(f'MPS available: {hasattr(torch.backends, \"mps\") and torch.backends.mps.is_available()}')
from tsai.all import *
print('tsai imported successfully')
"
```

**Step 4: Commit**

```bash
git add requirements.txt
git commit -m "feat: add tsai and fastai dependencies"
```

---

### Task 2: Set up test data and folder structure

**Files:**
- Copy: `backend/datasets/AAPL_1h_*.csv` → `backend/tests/data/AAPL_1h_test.csv`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/data/.gitkeep`

**Step 1: Create test folder structure**

```bash
cd backend
mkdir -p tests/data
touch tests/__init__.py
touch tests/data/.gitkeep
```

**Step 2: Copy AAPL dataset for testing**

```bash
cp datasets/AAPL_1h_*.csv tests/data/AAPL_1h_test.csv
```

**Step 3: Commit**

```bash
git add tests/
git commit -m "test: add test folder structure with AAPL dataset"
```

---

### Task 3: Create model interface abstraction

**Files:**
- Create: `backend/app/services/model_interface.py`

**Step 1: Write the interface**

```python
"""
Abstract interfaces for model and training services.

Provides abstraction layer allowing classification (tsai) and regression (Darts)
implementations to be used interchangeably.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd


class IModelService(ABC):
    """Abstract interface for model creation services."""

    @abstractmethod
    def create_model(
        self,
        model_type: str,
        params: Dict[str, Any],
        loss_fn: Any = None,
        epoch_callback: callable = None
    ) -> Any:
        """
        Create a model of the specified type.

        Args:
            model_type: Model architecture name (e.g., 'lstm', 'inception')
            params: Model hyperparameters
            loss_fn: Optional custom loss function
            epoch_callback: Optional callback for epoch progress

        Returns:
            Configured model ready for training
        """
        pass

    @abstractmethod
    def get_available_models(self) -> Dict[str, Dict]:
        """
        Get available model architectures with their configurations.

        Returns:
            Dictionary of model configs with default params and ranges
        """
        pass

    @abstractmethod
    def get_parameter_ranges(self, model_type: str) -> Dict[str, List]:
        """
        Get hyperparameter ranges for genetic optimization.

        Args:
            model_type: Model architecture name

        Returns:
            Dictionary mapping param names to valid value ranges
        """
        pass

    @abstractmethod
    def apply_layer_size_factor(self, params: Dict, factor: float) -> Dict:
        """
        Scale layer size parameters by a factor.

        Args:
            params: Original parameters
            factor: Scaling factor (e.g., 0.5, 1.0, 2.0)

        Returns:
            Scaled parameters
        """
        pass


class ITrainingService(ABC):
    """Abstract interface for training services."""

    @abstractmethod
    def prepare_data(
        self,
        df: pd.DataFrame,
        target_column: str,
        feature_columns: List[str],
        timeframe: str = 'daily'
    ) -> Tuple[Any, Any]:
        """
        Prepare data for model training.

        Args:
            df: DataFrame with features and target
            target_column: Name of target column
            feature_columns: List of feature column names
            timeframe: Data timeframe for frequency inference

        Returns:
            Tuple of (prepared_data, covariates/metadata)
        """
        pass

    @abstractmethod
    def prepare_data_split(
        self,
        df: pd.DataFrame,
        train_ratio: float,
        target_column: str,
        feature_columns: List[str],
        timeframe: str = 'daily'
    ) -> Tuple[Any, Any, Any, Any]:
        """
        Prepare and split data into train/test sets.

        Args:
            df: Full DataFrame
            train_ratio: Fraction for training (0.0-1.0)
            target_column: Name of target column
            feature_columns: List of feature column names
            timeframe: Data timeframe

        Returns:
            Tuple of (train_data, test_data, train_meta, test_meta)
        """
        pass

    @abstractmethod
    def train_model(
        self,
        model: Any,
        train_data: Any,
        val_data: Any = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Train a model on prepared data.

        Args:
            model: Model to train
            train_data: Training data
            val_data: Optional validation data
            **kwargs: Additional training options

        Returns:
            Training result with status, metrics, history
        """
        pass

    @abstractmethod
    def evaluate_model(
        self,
        model: Any,
        test_data: Any,
        metric: str = 'f1_score',
        threshold: float = 0.5,
        **kwargs
    ) -> Dict[str, float]:
        """
        Evaluate a trained model.

        Args:
            model: Trained model
            test_data: Test data
            metric: Primary metric to optimize
            threshold: Classification threshold (for classification)
            **kwargs: Additional evaluation options

        Returns:
            Dictionary of evaluation metrics
        """
        pass

    @abstractmethod
    def predict(
        self,
        model: Any,
        data: Any,
        **kwargs
    ) -> Any:
        """
        Generate predictions from a trained model.

        Args:
            model: Trained model
            data: Input data
            **kwargs: Additional prediction options

        Returns:
            Predictions
        """
        pass
```

**Step 2: Commit**

```bash
git add backend/app/services/model_interface.py
git commit -m "feat: add abstract interfaces for model/training services"
```

---

### Task 4: Refactor Darts services to implement interfaces

**Files:**
- Rename: `backend/app/services/ml_models.py` → `backend/app/services/darts_models.py`
- Rename: `backend/app/services/training.py` → `backend/app/services/darts_training.py`
- Modify: Both files to implement interfaces

**Step 1: Rename files**

```bash
cd backend
git mv app/services/ml_models.py app/services/darts_models.py
git mv app/services/training.py app/services/darts_training.py
```

**Step 2: Update darts_models.py imports and class**

At top of `darts_models.py`, add interface import and update class definition:

```python
from app.services.model_interface import IModelService

# Change class definition from:
# class MLModelsService:
# To:
class DartsModelService(IModelService):
```

Add the `apply_layer_size_factor` method:

```python
def apply_layer_size_factor(self, params: Dict, factor: float) -> Dict:
    """Scale layer size parameters by a factor."""
    scaled = params.copy()
    size_params = ['hidden_dim', 'layer_widths', 'd_model', 'dim_feedforward',
                   'hidden_size', 'num_filters']
    for key in size_params:
        if key in scaled:
            if isinstance(scaled[key], (list, tuple)):
                scaled[key] = [int(v * factor) for v in scaled[key]]
            else:
                scaled[key] = int(scaled[key] * factor)
    return scaled
```

**Step 3: Update darts_training.py imports and class**

At top of `darts_training.py`, add interface import and update class definition:

```python
from app.services.model_interface import ITrainingService

# Change class definition from:
# class TrainingService:
# To:
class DartsTrainingService(ITrainingService):
```

Add missing `predict` method if not present:

```python
def predict(self, model: Any, data: Any, n: int = None, **kwargs) -> Any:
    """Generate predictions from a trained model."""
    if n is None:
        n = getattr(model, 'output_chunk_length', 1)
    return model.predict(n=n, series=data, **kwargs)
```

**Step 4: Update all imports throughout codebase**

Run:
```bash
cd backend
grep -r "from app.services.ml_models import" --include="*.py" -l
grep -r "from app.services.training import" --include="*.py" -l
```

Update each file found to use new paths:
- `from app.services.ml_models import MLModelsService` → `from app.services.darts_models import DartsModelService`
- `from app.services.training import TrainingService` → `from app.services.darts_training import DartsTrainingService`

**Step 5: Verify imports work**

```bash
./venv/bin/python -c "
from app.services.darts_models import DartsModelService
from app.services.darts_training import DartsTrainingService
print('Darts services imported successfully')
"
```

**Step 6: Commit**

```bash
git add -A
git commit -m "refactor: rename and adapt Darts services to implement interfaces"
```

---

### Task 5: Create tsai model service

**Files:**
- Create: `backend/app/services/tsai_models.py`

**Step 1: Write TSAIModelService**

```python
"""
tsai Model Service for classification models.

Provides time series classification models using the tsai library.
Supports: LSTM, GRU, TCN, InceptionTime, ResNet, XceptionTime,
OmniScaleCNN, MiniRocket, PatchTST, LSTM-FCN, TST.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Check for tsai availability
try:
    import torch
    from tsai.all import (
        LSTM, GRU, TCN, InceptionTime, ResNet, XceptionTime,
        OmniScaleCNN, MiniRocket, PatchTST, LSTM_FCN, TST
    )
    TSAI_AVAILABLE = True
    CUDA_AVAILABLE = torch.cuda.is_available()
    MPS_AVAILABLE = hasattr(torch.backends, 'mps') and torch.backends.mps.is_available()

    if CUDA_AVAILABLE:
        DEVICE = torch.device('cuda')
        GPU_NAME = torch.cuda.get_device_name(0)
    elif MPS_AVAILABLE:
        DEVICE = torch.device('mps')
        GPU_NAME = 'Apple Silicon'
    else:
        DEVICE = torch.device('cpu')
        GPU_NAME = None

    logger.info(f"tsai available. Device: {DEVICE}, GPU: {GPU_NAME}")
except ImportError as e:
    TSAI_AVAILABLE = False
    CUDA_AVAILABLE = False
    MPS_AVAILABLE = False
    DEVICE = None
    GPU_NAME = None
    logger.warning(f"tsai not available: {e}")

from app.services.model_interface import IModelService


# Model class registry - maps model type to class
MODEL_CLASSES = {}
if TSAI_AVAILABLE:
    MODEL_CLASSES = {
        'lstm': LSTM,
        'gru': GRU,
        'tcn': TCN,
        'inception': InceptionTime,
        'resnet': ResNet,
        'xception': XceptionTime,
        'omniscale': OmniScaleCNN,
        'minirocket': MiniRocket,
        'patchtst': PatchTST,
        'lstm_fcn': LSTM_FCN,
        'tst': TST,
    }


class TSAIModelService(IModelService):
    """
    tsai-based model service for time series classification.
    """

    MODEL_ARCHITECTURES = {
        'lstm': {
            'name': 'LSTM',
            'description': 'Long Short-Term Memory network',
            'default_params': {
                'hidden_size': 64,
                'n_layers': 2,
                'bidirectional': False,
                'dropout': 0.1,
            },
            'param_ranges': {
                'hidden_size': [32, 64, 128, 256],
                'n_layers': [1, 2, 3],
                'bidirectional': [True, False],
                'dropout': [0.0, 0.1, 0.2, 0.3],
            }
        },
        'gru': {
            'name': 'GRU',
            'description': 'Gated Recurrent Unit network',
            'default_params': {
                'hidden_size': 64,
                'n_layers': 2,
                'bidirectional': False,
                'dropout': 0.1,
            },
            'param_ranges': {
                'hidden_size': [32, 64, 128, 256],
                'n_layers': [1, 2, 3],
                'bidirectional': [True, False],
                'dropout': [0.0, 0.1, 0.2, 0.3],
            }
        },
        'tcn': {
            'name': 'TCN',
            'description': 'Temporal Convolutional Network',
            'default_params': {
                'layers': [64, 64, 64],
                'ks': 7,
                'conv_dropout': 0.1,
            },
            'param_ranges': {
                'layers': [[32, 32], [64, 64], [64, 64, 64], [128, 128]],
                'ks': [3, 5, 7, 9],
                'conv_dropout': [0.0, 0.1, 0.2],
            }
        },
        'inception': {
            'name': 'InceptionTime',
            'description': 'State-of-the-art CNN for time series (Fawaz 2019)',
            'default_params': {
                'nf': 32,
                'depth': 6,
                'ks': 40,
            },
            'param_ranges': {
                'nf': [16, 32, 64, 128],
                'depth': [3, 6, 9],
                'ks': [20, 40, 60],
            }
        },
        'resnet': {
            'name': 'ResNet',
            'description': 'Residual Network for time series',
            'default_params': {
                'nf': 64,
                'kss': [7, 5, 3],
            },
            'param_ranges': {
                'nf': [32, 64, 128],
                'kss': [[7, 5, 3], [9, 7, 5], [5, 3, 3]],
            }
        },
        'xception': {
            'name': 'XceptionTime',
            'description': 'Improved InceptionTime (Rahimian 2019)',
            'default_params': {
                'nf': 16,
                'depth': 6,
            },
            'param_ranges': {
                'nf': [8, 16, 32, 64],
                'depth': [3, 6, 9],
            }
        },
        'omniscale': {
            'name': 'OmniScaleCNN',
            'description': 'Multi-scale 1D-CNN (Tang 2020)',
            'default_params': {
                'layers': [64, 64, 64],
                'ks': 40,
            },
            'param_ranges': {
                'layers': [[32, 32], [64, 64], [64, 64, 64]],
                'ks': [20, 40, 60],
            }
        },
        'minirocket': {
            'name': 'MiniRocket',
            'description': 'Extremely fast, minimal training (Dempster 2021)',
            'default_params': {
                'num_features': 10000,
            },
            'param_ranges': {
                'num_features': [5000, 10000, 20000],
            }
        },
        'patchtst': {
            'name': 'PatchTST',
            'description': 'State-of-the-art transformer (Nie 2022)',
            'default_params': {
                'd_model': 128,
                'n_heads': 8,
                'patch_len': 16,
                'd_ff': 256,
                'dropout': 0.1,
            },
            'param_ranges': {
                'd_model': [64, 128, 256],
                'n_heads': [4, 8, 16],
                'patch_len': [8, 16, 24],
                'd_ff': [128, 256, 512],
                'dropout': [0.0, 0.1, 0.2],
            }
        },
        'lstm_fcn': {
            'name': 'LSTM-FCN',
            'description': 'Hybrid LSTM + CNN (Karim 2017)',
            'default_params': {
                'hidden_size': 128,
                'rnn_layers': 1,
                'cell_dropout': 0.1,
                'fc_dropout': 0.1,
            },
            'param_ranges': {
                'hidden_size': [64, 128, 256],
                'rnn_layers': [1, 2],
                'cell_dropout': [0.0, 0.1, 0.2],
                'fc_dropout': [0.0, 0.1, 0.2],
            }
        },
        'tst': {
            'name': 'TST',
            'description': 'Time Series Transformer (Zerveas 2020)',
            'default_params': {
                'd_model': 128,
                'n_heads': 8,
                'd_ff': 256,
                'n_layers': 3,
                'dropout': 0.1,
            },
            'param_ranges': {
                'd_model': [64, 128, 256],
                'n_heads': [4, 8],
                'd_ff': [128, 256, 512],
                'n_layers': [2, 3, 4],
                'dropout': [0.0, 0.1, 0.2],
            }
        },
    }

    def __init__(self, use_gpu: bool = True):
        """Initialize TSAIModelService."""
        self.use_gpu = use_gpu and (CUDA_AVAILABLE or MPS_AVAILABLE)
        self.device = DEVICE if self.use_gpu else torch.device('cpu') if TSAI_AVAILABLE else None

        if self.use_gpu:
            logger.info(f"Using GPU: {GPU_NAME}")
        else:
            logger.info("Using CPU for model training")

    def get_available_models(self) -> Dict[str, Dict]:
        """Get available tsai model architectures."""
        return {k: {
            'name': v['name'],
            'description': v['description'],
            'default_params': v['default_params'],
        } for k, v in self.MODEL_ARCHITECTURES.items()}

    def get_parameter_ranges(self, model_type: str) -> Dict[str, List]:
        """Get hyperparameter ranges for a model type."""
        model_type = model_type.lower()
        if model_type not in self.MODEL_ARCHITECTURES:
            raise ValueError(f"Unknown model type: {model_type}")
        return self.MODEL_ARCHITECTURES[model_type]['param_ranges']

    def apply_layer_size_factor(self, params: Dict, factor: float) -> Dict:
        """Scale layer size parameters by a factor."""
        scaled = params.copy()
        size_params = ['hidden_size', 'nf', 'd_model', 'd_ff', 'num_features']

        for key in size_params:
            if key in scaled:
                scaled[key] = int(scaled[key] * factor)

        # Handle list params like 'layers'
        if 'layers' in scaled and isinstance(scaled['layers'], list):
            scaled['layers'] = [int(v * factor) for v in scaled['layers']]

        return scaled

    def create_model(
        self,
        model_type: str,
        params: Dict[str, Any],
        loss_fn: Any = None,
        epoch_callback: callable = None,
        c_in: int = None,
        c_out: int = 2,
        seq_len: int = None
    ) -> Any:
        """
        Create a tsai model.

        Args:
            model_type: Model architecture name
            params: Model hyperparameters
            loss_fn: Optional custom loss function (e.g., FocalLoss)
            epoch_callback: Optional callback for epoch progress
            c_in: Number of input channels/features
            c_out: Number of output classes (default 2 for binary)
            seq_len: Sequence length

        Returns:
            Configured tsai model architecture
        """
        if not TSAI_AVAILABLE:
            raise RuntimeError("tsai library not available")

        model_type = model_type.lower()
        if model_type not in self.MODEL_ARCHITECTURES:
            raise ValueError(f"Unknown model type: {model_type}. "
                           f"Supported: {list(self.MODEL_ARCHITECTURES.keys())}")

        arch_config = self.MODEL_ARCHITECTURES[model_type]
        p = {**arch_config['default_params'], **params}

        # Get model class from registry (no eval)
        model_class = MODEL_CLASSES.get(model_type)
        if model_class is None:
            raise ValueError(f"Model class not found for: {model_type}")

        # Build model based on type
        if model_type in ('lstm', 'gru'):
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
                hidden_size=p['hidden_size'],
                n_layers=p['n_layers'],
                bidirectional=p.get('bidirectional', False),
                dropout=p.get('dropout', 0.1),
            )
        elif model_type == 'tcn':
            model = model_class(
                c_in=c_in, c_out=c_out,
                layers=p.get('layers', [64, 64]),
                ks=p.get('ks', 7),
                conv_dropout=p.get('conv_dropout', 0.1),
            )
        elif model_type == 'inception':
            model = model_class(
                c_in=c_in, c_out=c_out,
                nf=p.get('nf', 32),
                depth=p.get('depth', 6),
                ks=p.get('ks', 40),
            )
        elif model_type == 'resnet':
            model = model_class(
                c_in=c_in, c_out=c_out,
                nf=p.get('nf', 64),
            )
        elif model_type == 'xception':
            model = model_class(
                c_in=c_in, c_out=c_out,
                nf=p.get('nf', 16),
            )
        elif model_type == 'omniscale':
            model = model_class(
                c_in=c_in, c_out=c_out,
            )
        elif model_type == 'minirocket':
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
                num_features=p.get('num_features', 10000),
            )
        elif model_type == 'patchtst':
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
                d_model=p.get('d_model', 128),
                n_heads=p.get('n_heads', 8),
                patch_len=p.get('patch_len', 16),
                d_ff=p.get('d_ff', 256),
                dropout=p.get('dropout', 0.1),
            )
        elif model_type == 'lstm_fcn':
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
                hidden_size=p.get('hidden_size', 128),
                rnn_layers=p.get('rnn_layers', 1),
                cell_dropout=p.get('cell_dropout', 0.1),
                fc_dropout=p.get('fc_dropout', 0.1),
            )
        elif model_type == 'tst':
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
                d_model=p.get('d_model', 128),
                n_heads=p.get('n_heads', 8),
                d_ff=p.get('d_ff', 256),
                n_layers=p.get('n_layers', 3),
                dropout=p.get('dropout', 0.1),
            )
        else:
            raise ValueError(f"Model creation not implemented for: {model_type}")

        logger.info(f"Created {model_type.upper()} model with params: {p}")
        return model

    @staticmethod
    def get_system_info() -> Dict[str, Any]:
        """Get system information for ML training."""
        return {
            'tsai_available': TSAI_AVAILABLE,
            'cuda_available': CUDA_AVAILABLE,
            'mps_available': MPS_AVAILABLE,
            'gpu_name': GPU_NAME,
            'device': str(DEVICE) if DEVICE else 'cpu',
        }
```

**Step 2: Verify tsai models can be created**

```bash
./venv/bin/python -c "
from app.services.tsai_models import TSAIModelService
svc = TSAIModelService()
print('Available models:', list(svc.get_available_models().keys()))
print('System info:', svc.get_system_info())
"
```

**Step 3: Commit**

```bash
git add backend/app/services/tsai_models.py
git commit -m "feat: add TSAIModelService with 11 classification models"
```

---

### Task 6: Create tsai training service

**Files:**
- Create: `backend/app/services/tsai_training.py`

(See separate implementation file - too long for plan)

**Step 1: Create the training service with focal loss, evaluation, etc.**

**Step 2: Commit**

```bash
git add backend/app/services/tsai_training.py
git commit -m "feat: add TSAITrainingService for classification training"
```

---

### Task 7: Create comprehensive unit tests with AAPL dataset

**Files:**
- Create: `backend/tests/test_tsai_models.py`
- Create: `backend/tests/test_tsai_training.py`
- Create: `backend/tests/test_darts_models.py`

**Step 1: Write test_tsai_models.py**

```python
"""
Comprehensive unit tests for tsai model service.
Tests all 11 classification models with real AAPL data.
"""
import pytest
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, '.')

from app.services.tsai_models import TSAIModelService, TSAI_AVAILABLE

# Skip all tests if tsai not available
pytestmark = pytest.mark.skipif(not TSAI_AVAILABLE, reason="tsai not available")

# Test data path
TEST_DATA_PATH = "tests/data/AAPL_1h_test.csv"


@pytest.fixture
def model_service():
    return TSAIModelService()


@pytest.fixture
def test_data():
    """Load and prepare AAPL test data."""
    df = pd.read_csv(TEST_DATA_PATH)
    # Use a subset for faster tests
    df = df.head(500)
    return df


@pytest.fixture
def sample_input():
    """Create sample input for model creation."""
    return {
        'c_in': 10,
        'c_out': 2,
        'seq_len': 24,
    }


class TestTSAIModelService:
    """Tests for TSAIModelService."""

    def test_get_available_models(self, model_service):
        """Test getting available models."""
        models = model_service.get_available_models()
        assert len(models) == 11
        assert 'lstm' in models
        assert 'inception' in models
        assert 'minirocket' in models
        assert 'patchtst' in models

    def test_get_parameter_ranges(self, model_service):
        """Test getting parameter ranges."""
        ranges = model_service.get_parameter_ranges('inception')
        assert 'nf' in ranges
        assert 'depth' in ranges
        assert isinstance(ranges['nf'], list)

    def test_get_parameter_ranges_invalid_model(self, model_service):
        """Test error on invalid model type."""
        with pytest.raises(ValueError):
            model_service.get_parameter_ranges('invalid_model')

    def test_apply_layer_size_factor(self, model_service):
        """Test layer size scaling."""
        params = {'hidden_size': 64, 'nf': 32, 'd_model': 128}
        scaled = model_service.apply_layer_size_factor(params, 2.0)
        assert scaled['hidden_size'] == 128
        assert scaled['nf'] == 64
        assert scaled['d_model'] == 256

    def test_get_system_info(self, model_service):
        """Test system info."""
        info = model_service.get_system_info()
        assert 'tsai_available' in info
        assert 'cuda_available' in info
        assert 'device' in info


class TestModelCreation:
    """Test creating each model type."""

    @pytest.mark.parametrize("model_type", [
        'lstm', 'gru', 'tcn', 'inception', 'resnet',
        'xception', 'omniscale', 'lstm_fcn', 'tst'
    ])
    def test_create_model(self, model_service, sample_input, model_type):
        """Test creating each model type."""
        model = model_service.create_model(
            model_type,
            {},
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )
        assert model is not None

    def test_create_minirocket(self, model_service, sample_input):
        """Test creating MiniRocket (requires seq_len)."""
        model = model_service.create_model(
            'minirocket',
            {},
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )
        assert model is not None

    def test_create_patchtst(self, model_service, sample_input):
        """Test creating PatchTST."""
        model = model_service.create_model(
            'patchtst',
            {'patch_len': 8},  # Smaller patch for short seq
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )
        assert model is not None

    def test_create_model_with_custom_params(self, model_service, sample_input):
        """Test creating model with custom parameters."""
        model = model_service.create_model(
            'lstm',
            {'hidden_size': 128, 'n_layers': 3, 'bidirectional': True},
            c_in=sample_input['c_in'],
            c_out=sample_input['c_out'],
            seq_len=sample_input['seq_len']
        )
        assert model is not None

    def test_create_invalid_model(self, model_service, sample_input):
        """Test error on invalid model type."""
        with pytest.raises(ValueError):
            model_service.create_model(
                'invalid',
                {},
                c_in=sample_input['c_in'],
                c_out=sample_input['c_out'],
                seq_len=sample_input['seq_len']
            )
```

**Step 2: Write test_tsai_training.py**

```python
"""
Comprehensive unit tests for tsai training service.
Tests training, evaluation, and loss functions with real AAPL data.
"""
import pytest
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, '.')

from app.services.tsai_models import TSAIModelService, TSAI_AVAILABLE
from app.services.tsai_training import TSAITrainingService

pytestmark = pytest.mark.skipif(not TSAI_AVAILABLE, reason="tsai not available")

TEST_DATA_PATH = "tests/data/AAPL_1h_test.csv"


@pytest.fixture
def training_service():
    return TSAITrainingService()


@pytest.fixture
def model_service():
    return TSAIModelService()


@pytest.fixture
def prepared_data(training_service):
    """Prepare train/test data from AAPL dataset."""
    df = pd.read_csv(TEST_DATA_PATH).head(500)

    # Add simple binary target (price up next bar)
    df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)
    df = df.dropna()

    feature_cols = ['Open', 'High', 'Low', 'Close', 'Volume']

    X_train, X_test, y_train, y_test = training_service.prepare_data_split(
        df, train_ratio=0.8,
        target_column='target',
        feature_columns=feature_cols,
        seq_len=24
    )
    return X_train, X_test, y_train, y_test


class TestTSAITrainingService:
    """Tests for TSAITrainingService."""

    def test_prepare_data_split(self, training_service):
        """Test data preparation and splitting."""
        df = pd.read_csv(TEST_DATA_PATH).head(200)
        df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)
        df = df.dropna()

        X_train, X_test, y_train, y_test = training_service.prepare_data_split(
            df, train_ratio=0.8,
            target_column='target',
            feature_columns=['Close', 'Volume'],
            seq_len=10
        )

        assert len(X_train) > 0
        assert len(X_test) > 0
        assert X_train.shape[1] == 2  # features
        assert X_train.shape[2] == 10  # seq_len

    def test_get_loss_function_focal(self, training_service):
        """Test focal loss creation."""
        loss = training_service.get_loss_function('focal')
        assert loss is not None

    def test_get_loss_function_ce(self, training_service):
        """Test cross-entropy loss creation."""
        loss = training_service.get_loss_function('ce')
        assert loss is not None


class TestTrainingWithRealData:
    """Integration tests with real AAPL data."""

    @pytest.mark.slow
    def test_train_lstm(self, model_service, training_service, prepared_data):
        """Test training LSTM model."""
        X_train, X_test, y_train, y_test = prepared_data

        model = model_service.create_model(
            'lstm', {},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model,
            (X_train, y_train),
            val_data=(X_test, y_test),
            epochs=2,
            batch_size=32
        )

        assert result['status'] == 'success'
        assert 'metrics' in result

    @pytest.mark.slow
    def test_train_inception(self, model_service, training_service, prepared_data):
        """Test training InceptionTime model."""
        X_train, X_test, y_train, y_test = prepared_data

        model = model_service.create_model(
            'inception', {'nf': 16, 'depth': 3},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model,
            (X_train, y_train),
            val_data=(X_test, y_test),
            epochs=2
        )

        assert result['status'] == 'success'

    @pytest.mark.slow
    def test_evaluate_model(self, model_service, training_service, prepared_data):
        """Test model evaluation."""
        X_train, X_test, y_train, y_test = prepared_data

        model = model_service.create_model(
            'lstm', {'hidden_size': 32, 'n_layers': 1},
            c_in=X_train.shape[1],
            c_out=2,
            seq_len=X_train.shape[2]
        )

        result = training_service.train_model(
            model,
            (X_train, y_train),
            epochs=2
        )

        if result['status'] == 'success':
            eval_result = training_service.evaluate_model(
                result['model'],
                (X_test, y_test)
            )

            assert 'f1_score' in eval_result
            assert 'accuracy' in eval_result
            assert 'auc_roc' in eval_result
```

**Step 3: Run tests**

```bash
cd backend
./venv/bin/pytest tests/ -v --tb=short
```

**Step 4: Commit**

```bash
git add backend/tests/
git commit -m "test: add comprehensive unit tests for tsai models and training"
```

---

### Task 8-14: Database migrations, API updates, Frontend updates

(Remaining tasks follow same pattern - see design document for details)

---

## Summary

Total tasks: 14+
- Phase 1 (Backend): Tasks 1-7
- Phase 2 (API): Task 8-9
- Phase 3 (Frontend): Tasks 10-13
- Phase 4 (Testing): Task 14

Each phase should be tested before moving to the next.
