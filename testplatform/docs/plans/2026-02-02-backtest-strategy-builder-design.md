# Backtest Strategy Builder Design

## Overview

Comprehensive backtesting system with strategy builder, genetic optimization, and multi-timeframe support. Replaces demo/mock backtest implementation with real functionality.

## Key Features

1. **Dual dataset support** - Separate datasets for model prediction and trade execution
2. **Strategy builder** - Visual condition builder with AND/OR logic
3. **Genetic optimization** - Optimize strategy parameters and condition enabled states
4. **Database storage** - All strategies and backtests persisted in database

---

## Database Schema

### New Tables

```sql
-- Strategy definitions with conditions and optimization ranges
CREATE TABLE strategies (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    required_fields JSON,  -- Auto-computed from conditions for compatibility matching
    entry_conditions JSON NOT NULL,
    exit_conditions JSON NOT NULL,
    initial_tp_percent FLOAT DEFAULT 5.0,
    initial_tp_optimize BOOLEAN DEFAULT FALSE,
    initial_tp_min FLOAT,
    initial_tp_max FLOAT,
    initial_tp_step FLOAT,
    initial_sl_percent FLOAT DEFAULT 2.0,
    initial_sl_optimize BOOLEAN DEFAULT FALSE,
    initial_sl_min FLOAT,
    initial_sl_max FLOAT,
    initial_sl_step FLOAT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
);

-- Strategy optimization runs
CREATE TABLE strategy_optimizations (
    id INTEGER PRIMARY KEY,
    strategy_id INTEGER REFERENCES strategies(id),
    backtest_id INTEGER REFERENCES backtests(id),
    name VARCHAR(255),
    status VARCHAR(50) DEFAULT 'pending',  -- pending/running/completed/failed
    fitness_metric VARCHAR(50) NOT NULL,  -- sharpe/return/profit_factor/win_rate/max_drawdown
    optimization_type VARCHAR(50) NOT NULL,  -- genetic/brute_force
    optimization_config JSON,  -- GA params: population, generations, etc.
    parameter_ranges JSON,  -- Snapshot of ranges being optimized
    best_params JSON,  -- Result: optimal values found
    best_fitness FLOAT,
    all_results JSON,  -- All tested combinations with fitness
    progress FLOAT DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    completed_at DATETIME
);
```

### Updated Tables

```sql
-- Update backtests table
ALTER TABLE backtests ADD COLUMN prediction_dataset_id INTEGER REFERENCES datasets(id);
ALTER TABLE backtests ADD COLUMN execution_dataset_id INTEGER REFERENCES datasets(id);
ALTER TABLE backtests ADD COLUMN strategy_id INTEGER REFERENCES strategies(id);
ALTER TABLE backtests ADD COLUMN strategy_params JSON;  -- Specific param values used
ALTER TABLE backtests ADD COLUMN fitness_metric VARCHAR(50);
ALTER TABLE backtests ADD COLUMN trades JSON;  -- Detailed trade list
ALTER TABLE backtests ADD COLUMN equity_curve JSON;
ALTER TABLE backtests ADD COLUMN drawdown_curve JSON;
```

---

## Condition Structure

### Entry Conditions

```json
{
  "operator": "AND",
  "conditions": [
    {
      "id": "cond-1",
      "field": "price_up_10pct_5dd_7d",
      "field_type": "model_probability",
      "comparison": ">",
      "value": 0.7,
      "optimize": true,
      "value_min": 0.5,
      "value_max": 0.9,
      "value_step": 0.05,
      "optimize_enabled": true,
      "confirmation_required": 2,
      "confirmation_bars": 3,
      "confirmation_bars_min": 1,
      "confirmation_bars_max": 5,
      "confirmation_bars_step": 1
    },
    {
      "id": "cond-2",
      "operator": "OR",
      "conditions": [
        {
          "id": "cond-2a",
          "field": "hour_of_day",
          "field_type": "time",
          "comparison": "between",
          "value": [9, 11],
          "optimize": false
        }
      ]
    }
  ]
}
```

### Exit Conditions

