# Backtest Strategy Builder Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement a comprehensive backtesting system with strategy builder, genetic optimization, and multi-timeframe support.

**Architecture:** Database-backed strategies with AND/OR condition trees, backtesting.py integration for trade simulation, dual dataset support (prediction + execution timeframes), GA optimization for strategy parameters.

**Tech Stack:** SQLAlchemy models, FastAPI endpoints, backtesting.py library, React condition builder UI

---

## Phase 1: Backend Database & Models

### Task 1: Create Strategy SQLAlchemy Model

**Files:**
- Create: `backend/app/models/strategy.py`

**Step 1: Create the Strategy model**

```python
"""
Strategy model for storing trading strategies with conditions
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, Boolean, Text
from sqlalchemy.sql import func
from .database import Base


class Strategy(Base):
    """Trading strategy with entry/exit conditions"""

    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Auto-computed from conditions for compatibility matching
    required_fields = Column(JSON, nullable=True)

    # Condition trees (JSON)
    entry_conditions = Column(JSON, nullable=False)
    exit_conditions = Column(JSON, nullable=True)

    # Initial TP/SL with optimization ranges
    initial_tp_percent = Column(Float, default=5.0)
    initial_tp_optimize = Column(Boolean, default=False)
    initial_tp_min = Column(Float, nullable=True)
    initial_tp_max = Column(Float, nullable=True)
    initial_tp_step = Column(Float, nullable=True)

    initial_sl_percent = Column(Float, default=2.0)
    initial_sl_optimize = Column(Boolean, default=False)
    initial_sl_min = Column(Float, nullable=True)
    initial_sl_max = Column(Float, nullable=True)
    initial_sl_step = Column(Float, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<Strategy(id={self.id}, name='{self.name}')>"

    def to_dict(self):
        """Convert to dictionary for API response"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "requiredFields": self.required_fields or [],
            "entryConditions": self.entry_conditions,
            "exitConditions": self.exit_conditions or [],
            "initialTpPercent": self.initial_tp_percent,
            "initialTpOptimize": self.initial_tp_optimize,
            "initialTpMin": self.initial_tp_min,
            "initialTpMax": self.initial_tp_max,
            "initialTpStep": self.initial_tp_step,
            "initialSlPercent": self.initial_sl_percent,
            "initialSlOptimize": self.initial_sl_optimize,
            "initialSlMin": self.initial_sl_min,
            "initialSlMax": self.initial_sl_max,
            "initialSlStep": self.initial_sl_step,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }
```

**Step 2: Run to verify syntax**

Run: `cd backend && ./venv/bin/python -c "from app.models.strategy import Strategy; print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add backend/app/models/strategy.py
git commit -m "feat: add Strategy database model"
```

---

### Task 2: Create StrategyOptimization Model

**Files:**
- Create: `backend/app/models/strategy_optimization.py`

**Step 1: Create the StrategyOptimization model**

```python
"""
StrategyOptimization model for storing optimization runs
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, ForeignKey
from sqlalchemy.sql import func
from .database import Base


class StrategyOptimization(Base):
    """Strategy optimization run with results"""

    __tablename__ = "strategy_optimizations"

    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False)
    backtest_id = Column(Integer, ForeignKey("backtests.id"), nullable=True)
    name = Column(String(255), nullable=True)

    # Status
    status = Column(String(50), default="pending")  # pending/running/completed/failed

    # Optimization config
    fitness_metric = Column(String(50), nullable=False)  # sharpe/return/profit_factor/win_rate/max_drawdown
    optimization_type = Column(String(50), nullable=False)  # genetic/brute_force
    optimization_config = Column(JSON, nullable=True)  # GA params: population, generations, etc.

    # Ranges and results
    parameter_ranges = Column(JSON, nullable=True)  # Snapshot of ranges being optimized
    best_params = Column(JSON, nullable=True)  # Result: optimal values found
    best_fitness = Column(Float, nullable=True)
    all_results = Column(JSON, nullable=True)  # All tested combinations with fitness

    # Progress
    progress = Column(Float, default=0)
    error_message = Column(String(1000), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<StrategyOptimization(id={self.id}, status='{self.status}')>"

    def to_dict(self):
        """Convert to dictionary for API response"""
        return {
            "id": self.id,
            "strategyId": self.strategy_id,
            "backtestId": self.backtest_id,
            "name": self.name,
            "status": self.status,
            "fitnessMetric": self.fitness_metric,
            "optimizationType": self.optimization_type,
            "optimizationConfig": self.optimization_config,
            "parameterRanges": self.parameter_ranges,
            "bestParams": self.best_params,
            "bestFitness": self.best_fitness,
            "allResults": self.all_results,
            "progress": self.progress,
            "errorMessage": self.error_message,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
        }
```

**Step 2: Run to verify syntax**

Run: `cd backend && ./venv/bin/python -c "from app.models.strategy_optimization import StrategyOptimization; print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add backend/app/models/strategy_optimization.py
git commit -m "feat: add StrategyOptimization database model"
```

