"""Robustness handler (Task 3): drives the two stress-test kinds attached to a saved backtest.

Two behaviours, both keyed off a ``RobustnessRun`` row:

  * ``run_monte_carlo_for_backtest(run_id)`` — load the run + its parent ``Backtest``, run the
    PURE-function Monte Carlo (``app.services.backtest.monte_carlo.run_monte_carlo``) over the
    parent's persisted ``trades`` (equity-relative ``pnl_pct``), and persist the percentile bands
    + drop-K table onto ``RobustnessRun.results`` with ``status='completed'``. Cheap (sub-second,
    no re-run). FAIL-SOFT: any exception sets ``status='failed'`` + ``error_message`` and does NOT
    raise (it may be called from a task worker or lazily from an API GET).

  * ``launch_schedule_variants(run_id)`` — clone the parent's ORIGINAL run config (via the shared
    ``rerun_handler.rebuild_config_for_backtest``) and, per schedule variant, override the
    ``run_schedule_override`` (weekly entry DAY Mon..Fri, and/or entry TIME shift). Each variant is
    a NEW pending ``Backtest`` row (``RBST-<variant>-<parent name>``, ``engine_type='daily_expert'``,
    ``is_saved=False``, ``optimization_id=None``) whose ``strategy_params`` carry the overridden
    ``runScheduleOverride`` so the standard ``rerun_backtest`` handler reconstructs+runs it. Each is
    queued on ``get_rerun_task_queue()`` and its id recorded in ``RobustnessRun.variant_backtest_ids``.
    The PARENT row is NEVER mutated (variants are copies).

  * ``collect_schedule_results(run_id)`` — callable lazily on GET or as a task: once every variant
    row is terminal (completed/failed), snapshot each variant's headline metrics into
    ``results['schedule_summary']`` and mark the run ``completed``.

``run_schedule_override`` shape (what ``daily_engine._entry_schedule`` reads, mirroring
``app.api.backtests._run_schedule_override``): ``{"days": {weekday: bool, ...}, "times": ["HH:MM", ...]}``.
Day variants pin exactly one weekday True with ``times=["09:30"]``; time variants keep all days True
with a single ``times=[<HH:MM>]`` entry.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.models.backtest import Backtest, RobustnessRun
from app.models.database import SessionLocal
from app.services.backtest import monte_carlo
from app.services.backtest.rerun_handler import rebuild_config_for_backtest
from app.services.task_queue import get_rerun_task_queue

import logging

logger = logging.getLogger(__name__)

# Weekday ordering — mirrors app.api.backtests._WEEKDAYS (Mon..Fri are the trading-day variants).
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TRADING_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
_DEFAULT_TIME = "09:30"  # first regular-session bar; matches _run_schedule_override

_TERMINAL = ("completed", "failed")


# ---------------------------------------------------------------------------
# Year derivation
# ---------------------------------------------------------------------------
def _years_for_backtest(bt: Backtest, trades: List[Dict[str, Any]]) -> float:
    """Years spanned by the run, for annualisation.

    Primary source: the row's ``start_date`` / ``end_date`` (how the Backtest stores its window).
    Falls back to the first/last trade ``exit_time`` (then ``entry_time``) when the columns are
    missing/degenerate. Guarded to a small positive floor so annualisation never divides by zero.
    """
    def _span_years(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
        if a is None or b is None:
            return None
        try:
            days = (b - a).days
        except Exception:  # noqa: BLE001
            return None
        return days / 365.25 if days > 0 else None

    yrs = _span_years(bt.start_date, bt.end_date)
    if yrs is None and trades:
        def _parse(v):
            if not v:
                return None
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00").split("+")[0])
            except Exception:  # noqa: BLE001
                return None
        for key in ("exit_time", "entry_time"):
            dts = [d for d in (_parse(t.get(key)) for t in trades) if d is not None]
            if len(dts) >= 2:
                yrs = _span_years(min(dts), max(dts))
                if yrs is not None:
                    break
    return yrs if (yrs is not None and yrs > 0) else 1e-6


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------
def run_monte_carlo_for_backtest(robustness_run_id: int) -> None:
    """Run the pure-function MC over the parent's persisted trades; persist results. Fail-soft."""
    db = SessionLocal()
    try:
        run = db.query(RobustnessRun).filter(RobustnessRun.id == robustness_run_id).first()
        if run is None:
            logger.warning(f"robustness MC: run {robustness_run_id} not found")
            return

        try:
            run.status = "running"
            db.commit()

            bt = db.query(Backtest).filter(Backtest.id == run.backtest_id).first()
            if bt is None:
                raise ValueError(f"parent backtest {run.backtest_id} not found")

            trades = bt.trades or []
            if not trades:
                raise ValueError(f"backtest {bt.id} has no trades to Monte-Carlo")

            initial = float(bt.initial_capital or 0.0)
            years = _years_for_backtest(bt, trades)
            cfg = run.params or {}

            results = monte_carlo.run_monte_carlo(trades, initial, years, cfg)

            run.results = results
            run.status = "completed"
            run.completed_at = datetime.now()
            run.error_message = None
            db.commit()
            logger.info(
                f"robustness MC run {run.id} completed over {len(trades)} trades "
                f"({years:.2f}y)"
            )
        except Exception as e:  # noqa: BLE001 — fail-soft: mark failed, don't raise
            logger.error(f"robustness MC run {robustness_run_id} failed: {e}", exc_info=True)
            db.rollback()
            row = db.query(RobustnessRun).filter(
                RobustnessRun.id == robustness_run_id
            ).first()
            if row is not None:
                row.status = "failed"
                row.error_message = str(e)[:1000]
                row.completed_at = datetime.now()
                db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Schedule variants
