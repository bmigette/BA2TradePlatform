# Prediction Mode: Shift vs Multi-Step Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add support for two prediction modes (shift and multi-step) that can be selected in Job Wizard and genetically optimized, with proper handling of imbalanced data.

**Architecture:**
- **Shift mode**: Target shifted by N bars, model predicts single value at T+N (binary classification, c_out=2)
- **Multi-step mode**: Model predicts T+1, T+2, ..., T+N simultaneously (multi-label classification, c_out=N with sigmoid)
- Loss function (Focal/CrossEntropy) stays user-selected, not genetically optimized (depends on data imbalance, not model quality)
- Activation functions removed from UI (fixed by architecture for all models except TST which uses GELU default)

**Tech Stack:** Python, tsai/PyTorch, React/TypeScript, SQLite

---

## Task 1: Add prediction_mode to Database Schema

**Files:**
- Create: `backend/db_migrate/006_add_model_prediction_mode.py`
- Modify: `backend/app/models/model.py:39` (add prediction_mode column)

**Step 1: Create migration script**

```python
# backend/db_migrate/006_add_model_prediction_mode.py
"""
Migration 006: Add prediction_mode column to trained_models table

Stores the prediction mode used during training:
- "shift": Target shifted by prediction_horizon, single output (c_out=2)
- "multistep": Multi-label output for T+1 to T+prediction_horizon (c_out=N)
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add prediction_mode column to trained_models table."""
    columns = get_table_columns(cursor, "trained_models")
    if "prediction_mode" not in columns:
        cursor.execute("ALTER TABLE trained_models ADD COLUMN prediction_mode TEXT DEFAULT 'shift'")
        conn.commit()
        print("  - Added prediction_mode column to trained_models table")
        return True
    else:
        print("  - prediction_mode column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
```

**Step 2: Update TrainedModel model**

In `backend/app/models/model.py`, add after line 39 (`prediction_horizon`):

```python
    prediction_mode = Column(String(20), default="shift")  # "shift" or "multistep"
```

And update `to_dict()` to include:
```python
            "predictionMode": self.prediction_mode,
```

**Step 3: Run migration**

```bash
cd backend && ./venv/bin/python -c "from db_migrate import run_migrations; run_migrations.main()"
```

**Step 4: Commit**

```bash
git add backend/db_migrate/006_add_model_prediction_mode.py backend/app/models/model.py
git commit -m "feat(db): add prediction_mode column to trained_models"
```

---

## Task 2: Update tsai_training.py for Multi-Step Mode

**Files:**
- Modify: `backend/app/services/tsai_training.py`
- Test: `backend/tests/test_tsai_training.py`

**Step 1: Add multi-step sequence creation method**

Add after `_create_sequences`:

```python
    def _create_sequences_multistep(
        self,
        X: np.ndarray,
        y: np.ndarray,
        seq_len: int,
        prediction_horizon: int
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Create sequences with multi-step targets (T+1, T+2, ..., T+N).

        Args:
            X: Feature array of shape (n_rows, n_features)
            y: Target array of shape (n_rows,)
            seq_len: Length of input sequence (lookback window)
            prediction_horizon: Number of future steps to predict

        Returns:
            X_seq: Sequences of shape (n_samples, n_features, seq_len)
            y_seq: Multi-step targets of shape (n_samples, prediction_horizon)

        Example with seq_len=24, prediction_horizon=3:
            Input: Bars T-23 to T (24 bars)
            Targets: [y[T+1], y[T+2], y[T+3]] - class labels at each future step
        """
        n_samples = len(X) - seq_len - prediction_horizon + 1

        if n_samples <= 0:
            raise ValueError(
                f"Not enough data: need at least {seq_len + prediction_horizon} rows, got {len(X)}"
            )

        n_features = X.shape[1]
        X_seq = np.zeros((n_samples, n_features, seq_len), dtype=np.float32)
        y_seq = np.zeros((n_samples, prediction_horizon), dtype=np.float32)

        for i in range(n_samples):
            X_seq[i] = X[i:i+seq_len].T
            for h in range(prediction_horizon):
                y_seq[i, h] = y[i + seq_len + h]

        return X_seq, y_seq
```

