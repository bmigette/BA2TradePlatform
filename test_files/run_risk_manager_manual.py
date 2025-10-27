"""Manually trigger Smart Risk Manager for expert 14."""
import sys
sys.path.insert(0, 'c:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.utils import get_expert_instance_from_id

# Get expert 14
expert = get_expert_instance_from_id(14)
if not expert:
    print("Expert 14 not found")
    sys.exit(1)

print(f"Expert loaded")

# Get the expert instance from DB to get account_id
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import ExpertInstance

expert_instance = get_instance(ExpertInstance, 14)
print(f"Account ID: {expert_instance.account_id}")

# Create toolkit
toolkit = SmartRiskManagerToolkit(expert_instance_id=14, account_id=expert_instance.account_id)

# Get portfolio status
print("\n=== Getting Portfolio Status ===")
result = toolkit.get_portfolio_status()

print(f"\n{result}")
