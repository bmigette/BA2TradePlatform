"""
Target Set Schemas
"""

from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


class TargetSetCreate(BaseModel):
    """Schema for creating a new target set."""
    name: str
    description: Optional[str] = None
    targets: List[dict]  # Array of target configurations


class TargetSetUpdate(BaseModel):
    """Schema for updating a target set."""
    name: Optional[str] = None
    description: Optional[str] = None
    targets: Optional[List[dict]] = None


class TargetSetResponse(BaseModel):
    """Schema for target set response."""
    id: int
    name: str
    description: Optional[str]
    targets: List[dict]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TargetSetListResponse(BaseModel):
    """Schema for listing target sets."""
    target_sets: List[TargetSetResponse]
    total: int
