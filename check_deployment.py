#!/usr/bin/env python3
"""
Quick deployment checklist for TP/SL migration

Simple status check.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def main():
    print("\n" + "="*70)
    print("TP/SL TRANSACTION SYNC - DEPLOYMENT READY CHECK")
    print("="*70)
    
    checks_passed = 0
    checks_total = 0
    
    # Check 1: Migration file
    migration = project_root / "alembic" / "versions" / "b7c3d9f5a1e8_add_tradingorder_data_column.py"
    checks_total += 1
    if migration.exists():
        print("✓ Migration file created:  b7c3d9f5a1e8_add_tradingorder_data_column.py")
        checks_passed += 1
    else:
        print("✗ Migration file MISSING")
    
    # Check 2: AlpacaAccount updated
    alpaca_file = project_root / "ba2_trade_platform" / "modules" / "accounts" / "AlpacaAccount.py"
    checks_total += 1
    if alpaca_file.exists():
        content = alpaca_file.read_text()
        if "transaction.take_profit = tp_price" in content:
            print("✓ AlpacaAccount updated: _set_order_tp_impl syncs Transaction")
            checks_passed += 1
        else:
            print("✗ AlpacaAccount not updated")
    
    # Check 3: TradeManager updated
    trade_mgr = project_root / "ba2_trade_platform" / "core" / "TradeManager.py"
    checks_total += 1
    if trade_mgr.exists():
        content = trade_mgr.read_text()
        if "transaction.take_profit = dependent_order.limit_price" in content:
            print("✓ TradeManager updated: _check_all_waiting_trigger_orders syncs Transaction")
            checks_passed += 1
        else:
            print("✗ TradeManager not updated")
    
    # Check 4: Models have data field
    models_file = project_root / "ba2_trade_platform" / "core" / "models.py"
    checks_total += 1
    if models_file.exists():
        content = models_file.read_text()
        if 'data: dict | None' in content:
            print("✓ TradingOrder.data field present in models")
            checks_passed += 1
        else:
            print("✗ TradingOrder.data field missing")
    
    # Check 5: Documentation
    doc_file = project_root / "docs" / "MIGRATION_TP_SL_TRANSACTION_SYNC.md"
    checks_total += 1
    if doc_file.exists():
        print("✓ Deployment guide created: MIGRATION_TP_SL_TRANSACTION_SYNC.md")
        checks_passed += 1
    else:
        print("✗ Deployment guide missing")
    
    print("\n" + "-"*70)
    print(f"Status: {checks_passed}/{checks_total} checks passed")
    print("-"*70)
    
    if checks_passed == checks_total:
        print("\n✅ ALL CHECKS PASSED - READY TO DEPLOY\n")
        print("Next steps:")
        print("  1. Backup database: db.sqlite.backup")
        print("  2. Run: python migrate.py")
        print("  3. Verify: alembic current")
        print("  4. Restart: main.py\n")
        return 0
    else:
        print(f"\n❌ {checks_total - checks_passed} check(s) failed\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
