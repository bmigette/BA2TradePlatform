"""
Test script for SmartRiskManagerGraph end-to-end execution.

This test creates a controlled environment with:
1. Test expert instance with AGNC enabled
2. At least one open position to manage
3. Executes complete graph workflow
4. Validates all nodes execute correctly
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import (
    get_instance, add_instance, update_instance, delete_instance,
    get_db
)
from ba2_trade_platform.core.models import (
    AccountDefinition, ExpertInstance, ExpertSetting, 
    Transaction, TradingOrder, SmartRiskManagerJob
)
from sqlmodel import select
from ba2_trade_platform.core.types import (
    OrderDirection, TransactionStatus
)
from ba2_trade_platform.core.utils import get_account_instance_from_id, get_expert_instance_from_id
from ba2_trade_platform.core.SmartRiskManagerGraph import run_smart_risk_manager
from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit

# Test configuration
TEST_SYMBOL = "AGNC"
TEST_EXPERT_NAME = "TradingAgents"  # Use real expert type
TEST_QUANTITY = 1


def setup_test_expert() -> tuple[int, int]:
    """
    Create test expert instance with AGNC enabled and proper settings.
    Returns: (expert_instance_id, account_id)
    """
    logger.info("=" * 80)
    logger.info("SETTING UP TEST EXPERT")
    logger.info("=" * 80)
    
    # Get first available account
    with get_db() as session:
        account = session.exec(select(AccountDefinition)).first()
        if not account:
            raise Exception("No account found in database")
        account_id = account.id
        logger.info(f"Using account: {account.name} (ID: {account_id})")
    
    # Create expert instance
    expert_instance = ExpertInstance(
        account_id=account_id,
        expert=TEST_EXPERT_NAME
    )
    expert_id = add_instance(expert_instance)
    logger.info(f"Created expert instance ID: {expert_id}")
    
    # Add settings
    import json
    enabled_instruments_config = {
        TEST_SYMBOL: {"enabled": True}
    }
    
    settings = [
        ExpertSetting(instance_id=expert_id, key="enabled_instruments", value_json=enabled_instruments_config),
        ExpertSetting(instance_id=expert_id, key="enable_buy", value_json=True),
        ExpertSetting(instance_id=expert_id, key="enable_sell", value_json=True),
        ExpertSetting(instance_id=expert_id, key="max_virtual_equity_per_instrument_percent", value_float=10.0),
    ]
    
    for setting in settings:
        add_instance(setting)
    logger.info(f"Added {len(settings)} settings")
    
    logger.info(f"✓ Test expert created: ID={expert_id}, Account={account_id}, Symbol={TEST_SYMBOL}")
    return expert_id, account_id


def cleanup_test_expert(expert_id: int):
    """Delete test expert and all related data."""
    logger.info("=" * 80)
    logger.info("CLEANING UP TEST EXPERT")
    logger.info("=" * 80)
    
    with get_db() as session:
        # Delete SmartRiskManagerJobs
        jobs = session.exec(
            select(SmartRiskManagerJob).where(
                SmartRiskManagerJob.expert_instance_id == expert_id
            )
        ).all()
        for job in jobs:
            session.delete(job)
            logger.info(f"Deleted SmartRiskManagerJob ID: {job.id}")
        
        # Delete transactions
        transactions = session.exec(
            select(Transaction).where(
                Transaction.expert_id == expert_id
            )
        ).all()
        for transaction in transactions:
            session.delete(transaction)
            logger.info(f"Deleted Transaction ID: {transaction.id}")
        
        # Delete settings
        settings = session.exec(
            select(ExpertSetting).where(
                ExpertSetting.instance_id == expert_id
            )
        ).all()
        for setting in settings:
            session.delete(setting)
        logger.info(f"Deleted {len(settings)} settings")
        
        # Delete expert instance
        expert = session.exec(
            select(ExpertInstance).where(ExpertInstance.id == expert_id)
        ).first()
        if expert:
            session.delete(expert)
            logger.info(f"Deleted ExpertInstance ID: {expert_id}")
        
        session.commit()
    
    logger.info("✓ Cleanup complete")


def verify_prerequisites(expert_id: int, account_id: int) -> bool:
    """Verify all prerequisites for testing."""
    logger.info("=" * 80)
    logger.info("VERIFYING PREREQUISITES")
    logger.info("=" * 80)
    
    # Check account exists
    account = get_instance(AccountDefinition, account_id)
    if not account:
        logger.error(f"Account {account_id} not found")
        return False
    logger.info(f"✓ Account found: {account.name}")
    
    # Get account instance for balance check
    account_instance = get_account_instance_from_id(account_id)
    account_info = account_instance.get_account_info()
    logger.info(f"✓ Account info: Balance=${float(account_info.cash):.2f}, Equity=${float(account_info.equity):.2f}")
    
    # Check expert exists
    expert = get_instance(ExpertInstance, expert_id)
    if not expert:
        logger.error(f"Expert {expert_id} not found")
        return False
    logger.info(f"✓ Expert found: {expert.expert}")
    
    # Check symbol enabled
    with get_db() as session:
        setting = session.exec(
            select(ExpertSetting).where(
                ExpertSetting.instance_id == expert_id,
                ExpertSetting.key == "enabled_instruments"
            )
        ).first()
    
    if not setting or not setting.value_json or TEST_SYMBOL not in setting.value_json:
        logger.error(f"Symbol {TEST_SYMBOL} not enabled in settings: {setting.value_json if setting else 'None'}")
        return False
    logger.info(f"✓ Symbol enabled: {TEST_SYMBOL}")
    
    # Check current price
    current_price = account_instance.get_instrument_current_price(TEST_SYMBOL)
    if not current_price or current_price <= 0:
        logger.error(f"Invalid current price for {TEST_SYMBOL}: {current_price}")
        return False
    logger.info(f"✓ Current price: ${current_price:.2f}")
    
    return True


def ensure_open_position(expert_id: int, account_id: int) -> Optional[int]:
    """
    Ensure there's at least one open position for testing.
    Returns transaction_id if successful.
    """
    logger.info("=" * 80)
    logger.info("ENSURING OPEN POSITION")
    logger.info("=" * 80)
    
    # Check if there's already an open position
    with get_db() as session:
        existing = session.exec(
            select(Transaction).where(
                Transaction.expert_id == expert_id,
                Transaction.symbol == TEST_SYMBOL,
                Transaction.status == TransactionStatus.OPENED
            )
        ).first()
        
        if existing:
            logger.info(f"✓ Found existing open position: Transaction {existing.id}")
            return existing.id
    
    # Create new position using toolkit
    logger.info(f"Creating new position for {TEST_SYMBOL}...")
    toolkit = SmartRiskManagerToolkit(expert_id, account_id)
    
    # Get current price
    account_instance = get_account_instance_from_id(account_id)
    current_price = account_instance.get_instrument_current_price(TEST_SYMBOL)
    
    if not current_price:
        logger.error("Cannot get current price")
        return None
    
    # Open position
    logger.info(f"Expert instance settings: {toolkit.expert.settings}")
    logger.info(f"Expert enabled instruments: {toolkit.expert.get_enabled_instruments()}")
    
    result = toolkit.open_new_position(
        symbol=TEST_SYMBOL,
        direction="BUY",
        quantity=TEST_QUANTITY,
        reason=f"Test position for SmartRiskGraph at ${current_price:.2f}"
    )
    
    logger.info(f"open_new_position result: {result}")
    
    if result.get("success"):
        transaction_id = result["transaction_id"]
        logger.info(f"✓ Created new position: Transaction {transaction_id}")
        return transaction_id
    else:
        logger.error(f"Failed to create position: {result.get('message', 'Unknown error')}")
        return None


def test_smart_risk_manager_execution(expert_id: int, account_id: int):
    """Execute complete SmartRiskManager graph and validate results."""
    logger.info("=" * 80)
    logger.info("TESTING SMART RISK MANAGER GRAPH")
    logger.info("=" * 80)
    
    # Ensure we have a position to manage
    transaction_id = ensure_open_position(expert_id, account_id)
    if not transaction_id:
        logger.error("Cannot proceed without open position")
        return False
    
    # Check if we have required settings for the expert
    expert = get_instance(ExpertInstance, expert_id)
    if not expert:
        logger.error(f"Expert {expert_id} not found")
        return False
    
    # Add risk manager settings if not present
    with get_db() as session:
        # Check if risk_manager_model exists
        rm_model_setting = session.exec(
            select(ExpertSetting).where(
                ExpertSetting.instance_id == expert_id,
                ExpertSetting.key == "risk_manager_model"
            )
        ).first()
        
        if not rm_model_setting:
            logger.info("Adding risk_manager_model setting")
            rm_setting = ExpertSetting(
                instance_id=expert_id,
                key="risk_manager_model",
                value_str="NagaAI/gpt-4o-mini"  # Using NagaAI for testing
            )
            session.add(rm_setting)
            session.commit()
    
    # Execute the graph
    logger.info("\n" + "=" * 80)
    logger.info("EXECUTING GRAPH...")
    logger.info("=" * 80 + "\n")
    
    try:
        result = run_smart_risk_manager(
            expert_instance_id=expert_id,
            account_id=account_id
        )
        
        logger.info("\n" + "=" * 80)
        logger.info("GRAPH EXECUTION COMPLETE")
        logger.info("=" * 80)
        
        # Validate results
        if result["success"]:
            logger.info(f"✓ Graph completed successfully")
            logger.info(f"  Job ID: {result['job_id']}")
            logger.info(f"  Iterations: {result['iterations_completed']}")
            logger.info(f"  Actions taken: {result['actions_count']}")
            logger.info(f"\nSummary:\n{result['summary']}")
            
            # Check job record
            job = get_instance(SmartRiskManagerJob, result["job_id"])
            if job:
                logger.info(f"\n✓ Job record found:")
                logger.info(f"  Status: {job.status}")
                logger.info(f"  Created: {job.created_at}")
                logger.info(f"  Completed: {job.completed_at}")
                
                if job.graph_state:
                    state = job.graph_state
                    logger.info(f"  Portfolio positions: {len(state.get('open_positions', []))}")
                    logger.info(f"  Actions logged: {len(state.get('actions_log', []))}")
            
            return True
        else:
            logger.error(f"✗ Graph failed: {result.get('error')}")
            return False
            
    except Exception as e:
        logger.error(f"✗ Exception during graph execution: {e}", exc_info=True)
        return False


def main():
    """Main test execution."""
    expert_id = None
    
    try:
        # Setup
        expert_id, account_id = setup_test_expert()
        
        # Verify prerequisites
        if not verify_prerequisites(expert_id, account_id):
            logger.error("Prerequisites check failed")
            return
        
        # Run test
        success = test_smart_risk_manager_execution(expert_id, account_id)
        
        # Report
        logger.info("\n" + "=" * 80)
        if success:
            logger.info("✓ ALL TESTS PASSED")
        else:
            logger.info("✗ TESTS FAILED")
        logger.info("=" * 80)
        
    except Exception as e:
        logger.error(f"Test failed with exception: {e}", exc_info=True)
    
    finally:
        # Cleanup
        if expert_id:
            cleanup_choice = input("\nCleanup test expert? (y/n): ").strip().lower()
            if cleanup_choice == 'y':
                cleanup_test_expert(expert_id)
            else:
                logger.info(f"Test expert preserved: ID={expert_id}")


if __name__ == "__main__":
    main()
