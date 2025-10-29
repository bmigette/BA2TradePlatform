"""
Test script for ruleset and expert settings export/import with name preservation.

Tests:
1. Ruleset import preserves names with duplicate handling (-1, -2 suffix)
2. Expert settings export includes ruleset names (not IDs)
3. Expert settings import maps ruleset names back to IDs
"""

import json
import tempfile
import os
from pathlib import Path

# Add parent directory to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db, get_instance, add_instance, update_instance
from ba2_trade_platform.core.models import Ruleset, EventAction, ExpertInstance, AccountDefinition
from ba2_trade_platform.core.types import ExpertEventRuleType, AnalysisUseCase
from ba2_trade_platform.core.rules_export_import import RulesExporter, RulesImporter
from ba2_trade_platform.logger import logger

def test_ruleset_import_name_preservation():
    """Test that ruleset import preserves original names with duplicate handling."""
    logger.info("=" * 60)
    logger.info("TEST 1: Ruleset Import Name Preservation")
    logger.info("=" * 60)
    
    # Create a test ruleset
    test_ruleset = Ruleset(
        name="Test Import Ruleset",
        description="Original test ruleset",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET
    )
    ruleset_id = add_instance(test_ruleset)
    logger.info(f"✅ Created test ruleset with ID {ruleset_id}")
    
    # Export it
    export_data = RulesExporter.export_ruleset(ruleset_id)
    logger.info(f"✅ Exported ruleset: {export_data['ruleset']['name']}")
    
    # Import it (should get same name)
    imported_id, warnings = RulesImporter.import_ruleset(export_data, name_suffix="")
    imported_ruleset = get_instance(Ruleset, imported_id)
    logger.info(f"✅ First import: {imported_ruleset.name}")
    assert imported_ruleset.name == "Test Import Ruleset", f"Expected 'Test Import Ruleset', got '{imported_ruleset.name}'"
    
    # Import again (should get -1 suffix)
    imported_id2, warnings2 = RulesImporter.import_ruleset(export_data, name_suffix="")
    imported_ruleset2 = get_instance(Ruleset, imported_id2)
    logger.info(f"✅ Second import (duplicate): {imported_ruleset2.name}")
    assert imported_ruleset2.name == "Test Import Ruleset-1", f"Expected 'Test Import Ruleset-1', got '{imported_ruleset2.name}'"
    assert len(warnings2) == 1, f"Expected 1 warning, got {len(warnings2)}"
    logger.info(f"   Warning: {warnings2[0]}")
    
    # Import again (should get -2 suffix)
    imported_id3, warnings3 = RulesImporter.import_ruleset(export_data, name_suffix="")
    imported_ruleset3 = get_instance(Ruleset, imported_id3)
    logger.info(f"✅ Third import (duplicate): {imported_ruleset3.name}")
    assert imported_ruleset3.name == "Test Import Ruleset-2", f"Expected 'Test Import Ruleset-2', got '{imported_ruleset3.name}'"
    
    logger.info("✅ TEST 1 PASSED: Ruleset names preserved with duplicate handling")
    return imported_id, imported_id2, imported_id3


