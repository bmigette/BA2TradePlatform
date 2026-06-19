"""
API Key model for storing encrypted data provider API keys
"""

from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from sqlalchemy.sql import func
from .database import Base


class APIKey(Base):
    """API Key model"""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    provider_name = Column(String(100), nullable=False, unique=True)
    api_key_encrypted = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)

    # Timestamps
    last_tested_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<APIKey(provider='{self.provider_name}', active={self.is_active})>"
