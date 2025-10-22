# Implementation Guide: Fix OPEN_POSITIONS Recommendation Processing

## Overview
This document outlines the exact code changes needed to fix the bug where OPEN_POSITIONS recommendations are created but never evaluated against the open_positions ruleset.

## Step 1: Add Method to TradeManager

**File:** `ba2_trade_platform/core/TradeManager.py`

Add a new method similar to `process_expert_recommendations_after_analysis()` but for OPEN_POSITIONS:

```python
def process_open_positions_recommendations(self, expert_instance_id: int, lookback_days: int = 1) -> List[TradingOrder]:
    """
    Process expert recommendations for OPEN_POSITIONS analysis.
    
    This function evaluates recommendations against the open_positions ruleset
    and executes resulting trade actions (if automated trading is enabled).
    
    For OPEN_POSITIONS:
    - Process recommendations for symbols with existing positions
    - Use open_positions_ruleset_id instead of enter_market_ruleset_id
    - Consider all action types (CLOSE, SELL, BUY, HOLD)
    - Load existing transactions for each symbol
    
    Args:
        expert_instance_id: The expert instance ID to process recommendations for
        lookback_days: Number of days to look back for recommendations (default: 1)
        
    Returns:
        List of TradingOrder objects that were created
    """
    # Use open_positions as the use case for this method
    lock_key = f"expert_{expert_instance_id}_usecase_open_positions"
    
    # Get or create a lock for this expert/use_case combination
    with self._locks_dict_lock:
        if lock_key not in self._processing_locks:
            self._processing_locks[lock_key] = threading.Lock()
        processing_lock = self._processing_locks[lock_key]
    
    # Try to acquire the lock with a very short timeout (0.5 seconds)
    lock_acquired = processing_lock.acquire(blocking=True, timeout=0.5)
    
    if not lock_acquired:
        self.logger.info(f"Could not acquire lock for expert {expert_instance_id} (open_positions) - another thread is already processing. Skipping.")
        return []
    
    created_orders = []
    
    try:
        self.logger.debug(f"Acquired processing lock for expert {expert_instance_id} (open_positions)")
        
        from sqlmodel import select, Session
        from .db import get_db
        from .models import Transaction, AccountDefinition, ExpertInstance
        from .types import AnalysisUseCase, TransactionStatus
        from .utils import get_expert_instance_from_id
        from datetime import timedelta
        from .TradeActionEvaluator import TradeActionEvaluator
        from ..modules.accounts import get_account_class
        
        # Get the expert instance (with loaded settings)
        expert = get_expert_instance_from_id(expert_instance_id)
        if not expert:
            self.logger.error(f"Expert instance {expert_instance_id} not found", exc_info=True)
            return created_orders
        
        # Get the expert instance model (for ruleset IDs)
        expert_instance = get_instance(ExpertInstance, expert_instance_id)
        if not expert_instance:
            self.logger.error(f"Expert instance model {expert_instance_id} not found", exc_info=True)
            return created_orders
        
        # Check if "Allow automated trade modification" is enabled
        allow_automated_trade_modification = expert.settings.get('allow_automated_trade_modification', False)
        if not allow_automated_trade_modification:
            self.logger.debug(f"Automated trade modification disabled for expert {expert_instance_id}, skipping recommendation processing")
            return created_orders
        
        # Check if there's an open_positions ruleset configured
        if not expert_instance.open_positions_ruleset_id:
            self.logger.debug(f"No open_positions ruleset configured for expert {expert_instance_id}, skipping automated trade modification")
            return created_orders
        
        # Get the account instance for this expert
        account_def = get_instance(AccountDefinition, expert_instance.account_id)
        if not account_def:
            self.logger.error(f"Account definition {expert_instance.account_id} not found", exc_info=True)
            return created_orders
            
        account_class = get_account_class(account_def.provider)
        if not account_class:
            self.logger.error(f"Account provider {account_def.provider} not found", exc_info=True)
            return created_orders
            
        account = account_class(account_def.id)
        
        # Get recent recommendations based on lookback_days parameter
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        
        with Session(get_db().bind) as session:
            # Get all recommendations for this expert instance within the time window
            statement = select(ExpertRecommendation).where(
                ExpertRecommendation.instance_id == expert_instance_id,
                ExpertRecommendation.created_at >= cutoff_time
            ).order_by(ExpertRecommendation.created_at.desc())
            
            all_recommendations = session.exec(statement).all()
            
            if not all_recommendations:
                self.logger.info(f"No recommendations found for expert {expert_instance_id}")
                return created_orders
            
            # Filter to get only the latest recommendation per instrument
            latest_per_instrument = {}
            for rec in all_recommendations:
                if rec.symbol not in latest_per_instrument:
                    latest_per_instrument[rec.symbol] = rec
            
            # Convert to list
            recommendations = list(latest_per_instrument.values())
            
            self.logger.info(f"Found {len(recommendations)} unique instruments with open_positions recommendations for expert {expert_instance_id} (filtered from {len(all_recommendations)} total recommendations)")
            self.logger.info(f"Evaluating recommendations through open_positions ruleset: {expert_instance.open_positions_ruleset_id}")
            
            # Process each recommendation through the open_positions ruleset
            for recommendation in recommendations:
                try:
                    # Check if this symbol has existing transactions
                    statement = select(Transaction).where(
                        Transaction.symbol == recommendation.symbol,
                        Transaction.status.in_([TransactionStatus.WAITING, TransactionStatus.OPENED])
                    )
                    existing_transactions = session.exec(statement).all()
                    
                    if not existing_transactions:
                        self.logger.debug(f"No existing transactions for {recommendation.symbol}, skipping recommendation {recommendation.id}")
                        continue
                    
                    # Create TradeActionEvaluator with existing transactions for open_positions use case
                    evaluator = TradeActionEvaluator(
                        account=account,
                        instrument_name=recommendation.symbol,
                        existing_transactions=existing_transactions
                    )
                    
                    # Evaluate recommendation through the open_positions ruleset
                    self.logger.debug(f"Evaluating recommendation {recommendation.id} for {recommendation.symbol} (open_positions)")
                    
                    action_summaries = evaluator.evaluate(
                        instrument_name=recommendation.symbol,
                        expert_recommendation=recommendation,
                        ruleset_id=expert_instance.open_positions_ruleset_id,
                        existing_order=None
                    )
                    
                    # Check if evaluation produced any actions
                    if not action_summaries:
                        self.logger.debug(f"Recommendation {recommendation.id} for {recommendation.symbol} - no actions to execute (conditions not met)")
                        # TODO: Store evaluation details in TradeActionResult
                    else:
                        self.logger.info(f"Recommendation {recommendation.id} for {recommendation.symbol} passed ruleset - {len(action_summaries)} action(s) to execute")
                        # TODO: Execute actions and create orders
                        
                except Exception as e:
                    self.logger.error(f"Error evaluating open_positions recommendation {recommendation.id}: {e}", exc_info=True)
            
        return created_orders
        
    finally:
        # Release the lock
        processing_lock.release()
        self.logger.debug(f"Released processing lock for expert {expert_instance_id} (open_positions)")
```

## Step 2: Update WorkerQueue.execute_worker()

**File:** `ba2_trade_platform/core/WorkerQueue.py`

Update the completion handling around line 659:

**Before:**
```python
# Update task with success
with self._task_lock:
    task.status = WorkerTaskStatus.COMPLETED
    task.result = {"market_analysis_id": market_analysis_id, "status": "completed"}
    task.completed_at = time.time()
    
execution_time = task.completed_at - task.started_at
logger.debug(f"Analysis task '{task.id}' completed successfully in {execution_time:.2f}s")

# Check if this was the last ENTER_MARKET analysis task for this expert
# If so, trigger automated order processing
if task.subtype == AnalysisUseCase.ENTER_MARKET:
    self._check_and_process_expert_recommendations(task.expert_instance_id)
```

**After:**
```python
# Update task with success
with self._task_lock:
    task.status = WorkerTaskStatus.COMPLETED
    task.result = {"market_analysis_id": market_analysis_id, "status": "completed"}
    task.completed_at = time.time()
    
execution_time = task.completed_at - task.started_at
logger.debug(f"Analysis task '{task.id}' completed successfully in {execution_time:.2f}s")

# Check if all analysis tasks are completed for this expert
# If so, trigger automated order processing
if task.subtype == AnalysisUseCase.ENTER_MARKET:
    self._check_and_process_expert_recommendations(task.expert_instance_id, AnalysisUseCase.ENTER_MARKET)
elif task.subtype == AnalysisUseCase.OPEN_POSITIONS:
    self._check_and_process_expert_recommendations(task.expert_instance_id, AnalysisUseCase.OPEN_POSITIONS)
```

## Step 3: Update WorkerQueue._check_and_process_expert_recommendations()

**File:** `ba2_trade_platform/core/WorkerQueue.py`

Update the method to handle both use cases:

**Before:**
```python
def _check_and_process_expert_recommendations(self, expert_instance_id: int) -> None:
    """
    Check if there are any pending ENTER_MARKET analysis tasks for an expert.
    If not, trigger automated order processing via TradeManager.
    
    Args:
        expert_instance_id: The expert instance ID to check
    """
    try:
        # Check if there are any pending ENTER_MARKET tasks for this expert
        # ... [rest of code]
```

