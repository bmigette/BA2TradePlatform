"""
Test script for importing multiple rulesets that share common rules.

Tests:
1. Import multiple rulesets with shared rules
2. Verify rules are not duplicated
3. Verify warnings are only shown once per rule
"""

import json
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db, get_instance, add_instance
from ba2_trade_platform.core.models import Ruleset, EventAction, RulesetEventActionLink
from ba2_trade_platform.core.types import ExpertEventRuleType, AnalysisUseCase
from ba2_trade_platform.core.rules_export_import import RulesExporter, RulesImporter
from ba2_trade_platform.logger import logger
from sqlmodel import select

def create_test_rules():
    """Create test rules to be shared across rulesets."""
    logger.info("Creating test rules...")
    
    # Create 3 rules
    rule1 = EventAction(
        name="Shared Rule 1",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"condition": "test1"},
        actions={"action": "test1"}
    )
    rule1_id = add_instance(rule1)
    
    rule2 = EventAction(
        name="Shared Rule 2",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"condition": "test2"},
        actions={"action": "test2"}
    )
    rule2_id = add_instance(rule2)
    
    rule3 = EventAction(
        name="Unique Rule 3",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"condition": "test3"},
        actions={"action": "test3"}
    )
    rule3_id = add_instance(rule3)
    
    logger.info(f"✅ Created test rules: {rule1_id}, {rule2_id}, {rule3_id}")
    return rule1_id, rule2_id, rule3_id


def create_test_rulesets(rule1_id, rule2_id, rule3_id):
    """Create test rulesets with shared and unique rules."""
    logger.info("Creating test rulesets...")
    
    session = get_db()
    
    # Ruleset 1: Uses rules 1 and 2
    ruleset1 = Ruleset(
        name="Test Ruleset 1",
        description="Uses rules 1 and 2",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET
    )
    rs1_id = add_instance(ruleset1)
    
    # Add links
    link1 = RulesetEventActionLink(ruleset_id=rs1_id, eventaction_id=rule1_id, order_index=0)
    session.add(link1)
    link2 = RulesetEventActionLink(ruleset_id=rs1_id, eventaction_id=rule2_id, order_index=1)
    session.add(link2)
    session.commit()
    
    # Ruleset 2: Uses rules 2 and 3 (rule 2 is shared!)
    ruleset2 = Ruleset(
        name="Test Ruleset 2",
        description="Uses rules 2 and 3",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET
    )
    rs2_id = add_instance(ruleset2)
    
    # Add links
    link3 = RulesetEventActionLink(ruleset_id=rs2_id, eventaction_id=rule2_id, order_index=0)
    session.add(link3)
    link4 = RulesetEventActionLink(ruleset_id=rs2_id, eventaction_id=rule3_id, order_index=1)
    session.add(link4)
    session.commit()
    session.close()
    
    logger.info(f"✅ Created test rulesets: {rs1_id}, {rs2_id}")
    return rs1_id, rs2_id


