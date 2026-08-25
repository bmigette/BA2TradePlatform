"""
Cleanup utilities for removing old MarketAnalysis records and associated data.

This module provides functions to safely clean up old analysis data while preserving
analyses that have linked open transactions.
"""

import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from sqlmodel import Session, select, func, text
from sqlalchemy import (
    LargeBinary,
    String,
    cast,
    delete as sa_delete,
    distinct,
    literal,
    or_,
    update as sa_update,
)
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


def cleanup_activity_logs(days_to_keep: int = 60, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Delete activity logs older than specified days.

    THE SINGLE implementation of activity-log retention. Two callers: the Settings ->
    Cleanup tab (manual button) and ``main.startup_db_maintenance()`` (every boot). Do
    not grow a second one — two functions that both mean "delete activity logs older
    than N days" will drift, and the one that drifts is the one nobody is watching.

    THE BOUNDARY: strictly ``created_at < now - days_to_keep``. A row whose age is
    EXACTLY the retention period is KEPT — "keep 60 days" is a closed window.

    BULK DELETE, deliberately. This used to ``select()`` every matching row into ORM
    objects and ``session.delete()`` them one at a time; on the live database that is
    24,435 objects materialised and 24,435 statements. Acceptable behind a button a
    human presses once a year, wrong on the startup path. It is now one ``DELETE ...
    WHERE``, which also means the returned count comes from the driver's rowcount
    rather than from a list we had to build anyway.

    ``synchronize_session=False`` because the session is opened here, holds no
    ActivityLog objects and is closed immediately: there is nothing in memory to keep
    in step, and the alternative ('fetch') would re-introduce exactly the SELECT of
    every matching row that this change removes.

    No write mutex is taken. This is a single statement — SQLite serialises it on its
    own — and taking ``db._db_write_lock`` around a caller-supplied session is the
    shape that self-deadlocked production twice in 2026-08.

    Args:
        days_to_keep: Number of days to keep. Logs older than this will be deleted.
                     Default is 60 days.
        now: The instant to measure age from. Defaults to the current UTC time;
             injected by tests so the window logic is not measured against the clock.

    Returns:
        Dictionary with cleanup results:
        {
            'deleted_count': int,
            'error': Optional[str]
        }
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_date = now - timedelta(days=days_to_keep)

    try:
        with get_db() as session:
            statement = sa_delete(ActivityLog).where(ActivityLog.created_at < cutoff_date)
            result = session.execute(
                statement, execution_options={"synchronize_session": False})
            deleted_count = result.rowcount
            session.commit()

            if deleted_count is None or deleted_count < 0:
                # pysqlite always reports a DELETE rowcount; if some driver ever does
                # not, say so rather than inventing a number.
                logger.error(f"Activity log cleanup: the database driver did not report "
                             f"how many rows it deleted (rowcount={deleted_count!r})")
            else:
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


# ===========================================================================
# AGE-BASED RETENTION FOR ``trade_action_result``
#
# THE PROBLEM. ``trade_action_result`` is the largest table on the live database:
# 12,221 rows / 189.7 MB, of which 188.2 MB is one ``data`` JSON column (15.2 kB a
# row). Nothing above ever removes it on age. ``execute_cleanup`` only reaches a
# ``trade_action_result`` row through its parent ``ExpertRecommendation``, and it
# refuses to delete an analysis whose transaction is still OPENED — so a single
# long-held position pins its whole chain of records forever.
#
# THE SHAPE OF THE FIX: two INDEPENDENT windows.
#   * BLANK after a short window (default 30 days). The ``data`` payload is replaced
#     by a small self-describing sentinel. The row, its id, its foreign key, its
#     timestamp and every scalar summary column (``action_type``/``success``/
#     ``message``) survive — which is ~99% of the bytes gone for ~1% of the meaning.
#   * DELETE after a long window (default 180 days).
#
# WHY A SENTINEL AND NOT ``{}``. The recurring bug class in this codebase is an
# UNKNOWN value silently read as a confident ZERO/EMPTY. Blanking to ``{}`` or NULL
# would make "this action genuinely recorded no data" and "the payload was reclaimed
# for age" the same byte string. Those are different facts and the difference is
# diagnostically important six months later, which is exactly when someone asks.
# The sentinel needs no migration (the column is already free-form JSON), is obvious
# on sight, is greppable, and records WHEN the payload went and HOW BIG it was.
#
# Correspondingly: a row whose payload is ALREADY empty is left exactly as it is.
# Stamping a redaction marker onto ``{}`` would invent a loss that never happened and
# reclaim nothing. ``rows_empty_payload`` reports how many were skipped for that
# reason, so "nothing to do" is never confused with "did nothing".
#
# OPEN TRANSACTIONS ARE *NOT* PROTECTED HERE, DELIBERATELY. A 180-day-old action
# record attached to a still-open position is precisely the case this feature exists
# to reclaim, and the protection above is what stops it ever happening. But it is
# never silent: ``preview_trade_action_result_retention`` reports
# ``rows_to_delete_with_open_transactions`` and
# ``rows_to_blank_with_open_transactions`` so the operator sees the consequence
# BEFORE pressing the button. Nothing reconstructs an open position's state from this
# table — it is a write-only audit trail of executed actions; the position state lives
# in ``transaction``/``tradingorder``, which this code never touches.
#
# BULK, NOT ROW BY ROW. One ``UPDATE`` and one ``DELETE``, whatever the row count, and
# the counts come from the driver's ``rowcount``. The previous activity-log purge
# materialised every matching row and deleted them one at a time (1.2 s / 143 MB heap
# for 24,435 rows); converted to a bulk statement it became 0.5-0.9 s / 1 MB. Both
# statements here are driven off ``ix_trade_action_result_created_at``.
#
# SPACE DOES NOT COME BACK BY ITSELF. Freed payload bytes land on the SQLite freelist,
# not on the filesystem. Only a ``VACUUM`` — followed by ``PRAGMA
# wal_checkpoint(TRUNCATE)``, or in WAL mode the compacted image sits in the -wal and
# the filesystem never sees it — shrinks the file. That is why the numbers reported
# here are called ``payload_bytes_freed`` and not "disk space reclaimed". Blanking
# ~185 MB puts far more than ``BA2_VACUUM_MIN_FREE_MB`` (50 MB) on the freelist, so
# the NEXT boot's ``run_startup_maintenance`` will vacuum, and that is where the file
# actually shrinks.
# ===========================================================================

#: Age at which a payload is replaced by the sentinel. 30 days: long enough that the
#: rule-evaluation detail behind the magnifying-glass icon is still there while anyone
#: is still asking about a recent trade.
DEFAULT_TRADE_ACTION_RESULT_BLANK_DAYS = 30
TRADE_ACTION_RESULT_BLANK_DAYS_ENV = "BA2_TRADE_ACTION_RESULT_BLANK_DAYS"

#: Age at which the row itself goes. 180 days: half a year of "what did this expert
#: actually do", which is what the summary columns are for.
DEFAULT_TRADE_ACTION_RESULT_DELETE_DAYS = 180
TRADE_ACTION_RESULT_DELETE_DAYS_ENV = "BA2_TRADE_ACTION_RESULT_DELETE_DAYS"

#: The sentinel's keys. Underscore-prefixed so they cannot collide with a real payload
#: key, and spelled out in full so ``grep _redacted_for_age`` finds every mention —
#: the SQL below builds the same JSON as a string literal and MUST stay in step, which
#: ``tests/test_trade_action_result_retention.py`` pins.
REDACTED_AT_KEY = "_redacted_for_age"
REDACTED_ORIGINAL_BYTES_KEY = "_original_bytes"


# ---------------------------------------------------------------------------
# The sentinel vocabulary. Pure functions — no DB, no clock.
# ---------------------------------------------------------------------------

def make_redaction_sentinel(original_bytes: Optional[int],
                            when: datetime) -> Dict[str, Any]:
    """The payload a blanked row carries instead of its data.

    Args:
        original_bytes: how many bytes the discarded payload occupied, or ``None`` when
            that could not be measured. ``None``, never ``0``: "we don't know" and
            "there was nothing" are different, and this module exists because that
            distinction keeps getting lost.
        when: the instant the payload was discarded.
    """
    return {
        REDACTED_AT_KEY: _ensure_timezone_aware(when).isoformat(),
        REDACTED_ORIGINAL_BYTES_KEY: original_bytes,
    }


def is_redacted_payload(data: Any) -> bool:
    """True only for a payload this module blanked.

    ``{}``, ``None`` and every real payload are False. Any reader that branches on
    ``data`` must go through this rather than a truthiness test, because a sentinel is
    a non-empty dict and therefore truthy.
    """
    return isinstance(data, dict) and REDACTED_AT_KEY in data


def describe_redaction(data: Any) -> Optional[str]:
    """A human sentence for a redacted payload, or ``None`` if it is not one.

    ``None`` for empty and for real payloads: telling a user their data was reclaimed
    for age when it never existed is the same failure in the other direction.
    """
    if not is_redacted_payload(data):
        return None
    when = data.get(REDACTED_AT_KEY)
    size = data.get(REDACTED_ORIGINAL_BYTES_KEY)
    if isinstance(size, int):
        # Thousands separator so 15209 does not read as 15.209 at a glance.
        size_text = f"{size:,} bytes"
    else:
        size_text = "original size unknown"
    return (f"Payload removed on {when} by age-based retention "
            f"({size_text}). The row, its timestamps and its summary fields were kept.")


def payload_evaluation_details(data: Any) -> Optional[Any]:
    """The ``evaluation_details`` a payload carries, or ``None``.

    THE ONE ACCESSOR every reader of ``trade_action_result.data`` must use. Returns
    ``None`` for a redacted payload even if it somehow also carries the key, so a
    sentinel can never be rendered as a rule evaluation. Callers that want to tell
    "never had any" from "reclaimed for age" ask ``describe_redaction`` as well.
    """
    if is_redacted_payload(data):
        return None
    if not isinstance(data, dict):
        return None
    if 'evaluation_details' not in data:
        return None
    return data['evaluation_details']


def summarize_action_result_payloads(payloads) -> Dict[str, Any]:
    """Decide what a UI should show for one recommendation's / analysis's action results.

    THE ONE PLACE the four readers in ``ui/pages/marketanalysis.py`` share, so the
    "reclaimed for age" vs "never recorded any" distinction is made once and pinned by
    one set of tests instead of being re-derived (and re-broken) in four places.

    Args:
        payloads: the ``data`` values of the relevant ``TradeActionResult`` rows.

    Returns:
        ``{'evaluation_details': <first real one or None>,
           'has_evaluation_details': bool,
           'redaction_note': <sentence or None>}``

        ``redaction_note`` is set ONLY when nothing survived: if one action's payload was
        reclaimed but a sibling still carries its evaluation, the surviving detail is what
        the user wants and the redaction is noise.
    """
    details = None
    redaction = None
    for data in payloads:
        if details is None:
            found = payload_evaluation_details(data)
            if found is not None:
                details = found
        if redaction is None:
            redaction = describe_redaction(data)
    return {
        'evaluation_details': details,
        'has_evaluation_details': details is not None,
        'redaction_note': None if details is not None else redaction,
    }


# ---------------------------------------------------------------------------
# Configuration. Bad values are REFUSED, never silently defaulted.
# ---------------------------------------------------------------------------

def _refuse_retention(source: str, raw: Any) -> ValueError:
    return ValueError(
        f"invalid trade_action_result retention setting {source}: {raw!r} — expected a "
        f"whole number of days >= 1. Refusing to guess: silently falling back to a "
        f"default is how an operator who asked for 365 days quietly gets 30 and loses "
        f"eleven months of trade action history."
    )


def _resolve_days(value: Any, env_name: str, default: int) -> int:
    if value is None:
        raw = os.getenv(env_name)
        if raw is None:
            return default
        source = env_name
    else:
        raw = value
        source = env_name
    if isinstance(raw, bool):
        # bool is an int subclass; True would otherwise resolve to "keep 1 day".
        raise _refuse_retention(source, raw)
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        raise _refuse_retention(source, raw) from None
    if days < 1:
        raise _refuse_retention(source, raw)
    return days


def resolve_trade_action_result_blank_days(value: Any = None) -> int:
    """Days after which a ``trade_action_result`` payload is blanked. Explicit > env > default."""
    return _resolve_days(value, TRADE_ACTION_RESULT_BLANK_DAYS_ENV,
                         DEFAULT_TRADE_ACTION_RESULT_BLANK_DAYS)


def resolve_trade_action_result_delete_days(value: Any = None) -> int:
    """Days after which a ``trade_action_result`` ROW is deleted. Explicit > env > default."""
    return _resolve_days(value, TRADE_ACTION_RESULT_DELETE_DAYS_ENV,
                         DEFAULT_TRADE_ACTION_RESULT_DELETE_DAYS)


def _resolve_windows(blank_days: Any, delete_days: Any) -> tuple:
    """Both windows, validated against each other.

    Raises:
        ValueError: on any unusable value, and on an INVERTED pair. Deleting sooner
            than blanking is never what anyone meant, and the failure mode (rows gone
            at 30 days instead of blanked) is not recoverable.
    """
    blank = resolve_trade_action_result_blank_days(blank_days)
    delete = resolve_trade_action_result_delete_days(delete_days)
    if delete < blank:
        raise ValueError(
            f"trade_action_result retention is inverted: delete_days={delete} is "
            f"shorter than blank_days={blank}. That would DELETE rows at {delete} days "
            f"that were only meant to have their payload blanked. Refusing to run."
        )
    return blank, delete


# ---------------------------------------------------------------------------
# SQL fragments. One definition each, shared by preview and execute, so the two can
# never disagree about which rows are in scope.
# ---------------------------------------------------------------------------

_DATA_COL = TradeActionResult.__table__.c.data
_CREATED_COL = TradeActionResult.__table__.c.created_at

#: Stored JSON text forms that mean "this action recorded nothing". SQL NULL is handled
#: separately; ``'null'`` is what SQLAlchemy's JSON type writes for a Python ``None``.
_EMPTY_PAYLOAD_TEXTS = ('{}', 'null', '')


def _payload_is_empty():
    return or_(_DATA_COL.is_(None), cast(_DATA_COL, String).in_(_EMPTY_PAYLOAD_TEXTS))


def _payload_is_redacted():
    # Matches the sentinel however it was written: the SQL literal below and
    # ``json.dumps(make_redaction_sentinel(...))`` both contain the quoted key.
    return cast(_DATA_COL, String).like(f'%"{REDACTED_AT_KEY}"%')


def _payload_bytes():
    """Byte length of the stored JSON text (0 for SQL NULL), not character length."""
    return func.coalesce(func.length(cast(_DATA_COL, LargeBinary)), 0)


def _sentinel_sql(when: datetime):
    """The exact JSON TEXT a blanked row is given, as a SQL expression.

    Built in SQL rather than in Python so the whole blanking is ONE statement and the
    per-row ``_original_bytes`` is a real measurement of that row. The separators match
    ``json.dumps`` defaults so a sentinel written here and one written from Python are
    byte-identical.
    """
    return (literal('{"') + literal(REDACTED_AT_KEY) + literal('": "')
            + literal(_ensure_timezone_aware(when).isoformat())
            + literal('", "') + literal(REDACTED_ORIGINAL_BYTES_KEY) + literal('": ')
            + cast(_payload_bytes(), String)
            + literal('}'))


def _delete_scope(delete_cutoff: datetime):
    """Rows old enough to remove entirely.

    ``created_at IS NOT NULL`` is stated rather than left to SQL's NULL semantics: a row
    whose age is UNKNOWN must not be swept up as ancient, and that must be visible in
    the source, not an emergent property of three-valued logic.
    """
    return (_CREATED_COL.isnot(None)) & (_CREATED_COL < delete_cutoff)


def _blank_age_band(blank_cutoff: datetime, delete_cutoff: datetime):
    """Rows in the band between the two windows — old enough to blank, young enough to keep.

    Both boundaries are CLOSED at the young end: a row exactly ``blank_days`` old is
    KEPT WHOLE, a row exactly ``delete_days`` old is KEPT AND BLANKED. Same convention
    as ``cleanup_activity_logs``.
    """
    return ((_CREATED_COL.isnot(None))
            & (_CREATED_COL < blank_cutoff)
            & (_CREATED_COL >= delete_cutoff))


def _blank_scope(blank_cutoff: datetime, delete_cutoff: datetime):
    """Rows that will actually be rewritten: in the band, with a payload worth reclaiming,
    and not already redacted (re-measuring a sentinel would overwrite the only surviving
    record of the original size with ~90)."""
    return (_blank_age_band(blank_cutoff, delete_cutoff)
            & ~_payload_is_empty()
            & ~_payload_is_redacted())


def _count_where(session: Session, condition) -> int:
    return session.exec(
        select(func.count()).select_from(TradeActionResult).where(condition)
    ).one()


def _count_with_open_transactions(session: Session, condition) -> int:
    """How many rows in *condition* hang off a chain whose transaction is still OPENED.

    TradeActionResult -> ExpertRecommendation -> TradingOrder -> Transaction(OPENED).
    ``distinct`` because one recommendation can carry several orders and therefore reach
    the same row through several paths — counting the join rows would over-report, which
    is exactly as misleading as under-reporting.
    """
    stmt = (
        select(func.count(distinct(TradeActionResult.id)))
        .select_from(TradeActionResult)
        .join(ExpertRecommendation,
              TradeActionResult.expert_recommendation_id == ExpertRecommendation.id)
        .join(TradingOrder,
              TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
        .join(Transaction, Transaction.id == TradingOrder.transaction_id)
        .where(condition, Transaction.status == TransactionStatus.OPENED)
    )
    return session.exec(stmt).one()


def _payload_bytes_to_free(session: Session, blank_cutoff: datetime,
                           delete_cutoff: datetime, when: datetime) -> int:
    """Payload TEXT bytes the run will remove.

    NOT a file-size delta. Freed bytes go onto the SQLite freelist; only a ``VACUUM``
    (plus a WAL checkpoint) returns them to the filesystem — see the header.

    The blanking term subtracts the sentinel each row will be given, computed from the
    very expression the ``UPDATE`` will write, so preview and execute cannot drift.
    """
    deleted = session.exec(
        select(func.coalesce(func.sum(_payload_bytes()), 0))
        .select_from(TradeActionResult)
        .where(_delete_scope(delete_cutoff))
    ).one()
    sentinel_bytes = func.length(cast(_sentinel_sql(when), LargeBinary))
    blanked = session.exec(
        select(func.coalesce(func.sum(_payload_bytes() - sentinel_bytes), 0))
        .select_from(TradeActionResult)
        .where(_blank_scope(blank_cutoff, delete_cutoff))
    ).one()
    return int(deleted) + int(blanked)


def _warn_about_undated(undated: int) -> None:
    """An age that cannot be established is not an age of zero and not an age of
    infinity. Such rows are left ALONE — and said out loud, because they are rows this
    feature can never reclaim and nobody would otherwise find out."""
    if undated > 0:
        logger.warning(
            f"trade_action_result retention: {undated} row(s) have a NULL created_at. "
            f"Their age is UNKNOWN, so they were neither blanked nor deleted — they are "
            f"left untouched rather than guessed at. They will never be reclaimed by "
            f"age until their timestamp is repaired."
        )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def preview_trade_action_result_retention(
        blank_days: Any = None,
        delete_days: Any = None,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    """What ``execute_trade_action_result_retention`` would do, changing nothing.

    Args:
        blank_days: payload-blanking window. ``None`` -> env -> 30.
        delete_days: row-deletion window. ``None`` -> env -> 180.
        now: the instant to measure age from; injected by tests so window logic is not
            measured against the wall clock.

    Returns:
        dict with ``rows_to_delete``, ``rows_to_blank``, ``rows_already_redacted``,
        ``rows_empty_payload``, ``rows_undated``, the two
        ``*_with_open_transactions`` counts, ``payload_bytes_to_free``, the resolved
        windows and cutoffs, and ``error``.

    Raises:
        ValueError: on unusable or inverted configuration. A preview that quietly
            previewed a DIFFERENT configuration than the one asked for would be worse
            than no preview.
    """
    blank_days, delete_days = _resolve_windows(blank_days, delete_days)
    now = _ensure_timezone_aware(now or datetime.now(timezone.utc))
    blank_cutoff = now - timedelta(days=blank_days)
    delete_cutoff = now - timedelta(days=delete_days)

    result: Dict[str, Any] = {
        'blank_days': blank_days,
        'delete_days': delete_days,
        'blank_cutoff': blank_cutoff.isoformat(),
        'delete_cutoff': delete_cutoff.isoformat(),
        'total_rows': 0,
        'rows_to_delete': 0,
        'rows_to_blank': 0,
        'rows_already_redacted': 0,
        'rows_empty_payload': 0,
        'rows_undated': 0,
        'rows_to_delete_with_open_transactions': 0,
        'rows_to_blank_with_open_transactions': 0,
        'payload_bytes_to_free': 0,
        'error': None,
    }

    try:
        with get_db() as session:
            delete_scope = _delete_scope(delete_cutoff)
            blank_scope = _blank_scope(blank_cutoff, delete_cutoff)
            band = _blank_age_band(blank_cutoff, delete_cutoff)

            result['total_rows'] = session.exec(
                select(func.count()).select_from(TradeActionResult)).one()
            result['rows_to_delete'] = _count_where(session, delete_scope)
            result['rows_to_blank'] = _count_where(session, blank_scope)
            result['rows_already_redacted'] = _count_where(
                session, band & _payload_is_redacted())
            result['rows_empty_payload'] = _count_where(session, band & _payload_is_empty())
            result['rows_undated'] = _count_where(session, _CREATED_COL.is_(None))
            result['rows_to_delete_with_open_transactions'] = _count_with_open_transactions(
                session, delete_scope)
            result['rows_to_blank_with_open_transactions'] = _count_with_open_transactions(
                session, blank_scope)
            result['payload_bytes_to_free'] = _payload_bytes_to_free(
                session, blank_cutoff, delete_cutoff, now)

        _warn_about_undated(result['rows_undated'])
        logger.info(
            f"trade_action_result retention preview (blank >{blank_days}d, "
            f"delete >{delete_days}d): {result['rows_to_delete']} row(s) to delete "
            f"({result['rows_to_delete_with_open_transactions']} on chains with an OPEN "
            f"transaction), {result['rows_to_blank']} payload(s) to blank "
            f"({result['rows_to_blank_with_open_transactions']} on chains with an OPEN "
            f"transaction); {result['rows_already_redacted']} already redacted, "
            f"{result['rows_empty_payload']} empty, {result['rows_undated']} undated; "
            f"~{result['payload_bytes_to_free']:,} payload bytes would be freed "
            f"(to the freelist — a VACUUM is what returns them to the filesystem)"
        )
        return result
    except Exception as e:
        logger.error(f"trade_action_result retention preview failed: {e}", exc_info=True)
        result['error'] = str(e)
        return result


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

def _delete_aged_trade_action_results(session: Session, delete_cutoff: datetime) -> int:
    """ONE ``DELETE``. Returns the driver's rowcount, not a number we counted ourselves."""
    statement = sa_delete(TradeActionResult).where(_delete_scope(delete_cutoff))
    result = session.execute(statement, execution_options={"synchronize_session": False})
    session.commit()
    return _checked_rowcount(result.rowcount, "delete")


def _blank_trade_action_result_payloads(session: Session, blank_cutoff: datetime,
                                        delete_cutoff: datetime, when: datetime) -> int:
    """ONE ``UPDATE``. Returns the driver's rowcount.

    Taking the count from the driver rather than from the ``SELECT`` that chose the rows
    is the difference between "12,221 blanked" and "12,221 rows matched a query while
    the UPDATE quietly did nothing".
    """
    statement = (sa_update(TradeActionResult)
                 .where(_blank_scope(blank_cutoff, delete_cutoff))
                 .values(data=_sentinel_sql(when)))
    result = session.execute(statement, execution_options={"synchronize_session": False})
    session.commit()
    return _checked_rowcount(result.rowcount, "blank")


def _checked_rowcount(rowcount: Any, stage: str) -> int:
    """A driver that will not say how many rows it touched must SAY SO, not report 0."""
    if rowcount is None or rowcount < 0:
        raise RuntimeError(
            f"trade_action_result retention: the database driver did not report how many "
            f"rows the {stage} statement affected (rowcount={rowcount!r}). Refusing to "
            f"report a count we do not have.")
    return int(rowcount)


def execute_trade_action_result_retention(
        blank_days: Any = None,
        delete_days: Any = None,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    """Blank old ``trade_action_result`` payloads and delete very old rows.

    DELETE RUNS FIRST, and the two scopes are DISJOINT. The blank band is bounded below
    by the delete cutoff (see ``_blank_age_band``), so no row is ever both blanked and
    deleted whichever order they run in — the counts cannot double-count. The order is
    therefore a cost choice, not a correctness one: deleting first means the ``UPDATE``
    never rewrites a row that the ``DELETE`` is about to throw away. (A mutation that
    swaps the two survives the test suite for exactly this reason; it is equivalent.)

    Args:
        blank_days: payload-blanking window. ``None`` -> env -> 30.
        delete_days: row-deletion window. ``None`` -> env -> 180.
        now: the instant to measure age from; injected by tests.

    Returns:
        dict with ``rows_deleted``, ``rows_blanked``, ``rows_empty_payload``,
        ``rows_already_redacted``, ``rows_undated``, ``payload_bytes_freed``, the
        resolved windows, ``seconds`` and ``error``. ``error`` is not ``None`` when the
        run FAILED — a plain zero must never be readable as "nothing needed doing".

    Raises:
        ValueError: on unusable or inverted configuration, BEFORE anything is touched.
    """
    blank_days, delete_days = _resolve_windows(blank_days, delete_days)
    now = _ensure_timezone_aware(now or datetime.now(timezone.utc))
    blank_cutoff = now - timedelta(days=blank_days)
    delete_cutoff = now - timedelta(days=delete_days)

    result: Dict[str, Any] = {
        'blank_days': blank_days,
        'delete_days': delete_days,
        'blank_cutoff': blank_cutoff.isoformat(),
        'delete_cutoff': delete_cutoff.isoformat(),
        'rows_deleted': 0,
        'rows_blanked': 0,
        'rows_already_redacted': 0,
        'rows_empty_payload': 0,
        'rows_undated': 0,
        'payload_bytes_freed': 0,
        'seconds': 0.0,
        'error': None,
    }

    started = time.perf_counter()
    try:
        with get_db() as session:
            band = _blank_age_band(blank_cutoff, delete_cutoff)
            # Measured BEFORE the statements: the bytes of a deleted row cannot be
            # measured after it is gone.
            result['payload_bytes_freed'] = _payload_bytes_to_free(
                session, blank_cutoff, delete_cutoff, now)
            result['rows_already_redacted'] = _count_where(session, band & _payload_is_redacted())
            result['rows_empty_payload'] = _count_where(session, band & _payload_is_empty())
            result['rows_undated'] = _count_where(session, _CREATED_COL.is_(None))

            result['rows_deleted'] = _delete_aged_trade_action_results(session, delete_cutoff)
            result['rows_blanked'] = _blank_trade_action_result_payloads(
                session, blank_cutoff, delete_cutoff, now)
    except Exception as e:
        logger.error(f"trade_action_result retention failed: {e}", exc_info=True)
        result['error'] = str(e)
        result['seconds'] = time.perf_counter() - started
        return result

    result['seconds'] = time.perf_counter() - started
    _warn_about_undated(result['rows_undated'])
    logger.info(
        f"trade_action_result retention done in {result['seconds']:.2f}s: deleted "
        f"{result['rows_deleted']} row(s) older than {delete_days}d, blanked "
        f"{result['rows_blanked']} payload(s) older than {blank_days}d "
        f"({result['rows_already_redacted']} already redacted, "
        f"{result['rows_empty_payload']} had no payload, {result['rows_undated']} undated "
        f"and untouched); freed ~{result['payload_bytes_freed']:,} payload bytes to the "
        f"freelist. A VACUUM is what returns that space to the filesystem."
    )
    return result
