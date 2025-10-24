"""Investigate expert 9 equity and TradeManager jobs #38 and #39."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import (
    ExpertInstance, AccountDefinition, Transaction, TradingOrder,
    ExpertRecommendation, SmartRiskManagerJob
)
from ba2_trade_platform.logger import logger
from sqlmodel import select
from datetime import datetime

def main():
    with get_db() as session:
        # Get expert 9
        expert = session.get(ExpertInstance, 9)
        if not expert:
            logger.error("Expert 9 not found")
            return
            
        logger.info(f"\n{'='*80}")
        logger.info(f"Expert: {expert.alias} (ID: {expert.id})")
        logger.info(f"Account ID: {expert.account_id}")
        
        # Get account
        account = session.get(AccountDefinition, expert.account_id)
        logger.info(f"Account: {account.name} (Provider: {account.provider})")
        
        # Check all transactions for expert 9 to calculate equity usage
        logger.info(f"\n{'='*80}")
        logger.info("EXPERT 9 TRANSACTION HISTORY (All Time):")
        logger.info(f"{'='*80}")
        
        transactions = session.exec(
            select(Transaction)
            .where(Transaction.expert_id == expert.id)
            .order_by(Transaction.created_at)
        ).all()
        
        total_open_value = 0.0
        
        for txn in transactions:
            status_str = txn.status.value if hasattr(txn.status, 'value') else str(txn.status)
            open_val = txn.quantity * txn.open_price if txn.quantity and txn.open_price else 0.0
            
            logger.info(
                f"  [{txn.created_at}] Txn {txn.id} ({status_str}): "
                f"{txn.symbol} qty={txn.quantity} open=${txn.open_price} = ${open_val:.2f}"
            )
            
            # Track open positions value
            if txn.status.value in ["OPEN", "WAITING"] if hasattr(txn.status, 'value') else txn.status in ["OPEN", "WAITING"]:
                total_open_value += open_val
        
        logger.info(f"\nTotal Open Positions Value: ${total_open_value:.2f}")
        
        # Check transactions specifically around 14:49 on 2025-10-24
        logger.info(f"\n{'='*80}")
        logger.info("TRANSACTIONS AROUND 2025-10-24 14:49:")
        logger.info(f"{'='*80}")
        
        target_time = datetime(2025, 10, 24, 14, 49)
        nearby_txns = session.exec(
            select(Transaction)
            .where(Transaction.expert_id == expert.id)
            .where(Transaction.created_at >= datetime(2025, 10, 24, 14, 0))
            .where(Transaction.created_at <= datetime(2025, 10, 24, 15, 0))
            .order_by(Transaction.created_at)
        ).all()
        
        if nearby_txns:
            for txn in nearby_txns:
                logger.info(
                    f"  [{txn.created_at}] Txn {txn.id}: "
                    f"{txn.symbol} qty={txn.quantity} open=${txn.open_price}"
                )
        else:
            logger.info("  No transactions found in that timeframe")
        
        # Check Smart Risk Manager jobs #38 and #39
        logger.info(f"\n{'='*80}")
        logger.info("SMART RISK MANAGER JOBS #38 and #39:")
        logger.info(f"{'='*80}")
        
        for job_id in [38, 39]:
            job = session.get(SmartRiskManagerJob, job_id)
            if job:
                logger.info(f"\nJob #{job.id}:")
                logger.info(f"  Expert Instance: {job.expert_instance_id}")
                logger.info(f"  Account ID: {job.account_id}")
                logger.info(f"  Run Date: {job.run_date}")
                logger.info(f"  Duration: {job.duration_seconds:.2f} seconds")
                logger.info(f"  Model Used: {job.model_used or 'N/A'}")
                logger.info(f"  Status: {job.status}")
                logger.info(f"  Actions Taken: {job.actions_taken_count}")
                logger.info(f"  Actions Summary: {job.actions_summary[:200] if job.actions_summary else 'None'}...")
                logger.info(f"  Error: {job.error_message or 'None'}")
            else:
                logger.info(f"\nJob #{job_id}: NOT FOUND")
        
        # Check expert settings for virtual equity allocation
        logger.info(f"\n{'='*80}")
        logger.info("EXPERT 9 SETTINGS:")
        logger.info(f"{'='*80}")
        
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents import TradingAgents
        trading_agents = TradingAgents(expert.id)
        settings = trading_agents.settings
        
        logger.info(f"  Virtual Equity Allocation: ${settings.get('virtual_equity_allocation', 'N/A')}")
        logger.info(f"  Max Position Size Percent: {settings.get('max_position_size_percent', 'N/A')}%")
        logger.info(f"  Risk Per Trade Percent: {settings.get('risk_per_trade_percent', 'N/A')}%")
        logger.info(f"  Allow Automated Trade Opening: {settings.get('allow_automated_trade_opening', 'N/A')}")

if __name__ == "__main__":
    main()
