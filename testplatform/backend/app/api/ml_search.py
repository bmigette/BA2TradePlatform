"""Custom ML model search (stage A) endpoint: enqueues an ``ml_model_search`` task on the main queue.

Mirrors the ``ba2-test ml-search`` CLI. Poll progress via ``GET /api/tasks/{task_id}``; results are written to
``out_dir`` (trials.jsonl, predictions/, result.json). Design: docs/plans/2026-10-27-custom-ml-models-design.md.
"""
import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.task_queue import get_task_queue

logger = logging.getLogger(__name__)

router = APIRouter()


class MlModelSearchRequest(BaseModel):
    out_dir: str
    panel_dir: str
    panel_start: str = "2023-01-03"
    panel_end: str = "2026-10-12"
    population: int = 24
    generations: int = 30
    early_stop: int = 5
    early_stop_min_rel: float = 0.01
    seed: int = 42
    parallel: int = 3
    top_n: int = 10
    darts_archs: list = ["tft", "nhits"]


@router.post("/model-search")
def start_model_search(req: MlModelSearchRequest):
    """Build (or reuse) the MAG7 panels, then enqueue the stage-A GA. Returns {task_id}."""
    from ba2_common import config as bc
    from app.services.ml_panel import PanelDataError
    from app.services.ml_search import ensure_panels
    try:
        panels = ensure_panels(Path(req.panel_dir), date.fromisoformat(req.panel_start),
                               date.fromisoformat(req.panel_end), bc.CACHE_FOLDER)
    except (PanelDataError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    payload = req.model_dump()
    payload["panels"] = panels
    task_id = get_task_queue().queue_task(
        task_type="ml_model_search", name=f"ML model search: {req.out_dir}", payload=payload,
        description="Stage-A custom ML model search (MAG7 H1/M15, 2025 folds)", timeout_seconds=72 * 3600)
    logger.info(f"ml_model_search enqueued task {task_id}")
    return {"task_id": task_id}


class MlGridRequest(BaseModel):
    """Exactly one of ``config_path`` (a JSON file of cells) and ``cells`` (the cell objects themselves)."""
    out_dir: str
    config_path: Optional[str] = None
    cells: Optional[List[Dict[str, Any]]] = None
    cpu_slots: int
    gpu_slots: int = 0
    limit_cells: Optional[int] = None
    cache_root: Optional[str] = None
    cores: Optional[int] = None
    settings: Optional[Dict[str, Any]] = None
    rerun_mismatched: bool = False


@router.post("/grid")
def start_grid(req: MlGridRequest):
    """Check everything that can be refused (payload and limits, strict cells, the holdout lock, unusable feature
    groups, a live run in ``out_dir``, finished rows computed under other settings or data) and only then enqueue the
    stage-1 grid (task ``ml_grid``): any refusal is a 400 and nothing is queued. Mirrors ``ba2-test ml-grid``; results
    land in ``out_dir`` (results.jsonl, predictions/, errors.jsonl)."""
    import os
    from app.services.ml_grid import GridLockedError, ResumeMismatchError, precheck_grid
    from app.services.ml_holdout import LockedSymbolError
    payload = req.model_dump()
    payload["cores"] = req.cores if req.cores is not None else (os.cpu_count() or 1)
    try:
        n_cells = precheck_grid(payload)
    except (ValueError, LockedSymbolError, OSError, ResumeMismatchError, GridLockedError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    task_id = get_task_queue().queue_task(
        task_type="ml_grid", name=f"ML grid: {req.out_dir}", payload=payload,
        description=f"ML exploration v2 stage-1 grid: {n_cells} cells", timeout_seconds=7 * 24 * 3600)
    logger.info(f"ml_grid enqueued task {task_id}")
    return {"task_id": task_id, "cells": n_cells}