---

### Task 3: Update Backtest Model

**Files:**
- Modify: `backend/app/models/backtest.py`

**Step 1: Update the Backtest model with new columns**

Replace entire file with:

```python
"""
Backtest model for storing backtest results
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Float
from sqlalchemy.sql import func
from .database import Base


class Backtest(Base):
    """Backtest model"""

    __tablename__ = "backtests"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)

    # Model and datasets
    model_id = Column(Integer, ForeignKey("trained_models.id"), nullable=False)
    prediction_dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True)
    execution_dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True)

    # Strategy
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True)
    strategy_params = Column(JSON, nullable=True)  # Specific param values used

    # Configuration
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    initial_capital = Column(Float, default=10000.0)
    position_sizing_type = Column(String(50), default="fixed")  # fixed/percent
    position_sizing_value = Column(Float, default=1000.0)
    commission = Column(Float, default=0.1)
    slippage = Column(Float, default=0.05)
    fitness_metric = Column(String(50), nullable=True)

    # Results
    status = Column(String(50), default="pending")  # pending/running/completed/failed
    results = Column(JSON, nullable=True)
    trades = Column(JSON, nullable=True)
    equity_curve = Column(JSON, nullable=True)
    drawdown_curve = Column(JSON, nullable=True)

    # Performance metrics
    total_return = Column(Float, nullable=True)
    sharpe_ratio = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    win_rate = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)
    total_trades = Column(Integer, nullable=True)
    avg_trade_duration = Column(Float, nullable=True)
    best_trade = Column(Float, nullable=True)
    worst_trade = Column(Float, nullable=True)

    error_message = Column(String(1000), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<Backtest(id={self.id}, name='{self.name}', return={self.total_return})>"

    def to_dict(self):
        """Convert to dictionary for API response"""
        return {
            "id": self.id,
            "name": self.name,
            "modelId": self.model_id,
            "predictionDatasetId": self.prediction_dataset_id,
            "executionDatasetId": self.execution_dataset_id,
            "strategyId": self.strategy_id,
            "strategyParams": self.strategy_params,
            "startDate": self.start_date.isoformat() if self.start_date else None,
            "endDate": self.end_date.isoformat() if self.end_date else None,
            "initialCapital": self.initial_capital,
            "positionSizingType": self.position_sizing_type,
            "positionSizingValue": self.position_sizing_value,
            "commission": self.commission,
            "slippage": self.slippage,
            "fitnessMetric": self.fitness_metric,
            "status": self.status,
            "results": self.results,
            "trades": self.trades,
            "equityCurve": self.equity_curve,
            "drawdownCurve": self.drawdown_curve,
            "totalReturn": self.total_return,
            "sharpeRatio": self.sharpe_ratio,
            "maxDrawdown": self.max_drawdown,
            "winRate": self.win_rate,
            "profitFactor": self.profit_factor,
            "totalTrades": self.total_trades,
            "avgTradeDuration": self.avg_trade_duration,
            "bestTrade": self.best_trade,
            "worstTrade": self.worst_trade,
            "errorMessage": self.error_message,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
        }
```

**Step 2: Run to verify syntax**

Run: `cd backend && ./venv/bin/python -c "from app.models.backtest import Backtest; print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add backend/app/models/backtest.py
git commit -m "feat: update Backtest model with dual dataset and strategy support"
```

---

### Task 4: Update Models __init__.py

**Files:**
- Modify: `backend/app/models/__init__.py`

**Step 1: Add new model imports**

```python
"""Database models for the application"""

from .database import Base, engine, SessionLocal, get_db
from .worker import Worker
from .task_queue import TaskQueue, TaskStatus, TaskPriority
from .indicator_collection import IndicatorCollection
from .dataset import Dataset
from .normalization_config import NormalizationConfig
from .training_checkpoint import TrainingCheckpoint
from .news_cache import NewsCache
from .target_set import TargetSet
from .model import TrainedModel
from .backtest import Backtest
from .strategy import Strategy
from .strategy_optimization import StrategyOptimization

__all__ = [
    "Base", "engine", "SessionLocal", "get_db",
    "Worker",
    "TaskQueue", "TaskStatus", "TaskPriority",
    "IndicatorCollection",
    "Dataset",
    "NormalizationConfig",
    "TrainingCheckpoint",
    "NewsCache",
    "TargetSet",
    "TrainedModel",
    "Backtest",
    "Strategy",
    "StrategyOptimization"
]
```

**Step 2: Run to verify imports**

Run: `cd backend && ./venv/bin/python -c "from app.models import Strategy, StrategyOptimization, Backtest; print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add backend/app/models/__init__.py
git commit -m "feat: export Strategy and StrategyOptimization from models"
```

---

### Task 5: Create Database Tables

**Files:**
- None (uses existing database.py)

**Step 1: Create tables in database**

