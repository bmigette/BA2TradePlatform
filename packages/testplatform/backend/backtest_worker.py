"""
Standalone backtest worker — runs a backtest in an isolated process.

Usage:
    python backtest_worker.py <task_id>

Spawned by TaskQueueService (use_subprocess=True) for backtest tasks.
Runs in a separate Python process to avoid GIL contention with the API.
"""

import os
import sys
import logging
import traceback
from datetime import datetime

backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)


def run_task(task_id: str):
    """Run a backtest task by ID."""
    try:
        from app.logging_config import setup_logging
        setup_logging()
    except Exception:
        logging.basicConfig(level=logging.INFO)

    logger = logging.getLogger("backtest_worker")
    logger.info(f"Backtest worker started for task {task_id} (PID={os.getpid()})")

    from app.models import SessionLocal, TaskQueue
    from app.services.backtest_handler import handle_backtest

    db = SessionLocal()
    try:
        task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if not task:
            logger.error(f"Task {task_id} not found")
            return
        if task.status != 'running':
            logger.warning(f"Task {task_id} status is '{task.status}', expected 'running'")
            return

        payload = task.payload or {}
        db.close()

        logger.info(f"Executing backtest handler for task {task_id}")
        result = handle_backtest(task_id, payload)

        db = SessionLocal()
        db_task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if db_task:
            result_status = result.get('status', 'completed') if isinstance(result, dict) else 'completed'
            db_task.result = result
            db_task.completed_at = datetime.now()
            db_task.progress = 100.0
            if result_status == 'failed':
                db_task.status = 'failed'
                db_task.error_message = result.get('error', 'Handler returned failed status')
            else:
                db_task.status = 'completed'
            db.commit()
            logger.info(f"Task {task_id} {db_task.status}")

    except Exception as e:
        logger.error(f"Backtest worker error for task {task_id}: {e}")
        logger.error(traceback.format_exc())
        try:
            db = SessionLocal()
            db_task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if db_task:
                db_task.status = 'failed'
                db_task.error_message = f"Worker process error: {str(e)}"
                db_task.completed_at = datetime.now()
                db.commit()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass

    logger.info(f"Backtest worker finished for task {task_id}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python backtest_worker.py <task_id>")
        sys.exit(1)
    run_task(sys.argv[1])