```json
[
  {
    "id": "exit-1",
    "name": "Tighten stop on bearish signal",
    "conditions": {
      "field": "zigzag_bearish",
      "field_type": "model_probability",
      "comparison": ">",
      "value": 0.8,
      "optimize": true,
      "value_min": 0.6,
      "value_max": 0.9,
      "value_step": 0.05
    },
    "action": "adjust_sl",
    "action_value": 1.0,
    "action_value_optimize": true,
    "action_value_min": 0.5,
    "action_value_max": 2.0,
    "action_value_step": 0.25
  },
  {
    "id": "exit-2",
    "name": "Time-based exit",
    "conditions": {
      "field": "bars_in_trade",
      "field_type": "position",
      "comparison": ">",
      "value": 50,
      "optimize": true,
      "value_min": 20,
      "value_max": 100,
      "value_step": 10
    },
    "action": "close"
  },
  {
    "id": "exit-3",
    "name": "Move to breakeven on profit",
    "conditions": {
      "operator": "AND",
      "conditions": [
        {"field": "position_pnl_pct", "field_type": "position", "comparison": ">", "value": 2.0},
        {"field": "bars_in_trade", "field_type": "position", "comparison": ">", "value": 10}
      ]
    },
    "action": "adjust_sl",
    "action_value": 0
  }
]
```

### Field Types

| Type | Fields | Description |
|------|--------|-------------|
| `model_probability` | Dynamic from model | Prediction probability 0-1 |
| `model_class` | Dynamic from model | Predicted class (0, 1, 2...) |
| `position` | `bars_in_trade`, `days_in_trade`, `position_pnl_pct`, `position_pnl_abs` | Current trade state |
| `time` | `hour_of_day`, `day_of_week`, `day_of_month` | Market time |

### Comparison Operators

| Operator | Description |
|----------|-------------|
| `>` | Greater than |
| `>=` | Greater than or equal |
| `<` | Less than |
| `<=` | Less than or equal |
| `==` | Equal |
| `!=` | Not equal |
| `between` | Value is array [min, max] |

### Exit Actions

| Action | Description |
|--------|-------------|
| `close` | Close the position immediately |
| `adjust_tp` | Set take profit to action_value % |
| `adjust_sl` | Set stop loss to action_value % (0 = breakeven) |

---

## API Endpoints

### Strategy Endpoints

```
GET    /api/strategies
       List all strategies
       Query: ?search=name

POST   /api/strategies
       Create new strategy
       Body: {name, description, entry_conditions, exit_conditions, initial_tp_percent, initial_sl_percent, ...}

GET    /api/strategies/:id
       Get strategy details

PUT    /api/strategies/:id
       Update strategy

DELETE /api/strategies/:id
       Delete strategy

GET    /api/strategies/compatible/:modelId
       Get strategies compatible with model's prediction fields
       Returns strategies where required_fields is subset of model's prediction_targets
```

### Backtest Endpoints (Updated)

```
GET    /api/backtests
       List all backtests (from database, no demo data)

POST   /api/backtests
       Create and run single backtest
       Body: {
         name,
         predictionDatasetId,
         executionDatasetId,
         modelId,
         strategyId,
         strategyParams,  // Optional: override strategy values
         startDate,
         endDate,
         initialCapital,
         positionSizing,  // {type: "fixed"|"percent", value: number}
         commission,
         slippage
       }

GET    /api/backtests/:id
       Get backtest details with full results

DELETE /api/backtests/:id
       Delete backtest

POST   /api/backtests/optimize
       Run strategy optimization
       Body: {
         name,
         predictionDatasetId,
         executionDatasetId,
         modelId,
         strategyId,
         fitnessMetric,  // sharpe/return/profit_factor/win_rate/max_drawdown
         optimizationType,  // genetic/brute_force
         optimizationConfig,  // {populationSize, generations, ...} for genetic
         startDate,
         endDate,
         initialCapital,
         ...
       }

GET    /api/backtests/optimize/:id
       Get optimization progress and results

GET    /api/backtests/optimize/:id/progress
       SSE stream for live optimization progress
```

### Model Endpoints (Updated)

```
GET    /api/models
       Query params:
       - datasetId: Filter by training dataset
       - timeframe: Filter by dataset timeframe (show all models trained on datasets with this timeframe)
       - showAll: If true, ignore filters

GET    /api/models/:id/prediction-fields
       Returns array of prediction target fields for condition builder
       Response: ["price_up_10pct_5dd_7d", "price_down_10pct_5dd_7d", "zigzag_bullish", ...]
```

### Dataset Endpoints (Updated)

```
GET    /api/datasets
       Query params:
       - timeframe: Filter by timeframe (1m, 1h, etc.)
```

---

## UI Workflow

### Backtest Page Flow