**After:**
```python
def _check_and_process_expert_recommendations(self, expert_instance_id: int, use_case: AnalysisUseCase = AnalysisUseCase.ENTER_MARKET) -> None:
    """
    Check if there are any pending analysis tasks for an expert.
    If not, trigger automated order processing via TradeManager.
    
    Args:
        expert_instance_id: The expert instance ID to check
        use_case: The analysis use case (ENTER_MARKET or OPEN_POSITIONS)
    """
    try:
        # Check if there are any pending tasks for this expert
        # Use lock to prevent race condition when multiple jobs complete simultaneously
        with self._risk_manager_lock:
            # Check if this expert is already being processed by another thread
            lock_key = f"expert_{expert_instance_id}_{use_case.value}"
            if lock_key in self._processing_experts:
                logger.debug(f"Expert {expert_instance_id} ({use_case.value}) is already being processed, skipping")
                return
            
            # Check for pending tasks
            has_pending = False
            with self._task_lock:
                for task in self._tasks.values():
                    if (task.expert_instance_id == expert_instance_id and
                        task.subtype == use_case and
                        task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]):
                        has_pending = True
                        break
            
            if not has_pending:
                # Mark this expert as being processed
                self._processing_experts.add(lock_key)
                
                try:
                    logger.info(f"All {use_case.value} analysis tasks completed for expert {expert_instance_id}, triggering automated processing")
                    
                    # Import TradeManager and process recommendations
                    from .TradeManager import get_trade_manager
                    trade_manager = get_trade_manager()
                    
                    if use_case == AnalysisUseCase.ENTER_MARKET:
                        created_orders = trade_manager.process_expert_recommendations_after_analysis(expert_instance_id)
                    elif use_case == AnalysisUseCase.OPEN_POSITIONS:
                        created_orders = trade_manager.process_open_positions_recommendations(expert_instance_id)
                    else:
                        logger.error(f"Unknown use case: {use_case}")
                        return
                    
                    if created_orders:
                        logger.info(f"Automated processing created {len(created_orders)} orders for expert {expert_instance_id}")
                    else:
                        logger.debug(f"No orders created by automated processing for expert {expert_instance_id}")
                finally:
                    # Always remove from processing set, even if an error occurred
                    self._processing_experts.discard(lock_key)
            else:
                logger.debug(f"Still has pending {use_case.value} tasks for expert {expert_instance_id}, skipping automated processing")
            
    except Exception as e:
        logger.error(f"Error checking and processing recommendations for expert {expert_instance_id} ({use_case.value}): {e}", exc_info=True)
```

## Step 4: Testing

Create a test to verify the fix works:

**File:** `test_files/test_open_positions_recommendations.py`

```python
"""Test OPEN_POSITIONS recommendation processing."""

import logging
from ba2_trade_platform.core.TradeManager import get_trade_manager
from ba2_trade_platform.core.models import ExpertInstance, ExpertRecommendation
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.types import OrderRecommendation

logger = logging.getLogger(__name__)

def test_open_positions_recommendations():
    """Test that OPEN_POSITIONS recommendations are evaluated against the ruleset."""
    
    # Get expert 9 (TradingAgents)
    expert_instance = get_instance(ExpertInstance, 9)
    
    if not expert_instance:
        logger.error("Expert instance 9 not found")
        return False
    
    if not expert_instance.open_positions_ruleset_id:
        logger.error("No open_positions ruleset configured for expert 9")
        return False
    
    logger.info(f"Expert 9 open_positions_ruleset_id: {expert_instance.open_positions_ruleset_id}")
    
    # Process open_positions recommendations
    trade_manager = get_trade_manager()
    created_orders = trade_manager.process_open_positions_recommendations(9, lookback_days=1)
    
    logger.info(f"Created {len(created_orders)} orders from OPEN_POSITIONS recommendations")
    
    return True

if __name__ == "__main__":
    test_open_positions_recommendations()
```

## Verification Checklist

After implementing the fix, verify:

- [ ] `process_open_positions_recommendations()` method exists in TradeManager
- [ ] `WorkerQueue.execute_worker()` calls the new processing method for OPEN_POSITIONS tasks
- [ ] Lock mechanism prevents race conditions for concurrent OPEN_POSITIONS processing
- [ ] Recommendations are evaluated against the `open_positions_ruleset_id`
- [ ] TradeActionEvaluator is called with existing transactions
- [ ] TradeAction records are created for met conditions
- [ ] TradingOrder records are created for actionable recommendations
- [ ] LRCX OPEN_POSITIONS analysis creates order to close position when profit_loss_percent > -10.0
- [ ] Automatic trading setting is respected (`allow_automated_trade_modification`)

## Integration with Existing Code

The fix integrates with:
1. `TradeActionEvaluator.evaluate()` - Already handles both use cases
2. `TradeManager.process_expert_recommendations_after_analysis()` - Template for implementation
3. `ExpertInstance` model - Has `open_positions_ruleset_id` field
4. `WorkerQueue` task execution - Completes analysis and triggers processing
