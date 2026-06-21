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

    # Distributed execution: remote Worker ids selected for this run (empty/None = local only).
    worker_ids = Column(JSON, nullable=True)

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
            "workerIds": self.worker_ids,
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
