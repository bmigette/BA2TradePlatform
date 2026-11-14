"""
StrategyOptimization model for storing optimization runs
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, ForeignKey, Index
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

    # Covering index for the dashboard's "recent optimizations" activity + status counts. Same
    # trap as backtests: all_results (0.5 MB avg, 3.3 MB max) is stored BEFORE created_at, so an
    # ORDER BY created_at scan reads every row's blob. See Backtest.__table_args__ for the rules;
    # mirrored by db_migrate/031.
    __table_args__ = (
        Index("ix_strategy_optimizations_activity",
              "created_at", "id", "name", "status", "started_at", "completed_at"),
    )

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


class StrategyOptimizationRunner(Base):
    """WHO is running (or ran) an optimization row, so an interrupted job can be resumed without
    ever hijacking a live one (app.services.optimization_resume). One row per optimization,
    written by the handler when the job starts.

    ``host`` + ``pid`` + ``pid_started`` (the process creation time, epoch seconds) +
    ``proc_identity`` (Linux boot id + kernel start tick, command-line hash) identify the process:
    on the same host a pid that no longer exists, or whose ``proc_identity`` proves reuse, is dead;
    a start-time mismatch alone is UNKNOWN (a clock step moves it), never dead. A row owned by
    another host is never judged here (optimization_resume.judge).
    ``code_versions`` = the TEST app version + package minimums the job started under (a resume
    across a code change is refused by default). ``resumed_from_id`` = the interrupted row this
    one continues. ``topn_done_at`` = when the launcher's end-of-job TOP-N persist ran to its
    end (NULL while it is running or after it was killed). A table of its own, not columns on
    ``strategy_optimizations``: that row carries the multi-MB ``all_results`` blob, and old code
    never reads this table. Migration 033 mirrors it."""

    __tablename__ = "strategy_optimization_runners"

    optimization_id = Column(Integer, ForeignKey("strategy_optimizations.id"), primary_key=True)
    host = Column(String(255), nullable=False)
    pid = Column(Integer, nullable=False)
    pid_started = Column(Float, nullable=False)
    proc_identity = Column(JSON, nullable=True)       # boot id + kernel start tick / cmdline hash
    # Random per RUN, written into every checkpoint of it: a checkpoint is this run's only if the
    # tokens match (SQLite reuses the id of a deleted max-id row, so the id alone is not proof).
    run_token = Column(String(64), nullable=True)
    code_versions = Column(JSON, nullable=True)
    resumed_from_id = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    topn_done_at = Column(DateTime(timezone=True), nullable=True)


class BacktestWriter(Base):
    """The process writing a re-run Backtest row (end-of-job TOP-N, walk-forward out-of-sample),
    recorded with the row: a row left non-completed is only retired once this writer is proven
    dead (optimization_resume.retire_dead_backtest). Migration 033 mirrors it."""

    __tablename__ = "backtest_writers"

    backtest_id = Column(Integer, ForeignKey("backtests.id"), primary_key=True)
    host = Column(String(255), nullable=False)
    pid = Column(Integer, nullable=False)
    pid_started = Column(Float, nullable=False)
    proc_identity = Column(JSON, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())


class StrategyOptimizationPopulation(Base):
    """The FINAL evaluated population of a completed GA job (walk-forward chain seeding).

    A table of its own so no reader of ``optimization_config`` (list endpoint, clone, re-run) ever
    loads it. ``population`` = {generation, gene_names, genes, fitnesses}; ``gene_space_signature``
    = the ordered gene definitions the genes were bred in (see app.services.chain_seed).
    Migration 032 mirrors this table."""

    __tablename__ = "strategy_optimization_populations"

    optimization_id = Column(Integer, ForeignKey("strategy_optimizations.id"), primary_key=True)
    gene_space_signature = Column(JSON, nullable=False)
    population = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
