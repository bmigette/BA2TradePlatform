#!/usr/bin/env python3
"""Test script to debug ruleset edit functionality."""

import sys
import os

# Add the project directory to Python path
project_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_dir)

from ba2_trade_platform.core.db import get_instance, get_db
from ba2_trade_platform.core.models import Ruleset, EventAction, RulesetEventActionLink
from sqlmodel import select

def test_ruleset_edit():
    """Test the ruleset edit functionality."""
    print("Testing ruleset edit functionality...")
    
    # Get a sample ruleset
    session = get_db()
    
    # Find an existing ruleset
    stmt = select(Ruleset)
    rulesets = session.exec(stmt).all()
    
    if not rulesets:
        print("No rulesets found. Creating a test ruleset...")
        # Create a test ruleset (this would normally be done through the UI)
        return
    
    ruleset = rulesets[0]
    print(f"Testing with ruleset: {ruleset.name} (ID: {ruleset.id})")
    
    # Get the current rule associations
    stmt = select(RulesetEventActionLink).where(RulesetEventActionLink.ruleset_id == ruleset.id)
    current_links = session.exec(stmt).all()
    
    print(f"Current rule associations ({len(current_links)}):")
    for link in current_links:
        rule = get_instance(EventAction, link.eventaction_id)
        print(f"  - Rule ID {link.eventaction_id}: {rule.name if rule else 'UNKNOWN'} (order: {link.order_index})")
    
    # Get all available rules
    stmt = select(EventAction)
    all_rules = session.exec(stmt).all()
    
    print(f"\nAll available rules ({len(all_rules)}):")
    for rule in all_rules:
        is_selected = any(link.eventaction_id == rule.id for link in current_links)
        print(f"  - Rule ID {rule.id}: {rule.name} {'[SELECTED]' if is_selected else ''}")
    
    session.close()

if __name__ == "__main__":
    test_ruleset_edit()