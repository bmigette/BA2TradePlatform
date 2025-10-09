import logging
import pandas as pd
from nicegui import ui
from typing import Optional
from sqlmodel import select


from ...core.models import AccountDefinition, AccountSetting, AppSetting, Instrument, ExpertInstance, EventAction, Ruleset
from ...logger import logger
from ...core.db import get_db, get_all_instances, delete_instance, add_instance, update_instance, get_instance
from ...modules.accounts import providers
from ...core.interfaces import AccountInterface
from ...core.types import InstrumentType, ExpertEventRuleType, ExpertEventType, ExpertActionType, ReferenceValue, is_numeric_event, is_adjustment_action, is_share_adjustment_action, AnalysisUseCase, MarketAnalysisStatus
from ...core.cleanup import preview_cleanup, execute_cleanup, get_cleanup_statistics
from yahooquery import Ticker, search as yq_search
from nicegui.events import UploadEventArguments
from ...modules.experts import experts
from ..components.InstrumentSelector import InstrumentSelector
from concurrent.futures import ThreadPoolExecutor, as_completed
from ...core.rules_export_import import RulesExportImportUI
from ...core.rules_documentation import get_event_type_documentation, get_action_type_documentation

# --- InstrumentSettingsTab ---
class InstrumentSettingsTab:
    """
    UI tab for managing instruments (Instrument SQL Model).
    Features: table with filter, fetch info, import, and add instrument.
    """
    def __init__(self):
        logger.debug('Initializing InstrumentSettingsTab')
        self.filter_text = ''
        self.render()

    def render(self):
        logger.debug('Rendering InstrumentSettingsTab UI')
        with ui.card().classes('w-full'):
            ui.label('Instrument Management')
            with ui.row():
                filter_input = ui.input(label='Filter') #, on_change=self.on_filter_change)
                ui.button('Fetch Info', on_click=self.fetch_info)
                ui.button('Import', on_click=self.import_instruments)
                ui.button('Add Instrument', on_click=lambda: self.add_instrument_dialog())
            self.table = ui.table(
                columns=[
                    {'name': 'checkbox', 'label': '', 'field': 'checkbox', 'type': 'checkbox', 'sortable': False},
                    {'name': 'id', 'label': 'ID', 'field': 'id'},
                    {'name': 'name', 'label': 'Name', 'field': 'name'},
                    {'name': 'company_name', 'label': 'Company', 'field': 'company_name'},
                    {'name': 'instrument_type', 'label': 'Type', 'field': 'instrument_type'},
                    {'name': 'categories', 'label': 'Categories', 'field': 'categories'},
                    {'name': 'labels', 'label': 'Labels', 'field': 'labels'},
                    {'name': 'actions', 'label': 'Actions', 'field': 'actions'},
                ],
                rows=self._get_all_instruments(),
                row_key='id',
                selection='multiple',
            ).classes('w-full')
            self.table.add_slot('body-cell-actions', """
                <q-td :props="props">
                    <q-btn @click="$parent.$emit('edit', props)" icon="edit" flat dense color='blue'/>
                    <q-btn @click="$parent.$emit('del', props)" icon="delete" flat dense color='red'/>
                </q-td>
            """)
            self.table.on('edit', self._on_table_edit_click)
            self.table.on('del', self._on_table_del_click)
            filter_input.bind_value(self.table, "filter")
        logger.debug('InstrumentSettingsTab UI rendered')

    def _get_all_instruments(self):
        logger.debug('Fetching all instruments for table')
        r = []
        for instrument in get_all_instances(Instrument):
            inst = dict(instrument)
            inst['categories'] = ", ".join(instrument.categories) if instrument.categories else ""
            inst['labels'] = ", ".join(instrument.labels) if instrument.labels else ""
            r.append(inst)
        logger.debug(f'Fetched {len(r)} instruments')
        return r
    
    def _update_table_rows(self):
        logger.debug('Updating instrument table rows')
        if self.table:
            instruments = self._get_all_instruments()
            self.table.rows = instruments
            logger.debug('Instrument table rows updated')



    def _ensure_list_field(self, instrument, field_name):
        """Ensure a field is initialized as a list."""
        current_value = getattr(instrument, field_name)
        if current_value is None:
            setattr(instrument, field_name, [])
            return []
        return current_value
    
    def _add_to_list_field(self, instrument, field_name, value, session):
        """Add a value to a list field if not already present, using SQLModel-compatible approach."""
        current_list = self._ensure_list_field(instrument, field_name)
        if value not in current_list:
            # Create new list to force SQLModel to detect change
            new_list = list(current_list)
            new_list.append(value)
            setattr(instrument, field_name, new_list)
            session.add(instrument)
            return True
        return False

    async def fetch_info(self):
        logger.debug('Fetching info for all instruments (batch mode)')
        session = get_db()
        statement = select(Instrument)
        results = session.exec(statement)
        instruments = results.all()
        session.close()

        if not instruments:
            logger.info('No instruments found to fetch info for.')
            ui.notify('No instruments found.', type='warning')
            return

        symbol_to_instrument = {inst.name: inst for inst in instruments}
        symbols = list(symbol_to_instrument.keys())
        updated = 0
        errors = 0

        try:
            ticker = Ticker(symbols)
            profiles = ticker.asset_profile
            prices = ticker.price

            for symbol, instrument in symbol_to_instrument.items():
                local_session = get_db()
                db_instrument = local_session.get(Instrument, instrument.id)
                updated_flag = False
                try:
                    profile_data = profiles.get(symbol.upper())
                    if profile_data:
                        sector = profile_data.get("sector")
                        if sector:
                            if self._add_to_list_field(db_instrument, 'categories', sector, local_session):
                                logger.debug(f'Added sector {sector} to instrument {symbol}')
                                local_session.commit()
                                updated_flag = True
                        else:
                            logger.debug(f'No sector found for instrument {symbol}')
                    else:
                        self._add_to_list_field(db_instrument, 'labels', 'not_found', local_session)
                        logger.warning(f'Instrument {symbol} not found in asset profile')
                        local_session.commit()

                    price_info = prices.get(symbol.upper())
                    longname = price_info.get("longName") if price_info else None
                    if longname:
                        db_instrument.company_name = longname
                        local_session.add(db_instrument)
                        local_session.commit()
                        logger.debug(f'Updated company_name for {symbol}: {longname}')
                        updated_flag = True

                    if updated_flag:
                        if db_instrument.labels and 'not_found' in db_instrument.labels:
                            labels = list(db_instrument.labels)
                            labels.remove('not_found')
                            db_instrument.labels = labels
                            local_session.add(db_instrument)
                            local_session.commit()
                        updated += 1
                except Exception as e:
                    self._add_to_list_field(db_instrument, 'labels', 'not_found', local_session)
                    logger.error(f"Error fetching info for {symbol}: {e}", exc_info=True)
                    local_session.commit()
                    errors += 1
                finally:
                    local_session.close()
        except Exception as e:
            logger.error(f"Error fetching batch info: {e}", exc_info=True)
            ui.notify(f'Error fetching batch info: {e}', type='negative')
            return

        logger.info(f'Fetched info for {updated} instruments. Errors: {errors}')
        ui.notify(f'Fetched info for {updated} instruments. Errors: {errors}', type='positive' if errors == 0 else 'warning')
        self._update_table_rows()

    def import_instruments(self):
        logger.debug('Opening import instruments dialog')
        
        if not hasattr(self, 'import_dialog'):
            self.import_dialog = ui.dialog()
        
        self.import_dialog.clear()
        
        with self.import_dialog:
            with ui.card().classes('w-full max-w-lg'):
                ui.label('Import Instruments').classes('text-h6 mb-4')
                
                # Labels input
                self.import_labels_input = ui.input(
                    label='Labels (comma separated)',
                    placeholder='e.g. tech, growth, blue-chip'
                ).classes('w-full mb-4')
                
                ui.label('Select file with instrument symbols (one per line):').classes('mb-2')
                
                def handle_upload(e: UploadEventArguments):
                    try:
                        # Read content from the uploaded file
                        e.content.seek(0)  # Ensure we're at the beginning of the file
                        content = e.content.read().decode('utf-8')
                        names = [line.strip() for line in content.splitlines() if line.strip()]
                        
                        # Parse labels
                        labels = []
                        if self.import_labels_input.value:
                            labels = [label.strip() for label in self.import_labels_input.value.split(',') if label.strip()]
                        
                        session = get_db()
                        added = 0
                        updated = 0
                        
                        # Get existing instruments
                        existing_instruments = {inst.name: inst for inst in session.exec(select(Instrument)).all()}
                        
                        for name in names:
                            if name in existing_instruments:
                                # Instrument exists, add labels if any specified
                                if labels:
                                    instrument = existing_instruments[name]
                                    existing_labels = instrument.labels or []
                                    
                                    # Add new labels that don't already exist
                                    new_labels = []
                                    for label in labels:
                                        if label not in existing_labels:
                                            new_labels.append(label)
                                    
                                    if new_labels:
                                        # Create a new list to force SQLAlchemy to detect the change
                                        instrument.labels = existing_labels + new_labels
                                        # Mark the field as modified for SQLAlchemy
                                        from sqlalchemy.orm import attributes
                                        attributes.flag_modified(instrument, 'labels')
                                        update_instance(instrument, session)
                                        updated += 1
                                        logger.debug(f'Added labels {new_labels} to existing instrument {name} (total labels: {instrument.labels})')
                            else:
                                # Create new instrument
                                inst = Instrument(
                                    name=name, 
                                    instrument_type='stock', 
                                    categories=[], 
                                    labels=labels.copy()
                                )
                                add_instance(inst, session)
                                added += 1
                                logger.debug(f'Added new instrument {name} with labels {labels}')
                        
                        session.commit()
                        session.close()
                        
                        logger.info(f'Import completed: {added} new instruments added, {updated} existing instruments updated with labels')
                        
                        if added > 0 and updated > 0:
                            ui.notify(f'Import completed: {added} new instruments added, {updated} existing instruments updated with labels', type='positive')
                        elif added > 0:
                            ui.notify(f'Imported {added} new instruments', type='positive')
                        elif updated > 0:
                            ui.notify(f'Updated {updated} existing instruments with new labels', type='positive')
                        else:
                            ui.notify('No changes made - all instruments already exist with specified labels', type='info')
                        
                        self._update_table_rows()
                        self.import_dialog.close()
                        
                    except Exception as e:
                        logger.error(f'Import failed: {e}', exc_info=True)
                        ui.notify(f'Import failed: {e}', type='negative')
                
                # File upload
                ui.upload(
                    label='Upload instrument list (.txt)', 
                    on_upload=handle_upload, 
                    max_files=1, 
                    auto_upload=True
                ).classes('w-full')
                
                # Buttons
                with ui.row().classes('w-full justify-end mt-4'):
                    ui.button('Cancel', on_click=self.import_dialog.close).props('flat')
        
        self.import_dialog.open()

    def add_instrument_dialog(self, instrument=None):
        if not hasattr(self, 'add_dialog'):
            self.add_dialog = ui.dialog()
        self.add_dialog.clear()
        is_edit = instrument is not None
        with self.add_dialog:
            with ui.card():
                name_input = ui.input(label='Instrument Name', value=instrument.name if is_edit else '')
                type_input = ui.select(
                    options=[t.value for t in InstrumentType],
                    label='Instrument Type',
                    value=instrument.instrument_type if is_edit else InstrumentType.STOCK
                )
                labels_input = ui.input(label='Labels (comma separated)', value=(', '.join(instrument.labels) if is_edit and hasattr(instrument, 'labels') else ''))
                def save():
                    session = get_db()
                    labels = [l.strip() for l in labels_input.value.split(',')] if labels_input.value else []
                    if is_edit:
                        logger.debug(f'Editing instrument {instrument.id}: {name_input.value}')
                        instrument.name = name_input.value
                        instrument.instrument_type = type_input.value
                        instrument.labels = labels
                        update_instance(instrument, session)
                        session.commit()
                        logger.info(f'Instrument {instrument.id} updated')
                        ui.notify('Instrument updated!', type='positive')
                    else:
                        logger.debug(f'Adding new instrument: {name_input.value}')
                        inst = Instrument(
                            name=name_input.value,
                            instrument_type=type_input.value,
                            categories=[],
                            labels=labels
                        )
                        add_instance(inst, session)
                        session.commit()
                        logger.info(f'Instrument {name_input.value} added')
                        ui.notify('Instrument added!', type='positive')
                    self.add_dialog.close()
                    self._update_table_rows()
                ui.button('Save', on_click=save)
        self.add_dialog.open()

    def _on_table_edit_click(self, msg):
        logger.debug(f'Edit instrument table click: {msg}')
        row = msg.args['row']
        instrument_id = row['id']
        instrument = get_instance(Instrument, instrument_id)
        if instrument:
            self.add_instrument_dialog(instrument)
        else:
            logger.warning(f'Instrument with id {instrument_id} not found')
            ui.notify('Instrument not found', type='error')

    def _on_table_del_click(self, msg):
        logger.debug(f'Delete instrument table click: {msg}')
        row = msg.args['row']
        instrument_id = row['id']
        instrument = get_instance(Instrument, instrument_id)
        if instrument:
            try:
                logger.debug(f'Deleting instrument {instrument_id}')
                delete_instance(instrument)
                logger.info(f'Instrument {instrument_id} deleted')
                ui.notify('Instrument deleted', type='positive')
                self._update_table_rows()
            except Exception as e:
                logger.error(f'Error deleting instrument {instrument_id}: {e}', exc_info=True)
                ui.notify(f'Error deleting instrument: {e}', type='negative')
        else:
            logger.warning(f'Instrument with id {instrument_id} not found')
            ui.notify('Instrument not found', type='error')