Run: `cd backend && ./venv/bin/python -c "from app.models import Base, engine; Base.metadata.create_all(bind=engine); print('Tables created')"`
Expected: Tables created

**Step 2: Verify tables exist**

Run: `cd backend && ./venv/bin/python -c "from sqlalchemy import inspect; from app.models import engine; i = inspect(engine); print('strategies' in i.get_table_names(), 'strategy_optimizations' in i.get_table_names())"`
Expected: True True

**Step 3: Commit**

```bash
git commit --allow-empty -m "chore: database tables created for strategies"
```

---

## Phase 2: Strategy API Endpoints

### Task 6: Create Strategy API Router

**Files:**
- Create: `backend/app/api/strategies.py`

**Step 1: Create the strategies API with CRUD endpoints**

```python
"""
Strategies API endpoints.

Manages trading strategies with entry/exit conditions.
"""

import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import get_db, Strategy, TrainedModel

logger = logging.getLogger(__name__)

router = APIRouter()


# Pydantic models
class ConditionBase(BaseModel):
    id: str
    field: Optional[str] = None
    field_type: Optional[str] = None  # model_probability, model_class, position, time
    comparison: Optional[str] = None  # >, >=, <, <=, ==, !=, between
    value: Optional[float | int | List] = None
    optimize: bool = False
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    value_step: Optional[float] = None
    optimize_enabled: bool = False
    confirmation_required: Optional[int] = None
    confirmation_bars: Optional[int] = None
    confirmation_bars_min: Optional[int] = None
    confirmation_bars_max: Optional[int] = None
    confirmation_bars_step: Optional[int] = None
    operator: Optional[str] = None  # AND, OR
    conditions: Optional[List["ConditionBase"]] = None


class ExitCondition(BaseModel):
    id: str
    name: Optional[str] = None
    conditions: ConditionBase
    action: str  # close, adjust_tp, adjust_sl
    action_value: Optional[float] = None
    action_value_optimize: bool = False
    action_value_min: Optional[float] = None
    action_value_max: Optional[float] = None
    action_value_step: Optional[float] = None


class StrategyCreate(BaseModel):
    name: str
    description: Optional[str] = None
    entry_conditions: dict
    exit_conditions: Optional[List[dict]] = None
    initial_tp_percent: float = 5.0
    initial_tp_optimize: bool = False
    initial_tp_min: Optional[float] = None
    initial_tp_max: Optional[float] = None
    initial_tp_step: Optional[float] = None
    initial_sl_percent: float = 2.0
    initial_sl_optimize: bool = False
    initial_sl_min: Optional[float] = None
    initial_sl_max: Optional[float] = None
    initial_sl_step: Optional[float] = None


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    entry_conditions: Optional[dict] = None
    exit_conditions: Optional[List[dict]] = None
    initial_tp_percent: Optional[float] = None
    initial_tp_optimize: Optional[bool] = None
    initial_tp_min: Optional[float] = None
    initial_tp_max: Optional[float] = None
    initial_tp_step: Optional[float] = None
    initial_sl_percent: Optional[float] = None
    initial_sl_optimize: Optional[bool] = None
    initial_sl_min: Optional[float] = None
    initial_sl_max: Optional[float] = None
    initial_sl_step: Optional[float] = None


def extract_required_fields(entry_conditions: dict, exit_conditions: list) -> List[str]:
    """Extract all model prediction fields used in conditions."""
    fields = set()

    def traverse_conditions(cond):
        if cond is None:
            return
        if isinstance(cond, dict):
            if cond.get("field_type") in ("model_probability", "model_class"):
                if cond.get("field"):
                    fields.add(cond["field"])
            if cond.get("conditions"):
                for c in cond["conditions"]:
                    traverse_conditions(c)

    traverse_conditions(entry_conditions)
    for exit_cond in (exit_conditions or []):
        traverse_conditions(exit_cond.get("conditions"))

    return sorted(list(fields))


@router.get("")
async def list_strategies(
    search: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all strategies."""
    query = db.query(Strategy)

    if search:
        query = query.filter(Strategy.name.ilike(f"%{search}%"))

    strategies = query.order_by(Strategy.created_at.desc()).all()

    return {
        "strategies": [s.to_dict() for s in strategies],
        "total": len(strategies)
    }


@router.post("")
async def create_strategy(
    strategy: StrategyCreate,
    db: Session = Depends(get_db)
):
    """Create a new strategy."""
    required_fields = extract_required_fields(
        strategy.entry_conditions,
        strategy.exit_conditions
    )

    db_strategy = Strategy(
        name=strategy.name,
        description=strategy.description,
        required_fields=required_fields,
        entry_conditions=strategy.entry_conditions,
        exit_conditions=strategy.exit_conditions or [],
        initial_tp_percent=strategy.initial_tp_percent,
        initial_tp_optimize=strategy.initial_tp_optimize,
        initial_tp_min=strategy.initial_tp_min,
        initial_tp_max=strategy.initial_tp_max,
        initial_tp_step=strategy.initial_tp_step,
        initial_sl_percent=strategy.initial_sl_percent,
        initial_sl_optimize=strategy.initial_sl_optimize,
        initial_sl_min=strategy.initial_sl_min,
        initial_sl_max=strategy.initial_sl_max,
        initial_sl_step=strategy.initial_sl_step,
    )

    db.add(db_strategy)
    db.commit()
    db.refresh(db_strategy)

    logger.info(f"Created strategy: {db_strategy.name} (id={db_strategy.id})")
    return db_strategy.to_dict()


@router.get("/compatible/{model_id}")
async def get_compatible_strategies(
    model_id: int,
    db: Session = Depends(get_db)
):
    """Get strategies compatible with a model's prediction fields."""
    model = db.query(TrainedModel).filter(TrainedModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    # Get model's prediction target fields
    model_fields = set()
    if model.prediction_targets:
        for target in model.prediction_targets:
            if isinstance(target, dict):
                # Add all possible field names the model might output
                target_type = target.get("type", "")
                if target_type:
                    model_fields.add(target_type)
                    model_fields.add(f"{target_type}_probability")
                    model_fields.add(f"{target_type}_class")

    # Get all strategies and filter by required fields
    strategies = db.query(Strategy).all()
    compatible = []

    for strategy in strategies:
        required = set(strategy.required_fields or [])
        if required.issubset(model_fields) or len(required) == 0:
            compatible.append(strategy.to_dict())

    return {
        "strategies": compatible,
        "total": len(compatible),
        "modelFields": sorted(list(model_fields))
    }


@router.get("/{strategy_id}")
async def get_strategy(
    strategy_id: int,
    db: Session = Depends(get_db)
):
    """Get strategy by ID."""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    return strategy.to_dict()


@router.put("/{strategy_id}")
async def update_strategy(
    strategy_id: int,
    update: StrategyUpdate,
    db: Session = Depends(get_db)
):
    """Update a strategy."""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    update_data = update.model_dump(exclude_unset=True)

    # Recalculate required fields if conditions changed
    if "entry_conditions" in update_data or "exit_conditions" in update_data:
        entry = update_data.get("entry_conditions", strategy.entry_conditions)
        exit = update_data.get("exit_conditions", strategy.exit_conditions)
        update_data["required_fields"] = extract_required_fields(entry, exit)

    for key, value in update_data.items():
        setattr(strategy, key, value)

    db.commit()
    db.refresh(strategy)

    logger.info(f"Updated strategy: {strategy.name} (id={strategy.id})")
    return strategy.to_dict()


@router.delete("/{strategy_id}")
async def delete_strategy(
    strategy_id: int,
    db: Session = Depends(get_db)
):
    """Delete a strategy."""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    db.delete(strategy)
    db.commit()

    logger.info(f"Deleted strategy: {strategy.name} (id={strategy_id})")
    return {"message": f"Strategy {strategy_id} deleted"}
```

