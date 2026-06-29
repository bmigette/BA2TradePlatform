"""Live rules export/import (Phase 6 split-shim).

The exporter/importer live in the package (single source of truth):
``ba2_common.core.rules_export_import.{RulesExporter, RulesImporter}``.
The NiceGUI ``RulesExportImportUI`` class is live-only platform UI and stays here
(ba2_common must not depend on nicegui), re-using the package exporter/importer.
"""
import json
from typing import Dict, List, Any, Optional, Tuple  # noqa: F401  (used by UI class)
from datetime import datetime
from sqlmodel import select, Session  # noqa: F401  (used by UI class)

from .models import Ruleset, EventAction, RulesetEventActionLink  # noqa: F401
from .db import get_db, get_all_instances, add_instance, get_instance  # noqa: F401
from ..logger import logger

# Exporter/importer come from the package (extracted in Phase 0):
from ba2_common.core.rules_export_import import RulesExporter, RulesImporter  # noqa: F401


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

                async def handle_import(e):
                    try:
                        # Debug: Check what attributes the event has
                        logger.debug(f"Upload event type: {type(e)}")
                        logger.debug(f"Upload event attributes: {dir(e)}")
                        
                        # Read the uploaded file content
                        # NiceGUI upload events have a 'file' attribute
                        if hasattr(e, 'file') and e.file:
                            # e.file is an UploadFile object with async read method
                            file_content = await e.file.read()
                            if isinstance(file_content, bytes):
                                content = file_content.decode('utf-8')
                            else:
                                content = file_content
                        else:
                            logger.error(f"Cannot find file in upload event. Available attributes: {dir(e)}")
                            self.ui.notify('Upload failed: Unsupported event format', type='negative')
                            return
                            
                        data = json.loads(content)

                        # A SAVED-BACKTEST ruleset export (test platform) carries condition TREES
                        # (buy/sell/exit) and no ``export_type`` — convert it to the live rulesets
                        # export format via the shared converter so it imports like a native export.
                        if data.get('export_type') is None and any(
                            k in data for k in ('buy_entry_conditions', 'sell_entry_conditions', 'exit_conditions')
                        ):
                            # Import from rule_builders (the canonical SINGLE import point for all
                            # rules/ruleset conversion) — NOT rules_convert directly: the two modules
                            # form an intentional cycle that only resolves cleanly when rule_builders
                            # is imported first, so importing rules_convert cold here raised an
                            # ImportError (ACTION_VALUES partially initialized).
                            from ba2_common.core.rule_builders import strategy_to_live_export
                            data = strategy_to_live_export(
                                buy_tree=data.get('buy_entry_conditions'),
                                sell_tree=data.get('sell_entry_conditions'),
                                exit_rules=data.get('exit_conditions') or [],
                                name=data.get('name') or 'backtest-strategy',
                            )

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
                rulesets = list(session.scalars(statement))

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
            # Don't add timestamp suffix - preserve original names, only add -1, -2 for duplicates
            name_suffix = ""

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