# --- AppSettingsTab for static settings ---
class AppSettingsTab:
    """
    UI tab for editing and saving static application settings (OpenAI API Key, Finnhub API Key, Worker Count).
    Uses the AppSetting model for persistence. Renders directly in the tab.
    """
    def __init__(self):
        self.openai_input = None
        self.openai_admin_input = None
        self.finnhub_input = None
        self.fred_input = None
        self.alpha_vantage_input = None
        self.fmp_input = None
        self.alpaca_key_input = None
        self.alpaca_secret_input = None
        self.worker_count_input = None
        self.account_refresh_interval_input = None
        self.render()

    def render(self):
        session = get_db()
        openai = session.exec(select(AppSetting).where(AppSetting.key == 'openai_api_key')).first()
        openai_admin = session.exec(select(AppSetting).where(AppSetting.key == 'openai_admin_api_key')).first()
        finnhub = session.exec(select(AppSetting).where(AppSetting.key == 'finnhub_api_key')).first()
        fred = session.exec(select(AppSetting).where(AppSetting.key == 'fred_api_key')).first()
        alpha_vantage = session.exec(select(AppSetting).where(AppSetting.key == 'alpha_vantage_api_key')).first()
        fmp = session.exec(select(AppSetting).where(AppSetting.key == 'FMP_API_KEY')).first()
        alpaca_key = session.exec(select(AppSetting).where(AppSetting.key == 'alpaca_api_key')).first()
        alpaca_secret = session.exec(select(AppSetting).where(AppSetting.key == 'alpaca_api_secret')).first()
        worker_count = session.exec(select(AppSetting).where(AppSetting.key == 'worker_count')).first()
        account_refresh_interval = session.exec(select(AppSetting).where(AppSetting.key == 'account_refresh_interval')).first()
        
        with ui.card().classes('w-full'):
            self.openai_input = ui.input(label='OpenAI API Key', value=openai.value_str if openai else '').classes('w-full')
            with ui.row().classes('w-full items-center gap-2 mt-2 mb-2'):
                self.openai_admin_input = ui.input(label='OpenAI Admin API Key (for usage data)', value=openai_admin.value_str if openai_admin else '').classes('flex-1')
                ui.link('Get Admin Key', 'https://platform.openai.com/settings/organization/admin-keys', new_tab=True).classes('text-sm text-blue-600 underline')
            self.finnhub_input = ui.input(label='Finnhub API Key', value=finnhub.value_str if finnhub else '').classes('w-full')
            self.fred_input = ui.input(label='FRED API Key', value=fred.value_str if fred else '').classes('w-full')
            self.alpha_vantage_input = ui.input(label='Alpha Vantage API Key', value=alpha_vantage.value_str if alpha_vantage else '').classes('w-full')
            with ui.row().classes('w-full items-center gap-2 mt-2 mb-2'):
                self.fmp_input = ui.input(label='Financial Modeling Prep (FMP) API Key', value=fmp.value_str if fmp else '').classes('flex-1')
                ui.link('Get FMP Key', 'https://site.financialmodelingprep.com/developer/docs', new_tab=True).classes('text-sm text-blue-600 underline')
            ui.label('Alpaca API Keys').classes('text-lg font-semibold mt-4')
            with ui.row().classes('w-full items-center gap-2'):
                self.alpaca_key_input = ui.input(label='Alpaca API Key', value=alpaca_key.value_str if alpaca_key else '').classes('flex-1')
                ui.link('Get Alpaca Keys', 'https://alpaca.markets/docs/trading/getting-started/', new_tab=True).classes('text-sm text-blue-600 underline')
            self.alpaca_secret_input = ui.input(label='Alpaca API Secret', value=alpaca_secret.value_str if alpaca_secret else '', password=True, password_toggle_button=True).classes('w-full')
            self.worker_count_input = ui.number(
                label='Worker Count', 
                value=int(worker_count.value_str) if worker_count and worker_count.value_str else 4,
                min=1,
                max=20,
                step=1
            ).classes('w-full')
            self.account_refresh_interval_input = ui.number(
                label='Account Refresh Interval (minutes)', 
                value=int(account_refresh_interval.value_str) if account_refresh_interval and account_refresh_interval.value_str else 5,
                min=1,
                max=1440,  # Maximum 24 hours
                step=1
            ).classes('w-full')
            ui.button('Save', on_click=self.save_settings)

    def save_settings(self):
        try:
            session = get_db()
            # OpenAI Regular Key
            openai = session.exec(select(AppSetting).where(AppSetting.key == 'openai_api_key')).first()
            if openai:
                openai.value_str = self.openai_input.value
                update_instance(openai, session)
            else:
                openai = AppSetting(key='openai_api_key', value_str=self.openai_input.value)
                add_instance(openai, session)

            # OpenAI Admin Key
            if self.openai_admin_input.value.strip():
                # Validate admin key format
                if not self.openai_admin_input.value.strip().startswith("sk-admin"):
                    ui.notify('Invalid admin key format. Admin keys should start with "sk-admin".', type='negative')
                    return
            
            openai_admin = session.exec(select(AppSetting).where(AppSetting.key == 'openai_admin_api_key')).first()
            if openai_admin:
                openai_admin.value_str = self.openai_admin_input.value
                update_instance(openai_admin, session)
            else:
                openai_admin = AppSetting(key='openai_admin_api_key', value_str=self.openai_admin_input.value)
                add_instance(openai_admin, session)

            # Finnhub
            finnhub = session.exec(select(AppSetting).where(AppSetting.key == 'finnhub_api_key')).first()
            if finnhub:
                finnhub.value_str = self.finnhub_input.value
                update_instance(finnhub, session)
            else:
                finnhub = AppSetting(key='finnhub_api_key', value_str=self.finnhub_input.value)
                add_instance(finnhub, session)

            # FRED
            fred = session.exec(select(AppSetting).where(AppSetting.key == 'fred_api_key')).first()
            if fred:
                fred.value_str = self.fred_input.value
                update_instance(fred, session)
            else:
                fred = AppSetting(key='fred_api_key', value_str=self.fred_input.value)
                add_instance(fred, session)

            # Alpha Vantage
            alpha_vantage = session.exec(select(AppSetting).where(AppSetting.key == 'alpha_vantage_api_key')).first()
            if alpha_vantage:
                alpha_vantage.value_str = self.alpha_vantage_input.value
                update_instance(alpha_vantage, session)
            else:
                alpha_vantage = AppSetting(key='alpha_vantage_api_key', value_str=self.alpha_vantage_input.value)
                add_instance(alpha_vantage, session)
            
            # FMP (Financial Modeling Prep)
            fmp = session.exec(select(AppSetting).where(AppSetting.key == 'FMP_API_KEY')).first()
            if fmp:
                fmp.value_str = self.fmp_input.value
                update_instance(fmp, session)
            else:
                fmp = AppSetting(key='FMP_API_KEY', value_str=self.fmp_input.value)
                add_instance(fmp, session)
            
            # Alpaca API Key
            alpaca_key = session.exec(select(AppSetting).where(AppSetting.key == 'alpaca_api_key')).first()
            if alpaca_key:
                alpaca_key.value_str = self.alpaca_key_input.value
                update_instance(alpaca_key, session)
            else:
                alpaca_key = AppSetting(key='alpaca_api_key', value_str=self.alpaca_key_input.value)
                add_instance(alpaca_key, session)
            
            # Alpaca API Secret
            alpaca_secret = session.exec(select(AppSetting).where(AppSetting.key == 'alpaca_api_secret')).first()
            if alpaca_secret:
                alpaca_secret.value_str = self.alpaca_secret_input.value
                update_instance(alpaca_secret, session)
            else:
                alpaca_secret = AppSetting(key='alpaca_api_secret', value_str=self.alpaca_secret_input.value)
                add_instance(alpaca_secret, session)
            
            # Worker Count
            worker_count = session.exec(select(AppSetting).where(AppSetting.key == 'worker_count')).first()
            if worker_count:
                worker_count.value_str = str(int(self.worker_count_input.value))
                update_instance(worker_count, session)
            else:
                worker_count = AppSetting(key='worker_count', value_str=str(int(self.worker_count_input.value)))
                add_instance(worker_count, session)
            
            # Account Refresh Interval
            account_refresh_interval = session.exec(select(AppSetting).where(AppSetting.key == 'account_refresh_interval')).first()
            if account_refresh_interval:
                account_refresh_interval.value_str = str(int(self.account_refresh_interval_input.value))
                update_instance(account_refresh_interval, session)
            else:
                account_refresh_interval = AppSetting(key='account_refresh_interval', value_str=str(int(self.account_refresh_interval_input.value)))
                add_instance(account_refresh_interval, session)
            
            session.commit()
            ui.notify('Settings saved successfully', type='positive')
            
            # Notify user that worker count changes require restart
            ui.notify('Worker count changes will take effect after restart', type='info')
            # Notify user that account refresh interval changes require restart
            ui.notify('Account refresh interval changes will take effect after restart', type='info')
            
        except Exception as e:
            logger.error(f"Error saving settings: {str(e)}", exc_info=True)
            ui.notify('Error saving settings', type='negative')
class AccountDefinitionsTab:
    def __init__(self):
        logger.debug('Initializing AccountDefinitionsTab')
        self.dialog = ui.dialog()
        self.accounts_table = None
        self.account_settings()

    def _update_table_rows(self):
        if self.accounts_table:
            accounts = list(get_all_instances(AccountDefinition))
            self.accounts_table.rows = [dict(a) for a in accounts]
            #self.accounts_table.refresh()
            logger.info('Accounts table rows updated')
        else:
            logger.warning('Accounts table is not initialized yet')

    def save_account(self, account: Optional[AccountDefinition] = None) -> None:
        """Save or update an account configuration.
        This method handles both creating new accounts and updating existing ones.
        It saves both the basic account information and any provider-specific dynamic settings.
        Args:
            account (Optional[AccountDefinition]): The existing account to update. 
                If None, a new account will be created.
        Returns:
            None
        Raises:
            Exception: If there is an error during the save process.
                The error will be logged and a UI notification will be shown.
        Notes:
            - For existing accounts, it updates the provider, name, and description
            - For new accounts, it creates a new AccountDefinition entry
            - Dynamic settings are saved via the provider's AccountInterface
            - UI dialog is closed and table rows are updated on successful save
        """
        
        try:
            
            provider = self.type_select.value
            provider_cls = providers.get(provider, None)
            dynamic_settings = {}
            if hasattr(self, 'settings_inputs') and self.settings_inputs:
                for key, inp in self.settings_inputs.items():
                    dynamic_settings[key] = inp.value
            logger.debug(f'Saving account with provider: {provider}, name: {self.name_input.value}, description: {self.desc_input.value}, dynamic_settings: {dynamic_settings}') 
            if account:
                account.provider = provider
                account.name = self.name_input.value
                account.description = self.desc_input.value
                update_instance(account)
                logger.info(f"Updated account: {account.name}")
                # Save dynamic settings using AccountInterface

                acc_iface = provider_cls(account.id)
                if isinstance(acc_iface, AccountInterface):
                    acc_iface.save_settings(dynamic_settings)
            else:
                new_account = AccountDefinition(
                    provider=provider,
                    name=self.name_input.value,
                    description=self.desc_input.value
                )
                new_account_id = add_instance(new_account)
                logger.info(f"Created new account: {self.name_input.value} with id {new_account_id}")
                # Save dynamic settings for new account
                # Get the new account's id
                acc_iface = provider_cls(new_account_id)
                if isinstance(acc_iface, AccountInterface):
                    acc_iface.save_settings(dynamic_settings)
            self.dialog.close()
            self._update_table_rows()
        except Exception as e:
            logger.error(f"Error saving account: {str(e)}", exc_info=True)
            ui.notify("Error saving account", type="error")

    def delete_account(self, account: AccountDefinition) -> None:
        try:
            # First delete related account settings
            with get_db() as session:
                settings = session.exec(
                    select(AccountSetting).where(AccountSetting.account_id == account.id)
                ).all()
                for setting in settings:
                    delete_instance(setting, session)
                logger.info(f"Deleted {len(settings)} settings for account: {account.name}")
            
            # Then delete the account
            delete_instance(account)
            logger.info(f"Deleted account: {account.name}")
            self._update_table_rows()
        except Exception as e:
            logger.error(f"Error deleting account: {str(e)}", exc_info=True)
            ui.notify("Error deleting account", type="error")

    def show_dialog(self, account: Optional[AccountDefinition] = None) -> None:
        logger.debug(f'Showing account dialog for account: {account.name if account else "new account"}')
        with self.dialog:
            self.dialog.clear()
            provider_names = list(providers.keys())
            with ui.card() as card:
                self.type_select = ui.select(provider_names, label='Account Provider').classes('w-full')
                self.name_input = ui.input(label='Account Name')
                self.desc_input = ui.input(label='Description')
                self.type_select.value = account.provider if account else provider_names[0]
                self.name_input.value = account.name if account else ''
                self.desc_input.value = account.description if account else ''
                self.dynamic_settings_container = ui.column().classes('w-full')
                self._render_dynamic_settings(self.type_select.value, account)
                self.type_select.on('update:model-value', lambda e: self._on_provider_change(e, account))
                ui.button('Save', on_click=lambda: self.save_account(account))
        self.dialog.open()

    def _on_provider_change(self, event, account):
        provider = event.value if hasattr(event, 'value') else event
        logger.debug(f'Provider changed to: {provider}')
        self.dynamic_settings_container.clear()
        self._render_dynamic_settings(provider, account)

    def _render_dynamic_settings(self, provider, account=None):
        # Example: Render provider-specific fields dynamically
        provider_config = providers.get(provider, {})
        settings_def = provider_config.get_settings_definitions()
        settings_values = provider_config(account.id).settings if account else {}
        self.settings_inputs = {}
        if settings_def and len(settings_def.keys()) > 0:
            for key, meta in settings_def.items():
                label = meta.get("description", key)
                value = settings_values.get(key, None) if settings_values else None
                tooltip_text = meta.get("tooltip")
                
                # Create a container for this setting (title + input)
                with ui.column().classes('w-full mb-4'):
                    # Create label with tooltip inline
                    if tooltip_text:
                        with ui.row().classes('items-center gap-1 mb-2'):
                            ui.label(label).classes('text-sm font-medium')
                            ui.icon('help_outline', size='sm').classes('text-gray-500 cursor-help').tooltip(tooltip_text).style('font-size: 18px !important; padding: 12px !important; max-width: 350px !important; line-height: 1.4 !important;')
                        
                        # Use empty label for input since we show it above
                        display_label = ""
                    else:
                        display_label = label
                    
                    # Create the input field directly in the same container
                    if meta["type"] == "str":
                        inp = ui.input(label=display_label, value=value or "").classes('w-full')
                    elif meta["type"] == "bool":
                        inp = ui.checkbox(text=display_label, value=bool(value) if value is not None else False)
                    else:
                        inp = ui.input(label=display_label, value=value or "").classes('w-full')
                
                self.settings_inputs[key] = inp
        else:
            ui.label("No provider-specific settings available.")

    def _on_table_edit_click(self, msg) -> None:
        logger.debug('Handling edit account from table, data %s', msg)
        row = msg.args['row']
        account_id = row['id']
        account = get_instance(AccountDefinition, account_id)
        if account:
            self.show_dialog(account)
        else:
            ui.notify("Account not found", type="error")
            logger.warning(f"Account with id {account_id} not found")
    
    def _on_table_del_click(self, msg) -> None:
        logger.debug('Handling delete account from table, data %s', msg)
        row = msg.args['row']
        account_id = row['id']
        account = get_instance(AccountDefinition, account_id)
        if account:
            self.delete_account(account)
        else:
            ui.notify("Account not found", type="error")
            logger.warning(f"Account with id {account_id} not found")

    def account_settings(self) -> None:
        logger.debug('Loading account settings')
        accounts = list(get_all_instances(AccountDefinition))
        logger.info(f'Loaded {len(accounts)} accounts')


        with ui.card().classes('w-full'):
            ui.button('Add Account', on_click=lambda: self.show_dialog())
            self.accounts_table = ui.table(
                columns=[
                    {'name': 'provider', 'label': 'Provider', 'field': 'provider'},
                    {'name': 'name', 'label': 'Name', 'field': 'name'},
                    {'name': 'description', 'label': 'Description', 'field': 'description'},
                    {'name': 'actions', 'label': 'Actions', 'field': 'actions'}
                ],
                rows=[dict(a) for a in accounts],
                row_key='id'
            ).classes('w-full')
            self.accounts_table.add_slot(f'body-cell-actions', """
                <q-td :props="props">
                    <q-btn @click="$parent.$emit('edit', props)" icon="edit" flat dense color='blue'/>
                    <q-btn @click="$parent.$emit('del', props)" icon="delete" flat dense color='red'/>
                </q-td>
            """)

            self.accounts_table.on('edit', self._on_table_edit_click)
            self.accounts_table.on('del', self._on_table_del_click)


