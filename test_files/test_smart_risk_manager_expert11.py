"""
Test Smart Risk Manager with Expert ID 11

This script runs the Smart Risk Manager for expert instance 11 (TradingAgents on Account 1)
with live data to test the refactored graph implementation.

Expert 11 Details:
- Expert Type: TradingAgents
- Account ID: 1 (Alpaca)
- Virtual Equity: 5%

Usage:
    .venv\Scripts\python.exe test_files\test_smart_risk_manager_expert11.py
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.SmartRiskManagerGraph import run_smart_risk_manager
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertInstance, AccountDefinition
from ba2_trade_platform.logger import logger
from sqlmodel import select


def main():
    """Run Smart Risk Manager for expert 11 with live data."""
    
    expert_instance_id = 11
    
    print("=" * 80)
    print("Smart Risk Manager Test - Expert ID 11")
    print("=" * 80)
    
    # Verify expert exists and get details
    with get_db() as session:
        expert = session.exec(
            select(ExpertInstance).where(ExpertInstance.id == expert_instance_id)
        ).first()
        
        if not expert:
            print(f"❌ ERROR: Expert instance {expert_instance_id} not found")
            return 1
        
        print(f"\n📊 Expert Details:")
        print(f"   ID: {expert.id}")
        print(f"   Type: {expert.expert}")
        print(f"   Account ID: {expert.account_id}")
        print(f"   Virtual Equity: {expert.virtual_equity_pct}%")
        
        # Get account details
        account = session.exec(
            select(AccountDefinition).where(AccountDefinition.id == expert.account_id)
        ).first()
        
        if not account:
            print(f"❌ ERROR: Account {expert.account_id} not found")
            return 1
        
        print(f"\n💼 Account Details:")
        print(f"   ID: {account.id}")
        print(f"   Name: {account.name}")
        print(f"   Provider: {account.provider}")
    
    print("\n" + "=" * 80)
    print("Starting Smart Risk Manager...")
    print("=" * 80 + "\n")
    
    try:
        # Run Smart Risk Manager
        result = run_smart_risk_manager(
            expert_instance_id=expert_instance_id,
            account_id=expert.account_id
        )
        
        print("\n" + "=" * 80)
        print("Smart Risk Manager Completed")
        print("=" * 80)
        
        # Display results
        if result.get("success"):
            print("\n✅ Status: SUCCESS")
            print(f"   Job ID: {result.get('job_id')}")
            print(f"   Iterations: {result.get('iterations')}")
            print(f"   Actions Count: {result.get('actions_count')}")
            print(f"\n📝 Summary:")
            print(f"   {result.get('summary', 'No summary available')}")
        else:
            print("\n❌ Status: FAILED")
            print(f"   Error: {result.get('error', 'Unknown error')}")
            return 1
        
        print("\n" + "=" * 80)
        print("View detailed logs in: logs/app.debug.log")
        print("=" * 80)
        
        return 0
        
    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user")
        return 130
    except Exception as e:
        print(f"\n\n❌ ERROR: {e}")
        logger.error(f"Test script error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
