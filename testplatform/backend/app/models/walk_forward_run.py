"""WalkForwardRun: one walk-forward chain (one goal2027 cell, or one run created in the UI).

The chain itself (app.services.wf_chain) keeps writing its report FILES exactly as before; this row is
the same result in the DB so the UI can show it without access to the report directory. Written by
app.services.wf_store as folds complete (live runs) or loaded from a report directory
(tools/import_wf_reports.py). Migration 034 mirrors this table.

Small columns first, the two JSON documents LAST: a list query selects only the small columns and
SQLite then never reads the (possibly large) ``report`` blob (same rule as backtests, see
Backtest.__table_args__).
"""
import os

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from .database import Base

#: Lifecycle of a row. ``deferred-pending``: the last fold is trained and its test waits for data
#: (two-phase walk-forward, ``--wf-defer-last-test``). ``incomplete``: an IMPORTED chain whose files
#: carry no verdict and no grid-summary entry (its driver had not finished, or was killed).
WF_STATUSES = ("pending", "running", "deferred-pending", "pass", "fail", "error", "cancelled",
               "incomplete")
WF_TERMINAL = ("deferred-pending", "pass", "fail", "error", "cancelled", "incomplete")
WF_SOURCES = ("ui", "cli", "import")


def lane_key(report_dir: str) -> str:
    """The identity of a chain's report directory (its LANE): normalised absolute path. Two report
    directories using the same cell name (goal2027 and another lane, or a copy imported from another
    host) are different chains with different rows; within one lane the chain's driver lock
    (``<report_dir>/<name>/.lock``) makes one driver at a time."""
    return os.path.normcase(os.path.abspath(report_dir))


class WalkForwardRun(Base):
    __tablename__ = "walk_forward_runs"

    id = Column(Integer, primary_key=True, index=True)
    # The chain prefix: its train jobs are <name>-wf<k>, the refit <name>-refit.
    name = Column(String(255), nullable=False)
    # lane_key(report_dir): one row per (name, lane). See lane_key.
    lane_key = Column(String(1024), nullable=False)
    source = Column(String(16), nullable=False)            # ui | cli | import
    expert = Column(String(100), nullable=True)
    strategy = Column(String(50), nullable=True)
    band = Column(String(50), nullable=True)
    status = Column(String(24), nullable=False)
    planned_folds = Column(Integer, nullable=True)
    folds_done = Column(Integer, nullable=True)
    current_fold = Column(Integer, nullable=True)
    progress_message = Column(String(500), nullable=True)
    # Stitched out-of-sample result of the fold winners (partial while running).
    stitched_return = Column(Float, nullable=True)
    worst_oos_dd = Column(Float, nullable=True)
    stitched_trades = Column(Integer, nullable=True)
    stitch_partial = Column(Boolean, nullable=True)       # the stitch covers the folds done so far
    error_message = Column(Text, nullable=True)
    # The task-queue task running it (UI runs) and the token of the driver that owns the row now.
    task_id = Column(String(64), nullable=True)
    driver_token = Column(String(64), nullable=True)
    driver_host = Column(String(255), nullable=True)
    driver_pid = Column(Integer, nullable=True)
    driver_pid_started = Column(Float, nullable=True)
    driver_identity = Column(JSON, nullable=True)
    report_dir = Column(String(1024), nullable=True)     # the chain's report directory (absolute)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    spec = Column(JSON, nullable=True)
    report = Column(JSON, nullable=True)

    __table_args__ = (UniqueConstraint("name", "lane_key", name="uq_walk_forward_runs_name_lane"),)

    def __repr__(self):
        return f"<WalkForwardRun(id={self.id}, name={self.name!r}, status={self.status!r})>"
