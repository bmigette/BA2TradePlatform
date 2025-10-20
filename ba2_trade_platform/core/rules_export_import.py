"""
Export/Import functionality for trading rules and rulesets.

This module provides functionality to export rules and rulesets to JSON files
and import them back into the system.
"""

import json
import os
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from sqlmodel import select, Session

from ..core.models import Ruleset, EventAction, RulesetEventActionLink
from ..core.db import get_db, get_all_instances, add_instance, get_instance
from ..logger import logger


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
                from .models import RulesetEventActionLink, EventAction
                
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

                # Create ruleset
                ruleset = Ruleset(
                    name=f"{ruleset_info['name']}{name_suffix}",
                    description=ruleset_info.get('description'),
                    type=ruleset_info.get('type'),
                    subtype=ruleset_info.get('subtype')
                )

                session.add(ruleset)
                session.flush()  # Get the ID

                # Import rules and create links
                for rule_data in ruleset_info["rules"]:
                    rule_id, rule_warnings = RulesImporter._import_rule_to_session(
                        session, rule_data, name_suffix
                    )
                    warnings.extend(rule_warnings)

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
                    session, rule_data["rule"], name_suffix
                )
                session.commit()
                return rule_id, warnings

        except Exception as e:
            logger.error(f"Error importing rule: {e}", exc_info=True)
            raise

    @staticmethod
    def import_multiple_rulesets(rulesets_data: Dict[str, Any], name_suffix: str = "") -> Tuple[List[int], List[str]]:
        """Import multiple rulesets. Returns (ruleset_ids, warnings)."""
        ruleset_ids = []
        all_warnings = []

        try:
            for ruleset_data in rulesets_data["rulesets"]:
                ruleset_id, warnings = RulesImporter.import_ruleset(
                    {"ruleset": ruleset_data}, name_suffix
                )
                ruleset_ids.append(ruleset_id)
                all_warnings.extend(warnings)

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
            existing_rule = session.exec(
                select(EventAction).where(EventAction.name == f"{rule_data['name']}{name_suffix}")
            ).first()

            if existing_rule:
                warnings.append(f"Rule '{rule_data['name']}{name_suffix}' already exists, skipping")
                return existing_rule.id, warnings

            # Create new rule
            rule = EventAction(
                name=f"{rule_data['name']}{name_suffix}",
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


class RulesExportImportUI:
    """UI components for export/import functionality."""

    def __init__(self):
        # Import nicegui here to avoid module-level dependency
        try:
            from nicegui import ui
            self.ui = ui
            self.export_dialog = ui.dialog()
            self.import_dialog = ui.dialog()
        except ImportError:
            # Handle case where nicegui is not available
            self.ui = None
            self.export_dialog = None
            self.import_dialog = None
        self.current_export_type = None

    def show_export_rules_dialog(self):
        """Show export dialog for rules."""
        self.current_export_type = 'rules'
        self._show_export_dialog()

    def show_export_rulesets_dialog(self):
        """Show export dialog for rulesets."""
        self.current_export_type = 'rulesets'
        self._show_export_dialog()

    def _show_export_dialog(self):
        """Show export dialog for the current export type."""
        if not self.ui:
            raise ImportError("NiceGUI not available")
            
        with self.export_dialog:
            self.export_dialog.clear()

            with self.ui.card().classes('w-full max-w-2xl'):
                title = f"Export {self.current_export_type.title()}"
                self.ui.label(title).classes('text-h6 mb-4')

                self.ui.label(f'Select {self.current_export_type} to export:').classes('mb-4')

                # Get items based on type
                if self.current_export_type == 'rules':
                    items = self._get_rules_for_export()
                    item_type = 'rule'
                else:  # rulesets
                    items = self._get_rulesets_for_export()
                    item_type = 'ruleset'

                if not items:
                    self.ui.label(f'No {self.current_export_type} available to export.').classes('text-grey-7')
                else:
                    # Selection controls row
                    with self.ui.row().classes('w-full justify-between items-center mb-2'):
                        self.ui.label(f'{len(items)} {self.current_export_type} available').classes('text-grey-7')
                        with self.ui.row().classes('gap-2'):
                            self.ui.button(
                                'Select All',
                                on_click=lambda: self._toggle_all_selections(True),
                                icon='check_box'
                            ).props('flat dense').classes('text-sm')
                            self.ui.button(
                                'Deselect All',
                                on_click=lambda: self._toggle_all_selections(False),
                                icon='check_box_outline_blank'
                            ).props('flat dense').classes('text-sm')
                    
                    # Selection checkboxes
                    self.export_selections = {}
                    with self.ui.column().classes('w-full gap-2 max-h-96 overflow-y-auto'):
                        for item in items:
                            checkbox = self.ui.checkbox(
                                f"{item['name']} ({item['description'] or 'No description'})",
                                value=False
                            )
                            self.export_selections[item['id']] = checkbox

                    # Export button
                    self.ui.button(
                        f'Export Selected {self.current_export_type.title()}',
                        on_click=self._perform_export
                    ).classes('mt-4')

        self.export_dialog.open()

    def _toggle_all_selections(self, select: bool):
        """Toggle all checkboxes in the export dialog."""
        for checkbox in self.export_selections.values():
            checkbox.value = select

    def show_import_dialog(self):
        """Show import dialog."""
        if not self.ui:
            raise ImportError("NiceGUI not available")
            
        with self.import_dialog:
            self.import_dialog.clear()

            with self.ui.card().classes('w-full max-w-lg'):
                self.ui.label('Import Rules/Rulesets').classes('text-h6 mb-4')

                self.ui.label('Select JSON file to import:').classes('mb-2')

                def handle_import(e):
                    try:
                        # Debug: Check what attributes the event has
                        logger.debug(f"Upload event type: {type(e)}")
                        logger.debug(f"Upload event attributes: {dir(e)}")
                        
                        # Read the uploaded file content
                        # Handle both old and new NiceGUI API
                        if hasattr(e, 'content'):
                            e.content.seek(0)  # Ensure we're at the beginning of the file
                            content = e.content.read().decode('utf-8')
                        elif hasattr(e, 'sender') and hasattr(e.sender, 'content'):
                            e.sender.content.seek(0)
                            content = e.sender.content.read().decode('utf-8')
                        else:
                            logger.error(f"Cannot find content in upload event. Available attributes: {dir(e)}")
                            self.ui.notify('Upload failed: Unsupported event format', type='negative')
                            return
                            
                        data = json.loads(content)

                        # Determine import type
                        import_type = data.get('export_type')
                        if import_type in ['ruleset', 'rulesets', 'rule', 'rules']:
                            self._perform_import(data)
                        else:
                            self.ui.notify('Invalid file format', type='negative')

                    except json.JSONDecodeError:
                        self.ui.notify('Invalid JSON file', type='negative')
                    except Exception as e:
                        logger.error(f"Import error: {e}", exc_info=True)
                        self.ui.notify(f'Import failed: {e}', type='negative')

                self.ui.upload(
                    label='Upload JSON file',
                    on_upload=handle_import,
                    max_files=1,
                    auto_upload=True
                ).classes('w-full')

        self.import_dialog.open()

    def _get_rules_for_export(self) -> List[Dict[str, Any]]:
        """Get rules available for export."""
        try:
            rules = get_all_instances(EventAction)
            return [
                {
                    'id': rule.id,
                    'name': rule.name,
                    'description': f"{rule.type.value if rule.type else 'No type'} - {len(rule.triggers or {})} triggers, {len(rule.actions or {})} actions"
                }
                for rule in rules
            ]
        except Exception as e:
            logger.error(f"Error getting rules for export: {e}", exc_info=True)
            return []

    def _get_rulesets_for_export(self) -> List[Dict[str, Any]]:
        """Get rulesets available for export."""
        try:
            with get_db() as session:
                from sqlalchemy.orm import selectinload
                statement = select(Ruleset).options(selectinload(Ruleset.event_actions))
                rulesets = session.exec(statement).all()

                return [
                    {
                        'id': ruleset.id,
                        'name': ruleset.name,
                        'description': f"{ruleset.subtype.value if ruleset.subtype else 'No subtype'} - {len(ruleset.event_actions or [])} rules"
                    }
                    for ruleset in rulesets
                ]
        except Exception as e:
            logger.error(f"Error getting rulesets for export: {e}", exc_info=True)
            return []

    def _perform_export(self):
        """Perform the actual export."""
        try:
            selected_ids = [
                item_id for item_id, checkbox in self.export_selections.items()
                if checkbox.value
            ]

            if not selected_ids:
                self.ui.notify('No items selected', type='warning')
                return

            # Export based on type
            if self.current_export_type == 'rules':
                if len(selected_ids) == 1:
                    data = RulesExporter.export_rule(selected_ids[0])
                else:
                    data = RulesExporter.export_multiple_rules(selected_ids)
                item_type = 'rule'
            else:  # rulesets
                if len(selected_ids) == 1:
                    data = RulesExporter.export_ruleset(selected_ids[0])
                else:
                    data = RulesExporter.export_multiple_rulesets(selected_ids)
                item_type = 'ruleset'

            # Create filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{self.current_export_type}_{timestamp}.json"

            # Download file
            json_str = json.dumps(data, indent=2)
            # Use ui.download directly from the nicegui module
            from nicegui import ui
            ui.download(json_str.encode('utf-8'), filename=filename)

            self.export_dialog.close()
            self.ui.notify(f'Exported {len(selected_ids)} {item_type}(s) successfully', type='positive')

        except Exception as e:
            logger.error(f"Export error: {e}", exc_info=True)
            self.ui.notify(f'Export failed: {e}', type='negative')

    def _perform_import(self, data: Dict[str, Any]):
        """Perform the actual import."""
        try:
            import_type = data.get('export_type')
            name_suffix = f" (Imported {datetime.now().strftime('%Y-%m-%d %H:%M')})"

            if import_type == 'ruleset':
                ruleset_id, warnings = RulesImporter.import_ruleset(data, name_suffix)
                self.ui.notify(f'Imported ruleset successfully', type='positive')
            elif import_type == 'rulesets':
                ruleset_ids, warnings = RulesImporter.import_multiple_rulesets(data, name_suffix)
                self.ui.notify(f'Imported {len(ruleset_ids)} rulesets successfully', type='positive')
            elif import_type == 'rule':
                rule_id, warnings = RulesImporter.import_rule(data, name_suffix)
                self.ui.notify(f'Imported rule successfully', type='positive')
            elif import_type == 'rules':
                rule_ids, warnings = RulesImporter.import_multiple_rules(data, name_suffix)
                self.ui.notify(f'Imported {len(rule_ids)} rules successfully', type='positive')

            # Show warnings if any
            if warnings:
                warning_msg = '\n'.join(warnings)
                self.ui.notify(f'Import completed with warnings:\n{warning_msg}', type='warning')

            # Refresh tables
            self.ui.notify('Please refresh the page to see imported items', type='info')

            self.import_dialog.close()

        except Exception as e:
            logger.error(f"Import error: {e}", exc_info=True)
            self.ui.notify(f'Import failed: {e}', type='negative')