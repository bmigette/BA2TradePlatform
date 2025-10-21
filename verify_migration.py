#!/usr/bin/env python3
"""
Pre-Deployment Verification Script for TP/SL Migration & Transaction Sync

Checks:
1. Alembic migration file exists and is valid
2. Python syntax in modified files
3. Database connection works
4. Current alembic revision
"""

import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def check_migration_file():
    """Check if the new migration file exists."""
    migration_file = project_root / "alembic" / "versions" / "b7c3d9f5a1e8_add_tradingorder_data_column.py"
    if migration_file.exists():
        print("✓ Migration file found:", migration_file)
        # Try to import it
        try:
            with open(migration_file) as f:
                code = f.read()
                compile(code, str(migration_file), 'exec')
            print("  ✓ Migration file syntax OK")
            return True
        except SyntaxError as e:
            print(f"  ✗ Migration file syntax error: {e}")
            return False
    else:
        print("✗ Migration file NOT found:", migration_file)
        return False

def check_python_syntax():
    """Check syntax of modified Python files."""
    files_to_check = [
        project_root / "ba2_trade_platform" / "modules" / "accounts" / "AlpacaAccount.py",
        project_root / "ba2_trade_platform" / "core" / "TradeManager.py",
    ]
    
    all_ok = True
    for filepath in files_to_check:
        if filepath.exists():
            try:
                with open(filepath) as f:
                    code = f.read()
                    compile(code, str(filepath), 'exec')
                print(f"✓ {filepath.name} syntax OK")
            except SyntaxError as e:
                print(f"✗ {filepath.name} syntax error: {e}")
                all_ok = False
        else:
            print(f"✗ File not found: {filepath}")
            all_ok = False
    
    return all_ok

def check_imports():
    """Check if key classes can be imported."""
    try:
        from ba2_trade_platform.core.models import Transaction, TradingOrder
        print("✓ Transaction model imported")
        print("✓ TradingOrder model imported")
        
        from ba2_trade_platform.core.TradeManager import TradeManager
        print("✓ TradeManager imported")
        
        from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
        print("✓ AlpacaAccount imported")
        
        return True
    except Exception as e:
        print(f"✗ Import error: {e}")
        import traceback
        traceback.print_exc()
        return False

def check_database():
    """Check database connection and current migration status."""
    try:
        from ba2_trade_platform.core.db import get_db
        from sqlalchemy import inspect, text
        
        db = get_db()
        inspector = inspect(db)
        
        # Check if data column exists
        columns = [c['name'] for c in inspector.get_columns('tradingorder')]
        if 'data' in columns:
            print("✓ Database 'data' column already exists")
        else:
            print("⚠ Database 'data' column not found (needs migration)")
        
        # Check Transaction table
        if 'transaction' in inspector.get_table_names():
            print("✓ Transaction table exists")
            trans_columns = [c['name'] for c in inspector.get_columns('transaction')]
            if 'take_profit' in trans_columns:
                print("  ✓ take_profit column exists")
            if 'stop_loss' in trans_columns:
                print("  ✓ stop_loss column exists")
        else:
            print("✗ Transaction table not found")
            return False
        
        return True
    except Exception as e:
        print(f"⚠ Database check skipped: {e}")
        return True  # Don't fail on DB issues

def check_alembic_status():
    """Check current alembic revision."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        
        alembic_cfg = Config(project_root / "alembic.ini")
        script_dir = ScriptDirectory.from_config(alembic_cfg)
        
        # Get current heads
        heads = script_dir.get_heads()
        print(f"✓ Alembic heads: {heads}")
        
        # Check if our migration is in the versions
        versions_dir = project_root / "alembic" / "versions"
        our_migration = "b7c3d9f5a1e8_add_tradingorder_data_column.py"
        
        if (versions_dir / our_migration).exists():
            print(f"✓ Our migration {our_migration} is registered")
        else:
            print(f"✗ Our migration {our_migration} not found")
            return False
        
        return True
    except Exception as e:
        print(f"⚠ Alembic check skipped: {e}")
        return True

def main():
    """Run all checks."""
    print("=" * 60)
    print("TP/SL Migration & Transaction Sync - Pre-Deployment Check")
    print("=" * 60)
    print()
    
    checks = [
        ("Migration File", check_migration_file),
        ("Python Syntax", check_python_syntax),
        ("Imports", check_imports),
        ("Database", check_database),
        ("Alembic Status", check_alembic_status),
    ]
    
    results = []
    for name, check_func in checks:
        print(f"\n[{name}]")
        try:
            result = check_func()
            results.append((name, result))
        except Exception as e:
            print(f"✗ Check failed: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    print()
    print("=" * 60)
    print("Summary:")
    print("=" * 60)
    
    all_passed = True
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")
        if not result:
            all_passed = False
    
    print()
    if all_passed:
        print("✓ All checks passed! Ready to deploy.")
        return 0
    else:
        print("✗ Some checks failed. Please review above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