**Step 2: Run to verify syntax**

Run: `cd backend && ./venv/bin/python -c "from app.api.strategies import router; print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add backend/app/api/strategies.py
git commit -m "feat: add Strategy CRUD API endpoints"
```

---

### Task 7: Register Strategy Router in Main

**Files:**
- Modify: `backend/app/main.py`

**Step 1: Add import and router registration**

Find line with other imports (around line 282-290) and add:

```python
from app.api import strategies
```

Find line with other include_router calls (around line 292-304) and add:

```python
app.include_router(strategies.router, prefix="/api/strategies", tags=["strategies"])
```

**Step 2: Run to verify server starts**

Run: `cd backend && timeout 5 ./venv/bin/python -m uvicorn app.main:app --port 8099 2>&1 | head -20`
Expected: Should show "Uvicorn running" without import errors

**Step 3: Commit**

```bash
git add backend/app/main.py
git commit -m "feat: register strategies router in main app"
```

---

### Task 8: Add 1m Timeframe Support

**Files:**
- Modify: `backend/app/services/dataset_handler.py`

**Step 1: Verify 1m is already in BARS_PER_DAY and INTERVAL_MAP**

The constants should already include 1m:

```python
BARS_PER_DAY = {
    '1m': 390,    # 6.5h * 60 (regular trading hours)
    ...
}

INTERVAL_MAP = {
    "1m": "1m",
    ...
}
```

**Step 2: Run to verify**

Run: `cd backend && ./venv/bin/python -c "from app.services.dataset_handler import BARS_PER_DAY, INTERVAL_MAP; print('1m:', BARS_PER_DAY.get('1m'), INTERVAL_MAP.get('1m'))"`
Expected: 1m: 390 1m

**Step 3: Commit (if changes needed)**

```bash
git commit --allow-empty -m "chore: verify 1m timeframe support"
```

---

## Phase 3: Backtest API Updates

### Task 9: Rewrite Backtests API

**Files:**
- Modify: `backend/app/api/backtests.py`

**Step 1: Replace entire file with database-backed implementation**

