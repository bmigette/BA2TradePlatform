"""
Training Checkpoint Service

Handles saving and loading training checkpoints for pause/resume functionality.
Works with PyTorch Lightning and Darts models.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from app.models.database import SessionLocal
from app.models.training_checkpoint import TrainingCheckpoint

logger = logging.getLogger(__name__)


class TrainingCheckpointService:
    """
    Service for managing training checkpoints.

    Provides:
    - Periodic checkpoint saving during training
    - Checkpoint loading for resume
    - Checkpoint cleanup for completed jobs
    """

    def __init__(self, checkpoint_dir: str = "checkpoints"):
        """
        Initialize checkpoint service.

        Args:
            checkpoint_dir: Directory to store checkpoint files
        """
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Checkpoint directory: {self.checkpoint_dir.absolute()}")

    def save_checkpoint(
        self,
        task_id: str,
        epoch: int,
        model: Any,
        scaler: Any = None,
        metrics: Dict[str, float] = None,
        training_config: Dict[str, Any] = None,
        total_epochs: int = None,
        best_val_loss: float = None
    ) -> str:
        """
        Save a training checkpoint.

        Args:
            task_id: Training task ID
            epoch: Current epoch number
            model: Darts/PyTorch model to save
            scaler: Darts scaler (if any)
            metrics: Current training metrics
            training_config: Training configuration snapshot
            total_epochs: Total planned epochs
            best_val_loss: Best validation loss so far

        Returns:
            Path to saved checkpoint
        """
        # Create task-specific checkpoint directory
        task_dir = self.checkpoint_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Generate checkpoint filename
        checkpoint_name = f"checkpoint_epoch_{epoch:04d}"
        checkpoint_path = task_dir / f"{checkpoint_name}.pt"
        scaler_path = task_dir / f"{checkpoint_name}_scaler.pt" if scaler else None

        try:
            # Save model checkpoint
            model.save(str(checkpoint_path))
            logger.info(f"Saved model checkpoint: {checkpoint_path}")

            # Save scaler if provided
            if scaler and scaler_path:
                import torch
                torch.save(scaler, str(scaler_path))
                logger.info(f"Saved scaler: {scaler_path}")

            # Save to database
            db = SessionLocal()
            try:
                # Mark previous checkpoints as not latest
                db.query(TrainingCheckpoint).filter(
                    TrainingCheckpoint.task_id == task_id,
                    TrainingCheckpoint.is_latest == 1
                ).update({"is_latest": 0})

                # Create new checkpoint record
                checkpoint = TrainingCheckpoint(
                    task_id=task_id,
                    epoch=epoch,
                    checkpoint_path=str(checkpoint_path),
                    scaler_path=str(scaler_path) if scaler_path else None,
                    metrics=metrics or {},
                    training_config=training_config or {},
                    total_epochs=total_epochs,
                    best_val_loss=best_val_loss,
                    is_latest=1
                )
                db.add(checkpoint)
                db.commit()
                db.refresh(checkpoint)

                logger.info(f"Saved checkpoint record: task={task_id}, epoch={epoch}")
                return str(checkpoint_path)

            except Exception as e:
                db.rollback()
                logger.error(f"Failed to save checkpoint to database: {e}")
                raise
            finally:
                db.close()

        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            raise

    def load_checkpoint(
        self,
        task_id: str,
        model_class: type = None,
        epoch: int = None
    ) -> Tuple[Any, Dict[str, Any]]:
        """
        Load a training checkpoint.

        Args:
            task_id: Training task ID
            model_class: Darts model class to use for loading
            epoch: Specific epoch to load (None = latest)

        Returns:
            Tuple of (model, checkpoint_info)
        """
        db = SessionLocal()
        try:
            # Find checkpoint
            query = db.query(TrainingCheckpoint).filter(
                TrainingCheckpoint.task_id == task_id
            )

            if epoch is not None:
                checkpoint_record = query.filter(
                    TrainingCheckpoint.epoch == epoch
                ).first()
            else:
                # Get latest checkpoint
                checkpoint_record = query.filter(
                    TrainingCheckpoint.is_latest == 1
                ).first()

            if not checkpoint_record:
                logger.warning(f"No checkpoint found for task {task_id}")
                return None, None

            checkpoint_path = checkpoint_record.checkpoint_path
            scaler_path = checkpoint_record.scaler_path

            # Load model
            if model_class and hasattr(model_class, 'load'):
                model = model_class.load(checkpoint_path)
            else:
                # Try generic Darts model loading
                from darts.models import RNNModel
                model = RNNModel.load(checkpoint_path)

            # Load scaler if exists
            scaler = None
            if scaler_path and os.path.exists(scaler_path):
                import torch
                scaler = torch.load(scaler_path)

            checkpoint_info = {
                "task_id": task_id,
                "epoch": checkpoint_record.epoch,
                "checkpoint_path": checkpoint_path,
                "scaler_path": scaler_path,
                "scaler": scaler,
                "metrics": checkpoint_record.metrics,
                "training_config": checkpoint_record.training_config,
                "total_epochs": checkpoint_record.total_epochs,
                "best_val_loss": checkpoint_record.best_val_loss,
                "created_at": checkpoint_record.created_at.isoformat() if checkpoint_record.created_at else None
            }

            logger.info(f"Loaded checkpoint: task={task_id}, epoch={checkpoint_record.epoch}")
            return model, checkpoint_info

        except Exception as e:
            logger.error(f"Failed to load checkpoint: {e}")
            return None, None
        finally:
            db.close()

    def get_latest_checkpoint_info(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get information about the latest checkpoint without loading the model.

        Args:
            task_id: Training task ID

        Returns:
            Checkpoint info dict or None
        """
        db = SessionLocal()
        try:
            checkpoint_record = db.query(TrainingCheckpoint).filter(
                TrainingCheckpoint.task_id == task_id,
                TrainingCheckpoint.is_latest == 1
            ).first()

            if not checkpoint_record:
                return None

            return {
                "task_id": task_id,
                "epoch": checkpoint_record.epoch,
                "checkpoint_path": checkpoint_record.checkpoint_path,
                "metrics": checkpoint_record.metrics,
                "training_config": checkpoint_record.training_config,
                "total_epochs": checkpoint_record.total_epochs,
                "best_val_loss": checkpoint_record.best_val_loss,
                "created_at": checkpoint_record.created_at.isoformat() if checkpoint_record.created_at else None
            }

        finally:
            db.close()

    def list_checkpoints(self, task_id: str) -> list:
        """
        List all checkpoints for a task.

        Args:
            task_id: Training task ID

        Returns:
            List of checkpoint info dicts
        """
        db = SessionLocal()
        try:
            checkpoints = db.query(TrainingCheckpoint).filter(
                TrainingCheckpoint.task_id == task_id
            ).order_by(TrainingCheckpoint.epoch.desc()).all()

            return [{
                "id": cp.id,
                "task_id": cp.task_id,
                "epoch": cp.epoch,
                "checkpoint_path": cp.checkpoint_path,
                "metrics": cp.metrics,
                "is_latest": bool(cp.is_latest),
                "created_at": cp.created_at.isoformat() if cp.created_at else None
            } for cp in checkpoints]

        finally:
            db.close()

    def delete_checkpoint(self, task_id: str, epoch: int) -> bool:
        """
        Delete a specific checkpoint.

        Args:
            task_id: Training task ID
            epoch: Epoch number to delete

        Returns:
            True if deleted
        """
        db = SessionLocal()
        try:
            checkpoint = db.query(TrainingCheckpoint).filter(
                TrainingCheckpoint.task_id == task_id,
                TrainingCheckpoint.epoch == epoch
            ).first()

            if not checkpoint:
                return False

            # Delete checkpoint files
            if checkpoint.checkpoint_path and os.path.exists(checkpoint.checkpoint_path):
                os.remove(checkpoint.checkpoint_path)
                # Also remove any associated files (.ckpt.meta, etc.)
                base_path = Path(checkpoint.checkpoint_path)
                for related in base_path.parent.glob(f"{base_path.stem}*"):
                    if related.is_file():
                        os.remove(related)

            if checkpoint.scaler_path and os.path.exists(checkpoint.scaler_path):
                os.remove(checkpoint.scaler_path)

            # Delete database record
            db.delete(checkpoint)
            db.commit()

            logger.info(f"Deleted checkpoint: task={task_id}, epoch={epoch}")
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to delete checkpoint: {e}")
            return False
        finally:
            db.close()

    def cleanup_task_checkpoints(
        self,
        task_id: str,
        keep_latest: bool = True,
        keep_best: bool = True
    ) -> int:
        """
        Clean up checkpoints for a completed task.

        Args:
            task_id: Training task ID
            keep_latest: Keep the latest checkpoint
            keep_best: Keep the checkpoint with best validation loss

        Returns:
            Number of checkpoints deleted
        """
        db = SessionLocal()
        try:
            checkpoints = db.query(TrainingCheckpoint).filter(
                TrainingCheckpoint.task_id == task_id
            ).all()

            if not checkpoints:
                return 0

            # Determine which to keep
            keep_epochs = set()

            if keep_latest:
                latest = max(checkpoints, key=lambda x: x.epoch)
                keep_epochs.add(latest.epoch)

            if keep_best:
                valid_checkpoints = [cp for cp in checkpoints if cp.best_val_loss is not None]
                if valid_checkpoints:
                    best = min(valid_checkpoints, key=lambda x: x.best_val_loss)
                    keep_epochs.add(best.epoch)

            # Delete others
            deleted = 0
            for cp in checkpoints:
                if cp.epoch not in keep_epochs:
                    if self.delete_checkpoint(task_id, cp.epoch):
                        deleted += 1

            logger.info(f"Cleaned up {deleted} checkpoints for task {task_id}")
            return deleted

        finally:
            db.close()

    def should_save_checkpoint(self, epoch: int, interval: int = 5) -> bool:
        """
        Determine if checkpoint should be saved at this epoch.

        Args:
            epoch: Current epoch number
            interval: Save every N epochs

        Returns:
            True if checkpoint should be saved
        """
        return epoch > 0 and epoch % interval == 0


# Global instance
_checkpoint_service: Optional[TrainingCheckpointService] = None


def get_checkpoint_service() -> TrainingCheckpointService:
    """Get the global checkpoint service instance."""
    global _checkpoint_service
    if _checkpoint_service is None:
        _checkpoint_service = TrainingCheckpointService()
    return _checkpoint_service
