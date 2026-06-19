"""
Backtest model for storing backtest results
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, ForeignKey, Float, Boolean
from sqlalchemy.sql import func
from .database import Base


# Max points per curve in a detail response. A 3yr × 5min run has ~58k equity/drawdown points;
# rendering that many in the recharts AreaChart froze the UI on load. We thin the curves to this
# many points for DISPLAY only (the full curves stay in the DB columns + CSV/JSON export).
_CHART_MAX_POINTS = 2000


def _num(v):
    return float(v) if isinstance(v, (int, float)) else 0.0


def _lttb_indices(values, target):
    """Largest-Triangle-Three-Buckets downsampling — returns sorted indices into ``values`` that
    preserve the curve's visual shape (peaks/troughs), unlike a uniform stride which can drop them.
    x is the point position (curves are evenly spaced in time). Full range when n <= target."""
    n = len(values)
    if n <= target or target < 3:
        return list(range(n))
    out = [0]
    bucket = (n - 2) / (target - 2)
    a = 0  # index of the previously selected point
    for i in range(target - 2):
        start = int((i + 1) * bucket) + 1
        end = min(int((i + 2) * bucket) + 1, n)
        avg_start = int((i + 2) * bucket) + 1
        avg_end = min(int((i + 3) * bucket) + 1, n)
        if avg_start >= avg_end:
            avg_start, avg_end = max(start, n - 1), n
        avg_x = (avg_start + avg_end - 1) / 2.0
        avg_y = sum(values[j] for j in range(avg_start, avg_end)) / max(1, avg_end - avg_start)
        ay = values[a]
        best_area, best = -1.0, start
        for j in range(start, end):
            area = abs((a - avg_x) * (values[j] - ay) - (a - j) * (avg_y - ay))
            if area > best_area:
                best_area, best = area, j
        out.append(best)
        a = best
    if out[-1] != n - 1:  # the last bucket may already have picked n-1; don't duplicate it
        out.append(n - 1)
    return out


def _downsample_curves(equity, drawdown, target=_CHART_MAX_POINTS):
    """Thin the (index-aligned) equity + drawdown curves to ~target points for charting: LTTB on
    equity plus the global max-drawdown trough, applied with the SAME indices to both so the two
    series stay aligned and the worst-drawdown point is never lost. Returns (equity, drawdown)
    lists; unchanged when already <= target."""
    eq = equity or []
    dd = drawdown or []
    n = len(eq)
    if n <= target:
        return eq, dd
    idx = set(_lttb_indices([_num(p.get("equity")) for p in eq], target))
    aligned = bool(dd) and len(dd) == n
    if aligned:
        dd_vals = [_num(p.get("drawdown")) for p in dd]
        idx.add(min(range(n), key=lambda j: dd_vals[j]))  # always keep the max-drawdown trough
    order = sorted(idx)
    return [eq[j] for j in order], ([dd[j] for j in order] if aligned else dd)


class Backtest(Base):
    """Backtest model"""

    __tablename__ = "backtests"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)

    # Model and datasets
    # nullable=True (Decision 3a): daily expert (non-ML) backtests are NOT model-driven, so
    # they store model_id=None. The legacy ML path always sets a real model_id. The matching
    # migration that flips this on EXISTING populated DBs is Task 7 (db_migrate revision 018).
    model_id = Column(Integer, ForeignKey("trained_models.id"), nullable=True)
    prediction_dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True)
    execution_dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True)

    # Engine discriminator (Task 7 / migration 018): distinguishes the two backtest
    # engines that share this one table. 'ml' = legacy model-driven backtesting.py runs
    # (model_id set); 'daily_expert' = Phase-2 daily expert engine (model_id=None). The
    # daily route sets this to 'daily_expert'; everything else defaults to 'ml'.
    engine_type = Column(String(50), default="ml")

    # Grouping/stats: the expert this run backtested, and the optimization job (if any)
    # it belongs to. Lets runs be filtered per expert (best-N retention) and per opt job
    # (group stats). Both nullable: legacy/manual runs may have neither.
    expert_name = Column(String(100), nullable=True, index=True)
    optimization_id = Column(Integer, nullable=True, index=True)

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
    description = Column(Text, nullable=True)  # User/agent notes about the backtest

    # Results
    status = Column(String(50), default="pending")  # pending/running/completed/failed
    results = Column(JSON, nullable=True)
    trades = Column(JSON, nullable=True)
    equity_curve = Column(JSON, nullable=True)
    drawdown_curve = Column(JSON, nullable=True)

    # Performance metrics (from backtesting.py)
    total_return = Column(Float, nullable=True)
    sharpe_ratio = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    win_rate = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)
    total_trades = Column(Integer, nullable=True)
    winning_trades = Column(Integer, nullable=True)
    losing_trades = Column(Integer, nullable=True)
    avg_trade_duration = Column(Float, nullable=True)
    final_equity = Column(Float, nullable=True)
    best_trade = Column(Float, nullable=True)
    worst_trade = Column(Float, nullable=True)

    # Additional metrics from backtesting.py
    exposure_time = Column(Float, nullable=True)  # % of time in position
    buy_hold_return = Column(Float, nullable=True)  # Benchmark B&H return
    annualized_return = Column(Float, nullable=True)  # Annualized return %
    volatility = Column(Float, nullable=True)  # Annualized volatility %
    sortino_ratio = Column(Float, nullable=True)  # Downside risk-adjusted return
    calmar_ratio = Column(Float, nullable=True)  # Return / Max Drawdown
    sqn = Column(Float, nullable=True)  # System Quality Number
    expectancy = Column(Float, nullable=True)  # Average expected return per trade
    avg_drawdown = Column(Float, nullable=True)  # Average drawdown %
    max_drawdown_duration = Column(Float, nullable=True)  # Max DD duration in days
    avg_trade = Column(Float, nullable=True)  # Average trade return % (geometric)
    equity_peak = Column(Float, nullable=True)  # Peak equity reached

    error_message = Column(String(1000), nullable=True)

    # Save status
    is_saved = Column(Boolean, default=False)  # Whether backtest has been explicitly saved with a name

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<Backtest(id={self.id}, name='{self.name}', return={self.total_return})>"

    def _transform_trades_for_frontend(self):
        """Transform trade data to match frontend expected format."""
        if not self.trades:
            return []

        transformed = []
        for i, trade in enumerate(self.trades):
            # Map backend field names to frontend expected names
            # Backend: entry_time, exit_time, direction (buy/sell), pnl_pct, bars_held
            # Frontend: entryDate, exitDate, direction (long/short), pnlPercent, duration
            direction = trade.get('direction', 'buy')
            if direction == 'buy':
                direction = 'long'
            elif direction == 'sell':
                direction = 'short'

            transformed.append({
                'id': i + 1,
                'symbol': trade.get('symbol', ''),
                'entryDate': trade.get('entry_time', ''),
                'exitDate': trade.get('exit_time', ''),
                'entryPrice': trade.get('entry_price', 0),
                'exitPrice': trade.get('exit_price', 0),
                'size': trade.get('size', 0),
                'direction': direction,
                'pnl': trade.get('pnl', 0),
                'pnlPercent': trade.get('pnl_pct', 0),
                'duration': trade.get('bars_held', 0),
                'exitReason': trade.get('exit_reason', 'unknown'),
            })
        return transformed

    def to_summary_dict(self):
        """Convert to lightweight dictionary for list endpoints (no curves/trades)."""
        return {
            "id": self.id,
            "name": self.name,
            "engineType": self.engine_type or "ml",
            "expertName": self.expert_name,
            "optimizationId": self.optimization_id,
            "modelId": self.model_id,
            "predictionDatasetId": self.prediction_dataset_id,
            "executionDatasetId": self.execution_dataset_id,
            "strategyId": self.strategy_id,
            "startDate": self.start_date.isoformat() if self.start_date else None,
            "endDate": self.end_date.isoformat() if self.end_date else None,
            "initialCapital": self.initial_capital,
            "fitnessMetric": self.fitness_metric,
            "status": self.status,
            "totalReturn": self.total_return,
            "sharpeRatio": self.sharpe_ratio,
            "maxDrawdown": self.max_drawdown,
            "winRate": self.win_rate,
            "profitFactor": self.profit_factor,
            "totalTrades": self.total_trades,
            "avgTradeDuration": self.avg_trade_duration,
            "finalEquity": self.final_equity,
            "bestTrade": self.best_trade,
            "worstTrade": self.worst_trade,
            "errorMessage": self.error_message,
            "description": self.description,
            "isSaved": self.is_saved or False,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
        }

    def to_dict(self):
        """Convert to full dictionary for detail endpoints (includes curves/trades)."""
        # Transform trades to frontend format
        transformed_trades = self._transform_trades_for_frontend()

        # Downsample the curves for DISPLAY (the full curves stay in the DB columns + CSV/JSON
        # export). A dense 5min run has ~58k points, which froze the recharts AreaChart on load.
        equity_curve, drawdown_curve = _downsample_curves(self.equity_curve, self.drawdown_curve)

        # Build results object that frontend expects
        results = {
            "equityCurve": equity_curve,
            "drawdownCurve": drawdown_curve,
            "trades": transformed_trades,
            "priceData": [],  # Price data would need to be fetched separately
        }

        return {
            "id": self.id,
            "name": self.name,
            "engineType": self.engine_type or "ml",
            "expertName": self.expert_name,
            "optimizationId": self.optimization_id,
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
            "results": results,  # Nested results object for frontend
            "trades": transformed_trades,  # Also at top level for backwards compat
            "equityCurve": equity_curve,
            "drawdownCurve": drawdown_curve,
            "totalReturn": self.total_return,
            "sharpeRatio": self.sharpe_ratio,
            "maxDrawdown": self.max_drawdown,
            "winRate": self.win_rate,
            "profitFactor": self.profit_factor,
            "totalTrades": self.total_trades,
            "winningTrades": self.winning_trades,
            "losingTrades": self.losing_trades,
            "avgTradeDuration": self.avg_trade_duration,
            "finalEquity": self.final_equity,
            "bestTrade": self.best_trade,
            "worstTrade": self.worst_trade,
            # Additional metrics from backtesting.py
            "exposureTime": self.exposure_time,
            "buyHoldReturn": self.buy_hold_return,
            "annualizedReturn": self.annualized_return,
            "volatility": self.volatility,
            "sortinoRatio": self.sortino_ratio,
            "calmarRatio": self.calmar_ratio,
            "sqn": self.sqn,
            "expectancy": self.expectancy,
            "avgDrawdown": self.avg_drawdown,
            "maxDrawdownDuration": self.max_drawdown_duration,
            "avgTrade": self.avg_trade,
            "equityPeak": self.equity_peak,
            "errorMessage": self.error_message,
            "description": self.description,
            "isSaved": self.is_saved or False,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
        }