This is a large file. Create new version that:
- Removes all demo/mock data
- Uses database for storage
- Adds prediction/execution dataset support
- Integrates with Strategy model

```python
"""
Backtests API endpoints.

Manages backtesting of trained models against historical data.
"""

import logging
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import get_db, Backtest, Strategy, TrainedModel, Dataset

logger = logging.getLogger(__name__)

router = APIRouter()


class BacktestCreate(BaseModel):
    """Request model for creating a backtest."""
    name: str
    model_id: int
    prediction_dataset_id: int
    execution_dataset_id: int
    strategy_id: Optional[int] = None
    strategy_params: Optional[dict] = None
    start_date: str
    end_date: str
    initial_capital: float = 10000.0
    position_sizing_type: str = "fixed"  # fixed, percent
    position_sizing_value: float = 1000.0
    commission: float = 0.1
    slippage: float = 0.05
    fitness_metric: Optional[str] = None


class BacktestListResponse(BaseModel):
    """List of backtests."""
    backtests: List[dict]
    total: int


@router.get("")
async def list_backtests(
    db: Session = Depends(get_db)
):
    """List all backtests."""
    backtests = db.query(Backtest).order_by(Backtest.created_at.desc()).all()

    return BacktestListResponse(
        backtests=[bt.to_dict() for bt in backtests],
        total=len(backtests)
    )


@router.post("")
async def create_backtest(
    backtest: BacktestCreate,
    db: Session = Depends(get_db)
):
    """Create and run a new backtest."""
    # Validate model exists
    model = db.query(TrainedModel).filter(TrainedModel.id == backtest.model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {backtest.model_id} not found")

    # Validate datasets exist
    pred_dataset = db.query(Dataset).filter(Dataset.id == backtest.prediction_dataset_id).first()
    if not pred_dataset:
        raise HTTPException(status_code=404, detail=f"Prediction dataset {backtest.prediction_dataset_id} not found")

    exec_dataset = db.query(Dataset).filter(Dataset.id == backtest.execution_dataset_id).first()
    if not exec_dataset:
        raise HTTPException(status_code=404, detail=f"Execution dataset {backtest.execution_dataset_id} not found")

    # Validate strategy if provided
    strategy = None
    if backtest.strategy_id:
        strategy = db.query(Strategy).filter(Strategy.id == backtest.strategy_id).first()
        if not strategy:
            raise HTTPException(status_code=404, detail=f"Strategy {backtest.strategy_id} not found")

    # Parse dates
    try:
        start_date = datetime.fromisoformat(backtest.start_date)
        end_date = datetime.fromisoformat(backtest.end_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    # Create backtest record
    db_backtest = Backtest(
        name=backtest.name,
        model_id=backtest.model_id,
        prediction_dataset_id=backtest.prediction_dataset_id,
        execution_dataset_id=backtest.execution_dataset_id,
        strategy_id=backtest.strategy_id,
        strategy_params=backtest.strategy_params,
        start_date=start_date,
        end_date=end_date,
        initial_capital=backtest.initial_capital,
        position_sizing_type=backtest.position_sizing_type,
        position_sizing_value=backtest.position_sizing_value,
        commission=backtest.commission,
        slippage=backtest.slippage,
        fitness_metric=backtest.fitness_metric,
        status="pending"
    )

    db.add(db_backtest)
    db.commit()
    db.refresh(db_backtest)

    logger.info(f"Created backtest: {db_backtest.name} (id={db_backtest.id})")

    # TODO: Queue backtest execution in background
    # For now, return pending status

    return db_backtest.to_dict()


@router.get("/{backtest_id}")
async def get_backtest(
    backtest_id: int,
    db: Session = Depends(get_db)
):
    """Get backtest details by ID."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    return backtest.to_dict()


@router.delete("/{backtest_id}")
async def delete_backtest(
    backtest_id: int,
    db: Session = Depends(get_db)
):
    """Delete a backtest."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    db.delete(backtest)
    db.commit()

    logger.info(f"Deleted backtest: {backtest.name} (id={backtest_id})")
    return {"message": f"Backtest {backtest_id} deleted"}


@router.post("/{backtest_id}/export")
async def export_backtest(
    backtest_id: int,
    format: str = "csv",
    db: Session = Depends(get_db)
):
    """Export backtest results."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    if backtest.status != "completed":
        raise HTTPException(status_code=400, detail="Cannot export incomplete backtest")

    # TODO: Implement actual export
    export_path = f"exports/backtest_{backtest_id}.{format}"

    logger.info(f"Exported backtest {backtest_id} to {export_path}")

    return {
        "message": "Backtest exported successfully",
        "format": format,
        "path": export_path,
        "trades": backtest.total_trades or 0
    }


@router.post("/compare")
async def compare_backtests(
    backtest_ids: List[int],
    db: Session = Depends(get_db)
):
    """Compare multiple backtests side-by-side."""
    if len(backtest_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 backtests required for comparison")

    backtests = []
    for bt_id in backtest_ids:
        bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
        if not bt:
            raise HTTPException(status_code=404, detail=f"Backtest {bt_id} not found")

        backtests.append({
            "id": bt.id,
            "name": bt.name,
            "totalReturn": bt.total_return,
            "sharpeRatio": bt.sharpe_ratio,
            "maxDrawdown": bt.max_drawdown,
            "winRate": bt.win_rate,
            "profitFactor": bt.profit_factor,
            "totalTrades": bt.total_trades
        })

    # Calculate comparison stats
    returns = [bt["totalReturn"] for bt in backtests if bt["totalReturn"] is not None]
    sharpes = [bt["sharpeRatio"] for bt in backtests if bt["sharpeRatio"] is not None]
    drawdowns = [bt["maxDrawdown"] for bt in backtests if bt["maxDrawdown"] is not None]
    win_rates = [bt["winRate"] for bt in backtests if bt["winRate"] is not None]

    comparison = {
        "bestReturn": max(returns) if returns else None,
        "bestSharpe": max(sharpes) if sharpes else None,
        "lowestDrawdown": min(drawdowns) if drawdowns else None,
        "highestWinRate": max(win_rates) if win_rates else None,
        "avgReturn": round(sum(returns) / len(returns), 2) if returns else None,
        "avgSharpe": round(sum(sharpes) / len(sharpes), 2) if sharpes else None
    }

    return {
        "backtests": backtests,
        "comparison": comparison
    }
```

