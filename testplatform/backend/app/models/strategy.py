"""
Strategy model — unified EventAction-shaped rule lists.

A Strategy is TWO ordered lists of TradeRule dicts (the canonical shape defined in
``ba2_common.core.rule_models``): each rule = conditions (AND/OR tree or None) + one-or-more
actions + ``continue_processing``. This mirrors the live ``EventAction``/``Ruleset`` contract
1:1 — within a ruleset rules evaluate in order, the first matching rule fires ALL its actions,
and evaluation stops unless ``continue_processing`` is True. The old split representation
(buy/sell condition trees + one flat entry_actions list + single-action exit rows) was removed
by migration 027 — see docs/plans/2026-07-08-unified-rule-model.md.
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Text
from sqlalchemy.sql import func
from .database import Base


class Strategy(Base):
    """Trading strategy: ordered entry/exit TradeRule lists (EventAction-shaped)."""

    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Auto-computed from conditions for compatibility matching
    required_fields = Column(JSON, nullable=True)

    # Ordered TradeRule lists (see module docstring). entry_rules' open action (buy/sell) is
    # explicit per rule; per-rule TP/SL brackets are just extra actions on the same rule.
    entry_rules = Column(JSON, nullable=True)
    exit_rules = Column(JSON, nullable=True)

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
            "entryRules": self.entry_rules or [],
            "exitRules": self.exit_rules or [],
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }
