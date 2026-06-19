"""
Indicator Collections API endpoints
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
import logging

from app.models.database import get_db
from app.models.indicator_collection import IndicatorCollection
from app.schemas.indicator_collection import (
    IndicatorCollectionCreate,
    IndicatorCollectionUpdate,
    IndicatorCollectionResponse,
    IndicatorCollectionListResponse,
    TimeframeValidationRequest,
    TimeframeValidationResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Timeframe ordering for validation (lower index = smaller timeframe)
TIMEFRAME_ORDER = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w', '1mo']

# Supported indicators with their default configurations
SUPPORTED_INDICATORS = {
    'sma': {'name': 'Simple Moving Average', 'has_period': True, 'default_period': 20},
    'ema': {'name': 'Exponential Moving Average', 'has_period': True, 'default_period': 20},
    'rsi': {'name': 'Relative Strength Index', 'has_period': True, 'default_period': 14},
    'macd': {'name': 'MACD', 'has_period': False, 'default_fast': 12, 'default_slow': 26, 'default_signal': 9},
    'bbands': {'name': 'Bollinger Bands', 'has_period': True, 'default_period': 20, 'default_std_dev': 2.0},
    'atr': {'name': 'Average True Range', 'has_period': True, 'default_period': 14},
    'stochastic': {'name': 'Stochastic Oscillator', 'has_period': False, 'default_k': 14, 'default_d': 3, 'default_smooth_k': 3},
}


def get_timeframe_index(timeframe: str) -> int:
    """Get the index of a timeframe in the ordering. Returns -1 if not found."""
    try:
        return TIMEFRAME_ORDER.index(timeframe)
    except ValueError:
        return -1


def validate_indicator_timeframe(indicator_timeframe: str, dataset_timeframe: str) -> bool:
    """
    Validate that indicator timeframe is >= dataset timeframe.
    Returns True if valid, False otherwise.
    """
    indicator_idx = get_timeframe_index(indicator_timeframe)
    dataset_idx = get_timeframe_index(dataset_timeframe)

    if indicator_idx == -1 or dataset_idx == -1:
        return False

    return indicator_idx >= dataset_idx


@router.get("", response_model=IndicatorCollectionListResponse)
async def list_collections(db: Session = Depends(get_db)):
    """
    List all indicator collections.

    Returns:
        List of all indicator collections, with defaults first.
    """
    try:
        collections = db.query(IndicatorCollection).order_by(
            IndicatorCollection.is_default.desc(),
            IndicatorCollection.name
        ).all()

        return {
            "collections": collections,
            "total": len(collections)
        }
    except Exception as e:
        logger.error(f"Error listing indicator collections: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list collections: {str(e)}"
        )


@router.get("/supported-indicators")
async def get_supported_indicators():
    """
    Get list of supported indicators and their configurations.

    Returns:
        Dictionary of supported indicators with metadata.
    """
    return {
        "indicators": SUPPORTED_INDICATORS,
        "timeframes": TIMEFRAME_ORDER,
        "description": "Available technical indicators for dataset creation"
    }


@router.get("/{collection_id}", response_model=IndicatorCollectionResponse)
async def get_collection(collection_id: int, db: Session = Depends(get_db)):
    """
    Get an indicator collection by ID.

    Args:
        collection_id: Collection ID

    Returns:
        Indicator collection details.
    """
    try:
        collection = db.query(IndicatorCollection).filter(
            IndicatorCollection.id == collection_id
        ).first()

        if not collection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Collection with ID {collection_id} not found"
            )

        return collection

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting collection: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get collection: {str(e)}"
        )


@router.post("", response_model=IndicatorCollectionResponse, status_code=status.HTTP_201_CREATED)
async def create_collection(
    collection_create: IndicatorCollectionCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new indicator collection.

    Args:
        collection_create: Collection creation parameters

    Returns:
        Created collection.
    """
    try:
        # Check if name already exists
        existing = db.query(IndicatorCollection).filter(
            IndicatorCollection.name == collection_create.name
        ).first()

        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Collection with name '{collection_create.name}' already exists"
            )

        # Convert indicator configs to dicts for JSON storage
        indicators_data = [ind.model_dump(exclude_none=True) for ind in collection_create.indicators]

        collection = IndicatorCollection(
            name=collection_create.name,
            description=collection_create.description,
            indicators=indicators_data,
            is_default=False
        )

        db.add(collection)
        db.commit()
        db.refresh(collection)

        logger.info(f"Created indicator collection: {collection.name}")
        return collection

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating collection: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create collection: {str(e)}"
        )


