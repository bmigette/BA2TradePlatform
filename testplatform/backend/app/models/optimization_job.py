"""
Optimization Job model for tracking genetic algorithm optimization jobs
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Float
from sqlalchemy.sql import func
from .database import Base


class OptimizationJob(Base):
    """Optimization Job model"""

    __tablename__ = "optimization_jobs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False)
    status = Column(
        String(50),
        nullable=False,
        default="pending"
    )  # pending, queued, running, paused, completed, failed, cancelled

    # Model and parameter configuration
    model_types = Column(JSON, nullable=False)  # Array of model type strings
    parameter_ranges = Column(JSON, nullable=False)  # Parameter optimization ranges
    prediction_targets = Column(JSON, nullable=False)  # User-defined output fields

    # Training configuration
    train_test_split = Column(Float, nullable=False, default=0.8)

    # Results
    best_model_id = Column(Integer, ForeignKey("models.id"), nullable=True)
    best_fitness_score = Column(Float, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<OptimizationJob(id={self.id}, name='{self.name}', status='{self.status}')>"
