"""
Pydantic schemas for indicator collection API requests and responses
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Any


class IndicatorConfig(BaseModel):
    """Schema for a single indicator configuration"""
    type: str = Field(..., description="Indicator type (e.g., sma, ema, rsi, macd)")
    name: str = Field(..., description="Display name for the indicator")
    timeframe: str = Field(..., description="Timeframe for this indicator (e.g., 1h, 4h, 1d)")
    period: Optional[int] = Field(None, description="Period for indicators that use it")
    # Additional parameters for specific indicators
    fast: Optional[int] = Field(None, description="Fast period for MACD")
    slow: Optional[int] = Field(None, description="Slow period for MACD")
    signal: Optional[int] = Field(None, description="Signal period for MACD")
    std_dev: Optional[float] = Field(None, description="Standard deviation for Bollinger Bands")
    k_period: Optional[int] = Field(None, description="K period for Stochastic")
    d_period: Optional[int] = Field(None, description="D period for Stochastic")
    smooth_k: Optional[int] = Field(None, description="Smooth K for Stochastic")


class IndicatorCollectionCreate(BaseModel):
    """Schema for creating a new indicator collection"""
    name: str = Field(..., description="Unique name for the collection")
    description: Optional[str] = Field(None, description="Description of the collection")
    indicators: List[IndicatorConfig] = Field(..., description="List of indicator configurations")


class IndicatorCollectionUpdate(BaseModel):
    """Schema for updating an indicator collection"""
    name: Optional[str] = Field(None, description="New name for the collection")
    description: Optional[str] = Field(None, description="New description")
    indicators: Optional[List[IndicatorConfig]] = Field(None, description="Updated indicator list")


class IndicatorCollectionResponse(BaseModel):
    """Schema for indicator collection response"""
    id: int
    name: str
    description: Optional[str]
    is_default: bool
    indicators: List[dict]  # Using dict to allow flexible indicator configs
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class IndicatorCollectionListResponse(BaseModel):
    """Schema for list of indicator collections"""
    collections: List[IndicatorCollectionResponse]
    total: int


class TimeframeValidationRequest(BaseModel):
    """Schema for validating indicator timeframes against dataset timeframe"""
    dataset_timeframe: str = Field(..., description="The dataset's base timeframe")
    indicators: List[IndicatorConfig] = Field(..., description="Indicators to validate")


class TimeframeValidationResponse(BaseModel):
    """Schema for timeframe validation response"""
    valid: bool
    invalid_indicators: List[str] = Field(default_factory=list, description="Names of indicators with invalid timeframes")
    message: str
