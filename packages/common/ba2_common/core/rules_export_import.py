"""
Export/Import functionality for trading rules and rulesets.

This module provides functionality to export rules and rulesets to JSON files
and import them back into the system.
"""

import json
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from sqlmodel import select, Session

from ba2_common.core.models import Ruleset, EventAction, RulesetEventActionLink
from ba2_common.core.db import get_db, get_all_instances, add_instance, get_instance
from ba2_common.logger import logger


class RulesExporter:
    """Handles exporting rules and rulesets to JSON format."""

    @staticmethod
    def export_ruleset(ruleset_id: int) -> Dict[str, Any]:
        """Export a single ruleset with all its rules."""
        try:
            with get_db() as session:
                # Get ruleset with rules
                statement = select(Ruleset).where(Ruleset.id == ruleset_id)
                ruleset = session.exec(statement).first()

                if not ruleset:
                    raise ValueError(f"Ruleset with ID {ruleset_id} not found")

                # Get all event actions for this ruleset with their order
                ruleset_data = {
                    "export_version": "1.0",
                    "export_type": "ruleset",
                    "export_timestamp": datetime.now().isoformat(),
                    "ruleset": {
                        "name": ruleset.name,
                        "description": ruleset.description,
                        "type": ruleset.type.value if ruleset.type else None,
                        "subtype": ruleset.subtype.value if ruleset.subtype else None,
                        "rules": []
                    }
                }

                # Get rules in order by querying the link table
                from ba2_common.core.models import RulesetEventActionLink, EventAction
                
                with get_db() as session:
                    # Query link table to get ordered associations
                    statement = (
                        select(RulesetEventActionLink, EventAction)
                        .join(EventAction, RulesetEventActionLink.eventaction_id == EventAction.id)
                        .where(RulesetEventActionLink.ruleset_id == ruleset_id)
                        .order_by(RulesetEventActionLink.order_index)
                    )
                    
                    results = session.exec(statement).all()
                    
                    for link, rule in results:
                        rule_data = {
                            "name": rule.name,
                            "type": rule.type.value if rule.type else None,
                            "subtype": rule.subtype.value if rule.subtype else None,
                            "triggers": rule.triggers,
                            "actions": rule.actions,
                            "extra_parameters": rule.extra_parameters,
                            "continue_processing": rule.continue_processing,
                            "order_index": link.order_index
                        }
                        ruleset_data["ruleset"]["rules"].append(rule_data)

                return ruleset_data

        except Exception as e:
            logger.error(f"Error exporting ruleset {ruleset_id}: {e}", exc_info=True)
            raise

    @staticmethod
    def export_rule(rule_id: int) -> Dict[str, Any]:
        """Export a single rule."""
        try:
            rule = get_instance(EventAction, rule_id)
            if not rule:
                raise ValueError(f"Rule with ID {rule_id} not found")

            rule_data = {
                "export_version": "1.0",
                "export_type": "rule",
                "export_timestamp": datetime.now().isoformat(),
                "rule": {
                    "name": rule.name,
                    "type": rule.type.value if rule.type else None,
                    "subtype": rule.subtype.value if rule.subtype else None,
                    "triggers": rule.triggers,
                    "actions": rule.actions,
                    "extra_parameters": rule.extra_parameters,
                    "continue_processing": rule.continue_processing
                }
            }

            return rule_data

        except Exception as e:
            logger.error(f"Error exporting rule {rule_id}: {e}", exc_info=True)
            raise

    @staticmethod
    def export_multiple_rulesets(ruleset_ids: List[int]) -> Dict[str, Any]:
        """Export multiple rulesets."""
        try:
            rulesets_data = {
                "export_version": "1.0",
                "export_type": "rulesets",
                "export_timestamp": datetime.now().isoformat(),
                "rulesets": []
            }

            for ruleset_id in ruleset_ids:
                ruleset_data = RulesExporter.export_ruleset(ruleset_id)
                rulesets_data["rulesets"].append(ruleset_data["ruleset"])

            return rulesets_data

        except Exception as e:
            logger.error(f"Error exporting multiple rulesets: {e}", exc_info=True)
            raise

    @staticmethod
    def export_multiple_rules(rule_ids: List[int]) -> Dict[str, Any]:
        """Export multiple rules."""
        try:
            rules_data = {
                "export_version": "1.0",
                "export_type": "rules",
                "export_timestamp": datetime.now().isoformat(),
                "rules": []
            }

            for rule_id in rule_ids:
                rule_data = RulesExporter.export_rule(rule_id)
                rules_data["rules"].append(rule_data["rule"])

            return rules_data

        except Exception as e:
            logger.error(f"Error exporting multiple rules: {e}", exc_info=True)
            raise


