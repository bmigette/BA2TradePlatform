"""
Indicator Collection model for storing reusable indicator configurations
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Text, Boolean
from sqlalchemy.sql import func
from .database import Base


class IndicatorCollection(Base):
    """
    Model for storing indicator collection configurations.

    Each collection contains a list of indicators with their individual
    timeframe settings, allowing reuse across multiple datasets.
    """

    __tablename__ = "indicator_collections"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, default=False, nullable=False)

    # JSON field containing list of indicator configurations
    # Structure: [{"type": "sma", "name": "SMA 20", "period": 20, "timeframe": "1h"}, ...]
    indicators = Column(JSON, nullable=False)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<IndicatorCollection(id={self.id}, name='{self.name}', is_default={self.is_default})>"
