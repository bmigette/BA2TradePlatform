"""
Training Checkpoint model for storing training state.

Enables pause/resume of training jobs with periodic state saving.
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Float
from sqlalchemy.sql import func
from .database import Base


class TrainingCheckpoint(Base):
    """Stores training checkpoints for pause/resume functionality."""

    __tablename__ = "training_checkpoints"

    id = Column(Integer, primary_key=True, index=True)

    # Link to the training task
    task_id = Column(String(50), index=True, nullable=False)

    # Epoch at which checkpoint was saved
    epoch = Column(Integer, nullable=False)

    # Path to saved model checkpoint file
    checkpoint_path = Column(String(500), nullable=False)

    # Path to saved scaler file
    scaler_path = Column(String(500), nullable=True)

    # Training metrics at checkpoint
    # Format: {"train_loss": 0.05, "val_loss": 0.07, "accuracy": 0.85}
    metrics = Column(JSON, nullable=True)

    # Training configuration snapshot
    # Stores hyperparameters, model type, dataset info
    training_config = Column(JSON, nullable=True)

    # Best validation loss seen so far
    best_val_loss = Column(Float, nullable=True)

    # Total epochs planned
    total_epochs = Column(Integer, nullable=True)

    # Whether this is the latest checkpoint for the task
    is_latest = Column(Integer, default=1)  # SQLite doesn't have boolean

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<TrainingCheckpoint(id={self.id}, task_id='{self.task_id}', epoch={self.epoch})>"
