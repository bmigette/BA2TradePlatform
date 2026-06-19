"""
Normalization Config model for storing normalization parameters per trained model.

This enables exporting normalization parameters for live trading use.
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, ForeignKey
from sqlalchemy.sql import func
from .database import Base


class NormalizationConfig(Base):
    """Stores normalization parameters for a trained model."""

    __tablename__ = "normalization_configs"

    id = Column(Integer, primary_key=True, index=True)

    # Link to the trained model
    model_id = Column(String(50), index=True, nullable=False)

    # Link to the dataset used for training
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True)

    # Scaling method used
    method = Column(String(50), default="minmax_buffered", nullable=False)

    # Buffer percentage used (for minmax_buffered)
    buffer_pct = Column(Float, default=0.35)

    # MinMax parameters with buffer (JSON)
    # Format: {"Close": {"observed_min": 100, "observed_max": 200, "buffered_min": 65, "buffered_max": 235}}
    feature_ranges = Column(JSON, nullable=True)

    # Z-score parameters (JSON)
    # Format: {"Close": 150.5, "Volume": 1000000}
    means = Column(JSON, nullable=True)

    # Standard deviations for z-score (JSON)
    # Format: {"Close": 25.3, "Volume": 500000}
    stds = Column(JSON, nullable=True)

    # Full export data (JSON) - complete params for live use
    export_data = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<NormalizationConfig(id={self.id}, model_id='{self.model_id}', method='{self.method}')>"