**Step 1: Dataset Selection**
```
┌─────────────────────────────────────────────────────────────┐
│ Prediction Dataset                                          │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ AAPL_1h_2024-2025 ▼                                     │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ Execution Dataset                                           │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ AAPL_1m_2024-2025 ▼                                     │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ⚠ Datasets must have same ticker and overlapping dates     │
└─────────────────────────────────────────────────────────────┘
```

**Step 2: Model Selection**
```
┌─────────────────────────────────────────────────────────────┐
│ Select Model                                                │
│                                                             │
│ ☑ Show only models trained on AAPL_1h_2024-2025            │
│ ☐ Show all models for 1h timeframe                         │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ LSTM_AAPL_v2                                            │ │
│ │ Type: LSTM | F1: 0.72 | Trained: 2026-01-28            │ │
│ │ Targets: price_up_10pct, price_down_10pct, zigzag_*    │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Inception_AAPL                                          │ │
│ │ ...                                                     │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**Step 3: Strategy Selection/Creation**
```
┌─────────────────────────────────────────────────────────────┐
│ Strategy                                                    │
│                                                             │
│ Compatible strategies (3):                                  │
│ ○ Momentum Breakout (uses: price_up_10pct, zigzag_*)       │
│ ○ Conservative Trend (uses: zigzag_bullish)                │
│ ● [Create New Strategy]                                     │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │                    Strategy Builder                      │ │
│ │ ─────────────────────────────────────────────────────── │ │
│ │ Entry Conditions:                            [+ Add]    │ │
│ │ ┌─────────────────────────────────────────────────────┐ │ │
│ │ │ AND                                                 │ │ │
│ │ │  ├─ price_up_10pct > [0.7] ☑ Optimize [0.5-0.9/0.05]│ │ │
│ │ │  └─ OR                                              │ │ │
│ │ │      ├─ hour_of_day between [9, 15]                │ │ │
│ │ │      └─ day_of_week != 0                           │ │ │
│ │ └─────────────────────────────────────────────────────┘ │ │
│ │                                                         │ │
│ │ Initial TP: [5.0]% ☑ Optimize [2-10/1]                 │ │
│ │ Initial SL: [2.0]% ☑ Optimize [1-5/0.5]                │ │
│ │                                                         │ │
│ │ Exit Conditions:                             [+ Add]    │ │
│ │ ┌─────────────────────────────────────────────────────┐ │ │
│ │ │ If zigzag_bearish > 0.8 → Adjust SL to 1%          │ │ │
│ │ │ If bars_in_trade > 50 → Close                       │ │ │
│ │ └─────────────────────────────────────────────────────┘ │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**Step 4: Backtest Configuration**
```
┌─────────────────────────────────────────────────────────────┐
│ Backtest Settings                                           │
│                                                             │
│ Date Range: [2025-01-01] to [2025-12-31]                   │
│ Initial Capital: [$10,000]                                  │
│ Position Sizing: ○ Fixed [$1000]  ● Percent [10%]          │
│ Commission: [0.1]%                                          │
│ Slippage: [0.05]%                                          │
│                                                             │
│ Fitness Metric (for optimization):                          │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Sharpe Ratio ▼                                          │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ [Run Single Backtest]  [Optimize Strategy]                  │
└─────────────────────────────────────────────────────────────┘
```

**Step 5: Results**
```
┌─────────────────────────────────────────────────────────────┐
│ Backtest Results: LSTM_AAPL Momentum Strategy               │
│                                                             │
│ ┌──────────────┬──────────────┬──────────────┬────────────┐ │
│ │ Total Return │ Sharpe Ratio │ Max Drawdown │ Win Rate   │ │
│ │    +24.5%    │     1.85     │    -8.2%     │   58.3%    │ │
│ └──────────────┴──────────────┴──────────────┴────────────┘ │
│                                                             │
│ [Equity Curve Chart]                                        │
│ ████████████████████████▄▄▄████████████████████████████    │
│                                                             │
│ [Drawdown Chart]                                            │
│ ▁▁▁▁▁▂▃▂▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁        │
│                                                             │
│ Trades (47):                                                │
│ ┌────────┬────────┬────────┬────────┬────────┬───────────┐ │
│ │ Entry  │ Exit   │ Side   │ P&L    │ P&L %  │ Duration  │ │
│ ├────────┼────────┼────────┼────────┼────────┼───────────┤ │
│ │ Jan 15 │ Jan 18 │ Long   │ +$245  │ +2.4%  │ 3 days    │ │
│ │ ...    │ ...    │ ...    │ ...    │ ...    │ ...       │ │
│ └────────┴────────┴────────┴────────┴────────┴───────────┘ │
│                                                             │
│ [Save Strategy]  [Export Results]                           │
└─────────────────────────────────────────────────────────────┘
```