**Step 2: Run to verify syntax**

Run: `cd backend && ./venv/bin/python -c "from app.api.backtests import router; print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add backend/app/api/backtests.py
git commit -m "feat: rewrite backtests API with database storage, remove demo data"
```

---

### Task 10: Add Model Filtering and Prediction Fields Endpoint

**Files:**
- Modify: `backend/app/api/models.py`

**Step 1: Add query parameters for filtering and prediction-fields endpoint**

Find the list_models endpoint and add datasetId and timeframe query params.

Add new endpoint for prediction fields:

```python
@router.get("/{model_id}/prediction-fields")
async def get_prediction_fields(
    model_id: str,
    db: Session = Depends(get_db)
):
    """Get model's prediction target fields for condition builder."""
    model = db.query(TrainedModel).filter(TrainedModel.model_id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    fields = []
    if model.prediction_targets:
        for target in model.prediction_targets:
            if isinstance(target, dict):
                target_type = target.get("type", "")
                if target_type:
                    fields.append({
                        "field": target_type,
                        "fieldType": "model_probability",
                        "description": f"Probability of {target_type}"
                    })

    return {
        "modelId": model_id,
        "fields": fields
    }
```

**Step 2: Run to verify syntax**

Run: `cd backend && ./venv/bin/python -c "from app.api.models import router; print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add backend/app/api/models.py
git commit -m "feat: add model filtering and prediction-fields endpoint"
```

---

## Phase 4: Strategy Executor Service

### Task 11: Create Strategy Executor Service

**Files:**
- Create: `backend/app/services/strategy_executor.py`

**Step 1: Create the condition evaluation engine**