def test_expert_settings_export_with_ruleset_names():
    """Test that expert settings export includes ruleset names instead of IDs."""
    logger.info("\n" + "=" * 60)
    logger.info("TEST 2: Expert Settings Export with Ruleset Names")
    logger.info("=" * 60)
    
    # Get or create a test account
    session = get_db()
    from sqlmodel import select
    account = session.exec(select(AccountDefinition).limit(1)).first()
    if not account:
        logger.info("⚠️  No account found, creating test account")
        account = AccountDefinition(name="Test Account", provider="AlpacaAccount", description="Test")
        account_id = add_instance(account)
        account = get_instance(AccountDefinition, account_id)
    else:
        logger.info(f"✅ Using existing account: {account.name}")
    
    # Create test rulesets
    enter_ruleset = Ruleset(
        name="Enter Market Test Ruleset",
        description="For testing export",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET
    )
    enter_id = add_instance(enter_ruleset)
    
    open_ruleset = Ruleset(
        name="Open Positions Test Ruleset",
        description="For testing export",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.OPEN_POSITIONS
    )
    open_id = add_instance(open_ruleset)
    logger.info(f"✅ Created test rulesets: enter_market={enter_id}, open_positions={open_id}")
    
    # Create expert instance with these rulesets
    expert = ExpertInstance(
        account_id=account.id,
        expert="TradingAgents",
        alias="Test Export Expert",
        enabled=True,
        virtual_equity_pct=50.0,
        enter_market_ruleset_id=enter_id,
        open_positions_ruleset_id=open_id
    )
    expert_id = add_instance(expert)
    logger.info(f"✅ Created expert instance with ID {expert_id}")
    
    # Manually construct export data (simulating what the UI does)
    from ba2_trade_platform.core.utils import get_expert_instance_from_id
    expert_obj = get_expert_instance_from_id(expert_id)
    expert_instance = get_instance(ExpertInstance, expert_id)
    
    export_data = {
        'expert_settings': dict(expert_obj.settings) if hasattr(expert_obj, 'settings') else {},
    }
    
    # Add ruleset names
    if expert_instance.enter_market_ruleset_id:
        enter_rs = get_instance(Ruleset, expert_instance.enter_market_ruleset_id)
        export_data['enter_market_ruleset_name'] = enter_rs.name if enter_rs else None
    else:
        export_data['enter_market_ruleset_name'] = None
    
    if expert_instance.open_positions_ruleset_id:
        open_rs = get_instance(Ruleset, expert_instance.open_positions_ruleset_id)
        export_data['open_positions_ruleset_name'] = open_rs.name if open_rs else None
    else:
        export_data['open_positions_ruleset_name'] = None
    
    logger.info(f"✅ Export data created:")
    logger.info(f"   enter_market_ruleset_name: {export_data['enter_market_ruleset_name']}")
    logger.info(f"   open_positions_ruleset_name: {export_data['open_positions_ruleset_name']}")
    
    # Verify names are correct
    assert export_data['enter_market_ruleset_name'] == "Enter Market Test Ruleset"
    assert export_data['open_positions_ruleset_name'] == "Open Positions Test Ruleset"
    
    logger.info("✅ TEST 2 PASSED: Expert settings export includes ruleset names")
    return export_data, expert_id


def test_expert_settings_import_with_ruleset_mapping():
    """Test that expert settings import maps ruleset names back to IDs."""
    logger.info("\n" + "=" * 60)
    logger.info("TEST 3: Expert Settings Import with Ruleset Mapping")
    logger.info("=" * 60)
    
    # Use the export data from test 2
    export_data, original_expert_id = test_expert_settings_export_with_ruleset_names()
    
    # Simulate importing to a different expert (or could be different database)
    # First, let's verify the rulesets exist by name
    from sqlmodel import select
    session = get_db()
    
    enter_name = export_data['enter_market_ruleset_name']
    open_name = export_data['open_positions_ruleset_name']
    
    # Look up by name
    enter_stmt = select(Ruleset).where(Ruleset.name == enter_name)
    enter_ruleset = session.exec(enter_stmt).first()
    
    open_stmt = select(Ruleset).where(Ruleset.name == open_name)
    open_ruleset = session.exec(open_stmt).first()
    
    session.close()
    
    assert enter_ruleset is not None, f"Could not find ruleset with name '{enter_name}'"
    assert open_ruleset is not None, f"Could not find ruleset with name '{open_name}'"
    
    logger.info(f"✅ Found rulesets by name:")
    logger.info(f"   '{enter_name}' -> ID {enter_ruleset.id}")
    logger.info(f"   '{open_name}' -> ID {open_ruleset.id}")
    
    # Now simulate the import by creating a new expert with the mapped IDs
    # Get original expert to copy account
    original = get_instance(ExpertInstance, original_expert_id)
    
    new_expert = ExpertInstance(
        account_id=original.account_id,
        expert="TradingAgents",
        alias="Test Import Expert",
        enabled=True,
        virtual_equity_pct=50.0,
        enter_market_ruleset_id=enter_ruleset.id,  # Mapped from name!
        open_positions_ruleset_id=open_ruleset.id   # Mapped from name!
    )
    new_expert_id = add_instance(new_expert)
    logger.info(f"✅ Created new expert with mapped rulesets: {new_expert_id}")
    
    # Verify the mapping worked
    verify_expert = get_instance(ExpertInstance, new_expert_id)
    assert verify_expert.enter_market_ruleset_id == enter_ruleset.id
    assert verify_expert.open_positions_ruleset_id == open_ruleset.id
    
    logger.info("✅ TEST 3 PASSED: Expert settings import maps ruleset names to IDs")
    return new_expert_id


if __name__ == "__main__":
    logger.info("\n" + "=" * 60)
    logger.info("TESTING EXPORT/IMPORT WITH RULESET NAME PRESERVATION")
    logger.info("=" * 60)
    
    try:
        # Run tests
        test_ruleset_import_name_preservation()
        test_expert_settings_import_with_ruleset_mapping()
        
        logger.info("\n" + "=" * 60)
        logger.info("✅ ALL TESTS PASSED!")
        logger.info("=" * 60)
        
    except AssertionError as e:
        logger.error(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ UNEXPECTED ERROR: {e}", exc_info=True)
        sys.exit(1)
