"""
Strategy model for storing trading strategies with conditions
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Text
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

    # Entry TP/SL is expressed as RULES, not bespoke fields — the same shape/conversion
    # (action_from_rule) exit_conditions rows already use, minus a nested `conditions`
    # sub-tree (an entry action fires on the SAME gate as the buy/sell action). A non-empty
    # list is the opt-in signal, exactly like a non-empty exit_conditions list already is for
    # exit management — there is no separate boolean flag. See
    # docs/plans/2026-07-03-entry-tp-sl-bracket-actions.md (REVISION 2026-07-04) for why the
    # earlier initial_tp_*/initial_sl_* dedicated columns were removed in favor of this.
    entry_actions = Column(JSON, nullable=True)

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
            "entryActions": self.entry_actions or [],
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }
