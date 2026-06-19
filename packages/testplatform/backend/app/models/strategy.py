"""
Strategy model for storing trading strategies with conditions
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, Boolean, Text
from sqlalchemy.sql import func
from .database import Base


class Strategy(Base):
    """Trading strategy with entry/exit conditions"""

    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Auto-computed from conditions for compatibility matching
    required_fields = Column(JSON, nullable=True)

    # Condition trees (JSON)
    # Deprecated - kept for backwards compatibility
    entry_conditions = Column(JSON, nullable=True)
    # New separate buy/sell conditions
    buy_entry_conditions = Column(JSON, nullable=True)
    sell_entry_conditions = Column(JSON, nullable=True)
    exit_conditions = Column(JSON, nullable=True)

    # Initial TP/SL with optimization ranges
    initial_tp_percent = Column(Float, default=5.0)
    initial_tp_optimize = Column(Boolean, default=False)
    initial_tp_min = Column(Float, nullable=True)
    initial_tp_max = Column(Float, nullable=True)
    initial_tp_step = Column(Float, nullable=True)

    initial_sl_percent = Column(Float, default=2.0)
    initial_sl_optimize = Column(Boolean, default=False)
    initial_sl_min = Column(Float, nullable=True)
    initial_sl_max = Column(Float, nullable=True)
    initial_sl_step = Column(Float, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<Strategy(id={self.id}, name='{self.name}')>"

    def to_dict(self):
        """Convert to dictionary for API response"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "requiredFields": self.required_fields or [],
            # Include both old and new fields for backwards compatibility
            "entryConditions": self.entry_conditions,
            "buyEntryConditions": self.buy_entry_conditions,
            "sellEntryConditions": self.sell_entry_conditions,
            "exitConditions": self.exit_conditions or [],
            "initialTpPercent": self.initial_tp_percent,
            "initialTpOptimize": self.initial_tp_optimize,
            "initialTpMin": self.initial_tp_min,
            "initialTpMax": self.initial_tp_max,
            "initialTpStep": self.initial_tp_step,
            "initialSlPercent": self.initial_sl_percent,
            "initialSlOptimize": self.initial_sl_optimize,
            "initialSlMin": self.initial_sl_min,
            "initialSlMax": self.initial_sl_max,
            "initialSlStep": self.initial_sl_step,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }
