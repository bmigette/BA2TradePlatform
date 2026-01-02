"""
LLM Usage Query Functions - Data aggregation for usage tracking UI.

Provides functions to query and aggregate LLM usage data for charts and reports.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone
from sqlmodel import select, func, and_
from collections import defaultdict

from .models import LLMUsageLog, ExpertInstance
from .db import get_db


def get_usage_summary(days: int = 30) -> Dict[str, Any]:
    """
    Get summary statistics for LLM usage over the specified period.
    
    Returns:
        Dict with total_requests, total_tokens, total_input_tokens, total_output_tokens,
        estimated_cost, unique_models, unique_experts
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    with get_db() as db:
        # Get all usage logs in the period
        logs = db.exec(
            select(LLMUsageLog).where(LLMUsageLog.timestamp >= cutoff_date)
        ).all()
        
        total_requests = len(logs)
        total_tokens = sum(log.total_tokens for log in logs)
        total_input = sum(log.input_tokens for log in logs)
        total_output = sum(log.output_tokens for log in logs)
        total_cost = sum(log.estimated_cost_usd or 0 for log in logs)
        
        unique_models = len(set(log.model_selection for log in logs))
        unique_experts = len(set(log.expert_instance_id for log in logs if log.expert_instance_id))
        
        return {
            'total_requests': total_requests,
            'total_tokens': total_tokens,
            'total_input_tokens': total_input,
            'total_output_tokens': total_output,
            'estimated_cost_usd': total_cost if total_cost > 0 else None,
            'unique_models': unique_models,
            'unique_experts': unique_experts
        }


def get_usage_by_day(days: int = 30) -> List[Dict[str, Any]]:
    """
    Get token usage aggregated by day.
    
    Returns:
        List of dicts with date, total_tokens, requests
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    with get_db() as db:
        logs = db.exec(
            select(LLMUsageLog).where(LLMUsageLog.timestamp >= cutoff_date)
        ).all()
        
        # Group by date
        by_date = defaultdict(lambda: {'total_tokens': 0, 'requests': 0})
        for log in logs:
            date_str = log.timestamp.strftime('%Y-%m-%d')
            by_date[date_str]['total_tokens'] += log.total_tokens
            by_date[date_str]['requests'] += 1
        
        # Convert to list and sort
        result = [
            {'date': date, **data}
            for date, data in sorted(by_date.items())
        ]
        
        return result


def get_usage_by_model(days: int = 30, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Get token usage aggregated by model.
    
    Returns:
        List of dicts with model_selection, provider, total_tokens, requests
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    with get_db() as db:
        logs = db.exec(
            select(LLMUsageLog).where(LLMUsageLog.timestamp >= cutoff_date)
        ).all()
        
        # Group by model
        by_model = defaultdict(lambda: {'total_tokens': 0, 'requests': 0, 'provider': ''})
        for log in logs:
            by_model[log.model_selection]['total_tokens'] += log.total_tokens
            by_model[log.model_selection]['requests'] += 1
            by_model[log.model_selection]['provider'] = log.provider
        
        # Convert to list and sort by tokens
        result = [
            {'model': model, **data}
            for model, data in sorted(by_model.items(), key=lambda x: x[1]['total_tokens'], reverse=True)
        ]
        
        return result[:limit]


def get_usage_by_expert(days: int = 30, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Get token usage aggregated by expert instance.
    
    Returns:
        List of dicts with expert_instance_id, expert_type, total_tokens, requests
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    with get_db() as db:
        logs = db.exec(
            select(LLMUsageLog).where(
                and_(
                    LLMUsageLog.timestamp >= cutoff_date,
                    LLMUsageLog.expert_instance_id.is_not(None)
                )
            )
        ).all()
        
        # Group by expert
        by_expert = defaultdict(lambda: {'total_tokens': 0, 'requests': 0})
        for log in logs:
            by_expert[log.expert_instance_id]['total_tokens'] += log.total_tokens
            by_expert[log.expert_instance_id]['requests'] += 1
        
        # Get expert names
        result = []
        for expert_id, data in sorted(by_expert.items(), key=lambda x: x[1]['total_tokens'], reverse=True)[:limit]:
            expert = db.get(ExpertInstance, expert_id)
            expert_name = f"Expert {expert_id}"
            if expert:
                expert_name = f"{expert.expert} #{expert_id}"
                if expert.alias:
                    expert_name = f"{expert.alias} (#{expert_id})"
            
            result.append({
                'expert_instance_id': expert_id,
                'expert_name': expert_name,
                **data
            })
        
        return result


def get_usage_by_use_case(days: int = 30) -> List[Dict[str, Any]]:
    """
    Get token usage aggregated by use case.
    
    Returns:
        List of dicts with use_case, total_tokens, requests
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    with get_db() as db:
        logs = db.exec(
            select(LLMUsageLog).where(LLMUsageLog.timestamp >= cutoff_date)
        ).all()
        
        # Group by use case
        by_use_case = defaultdict(lambda: {'total_tokens': 0, 'requests': 0})
        for log in logs:
            by_use_case[log.use_case]['total_tokens'] += log.total_tokens
            by_use_case[log.use_case]['requests'] += 1
        
        # Convert to list and sort
        result = [
            {'use_case': use_case, **data}
            for use_case, data in sorted(by_use_case.items(), key=lambda x: x[1]['total_tokens'], reverse=True)
        ]
        
        return result


def get_usage_by_provider(days: int = 30) -> List[Dict[str, Any]]:
    """
    Get token usage aggregated by provider.
    
    Returns:
        List of dicts with provider, total_tokens, requests
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    with get_db() as db:
        logs = db.exec(
            select(LLMUsageLog).where(LLMUsageLog.timestamp >= cutoff_date)
        ).all()
        
        # Group by provider
        by_provider = defaultdict(lambda: {'total_tokens': 0, 'requests': 0})
        for log in logs:
            by_provider[log.provider]['total_tokens'] += log.total_tokens
            by_provider[log.provider]['requests'] += 1
        
        # Convert to list and sort
        result = [
            {'provider': provider, **data}
            for provider, data in sorted(by_provider.items(), key=lambda x: x[1]['total_tokens'], reverse=True)
        ]
        
        return result


def get_recent_requests(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Get recent LLM requests with details.
    
    Returns:
        List of recent requests with all details
    """
    with get_db() as db:
        logs = db.exec(
            select(LLMUsageLog).order_by(LLMUsageLog.timestamp.desc()).limit(limit)
        ).all()
        
        result = []
        for log in logs:
            result.append({
                'id': log.id,
                'timestamp': log.timestamp.isoformat(),
                'use_case': log.use_case,
                'model': log.model_selection,
                'provider': log.provider,
                'total_tokens': log.total_tokens,
                'input_tokens': log.input_tokens,
                'output_tokens': log.output_tokens,
                'duration_ms': log.duration_ms,
                'expert_instance_id': log.expert_instance_id,
                'symbol': log.symbol,
                'error': log.error
            })
        
        return result