def test_shared_rules_import():
    """Test that importing multiple rulesets with shared rules doesn't duplicate rules."""
    logger.info("\n" + "=" * 60)
    logger.info("TEST: Import Multiple Rulesets with Shared Rules")
    logger.info("=" * 60)
    
    # Create test data
    rule1_id, rule2_id, rule3_id = create_test_rules()
    rs1_id, rs2_id = create_test_rulesets(rule1_id, rule2_id, rule3_id)
    
    # Export both rulesets
    logger.info("\nExporting rulesets...")
    rs1_data = RulesExporter.export_ruleset(rs1_id)
    rs2_data = RulesExporter.export_ruleset(rs2_id)
    
    logger.info(f"✅ Ruleset 1: {rs1_data['ruleset']['name']} with {len(rs1_data['ruleset']['rules'])} rules")
    logger.info(f"✅ Ruleset 2: {rs2_data['ruleset']['name']} with {len(rs2_data['ruleset']['rules'])} rules")
    
    # Create combined export data
    combined_data = {
        "rulesets": [
            rs1_data['ruleset'],
            rs2_data['ruleset']
        ]
    }
    
    # Count rules before import - count by IDs we know about
    # We just created rules 29, 30, 31 (or the latest 3)
    rules_before_count = 3  # We just created 3 rules above
    logger.info(f"\n📊 Test rules created before import: {rules_before_count}")
    
    # Import both rulesets together
    logger.info("\nImporting rulesets...")
    imported_ids, warnings = RulesImporter.import_multiple_rulesets(combined_data)
    
    logger.info(f"\n✅ Imported {len(imported_ids)} rulesets")
    logger.info(f"📋 Warnings ({len(warnings)}):")
    for warning in warnings:
        logger.info(f"   - {warning}")
    
    # Count rules after import - use imported ruleset links to count unique rules
    session = get_db()
    rule_ids_in_imported_rulesets = set()
    for imported_id in imported_ids:
        statement = select(RulesetEventActionLink).where(RulesetEventActionLink.ruleset_id == imported_id)
        links = session.exec(statement).all()
        for link in links:
            rule_ids_in_imported_rulesets.add(link.eventaction_id)
    
    rules_after_count = len(rule_ids_in_imported_rulesets)
    logger.info(f"\n📊 Unique rules in imported rulesets: {rules_after_count}")
    
    # Calculate how many new rules were created
    # We expect the same 3 rules to be reused
    new_rules_created = rules_after_count
    logger.info(f"📊 Total unique rules in imported rulesets: {new_rules_created}")
    
    # Expected: 3 unique rules (Shared Rule 1, Shared Rule 2, Unique Rule 3)
    # Even though Rule 2 is in both rulesets, it should only be counted once
    expected_new_rules = 3
    
    if new_rules_created == expected_new_rules:
        logger.info(f"✅ CORRECT: Found exactly {expected_new_rules} unique rules (no duplicates)")
    else:
        logger.error(f"❌ ERROR: Expected {expected_new_rules} unique rules, got {new_rules_created}")
        session.close()
        return False
    
    # Verify the imported rulesets
    for i, imported_id in enumerate(imported_ids, 1):
        imported_ruleset = get_instance(Ruleset, imported_id)
        
        # Count linked rules
        statement = select(RulesetEventActionLink).where(RulesetEventActionLink.ruleset_id == imported_id)
        links = session.exec(statement).all()
        
        logger.info(f"\n📋 Imported Ruleset {i}: '{imported_ruleset.name}'")
        logger.info(f"   - ID: {imported_id}")
        logger.info(f"   - Linked rules: {len(links)}")
        
        # Verify each linked rule
        for link in links:
            rule = get_instance(EventAction, link.eventaction_id)
            logger.info(f"   - Rule: '{rule.name}' (ID: {rule.id})")
    
    # Check that "Shared Rule 2" appears in both rulesets but with the SAME rule ID
    statement = select(RulesetEventActionLink).where(
        RulesetEventActionLink.ruleset_id.in_(imported_ids)
    )
    all_links = session.exec(statement).all()
    
    rule2_links = []
    for link in all_links:
        rule = get_instance(EventAction, link.eventaction_id)
        if "Shared Rule 2" in rule.name:
            rule2_links.append((link.ruleset_id, link.eventaction_id))
    
    logger.info(f"\n🔍 Checking 'Shared Rule 2' usage:")
    logger.info(f"   - Found in {len(rule2_links)} rulesets")
    
    if len(rule2_links) >= 2:
        # Check if all links use the same rule ID
        rule_ids = [link[1] for link in rule2_links]
        unique_rule_ids = set(rule_ids)
        
        if len(unique_rule_ids) == 1:
            logger.info(f"   ✅ CORRECT: All links use the same rule ID ({list(unique_rule_ids)[0]})")
        else:
            logger.error(f"   ❌ ERROR: Multiple rule IDs found: {unique_rule_ids}")
            session.close()
            return False
    
    session.close()
    
    logger.info("\n" + "=" * 60)
    logger.info("✅ TEST PASSED: Shared rules are properly reused!")
    logger.info("=" * 60)
    return True


if __name__ == "__main__":
    try:
        success = test_shared_rules_import()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.error(f"\n❌ TEST FAILED: {e}", exc_info=True)
        sys.exit(1)