@router.put("/{collection_id}", response_model=IndicatorCollectionResponse)
async def update_collection(
    collection_id: int,
    collection_update: IndicatorCollectionUpdate,
    db: Session = Depends(get_db)
):
    """
    Update an indicator collection.

    Args:
        collection_id: Collection ID
        collection_update: Updated collection data

    Returns:
        Updated collection.
    """
    try:
        collection = db.query(IndicatorCollection).filter(
            IndicatorCollection.id == collection_id
        ).first()

        if not collection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Collection with ID {collection_id} not found"
            )

        # Don't allow editing default collections
        if collection.is_default:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot modify default collections"
            )

        # Check for name conflicts if name is being updated
        if collection_update.name and collection_update.name != collection.name:
            existing = db.query(IndicatorCollection).filter(
                IndicatorCollection.name == collection_update.name
            ).first()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Collection with name '{collection_update.name}' already exists"
                )
            collection.name = collection_update.name

        if collection_update.description is not None:
            collection.description = collection_update.description

        if collection_update.indicators is not None:
            collection.indicators = [ind.model_dump(exclude_none=True) for ind in collection_update.indicators]

        db.commit()
        db.refresh(collection)

        logger.info(f"Updated indicator collection: {collection.name}")
        return collection

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating collection: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update collection: {str(e)}"
        )


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(collection_id: int, db: Session = Depends(get_db)):
    """
    Delete an indicator collection.

    Args:
        collection_id: Collection ID
    """
    try:
        collection = db.query(IndicatorCollection).filter(
            IndicatorCollection.id == collection_id
        ).first()

        if not collection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Collection with ID {collection_id} not found"
            )

        # Don't allow deleting default collections
        if collection.is_default:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete default collections"
            )

        db.delete(collection)
        db.commit()

        logger.info(f"Deleted indicator collection: {collection.name}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting collection: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete collection: {str(e)}"
        )


@router.post("/validate-timeframes", response_model=TimeframeValidationResponse)
async def validate_timeframes(request: TimeframeValidationRequest):
    """
    Validate that all indicator timeframes are >= dataset timeframe.

    Args:
        request: Dataset timeframe and list of indicators to validate

    Returns:
        Validation result with list of invalid indicators if any.
    """
    invalid_indicators = []

    for indicator in request.indicators:
        if not validate_indicator_timeframe(indicator.timeframe, request.dataset_timeframe):
            invalid_indicators.append(indicator.name)

    if invalid_indicators:
        return {
            "valid": False,
            "invalid_indicators": invalid_indicators,
            "message": f"The following indicators have timeframes smaller than the dataset timeframe ({request.dataset_timeframe}): {', '.join(invalid_indicators)}"
        }

    return {
        "valid": True,
        "invalid_indicators": [],
        "message": "All indicator timeframes are valid"
    }


@router.post("/init-defaults")
async def initialize_default_collections(db: Session = Depends(get_db)):
    """
    Initialize default indicator collections (one per supported timeframe).

    Creates collections with all indicators for each timeframe:
    - All Indicators - 15m
    - All Indicators - 1h
    - All Indicators - 4h
    - All Indicators - 1D

    Returns:
        List of created collections.
    """
    try:
        default_timeframes = ['15m', '1h', '4h', '1d']
        created_collections = []

        for tf in default_timeframes:
            collection_name = f"All Indicators - {tf.upper()}"

            # Check if already exists
            existing = db.query(IndicatorCollection).filter(
                IndicatorCollection.name == collection_name
            ).first()

            if existing:
                logger.info(f"Default collection '{collection_name}' already exists")
                created_collections.append({"name": collection_name, "status": "already_exists"})
                continue

            # Create all indicators for this timeframe
            indicators = []

            # SMA variations
            for period in [10, 20, 50, 100, 200]:
                indicators.append({
                    "type": "sma",
                    "name": f"SMA {period}",
                    "period": period,
                    "timeframe": tf
                })

            # EMA variations
            for period in [12, 26, 50, 100, 200]:
                indicators.append({
                    "type": "ema",
                    "name": f"EMA {period}",
                    "period": period,
                    "timeframe": tf
                })

            # RSI
            indicators.append({
                "type": "rsi",
                "name": "RSI 14",
                "period": 14,
                "timeframe": tf
            })

            # MACD
            indicators.append({
                "type": "macd",
                "name": "MACD (12,26,9)",
                "fast": 12,
                "slow": 26,
                "signal": 9,
                "timeframe": tf
            })

            # Bollinger Bands
            indicators.append({
                "type": "bbands",
                "name": "Bollinger Bands (20,2)",
                "period": 20,
                "std_dev": 2.0,
                "timeframe": tf
            })

            # ATR
            indicators.append({
                "type": "atr",
                "name": "ATR 14",
                "period": 14,
                "timeframe": tf
            })

            # Stochastic
            indicators.append({
                "type": "stochastic",
                "name": "Stochastic (14,3,3)",
                "k_period": 14,
                "d_period": 3,
                "smooth_k": 3,
                "timeframe": tf
            })

            collection = IndicatorCollection(
                name=collection_name,
                description=f"Default collection with all standard indicators at {tf.upper()} timeframe",
                indicators=indicators,
                is_default=True
            )

            db.add(collection)
            created_collections.append({"name": collection_name, "status": "created"})

        db.commit()
        logger.info(f"Initialized {len(created_collections)} default indicator collections")

        return {
            "message": "Default collections initialized",
            "collections": created_collections
        }

    except Exception as e:
        logger.error(f"Error initializing default collections: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initialize default collections: {str(e)}"
        )