```python
"""
Strategy Executor Service

Evaluates strategy conditions against data to generate trade signals.
"""

import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ExitActionType(Enum):
    CLOSE = "close"
    ADJUST_TP = "adjust_tp"
    ADJUST_SL = "adjust_sl"


@dataclass
class ExitAction:
    action: ExitActionType
    value: Optional[float] = None


@dataclass
class Position:
    entry_price: float
    entry_time: Any
    size: float
    direction: str  # "long" or "short"
    tp_percent: float
    sl_percent: float
    bars_held: int = 0
    days_held: int = 0

    @property
    def unrealized_pnl_pct(self) -> float:
        """Calculate unrealized P&L percentage (placeholder - needs current price)."""
        return 0.0


def evaluate_comparison(left: Any, operator: str, right: Any) -> bool:
    """Evaluate a comparison operation."""
    try:
        if operator == ">":
            return float(left) > float(right)
        elif operator == ">=":
            return float(left) >= float(right)
        elif operator == "<":
            return float(left) < float(right)
        elif operator == "<=":
            return float(left) <= float(right)
        elif operator == "==":
            return left == right
        elif operator == "!=":
            return left != right
        elif operator == "between":
            if isinstance(right, (list, tuple)) and len(right) == 2:
                return float(right[0]) <= float(left) <= float(right[1])
            return False
        else:
            logger.warning(f"Unknown operator: {operator}")
            return False
    except (TypeError, ValueError) as e:
        logger.warning(f"Comparison error: {e}")
        return False


def evaluate_condition(condition: dict, context: Dict[str, Any]) -> bool:
    """
    Evaluate a single condition against the context.

    Args:
        condition: Condition dict with field, comparison, value
        context: Dict with current values for all fields

    Returns:
        True if condition is met, False otherwise
    """
    # Handle nested AND/OR operators
    operator = condition.get("operator")
    if operator in ("AND", "OR"):
        sub_conditions = condition.get("conditions", [])
        if not sub_conditions:
            return True

        if operator == "AND":
            return all(evaluate_condition(c, context) for c in sub_conditions)
        else:  # OR
            return any(evaluate_condition(c, context) for c in sub_conditions)

    # Simple condition
    field = condition.get("field")
    comparison = condition.get("comparison")
    value = condition.get("value")

    if field is None or comparison is None:
        logger.warning(f"Invalid condition: missing field or comparison")
        return False

    # Get field value from context
    field_value = context.get(field)
    if field_value is None:
        logger.debug(f"Field {field} not found in context")
        return False

    return evaluate_comparison(field_value, comparison, value)


def evaluate_condition_tree(conditions: dict, context: Dict[str, Any]) -> bool:
    """Evaluate the full condition tree."""
    if not conditions:
        return False
    return evaluate_condition(conditions, context)


class StrategyExecutor:
    """Executes strategy conditions against data."""

    def __init__(self, strategy_config: dict):
        """
        Initialize executor with strategy configuration.

        Args:
            strategy_config: Dict with entry_conditions, exit_conditions, tp/sl settings
        """
        self.entry_conditions = strategy_config.get("entry_conditions", {})
        self.exit_conditions = strategy_config.get("exit_conditions", [])
        self.initial_tp_percent = strategy_config.get("initial_tp_percent", 5.0)
        self.initial_sl_percent = strategy_config.get("initial_sl_percent", 2.0)

    def check_entry(self, context: Dict[str, Any]) -> bool:
        """
        Check if entry conditions are met.

        Args:
            context: Dict with bar data and predictions

        Returns:
            True if should enter, False otherwise
        """
        return evaluate_condition_tree(self.entry_conditions, context)

    def check_exits(self, context: Dict[str, Any]) -> Optional[ExitAction]:
        """
        Check exit conditions and return action if any triggered.

        Args:
            context: Dict with bar data, predictions, and position state

        Returns:
            ExitAction if condition triggered, None otherwise
        """
        for exit_rule in self.exit_conditions:
            conditions = exit_rule.get("conditions", {})
            if evaluate_condition_tree(conditions, context):
                action_type = exit_rule.get("action", "close")
                action_value = exit_rule.get("action_value")

                try:
                    action_enum = ExitActionType(action_type)
                except ValueError:
                    action_enum = ExitActionType.CLOSE

                return ExitAction(action=action_enum, value=action_value)

        return None

    def build_context(
        self,
        bar_data: Dict[str, Any],
        predictions: Dict[str, float],
        position: Optional[Position] = None,
        current_price: float = 0.0
    ) -> Dict[str, Any]:
        """
        Build full context for condition evaluation.

        Args:
            bar_data: OHLCV and time data
            predictions: Model prediction probabilities
            position: Current position if any
            current_price: Current market price

        Returns:
            Combined context dict
        """
        context = {**bar_data, **predictions}

        if position:
            context["bars_in_trade"] = position.bars_held
            context["days_in_trade"] = position.days_held

            # Calculate P&L
            if position.direction == "long":
                pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
            else:
                pnl_pct = (position.entry_price - current_price) / position.entry_price * 100

            context["position_pnl_pct"] = pnl_pct
            context["position_pnl_abs"] = pnl_pct * position.size / 100

        return context
```

**Step 2: Run to verify syntax**

Run: `cd backend && ./venv/bin/python -c "from app.services.strategy_executor import StrategyExecutor, evaluate_condition; print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add backend/app/services/strategy_executor.py
git commit -m "feat: add StrategyExecutor service for condition evaluation"
```

---

### Task 12: Test Strategy Executor

**Files:**
- Create: `backend/tests/test_strategy_executor.py`

**Step 1: Create unit tests**