---

## Backend Implementation

### Strategy Execution Engine

```python
class StrategyExecutor:
    """Executes strategy conditions against data."""

    def __init__(self, strategy: Strategy, model: TrainedModel):
        self.strategy = strategy
        self.model = model
        self.entry_conditions = parse_conditions(strategy.entry_conditions)
        self.exit_conditions = parse_conditions(strategy.exit_conditions)

    def check_entry(self, bar_data: dict, predictions: dict) -> bool:
        """Check if entry conditions are met."""
        context = {**bar_data, **predictions}
        return evaluate_condition_tree(self.entry_conditions, context)

    def check_exits(self, bar_data: dict, predictions: dict, position: Position) -> ExitAction:
        """Check exit conditions and return action if any triggered."""
        context = {
            **bar_data,
            **predictions,
            'bars_in_trade': position.bars_held,
            'days_in_trade': position.days_held,
            'position_pnl_pct': position.unrealized_pnl_pct,
            'position_pnl_abs': position.unrealized_pnl,
        }

        for exit_rule in self.exit_conditions:
            if evaluate_condition_tree(exit_rule.conditions, context):
                return ExitAction(
                    action=exit_rule.action,
                    value=exit_rule.action_value
                )

        return None
```

### Backtest Engine Integration

```python
from backtesting import Backtest, Strategy
from backtesting.lib import resample_apply

class MLStrategy(Strategy):
    """Strategy that uses ML model predictions."""

    # Parameters set by optimizer
    model_path = None
    strategy_config = None
    prediction_timeframe = '1H'

    def init(self):
        # Load model
        self.model = load_model(self.model_path)
        self.executor = StrategyExecutor(self.strategy_config, self.model)

        # Compute predictions on prediction timeframe
        self.predictions = resample_apply(
            self.prediction_timeframe,
            self._compute_predictions,
            self.data.df
        )

    def next(self):
        # Get current predictions
        preds = {field: self.predictions[field][-1] for field in self.model.prediction_fields}

        bar_data = {
            'hour_of_day': self.data.index[-1].hour,
            'day_of_week': self.data.index[-1].weekday(),
        }

        if not self.position:
            # Check entry
            if self.executor.check_entry(bar_data, preds):
                self.buy(
                    tp=self.data.Close[-1] * (1 + self.strategy_config.initial_tp_percent/100),
                    sl=self.data.Close[-1] * (1 - self.strategy_config.initial_sl_percent/100)
                )
        else:
            # Check exits
            action = self.executor.check_exits(bar_data, preds, self.position)
            if action:
                if action.action == 'close':
                    self.position.close()
                elif action.action == 'adjust_sl':
                    self.position.sl = self.data.Close[-1] * (1 - action.value/100)
                elif action.action == 'adjust_tp':
                    self.position.tp = self.data.Close[-1] * (1 + action.value/100)


def run_backtest(config: BacktestConfig) -> BacktestResult:
    """Run a single backtest with given configuration."""

    # Load execution dataset (1m or other granular data)
    execution_df = load_dataset(config.execution_dataset_id)

    # Run backtest
    bt = Backtest(
        execution_df,
        MLStrategy,
        cash=config.initial_capital,
        commission=config.commission / 100,
        trade_on_close=True
    )

    stats = bt.run(
        model_path=config.model.file_path,
        strategy_config=config.strategy_params,
        prediction_timeframe=config.prediction_dataset.timeframe
    )

    return BacktestResult(
        total_return=stats['Return [%]'],
        sharpe_ratio=stats['Sharpe Ratio'],
        max_drawdown=stats['Max. Drawdown [%]'],
        win_rate=stats['Win Rate [%]'],
        profit_factor=stats['Profit Factor'],
        trades=stats['_trades'].to_dict('records'),
        equity_curve=stats['_equity_curve'].to_dict('records')
    )
```

---

## Optimization Engine

### Genetic Algorithm for Strategy Parameters