**Step 2: Update prepare_data to support both modes**

Add `prediction_mode` parameter:

```python
    def prepare_data(
        self,
        df: pd.DataFrame,
        target_column: str,
        feature_columns: List[str],
        timeframe: str = 'daily',
        seq_len: int = 24,
        prediction_horizon: int = 0,
        prediction_mode: str = 'shift',
        fit_scaler: bool = True
    ) -> Tuple[np.ndarray, np.ndarray]:
```

Update sequence creation:

```python
        if prediction_mode == 'multistep':
            if prediction_horizon < 1:
                raise ValueError("Multi-step mode requires prediction_horizon >= 1")
            X, y = self._create_sequences_multistep(X_data, y_data, seq_len, prediction_horizon)
        else:
            X, y = self._create_sequences(X_data, y_data, seq_len, prediction_horizon)
```

**Step 3: Update prepare_data_split similarly**

Add `prediction_mode` parameter and pass through.

**Step 4: Write unit tests**

```python
    def test_sequence_creation_multistep(self, training_service):
        """Test multi-step sequence creation."""
        X = np.random.randn(100, 5).astype(np.float32)
        y = np.random.randint(0, 2, 100).astype(np.int64)

        X_seq, y_seq = training_service._create_sequences_multistep(X, y, seq_len=10, prediction_horizon=3)

        assert X_seq.shape == (88, 5, 10)  # 100 - 10 - 3 + 1 = 88
        assert y_seq.shape == (88, 3)

    def test_sequence_creation_multistep_horizon_1(self, training_service):
        """Test multi-step with horizon=1."""
        X = np.random.randn(100, 5).astype(np.float32)
        y = np.random.randint(0, 2, 100).astype(np.int64)

        X_seq, y_seq = training_service._create_sequences_multistep(X, y, seq_len=10, prediction_horizon=1)

        assert X_seq.shape == (90, 5, 10)  # 100 - 10 - 1 + 1 = 90
        assert y_seq.shape == (90, 1)
```

**Step 5: Commit**

```bash
git add backend/app/services/tsai_training.py backend/tests/test_tsai_training.py
git commit -m "feat(tsai): add multi-step sequence creation"
```

---

## Task 3: Update Loss Functions for Multi-Step

**Files:**
- Modify: `backend/app/services/tsai_training.py`

**Step 1: Update get_loss_function**

```python
    def get_loss_function(
        self,
        loss_type: str = 'focal',
        prediction_mode: str = 'shift',
        gamma: float = 2.0,
        pos_weight: float = None
    ) -> Any:
        """
        Get loss function for classification.

        Note: Loss function choice depends on data imbalance, not model quality.
        - Focal Loss: Best for imbalanced data (rare positive class)
        - CrossEntropy: Standard choice for balanced data
        - Weighted BCE/CE: Manual class weighting
        """
        if not TSAI_AVAILABLE:
            raise RuntimeError("tsai library not available")

        if prediction_mode == 'multistep':
            if loss_type == 'focal':
                return FocalLossFlat(gamma=gamma)
            elif loss_type == 'weighted_ce' and pos_weight is not None:
                weights = torch.tensor([pos_weight], dtype=torch.float32)
                if DEVICE:
                    weights = weights.to(DEVICE)
                return nn.BCEWithLogitsLoss(pos_weight=weights)
            else:
                return nn.BCEWithLogitsLoss()
        else:
            if loss_type == 'focal':
                return FocalLossFlat(gamma=gamma)
            elif loss_type == 'ce':
                return CrossEntropyLossFlat()
            elif loss_type == 'weighted_ce' and pos_weight is not None:
                weights = torch.tensor([1.0, pos_weight], dtype=torch.float32)
                if DEVICE:
                    weights = weights.to(DEVICE)
                return CrossEntropyLossFlat(weight=weights)
            else:
                return CrossEntropyLossFlat()
```

