"""
Cleanup utilities for removing old MarketAnalysis records and associated data.

This module provides functions to safely clean up old analysis data while preserving
analyses that have linked open transactions.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from sqlmodel import Session, select, func, text
from ba2_trade_platform.core.models import (
    MarketAnalysis, 
    AnalysisOutput, 
    ExpertRecommendation,
    TradingOrder,
    Transaction,
    TradeActionResult
)
from ba2_trade_platform.core.types import MarketAnalysisStatus, TransactionStatus
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.logger import logger


def _ensure_timezone_aware(dt: datetime) -> datetime:
    """
    Ensure a datetime object is timezone-aware (UTC).
    
    SQLite doesn't natively support timezones, so datetimes retrieved from
    the database may be naive even though they were stored with timezone info.
    
    Args:
        dt: Datetime object to check
    
    Returns:
        Timezone-aware datetime in UTC
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def preview_cleanup(
    days_to_keep: int = 30,
    statuses: Optional[List[MarketAnalysisStatus]] = None,
    expert_instance_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Preview what would be deleted by the cleanup operation without actually deleting.
    
    Args:
        days_to_keep: Number of days to keep. Analyses older than this will be deleted.
        statuses: List of MarketAnalysisStatus values to target. If None, all statuses.
        expert_instance_id: If provided, only preview cleanup for this expert instance.
    
    Returns:
        Dictionary with preview information:
        {
            'total_analyses': int,
            'deletable_analyses': int,
            'protected_analyses': int,
            'analyses_by_status': {status: count},
            'estimated_outputs_deleted': int,
            'estimated_recommendations_deleted': int,
            'preview_items': [list of analysis info dicts]
        }
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
    
    with get_db() as session:
        # Build base query for old analyses
        query = select(MarketAnalysis).where(MarketAnalysis.created_at < cutoff_date)
        
        # Add status filter if provided
        if statuses:
            query = query.where(MarketAnalysis.status.in_(statuses))
        
        # Add expert filter if provided
        if expert_instance_id:
            query = query.where(MarketAnalysis.expert_instance_id == expert_instance_id)
        
        old_analyses = session.exec(query).all()
        
        # Categorize analyses
        deletable = []
        protected = []
        analyses_by_status = {}
        total_outputs = 0
        total_recommendations = 0
        
        for analysis in old_analyses:
            # Count outputs and recommendations
            outputs_count = len(analysis.analysis_outputs)
            recommendations_count = len(analysis.expert_recommendations)
            
            # Check if analysis has open transactions linked via expert recommendations
            has_open_transaction = _has_open_transaction(session, analysis.id)
            
            # Build preview item
            preview_item = {
                'id': analysis.id,
                'symbol': analysis.symbol,
                'status': analysis.status.value,
                'created_at': analysis.created_at.isoformat(),
                'outputs_count': outputs_count,
                'recommendations_count': recommendations_count,
                'has_open_transaction': has_open_transaction
            }
            
            # Track by status
            status_key = analysis.status.value
            analyses_by_status[status_key] = analyses_by_status.get(status_key, 0) + 1
            
            if has_open_transaction:
                protected.append(preview_item)
            else:
                deletable.append(preview_item)
                total_outputs += outputs_count
                total_recommendations += recommendations_count
        
        return {
            'total_analyses': len(old_analyses),
            'deletable_analyses': len(deletable),
            'protected_analyses': len(protected),
            'analyses_by_status': analyses_by_status,
            'estimated_outputs_deleted': total_outputs,
            'estimated_recommendations_deleted': total_recommendations,
            'preview_items': deletable[:100]  # Limit to 100 items for preview
        }


def execute_cleanup(
    days_to_keep: int = 30,
    statuses: Optional[List[MarketAnalysisStatus]] = None,
    expert_instance_id: Optional[int] = None,
    outputs_only: bool = False
) -> Dict[str, Any]:
    """
    Execute cleanup of old MarketAnalysis records and associated data.
    
    Will NOT delete analyses that have linked open transactions.
    
    Args:
        days_to_keep: Number of days to keep. Analyses older than this will be deleted.
        statuses: List of MarketAnalysisStatus values to target. If None, all statuses.
        expert_instance_id: If provided, only cleanup for this expert instance.
        outputs_only: If True, only delete outputs/recommendations (keep analyses). If False, delete all.
    
    Returns:
        Dictionary with cleanup results:
        {
            'success': bool,
            'analyses_deleted': int,
            'analyses_protected': int,
            'outputs_deleted': int,
            'recommendations_deleted': int,
            'errors': [list of error messages]
        }
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
    
    analyses_deleted = 0
    analyses_protected = 0
    outputs_deleted = 0
    recommendations_deleted = 0
    errors = []
    
    try:
        with get_db() as session:
            # Step 1: Clean up orphaned trade_action_result records (with NULL expert_recommendation_id)
            try:
                orphaned_count = session.exec(
                    select(TradeActionResult).where(
                        TradeActionResult.expert_recommendation_id == None
                    )
                ).all()
                
                for orphaned in orphaned_count:
                    session.delete(orphaned)
                
                if orphaned_count:
                    logger.info(f"Cleanup: Deleted {len(orphaned_count)} orphaned trade_action_result records")
                    session.commit()
            except Exception as e:
                logger.warning(f"Cleanup: Could not clean orphaned trade_action_result records: {e}")
                session.rollback()
            
            # Step 2: Build base query for old analyses
            query = select(MarketAnalysis).where(MarketAnalysis.created_at < cutoff_date)
            
            # Add status filter if provided
            if statuses:
                query = query.where(MarketAnalysis.status.in_(statuses))
            
            # Add expert filter if provided
            if expert_instance_id:
                query = query.where(MarketAnalysis.expert_instance_id == expert_instance_id)
            
            old_analyses = session.exec(query).all()
            
            logger.info(f"Cleanup: Found {len(old_analyses)} analyses older than {days_to_keep} days")
            
            for analysis in old_analyses:
                try:
                    # Check if analysis has open transactions
                    if _has_open_transaction(session, analysis.id):
                        analyses_protected += 1
                        logger.debug(f"Cleanup: Protecting analysis {analysis.id} (has open transaction)")
                        continue
                    
                    # Count what we're about to delete
                    outputs_count = len(analysis.analysis_outputs)
                    recommendations_count = len(analysis.expert_recommendations)
                    
                    # Delete analysis outputs
                    for output in analysis.analysis_outputs:
                        session.delete(output)
                    outputs_deleted += outputs_count
                    
                    # Delete expert recommendations (this will cascade delete trade_action_results via FK)
                    for recommendation in analysis.expert_recommendations:
                        session.delete(recommendation)
                    recommendations_deleted += recommendations_count
                    
                    # Delete the analysis itself only if not outputs_only mode
                    if not outputs_only:
                        session.delete(analysis)
                        analyses_deleted += 1
                        logger.debug(f"Cleanup: Deleted analysis {analysis.id} ({analysis.symbol}, {analysis.status.value})")
                    else:
                        # In outputs_only mode, just mark as "cleaned" by counting it
                        analyses_deleted += 1
                        logger.debug(f"Cleanup: Deleted outputs for analysis {analysis.id} ({analysis.symbol}, {analysis.status.value})")
                    
                except Exception as e:
                    error_msg = f"Error deleting analysis {analysis.id}: {str(e)}"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    session.rollback()
                    continue
            
            # Commit all deletions
            session.commit()
            cleanup_mode = "outputs only" if outputs_only else "all data"
            logger.info(f"Cleanup completed ({cleanup_mode}): {analyses_deleted} analyses processed, {analyses_protected} protected")
            
            # Step 3: Run VACUUM to reclaim disk space
            try:
                session.exec(text("VACUUM"))
                logger.info("Cleanup: Database VACUUM completed - disk space reclaimed")
            except Exception as e:
                logger.warning(f"Cleanup: VACUUM operation failed: {e}")
            
    except Exception as e:
        error_msg = f"Cleanup failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        errors.append(error_msg)
        return {
            'success': False,
            'analyses_deleted': 0,
            'analyses_protected': 0,
            'outputs_deleted': 0,
            'recommendations_deleted': 0,
            'errors': errors
        }
    
    return {
        'success': True,
        'analyses_deleted': analyses_deleted,
        'analyses_protected': analyses_protected,
        'outputs_deleted': outputs_deleted,
        'recommendations_deleted': recommendations_deleted,
        'errors': errors
    }


def _has_open_transaction(session: Session, market_analysis_id: int) -> bool:
    """
    Check if a MarketAnalysis has any linked open transactions.
    
    A transaction is linked to an analysis via ExpertRecommendation -> TradingOrder -> Transaction.
    
    Args:
        session: Database session
        market_analysis_id: ID of the MarketAnalysis to check
    
    Returns:
        True if there are any open transactions linked to this analysis, False otherwise
    """
    # Get all expert recommendations for this analysis
    recommendations = session.exec(
        select(ExpertRecommendation).where(
            ExpertRecommendation.market_analysis_id == market_analysis_id
        )
    ).all()
    
    # Check each recommendation for linked open transactions
    for recommendation in recommendations:
        # Get trading orders for this recommendation
        orders = session.exec(
            select(TradingOrder).where(
                TradingOrder.expert_recommendation_id == recommendation.id
            )
        ).all()
        
        # Check if any of these orders have open transactions
        for order in orders:
            if order.transaction_id:
                transaction = session.get(Transaction, order.transaction_id)
                if transaction and transaction.status == TransactionStatus.OPENED:
                    return True
    
    return False


def get_cleanup_statistics(expert_instance_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Get statistics about cleanable data.
    
    Args:
        expert_instance_id: If provided, only get stats for this expert instance.
    
    Returns:
        Dictionary with statistics:
        {
            'total_analyses': int,
            'analyses_by_status': {status: count},
            'analyses_by_age': {
                '7_days': count,
                '30_days': count,
                '90_days': count,
                '180_days': count,
                'older': count
            },
            'total_outputs': int,
            'total_recommendations': int
        }
    """
    now = datetime.now(timezone.utc)
    age_buckets = {
        '7_days': now - timedelta(days=7),
        '30_days': now - timedelta(days=30),
        '90_days': now - timedelta(days=90),
        '180_days': now - timedelta(days=180)
    }
    
    with get_db() as session:
        # Build base query
        query = select(MarketAnalysis)
        if expert_instance_id:
            query = query.where(MarketAnalysis.expert_instance_id == expert_instance_id)
        
        all_analyses = session.exec(query).all()
        
        # Count by status
        analyses_by_status = {}
        for status in MarketAnalysisStatus:
            count = sum(1 for a in all_analyses if a.status == status)
            if count > 0:
                analyses_by_status[status.value] = count
        
        # Count by age
        analyses_by_age = {
            '7_days': 0,
            '30_days': 0,
            '90_days': 0,
            '180_days': 0,
            'older': 0
        }
        
        for analysis in all_analyses:
            # Ensure created_at is timezone-aware for comparison
            created_at = _ensure_timezone_aware(analysis.created_at)
            
            if created_at > age_buckets['7_days']:
                analyses_by_age['7_days'] += 1
            elif created_at > age_buckets['30_days']:
                analyses_by_age['30_days'] += 1
            elif created_at > age_buckets['90_days']:
                analyses_by_age['90_days'] += 1
            elif created_at > age_buckets['180_days']:
                analyses_by_age['180_days'] += 1
            else:
                analyses_by_age['older'] += 1
        
        # Count outputs and recommendations
        total_outputs = session.exec(
            select(func.count(AnalysisOutput.id))
        ).one()
        
        total_recommendations = session.exec(
            select(func.count(ExpertRecommendation.id))
        ).one()
        
        return {
            'total_analyses': len(all_analyses),
            'analyses_by_status': analyses_by_status,
            'analyses_by_age': analyses_by_age,
            'total_outputs': total_outputs,
            'total_recommendations': total_recommendations
        }