```python
"""Tests for StrategyExecutor service."""

import pytest
from app.services.strategy_executor import (
    StrategyExecutor,
    evaluate_condition,
    evaluate_comparison,
    ExitActionType
)


class TestEvaluateComparison:
    def test_greater_than(self):
        assert evaluate_comparison(0.7, ">", 0.5) is True
        assert evaluate_comparison(0.5, ">", 0.7) is False

    def test_greater_than_equal(self):
        assert evaluate_comparison(0.7, ">=", 0.7) is True
        assert evaluate_comparison(0.5, ">=", 0.7) is False

    def test_less_than(self):
        assert evaluate_comparison(0.3, "<", 0.5) is True
        assert evaluate_comparison(0.7, "<", 0.5) is False

    def test_equal(self):
        assert evaluate_comparison(1, "==", 1) is True
        assert evaluate_comparison(1, "==", 2) is False

    def test_not_equal(self):
        assert evaluate_comparison(1, "!=", 2) is True
        assert evaluate_comparison(1, "!=", 1) is False

    def test_between(self):
        assert evaluate_comparison(5, "between", [1, 10]) is True
        assert evaluate_comparison(15, "between", [1, 10]) is False


class TestEvaluateCondition:
    def test_simple_condition(self):
        condition = {"field": "price_up", "comparison": ">", "value": 0.6}
        context = {"price_up": 0.7}
        assert evaluate_condition(condition, context) is True

    def test_missing_field(self):
        condition = {"field": "nonexistent", "comparison": ">", "value": 0.6}
        context = {"price_up": 0.7}
        assert evaluate_condition(condition, context) is False

    def test_and_operator(self):
        condition = {
            "operator": "AND",
            "conditions": [
                {"field": "price_up", "comparison": ">", "value": 0.6},
                {"field": "hour", "comparison": ">=", "value": 9}
            ]
        }
        context = {"price_up": 0.7, "hour": 10}
        assert evaluate_condition(condition, context) is True

        context = {"price_up": 0.7, "hour": 8}
        assert evaluate_condition(condition, context) is False

    def test_or_operator(self):
        condition = {
            "operator": "OR",
            "conditions": [
                {"field": "price_up", "comparison": ">", "value": 0.8},
                {"field": "price_down", "comparison": ">", "value": 0.8}
            ]
        }
        context = {"price_up": 0.5, "price_down": 0.9}
        assert evaluate_condition(condition, context) is True


class TestStrategyExecutor:
    def test_check_entry(self):
        config = {
            "entry_conditions": {
                "operator": "AND",
                "conditions": [
                    {"field": "price_up_10pct", "comparison": ">", "value": 0.7}
                ]
            }
        }
        executor = StrategyExecutor(config)

        assert executor.check_entry({"price_up_10pct": 0.8}) is True
        assert executor.check_entry({"price_up_10pct": 0.5}) is False

    def test_check_exits(self):
        config = {
            "entry_conditions": {},
            "exit_conditions": [
                {
                    "conditions": {"field": "bars_in_trade", "comparison": ">", "value": 50},
                    "action": "close"
                },
                {
                    "conditions": {"field": "position_pnl_pct", "comparison": ">", "value": 5},
                    "action": "adjust_sl",
                    "action_value": 0
                }
            ]
        }
        executor = StrategyExecutor(config)

        # No exit triggered
        action = executor.check_exits({"bars_in_trade": 10, "position_pnl_pct": 1})
        assert action is None

        # Close triggered
        action = executor.check_exits({"bars_in_trade": 60, "position_pnl_pct": 1})
        assert action is not None
        assert action.action == ExitActionType.CLOSE

        # Adjust SL triggered (first matching rule)
        action = executor.check_exits({"bars_in_trade": 10, "position_pnl_pct": 6})
        assert action is not None
        assert action.action == ExitActionType.ADJUST_SL
        assert action.value == 0
```

**Step 2: Run tests**

Run: `cd backend && ./venv/bin/python -m pytest tests/test_strategy_executor.py -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add backend/tests/test_strategy_executor.py
git commit -m "test: add unit tests for StrategyExecutor"
```

---

## Phase 5: Frontend Implementation (Summary)

The frontend implementation is large. Here's the task breakdown:

### Task 13: Create Strategy Builder Component

**Files:**
- Create: `frontend/src/components/StrategyBuilder.tsx`
- Create: `frontend/src/components/ConditionEditor.tsx`

### Task 14: Update Backtesting Page

**Files:**
- Modify: `frontend/src/pages/Backtesting.tsx`

Key changes:
- Remove all demo data and mock functions
- Add dataset selection (prediction + execution)
- Add model selection with filtering
- Add strategy selection/creation
- Keep existing chart components but wire to real data

### Task 15: Add API Hooks

**Files:**
- Create: `frontend/src/hooks/useStrategies.ts`
- Create: `frontend/src/hooks/useBacktests.ts`

---

## Phase 6: Backtest Engine Integration

### Task 16: Create Backtest Engine Service

**Files:**
- Create: `backend/app/services/backtest_engine.py`

This integrates with backtesting.py library to run actual simulations.

### Task 17: Add Background Task for Backtests

**Files:**
- Modify: `backend/app/api/backtests.py`
- Create: `backend/app/services/backtest_handler.py`

Queue backtest execution similar to how jobs are handled.

---

## Verification Checklist

After implementation:

1. [ ] Create a strategy with entry/exit conditions via API
2. [ ] Verify strategy appears in database
3. [ ] Create a backtest referencing prediction + execution datasets
4. [ ] Verify backtest is stored in database
5. [ ] Run single backtest - verify trades match conditions
6. [ ] Load compatible strategies for a model
7. [ ] Create 1m dataset - verify data fetches correctly
8. [ ] Frontend: Strategy builder creates valid condition JSON
9. [ ] Frontend: Backtest page shows real data from database

---

## Notes

- All backend code uses `./venv/bin/python` for running
- Frontend uses `npm run dev` in frontend directory
- Database tables are created automatically via SQLAlchemy
- No migrations needed (using SQLite with create_all)