**Step 2: Update train_model signature**

Add `prediction_mode` parameter.

**Step 3: Commit**

```bash
git add backend/app/services/tsai_training.py
git commit -m "feat(tsai): update loss functions for multi-step mode"
```

---

## Task 4: Update assess_model and predict

**Files:**
- Modify: `backend/app/services/tsai_training.py`

**Step 1: Update assess_model**

```python
    def assess_model(
        self,
        model: Any,
        test_data: Tuple[np.ndarray, np.ndarray],
        metric: str = 'f1_score',
        threshold: float = 0.5,
        prediction_mode: str = 'shift',
        **kwargs
    ) -> Dict[str, float]:
        # ... setup code ...

        if prediction_mode == 'multistep':
            probs = torch.sigmoid(outputs).cpu().numpy()
            y_pred = (probs > threshold).astype(int)
            y_true = np.array(y_test)

            metrics = {}
            n_horizons = probs.shape[1]

            for h in range(n_horizons):
                metrics[f'h{h+1}_f1'] = f1_score(y_true[:, h], y_pred[:, h], zero_division=0)
                metrics[f'h{h+1}_accuracy'] = accuracy_score(y_true[:, h], y_pred[:, h])

            # Average for genetic optimization
            metrics['f1_score'] = np.mean([metrics[f'h{i+1}_f1'] for i in range(n_horizons)])
            metrics['accuracy'] = np.mean([metrics[f'h{i+1}_accuracy'] for i in range(n_horizons)])
        else:
            # Existing shift mode logic
            probs = torch.softmax(outputs, dim=1)[:, 1].cpu().numpy()
            # ... rest of existing code ...
```

**Step 2: Update predict**

```python
    def predict(self, model, data, prediction_mode='shift', **kwargs):
        # ...
        if prediction_mode == 'multistep':
            probs = torch.sigmoid(outputs).cpu().numpy()
        else:
            probs = torch.softmax(outputs, dim=1)[:, 1].cpu().numpy()
        return probs
```

**Step 3: Commit**

```bash
git add backend/app/services/tsai_training.py
git commit -m "feat(tsai): update assess and predict for multi-step"
```

---

## Task 5: Comprehensive Model Tests

**Files:**
- Modify: `backend/tests/test_tsai_training.py`

**Step 1: Parametrized tests for shift mode**

```python
@pytest.mark.slow
@pytest.mark.parametrize("model_type,horizon", [
    ('lstm', 1), ('lstm', 3),
    ('gru', 1), ('gru', 3),
    ('tcn', 1), ('tcn', 3),
    ('inception', 1), ('inception', 3),
    ('resnet', 1), ('resnet', 3),
    ('xception', 1), ('xception', 3),
    ('omniscale', 1), ('omniscale', 3),
    ('minirocket', 1), ('minirocket', 3),
    ('lstm_fcn', 1), ('lstm_fcn', 3),
    ('tst', 1), ('tst', 3),
])
def test_train_all_models_shift_mode(self, model_service, training_service, model_type, horizon):
    """Test all models with shift mode at horizon 1 and 3."""
    df = pd.read_csv(TEST_DATA_PATH).head(800)
    feature_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)
    df = df.dropna(subset=feature_cols + ['target'])

    X_train, X_test, y_train, y_test = training_service.prepare_data_split(
        df, train_ratio=0.8, target_column='target', feature_columns=feature_cols,
        seq_len=24, prediction_horizon=horizon, prediction_mode='shift'
    )

    assert len(y_train.shape) == 1  # Shift: 1D

    model = model_service.create_model(model_type, {}, c_in=X_train.shape[1], c_out=2, seq_len=X_train.shape[2])
    result = training_service.train_model(model, (X_train, y_train), val_data=(X_test, y_test), epochs=2, prediction_mode='shift')
    assert result['status'] == 'success'
```

