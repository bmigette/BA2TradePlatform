# Advanced Prediction Targets System Design

**Date:** 2026-01-29
**Status:** Approved

## Overview

This feature adds 4 new prediction target types to the dataset view, with live indicator calculation, chart visualization, and saveable target templates.

## Target Types

| Target Type | Category | Output | Description |
|-------------|----------|--------|-------------|
| Price-Based (existing) | Binary Classification | 0/1 | Price moves X% with max Y% drawdown in Z bars |
| Directional Movement | Binary Classification | 0/1 | Price higher/lower in N bars |
| Triple-Barrier | Multi-class Classification | 0/1/2 | Profit hit, stop hit, or timeout first |
| Trend Reversal | Binary Classification | 0/1 | Reversal detected via RSI/MACD/SAR/ZigZag |
| Volatility Forecasting | Regression | float | Predicted realized volatility |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend                                  │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────┐ │
│  │ TradingChart    │  │ PredictionPanel  │  │ TargetSetModal │ │
│  │ (markers, panes)│  │ (tabbed config)  │  │ (save/load)    │ │
│  └────────┬────────┘  └────────┬─────────┘  └───────┬────────┘ │
└───────────┼────────────────────┼────────────────────┼──────────┘
            │                    │                    │
            ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Backend API                               │
│  POST /datasets/{id}/calculate-indicators  (RSI, MACD, SAR, ZZ) │
│  POST /datasets/{id}/calculate-targets     (all 5 target types) │
│  GET/POST/DELETE /target-sets              (CRUD for templates) │
└─────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Services                                     │
│  IndicatorService    - Calculate RSI, MACD, SAR, ZigZag         │
│  PredictionTargetService - Extended with 4 new target types     │
└─────────────────────────────────────────────────────────────────┘
```

## Backend Design

### Indicator Service

**New file: `backend/app/services/indicators.py`**

```python
class IndicatorService:
    def calculate_rsi(self, df: DataFrame, period: int = 14) -> Series
    def calculate_macd(self, df: DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, Series]
    def calculate_sar(self, df: DataFrame, af_start: float = 0.02, af_max: float = 0.2) -> Series
    def calculate_zigzag(self, df: DataFrame, deviation_pct: float = 5.0) -> Series
```

**API Endpoint: `POST /datasets/{id}/calculate-indicators`**

Request:
```json
{
  "indicators": [
    {"type": "rsi", "period": 14},
    {"type": "macd", "fast": 12, "slow": 26, "signal": 9},
    {"type": "sar", "af_start": 0.02, "af_max": 0.2},
    {"type": "zigzag", "deviation_pct": 5.0}
  ]
}
```

### Prediction Target Service Extensions

```python
class PredictionTargetService:
    def calculate_directional(self, df, horizon: int, direction: str) -> Series
    def calculate_triple_barrier(self, df, profit_pct: float, stop_pct: float, max_bars: int) -> Series
    def calculate_trend_reversal(self, df, indicator: str, params: dict, threshold: float, direction: str) -> Series
    def calculate_volatility(self, df, horizon: int, method: str) -> Series
```

### Target Set Database Model

**New file: `backend/app/models/target_set.py`**

```python
class TargetSet(Base):
    __tablename__ = "target_sets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    targets = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

**API Endpoints:**
- GET `/target-sets` - List all
- GET `/target-sets/{id}` - Get one
- POST `/target-sets` - Create
- PUT `/target-sets/{id}` - Update
- DELETE `/target-sets/{id}` - Delete

## Frontend Design

### Prediction Targets Panel

Located directly below TradingChart. Tabbed interface:

- **Tab 1: Price-Based** - Existing profit/drawdown/time targets
- **Tab 2: Directional** - Horizon (bars), Direction (up/down)
- **Tab 3: Triple-Barrier** - Profit %, Stop %, Max bars
- **Tab 4: Trend Reversal** - Indicator, Parameters, Threshold, Direction
- **Tab 5: Volatility** - Horizon, Method (std/range/atr)

Active targets shown in list below tabs with:
- Checkbox to toggle chart visibility
- Color swatch for marker color
- Stats (hit counts / percentages)
- Remove button (with confirmation dialog)

### Chart Visualization

**Markers by target type:**

| Target Type | Hit Condition | Marker |
|-------------|---------------|--------|
| Price-Based Up | Profit hit going up | ▲ green above |
| Price-Based Down | Profit hit going down | ▼ green below |
| Directional Up | Price higher in N bars | ▲ blue above |
| Directional Down | Price lower in N bars | ▼ blue below |
| Triple-Barrier Profit | Profit hit | ▲ green above |
| Triple-Barrier Stop | Stop hit | ▼ red below |
| Triple-Barrier Timeout | Timeout | ◆ yellow on candle |
| Trend Reversal Bullish | Bullish reversal | ▲ purple above |
| Trend Reversal Bearish | Bearish reversal | ▼ purple below |
| Volatility | (regression - no markers) | - |

**Indicator panes** (for Trend Reversal):
- ZigZag, SAR: Overlay on price chart
- RSI: Separate pane, 0-100 scale
- MACD: Separate pane, histogram + lines

### Job Wizard Metrics

Support one metric per target category:
- Classification: Accuracy, F1, Precision, Recall, AUC-ROC
- Multi-class: Macro-F1, Weighted-F1
- Regression: MSE, RMSE, MAE, R², MAPE

## File Structure

**Backend:**
```
backend/app/
├── models/
│   └── target_set.py              # NEW
├── services/
│   ├── indicators.py              # NEW
│   └── ml_models.py               # MODIFY
├── api/
│   ├── target_sets.py             # NEW
│   └── datasets.py                # MODIFY
└── main.py                        # MODIFY
```

**Frontend:**
```
frontend/src/
├── components/
│   ├── TradingChart.tsx           # MODIFY
│   ├── PredictionTargetsPanel.tsx # NEW
│   └── TargetSetModal.tsx         # NEW
├── pages/
│   ├── DatasetDetails.tsx         # MODIFY
│   └── Training.tsx               # MODIFY
└── types/
    └── targets.ts                 # NEW
```

## Implementation Notes

- Indicator calculations shared between live preview and training (same backend code)
- Target sets are global templates, applicable to any compatible dataset
- Each target includes `category` field (binary_classification, multiclass_classification, regression)
- Avoid look-ahead bias: training data must not use future data for target calculation