# ---------------------------------------------------------------------------
def _day_override(weekday: str) -> Dict[str, Any]:
    """run_schedule_override pinning exactly ``weekday`` (weekly entry-day variant)."""
    return {
        "days": {d: (d == weekday) for d in _WEEKDAYS},
        "times": [_DEFAULT_TIME],
    }


def _time_override(hhmm: str) -> Dict[str, Any]:
    """run_schedule_override shifting the entry TIME (all days on, single time-of-day)."""
    return {
        "days": {d: True for d in _WEEKDAYS},
        "times": [str(hhmm)],
    }


def _schedule_variants(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the list of ``{variant, override}`` from the run params.

    * ``day_variants=True`` -> one weekly-entry-day variant per Mon..Fri.
    * ``time_variants=[...]`` -> one entry-time variant per requested HH:MM.
    """
    out: List[Dict[str, Any]] = []
    if params.get("day_variants"):
        for wd in _TRADING_DAYS:
            out.append({"variant": f"day-{wd}", "override": _day_override(wd)})
    for t in (params.get("time_variants") or []):
        out.append({"variant": f"time-{t}", "override": _time_override(t)})
    return out


def launch_schedule_variants(robustness_run_id: int) -> List[int]:
    """Create + queue one variant Backtest row per schedule variant. Returns the new row ids.

    Fail-soft: on any error before rows are created, marks the run failed and returns []. Rows that
    ARE created are always queued + recorded.
    """
    db = SessionLocal()
    created_ids: List[int] = []
    try:
        run = db.query(RobustnessRun).filter(RobustnessRun.id == robustness_run_id).first()
        if run is None:
            logger.warning(f"robustness schedule: run {robustness_run_id} not found")
            return []
        try:
            run.status = "running"
            db.commit()

            bt = db.query(Backtest).filter(Backtest.id == run.backtest_id).first()
            if bt is None:
                raise ValueError(f"parent backtest {run.backtest_id} not found")

            # Reconstruct the parent's ORIGINAL config ONCE via the shared helper (validates it is
            # a daily_expert row and is reconstructible). We don't run it here — we only need the
            # persisted strategy_params to CLONE per variant; the rerun handler rebuilds+runs each
            # variant from its own row. Calling it here surfaces reconstruction errors early.
            rebuild_config_for_backtest(bt, db)

            base_sp = copy.deepcopy(bt.strategy_params or {})
            variants = _schedule_variants(run.params or {})
            if not variants:
                raise ValueError("no schedule variants requested (day_variants/time_variants both empty)")

            parent_name = bt.name
            for spec in variants:
                variant_sp = copy.deepcopy(base_sp)
                variant_sp["runScheduleOverride"] = spec["override"]
                row = Backtest(
                    name=f"RBST-{spec['variant']}-{parent_name}",
                    engine_type="daily_expert",
                    expert_name=bt.expert_name,
                    optimization_id=None,   # variants are standalone (never opt-linked)
                    strategy_id=bt.strategy_id,
                    strategy_params=variant_sp,
                    start_date=bt.start_date,
                    end_date=bt.end_date,
                    initial_capital=bt.initial_capital,
                    position_sizing_type=bt.position_sizing_type,
                    position_sizing_value=bt.position_sizing_value,
                    commission=bt.commission,
                    slippage=bt.slippage,
                    fitness_metric=bt.fitness_metric,
                    status="pending",
                    is_saved=False,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                created_ids.append(row.id)

            run.variant_backtest_ids = list(created_ids)
            db.commit()

            # Queue each variant on the dedicated re-run pool (the rerun_backtest handler rebuilds
            # its config from the row — reading the overridden runScheduleOverride — and runs it).
            queue = get_rerun_task_queue()
            for vid in created_ids:
                queue.queue_task(
                    task_type="rerun_backtest",
                    name=f"RBST variant #{vid}",
                    payload={"backtest_id": vid},
                )
            logger.info(
                f"robustness schedule run {run.id}: launched {len(created_ids)} variants "
                f"of backtest {bt.id}"
            )
            return created_ids
        except Exception as e:  # noqa: BLE001 — fail-soft
            logger.error(
                f"robustness schedule run {robustness_run_id} failed: {e}", exc_info=True
            )
            db.rollback()
            row = db.query(RobustnessRun).filter(
                RobustnessRun.id == robustness_run_id
            ).first()
            if row is not None:
                row.status = "failed"
                row.error_message = str(e)[:1000]
                row.completed_at = datetime.now()
                db.commit()
            return created_ids
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
def _headline(v: Backtest) -> Dict[str, Any]:
    """Headline metrics snapshot for one variant row."""
    return {
        "backtest_id": v.id,
        "name": v.name,
        "status": v.status,
        "annualized_return": v.annualized_return,
        "total_return": v.total_return,
        "max_drawdown": v.max_drawdown,
        "calmar": v.calmar_ratio,
        "sharpe": v.sharpe_ratio,
        "total_trades": v.total_trades,
    }


def collect_schedule_results(robustness_run_id: int) -> bool:
    """If every variant row is terminal, snapshot headline metrics + mark the run completed.

    Idempotent: safe to call repeatedly (e.g. lazily on GET). Returns True when the run is (now or
    already) completed, False while variants are still pending/running. Fail-soft.
    """
    db = SessionLocal()
    try:
        run = db.query(RobustnessRun).filter(RobustnessRun.id == robustness_run_id).first()
        if run is None:
            return False
        if run.status == "completed":
            return True
        variant_ids = list(run.variant_backtest_ids or [])
        if not variant_ids:
            return False

        variants = db.query(Backtest).filter(Backtest.id.in_(variant_ids)).all()
        if len(variants) < len(variant_ids):
            return False  # a variant row vanished — wait rather than snapshot a partial set
        if any(v.status not in _TERMINAL for v in variants):
            return False

        order = {vid: i for i, vid in enumerate(variant_ids)}
        variants.sort(key=lambda v: order.get(v.id, 0))
        summary = [_headline(v) for v in variants]

        ann = [v.annualized_return for v in variants if v.annualized_return is not None]
        results = dict(run.results or {})
        results["schedule_summary"] = summary
        if ann:
            results["ann_return_spread"] = float(max(ann) - min(ann))
        run.results = results
        run.status = "completed"
        run.completed_at = datetime.now()
        db.commit()
        logger.info(
            f"robustness schedule run {run.id} collected: {len(summary)} variants terminal"
        )
        return True
    except Exception as e:  # noqa: BLE001 — fail-soft: don't break the GET path
        logger.error(
            f"robustness collect for run {robustness_run_id} failed: {e}", exc_info=True
        )
        db.rollback()
        return False
    finally:
        db.close()
