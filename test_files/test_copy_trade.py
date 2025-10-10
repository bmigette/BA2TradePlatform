"""
Test script for FMP Senate Trade Copy Trading feature
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertSetting
from sqlmodel import select

def set_copy_trade_names(expert_id: int, names: str):
    """Set copy trade names for an expert instance."""
    session = get_db()
    
    try:
        # Check if setting exists
        stmt = select(ExpertSetting).where(
            ExpertSetting.instance_id == expert_id,
            ExpertSetting.key == 'copy_trade_names'
        )
        setting = session.exec(stmt).first()
        
        if setting:
            # Update existing setting
            setting.value_str = names
            print(f"Updated copy_trade_names to: {names}")
        else:
            # Create new setting
            setting = ExpertSetting(
                instance_id=expert_id,
                key='copy_trade_names',
                value_str=names
            )
            session.add(setting)
            print(f"Created copy_trade_names setting: {names}")
        
        session.commit()
        print("✅ Setting saved successfully")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # Use command line argument
        names = sys.argv[1]
    else:
        # Default: Clear copy trade (use weighted algorithm)
        names = ""
    
    # Set copy trade for expert instance 8
    set_copy_trade_names(8, names)
    
    if names:
        print(f"\n🎯 Copy trade mode ENABLED for: {names}")
        print("   → 100% confidence, 50% expected profit")
    else:
        print(f"\n📊 Copy trade mode DISABLED")
        print("   → Using weighted algorithm")
