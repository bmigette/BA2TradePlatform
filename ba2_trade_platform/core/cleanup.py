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
    TradeActionResult,
    ActivityLog
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
        from sqlalchemy.orm import selectinload

        # Build base query for old analyses with eager loading to avoid N+1 queries
        query = (
            select(MarketAnalysis)
            .options(
                selectinload(MarketAnalysis.analysis_outputs),
                selectinload(MarketAnalysis.expert_recommendations)
            )
            .where(MarketAnalysis.created_at < cutoff_date)
        )

        # Add status filter if provided
        if statuses:
            query = query.where(MarketAnalysis.status.in_(statuses))

        # Add expert filter if provided
        if expert_instance_id:
            query = query.where(MarketAnalysis.expert_instance_id == expert_instance_id)

        old_analyses = session.exec(query).all()
        logger.debug(f"[preview_cleanup] Found {len(old_analyses)} analyses to check")

        # Batch query: get all analysis IDs that have open transactions (single query)
        all_analysis_ids = [a.id for a in old_analyses]
        protected_ids = _get_analysis_ids_with_open_transactions(session, all_analysis_ids)
        logger.debug(f"[preview_cleanup] {len(protected_ids)} analyses have open transactions")

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

            # Check if analysis has open transactions (from batch result)
            has_open_transaction = analysis.id in protected_ids

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

        logger.debug(f"[preview_cleanup] Categorization complete: {len(deletable)} deletable, {len(protected)} protected")
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
    outputs_only: bool = False,
    delete_outputs_from_open_transactions: bool = False
) -> Dict[str, Any]:
    """
    Execute cleanup of old MarketAnalysis records and associated data.
    
    Will NOT delete analyses that have linked open transactions.
    
    Args:
        days_to_keep: Number of days to keep. Analyses older than this will be deleted.
        statuses: List of MarketAnalysisStatus values to target. If None, all statuses.
        expert_instance_id: If provided, only cleanup for this expert instance.
        outputs_only: If True, only delete outputs/recommendations (keep analyses). If False, delete all.
        delete_outputs_from_open_transactions: If True, delete outputs even from analyses with open transactions.
    
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
                orphaned_results = session.exec(
                    select(TradeActionResult).where(
                        TradeActionResult.expert_recommendation_id == None
                    )
                ).all()
                
                for orphaned in orphaned_results:
                    session.delete(orphaned)
                
                if orphaned_results:
                    logger.info(f"Cleanup: Deleted {len(orphaned_results)} orphaned trade_action_result records")
                    session.commit()
            except Exception as e:
                logger.warning(f"Cleanup: Could not clean orphaned trade_action_result records: {e}")
                session.rollback()
            
            # Step 1.5: Clean up old orphaned AnalysisOutput records (with NULL market_analysis_id)
            try:
                orphaned_outputs = session.exec(
                    select(AnalysisOutput).where(
                        AnalysisOutput.market_analysis_id == None,
                        AnalysisOutput.created_at < cutoff_date
                    )
                ).all()
                
                for orphaned_output in orphaned_outputs:
                    session.delete(orphaned_output)
                    outputs_deleted += 1
                
                if orphaned_outputs:
                    logger.info(f"Cleanup: Deleted {len(orphaned_outputs)} orphaned analysis output records")
                    session.commit()
            except Exception as e:
                logger.warning(f"Cleanup: Could not clean orphaned analysis output records: {e}")
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

            # Batch query: get all analysis IDs that have open transactions (single query)
            all_analysis_ids = [a.id for a in old_analyses]
            protected_ids = _get_analysis_ids_with_open_transactions(session, all_analysis_ids)
            logger.info(f"Cleanup: {len(protected_ids)} analyses have open transactions and will be protected")

            for analysis in old_analyses:
                # Create a new session context for each analysis to avoid rollback cascade issues
                try:
                    with get_db() as analysis_session:
                        # Re-fetch the analysis in the new session
                        analysis_obj = analysis_session.get(MarketAnalysis, analysis.id)
                        if not analysis_obj:
                            continue

                        # Check if analysis has open transactions (from batch result)
                        has_open = analysis.id in protected_ids

                        if has_open:
                            analyses_protected += 1
                            logger.debug(f"Cleanup: Protecting analysis {analysis_obj.id} (has open transaction)")
                            
                            # Optionally delete outputs from protected analyses (if configured)
                            if delete_outputs_from_open_transactions:
                                outputs_count = len(analysis_obj.analysis_outputs)
                                for output in analysis_obj.analysis_outputs:
                                    analysis_session.delete(output)
                                outputs_deleted += outputs_count
                                
                                if outputs_count > 0:
                                    logger.debug(f"Cleanup: Deleted {outputs_count} outputs from protected analysis {analysis_obj.id}")
                                
                                # Commit the output deletions
                                analysis_session.commit()
                            continue
                        
                        # Count what we're about to delete
                        outputs_count = len(analysis_obj.analysis_outputs)
                        recommendations_count = len(analysis_obj.expert_recommendations)
                        
                        # Delete in proper order to avoid constraint violations
                        # 1. First delete TradeActionResult records explicitly to avoid CASCADE issues
                        for recommendation in analysis_obj.expert_recommendations:
                            trade_results = analysis_session.exec(
                                select(TradeActionResult).where(
                                    TradeActionResult.expert_recommendation_id == recommendation.id
                                )
                            ).all()
                            for result in trade_results:
                                analysis_session.delete(result)
                        
                        # 2. Then delete expert recommendations
                        for recommendation in analysis_obj.expert_recommendations:
                            analysis_session.delete(recommendation)
                        recommendations_deleted += recommendations_count
                        
                        # 3. Delete analysis outputs
                        for output in analysis_obj.analysis_outputs:
                            analysis_session.delete(output)
                        outputs_deleted += outputs_count
                        
                        # 4. Delete the analysis itself only if not outputs_only mode
                        if not outputs_only:
                            analysis_session.delete(analysis_obj)
                            analyses_deleted += 1
                            logger.debug(f"Cleanup: Deleted analysis {analysis_obj.id} ({analysis_obj.symbol}, {analysis_obj.status.value})")
                        else:
                            # In outputs_only mode, just mark as "cleaned" by counting it
                            analyses_deleted += 1
                            logger.debug(f"Cleanup: Deleted outputs for analysis {analysis_obj.id} ({analysis_obj.symbol}, {analysis_obj.status.value})")
                        
                        # Commit this analysis's changes
                        analysis_session.commit()
                    
                except Exception as e:
                    error_msg = f"Error deleting analysis {analysis.id}: {str(e)}"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    # No need to rollback - each analysis has its own session context
                    continue
            
            cleanup_mode = "outputs only" if outputs_only else "all data"
            logger.info(f"Cleanup completed ({cleanup_mode}): {analyses_deleted} analyses processed, {analyses_protected} protected")
            
            # Step 3: Run VACUUM to reclaim disk space
            try:
                with get_db() as vacuum_session:
                    vacuum_session.exec(text("VACUUM"))
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


def _get_analysis_ids_with_open_transactions(session: Session, analysis_ids: List[int]) -> set:
    """
    Get set of MarketAnalysis IDs that have linked open transactions (batch query).

    Uses a single efficient join query instead of N+1 queries per analysis.

    Args:
        session: Database session
        analysis_ids: List of MarketAnalysis IDs to check

    Returns:
        Set of analysis IDs that have open transactions linked to them
    """
    if not analysis_ids:
        return set()

    try:
        logger.debug(f"[_get_analysis_ids_with_open_transactions] Checking {len(analysis_ids)} analyses")
        # Single efficient query: join through the chain and filter for OPENED transactions
        # MarketAnalysis -> ExpertRecommendation -> TradingOrder -> Transaction (OPENED)
        stmt = (
            select(ExpertRecommendation.market_analysis_id)
            .distinct()
            .join(TradingOrder, TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
            .join(Transaction, Transaction.id == TradingOrder.transaction_id)
            .where(
                ExpertRecommendation.market_analysis_id.in_(analysis_ids),
                Transaction.status == TransactionStatus.OPENED
            )
        )
        result = session.exec(stmt).all()
        logger.debug(f"[_get_analysis_ids_with_open_transactions] Found {len(result)} with open transactions")
        return set(result)
    except Exception as e:
        logger.error(f"Error checking open transactions for analyses: {e}", exc_info=True)
        # If we can't determine, return empty set (analyses won't be protected)
        return set()


def _has_open_transaction(session: Session, market_analysis_id: int) -> bool:
    """
    Check if a MarketAnalysis has any linked open transactions.

    NOTE: For batch operations, use _get_analysis_ids_with_open_transactions() instead
    to avoid N+1 query issues.

    Args:
        session: Database session
        market_analysis_id: ID of the MarketAnalysis to check

    Returns:
        True if there are any open transactions linked to this analysis, False otherwise
    """
    try:
        result = _get_analysis_ids_with_open_transactions(session, [market_analysis_id])
        return market_analysis_id in result
    except Exception as e:
        # If we can't determine transaction status, err on the side of caution and protect the analysis
        logger.warning(f"Could not check transaction status for analysis {market_analysis_id}: {e}")
        return True


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


def cleanup_activity_logs(days_to_keep: int = 60) -> Dict[str, Any]:
    """
    Delete activity logs older than specified days.
    
    Args:
        days_to_keep: Number of days to keep. Logs older than this will be deleted.
                     Default is 60 days.
    
    Returns:
        Dictionary with cleanup results:
        {
            'deleted_count': int,
            'error': Optional[str]
        }
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
    
    try:
        with get_db() as session:
            # Find old activity logs
            old_logs_query = select(ActivityLog).where(ActivityLog.created_at < cutoff_date)
            old_logs = session.exec(old_logs_query).all()
            
            deleted_count = len(old_logs)
            
            # Delete old logs
            for log in old_logs:
                session.delete(log)
            
            session.commit()
            
            logger.info(f"Deleted {deleted_count} activity logs older than {days_to_keep} days")
            
            return {
                'deleted_count': deleted_count,
                'error': None
            }
            
    except Exception as e:
        logger.error(f"Error cleaning up activity logs: {e}", exc_info=True)
        return {
            'deleted_count': 0,
            'error': str(e)
        }


def get_activity_log_statistics() -> Dict[str, Any]:
    """
    Get statistics about activity logs in the database.
    
    Returns:
        Dictionary with statistics:
        {
            'total_logs': int,
            'logs_by_age': {age_bucket: count},
            'logs_by_type': {type: count},
            'logs_by_severity': {severity: count}
        }
    """
    now = datetime.now(timezone.utc)
    age_buckets = {
        '7_days': now - timedelta(days=7),
        '30_days': now - timedelta(days=30),
        '60_days': now - timedelta(days=60),
        '90_days': now - timedelta(days=90),
        '180_days': now - timedelta(days=180)
    }
    
    with get_db() as session:
        # Get all activity logs
        all_logs = session.exec(select(ActivityLog)).all()
        
        # Count by age
        logs_by_age = {
            '7_days': 0,
            '30_days': 0,
            '60_days': 0,
            '90_days': 0,
            '180_days': 0,
            'older': 0
        }
        
        for log in all_logs:
            # Ensure timestamp is timezone-aware for comparison
            timestamp = _ensure_timezone_aware(log.created_at)

            if timestamp > age_buckets['7_days']:
                logs_by_age['7_days'] += 1
            elif timestamp > age_buckets['30_days']:
                logs_by_age['30_days'] += 1
            elif timestamp > age_buckets['60_days']:
                logs_by_age['60_days'] += 1
            elif timestamp > age_buckets['90_days']:
                logs_by_age['90_days'] += 1
            elif timestamp > age_buckets['180_days']:
                logs_by_age['180_days'] += 1
            else:
                logs_by_age['older'] += 1

        # Count by type
        logs_by_type = {}
        for log in all_logs:
            log_type = log.type.value if log.type else 'unknown'
            logs_by_type[log_type] = logs_by_type.get(log_type, 0) + 1
        
        # Count by severity
        logs_by_severity = {}
        for log in all_logs:
            severity = log.severity.value if log.severity else 'unknown'
            logs_by_severity[severity] = logs_by_severity.get(severity, 0) + 1
        
        return {
            'total_logs': len(all_logs),
            'logs_by_age': logs_by_age,
            'logs_by_type': logs_by_type,
            'logs_by_severity': logs_by_severity
        }