**Step 2: Parametrized tests for multi-step mode**

```python
@pytest.mark.slow
@pytest.mark.parametrize("model_type,horizon", [
    ('lstm', 1), ('lstm', 3),
    ('gru', 1), ('gru', 3),
    ('tcn', 1), ('tcn', 3),
    ('inception', 1), ('inception', 3),
    ('resnet', 1), ('resnet', 3),
    ('xception', 1), ('xception', 3),
    ('omniscale', 1), ('omniscale', 3),
    ('minirocket', 1), ('minirocket', 3),
    ('lstm_fcn', 1), ('lstm_fcn', 3),
    ('tst', 1), ('tst', 3),
])
def test_train_all_models_multistep_mode(self, model_service, training_service, model_type, horizon):
    """Test all models with multi-step mode at horizon 1 and 3."""
    df = pd.read_csv(TEST_DATA_PATH).head(800)
    feature_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)
    df = df.dropna(subset=feature_cols + ['target'])

    X_train, X_test, y_train, y_test = training_service.prepare_data_split(
        df, train_ratio=0.8, target_column='target', feature_columns=feature_cols,
        seq_len=24, prediction_horizon=horizon, prediction_mode='multistep'
    )

    assert len(y_train.shape) == 2  # Multi-step: 2D
    assert y_train.shape[1] == horizon

    model = model_service.create_model(model_type, {}, c_in=X_train.shape[1], c_out=horizon, seq_len=X_train.shape[2])
    result = training_service.train_model(model, (X_train, y_train), val_data=(X_test, y_test), epochs=2, prediction_mode='multistep')
    assert result['status'] == 'success'

    if result['status'] == 'success':
        metrics = training_service.assess_model(result['model'], (X_test, y_test), prediction_mode='multistep', learner=result.get('learner'))
        assert 'h1_f1' in metrics
        assert 'f1_score' in metrics
```

**Step 3: Commit**

```bash
git add backend/tests/test_tsai_training.py
git commit -m "test: comprehensive tests for shift/multistep with all models"
```

---

## Task 6: Update Job Wizard Frontend

**Files:**
- Modify: `frontend/src/components/JobWizard.tsx`

**Step 1: Add predictionModes to state**

```typescript
const getDefaultState = () => ({
  jobType: 'classification' as 'classification' | 'regression',
  predictionModes: ['shift'] as ('shift' | 'multistep')[],
  // ... rest (remove activationFunctions from parameterRanges)
});
```

**Step 2: Add Prediction Mode Selector UI**

```tsx
{state.jobType === 'classification' && (
  <div>
    <div className="flex items-center space-x-2 mb-3">
      <Layers size={16} className="text-gray-400" />
      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
        Prediction Mode
      </label>
    </div>
    <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
      How the model makes predictions. Select both to let genetic algorithm optimize.
    </p>
    <div className="space-y-2">
      <label className={`flex items-start space-x-3 p-3 rounded-lg border cursor-pointer ${
        state.predictionModes.includes('shift') ? 'border-green-500 bg-green-50 dark:bg-green-900/20' : 'border-gray-200 dark:border-gray-600'
      }`}>
        <input type="checkbox" checked={state.predictionModes.includes('shift')}
          onChange={() => togglePredictionMode('shift')} className="mt-1 w-4 h-4" />
        <div>
          <div className="font-medium text-sm">Shift Mode</div>
          <div className="text-xs text-gray-500 mt-1">
            <strong>Single point prediction.</strong> Input: bars T-23 to T. Output: class at T+{state.predictionHorizon}.
            Best when you only need one future prediction.
          </div>
        </div>
      </label>
      <label className={`flex items-start space-x-3 p-3 rounded-lg border cursor-pointer ${
        state.predictionModes.includes('multistep') ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20' : 'border-gray-200 dark:border-gray-600'
      }`}>
        <input type="checkbox" checked={state.predictionModes.includes('multistep')}
          onChange={() => togglePredictionMode('multistep')} className="mt-1 w-4 h-4" />
        <div>
          <div className="font-medium text-sm">Multi-Step Mode</div>
          <div className="text-xs text-gray-500 mt-1">
            <strong>Multiple point predictions.</strong> Input: bars T-23 to T. Output: classes at T+1, T+2, ..., T+{state.predictionHorizon}.
            Captures temporal dependencies.
          </div>
        </div>
      </label>
    </div>
    {state.predictionModes.length === 2 && (
      <p className="text-xs text-blue-600 mt-2">Both selected: genetic algorithm will find best mode.</p>
    )}
  </div>
)}
```

