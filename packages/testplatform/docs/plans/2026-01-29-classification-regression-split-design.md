# Classification vs Regression Job Types - Design Document

**Date:** 2026-01-29
**Status:** Approved

## Overview

Split the ML training system into two distinct job types:
- **Classification jobs** using tsai library (binary classification)
- **Regression jobs** using Darts library (continuous value prediction)

This provides proper tooling for each use case instead of forcing classification through a regression library.

## Motivation

Darts is designed for time series forecasting (regression), not classification. Using it for binary classification required workarounds:
- Manual sigmoid application
- Custom loss function integration
- Float target conversion

tsai is purpose-built for time series classification with native support for:
- Classification architectures (InceptionTime, ResNet)
- Focal loss and class imbalance handling
- Proper probability outputs

## Database Schema Changes

### TrainedModel table
```python
job_type = Column(String(20), default='classification')  # 'classification' or 'regression'
```

### OptimizationProfile table
```python
job_type = Column(String(20), default='classification')
```

### Migrations
- `005_add_job_type_to_models.py`
- `006_add_job_type_to_profiles.py`

### TargetConfig type
```typescript
interface TargetConfig {
  // ... existing fields
  jobType: 'classification' | 'regression';
}
```

## Backend Architecture

### Interface Abstraction

**New file: `app/services/model_interface.py`**
```python
from abc import ABC, abstractmethod
from typing import Any, Dict, List

class IModelService(ABC):
    """Abstract interface for model services"""

    @abstractmethod
    def create_model(self, model_type: str, params: Dict, loss_fn: Any = None) -> Any:
        """Create a model of the specified type"""
        pass

    @abstractmethod
    def get_available_models(self) -> Dict[str, Dict]:
        """Get available model architectures"""
        pass

    @abstractmethod
    def get_parameter_ranges(self, model_type: str) -> Dict:
        """Get hyperparameter ranges for a model type"""
        pass

class ITrainingService(ABC):
    """Abstract interface for training services"""

    @abstractmethod
    def prepare_data(self, df, target_column: str, feature_columns: List[str],
                     timeframe: str) -> Any:
        """Prepare data for training"""
        pass

    @abstractmethod
    def prepare_data_split(self, df, train_ratio: float, target_column: str,
                           feature_columns: List[str], timeframe: str) -> tuple:
        """Prepare and split data into train/test"""
        pass

    @abstractmethod
    def train_model(self, model, train_data, val_data=None, **kwargs) -> Dict:
        """Train a model"""
        pass

    @abstractmethod
    def evaluate_model(self, model, test_data, metric: str, **kwargs) -> Dict:
        """Evaluate a trained model"""
        pass
```

### Implementations

**Darts (Regression):**
- `app/services/darts_models.py` - DartsModelService(IModelService)
- `app/services/darts_training.py` - DartsTrainingService(ITrainingService)

**tsai (Classification):**
- `app/services/tsai_models.py` - TSAIModelService(IModelService)
- `app/services/tsai_training.py` - TSAITrainingService(ITrainingService)

### Factory Routing

**In `job_handler.py`:**
```python
def get_services(job_type: str) -> tuple[IModelService, ITrainingService]:
    if job_type == 'classification':
        from app.services.tsai_models import TSAIModelService
        from app.services.tsai_training import TSAITrainingService
        return TSAIModelService(), TSAITrainingService()
    else:
        from app.services.darts_models import DartsModelService
        from app.services.darts_training import DartsTrainingService
        return DartsModelService(), DartsTrainingService()
```

## Model Types

### Classification Models (tsai)

| Model | Key Parameters | Notes |
|-------|----------------|-------|
| LSTM | hidden_size, n_layers, bidirectional, dropout | Classic RNN |
| GRU | hidden_size, n_layers, bidirectional, dropout | Faster than LSTM |
| TCN | layers, ks (kernel size), conv_dropout | Temporal CNN |
| InceptionTime | nf (filters), depth, ks | State-of-the-art CNN |
| ResNet | nf, kss (kernel sizes) | Residual CNN |
| XceptionTime | nf, depth | Improved Inception |
| OmniScaleCNN | layers, hidden_size | Multi-scale CNN |
| MiniRocket | num_features | Extremely fast, minimal training |
| PatchTST | d_model, n_heads, patch_len | State-of-the-art transformer (2022) |
| LSTM-FCN | hidden_size, rnn_layers, conv_layers | Hybrid LSTM + CNN |
| TST | d_model, n_heads, d_ff | Time Series Transformer |

### Regression Models (Darts)

| Model | Key Parameters |
|-------|----------------|
| LSTM | hidden_dim, n_rnn_layers, dropout |
| GRU | hidden_dim, n_rnn_layers, dropout |
| TCN | kernel_size, num_filters, dilation_base |
| N-BEATS | num_stacks, num_blocks, layer_widths |
| TFT | hidden_size, lstm_layers, num_attention_heads |
| Transformer | d_model, nhead, num_encoder_layers |

