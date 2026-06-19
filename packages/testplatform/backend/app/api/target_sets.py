"""
Target Sets API endpoints

CRUD operations for managing reusable prediction target configurations.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
import logging

from app.models.database import get_db
from app.models.target_set import TargetSet
from app.schemas.target_set import (
    TargetSetCreate,
    TargetSetUpdate,
    TargetSetResponse,
    TargetSetListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=TargetSetListResponse)
async def list_target_sets(db: Session = Depends(get_db)):
    """
    List all saved target sets.

    Returns:
        List of all target sets ordered by name.
    """
    try:
        target_sets = db.query(TargetSet).order_by(TargetSet.name).all()

        return {
            "target_sets": target_sets,
            "total": len(target_sets)
        }
    except Exception as e:
        logger.error(f"Error listing target sets: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list target sets: {str(e)}"
        )


@router.get("/{target_set_id}", response_model=TargetSetResponse)
async def get_target_set(target_set_id: int, db: Session = Depends(get_db)):
    """
    Get a single target set by ID.

    Args:
        target_set_id: The target set ID

    Returns:
        The target set details.
    """
    target_set = db.query(TargetSet).filter(TargetSet.id == target_set_id).first()
    if not target_set:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target set with ID {target_set_id} not found"
        )
    return target_set


@router.post("", response_model=TargetSetResponse, status_code=status.HTTP_201_CREATED)
async def create_target_set(
    target_set_data: TargetSetCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new target set.

    Args:
        target_set_data: The target set configuration

    Returns:
        The created target set.
    """
    try:
        # Validate targets array is not empty
        if not target_set_data.targets or len(target_set_data.targets) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Target set must contain at least one target"
            )

        target_set = TargetSet(
            name=target_set_data.name,
            description=target_set_data.description,
            targets=target_set_data.targets
        )

        db.add(target_set)
        db.commit()
        db.refresh(target_set)

        logger.info(f"Created target set: {target_set.name} (ID: {target_set.id})")

        return target_set

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating target set: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create target set: {str(e)}"
        )


@router.put("/{target_set_id}", response_model=TargetSetResponse)
async def update_target_set(
    target_set_id: int,
    target_set_data: TargetSetUpdate,
    db: Session = Depends(get_db)
):
    """
    Update an existing target set.

    Args:
        target_set_id: The target set ID
        target_set_data: The updated configuration

    Returns:
        The updated target set.
    """
    target_set = db.query(TargetSet).filter(TargetSet.id == target_set_id).first()
    if not target_set:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target set with ID {target_set_id} not found"
        )

    try:
        # Update fields if provided
        if target_set_data.name is not None:
            target_set.name = target_set_data.name
        if target_set_data.description is not None:
            target_set.description = target_set_data.description
        if target_set_data.targets is not None:
            if len(target_set_data.targets) == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Target set must contain at least one target"
                )
            target_set.targets = target_set_data.targets

        db.commit()
        db.refresh(target_set)

        logger.info(f"Updated target set: {target_set.name} (ID: {target_set.id})")

        return target_set

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating target set: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update target set: {str(e)}"
        )


@router.delete("/{target_set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target_set(target_set_id: int, db: Session = Depends(get_db)):
    """
    Delete a target set.

    Args:
        target_set_id: The target set ID
    """
    target_set = db.query(TargetSet).filter(TargetSet.id == target_set_id).first()
    if not target_set:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target set with ID {target_set_id} not found"
        )

    try:
        db.delete(target_set)
        db.commit()

        logger.info(f"Deleted target set ID: {target_set_id}")

    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting target set: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete target set: {str(e)}"
        )
