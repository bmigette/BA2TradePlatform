"""
Target Set Model

Stores reusable prediction target configurations as global templates.
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, JSON
from datetime import datetime
from .database import Base


class TargetSet(Base):
    """
    A saved set of prediction target configurations.

    Target sets are global templates that can be applied to any compatible dataset.
    They store the configuration for multiple prediction targets of various types.
    """
    __tablename__ = "target_sets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    targets = Column(JSON, nullable=False)  # Array of target configurations
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<TargetSet(id={self.id}, name='{self.name}')>"