class ExpertSettingsTab:
    """
    UI tab for managing expert instances (ExpertInstance SQL Model).
    Features: table with experts, add/edit experts with settings and instrument selection.
    
    This tab manages three main categories of settings:
    
    1. **General Settings** (saved as ExpertSetting records):
       - execution_schedule_enter_market (JSON): Schedule configuration for entering market containing:
         - days: Dict of weekday names (monday, tuesday, etc.) to boolean values
         - times: List of execution times in HH:MM format
       - enable_buy (bool): Whether the expert can place BUY orders (default: True)
       - enable_sell (bool): Whether the expert can place SELL orders (default: False)
    
    2. **Expert-Specific Settings** (saved as ExpertSetting records):
       - Settings defined by each expert class's get_settings_definitions() method
       - Automatically detects setting types (str, bool, float, json) and handles valid_values
       - Uses default values when available from settings definitions
    
    3. **Instrument Configuration** (saved as ExpertSetting records):
       - Instrument selection and weight configuration
       - Managed through the InstrumentSelector component
       - Stores enabled instruments with their respective trading weights
    
    All settings are persisted using the ExtendableSettingsInterface save_setting() method
    with appropriate setting_type parameters to ensure proper data type handling.
    """
    
    def __init__(self):
        logger.debug('Initializing ExpertSettingsTab')
        self.dialog = ui.dialog()
        self.experts_table = None
        self.instrument_selector = None
        self.render()
    
    def render(self):
        logger.debug('Rendering ExpertSettingsTab UI')
        with ui.card().classes('w-full'):
            ui.label('Expert Management').classes('text-h6')
            
            ui.button('Add Expert', on_click=lambda: self.show_dialog())
            
            self.experts_table = ui.table(
                columns=[
                    {'name': 'expert', 'label': 'Expert Type', 'field': 'expert', 'sortable': True},
                    {'name': 'alias', 'label': 'Alias', 'field': 'alias', 'sortable': True},
                    {'name': 'user_description', 'label': 'User Notes', 'field': 'user_description'},
                    {'name': 'enabled', 'label': 'Enabled', 'field': 'enabled', 'align': 'center'},
                    {'name': 'virtual_equity_pct', 'label': 'Virtual Equity %', 'field': 'virtual_equity_pct', 'align': 'right'},
                    {'name': 'account_id', 'label': 'Account ID', 'field': 'account_id'},
                    {'name': 'enter_market_ruleset_name', 'label': 'Enter Market Ruleset', 'field': 'enter_market_ruleset_name'},
                    {'name': 'open_positions_ruleset_name', 'label': 'Open Positions Ruleset', 'field': 'open_positions_ruleset_name'},
                    {'name': 'actions', 'label': 'Actions', 'field': 'actions'}
                ],
                rows=self._get_all_expert_instances(),
                row_key='id'
            ).classes('w-full')
            
            self.experts_table.add_slot('body-cell-enabled', '''
                <q-td :props="props">
                    <q-icon :name="props.value ? 'check_circle' : 'cancel'" 
                            :color="props.value ? 'green' : 'red'" />
                </q-td>
            ''')
            
            self.experts_table.add_slot('body-cell-actions', """
                <q-td :props="props">
                    <q-btn @click="$parent.$emit('edit', props)" icon="edit" flat dense color='blue'/>
                    <q-btn @click="$parent.$emit('del', props)" icon="delete" flat dense color='red'/>
                </q-td>
            """)
            
            self.experts_table.on('edit', self._on_table_edit_click)
            self.experts_table.on('del', self._on_table_del_click)
        
        logger.debug('ExpertSettingsTab UI rendered')
    
    def _get_all_expert_instances(self):
        """Get all expert instances for the table."""
        logger.debug('Fetching all expert instances for table')
        try:
            instances = get_all_instances(ExpertInstance)
            rows = []
            for instance in instances:
                row = dict(instance)
                
                # Ensure alias is displayed (no truncation needed, max 100 chars)
                row['alias'] = instance.alias or ''
                
                # Ensure user_description is displayed properly (truncate if too long for table)
                user_desc = instance.user_description or ''
                if len(user_desc) > 50:
                    row['user_description'] = user_desc[:47] + '...'
                else:
                    row['user_description'] = user_desc
                
                # Fetch and add ruleset names
                if instance.enter_market_ruleset_id:
                    enter_market_ruleset = get_instance(Ruleset, instance.enter_market_ruleset_id)
                    row['enter_market_ruleset_name'] = enter_market_ruleset.name if enter_market_ruleset else '(Not found)'
                else:
                    row['enter_market_ruleset_name'] = '(None)'
                
                if instance.open_positions_ruleset_id:
                    open_positions_ruleset = get_instance(Ruleset, instance.open_positions_ruleset_id)
                    row['open_positions_ruleset_name'] = open_positions_ruleset.name if open_positions_ruleset else '(Not found)'
                else:
                    row['open_positions_ruleset_name'] = '(None)'
                
                rows.append(row)
            logger.debug(f'Fetched {len(rows)} expert instances')
            return rows
        except Exception as e:
            logger.error(f'Error fetching expert instances: {e}', exc_info=True)
            return []
    
    def _update_table_rows(self):
        """Update the table with fresh data."""
        if self.experts_table:
            self.experts_table.rows = self._get_all_expert_instances()
            logger.debug('Expert instances table rows updated')
    
    def _get_available_expert_types(self):
        """Get list of available expert types."""
        expert_types = []
        for expert_class in experts:
            expert_types.append(expert_class.__name__)
        return expert_types
    
    def _get_available_accounts(self):
        """Get list of available accounts."""
        accounts = get_all_instances(AccountDefinition)
        return [f"{acc.name} ({acc.provider})" for acc in accounts]
    
    def _get_account_id_from_display_string(self, display_string):
        """Get account ID from display string like 'Account Name (provider)'."""
        if not display_string:
            return None
        
        accounts = get_all_instances(AccountDefinition)
        for acc in accounts:
            if f"{acc.name} ({acc.provider})" == display_string:
                return acc.id
        return None
    
    def _get_rulesets_by_use_case(self, use_case):
        """Get rulesets filtered by AnalysisUseCase subtype.
        
        Args:
            use_case: 'enter_market' or 'open_positions'
            
        Returns:
            Dictionary mapping ruleset display names to IDs, plus None option
        """
        from ...core.types import AnalysisUseCase
        from sqlmodel import select
        
        # Map string to enum
        if use_case == 'enter_market':
            subtype = AnalysisUseCase.ENTER_MARKET
        elif use_case == 'open_positions':
            subtype = AnalysisUseCase.OPEN_POSITIONS
        else:
            return {'(None)': None}
        
        # Get all rulesets with matching subtype or no subtype
        session = get_db()
        statement = select(Ruleset).where(
            (Ruleset.subtype == subtype) | (Ruleset.subtype == None)
        )
        rulesets = session.exec(statement).all()
        session.close()
        
        # Create display dictionary
        result = {'(None)': None}
        for ruleset in rulesets:
            display_name = ruleset.name
            if ruleset.description:
                display_name += f" - {ruleset.description[:50]}"
            result[display_name] = ruleset.id
        
        return result
    
    def _get_rulesets_list_by_use_case(self, use_case):
        """Get rulesets filtered by AnalysisUseCase subtype as a list and mapping.
        
        Args:
            use_case: 'enter_market' or 'open_positions'
            
        Returns:
            Tuple of (list of display names, dict mapping display names to IDs)
        """
        from ...core.types import AnalysisUseCase
        from sqlmodel import select
        
        # Map string to enum
        if use_case == 'enter_market':
            subtype = AnalysisUseCase.ENTER_MARKET
        elif use_case == 'open_positions':
            subtype = AnalysisUseCase.OPEN_POSITIONS
        else:
            return ['(None)'], {'(None)': None}
        
        # Get all rulesets with matching subtype or no subtype
        session = get_db()
        statement = select(Ruleset).where(
            (Ruleset.subtype == subtype) | (Ruleset.subtype == None)
        )
        rulesets = session.exec(statement).all()
        session.close()
        
        # Create display list and mapping
        display_list = ['(None)']
        name_to_id_map = {'(None)': None}
        
        for ruleset in rulesets:
            display_name = ruleset.name
            if ruleset.description:
                display_name += f" - {ruleset.description[:50]}"
            display_list.append(display_name)
            name_to_id_map[display_name] = ruleset.id
        
        return display_list, name_to_id_map
    
    def show_dialog(self, expert_instance=None):
        """Show the add/edit expert dialog."""
        logger.debug(f'Showing expert dialog for instance: {expert_instance.id if expert_instance else "new instance"}')
        
        is_edit = expert_instance is not None
        
        with self.dialog:
            self.dialog.clear()
            
            with ui.card().classes('w-full').style('width: 90vw; max-width: 1400px; height: 95vh; margin: auto; display: flex; flex-direction: column'):
                ui.label('Add Expert' if not is_edit else 'Edit Expert').classes('text-h6')
                
                # Basic expert information
                with ui.column().classes('w-full gap-4'):
                    expert_types = self._get_available_expert_types()
                    self.expert_select = ui.select(
                        expert_types, 
                        label='Expert Type'
                    ).classes('w-full')
                    
                    # Description as read-only display
                    self.description_label = ui.label('').classes('text-grey-7 mb-2')
                    
                    # Alias as short display name
                    self.alias_input = ui.input(
                        label='Alias (max 100 chars)',
                        placeholder='Short display name for this expert...'
                    ).classes('w-full').props('maxlength=100')
                    
                    # User description as editable textarea
                    self.user_description_textarea = ui.textarea(
                        label='User Notes',
                        placeholder='Add your own notes about this expert instance...'
                    ).classes('w-full')
                    
                    with ui.row().classes('w-full'):
                        self.enabled_checkbox = ui.checkbox('Enabled', value=True)
                        self.virtual_equity_input = ui.input(
                            label='Virtual Equity', 
                            value='100.0'
                        ).classes('w-48')
                    
                    accounts = self._get_available_accounts()
                    self.account_select = ui.select(
                        accounts,
                        label='Trading Account'
                    ).classes('w-full')
                
                # Fill values if editing
                if is_edit:
                    self.expert_select.value = expert_instance.expert
                    self.alias_input.value = expert_instance.alias or ''
                    self.user_description_textarea.value = expert_instance.user_description or ''
                    self.enabled_checkbox.value = expert_instance.enabled
                    self.virtual_equity_input.value = str(expert_instance.virtual_equity_pct)
                    
                    # Find and set the account display string
                    account_instance = get_instance(AccountDefinition, expert_instance.account_id)
                    if account_instance:
                        self.account_select.value = f"{account_instance.name} ({account_instance.provider})"
                else:
                    if expert_types:
                        self.expert_select.value = expert_types[0]
                    if accounts:
                        self.account_select.value = accounts[0]
                
                # Update description when expert type changes
                self.expert_select.on('update:model-value', 
                                    lambda e: self._on_expert_type_change_dialog(e, expert_instance))
                
                # Set initial description
                self._update_expert_description()
                
                # Tabs for different settings sections
                with ui.tabs() as settings_tabs:
                    ui.tab('General Settings', icon='schedule')
                    ui.tab('Instruments', icon='trending_up')
                    ui.tab('Expert Settings', icon='settings')
                    ui.tab('Cleanup', icon='delete_sweep')
                
                with ui.tab_panels(settings_tabs, value='General Settings').classes('w-full').style('flex: 1; overflow-y: auto'):
                    # General Settings tab
                    with ui.tab_panel('General Settings'):
                        ui.label('General Expert Configuration:').classes('text-subtitle1 mb-4')
                        
                        # Schedule settings with expandable cards for both types
                        ui.label('Execution Schedules:').classes('text-subtitle2 mb-2')
                        ui.label('Configure when the expert should run for different analysis types:').classes('text-body2 mb-2')
                        
                        # Enter Market Analysis Schedule
                        with ui.expansion('📈 Enter Market Analysis Schedule', value=True).classes('w-full mb-4'):
                            with ui.card().classes('w-full'):
                                ui.label('Schedule for analyzing new market entry opportunities:').classes('text-body2 mb-2')
                            
                                ui.label('Select days when the expert should analyze for new positions:').classes('text-body2 mb-2')
                                with ui.row().classes('w-full gap-2 mb-4'):
                                    self.enter_market_schedule_days = {}
                                    for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']:
                                        # Default weekdays to True, weekends to False for enter market
                                        default_value = day not in ['Saturday', 'Sunday']
                                        self.enter_market_schedule_days[day.lower()] = ui.checkbox(day, value=default_value).classes('mb-2')
                                
                                ui.label('Execution times (24-hour format, e.g., 09:30, 15:00):').classes('text-body2 mb-2')
                                try:
                                    self.enter_market_times_container = ui.column().classes('w-full mb-4')
                                    if self.enter_market_times_container is None:
                                        logger.error("ui.column() returned None for enter_market_times_container")
                                        self.enter_market_times_container = ui.column()  # Try again without classes
                                except Exception as e:
                                    logger.error(f"Error creating enter_market_times_container: {e}", exc_info=True)
                                    self.enter_market_times_container = None
                                
                                self.enter_market_execution_times = []
                                
                                # Verify container was created successfully
                                if self.enter_market_times_container is None:
                                    logger.error("Failed to create enter_market_times_container, skipping time input setup")
                                else:
                                    # Add initial time input
                                    self._add_time_input_enter_market('09:30')
                                
                                # Only create the add time button if we have a valid container
                                if self.enter_market_times_container is not None:
                                    with ui.row().classes('w-full gap-2 mb-4'):
                                        ui.button('Add Time', on_click=self._add_time_input_enter_market, icon='add_alarm').props('flat')
                        
                        # Open Positions Analysis Schedule
                        with ui.expansion('💼 Open Positions Analysis Schedule', value=False).classes('w-full mb-4'):
                            with ui.card().classes('w-full'):
                                ui.label('Schedule for analyzing existing open positions:').classes('text-body2 mb-2')
                                ui.label('Select days when the expert should analyze open positions:').classes('text-body2 mb-2')
                                with ui.row().classes('w-full gap-2 mb-4'):
                                    self.open_positions_schedule_days = {}
                                    for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']:
                                        # Default weekdays to True, weekends to False for open positions
                                        default_value = day not in ['Saturday', 'Sunday']
                                        self.open_positions_schedule_days[day.lower()] = ui.checkbox(day, value=default_value).classes('mb-2')
                                
                                ui.label('Execution times (24-hour format, e.g., 09:30, 15:00):').classes('text-body2 mb-2')
                                try:
                                    self.open_positions_times_container = ui.column().classes('w-full mb-4')
                                    if self.open_positions_times_container is None:
                                        logger.error("ui.column() returned None for open_positions_times_container")
                                        self.open_positions_times_container = ui.column()  # Try again without classes
                                except Exception as e:
                                    logger.error(f"Error creating open_positions_times_container: {e}", exc_info=True)
                                    self.open_positions_times_container = None
                                
                                self.open_positions_execution_times = []
                                
                                # Verify container was created successfully
                                if self.open_positions_times_container is None:
                                    logger.error("Failed to create open_positions_times_container, skipping time input setup")
                                else:
                                    # Add initial time inputs (more frequent for position monitoring)
                                    for time in ['09:30', '10:30', '11:30', '12:30', '13:30', '14:30', '15:30']:
                                        self._add_time_input_open_positions(time)
                                
                                # Only create the add time button if we have a valid container
                                if self.open_positions_times_container is not None:
                                    with ui.row().classes('w-full gap-2 mb-4'):
                                        ui.button('Add Time', on_click=self._add_time_input_open_positions, icon='add_alarm').props('flat')
                        
                        ui.separator().classes('my-4')
                        
                        # Trading direction settings
                        ui.label('Trading Permissions:').classes('text-subtitle2 mb-2')
                        ui.label('Select which trading actions this expert can perform:').classes('text-body2 mb-2')
                        
                        with ui.row().classes('w-full gap-4'):
                            self.enable_buy_checkbox = ui.checkbox('Enable BUY orders', value=True)
                            self.enable_sell_checkbox = ui.checkbox('Enable SELL orders', value=False)
                        
                        ui.separator().classes('my-4')
                        
                        # Automatic trading settings
                        ui.label('Automatic Trading:').classes('text-subtitle2 mb-2')
                        ui.label('Configure automatic trading permissions for this expert:').classes('text-body2 mb-2')
                        
                        with ui.column().classes('w-full gap-2'):
                            self.allow_automated_trade_opening_checkbox = ui.checkbox(
                                'Allow automated trade opening', 
                                value=False
                            )
                            ui.label('Allows the expert to automatically open new trading positions').classes('text-body2 text-grey-7 ml-6')
                            
                            self.allow_automated_trade_modification_checkbox = ui.checkbox(
                                'Allow automated trade modification/closing', 
                                value=False
                            )
                            ui.label('Allows the expert to automatically modify or close existing positions').classes('text-body2 text-grey-7 ml-6')
                        
                        ui.separator().classes('my-4')
                        
                        # Risk Management settings
                        ui.label('Risk Management:').classes('text-subtitle2 mb-2')
                        ui.label('Configure risk management parameters for this expert:').classes('text-body2 mb-2')
                        
                        with ui.column().classes('w-full gap-2'):
                            # Max Virtual Equity Per Instrument
                            with ui.row().classes('items-center gap-2'):
                                ui.label('Max equity per instrument (%):').classes('text-sm font-medium')
                                self.max_virtual_equity_per_instrument_input = ui.input(
                                    value='10.0',
                                    placeholder='10.0'
                                ).classes('w-20')
                                ui.label('%').classes('text-sm')
                            ui.label('Maximum percentage of virtual trading balance that can be allocated to a single instrument. Recommended: 5-15%.').classes('text-body2 text-grey-7 ml-2')
                        
                        ui.separator().classes('my-4')
                        
                        # Ruleset assignment settings
                        ui.label('Automation Rulesets:').classes('text-subtitle2 mb-2')
                        ui.label('Assign rulesets to control automated trading behavior:').classes('text-body2 mb-2')
                        
                        with ui.column().classes('w-full gap-2'):
                            # Enter Market Ruleset
                            enter_market_rulesets_list, self.enter_market_ruleset_map = self._get_rulesets_list_by_use_case('enter_market')
                            self.enter_market_ruleset_select = ui.select(
                                options=enter_market_rulesets_list,
                                label='Enter Market Ruleset',
                                with_input=True,
                                clearable=True
                            ).classes('w-full')
                            ui.label('Ruleset to evaluate when creating new positions from expert recommendations').classes('text-body2 text-grey-7 ml-2')
                            
                            # Open Positions Ruleset
                            open_positions_rulesets_list, self.open_positions_ruleset_map = self._get_rulesets_list_by_use_case('open_positions')
                            self.open_positions_ruleset_select = ui.select(
                                options=open_positions_rulesets_list,
                                label='Open Positions Ruleset',
                                with_input=True,
                                clearable=True
                            ).classes('w-full')
                            ui.label('Ruleset to evaluate when managing existing open positions').classes('text-body2 text-grey-7 ml-2')
                    
                    # Instruments tab
                    with ui.tab_panel('Instruments'):
                        ui.label('Select and configure instruments for this expert:').classes('text-subtitle1 mb-4')
                        
                        # Create instrument selector
                        self.instrument_selector = InstrumentSelector(
                            on_selection_change=self._on_instrument_selection_change
                        )
                        self.instrument_selector.render()
                        
                        # Load current instrument configuration if editing
                        if is_edit:
                            self._load_expert_instrument_config(expert_instance)
                    
                    # Expert-specific settings tab  
                    with ui.tab_panel('Expert Settings'):
                        ui.label('Expert-specific settings:').classes('text-subtitle1 mb-4')
                        self.expert_settings_container = ui.column().classes('w-full')
                        
                        # Render expert-specific settings
                        self._render_expert_settings(expert_instance)
                        
                        # Update settings when expert type changes
                        self.expert_select.on('update:model-value', 
                                            lambda e: self._on_expert_type_change(e, expert_instance))
                    
                    # Cleanup tab
                    with ui.tab_panel('Cleanup'):
                        self._render_cleanup_tab(expert_instance)
                
                # Load general settings if editing
                if is_edit:
                    self._load_general_settings(expert_instance)
                
                # Save button
                with ui.row().classes('w-full justify-end mt-4'):
                    ui.button('Cancel', on_click=self.dialog.close).props('flat')
                    ui.button('Save', on_click=lambda: self._save_expert(expert_instance))
        
        self.dialog.open()
    
    def _load_expert_instrument_config(self, expert_instance):
        """Load instrument configuration for an existing expert."""
        try:
            # Get the expert instance with appropriate class
            from ...core.utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(expert_instance.id)
            if expert:
                
                # Get enabled instruments configuration
                enabled_config = expert._get_enabled_instruments_config()
                
                # Convert to format expected by InstrumentSelector
                instrument_configs = {}
                for symbol, config in enabled_config.items():
                    # Find the instrument ID by symbol
                    session = get_db()
                    from sqlmodel import select
                    statement = select(Instrument).where(Instrument.name == symbol)
                    result = session.exec(statement).first()
                    if result:
                        instrument_configs[result.id] = {
                            'enabled': True,
                            'weight': config.get('weight', 100.0)
                        }
                    session.close()
                
                # Set the configuration in the selector
                if self.instrument_selector:
                    self.instrument_selector.set_selected_instruments(instrument_configs)
                    
        except Exception as e:
            logger.error(f'Error loading expert instrument config: {e}', exc_info=True)
    
    def _get_expert_class(self, expert_type):
        """Get the expert class by type name."""
        from ...modules.experts import get_expert_class
        return get_expert_class(expert_type)
    
    def _update_expert_description(self):
        """Update the description display based on selected expert type."""
        expert_type = self.expert_select.value if hasattr(self, 'expert_select') else None
        if expert_type:
            expert_class = self._get_expert_class(expert_type)
            if expert_class:
                try:
                    description = expert_class.description()
                except Exception as e:
                    logger.debug(f'Error getting description for {expert_type}: {e}')
                    description = f'{expert_type} - Trading expert'
                
                self.description_label.text = f"Description: {description}"
            else:
                self.description_label.text = "Description: Unknown expert type"
        else:
            self.description_label.text = "Description: Select an expert type"
    
    def _add_time_input(self, initial_time=''):
        """Add a new time input field to the execution times container."""
        # Guard check to ensure container exists
        logger.debug(f"_add_time_input called with initial_time={initial_time}")
        
        if not hasattr(self, 'execution_times_container'):
            logger.warning("_add_time_input called but execution_times_container attribute doesn't exist")
            return
        
        logger.debug(f"execution_times_container type: {type(self.execution_times_container)}")
        logger.debug(f"execution_times_container value: {self.execution_times_container}")
        
        if self.execution_times_container is None:
            logger.warning("_add_time_input called but execution_times_container is None")
            return
        
        # Additional check - make sure it's a UI element that supports move()
        if not hasattr(self.execution_times_container, '__enter__') or not hasattr(self.execution_times_container, '__exit__'):
            logger.error(f"execution_times_container is not a context manager: {type(self.execution_times_container)}")
            return
            
        try:
            # Create the row and move it to the container
            row = ui.row().classes('w-full gap-2')
            if row is None:
                logger.error("ui.row() returned None")
                return
            
            # Move the row to the container - move() modifies the row in place
            row.move(self.execution_times_container)
            
            # Use the row directly as context manager
            with row:
                time_input = ui.input(
                    label='Time (HH:MM)', 
                    value=initial_time,
                    placeholder='09:30'
                ).classes('flex-grow')
                
                # Validate time format on change
                def validate_time(e):
                    try:
                        time_str = time_input.value  # Get value from the input element directly
                        if time_str and ':' in time_str:
                            hours, minutes = time_str.split(':')
                            if len(hours) == 2 and len(minutes) == 2:
                                int(hours), int(minutes)  # Validate they're numbers
                                if 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59:
                                    time_input.props('error=false')
                                    return
                        time_input.props('error=true error-message="Invalid time format (use HH:MM)"')
                    except ValueError:
                        time_input.props('error=true error-message="Invalid time format (use HH:MM)"')
                
                time_input.on('blur', validate_time)
                
                # Add remove button
                ui.button(icon='remove', on_click=lambda: self._remove_time_input(time_input)).props('flat round').classes('ml-2')
                
                # Store reference for removal
                if not hasattr(self, 'execution_times'):
                    self.execution_times = []
                self.execution_times.append(time_input)
        except Exception as e:
            logger.error(f"Error creating time input: {e}", exc_info=True)

    def _remove_time_input(self, time_input):
        """Remove a time input field."""
        if len(self.execution_times) <= 1:
            return  # Don't remove the last time input
            
        # Find and remove the input from our list
        if time_input in self.execution_times:
            self.execution_times.remove(time_input)
            
        # Remove the entire row containing the input and button
        parent = time_input.parent_slot.parent
        parent.delete()
        
        # Hide remove buttons if only one input remains
        if len(self.execution_times) == 1:
            for time_inp in self.execution_times:
                parent = time_inp.parent_slot.parent
                for child in parent._props.get('children', []):
                    if hasattr(child, 'props') and 'remove' in str(child.props):
                        child.set_visibility(False)
                        break
    
    def _get_schedule_config(self):
        """Get the current schedule configuration as a JSON-serializable dict."""
        schedule = {
            'days': {},
            'times': []
        }
        
        # Get selected days
        for day, checkbox in self.schedule_days.items():
            schedule['days'][day] = checkbox.value
        
        # Get execution times
        for time_input in self.execution_times:
            time_value = time_input.value.strip()
            if time_value and ':' in time_value:
                try:
                    hours, minutes = time_value.split(':')
                    hours_int = int(hours)
                    minutes_int = int(minutes)
                    if 0 <= hours_int <= 23 and 0 <= minutes_int <= 59:
                        schedule['times'].append(time_value)
                except ValueError:
                    pass  # Skip invalid times
        
        return schedule
    
    def _get_enter_market_schedule_config(self):
        """Get the current enter market schedule configuration as a JSON-serializable dict."""
        schedule = {
            'days': {},
            'times': []
        }
        
        # Get selected days
        if hasattr(self, 'enter_market_schedule_days'):
            for day, checkbox in self.enter_market_schedule_days.items():
                schedule['days'][day] = checkbox.value
        
        # Get execution times
        if hasattr(self, 'enter_market_execution_times'):
            for time_input in self.enter_market_execution_times:
                time_value = time_input.value.strip()
                if time_value and ':' in time_value:
                    try:
                        hours, minutes = time_value.split(':')
                        hours_int = int(hours)
                        minutes_int = int(minutes)
                        if 0 <= hours_int <= 23 and 0 <= minutes_int <= 59:
                            schedule['times'].append(time_value)
                    except ValueError:
                        pass  # Skip invalid times
        
        return schedule
    
    def _get_open_positions_schedule_config(self):
        """Get the current open positions schedule configuration as a JSON-serializable dict."""
        schedule = {
            'days': {},
            'times': []
        }
        
        # Get selected days
        if hasattr(self, 'open_positions_schedule_days'):
            for day, checkbox in self.open_positions_schedule_days.items():
                schedule['days'][day] = checkbox.value
        
        # Get execution times
        if hasattr(self, 'open_positions_execution_times'):
            for time_input in self.open_positions_execution_times:
                time_value = time_input.value.strip()
                if time_value and ':' in time_value:
                    try:
                        hours, minutes = time_value.split(':')
                        hours_int = int(hours)
                        minutes_int = int(minutes)
                        if 0 <= hours_int <= 23 and 0 <= minutes_int <= 59:
                            schedule['times'].append(time_value)
                    except ValueError:
                        pass  # Skip invalid times
        
        return schedule
    
    def _load_schedule_config(self, schedule_config):
        """Load schedule configuration from a JSON dict."""
        if not schedule_config:
            return
        
        # Guard check to ensure UI components exist
        if not hasattr(self, 'execution_times_container') or self.execution_times_container is None:
            return
            
        # Load days
        days = schedule_config.get('days', {})
        for day, checkbox in self.schedule_days.items():
            # Default weekdays to True, weekends to False if not specified
            default_value = day not in ['saturday', 'sunday']
            checkbox.value = days.get(day, default_value)
        
        # Load times
        times = schedule_config.get('times', ['09:30'])
        
        # Clear existing time inputs
        self.execution_times.clear()
        self.execution_times_container.clear()
        
        # Add time inputs for each configured time
        for time_str in times:
            self._add_time_input(time_str)
        
        # If no times were configured, add a default
        if not times:
            self._add_time_input('09:30')
    
    def _load_general_settings(self, expert_instance):
        """Load general settings (schedule and trading permissions) for an existing expert."""
        try:
            from ...core.utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(expert_instance.id)
            if not expert:
                return
            
            # Load execution schedule (entering market)
            enter_market_schedule = expert.settings.get('execution_schedule_enter_market')
            if enter_market_schedule:
                if isinstance(enter_market_schedule, str):
                    import json
                    try:
                        enter_market_schedule = json.loads(enter_market_schedule)
                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON in execution_schedule_enter_market: {enter_market_schedule}", exc_info=True)
                        enter_market_schedule = None
                
                if enter_market_schedule:
                    self._load_enter_market_schedule_config(enter_market_schedule)
            
            # Load execution schedule (open positions)
            open_positions_schedule = expert.settings.get('execution_schedule_open_positions')
            if open_positions_schedule:
                if isinstance(open_positions_schedule, str):
                    import json
                    try:
                        open_positions_schedule = json.loads(open_positions_schedule)
                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON in execution_schedule_open_positions: {open_positions_schedule}", exc_info=True)
                        open_positions_schedule = None
                
                if open_positions_schedule:
                    self._load_open_positions_schedule_config(open_positions_schedule)
            
            # Load trading permissions - convert to booleans if they're strings
            enable_buy = expert.settings.get('enable_buy', True)  # Default to True
            enable_sell = expert.settings.get('enable_sell', False)  # Default to False
            
            # Handle legacy automatic_trading setting by splitting it into new settings
            legacy_automatic_trading = expert.settings.get('automatic_trading', None)
            allow_automated_trade_opening = expert.settings.get('allow_automated_trade_opening', False)
            allow_automated_trade_modification = expert.settings.get('allow_automated_trade_modification', False)
            
            # If legacy setting exists and new settings don't, migrate the legacy setting
            if legacy_automatic_trading is not None and 'allow_automated_trade_opening' not in expert.settings and 'allow_automated_trade_modification' not in expert.settings:
                if isinstance(legacy_automatic_trading, str):
                    legacy_value = legacy_automatic_trading.lower() == 'true'
                else:
                    legacy_value = bool(legacy_automatic_trading)
                allow_automated_trade_opening = legacy_value
                allow_automated_trade_modification = legacy_value
            
            # Convert string values to booleans if needed
            if isinstance(enable_buy, str):
                enable_buy = enable_buy.lower() == 'true'
            if isinstance(enable_sell, str):
                enable_sell = enable_sell.lower() == 'true'
            if isinstance(allow_automated_trade_opening, str):
                allow_automated_trade_opening = allow_automated_trade_opening.lower() == 'true'
            if isinstance(allow_automated_trade_modification, str):
                allow_automated_trade_modification = allow_automated_trade_modification.lower() == 'true'
            
            if hasattr(self, 'enable_buy_checkbox'):
                self.enable_buy_checkbox.value = enable_buy
            if hasattr(self, 'enable_sell_checkbox'):
                self.enable_sell_checkbox.value = enable_sell
            if hasattr(self, 'allow_automated_trade_opening_checkbox'):
                self.allow_automated_trade_opening_checkbox.value = allow_automated_trade_opening
            if hasattr(self, 'allow_automated_trade_modification_checkbox'):
                self.allow_automated_trade_modification_checkbox.value = allow_automated_trade_modification
            
            # Load risk management settings
            max_virtual_equity_per_instrument = expert.settings.get('max_virtual_equity_per_instrument_percent', 10.0)
            if isinstance(max_virtual_equity_per_instrument, str):
                max_virtual_equity_per_instrument = float(max_virtual_equity_per_instrument)
            
            if hasattr(self, 'max_virtual_equity_per_instrument_input'):
                self.max_virtual_equity_per_instrument_input.value = str(max_virtual_equity_per_instrument)
            
            # Load ruleset assignments from ExpertInstance model
            if hasattr(self, 'enter_market_ruleset_select') and hasattr(self, 'enter_market_ruleset_map'):
                if expert_instance.enter_market_ruleset_id:
                    # Find the display name for this ruleset ID
                    for display_name, ruleset_id in self.enter_market_ruleset_map.items():
                        if ruleset_id == expert_instance.enter_market_ruleset_id:
                            self.enter_market_ruleset_select.value = display_name
                            break
                else:
                    self.enter_market_ruleset_select.value = '(None)'
            
            if hasattr(self, 'open_positions_ruleset_select') and hasattr(self, 'open_positions_ruleset_map'):
                if expert_instance.open_positions_ruleset_id:
                    # Find the display name for this ruleset ID
                    for display_name, ruleset_id in self.open_positions_ruleset_map.items():
                        if ruleset_id == expert_instance.open_positions_ruleset_id:
                            self.open_positions_ruleset_select.value = display_name
                            break
                else:
                    self.open_positions_ruleset_select.value = '(None)'
                
            logger.debug(f'Loaded general settings for expert {expert_instance.id}: enter_market_schedule={enter_market_schedule}, open_positions_schedule={open_positions_schedule}, buy={enable_buy}, sell={enable_sell}, auto_open={allow_automated_trade_opening}, auto_modify={allow_automated_trade_modification}, enter_market_ruleset_id={expert_instance.enter_market_ruleset_id}, open_positions_ruleset_id={expert_instance.open_positions_ruleset_id}')
            
        except Exception as e:
            logger.error(f'Error loading general settings for expert {expert_instance.id}: {e}', exc_info=True)

    def _add_time_input_enter_market(self, initial_time=''):
        """Add a new time input field to the enter market execution times container."""
        logger.debug(f"_add_time_input_enter_market called with initial_time={initial_time}")
        
        if not hasattr(self, 'enter_market_times_container') or self.enter_market_times_container is None:
            logger.warning("_add_time_input_enter_market called but enter_market_times_container is None")
            return
            
        try:
            with self.enter_market_times_container:
                with ui.row().classes('w-full gap-2'):
                    time_input = ui.input(
                        label='Time (HH:MM)', 
                        value=initial_time,
                        placeholder='09:30'
                    ).classes('flex-grow')
                    
                    def validate_time(e):
                        try:
                            time_str = time_input.value
                            if time_str and ':' in time_str:
                                hours, minutes = time_str.split(':')
                                if len(hours) == 2 and len(minutes) == 2:
                                    int(hours), int(minutes)
                                    if 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59:
                                        time_input.props('error=false')
                                        return
                            time_input.props('error=true error-message="Invalid time format (use HH:MM)"')
                        except ValueError:
                            time_input.props('error=true error-message="Invalid time format (use HH:MM)"')
                    
                    time_input.on('blur', validate_time)
                    ui.button(icon='remove', on_click=lambda: self._remove_time_input_enter_market(time_input)).props('flat round').classes('ml-2')
                    
                    if not hasattr(self, 'enter_market_execution_times'):
                        self.enter_market_execution_times = []
                    self.enter_market_execution_times.append(time_input)
        except Exception as e:
            logger.error(f"Error creating enter market time input: {e}", exc_info=True)

    def _remove_time_input_enter_market(self, time_input):
        """Remove a time input field from enter market schedule."""
        if len(self.enter_market_execution_times) <= 1:
            return  # Don't remove the last time input
            
        if time_input in self.enter_market_execution_times:
            self.enter_market_execution_times.remove(time_input)
            
        parent = time_input.parent_slot.parent
        parent.delete()

    def _add_time_input_open_positions(self, initial_time=''):
        """Add a new time input field to the open positions execution times container."""
        logger.debug(f"_add_time_input_open_positions called with initial_time={initial_time}")
        
        if not hasattr(self, 'open_positions_times_container') or self.open_positions_times_container is None:
            logger.warning("_add_time_input_open_positions called but open_positions_times_container is None")
            return
            
        try:
            with self.open_positions_times_container:
                with ui.row().classes('w-full gap-2'):
                    time_input = ui.input(
                        label='Time (HH:MM)', 
                        value=initial_time,
                        placeholder='09:30'
                    ).classes('flex-grow')
                    
                    def validate_time(e):
                        try:
                            time_str = time_input.value
                            if time_str and ':' in time_str:
                                hours, minutes = time_str.split(':')
                                if len(hours) == 2 and len(minutes) == 2:
                                    int(hours), int(minutes)
                                    if 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59:
                                        time_input.props('error=false')
                                        return
                            time_input.props('error=true error-message="Invalid time format (use HH:MM)"')
                        except ValueError:
                            time_input.props('error=true error-message="Invalid time format (use HH:MM)"')
                    
                    time_input.on('blur', validate_time)
                    ui.button(icon='remove', on_click=lambda: self._remove_time_input_open_positions(time_input)).props('flat round').classes('ml-2')
                    
                    if not hasattr(self, 'open_positions_execution_times'):
                        self.open_positions_execution_times = []
                    self.open_positions_execution_times.append(time_input)
        except Exception as e:
            logger.error(f"Error creating open positions time input: {e}", exc_info=True)

    def _remove_time_input_open_positions(self, time_input):
        """Remove a time input field from open positions schedule."""
        if len(self.open_positions_execution_times) <= 1:
            return  # Don't remove the last time input
            
        if time_input in self.open_positions_execution_times:
            self.open_positions_execution_times.remove(time_input)
            
        parent = time_input.parent_slot.parent
        parent.delete()

    def _load_enter_market_schedule_config(self, schedule_config):
        """Load enter market schedule configuration from a JSON dict."""
        if not schedule_config:
            return
        
        if not hasattr(self, 'enter_market_times_container') or self.enter_market_times_container is None:
            return
            
        # Load days
        days = schedule_config.get('days', {})
        if hasattr(self, 'enter_market_schedule_days'):
            for day, checkbox in self.enter_market_schedule_days.items():
                default_value = day not in ['saturday', 'sunday']
                checkbox.value = days.get(day, default_value)
        
        # Load times
        times = schedule_config.get('times', ['09:30'])
        
        # Clear existing time inputs
        if hasattr(self, 'enter_market_execution_times'):
            self.enter_market_execution_times.clear()
        self.enter_market_times_container.clear()
        
        # Add time inputs for each configured time
        for time_str in times:
            self._add_time_input_enter_market(time_str)
        
        # If no times were configured, add a default
        if not times:
            self._add_time_input_enter_market('09:30')

    def _load_open_positions_schedule_config(self, schedule_config):
        """Load open positions schedule configuration from a JSON dict."""
        if not schedule_config:
            return
        
        if not hasattr(self, 'open_positions_times_container') or self.open_positions_times_container is None:
            return
            
        # Load days
        days = schedule_config.get('days', {})
        if hasattr(self, 'open_positions_schedule_days'):
            for day, checkbox in self.open_positions_schedule_days.items():
                default_value = day not in ['saturday', 'sunday']
                checkbox.value = days.get(day, default_value)
        
        # Load times
        times = schedule_config.get('times', ['15:00'])
        
        # Clear existing time inputs
        if hasattr(self, 'open_positions_execution_times'):
            self.open_positions_execution_times.clear()
        self.open_positions_times_container.clear()
        
        # Add time inputs for each configured time
        for time_str in times:
            self._add_time_input_open_positions(time_str)
        
        # If no times were configured, add defaults
        if not times:
            for time in ['09:30', '10:30', '11:30', '12:30', '13:30', '14:30', '15:30']:
                self._add_time_input_open_positions(time)
    
    def _on_expert_type_change_dialog(self, event, expert_instance):
        """Handle expert type change in the dialog."""
        logger.debug(f'Expert type changed in dialog to: {event.value if hasattr(event, "value") else event}')
        self._update_expert_description()
        self._render_expert_settings(expert_instance)
    
    def _render_expert_settings(self, expert_instance=None):
        """Render expert-specific settings based on the selected expert type."""
        self.expert_settings_container.clear()
        
        expert_type = self.expert_select.value if hasattr(self, 'expert_select') else None
        if not expert_type:
            ui.label('Select an expert type to see settings').move(self.expert_settings_container)
            return
        
        expert_class = self._get_expert_class(expert_type)
        if not expert_class:
            ui.label('No settings available for this expert type').move(self.expert_settings_container)
            return
        
        try:
            # Get settings definitions
            settings_def = expert_class.get_settings_definitions()
            current_settings = {}
            
            if expert_instance:
                expert = expert_class(expert_instance.id)
                current_settings = expert.settings  # This will include defaults for missing values
            
            self.expert_settings_inputs = {}
            
            if settings_def and len(settings_def.keys()) > 0:
                for key, meta in settings_def.items():
                    label = meta.get("description", key)
                    # Use current setting value or fall back to default
                    current_value = current_settings.get(key)
                    default_value = meta.get("default")
                    
                    # Check if setting has valid_values (dropdown)
                    valid_values = meta.get("valid_values")
                    help_text = meta.get("help")
                    tooltip_text = meta.get("tooltip")
                    
                    # Create a container for this setting (title + input)
                    setting_container = ui.column().classes('w-full mb-4')
                    with setting_container:
                        # Create label with tooltip inline
                        if tooltip_text:
                            with ui.row().classes('items-center gap-1 mb-2'):
                                ui.label(label).classes('text-sm font-medium')
                                ui.icon('help_outline', size='sm').classes('text-gray-500 cursor-help').tooltip(tooltip_text).style('font-size: 18px !important; padding: 12px !important; max-width: 350px !important; line-height: 1.4 !important;')
                            
                            # Use empty label for input since we show it above
                            display_label = ""
                        else:
                            display_label = label
                        
                        # Create the input field directly in the same container
                        if meta["type"] == "str":
                            value = current_value if current_value is not None else default_value or ""
                            if valid_values:
                                # Show as dropdown
                                inp = ui.select(
                                    options=valid_values,
                                    label=display_label,
                                    value=value if value in valid_values else (valid_values[0] if valid_values else "")
                                ).classes('w-full')
                            else:
                                inp = ui.input(label=display_label, value=value).classes('w-full')
                        elif meta["type"] == "list":
                            # Handle list-type settings
                            value = current_value if current_value is not None else default_value or []
                            if valid_values and meta.get("multiple", False):
                                # Show as multi-select dropdown
                                inp = ui.select(
                                    options=valid_values,
                                    label=display_label,
                                    value=value if isinstance(value, list) else [value] if value else [],
                                    multiple=True
                                ).classes('w-full')
                            else:
                                # Fallback to JSON input for list without valid_values
                                import json
                                inp = ui.input(label=display_label, value=json.dumps(value)).classes('w-full')
                        elif meta["type"] == "bool":
                            value = current_value if current_value is not None else default_value or False
                            inp = ui.checkbox(text=display_label, value=bool(value))
                        elif meta["type"] == "float":
                            value = current_value if current_value is not None else default_value or 0.0
                            inp = ui.input(label=display_label, value=str(value)).classes('w-full')
                        else:
                            value = current_value if current_value is not None else default_value or ""
                            if valid_values:
                                # Show as dropdown for other types too if valid_values exist
                                inp = ui.select(
                                    options=valid_values,
                                    label=display_label,
                                    value=value if value in valid_values else (valid_values[0] if valid_values else "")
                                ).classes('w-full')
                            else:
                                inp = ui.input(label=display_label, value=str(value)).classes('w-full')
                        
                        # Add help text if available
                        if help_text:
                            ui.markdown(help_text).classes('text-sm text-gray-600 mt-1')
                    
                    setting_container.move(self.expert_settings_container)
                    
                    self.expert_settings_inputs[key] = inp
            else:
                ui.label("No expert-specific settings available.").move(self.expert_settings_container)
                
        except Exception as e:
            logger.error(f'Error rendering expert settings: {e}', exc_info=True)
            ui.label(f"Error loading settings: {e}").move(self.expert_settings_container)
    
    def _on_expert_type_change(self, event, expert_instance):
        """Handle expert type change."""
        logger.debug(f'Expert type changed to: {event.value if hasattr(event, "value") else event}')
        self._render_expert_settings(expert_instance)
    
    def _render_cleanup_tab(self, expert_instance=None):
        """Render the cleanup tab for managing old analysis data."""
        ui.label('Database Cleanup').classes('text-subtitle1 mb-4')
        ui.label('Remove old market analysis records and associated data to keep your database clean.').classes('text-body2 mb-4')
        
        # Show statistics first
        with ui.card().classes('w-full mb-4'):
            ui.label('Current Database Statistics').classes('text-subtitle2 mb-2')
            self.cleanup_stats_container = ui.column().classes('w-full')
            self._refresh_cleanup_statistics(expert_instance)
        
        # Cleanup configuration
        with ui.card().classes('w-full mb-4'):
            ui.label('Cleanup Configuration').classes('text-subtitle2 mb-2')
            
            # Days to keep
            ui.label('Analyses older than this many days will be cleaned up:').classes('text-body2 mb-2')
            self.cleanup_days_input = ui.number(
                label='Days to Keep',
                value=30,
                min=1,
                max=365,
                step=1,
                format='%.0f'
            ).classes('w-full mb-4').props('outlined')
            
            # Status filter
            ui.label('Select which analysis statuses to clean up:').classes('text-body2 mb-2')
            with ui.column().classes('w-full mb-4'):
                self.cleanup_status_checkboxes = {}
                for status in MarketAnalysisStatus:
                    # Default: clean up COMPLETED and FAILED, but not PENDING, RUNNING, or CANCELLED
                    default_checked = status in [MarketAnalysisStatus.COMPLETED, MarketAnalysisStatus.FAILED]
                    self.cleanup_status_checkboxes[status] = ui.checkbox(
                        status.value.upper(),
                        value=default_checked
                    ).classes('mb-2')
            
            ui.label('⚠️ Analyses with open transactions will never be deleted.').classes('text-caption text-orange mb-2')
        
        # Preview results container
        with ui.card().classes('w-full mb-4'):
            ui.label('Preview').classes('text-subtitle2 mb-2')
            self.cleanup_preview_container = ui.column().classes('w-full')
            ui.label('Click "Preview Cleanup" to see what will be deleted.').classes('text-body2 text-grey')
        
        # Action buttons
        with ui.row().classes('w-full gap-2 justify-end'):
            ui.button(
                'Preview Cleanup',
                icon='visibility',
                on_click=lambda: self._preview_cleanup(expert_instance)
            ).props('outlined')
            
            self.cleanup_execute_button = ui.button(
                'Execute Cleanup',
                icon='delete_sweep',
                on_click=lambda: self._execute_cleanup(expert_instance)
            ).props('color=orange')
            self.cleanup_execute_button.set_enabled(False)  # Disabled until preview is run
    
    def _refresh_cleanup_statistics(self, expert_instance=None):
        """Refresh the cleanup statistics display."""
        self.cleanup_stats_container.clear()
        
        try:
            expert_id = expert_instance.id if expert_instance else None
            stats = get_cleanup_statistics(expert_id)
            
            with self.cleanup_stats_container:
                with ui.grid(columns=2).classes('w-full gap-4'):
                    # Total analyses
                    with ui.card().classes('p-4'):
                        ui.label('Total Analyses').classes('text-caption text-grey')
                        ui.label(str(stats['total_analyses'])).classes('text-h6')
                    
                    # Total outputs
                    with ui.card().classes('p-4'):
                        ui.label('Total Outputs').classes('text-caption text-grey')
                        ui.label(str(stats['total_outputs'])).classes('text-h6')
                    
                    # Total recommendations
                    with ui.card().classes('p-4'):
                        ui.label('Total Recommendations').classes('text-caption text-grey')
                        ui.label(str(stats['total_recommendations'])).classes('text-h6')
                
                # By status
                if stats['analyses_by_status']:
                    ui.label('Analyses by Status:').classes('text-body2 mt-4 mb-2')
                    with ui.grid(columns=3).classes('w-full gap-2'):
                        for status, count in sorted(stats['analyses_by_status'].items()):
                            with ui.card().classes('p-2'):
                                ui.label(status.upper()).classes('text-caption text-grey')
                                ui.label(str(count)).classes('text-subtitle2')
                
                # By age
                ui.label('Analyses by Age:').classes('text-body2 mt-4 mb-2')
                with ui.grid(columns=5).classes('w-full gap-2'):
                    age_labels = {
                        '7_days': '< 7 days',
                        '30_days': '7-30 days',
                        '90_days': '30-90 days',
                        '180_days': '90-180 days',
                        'older': '> 180 days'
                    }
                    for age_key, label in age_labels.items():
                        count = stats['analyses_by_age'].get(age_key, 0)
                        with ui.card().classes('p-2'):
                            ui.label(label).classes('text-caption text-grey')
                            ui.label(str(count)).classes('text-subtitle2')
        
        except Exception as e:
            logger.error(f'Error refreshing cleanup statistics: {e}')
            with self.cleanup_stats_container:
                ui.label(f'Error loading statistics: {str(e)}').classes('text-negative')
    
    def _preview_cleanup(self, expert_instance=None):
        """Preview what would be cleaned up."""
        self.cleanup_preview_container.clear()
        
        try:
            # Get selected statuses
            selected_statuses = [
                status for status, checkbox in self.cleanup_status_checkboxes.items()
                if checkbox.value
            ]
            
            if not selected_statuses:
                with self.cleanup_preview_container:
                    ui.label('⚠️ Please select at least one status to clean up.').classes('text-orange')
                self.cleanup_execute_button.set_enabled(False)
                return
            
            # Get days to keep
            days_to_keep = int(self.cleanup_days_input.value)
            
            # Get preview
            expert_id = expert_instance.id if expert_instance else None
            preview = preview_cleanup(
                days_to_keep=days_to_keep,
                statuses=selected_statuses,
                expert_instance_id=expert_id
            )
            
            with self.cleanup_preview_container:
                # Summary
                with ui.card().classes('w-full p-4 mb-4').style('border: 2px solid orange'):
                    ui.label('Cleanup Summary').classes('text-subtitle2 mb-2')
                    
                    if preview['deletable_analyses'] == 0:
                        ui.label('✅ No analyses to clean up with current settings.').classes('text-positive')
                        self.cleanup_execute_button.set_enabled(False)
                        return
                    
                    with ui.grid(columns=2).classes('w-full gap-4'):
                        with ui.column():
                            ui.label(f"Will delete: {preview['deletable_analyses']} analyses").classes('text-body1 font-bold text-orange')
                            ui.label(f"Protected: {preview['protected_analyses']} analyses (have open transactions)").classes('text-body2')
                        
                        with ui.column():
                            ui.label(f"Outputs to delete: {preview['estimated_outputs_deleted']}").classes('text-body2')
                            ui.label(f"Recommendations to delete: {preview['estimated_recommendations_deleted']}").classes('text-body2')
                
                # Details table
                if preview['preview_items']:
                    ui.label('Sample of analyses to be deleted (up to 100):').classes('text-body2 mt-4 mb-2')
                    
                    columns = [
                        {'name': 'id', 'label': 'ID', 'field': 'id', 'align': 'left', 'sortable': True},
                        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left', 'sortable': True},
                        {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'left', 'sortable': True},
                        {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left', 'sortable': True},
                        {'name': 'outputs_count', 'label': 'Outputs', 'field': 'outputs_count', 'align': 'right', 'sortable': True},
                        {'name': 'recommendations_count', 'label': 'Recs', 'field': 'recommendations_count', 'align': 'right', 'sortable': True}
                    ]
                    
                    ui.table(
                        columns=columns,
                        rows=preview['preview_items'],
                        row_key='id'
                    ).classes('w-full')
                
                # Enable execute button
                self.cleanup_execute_button.set_enabled(True)
        
        except Exception as e:
            logger.error(f'Error previewing cleanup: {e}')
            with self.cleanup_preview_container:
                ui.label(f'❌ Error: {str(e)}').classes('text-negative')
            self.cleanup_execute_button.set_enabled(False)
    
    def _execute_cleanup(self, expert_instance=None):
        """Execute the cleanup operation."""
        # Create confirmation dialog
        with ui.dialog() as dialog, ui.card():
            ui.label('⚠️ Confirm Cleanup').classes('text-h6 mb-4')
            ui.label('This will permanently delete the previewed analyses and their data.').classes('text-body1 mb-4')
            ui.label('Are you sure you want to continue?').classes('text-body2 mb-4')
            
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Delete', on_click=lambda: self._perform_cleanup(expert_instance, dialog)).props('color=negative')
        
        dialog.open()
    
    def _perform_cleanup(self, expert_instance, dialog):
        """Perform the actual cleanup operation."""
        dialog.close()
        
        try:
            # Get selected statuses
            selected_statuses = [
                status for status, checkbox in self.cleanup_status_checkboxes.items()
                if checkbox.value
            ]
            
            # Get days to keep
            days_to_keep = int(self.cleanup_days_input.value)
            
            # Execute cleanup
            expert_id = expert_instance.id if expert_instance else None
            cleanup_result = execute_cleanup(
                days_to_keep=days_to_keep,
                statuses=selected_statuses,
                expert_instance_id=expert_id
            )
            
            if cleanup_result['success']:
                message = f"✅ Cleanup completed!\n"
                message += f"Deleted: {cleanup_result['analyses_deleted']} analyses\n"
                message += f"Protected: {cleanup_result['analyses_protected']} analyses with open transactions\n"
                message += f"Outputs deleted: {cleanup_result['outputs_deleted']}\n"
                message += f"Recommendations deleted: {cleanup_result['recommendations_deleted']}"
                
                if cleanup_result['errors']:
                    message += f"\n⚠️ {len(cleanup_result['errors'])} errors occurred"
                
                ui.notify(message, type='positive', multi_line=True, timeout=5000)
                
                # Refresh statistics
                self._refresh_cleanup_statistics(expert_instance)
                
                # Clear preview
                self.cleanup_preview_container.clear()
                with self.cleanup_preview_container:
                    ui.label('Click "Preview Cleanup" to see what will be deleted.').classes('text-body2 text-grey')
                
                # Disable execute button
                self.cleanup_execute_button.set_enabled(False)
            else:
                error_msg = "❌ Cleanup failed:\n" + "\n".join(cleanup_result['errors'])
                ui.notify(error_msg, type='negative', multi_line=True, timeout=5000)
        
        except Exception as e:
            logger.error(f'Error executing cleanup: {e}')
            ui.notify(f'❌ Error: {str(e)}', type='negative')
    
    def _on_instrument_selection_change(self, selected_instruments):
        """Handle instrument selection changes."""
        logger.debug(f'Instrument selection changed: {len(selected_instruments)} instruments selected')
    
    def _save_expert(self, expert_instance=None):
        """Save the expert instance."""
        try:
            is_edit = expert_instance is not None
            
            # Get account ID from the selected account string
            account_id = self._get_account_id_from_display_string(self.account_select.value)
            if not account_id:
                ui.notify('Please select a valid trading account', type='negative')
                return
            
            if is_edit:
                # Update existing instance
                expert_instance.expert = self.expert_select.value
                expert_instance.alias = self.alias_input.value or None
                expert_instance.user_description = self.user_description_textarea.value or None
                expert_instance.enabled = self.enabled_checkbox.value
                expert_instance.virtual_equity_pct = float(self.virtual_equity_input.value)
                expert_instance.account_id = account_id
                
                # Update ruleset assignments - convert display name to ID
                if hasattr(self, 'enter_market_ruleset_select') and hasattr(self, 'enter_market_ruleset_map'):
                    selected_display_name = self.enter_market_ruleset_select.value
                    expert_instance.enter_market_ruleset_id = self.enter_market_ruleset_map.get(selected_display_name)
                if hasattr(self, 'open_positions_ruleset_select') and hasattr(self, 'open_positions_ruleset_map'):
                    selected_display_name = self.open_positions_ruleset_select.value
                    expert_instance.open_positions_ruleset_id = self.open_positions_ruleset_map.get(selected_display_name)
                
                update_instance(expert_instance)
                logger.info(f"Updated expert instance: {expert_instance.id}")
                
                expert_id = expert_instance.id
            else:
                # Create new instance
                enter_market_id = None
                open_positions_id = None
                if hasattr(self, 'enter_market_ruleset_select') and hasattr(self, 'enter_market_ruleset_map'):
                    selected_display_name = self.enter_market_ruleset_select.value
                    enter_market_id = self.enter_market_ruleset_map.get(selected_display_name)
                if hasattr(self, 'open_positions_ruleset_select') and hasattr(self, 'open_positions_ruleset_map'):
                    selected_display_name = self.open_positions_ruleset_select.value
                    open_positions_id = self.open_positions_ruleset_map.get(selected_display_name)
                
                new_instance = ExpertInstance(
                    expert=self.expert_select.value,
                    alias=self.alias_input.value or None,
                    user_description=self.user_description_textarea.value or None,
                    enabled=self.enabled_checkbox.value,
                    virtual_equity_pct=float(self.virtual_equity_input.value),
                    account_id=account_id,
                    enter_market_ruleset_id=enter_market_id,
                    open_positions_ruleset_id=open_positions_id
                )
                
                expert_id = add_instance(new_instance)
                logger.info(f"Created new expert instance: {expert_id}")
            
            # Save expert-specific settings
            self._save_expert_settings(expert_id)
            
            # Check if any instruments are enabled
            has_instruments = False
            if self.instrument_selector:
                selected_instruments = self.instrument_selector.get_selected_instruments()
                has_instruments = len(selected_instruments) > 0
            
            # Save instrument configuration
            self._save_instrument_configuration(expert_id)
            
            self.dialog.close()
            self._update_table_rows()
            
            # Show success notification with warning if no instruments
            if has_instruments:
                ui.notify('Expert saved successfully!', type='positive')
            else:
                ui.notify('Expert saved successfully! ⚠️ Warning: No instruments enabled. This expert will not analyze any symbols.', 
                         type='warning', timeout=5000)
            
        except Exception as e:
            logger.error(f"Error saving expert: {e}", exc_info=True)
            ui.notify(f"Error saving expert: {e}", type='negative')
    
    def _save_expert_settings(self, expert_id):
        """Save expert-specific settings and general settings."""
        expert_class = self._get_expert_class(self.expert_select.value)
        if not expert_class:
            return
        
        expert = expert_class(expert_id)
        
        # Save general settings (schedules and trading permissions)
        if hasattr(self, 'enter_market_schedule_days') and hasattr(self, 'enter_market_execution_times'):
            schedule_config = self._get_enter_market_schedule_config()
            expert.save_setting('execution_schedule_enter_market', schedule_config, setting_type="json")
            logger.debug(f'Saved execution_schedule_enter_market: {schedule_config}')
        
        if hasattr(self, 'open_positions_schedule_days') and hasattr(self, 'open_positions_execution_times'):
            schedule_config_open = self._get_open_positions_schedule_config()
            expert.save_setting('execution_schedule_open_positions', schedule_config_open, setting_type="json")
            logger.debug(f'Saved execution_schedule_open_positions: {schedule_config_open}')
        
        if (hasattr(self, 'enable_buy_checkbox') and hasattr(self, 'enable_sell_checkbox') and 
            hasattr(self, 'allow_automated_trade_opening_checkbox') and hasattr(self, 'allow_automated_trade_modification_checkbox')):
            expert.save_setting('enable_buy', self.enable_buy_checkbox.value, setting_type="bool")
            expert.save_setting('enable_sell', self.enable_sell_checkbox.value, setting_type="bool")
            expert.save_setting('allow_automated_trade_opening', self.allow_automated_trade_opening_checkbox.value, setting_type="bool")
            expert.save_setting('allow_automated_trade_modification', self.allow_automated_trade_modification_checkbox.value, setting_type="bool")
            logger.debug(f'Saved trading permissions: buy={self.enable_buy_checkbox.value}, sell={self.enable_sell_checkbox.value}, auto_open={self.allow_automated_trade_opening_checkbox.value}, auto_modify={self.allow_automated_trade_modification_checkbox.value}')
        
        # Save risk management settings
        if hasattr(self, 'max_virtual_equity_per_instrument_input'):
            max_equity_value = float(self.max_virtual_equity_per_instrument_input.value or 10.0)
            expert.save_setting('max_virtual_equity_per_instrument_percent', max_equity_value, setting_type="float")
            logger.debug(f'Saved risk management: max_virtual_equity_per_instrument_percent={max_equity_value}%')
        
        # Save expert-specific settings
        if hasattr(self, 'expert_settings_inputs') and self.expert_settings_inputs:
            settings_def = expert.get_settings_definitions()
            
            for key, inp in self.expert_settings_inputs.items():
                meta = settings_def.get(key, {})
                
                if meta.get("type") == "bool":
                    expert.save_setting(key, inp.value, setting_type="bool")
                elif meta.get("type") == "float":
                    expert.save_setting(key, float(inp.value or 0), setting_type="float")
                elif meta.get("type") == "list":
                    # Handle list types - save as JSON
                    expert.save_setting(key, inp.value, setting_type="json")
                else:
                    expert.save_setting(key, inp.value, setting_type="str")
        
        logger.debug(f'Saved all expert settings for instance {expert_id}')
        
        # Refresh scheduled jobs for this expert
        try:
            from ...core.JobManager import get_job_manager
            job_manager = get_job_manager()
            job_manager.refresh_expert_schedules(expert_id)
            logger.info(f'Refreshed scheduled analysis jobs for expert {expert_id}')
        except Exception as e:
            logger.error(f'Error refreshing scheduled jobs for expert {expert_id}: {e}', exc_info=True)
    
    def _save_instrument_configuration(self, expert_id):
        """Save instrument selection and configuration."""
        if not self.instrument_selector:
            return
        
        selected_instruments = self.instrument_selector.get_selected_instruments()
        
        # Convert to format expected by expert
        instrument_configs = {}
        for inst in selected_instruments:
            instrument_configs[inst['name']] = {
                'enabled': True,
                'weight': inst['weight']
            }
        
        # Get expert and save configuration
        expert_class = self._get_expert_class(self.expert_select.value)
        if expert_class:
            expert = expert_class(expert_id)
            expert.set_enabled_instruments(instrument_configs)
            
        logger.debug(f'Saved instrument configuration for expert {expert_id}: {len(instrument_configs)} instruments')
    
    def _on_table_edit_click(self, msg):
        """Handle edit button click from table."""
        logger.debug(f'Edit expert table click: {msg}')
        row = msg.args['row']
        expert_id = row['id']
        expert_instance = get_instance(ExpertInstance, expert_id)
        if expert_instance:
            self.show_dialog(expert_instance)
        else:
            logger.warning(f'Expert instance with id {expert_id} not found')
            ui.notify('Expert instance not found', type='error')
    
    def _on_table_del_click(self, msg):
        """Handle delete button click from table."""
        logger.debug(f'Delete expert table click: {msg}')
        row = msg.args['row']
        expert_id = row['id']
        expert_instance = get_instance(ExpertInstance, expert_id)
        if expert_instance:
            try:
                logger.debug(f'Deleting expert instance {expert_id}')
                delete_instance(expert_instance)
                logger.info(f'Expert instance {expert_id} deleted')
                ui.notify('Expert instance deleted', type='positive')
                self._update_table_rows()
            except Exception as e:
                logger.error(f'Error deleting expert instance {expert_id}: {e}', exc_info=True)
                ui.notify(f'Error deleting expert instance: {e}', type='negative')
        else:
            logger.warning(f'Expert instance with id {expert_id} not found')
            ui.notify('Expert instance not found', type='error')


class TradeSettingsTab:
    """
    UI tab for managing trading rules and rulesets.
    Features: separate sections for rules (EventAction) and rulesets (Ruleset) with edit/delete functions.
    """
    
    def __init__(self):
        logger.debug('Initializing TradeSettingsTab')
        self.rules_dialog = ui.dialog()
        self.rulesets_dialog = ui.dialog()
        self.reorder_dialog = ui.dialog()
        self.rules_table = None
        self.rulesets_table = None
        self.triggers = {}
        self.actions = {}
        self.rules_export_import_ui = RulesExportImportUI()
        self.render()
    
    def render(self):
        logger.debug('Rendering TradeSettingsTab UI')
        
        with ui.card().classes('w-full'):
            ui.label('Trading Rules and Rulesets Management').classes('text-h6 mb-4')
            
            # Create tabs for rules and rulesets
            with ui.tabs() as trade_tabs:
                ui.tab('Rules', icon='rule')
                ui.tab('Rulesets', icon='list_alt')
            
            with ui.tab_panels(trade_tabs, value='Rules').classes('w-full'):
                # Rules tab
                with ui.tab_panel('Rules'):
                    with ui.row().classes('w-full justify-between items-center mb-4'):
                        with ui.column():
                            ui.label('Trading Rules (EventAction)').classes('text-h6 mb-0')
                            ui.label('Rules define triggers and actions for automated trading decisions.').classes('text-grey-7')
                        
                        # Help button for rules documentation
                        ui.button('📚 Help & Documentation', on_click=self._show_rules_help, icon='help_outline').props('color=info').tooltip('View comprehensive rules documentation with examples')
                    
                    with ui.row().classes('w-full justify-end gap-2 mb-4'):
                        ui.button('Import Rules', on_click=self.rules_export_import_ui.show_import_dialog, icon='upload_file').props('flat')
                        ui.button('Export Rules', on_click=self.rules_export_import_ui.show_export_rules_dialog, icon='download').props('flat')
                    
                    ui.button('Add Rule', on_click=lambda: self.show_rule_dialog(), icon='add').classes('mb-4')
                    
                    self.rules_table = ui.table(
                        columns=[
                            {'name': 'name', 'label': 'Name', 'field': 'name', 'sortable': True},
                            {'name': 'subtype', 'label': 'Subtype', 'field': 'subtype', 'sortable': True},
                            {'name': 'triggers_count', 'label': 'Triggers', 'field': 'triggers_count', 'align': 'center'},
                            {'name': 'actions_count', 'label': 'Actions', 'field': 'actions_count', 'align': 'center'},
                            {'name': 'continue_processing', 'label': 'Continue Processing', 'field': 'continue_processing', 'align': 'center'},
                            {'name': 'actions', 'label': 'Actions', 'field': 'actions'}
                        ],
                        rows=self._get_all_rules(),
                        row_key='id'
                    ).classes('w-full')
                    
                    self.rules_table.add_slot('body-cell-continue_processing', '''
                        <q-td :props="props">
                            <q-icon :name="props.value ? 'check_circle' : 'cancel'" 
                                    :color="props.value ? 'green' : 'red'" />
                        </q-td>
                    ''')
                    
                    self.rules_table.add_slot('body-cell-actions', """
                        <q-td :props="props">
                            <q-btn @click="$parent.$emit('edit', props)" icon="edit" flat dense color='blue' title="Edit Rule"/>
                            <q-btn @click="$parent.$emit('duplicate', props)" icon="content_copy" flat dense color='green' title="Duplicate Rule"/>
                            <q-btn @click="$parent.$emit('del', props)" icon="delete" flat dense color='red' title="Delete Rule"/>
                        </q-td>
                    """)
                    
                    self.rules_table.on('edit', self._on_rule_edit_click)
                    self.rules_table.on('duplicate', self._on_rule_duplicate_click)
                    self.rules_table.on('del', self._on_rule_del_click)
                
                # Rulesets tab
                with ui.tab_panel('Rulesets'):
                    with ui.row().classes('w-full justify-between items-center mb-4'):
                        with ui.column():
                            ui.label('Trading Rulesets').classes('text-h6 mb-0')
                            ui.label('Rulesets are collections of rules that work together for specific trading strategies.').classes('text-grey-7')
                        
                        # Help button for rules documentation
                        ui.button('Help', on_click=self._show_rules_help, icon='help_outline').props('color=info outline')
                    
                    with ui.row().classes('w-full justify-end gap-2 mb-4'):
                        ui.button('Import Rulesets', on_click=self.rules_export_import_ui.show_import_dialog, icon='upload_file').props('flat')
                        ui.button('Export Rulesets', on_click=self.rules_export_import_ui.show_export_rulesets_dialog, icon='download').props('flat')
                    
                    ui.button('Add Ruleset', on_click=lambda: self.show_ruleset_dialog(), icon='add').classes('mb-4')
                    
                    self.rulesets_table = ui.table(
                        columns=[
                            {'name': 'name', 'label': 'Name', 'field': 'name', 'sortable': True},
                            {'name': 'description', 'label': 'Description', 'field': 'description'},
                            {'name': 'subtype', 'label': 'Subtype', 'field': 'subtype', 'sortable': True},
                            {'name': 'rules_count', 'label': 'Rules Count', 'field': 'rules_count', 'align': 'center'},
                            {'name': 'actions', 'label': 'Actions', 'field': 'actions'}
                        ],
                        rows=self._get_all_rulesets(),
                        row_key='id'
                    ).classes('w-full')
                    
                    self.rulesets_table.add_slot('body-cell-actions', """
                        <q-td :props="props">
                            <q-btn @click="$parent.$emit('test', props)" icon="science" flat dense color='green' title="Test Ruleset"/>
                            <q-btn @click="$parent.$emit('edit', props)" icon="edit" flat dense color='blue' title="Edit Ruleset"/>
                            <q-btn @click="$parent.$emit('duplicate', props)" icon="content_copy" flat dense color='orange' title="Duplicate Ruleset"/>
                            <q-btn @click="$parent.$emit('reorder', props)" icon="reorder" flat dense color='purple' title="Reorder Rules"/>
                            <q-btn @click="$parent.$emit('del', props)" icon="delete" flat dense color='red' title="Delete Ruleset"/>
                        </q-td>
                    """)
                    
                    self.rulesets_table.on('test', self._on_ruleset_test_click)
                    self.rulesets_table.on('edit', self._on_ruleset_edit_click)
                    self.rulesets_table.on('duplicate', self._on_ruleset_duplicate_click)
                    self.rulesets_table.on('reorder', self._on_ruleset_reorder_click)
                    self.rulesets_table.on('del', self._on_ruleset_del_click)
        
        logger.debug('TradeSettingsTab UI rendered')
    
    def _show_rules_help(self):
        """Show comprehensive rules documentation dialog."""
        try:
            from ...core.rules_documentation import get_rules_overview_html
            
            with ui.dialog() as help_dialog, ui.card().classes('w-full max-w-4xl'):
                help_html = get_rules_overview_html()
                ui.html(help_html, sanitize=False)
                
                with ui.row().classes('w-full justify-end mt-4'):
                    ui.button('Close', on_click=help_dialog.close).props('color=primary')
            
            help_dialog.open()
            
        except Exception as e:
            logger.error(f"Error showing rules help: {e}", exc_info=True)
            ui.notify(f'Error loading help: {str(e)}', type='negative')
    
    def _get_all_rules(self):
        """Get all rules (EventAction) for the table."""
        logger.debug('Fetching all rules for table')
        try:
            rules = get_all_instances(EventAction)
            rows = []
            for rule in rules:
                # Manually create dict with JSON-serializable values
                row = {
                    'id': rule.id,
                    'name': rule.name,
                    'type': rule.type.value if rule.type else None,  # Convert enum to string
                    'subtype': rule.subtype.value if rule.subtype else 'Not set',
                    'continue_processing': rule.continue_processing,
                    'triggers_count': len(rule.triggers) if rule.triggers else 0,
                    'actions_count': len(rule.actions) if rule.actions else 0
                }
                rows.append(row)
            logger.debug(f'Fetched {len(rows)} rules')
            return rows
        except Exception as e:
            logger.error(f'Error fetching rules: {e}', exc_info=True)
            return []
    
    def _get_all_rulesets(self):
        """Get all rulesets for the table."""
        logger.debug('Fetching all rulesets for table')
        try:
            session = get_db()
            from sqlmodel import select
            from sqlalchemy.orm import selectinload
            
            # Use selectinload to eagerly load the event_actions relationship
            statement = select(Ruleset).options(selectinload(Ruleset.event_actions))
            results = session.exec(statement)
            rulesets = results.all()
            
            rows = []
            for ruleset in rulesets:
                # Manually create dict with JSON-serializable values
                row = {
                    'id': ruleset.id,
                    'name': ruleset.name,
                    'description': ruleset.description[:47] + '...' if ruleset.description and len(ruleset.description) > 50 else ruleset.description,
                    'subtype': ruleset.subtype.value if ruleset.subtype else 'Not set',
                    'rules_count': len(ruleset.event_actions) if ruleset.event_actions else 0
                }
                rows.append(row)
            
            session.close()
            logger.debug(f'Fetched {len(rows)} rulesets')
            return rows
        except Exception as e:
            logger.error(f'Error fetching rulesets: {e}', exc_info=True)
            return []
    
    def _update_rules_table(self):
        """Update the rules table with fresh data."""
        if self.rules_table:
            self.rules_table.rows = self._get_all_rules()
            logger.debug('Rules table rows updated')
    
    def _update_rulesets_table(self):
        """Update the rulesets table with fresh data."""
        if self.rulesets_table:
            self.rulesets_table.rows = self._get_all_rulesets()
            logger.debug('Rulesets table rows updated')
    
    def show_rule_dialog(self, rule=None):
        """Show the add/edit rule dialog."""
        logger.debug(f'Showing rule dialog for rule: {rule.id if rule else "new rule"}')
        
        is_edit = rule is not None
        
        with self.rules_dialog:
            self.rules_dialog.clear()
            
            with ui.card().classes('w-full').style('width: 90vw; max-width: 1200px; height: 90vh; margin: auto; display: flex; flex-direction: column'):
                ui.label('Add Rule' if not is_edit else 'Edit Rule').classes('text-h6 mb-4')
                
                # Basic rule information
                with ui.column().classes('w-full gap-4'):
                    self.rule_name_input = ui.input(
                        label='Rule Name',
                        value=rule.name if is_edit else ''
                    ).classes('w-full')
                    
                    # Subtype selection
                    self.rule_subtype_select = ui.select(
                        options={subtype.value: subtype.value.replace('_', ' ').title() for subtype in AnalysisUseCase},
                        label='Subtype (Analysis Use Case)',
                        value=rule.subtype.value if is_edit and rule.subtype else AnalysisUseCase.ENTER_MARKET.value
                    ).classes('w-full')
                    
                    self.continue_processing_checkbox = ui.checkbox(
                        'Continue processing other rules after this one',
                        value=rule.continue_processing if is_edit else False
                    )
                
                # Tabs for triggers and actions
                with ui.tabs() as rule_tabs:
                    ui.tab('Triggers', icon='play_arrow')
                    ui.tab('Actions', icon='settings')
                    ui.tab('Help', icon='help')
                
                with ui.tab_panels(rule_tabs, value='Triggers').classes('w-full').style('flex: 1; overflow-y: auto'):
                    # Triggers tab
                    with ui.tab_panel('Triggers'):
                        ui.label('Configure Triggers').classes('text-subtitle1 mb-4')
                        ui.label('Triggers define when this rule should activate.').classes('text-grey-7 mb-4')
                        
                        self.triggers_container = ui.column().classes('w-full')
                        self.triggers = {}
                        
                        # Load existing triggers if editing
                        if is_edit and rule.triggers:
                            for trigger_key, trigger_config in rule.triggers.items():
                                self._add_trigger_row(trigger_key, trigger_config)
                        
                        ui.button('Add Trigger', on_click=lambda: self._add_trigger_row(), icon='add').classes('mt-4')
                    
                    # Actions tab
                    with ui.tab_panel('Actions'):
                        ui.label('Configure Actions').classes('text-subtitle1 mb-4')
                        ui.label('Actions define what should happen when triggers are met.').classes('text-grey-7 mb-4')
                        
                        self.actions_container = ui.column().classes('w-full')
                        self.actions = {}
                        
                        # Load existing actions if editing
                        if is_edit and rule.actions:
                            for action_key, action_config in rule.actions.items():
                                self._add_action_row(action_key, action_config)
                        
                        ui.button('Add Action', on_click=lambda: self._add_action_row(), icon='add').classes('mt-4')
                    
                    # Help tab
                    with ui.tab_panel('Help'):
                        ui.label('Rules Documentation').classes('text-subtitle1 mb-4')
                        ui.label('Quick reference for available triggers and actions.').classes('text-grey-7 mb-4')
                        
                        with ui.scroll_area().classes('w-full').style('height: 400px'):
                            # Event Types Documentation
                            ui.label('📋 Available Trigger Types').classes('text-h6 mb-3')
                            event_docs = get_event_type_documentation()
                            
                            # Group by type
                            boolean_events = {k: v for k, v in event_docs.items() if v['type'] == 'boolean'}
                            numeric_events = {k: v for k, v in event_docs.items() if v['type'] == 'numeric'}
                            
                            # Boolean events
                            ui.label('🔄 Boolean Events (True/False)').classes('text-subtitle1 mt-4 mb-2')
                            for event_key, doc in list(boolean_events.items())[:6]:  # Show first 6
                                with ui.card().classes('w-full mb-2 p-2 bg-blue-50'):
                                    ui.label(f"{doc['name']} ({event_key})").classes('text-sm font-medium')
                                    ui.label(doc['description']).classes('text-xs text-gray-600 mt-1')
                            
                            # Numeric events
                            ui.label('🔢 Numeric Events (Comparisons)').classes('text-subtitle1 mt-4 mb-2')
                            for event_key, doc in numeric_events.items():
                                with ui.card().classes('w-full mb-2 p-2 bg-green-50'):
                                    ui.label(f"{doc['name']} ({event_key})").classes('text-sm font-medium')
                                    ui.label(doc['description']).classes('text-xs text-gray-600 mt-1')
                            
                            # Action Types Documentation
                            ui.separator().classes('my-4')
                            ui.label('⚡ Available Action Types').classes('text-h6 mb-3')
                            action_docs = get_action_type_documentation()
                            
                            for action_key, doc in action_docs.items():
                                with ui.card().classes('w-full mb-2 p-2 bg-orange-50'):
                                    ui.label(f"{doc['name']} ({action_key})").classes('text-sm font-medium')
                                    ui.label(doc['description']).classes('text-xs text-gray-600 mt-1')
                                    if doc.get('use_cases'):
                                        ui.label(f"• {doc['use_cases'][0]}").classes('text-xs text-gray-500 mt-1 italic')
                
                # Save button
                with ui.row().classes('w-full justify-end mt-4'):
                    ui.button('Cancel', on_click=self.rules_dialog.close).props('flat')
                    ui.button('Save', on_click=lambda: self._save_rule(rule))
        
        self.rules_dialog.open()
    
    def _add_trigger_row(self, trigger_key=None, trigger_config=None):
        """Add a trigger configuration row."""
        if not hasattr(self, 'triggers_container') or self.triggers_container is None:
            logger.error("Triggers container not initialized", exc_info=True)
            return
            
        trigger_id = trigger_key or f"trigger_{len(self.triggers)}"
        
        with self.triggers_container:
            with ui.card().classes('w-full p-4') as trigger_card:
                with ui.row().classes('w-full items-center'):
                    # Trigger type selection
                    trigger_select = ui.select(
                        options=[t.value for t in ExpertEventType],
                        label='Trigger Type',
                        value=trigger_config.get('event_type', trigger_config.get('type', ExpertEventType.F_HAS_POSITION.value)) if trigger_config else ExpertEventType.F_HAS_POSITION.value
                    ).classes('flex-1')
                    
                    # Add help icon with tooltip showing event documentation
                    help_icon = ui.icon('help_outline', size='sm').classes('text-gray-500 cursor-help ml-2')
                    
                    # Remove button
                    ui.button('Remove', on_click=lambda: self._remove_trigger_row(trigger_id, trigger_card), 
                             icon='delete', color='red').props('flat dense')
                
                # Documentation area for selected trigger type
                docs_container = ui.column().classes('w-full mt-2')
                
                def update_trigger_documentation():
                    """Update the documentation for the selected trigger type."""
                    docs_container.clear()
                    selected_type = trigger_select.value
                    if selected_type:
                        event_docs = get_event_type_documentation()
                        if selected_type in event_docs:
                            doc = event_docs[selected_type]
                            with docs_container:
                                with ui.card().classes('w-full bg-blue-50 border-l-4 border-blue-400 p-3'):
                                    ui.label(f"📘 {doc['name']}").classes('text-sm font-semibold text-blue-800')
                                    ui.label(doc['description']).classes('text-sm text-blue-700 mt-1')
                                    if doc.get('example'):
                                        ui.label(f"💡 Example: {doc['example']}").classes('text-xs text-blue-600 mt-1 italic')
                
                # Value input (for N_ types)
                value_row = ui.row().classes('w-full')
                operator_select = None
                value_input = None
                
                def update_value_inputs():
                    value_row.clear()
                    selected_type = trigger_select.value
                    
                    # Update documentation
                    update_trigger_documentation()
                    
                    if selected_type and is_numeric_event(selected_type):
                        # Numeric trigger - show operator and value
                        with value_row:
                            nonlocal operator_select, value_input
                            operator_select = ui.select(
                                options=['<', '>', '=', '!=', '<=', '>='],
                                label='Operator',
                                value=trigger_config.get('operator', '>') if trigger_config else '>'
                            ).classes('w-32')
                            
                            value_input = ui.input(
                                label='Value',
                                value=str(trigger_config.get('value', '')) if trigger_config else ''
                            ).classes('flex-1')
                    else:
                        # Flag trigger - no additional inputs needed
                        with value_row:
                            ui.label('Flag trigger - no additional configuration needed').classes('text-grey-7')
                
                # Initial setup
                update_value_inputs()
                trigger_select.on('update:model-value', lambda: update_value_inputs())
                
                # Store references
                self.triggers[trigger_id] = {
                    'card': trigger_card,
                    'type_select': trigger_select,
                    'operator_select': lambda: operator_select,
                    'value_input': lambda: value_input
                }
    
    def _remove_trigger_row(self, trigger_id, trigger_card):
        """Remove a trigger row."""
        trigger_card.delete()
        if trigger_id in self.triggers:
            del self.triggers[trigger_id]
    
    def _add_action_row(self, action_key=None, action_config=None):
        """Add an action configuration row."""
        if not hasattr(self, 'actions_container') or self.actions_container is None:
            logger.error("Actions container not initialized", exc_info=True)
            return
            
        action_id = action_key or f"action_{len(self.actions)}"
        
        with self.actions_container:
            with ui.card().classes('w-full p-4') as action_card:
                with ui.row().classes('w-full items-center'):
                    # Action type selection
                    action_select = ui.select(
                        options=[a.value for a in ExpertActionType],
                        label='Action Type',
                        value=action_config.get('action_type', action_config.get('type', ExpertActionType.BUY.value)) if action_config else ExpertActionType.BUY.value
                    ).classes('flex-1')
                    
                    # Add help icon with tooltip showing action documentation
                    help_icon = ui.icon('help_outline', size='sm').classes('text-gray-500 cursor-help ml-2')
                    
                    # Remove button
                    ui.button('Remove', on_click=lambda: self._remove_action_row(action_id, action_card), 
                             icon='delete', color='red').props('flat dense')
                
                # Documentation area for selected action type
                action_docs_container = ui.column().classes('w-full mt-2')
                
                def update_action_documentation():
                    """Update the documentation for the selected action type."""
                    action_docs_container.clear()
                    selected_type = action_select.value
                    if selected_type:
                        action_docs = get_action_type_documentation()
                        if selected_type in action_docs:
                            doc = action_docs[selected_type]
                            with action_docs_container:
                                with ui.card().classes('w-full bg-green-50 border-l-4 border-green-400 p-3'):
                                    ui.label(f"⚡ {doc['name']}").classes('text-sm font-semibold text-green-800')
                                    ui.label(doc['description']).classes('text-sm text-green-700 mt-1')
                                    if doc.get('example'):
                                        ui.label(f"💡 Example: {doc['example']}").classes('text-xs text-green-600 mt-1 italic')
                                    # Show use cases if available
                                    if doc.get('use_cases'):
                                        ui.label("Common use cases:").classes('text-xs text-green-700 mt-2 font-medium')
                                        for use_case in doc['use_cases'][:2]:  # Show first 2 use cases
                                            ui.label(f"• {use_case}").classes('text-xs text-green-600 ml-2')
                
                # Value input (for ADJUST_ types and INCREASE/DECREASE_INSTRUMENT_SHARE)
                value_row = ui.row().classes('w-full')
                value_input = None
                reference_select = None
                target_percent_input = None
                
                def update_action_inputs():
                    value_row.clear()
                    selected_type = action_select.value
                    
                    # Update documentation
                    update_action_documentation()
                    
                    if selected_type and is_adjustment_action(selected_type):
                        # Adjustment action - show value input and reference selector
                        with value_row:
                            with ui.column().classes('w-full gap-2'):
                                nonlocal value_input, reference_select
                                
                                # Value input
                                value_input = ui.input(
                                    label='Adjustment Value (can be positive or negative)',
                                    value=str(action_config.get('value', '')) if action_config else '',
                                    placeholder='e.g. 5.0 or -2.5'
                                ).classes('w-full')
                                
                                # Reference value selector
                                from ...core.types import get_reference_value_options
                                reference_select = ui.select(
                                    options=get_reference_value_options(),
                                    label='Reference Value',
                                    value=action_config.get('reference_value', 'current_price') if action_config else 'current_price'
                                ).classes('w-full')
                    elif selected_type and is_share_adjustment_action(selected_type):
                        # Share adjustment action - show target_percent input
                        with value_row:
                            with ui.column().classes('w-full gap-2'):
                                nonlocal target_percent_input
                                
                                # Target percent input
                                target_percent_input = ui.number(
                                    label='Target Percent of Account Equity (%)',
                                    value=action_config.get('target_percent', 10.0) if action_config else 10.0,
                                    min=0.0,
                                    max=100.0,
                                    step=0.1,
                                    format='%.1f',
                                    placeholder='e.g. 10.0 for 10%'
                                ).classes('w-full')
                                
                                # Help text
                                ui.label('Minimum: 1 share | Maximum: Respects max_virtual_equity_per_instrument_percent setting and available balance').classes('text-xs text-grey-6 mt-1')
                    else:
                        # Simple action - no additional inputs needed
                        with value_row:
                            ui.label('Simple action - no additional configuration needed').classes('text-grey-7')
                
                # Initial setup
                update_action_inputs()
                action_select.on('update:model-value', lambda: update_action_inputs())
                
                # Store references
                self.actions[action_id] = {
                    'card': action_card,
                    'type_select': action_select,
                    'value_input': lambda: value_input,
                    'reference_select': lambda: reference_select,
                    'target_percent_input': lambda: target_percent_input
                }
    
    def _remove_action_row(self, action_id, action_card):
        """Remove an action row."""
        action_card.delete()
        if action_id in self.actions:
            del self.actions[action_id]
    
    def _save_rule(self, rule=None):
        """Save the rule (EventAction)."""
        try:
            is_edit = rule is not None
            
            # Collect triggers
            triggers_data = {}
            for trigger_id, trigger_refs in self.triggers.items():
                trigger_type = trigger_refs['type_select'].value
                trigger_config = {'event_type': trigger_type}  # Use 'event_type' instead of 'type'
                
                if is_numeric_event(trigger_type):
                    # Numeric trigger
                    operator_select = trigger_refs['operator_select']()
                    value_input = trigger_refs['value_input']()
                    if operator_select and value_input:
                        trigger_config['operator'] = operator_select.value
                        try:
                            trigger_config['value'] = float(value_input.value)
                        except (ValueError, TypeError):
                            ui.notify(f'Invalid numeric value for trigger {trigger_type}', type='negative')
                            return
                
                triggers_data[trigger_id] = trigger_config
            
            # Collect actions
            actions_data = {}
            for action_id, action_refs in self.actions.items():
                action_type = action_refs['type_select'].value
                action_config = {'action_type': action_type}  # Use 'action_type' instead of 'type'
                
                if is_adjustment_action(action_type):
                    # Adjustment action
                    value_input = action_refs['value_input']()
                    reference_select = action_refs['reference_select']()
                    
                    if value_input and value_input.value:
                        try:
                            action_config['value'] = float(value_input.value)
                        except (ValueError, TypeError):
                            ui.notify(f'Invalid numeric value for action {action_type}', type='negative')
                            return
                    
                    # Save reference value (always save, even if value is empty)
                    if reference_select:
                        action_config['reference_value'] = reference_select.value
                
                elif is_share_adjustment_action(action_type):
                    # Share adjustment action (INCREASE/DECREASE_INSTRUMENT_SHARE)
                    target_percent_input = action_refs['target_percent_input']()
                    
                    if target_percent_input and target_percent_input.value is not None:
                        try:
                            target_percent = float(target_percent_input.value)
                            # Validate range
                            if target_percent < 0 or target_percent > 100:
                                ui.notify(f'Target percent must be between 0 and 100 for action {action_type}', type='negative')
                                return
                            action_config['target_percent'] = target_percent
                        except (ValueError, TypeError):
                            ui.notify(f'Invalid target percent value for action {action_type}', type='negative')
                            return
                    else:
                        ui.notify(f'Target percent is required for action {action_type}', type='negative')
                        return
                
                actions_data[action_id] = action_config
            
            if is_edit:
                # Update existing rule
                rule.name = self.rule_name_input.value
                rule.type = ExpertEventRuleType.TRADING_RECOMMENDATION_RULE
                rule.subtype = AnalysisUseCase(self.rule_subtype_select.value) if self.rule_subtype_select.value else None
                rule.triggers = triggers_data
                rule.actions = actions_data
                rule.continue_processing = self.continue_processing_checkbox.value
                
                update_instance(rule)
                logger.info(f"Updated rule: {rule.id}")
            else:
                # Create new rule
                new_rule = EventAction(
                    name=self.rule_name_input.value,
                    type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                    subtype=AnalysisUseCase(self.rule_subtype_select.value) if self.rule_subtype_select.value else None,
                    triggers=triggers_data,
                    actions=actions_data,
                    extra_parameters={},
                    continue_processing=self.continue_processing_checkbox.value
                )
                
                rule_id = add_instance(new_rule)
                logger.info(f"Created new rule: {rule_id}")
            
            self.rules_dialog.close()
            self._update_rules_table()
            ui.notify('Rule saved successfully!', type='positive')
            
        except Exception as e:
            logger.error(f"Error saving rule: {e}", exc_info=True)
            ui.notify(f"Error saving rule: {e}", type='negative')
    
    def show_ruleset_dialog(self, ruleset=None):
        """Show the add/edit ruleset dialog."""
        logger.debug(f'Showing ruleset dialog for ruleset: {ruleset.id if ruleset else "new ruleset"}')
        
        is_edit = ruleset is not None
        
        with self.rulesets_dialog:
            self.rulesets_dialog.clear()
            
            with ui.card().classes('w-full').style('width: 90vw; max-width: 1200px; height: 90vh; margin: auto; display: flex; flex-direction: column'):
                ui.label('Add Ruleset' if not is_edit else 'Edit Ruleset').classes('text-h6 mb-4')
                
                # Basic ruleset information
                with ui.column().classes('w-full gap-4'):
                    self.ruleset_name_input = ui.input(
                        label='Ruleset Name',
                        value=ruleset.name if is_edit else ''
                    ).classes('w-full')
                    
                    # Subtype selection
                    self.ruleset_subtype_select = ui.select(
                        options={subtype.value: subtype.value.replace('_', ' ').title() for subtype in AnalysisUseCase},
                        label='Subtype (Analysis Use Case)',
                        value=ruleset.subtype.value if is_edit and ruleset.subtype else AnalysisUseCase.ENTER_MARKET.value
                    ).classes('w-full')
                    
                    self.ruleset_description_input = ui.textarea(
                        label='Description',
                        value=ruleset.description if is_edit and ruleset.description else ''
                    ).classes('w-full')
                
                # Rules selection section
                ui.label('Select Rules for this Ruleset').classes('text-subtitle1 mt-4 mb-2')
                ui.label('Choose which rules should be part of this ruleset. Only rules with matching subtype are shown.').classes('text-grey-7 mb-4')
                
                # Container for rules that will be updated when subtype changes
                self.rules_selection_container = ui.column().classes('w-full')
                
                # Function to update available rules based on selected subtype
                def update_available_rules():
                    self.rules_selection_container.clear()
                    
                    # Get selected subtype
                    selected_subtype = self.ruleset_subtype_select.value
                    if not selected_subtype:
                        with self.rules_selection_container:
                            ui.label('Please select a subtype first.').classes('text-orange')
                        return
                    
                    # Get all available rules that match the subtype
                    available_rules = [rule for rule in get_all_instances(EventAction) 
                                     if rule.subtype and rule.subtype.value == selected_subtype]
                    
                    if not available_rules:
                        with self.rules_selection_container:
                            ui.label(f'No rules available for subtype "{selected_subtype.replace("_", " ").title()}". Create some rules with this subtype first.').classes('text-orange')
                        return
                    
                    # Get currently selected rule IDs if editing
                    selected_rule_ids = set()
                    if is_edit:
                        session = get_db()
                        from sqlmodel import select
                        from ...core.models import RulesetEventActionLink
                        stmt = select(RulesetEventActionLink).where(RulesetEventActionLink.ruleset_id == ruleset.id)
                        links = session.exec(stmt).all()
                        selected_rule_ids = {link.eventaction_id for link in links}
                        logger.debug(f'Loaded selected rule IDs for ruleset {ruleset.id}: {selected_rule_ids}')
                        session.close()
                    
                    # Clear and recreate selected_rules dict
                    self.selected_rules = {}
                    
                    with self.rules_selection_container:
                        with ui.column().classes('w-full').style('max-height: 400px; overflow-y: auto') as rules_container:
                            for rule in available_rules:
                                # Check if rule is currently associated with this ruleset
                                is_selected = rule.id in selected_rule_ids
                                
                                with ui.card().classes('w-full p-4 mb-2'):
                                    with ui.row().classes('w-full items-center'):
                                        # Checkbox for selection
                                        rule_checkbox = ui.checkbox(
                                            text='',
                                            value=is_selected
                                        )
                                        
                                        # Rule information
                                        with ui.column().classes('flex-1'):
                                            ui.label(f'{rule.name}').classes('font-medium')
                                            ui.label(f'Subtype: {rule.subtype.value.replace("_", " ").title()}').classes('text-sm text-grey-6')
                                            ui.label(f'Continue Processing: {"Yes" if rule.continue_processing else "No"}').classes('text-sm text-grey-6')
                                            if rule.triggers:
                                                trigger_summary = ', '.join([f"{k}: {v.get('event_type', v.get('type', 'unknown'))}" for k, v in rule.triggers.items()])
                                                ui.label(f'Triggers: {trigger_summary}').classes('text-sm text-grey-6')
                                            if rule.actions:
                                                action_summary = ', '.join([f"{k}: {v.get('action_type', v.get('type', 'unknown'))}" for k, v in rule.actions.items()])
                                                ui.label(f'Actions: {action_summary}').classes('text-sm text-grey-6')
                                
                                self.selected_rules[rule.id] = rule_checkbox
                
                # Initial load of rules
                update_available_rules()
                
                # Update rules when subtype changes
                self.ruleset_subtype_select.on('update:model-value', lambda: update_available_rules())
                
                # Save button
                with ui.row().classes('w-full justify-end mt-4'):
                    ui.button('Cancel', on_click=self.rulesets_dialog.close).props('flat')
                    ui.button('Save', on_click=lambda: self._save_ruleset(ruleset))
        
        self.rulesets_dialog.open()
    
    def _save_ruleset(self, ruleset=None):
        """Save the ruleset."""
        try:
            is_edit = ruleset is not None
            
            # Collect selected rules
            selected_rule_ids = []
            for rule_id, checkbox in self.selected_rules.items():
                if checkbox.value:
                    selected_rule_ids.append(rule_id)
            
            if is_edit:
                # Update existing ruleset
                ruleset.name = self.ruleset_name_input.value
                ruleset.description = self.ruleset_description_input.value or None
                ruleset.subtype = AnalysisUseCase(self.ruleset_subtype_select.value) if self.ruleset_subtype_select.value else None
                
                update_instance(ruleset)
                
                # Update rule associations
                session = get_db()
                # Clear existing associations
                from sqlmodel import delete
                from ...core.models import RulesetEventActionLink
                stmt = delete(RulesetEventActionLink).where(RulesetEventActionLink.ruleset_id == ruleset.id)
                session.exec(stmt)
                
                # Add new associations with proper ordering
                for order_index, rule_id in enumerate(selected_rule_ids):
                    link = RulesetEventActionLink(ruleset_id=ruleset.id, eventaction_id=rule_id, order_index=order_index)
                    session.add(link)
                
                session.commit()
                session.close()
                
                logger.info(f"Updated ruleset: {ruleset.id}")
            else:
                # Create new ruleset
                new_ruleset = Ruleset(
                    name=self.ruleset_name_input.value,
                    description=self.ruleset_description_input.value or None,
                    subtype=AnalysisUseCase(self.ruleset_subtype_select.value) if self.ruleset_subtype_select.value else None
                )
                
                ruleset_id = add_instance(new_ruleset)
                
                # Add rule associations with proper ordering
                if selected_rule_ids:
                    session = get_db()
                    for order_index, rule_id in enumerate(selected_rule_ids):
                        from ...core.models import RulesetEventActionLink
                        link = RulesetEventActionLink(ruleset_id=ruleset_id, eventaction_id=rule_id, order_index=order_index)
                        session.add(link)
                    session.commit()
                    session.close()
                
                logger.info(f"Created new ruleset: {ruleset_id}")
            
            self.rulesets_dialog.close()
            self._update_rulesets_table()
            ui.notify('Ruleset saved successfully!', type='positive')
            
        except Exception as e:
            logger.error(f"Error saving ruleset: {e}", exc_info=True)
            ui.notify(f"Error saving ruleset: {e}", type='negative')
    
    def _on_rule_edit_click(self, msg):
        """Handle edit button click for rules."""
        logger.debug(f'Edit rule table click: {msg}')
        row = msg.args['row']
        rule_id = row['id']
        rule = get_instance(EventAction, rule_id)
        if rule:
            self.show_rule_dialog(rule)
        else:
            logger.warning(f'Rule with id {rule_id} not found')
            ui.notify('Rule not found', type='error')
    
    def _on_rule_del_click(self, msg):
        """Handle delete button click for rules."""
        logger.debug(f'Delete rule table click: {msg}')
        row = msg.args['row']
        rule_id = row['id']
        rule = get_instance(EventAction, rule_id)
        if rule:
            try:
                logger.debug(f'Deleting rule {rule_id}')
                delete_instance(rule)
                logger.info(f'Rule {rule_id} deleted')
                ui.notify('Rule deleted', type='positive')
                self._update_rules_table()
                # Also update rulesets table as rule counts may have changed
                self._update_rulesets_table()
            except Exception as e:
                logger.error(f'Error deleting rule {rule_id}: {e}', exc_info=True)
                ui.notify(f'Error deleting rule: {e}', type='negative')
        else:
            logger.warning(f'Rule with id {rule_id} not found')
            ui.notify('Rule not found', type='error')
    
    def _on_rule_duplicate_click(self, msg):
        """Handle duplicate button click for rules."""
        logger.debug(f'Duplicate rule table click: {msg}')
        row = msg.args['row']
        rule_id = row['id']
        rule = get_instance(EventAction, rule_id)
        if rule:
            try:
                logger.debug(f'Duplicating rule {rule_id}')
                
                # Create a new rule with copied data
                new_rule = EventAction(
                    name=f"{rule.name} (Copy)",
                    type=rule.type,
                    subtype=rule.subtype,
                    continue_processing=rule.continue_processing,
                    triggers=rule.triggers.copy() if rule.triggers else {},
                    actions=rule.actions.copy() if rule.actions else {}
                )
                
                new_rule_id = add_instance(new_rule)
                logger.info(f'Rule {rule_id} duplicated as {new_rule_id}')
                
                # Get fresh instance from database to avoid detached instance error
                new_rule_fresh = get_instance(EventAction, new_rule_id)
                rule_name = new_rule_fresh.name if new_rule_fresh else f"Rule {new_rule_id}"
                
                ui.notify(f'Rule duplicated successfully as "{rule_name}"', type='positive')
                self._update_rules_table()
            except Exception as e:
                logger.error(f'Error duplicating rule {rule_id}: {e}', exc_info=True)
                ui.notify(f'Error duplicating rule: {e}', type='negative')
        else:
            logger.warning(f'Rule with id {rule_id} not found')
            ui.notify('Rule not found', type='error')
    
    def _on_ruleset_test_click(self, msg):
        """Handle test button click for rulesets."""
        logger.debug(f'Test ruleset table click: {msg}')
        row = msg.args['row']
        ruleset_id = row['id']
        ruleset_name = row['name']
        
        # Navigate to ruleset test page with the selected ruleset
        logger.info(f'Navigating to ruleset test page for ruleset: {ruleset_name} (ID: {ruleset_id})')
        ui.navigate.to(f'/rulesettest?ruleset_id={ruleset_id}')
    
    def _on_ruleset_edit_click(self, msg):
        """Handle edit button click for rulesets."""
        logger.debug(f'Edit ruleset table click: {msg}')
        row = msg.args['row']
        ruleset_id = row['id']
        ruleset = get_instance(Ruleset, ruleset_id)
        if ruleset:
            self.show_ruleset_dialog(ruleset)
        else:
            logger.warning(f'Ruleset with id {ruleset_id} not found')
            ui.notify('Ruleset not found', type='error')
    
    def _on_ruleset_del_click(self, msg):
        """Handle delete button click for rulesets."""
        logger.debug(f'Delete ruleset table click: {msg}')
        row = msg.args['row']
        ruleset_id = row['id']
        ruleset = get_instance(Ruleset, ruleset_id)
        if ruleset:
            try:
                logger.debug(f'Deleting ruleset {ruleset_id}')
                # Delete associations first
                session = get_db()
                from sqlmodel import delete
                from ...core.models import RulesetEventActionLink
                stmt = delete(RulesetEventActionLink).where(RulesetEventActionLink.ruleset_id == ruleset_id)
                session.exec(stmt)
                session.commit()
                session.close()
                
                # Delete the ruleset
                delete_instance(ruleset)
                logger.info(f'Ruleset {ruleset_id} deleted')
                ui.notify('Ruleset deleted', type='positive')
                self._update_rulesets_table()
            except Exception as e:
                logger.error(f'Error deleting ruleset {ruleset_id}: {e}', exc_info=True)
                ui.notify(f'Error deleting ruleset: {e}', type='negative')
        else:
            logger.warning(f'Ruleset with id {ruleset_id} not found')
            ui.notify('Ruleset not found', type='error')
    
    def _on_ruleset_duplicate_click(self, msg):
        """Handle duplicate button click for rulesets."""
        logger.debug(f'Duplicate ruleset table click: {msg}')
        row = msg.args['row']
        ruleset_id = row['id']
        
        session = get_db()
        try:
            # Get the original ruleset with relationships loaded
            from sqlmodel import select
            from sqlalchemy.orm import selectinload
            from ...core.models import RulesetEventActionLink
            
            statement = select(Ruleset).where(Ruleset.id == ruleset_id).options(selectinload(Ruleset.event_actions))
            ruleset = session.exec(statement).first()
            
            if ruleset:
                logger.debug(f'Duplicating ruleset {ruleset_id}')
                
                # Create a new ruleset with copied data
                new_ruleset = Ruleset(
                    name=f"{ruleset.name} (Copy)",
                    description=ruleset.description,
                    subtype=ruleset.subtype
                )
                
                new_ruleset_id = add_instance(new_ruleset, session)
                session.commit()
                
                # Copy the rule associations with their order
                if ruleset.event_actions:
                    # Get the link records to preserve order
                    links_statement = select(RulesetEventActionLink).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id
                    ).order_by(RulesetEventActionLink.order_index)
                    links = session.exec(links_statement).all()
                    
                    for link in links:
                        new_link = RulesetEventActionLink(
                            ruleset_id=new_ruleset_id,
                            eventaction_id=link.eventaction_id,
                            order_index=link.order_index
                        )
                        session.add(new_link)
                    
                    session.commit()
                
                logger.info(f'Ruleset {ruleset_id} duplicated as {new_ruleset_id} with {len(ruleset.event_actions) if ruleset.event_actions else 0} rules')
                ui.notify(f'Ruleset duplicated successfully as "{new_ruleset.name}"', type='positive')
                self._update_rulesets_table()
            else:
                logger.warning(f'Ruleset with id {ruleset_id} not found')
                ui.notify('Ruleset not found', type='error')
                
        except Exception as e:
            logger.error(f'Error duplicating ruleset {ruleset_id}: {e}', exc_info=True)
            ui.notify(f'Error duplicating ruleset: {e}', type='negative')
        finally:
            session.close()
    
    def _on_ruleset_reorder_click(self, msg):
        """Handle reorder button click for rulesets."""
        logger.debug(f'Reorder ruleset table click: {msg}')
        row = msg.args['row']
        ruleset_id = row['id']
        self.show_reorder_dialog(ruleset_id)
    
    def show_reorder_dialog(self, ruleset_id):
        """Show the ruleset reorder dialog."""
        logger.debug(f'Showing reorder dialog for ruleset: {ruleset_id}')
        
        # Get the ruleset and its rules with ordering
        ruleset = get_instance(Ruleset, ruleset_id)
        if not ruleset:
            ui.notify('Ruleset not found', type='error')
            return
        
        # Get rules ordered by order_index
        session = get_db()
        from sqlmodel import select
        from sqlalchemy.orm import selectinload
        from ...core.models import RulesetEventActionLink
        
        # Get the link table entries with their order
        links_stmt = select(RulesetEventActionLink).where(
            RulesetEventActionLink.ruleset_id == ruleset_id
        ).order_by(RulesetEventActionLink.order_index)
        links = session.exec(links_stmt).all()
        
        # Get the corresponding EventActions
        rules = []
        for link in links:
            rule = get_instance(EventAction, link.eventaction_id)
            if rule:
                rules.append({
                    'id': rule.id,
                    'name': rule.name,
                    'description': f"{rule.type.value} - {rule.subtype}" if rule.type else rule.subtype,
                    'order_index': link.order_index
                })
        
        session.close()
        
        with self.reorder_dialog:
            self.reorder_dialog.clear()
            
            with ui.card().classes('w-96'):
                ui.label(f'Reorder Rules for: {ruleset.name}').classes('text-h6 mb-4')
                ui.label('Use the up/down arrows to reorder the rules.').classes('text-grey-7 mb-4')
                
                # Create a container for the rule list
                rule_list_container = ui.column().classes('w-full')
                
                def update_rule_list():
                    """Update the rule list display."""
                    rule_list_container.clear()
                    
                    for i, rule in enumerate(rules):
                        with rule_list_container:
                            with ui.row().classes('w-full items-center mb-2'):
                                with ui.column().classes('flex-grow'):
                                    ui.label(rule['name']).classes('font-bold')
                                    ui.label(rule['description']).classes('text-sm text-grey-7')
                                
                                with ui.row().classes('gap-1'):
                                    ui.button('↑', on_click=lambda idx=i: move_rule_up(idx)).props('dense flat').classes('w-8 h-8').set_enabled(i > 0)
                                    ui.button('↓', on_click=lambda idx=i: move_rule_down(idx)).props('dense flat').classes('w-8 h-8').set_enabled(i < len(rules) - 1)
                
                def move_rule_up(index):
                    """Move rule up in the list."""
                    if index > 0:
                        rules[index], rules[index - 1] = rules[index - 1], rules[index]
                        update_rule_list()
                
                def move_rule_down(index):
                    """Move rule down in the list."""
                    if index < len(rules) - 1:
                        rules[index], rules[index + 1] = rules[index + 1], rules[index]
                        update_rule_list()
                
                def save_order():
                    """Save the new rule order to the database."""
                    try:
                        # Create the new order list (just the rule IDs)
                        new_order = [rule['id'] for rule in rules]
                        
                        # Import and use the reordering function
                        from ...core.db import reorder_ruleset_rules
                        success = reorder_ruleset_rules(ruleset_id, new_order)
                        
                        if success:
                            ui.notify('Rule order updated successfully', type='positive')
                            self.reorder_dialog.close()
                            # Refresh the rulesets table (rules count might change display)
                            self._update_rulesets_table()
                        else:
                            ui.notify('Failed to update rule order', type='negative')
                            
                    except Exception as e:
                        logger.error(f'Error saving rule order: {e}', exc_info=True)
                        ui.notify(f'Error saving order: {e}', type='negative')
                
                # Initial population of the list
                update_rule_list()
                
                # Dialog buttons
                with ui.row().classes('justify-end gap-2 mt-4'):
                    ui.button('Cancel', on_click=self.reorder_dialog.close).props('flat')
                    ui.button('Save Order', on_click=save_order, color='primary')
        
        self.reorder_dialog.open()