### Layer Size Factor

Both implementations support a layer size factor that scales all size-related parameters proportionally:
- `hidden_size *= factor`
- `nf *= factor`
- `layer_widths *= factor`
- etc.

## Prediction Targets

### Classification Targets (binary 0/1)

| Target Type | Description |
|-------------|-------------|
| price_based | Price up/down X% with max drawdown constraint |
| directional | Price up/down in N bars |
| triple_barrier | Hit profit target, stop loss, or timeout |
| trend_reversal | RSI/MACD/ZigZag/etc. signal detection |

### Regression Targets (continuous)

| Target Type | Description |
|-------------|-------------|
| volatility | Predict future volatility (std, range, ATR) |
| price_return | Predict % return in N bars (new) |

## Frontend Changes

### Job Wizard - 3 Step Flow

**Step 1: Common Settings**
- Job type toggle (Classification / Regression)
- Dataset selection
- Train/Test split
- Training date range
- Genetic algorithm config

**Step 2: Job-Specific Settings**

*Classification:*
- Model selection (tsai models)
- Target selection (classification targets)
- Metric: F1, Accuracy, Precision, Recall, MCC, etc.
- Loss: Focal Loss, Weighted BCE, Cross Entropy

*Regression:*
- Model selection (Darts models)
- Target selection (regression targets)
- Metric: MSE, RMSE, MAE, R², MAPE
- Loss: MSE, MAE, Huber

**Step 3: Parameter Ranges & Review**
- Hyperparameter ranges editor
- Layer size factor
- Job summary
- Create button

### PredictionTargetsPanel - Nested Tabs

```
[Classification] [Regression]  ← Top-level tabs
      │               │
      │               └── [Volatility] [Price Return]
      │
      └── [Price Based] [Directional] [Triple Barrier] [Trend Reversal]
```

### Job List Updates

- Add "Job Type" column with badge (green=Classification, blue=Regression)
- Add filter dropdown (All / Classification / Regression)

### Model Inventory Updates

- Add tab filter (All / Classification / Regression)
- Model cards show job type badge
- Job detail view shows library used (tsai / Darts)

## Dependencies

### New Packages
```
tsai>=0.3.9
fastai>=2.7.0
```

### GPU/CUDA Support
```python
import torch

def get_device():
    if torch.cuda.is_available():
        return torch.device('cuda')
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return torch.device('mps')
    return torch.device('cpu')
```

### Installation
```bash
# For CUDA support (Linux/Windows with NVIDIA GPU)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118

# For CPU only or Apple Silicon
pip install torch torchvision

# tsai
pip install tsai
```

## Files to Create

| File | Purpose |
|------|---------|
| `app/services/model_interface.py` | Abstract interfaces |
| `app/services/tsai_models.py` | TSAIModelService |
| `app/services/tsai_training.py` | TSAITrainingService |
| `db_migrate/005_add_job_type_to_models.py` | Migration |
| `db_migrate/006_add_job_type_to_profiles.py` | Migration |

## Files to Modify

| File | Changes |
|------|---------|
| `app/services/ml_models.py` | Rename to `darts_models.py`, implement IModelService |
| `app/services/training.py` | Rename to `darts_training.py`, implement ITrainingService |
| `app/services/job_handler.py` | Add factory routing based on job_type |
| `app/api/jobs.py` | Add jobType field to models |
| `app/models/model.py` | Add job_type column |
| `app/models/optimization_profile.py` | Add job_type column |
| `frontend/src/components/JobWizard.tsx` | 3-step wizard with job type |
| `frontend/src/components/PredictionTargetsPanel.tsx` | Nested tabs structure |
| `frontend/src/pages/Training.tsx` | Job type column and filter |
| `frontend/src/types/targets.ts` | Add jobType to TargetConfig |

## API Changes

### JobCreate Model
```python
class JobCreate(BaseModel):
    jobType: str = 'classification'  # 'classification' or 'regression'
    # ... existing fields
```

### ProfileCreate/ProfileResponse
```python
class OptimizationProfileCreate(BaseModel):
    jobType: str = 'classification'
    # ... existing fields
```

## Testing Plan

1. Unit tests for IModelService/ITrainingService implementations
2. Integration test: classification job with InceptionTime
3. Integration test: regression job with N-BEATS
4. Frontend test: wizard flow for both job types
5. Migration test: existing data gets default job_type='classification'

## Rollout Plan

1. Create interfaces and tsai implementation
2. Refactor Darts code to implement interfaces
3. Update job_handler with factory routing
4. Database migrations
5. Frontend wizard redesign
6. PredictionTargetsPanel nested tabs
7. Job list and inventory updates
8. Documentation and testing