```python
class StrategyOptimizer:
    """Optimize strategy parameters using genetic algorithm."""

    def __init__(self, strategy: Strategy, fitness_metric: str):
        self.strategy = strategy
        self.fitness_metric = fitness_metric
        self.param_ranges = self._extract_param_ranges(strategy)

    def _extract_param_ranges(self, strategy) -> List[ParamRange]:
        """Extract all optimizable parameters from strategy."""
        params = []

        # TP/SL
        if strategy.initial_tp_optimize:
            params.append(ParamRange('initial_tp_percent',
                strategy.initial_tp_min, strategy.initial_tp_max, strategy.initial_tp_step))
        if strategy.initial_sl_optimize:
            params.append(ParamRange('initial_sl_percent',
                strategy.initial_sl_min, strategy.initial_sl_max, strategy.initial_sl_step))

        # Entry conditions
        params.extend(self._extract_condition_params(strategy.entry_conditions, 'entry'))

        # Exit conditions
        params.extend(self._extract_condition_params(strategy.exit_conditions, 'exit'))

        return params

    def optimize(self, backtest_config: BacktestConfig,
                 population_size: int = 20,
                 generations: int = 50) -> OptimizationResult:
        """Run genetic optimization."""

        def fitness(params):
            config = self._apply_params(backtest_config, params)
            result = run_backtest(config)
            return self._get_fitness_value(result)

        # Use existing GeneticOptimizer
        optimizer = GeneticOptimizer(
            fitness_function=fitness,
            param_ranges=self.param_ranges,
            population_size=population_size,
            n_generations=generations
        )

        best_params, best_fitness = optimizer.run()

        return OptimizationResult(
            best_params=best_params,
            best_fitness=best_fitness,
            all_results=optimizer.history
        )
```

---

## 1-Minute Dataset Support

### Updates to dataset_handler.py

```python
BARS_PER_DAY = {
    '1m': 390,    # 6.5h * 60 (regular trading hours)
    '5m': 78,     # 6.5h * 12
    '15m': 26,    # 6.5h * 4
    '30m': 13,    # 6.5h * 2
    '1h': 7,      # ~6.5h (rounded)
    # ... existing ...
}

INTERVAL_MAP = {
    "1m": "1m",   # Add 1m support
    "5m": "5m",
    "15m": "15m",
    # ... existing ...
}
```

### UI Warning for Large Datasets

When user selects 1m timeframe:
- Show warning: "1-minute data generates ~390 bars per day (~98,000 bars per year). This may take longer to fetch and process."
- Consider adding progress indicator for fetch

---

## Migration Plan

1. Create new database tables (strategies, strategy_optimizations)
2. Add new columns to backtests table
3. Remove all demo data and in-memory stores from backtests.py
4. Implement Strategy model and CRUD endpoints
5. Implement StrategyExecutor condition evaluation
6. Integrate with backtesting.py library
7. Implement optimization endpoints
8. Build frontend strategy builder UI
9. Update backtest page with new workflow

---

## Files to Create/Modify

### Backend

| File | Action | Description |
|------|--------|-------------|
| `backend/app/models/strategy.py` | Create | Strategy SQLAlchemy model |
| `backend/app/models/strategy_optimization.py` | Create | StrategyOptimization model |
| `backend/app/api/strategies.py` | Create | Strategy CRUD endpoints |
| `backend/app/api/backtests.py` | Rewrite | Remove demo data, implement real backtesting |
| `backend/app/services/strategy_executor.py` | Create | Condition evaluation engine |
| `backend/app/services/backtest_engine.py` | Create | Backtesting.py integration |
| `backend/app/services/strategy_optimizer.py` | Create | GA for strategy optimization |
| `backend/app/services/dataset_handler.py` | Update | Add 1m to BARS_PER_DAY, INTERVAL_MAP |
| `backend/alembic/versions/xxx_add_strategies.py` | Create | Database migration |

### Frontend

| File | Action | Description |
|------|--------|-------------|
| `frontend/src/pages/Backtests.tsx` | Rewrite | New workflow, remove demo data |
| `frontend/src/components/StrategyBuilder.tsx` | Create | Condition tree builder |
| `frontend/src/components/ConditionEditor.tsx` | Create | Single condition editor |
| `frontend/src/components/BacktestResults.tsx` | Update | Keep charts, use real data |
| `frontend/src/api/strategies.ts` | Create | Strategy API client |
| `frontend/src/api/backtests.ts` | Update | New endpoints |

---

## Verification

1. Create strategy with entry/exit conditions
2. Run single backtest - verify trades match conditions
3. Run optimization - verify parameters are tuned
4. Save optimized strategy - verify values and ranges preserved
5. Load strategy on different compatible model - verify it works
6. Create 1m dataset - verify data fetches correctly
7. Run backtest with 1h prediction + 1m execution - verify multi-timeframe works