class RulesImporter:
    """Handles importing rules and rulesets from JSON format."""

    @staticmethod
    def import_ruleset(ruleset_data: Dict[str, Any], name_suffix: str = "") -> Tuple[int, List[str]]:
        """Import a ruleset from JSON data. Returns (ruleset_id, warnings)."""
        warnings = []

        try:
            with get_db() as session:
                ruleset_info = ruleset_data["ruleset"]
                
                # Preserve original name, but handle duplicates
                base_name = ruleset_info['name']
                final_name = base_name
                
                # Check if name already exists
                counter = 1
                while True:
                    existing = session.exec(
                        select(Ruleset).where(Ruleset.name == final_name)
                    ).first()
                    
                    if not existing:
                        break
                    
                    # Name exists, try with suffix
                    final_name = f"{base_name}-{counter}"
                    counter += 1
                    warnings.append(f"Ruleset name '{base_name}' already exists, renamed to '{final_name}'")

                # Create ruleset with unique name
                ruleset = Ruleset(
                    name=final_name,
                    description=ruleset_info.get('description'),
                    type=ruleset_info.get('type'),
                    subtype=ruleset_info.get('subtype')
                )

                session.add(ruleset)
                session.flush()  # Get the ID

                # Track rules already processed in this import to avoid duplicate warnings
                processed_rule_ids = set()

                # Import rules and create links
                for rule_data in ruleset_info["rules"]:
                    rule_id, rule_warnings = RulesImporter._import_rule_to_session(
                        session, rule_data, ""  # Don't add suffix to individual rules
                    )
                    
                    # Only add warnings for rules we haven't seen yet in this import
                    if rule_id not in processed_rule_ids:
                        warnings.extend(rule_warnings)
                        processed_rule_ids.add(rule_id)

                    # Create link
                    link = RulesetEventActionLink(
                        ruleset_id=ruleset.id,
                        eventaction_id=rule_id,
                        order_index=rule_data.get('order_index', 0)
                    )
                    session.add(link)

                session.commit()
                return ruleset.id, warnings

        except Exception as e:
            logger.error(f"Error importing ruleset: {e}", exc_info=True)
            raise

    @staticmethod
    def import_rule(rule_data: Dict[str, Any], name_suffix: str = "") -> Tuple[int, List[str]]:
        """Import a single rule from JSON data. Returns (rule_id, warnings)."""
        try:
            with get_db() as session:
                rule_id, warnings = RulesImporter._import_rule_to_session(
                    session, rule_data["rule"], ""  # Don't add suffix
                )
                session.commit()
                return rule_id, warnings

        except Exception as e:
            logger.error(f"Error importing rule: {e}", exc_info=True)
            raise

    @staticmethod
    def import_multiple_rulesets(rulesets_data: Dict[str, Any], name_suffix: str = "") -> Tuple[List[int], List[str]]:
        """Import multiple rulesets, reusing rules across rulesets. Returns (ruleset_ids, warnings)."""
        ruleset_ids = []
        all_warnings = []
        processed_rule_names = {}  # Map rule names to IDs to avoid duplicates across rulesets

        try:
            with get_db() as session:
                for ruleset_data in rulesets_data["rulesets"]:
                    ruleset_info = ruleset_data
                    warnings = []
                    
                    # Preserve original name, but handle duplicates
                    base_name = ruleset_info['name']
                    final_name = base_name
                    
                    # Check if name already exists
                    counter = 1
                    while True:
                        existing = session.exec(
                            select(Ruleset).where(Ruleset.name == final_name)
                        ).first()
                        
                        if not existing:
                            break
                        
                        # Name exists, try with suffix
                        final_name = f"{base_name}-{counter}"
                        counter += 1
                        warnings.append(f"Ruleset name '{base_name}' already exists, renamed to '{final_name}'")

                    # Create ruleset with unique name
                    ruleset = Ruleset(
                        name=final_name,
                        description=ruleset_info.get('description'),
                        type=ruleset_info.get('type'),
                        subtype=ruleset_info.get('subtype')
                    )

                    session.add(ruleset)
                    session.flush()  # Get the ID
                    ruleset_ids.append(ruleset.id)

                    # Import rules and create links, reusing rules across rulesets
                    for rule_data in ruleset_info["rules"]:
                        rule_name = f"{rule_data['name']}{name_suffix}"
                        
                        # Check if we already processed this rule in current import batch
                        if rule_name in processed_rule_names:
                            rule_id = processed_rule_names[rule_name]
                        else:
                            # Import/reuse rule
                            rule_id, rule_warnings = RulesImporter._import_rule_to_session(
                                session, rule_data, name_suffix
                            )
                            processed_rule_names[rule_name] = rule_id
                            warnings.extend(rule_warnings)

                        # Create link
                        link = RulesetEventActionLink(
                            ruleset_id=ruleset.id,
                            eventaction_id=rule_id,
                            order_index=rule_data.get('order_index', 0)
                        )
                        session.add(link)
                    
                    all_warnings.extend(warnings)

                session.commit()
                return ruleset_ids, all_warnings

        except Exception as e:
            logger.error(f"Error importing multiple rulesets: {e}", exc_info=True)
            raise

    @staticmethod
    def import_multiple_rules(rules_data: Dict[str, Any], name_suffix: str = "") -> Tuple[List[int], List[str]]:
        """Import multiple rules. Returns (rule_ids, warnings)."""
        rule_ids = []
        all_warnings = []

        try:
            for rule_data in rules_data["rules"]:
                rule_id, warnings = RulesImporter.import_rule(
                    {"rule": rule_data}, name_suffix
                )
                rule_ids.append(rule_id)
                all_warnings.extend(warnings)

            return rule_ids, all_warnings

        except Exception as e:
            logger.error(f"Error importing multiple rules: {e}", exc_info=True)
            raise

    @staticmethod
    def _import_rule_to_session(session: Session, rule_data: Dict[str, Any], name_suffix: str = "") -> Tuple[int, List[str]]:
        """Import a rule within an existing session. Returns (rule_id, warnings)."""
        warnings = []

        try:
            # Check if rule with same name already exists
            rule_name = f"{rule_data['name']}{name_suffix}"
            try:
                existing_rule = session.exec(
                    select(EventAction).where(EventAction.name == rule_name)
                ).first()
            except Exception as e:
                # Handle corrupted data in database (e.g., invalid enum values from old tests)
                logger.debug(f"Error checking for existing rule '{rule_name}': {e}")
                existing_rule = None

            if existing_rule:
                warnings.append(f"Rule '{rule_name}' already exists, reusing existing rule (ID: {existing_rule.id})")
                return existing_rule.id, warnings

            # Create new rule
            rule = EventAction(
                name=rule_name,
                type=rule_data.get('type'),
                subtype=rule_data.get('subtype'),
                triggers=rule_data.get('triggers', {}),
                actions=rule_data.get('actions', {}),
                extra_parameters=rule_data.get('extra_parameters', {}),
                continue_processing=rule_data.get('continue_processing', False)
            )

            session.add(rule)
            session.flush()  # Get the ID

            return rule.id, warnings

        except Exception as e:
            logger.error(f"Error importing rule to session: {e}", exc_info=True)
            raise
