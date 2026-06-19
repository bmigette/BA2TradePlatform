"""
Standalone training worker — runs a training job in an isolated process.

Usage:
    python training_worker.py <task_id>

This script is spawned by the TaskQueueService when use_subprocess=True.
It runs the training handler in a separate Python process, freeing the
main API process from GIL contention during CPU/GPU-intensive training.

Communication with the parent process is via the database:
- The parent claims the task (sets status='running') before spawning
- This worker runs the handler, which updates progress via DB
- On completion, this worker sets status='completed' or 'failed'
- The parent detects task completion by polling the DB
"""

import os
import sys
import logging
import traceback
from datetime import datetime

# Ensure backend directory is on the path
backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)


def run_task(task_id: str):
    """Run a training task by ID."""
    # Set up logging
    try:
        from app.logging_config import setup_logging
        setup_logging()
    except Exception:
        logging.basicConfig(level=logging.INFO)

    logger = logging.getLogger("training_worker")
    logger.info(f"Training worker started for task {task_id} (PID={os.getpid()})")

    # Import after path setup
    from app.models import SessionLocal, TaskQueue
    from app.services.job_handler import handle_training_job

    db = SessionLocal()
    try:
        # Load the task
        task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if not task:
            logger.error(f"Task {task_id} not found")
            return

        if task.status != 'running':
            logger.warning(f"Task {task_id} status is '{task.status}', expected 'running'")
            return

        payload = task.payload or {}
        db.close()  # Close before long-running handler (it creates its own sessions)

        # Run the handler
        logger.info(f"Executing training handler for task {task_id}")
        result = handle_training_job(task_id, payload)

        # Update final status
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
                logger.warning(f"Task {task_id} failed: {db_task.error_message}")
            else:
                db_task.status = 'completed'
                logger.info(f"Task {task_id} completed successfully")

            db.commit()

    except Exception as e:
        logger.error(f"Training worker error for task {task_id}: {e}")
        logger.error(traceback.format_exc())

        # Mark task as failed
        try:
            db = SessionLocal()
            db_task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if db_task:
                db_task.status = 'failed'
                db_task.error_message = f"Worker process error: {str(e)}"
                db_task.completed_at = datetime.now()
                db.commit()
        except Exception as db_err:
            logger.error(f"Failed to update task status: {db_err}")
        finally:
            db.close()

    finally:
        try:
            db.close()
        except Exception:
            pass

    logger.info(f"Training worker finished for task {task_id}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python training_worker.py <task_id>")
        sys.exit(1)

    run_task(sys.argv[1])
