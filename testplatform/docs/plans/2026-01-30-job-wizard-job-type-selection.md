# Job Wizard Job Type Selection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Classification/Regression job type toggle to Job Wizard that dynamically filters available models.

**Architecture:** Add a jobType state field ('classification' | 'regression') at the top of the wizard. Fetch models from the appropriate API endpoint based on job type. Filter UI elements (metrics, loss functions) based on job type. Pass jobType to the job creation API.

**Tech Stack:** React, TypeScript, Tailwind CSS

---

## Task 1: Add Job Type State and Toggle UI

**Files:**
- Modify: `frontend/src/components/JobWizard.tsx:141-183` (getDefaultState and state type)

**Step 1: Add jobType to default state**

In `getDefaultState()`, add at the very beginning:

```typescript
const getDefaultState = () => ({
  jobType: 'classification' as 'classification' | 'regression',  // NEW
  selectedDatasetId: null as number | null,
  // ... rest unchanged
});
```

**Step 2: Add Job Type Toggle UI**

After the "Profile buttons" div (line ~695) and before "Dataset Selection", add:

```tsx
{/* Job Type Selection */}
<div>
  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
    Job Type
  </label>
  <div className="flex space-x-4">
    <label
      className={`flex-1 flex items-center justify-center space-x-2 p-4 rounded-lg border-2 cursor-pointer transition-colors ${
        state.jobType === 'classification'
          ? 'border-green-500 bg-green-50 dark:bg-green-900/20'
          : 'border-gray-200 dark:border-gray-600 hover:border-gray-300'
      }`}
    >
      <input
        type="radio"
        name="jobType"
        value="classification"
        checked={state.jobType === 'classification'}
        onChange={() => setState(prev => ({ ...prev, jobType: 'classification', selectedModels: [] }))}
        className="sr-only"
      />
      <Target size={20} className={state.jobType === 'classification' ? 'text-green-600' : 'text-gray-400'} />
      <div>
        <div className="font-medium">Classification</div>
        <div className="text-xs text-gray-500">Binary prediction (up/down, signal/no-signal)</div>
      </div>
    </label>
    <label
      className={`flex-1 flex items-center justify-center space-x-2 p-4 rounded-lg border-2 cursor-pointer transition-colors ${
        state.jobType === 'regression'
          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
          : 'border-gray-200 dark:border-gray-600 hover:border-gray-300'
      }`}
    >
      <input
        type="radio"
        name="jobType"
        value="regression"
        checked={state.jobType === 'regression'}
        onChange={() => setState(prev => ({ ...prev, jobType: 'regression', selectedModels: [] }))}
        className="sr-only"
      />
      <Activity size={20} className={state.jobType === 'regression' ? 'text-blue-600' : 'text-gray-400'} />
      <div>
        <div className="font-medium">Regression</div>
        <div className="text-xs text-gray-500">Continuous value prediction (price, volatility)</div>
      </div>
    </label>
  </div>
</div>
```

**Step 3: Test manually**

Run: `cd frontend && npm run dev`
Expected: See job type toggle at top of wizard, clicking switches between types

**Step 4: Commit**

```bash
git add frontend/src/components/JobWizard.tsx
git commit -m "feat(wizard): add job type selection toggle (classification/regression)"
```

---

## Task 2: Fetch Models Dynamically Based on Job Type

**Files:**
- Modify: `frontend/src/components/JobWizard.tsx`

**Step 1: Remove hardcoded MODEL_TYPES constant**

Delete lines 103-110 (the hardcoded MODEL_TYPES array).

**Step 2: Add model fetching state and effect**

Add near other useState hooks (around line 194):

```typescript
const [availableModels, setAvailableModels] = useState<Array<{id: string, name: string, description: string}>>([]);
const [modelsLoading, setModelsLoading] = useState(false);
```

**Step 3: Add fetchModels function**

Add after fetchTargetSets function:

```typescript
const fetchModels = useCallback(async (jobType: 'classification' | 'regression') => {
  setModelsLoading(true);
  try {
    const endpoint = jobType === 'classification'
      ? 'http://localhost:8000/api/ml/classification-models'
      : 'http://localhost:8000/api/ml/models';
    const response = await fetch(endpoint);
    if (response.ok) {
      const data = await response.json();
      // Transform to array format
      const models = Object.entries(data.models || {}).map(([id, info]: [string, any]) => ({
        id,
        name: info.name || id.toUpperCase(),
        description: info.description || '',
      }));
      setAvailableModels(models);
    }
  } catch (error) {
    console.error('Failed to fetch models:', error);
  } finally {
    setModelsLoading(false);
  }
}, []);
```

**Step 4: Call fetchModels on jobType change**

Add useEffect after the isOpen effect:

```typescript
useEffect(() => {
  if (isOpen) {
    fetchModels(state.jobType);
  }
}, [isOpen, state.jobType, fetchModels]);
```

**Step 5: Update model toggle handlers**

Replace references to `MODEL_TYPES` with `availableModels`:

```typescript
const handleAllModelsToggle = () => {
  setState(prev => ({
    ...prev,
    selectedModels: prev.selectedModels.length === availableModels.length
      ? []
      : availableModels.map(m => m.id)
  }));
};
```

**Step 6: Pass availableModels to Step1Settings**

Add to Step1Settings props and usage:
- Add `availableModels` and `modelsLoading` to Step1Props interface
- Pass them in the JSX: `availableModels={availableModels} modelsLoading={modelsLoading}`
- Update Step1Settings to use `availableModels` instead of `MODEL_TYPES`

**Step 7: Update allModelsSelected calculation in Step1Settings**

```typescript
const allModelsSelected = state.selectedModels.length === availableModels.length && availableModels.length > 0;
```

**Step 8: Add loading state to model grid**

```tsx
{modelsLoading ? (
  <div className="flex items-center justify-center py-8">
    <Loader2 className="animate-spin text-gray-400" size={24} />
    <span className="ml-2 text-gray-500">Loading models...</span>
  </div>
) : (
  <div className="grid grid-cols-3 gap-3">
    {availableModels.map((model) => (
      // ... existing model card JSX
    ))}
  </div>
)}
```

**Step 9: Test manually**

Run: `cd frontend && npm run dev`
Expected:
- Classification shows 10 models (LSTM, GRU, TCN, InceptionTime, etc.)
- Regression shows 6 models (LSTM, GRU, N-BEATS, TCN, Transformer, TFT)
- Switching job type clears selected models and loads new list

**Step 10: Commit**

```bash
git add frontend/src/components/JobWizard.tsx
git commit -m "feat(wizard): fetch models dynamically based on job type"
```

---

## Task 3: Filter Metrics and Loss Functions by Job Type

**Files:**
- Modify: `frontend/src/components/JobWizard.tsx`

**Step 1: Update Optimization Metrics section**

In Step1Settings, update the metrics section to show only relevant metrics:

```tsx
{/* Optimization Metrics */}
<div>
  <div className="flex items-center space-x-2 mb-3">
    <Zap size={16} className="text-gray-400" />
    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
      Optimization Metric
    </label>
  </div>

  {state.jobType === 'classification' ? (
    <div className="flex flex-wrap gap-2">
      {CLASSIFICATION_METRICS.map((metric) => (
        <label
          key={metric.id}
          className={`flex items-center space-x-2 px-3 py-1.5 rounded-full border cursor-pointer text-sm ${
            state.metricsConfig.classificationMetric === metric.id
              ? 'border-green-500 bg-green-50 dark:bg-green-900/20 text-green-700'
              : 'border-gray-300 dark:border-gray-600 hover:border-gray-400'
          }`}
          title={metric.description}
        >
          <input
            type="radio"
            name="optimizeMetric"
            checked={state.metricsConfig.classificationMetric === metric.id}
            onChange={() => setState(prev => ({
              ...prev,
              metricsConfig: { ...prev.metricsConfig, classificationMetric: metric.id, optimizeMetric: metric.id }
            }))}
            className="sr-only"
          />
          <span>{metric.name}</span>
        </label>
      ))}
    </div>
  ) : (
    <div className="flex flex-wrap gap-2">
      {REGRESSION_METRICS.map((metric) => (
        <label
          key={metric.id}
          className={`flex items-center space-x-2 px-3 py-1.5 rounded-full border cursor-pointer text-sm ${
            state.metricsConfig.regressionMetric === metric.id
              ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 text-blue-700'
              : 'border-gray-300 dark:border-gray-600 hover:border-gray-400'
          }`}
          title={metric.description}
        >
          <input
            type="radio"
            name="optimizeMetric"
            checked={state.metricsConfig.regressionMetric === metric.id}
            onChange={() => setState(prev => ({
              ...prev,
              metricsConfig: { ...prev.metricsConfig, regressionMetric: metric.id, optimizeMetric: metric.id }
            }))}
            className="sr-only"
          />
          <span>{metric.name}</span>
        </label>
      ))}
    </div>
  )}
</div>
```

**Step 2: Update Loss Function section to show only for classification**

```tsx
{/* Loss Function - Only for Classification */}
{state.jobType === 'classification' && (
  <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-600">
    <label className="block text-xs text-gray-500 dark:text-gray-400 mb-2">Training Loss Function</label>
    <div className="flex flex-wrap gap-2">
      {LOSS_FUNCTIONS.filter(l => l.id !== 'mse').map((loss) => (
        // ... existing loss function buttons
      ))}
    </div>
  </div>
)}
```

**Step 3: Test manually**

Expected:
- Classification shows F1/Accuracy/etc metrics + Loss Function selector
- Regression shows MSE/RMSE/MAE/etc metrics, no loss function

**Step 4: Commit**

```bash
git add frontend/src/components/JobWizard.tsx
git commit -m "feat(wizard): filter metrics and loss functions by job type"
```

---

## Task 4: Pass jobType to API and Update Summary

**Files:**
- Modify: `frontend/src/components/JobWizard.tsx`

**Step 1: Update submitJob to include jobType**

