#!/usr/bin/env python3
"""Test script to verify the ruleset edit fix."""

import sys
import os

# Add the project directory to Python path
project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_dir)

from ba2_trade_platform.core.db import get_instance, get_db, get_all_instances
from ba2_trade_platform.core.models import Ruleset, EventAction, RulesetEventActionLink
from sqlmodel import select

def test_ruleset_edit_logic():
    """Test the logic used in the fixed ruleset edit dialog."""
    print("Testing ruleset edit logic...")
    
    # Get a sample ruleset
    session = get_db()
    
    # Find an existing ruleset
    stmt = select(Ruleset)
    rulesets = session.exec(stmt).all()
    
    if not rulesets:
        print("No rulesets found.")
        session.close()
        return
    
    ruleset = rulesets[0]
    print(f"Testing with ruleset: {ruleset.name} (ID: {ruleset.id})")
    
    # Test the FIXED logic for determining selected rules
    print("\n=== FIXED LOGIC ===")
    
    # Get currently selected rule IDs (this is the fix)
    selected_rule_ids = set()
    stmt = select(RulesetEventActionLink).where(RulesetEventActionLink.ruleset_id == ruleset.id)
    links = session.exec(stmt).all()
    selected_rule_ids = {link.eventaction_id for link in links}
    print(f"Selected rule IDs from RulesetEventActionLink: {selected_rule_ids}")
    
    # Get all available rules
    available_rules = get_all_instances(EventAction)
    print(f"\nAll available rules:")
    for rule in available_rules:
        is_selected = rule.id in selected_rule_ids
        print(f"  - Rule ID {rule.id}: {rule.name} {'[SELECTED]' if is_selected else ''}")
    
    # Test the OLD logic for comparison
    print("\n=== OLD LOGIC (potentially broken) ===")
    try:
        if ruleset.event_actions:
            old_selected_ids = [r.id for r in ruleset.event_actions]
            print(f"Selected rule IDs from ruleset.event_actions: {old_selected_ids}")
        else:
            print("ruleset.event_actions is None or empty")
    except Exception as e:
        print(f"Error accessing ruleset.event_actions: {e}")
    
    session.close()

if __name__ == "__main__":
    test_ruleset_edit_logic()