def content() -> None:
    logger.debug('Initializing settings page')
    
    # Tab configuration: (tab_name, tab_label)
    tab_config = [
        ('global', 'Global Settings'),
        ('account', 'Account Settings'),
        ('expert', 'Expert Settings'),
        ('trade', 'Trade Settings'),
        ('instruments', 'Instruments')
    ]
    
    with ui.tabs() as tabs:
        tab_objects = {}
        for tab_name, tab_label in tab_config:
            tab_objects[tab_name] = ui.tab(tab_name, label=tab_label)
    
    logger.info('Settings page tabs initialized')
            
    with ui.tab_panels(tabs, value=tab_objects['global']).classes('w-full'):
        with ui.tab_panel(tab_objects['global']):
            AppSettingsTab()
        with ui.tab_panel(tab_objects['account']):
            AccountDefinitionsTab()
        with ui.tab_panel(tab_objects['expert']):
            ExpertSettingsTab()
        with ui.tab_panel(tab_objects['trade']):
            TradeSettingsTab()
        with ui.tab_panel(tab_objects['instruments']):
            InstrumentSettingsTab()
    
    # Setup HTML5 history navigation for tabs (NiceGUI 3.0 compatible)
    async def setup_tab_navigation():
        # In NiceGUI 3.0, ui.run_javascript automatically waits for client.connected()
        # So we use await to properly handle the async nature
        from nicegui import context
        await context.client.connected()
        await ui.run_javascript('''
            (function() {
                let isPopstateNavigation = false;
                
                // Map display labels to tab names
                const labelToName = {
                    'Global Settings': 'global',
                    'Account Settings': 'account',
                    'Expert Settings': 'expert',
                    'Trade Settings': 'trade',
                    'Instruments': 'instruments'
                };
                
                // Get tab name from tab element
                function getTabName(tab) {
                    const label = tab.textContent.trim();
                    return labelToName[label] || label.toLowerCase().replace(/\s+/g, '-');
                }
                
                // Handle browser back/forward buttons
                window.addEventListener('popstate', (e) => {
                    isPopstateNavigation = true;
                    const hash = window.location.hash.substring(1) || 'global';
                    
                    // Find and click the correct tab
                    const tabs = document.querySelectorAll('.q-tab');
                    tabs.forEach(tab => {
                        const tabName = getTabName(tab);
                        if (tabName === hash) {
                            tab.click();
                        }
                    });
                    
                    setTimeout(() => { isPopstateNavigation = false; }, 100);
                });
                
                // Setup click handlers for tabs to update URL
                function setupTabClickHandlers() {
                    const tabs = document.querySelectorAll('.q-tab');
                    console.log('Found', tabs.length, 'tabs');
                    tabs.forEach(tab => {
                        const tabName = getTabName(tab);
                        console.log('Setting up listener for tab:', tabName, '(label:', tab.textContent.trim() + ')');
                        tab.addEventListener('click', () => {
                            if (!isPopstateNavigation) {
                                console.log('Tab clicked:', tabName);
                                history.pushState({tab: tabName}, '', '#' + tabName);
                            }
                        });
                    });
                }
                
                // Handle initial page load with hash
                const hash = window.location.hash.substring(1);
                if (hash && hash !== 'global') {
                    // Wait a bit for tabs to be fully rendered
                    setTimeout(() => {
                        const tabs = document.querySelectorAll('.q-tab');
                        tabs.forEach(tab => {
                            const tabName = getTabName(tab);
                            if (tabName === hash) {
                                console.log('Initial load: activating tab for hash:', hash);
                                tab.click();
                            }
                        });
                    }, 50);
                } else if (!hash) {
                    // Set initial hash if none exists
                    history.replaceState({tab: 'global'}, '', '#global');
                }
                
                setupTabClickHandlers();
            })();
        ''', timeout=3.0)
    
    # Use timer to run async setup (shorter delay since we explicitly wait for connection)
    ui.timer(0.1, setup_tab_navigation, once=True)