In the submitJob function, add jobType to the request body:

```typescript
body: JSON.stringify({
  jobType: state.jobType,  // NEW
  datasetId: state.selectedDatasetId,
  selectedModels: state.selectedModels,
  // ... rest unchanged
}),
```

**Step 2: Update Step2Summary to show job type**

Add job type badge in the summary:

```tsx
{/* Job Type */}
<div className="flex items-center space-x-2 mb-4">
  <span className={`px-3 py-1 rounded-full text-sm font-medium ${
    state.jobType === 'classification'
      ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300'
      : 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300'
  }`}>
    {state.jobType === 'classification' ? 'Classification' : 'Regression'}
  </span>
</div>
```

**Step 3: Update profile save/load to include jobType**

In saveProfile:
```typescript
await onSaveProfile(newProfileName.trim(), {
  jobType: state.jobType,  // NEW
  selectedModels: state.selectedModels,
  // ... rest unchanged
});
```

In loadProfile:
```typescript
const loadProfile = (profile: JobProfile) => {
  const jobType = profile.jobType || 'classification';  // Default for old profiles
  setState(prev => ({
    ...prev,
    jobType,
    // ... rest unchanged
  }));
  // Fetch models for the profile's job type
  fetchModels(jobType);
  setShowLoadProfileDialog(false);
};
```

**Step 4: Update JobProfile interface**

```typescript
interface JobProfile {
  id: number;
  name: string;
  jobType?: 'classification' | 'regression';  // NEW
  // ... rest unchanged
}
```

**Step 5: Test end-to-end**

1. Create classification job - verify API receives jobType: 'classification'
2. Create regression job - verify API receives jobType: 'regression'
3. Save/load profiles - verify job type is preserved

**Step 6: Commit**

```bash
git add frontend/src/components/JobWizard.tsx
git commit -m "feat(wizard): pass jobType to API and update summary/profiles"
```

---

## Task 5: Update Prediction Horizon Description

**Files:**
- Modify: `frontend/src/components/JobWizard.tsx`

**Step 1: Update prediction horizon section to be job-type aware**

```tsx
{/* Model-specific behavior explanation */}
<div className="border-t border-gray-200 dark:border-gray-600 pt-3">
  <div className="text-xs text-gray-500 dark:text-gray-400 mb-2 font-medium">
    How {state.jobType} models handle prediction horizon:
  </div>
  {state.jobType === 'classification' ? (
    <div className="bg-white dark:bg-gray-800 rounded p-2 border border-gray-200 dark:border-gray-600 text-xs">
      <div className="font-medium text-green-600 dark:text-green-400 mb-1">Classification Models (tsai)</div>
      <p className="text-gray-600 dark:text-gray-300">
        Target shifted by {state.predictionHorizon} bars. Model predicts <strong>probability of positive class</strong> at bar +{state.predictionHorizon}.
      </p>
    </div>
  ) : (
    <div className="grid grid-cols-2 gap-3 text-xs">
      <div className="bg-white dark:bg-gray-800 rounded p-2 border border-gray-200 dark:border-gray-600">
        <div className="font-medium text-purple-600 dark:text-purple-400 mb-1">LSTM / GRU</div>
        <p className="text-gray-600 dark:text-gray-300">
          Target shifted by {state.predictionHorizon} bars. Predicts <strong>single value</strong> at bar +{state.predictionHorizon}.
        </p>
      </div>
      <div className="bg-white dark:bg-gray-800 rounded p-2 border border-gray-200 dark:border-gray-600">
        <div className="font-medium text-blue-600 dark:text-blue-400 mb-1">N-BEATS / TCN / Transformer / TFT</div>
        <p className="text-gray-600 dark:text-gray-300">
          Multi-step output. Predicts <strong>{state.predictionHorizon} values</strong> (bars +1 to +{state.predictionHorizon}) in one pass.
        </p>
      </div>
    </div>
  )}
</div>
```

**Step 2: Commit**

```bash
git add frontend/src/components/JobWizard.tsx
git commit -m "feat(wizard): update prediction horizon description for job types"
```

---

## Task 6: Final Testing and Cleanup

**Step 1: Run linter**

```bash
cd frontend && npm run lint
```

Fix any errors.

**Step 2: Test complete flow**

1. Open wizard → Verify classification is default
2. Switch to regression → Models change, metrics change
3. Switch back → Models reset to classification list
4. Select models, targets, configure settings
5. Click Next → Summary shows correct job type badge
6. Submit → Verify job created with correct jobType

**Step 3: Final commit**

```bash
git add -A
git commit -m "feat(wizard): complete job type selection implementation"
git push
```

---

## Summary

| Task | Description | Est. Time |
|------|-------------|-----------|
| 1 | Add jobType state and toggle UI | 10 min |
| 2 | Fetch models dynamically | 15 min |
| 3 | Filter metrics by job type | 10 min |
| 4 | Pass jobType to API | 10 min |
| 5 | Update prediction horizon | 5 min |
| 6 | Testing and cleanup | 10 min |

**Total: ~60 minutes**