**Step 3: Add toggle handler and validation**

```typescript
const togglePredictionMode = (mode: 'shift' | 'multistep') => {
  setState(prev => {
    const modes = prev.predictionModes.includes(mode)
      ? prev.predictionModes.filter(m => m !== mode)
      : [...prev.predictionModes, mode];
    return { ...prev, predictionModes: modes.length > 0 ? modes : [mode] };
  });
};
```

**Step 4: Update submitJob and profile save/load**

Pass `predictionModes` in API call and save/load with profiles.

**Step 5: Commit**

```bash
git add frontend/src/components/JobWizard.tsx
git commit -m "feat(wizard): add prediction mode selector"
```

---

## Task 7: Update Job Handler

**Files:**
- Modify: `backend/app/services/job_handler.py`

**Step 1: Extract and handle predictionModes**

```python
prediction_modes = payload.get('prediction_modes', ['shift'])

# Prepare data for each mode
classification_data_by_mode = {}
for mode in prediction_modes:
    X_train, X_test, y_train, y_test = tsai_training.prepare_data_split(
        df, train_ratio, target_column, feature_columns,
        seq_len=24, prediction_horizon=prediction_horizon, prediction_mode=mode
    )
    classification_data_by_mode[mode] = {
        'X_train': X_train, 'X_test': X_test, 'y_train': y_train, 'y_test': y_test,
        'c_out': 2 if mode == 'shift' else prediction_horizon
    }
```

**Step 2: Add to genetic search if both selected**

```python
if len(prediction_modes) > 1:
    genes['prediction_mode_idx'] = {'type': 'int', 'min': 0, 'max': len(prediction_modes) - 1}
```

**Step 3: Store with model**

```python
model_record.prediction_mode = prediction_mode
```

**Step 4: Commit**

```bash
git add backend/app/services/job_handler.py
git commit -m "feat(jobs): support prediction modes in genetic optimization"
```

---

## Task 8: Final Testing

**Step 1: Run tests**

```bash
cd backend && ./venv/bin/python -m pytest tests/ -v
```

**Step 2: Lint and type check**

```bash
cd frontend && npm run lint && npx tsc --noEmit
```

**Step 3: End-to-end test**

1. Open wizard, select Classification
2. Verify mode selector appears
3. Test with shift only, multistep only, and both
4. Verify job runs and models saved correctly

**Step 4: Final commit**

```bash
git add -A && git commit -m "feat: complete prediction mode implementation" && git push
```

---

## Summary

| Task | Description |
|------|-------------|
| 1 | Database migration for prediction_mode |
| 2 | Multi-step sequence creation |
| 3 | Loss functions for multi-step |
| 4 | assess_model and predict updates |
| 5 | 40 test cases (10 models x 2 horizons x 2 modes) |
| 6 | Job Wizard UI with explanations |
| 7 | Job handler with genetic optimization |
| 8 | Final integration testing |

**Loss Function Note:**
- Focal Loss: Use for imbalanced data (ZigZag, rare signals)
- CrossEntropy: Use for balanced data
- The loss function is NOT genetically optimized - it's a user choice based on data characteristics, not model quality
