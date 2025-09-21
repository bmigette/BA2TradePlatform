import logging
import pandas as pd
from nicegui import ui
from typing import Optional
from sqlmodel import select


from ...core.models import AccountDefinition, AccountSetting, AppSetting, Instrument, ExpertInstance
from ...logger import logger
from ...core.db import get_db, get_all_instances, delete_instance, add_instance, update_instance, get_instance
from ...modules.accounts import providers
from ...core.AccountInterface import AccountInterface
from ...core.types import InstrumentType
from yahooquery import Ticker, search as yq_search
from nicegui.events import UploadEventArguments
from ...modules.experts import experts
from ..components.InstrumentSelector import InstrumentSelector
from concurrent.futures import ThreadPoolExecutor, as_completed

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
                                    labels_added = False
                                    for label in labels:
                                        if label not in existing_labels:
                                            existing_labels.append(label)
                                            labels_added = True
                                    
                                    if labels_added:
                                        instrument.labels = existing_labels
                                        update_instance(instrument, session)
                                        updated += 1
                                        logger.debug(f'Added labels {labels} to existing instrument {name}')
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
    UI tab for editing and saving static application settings (OpenAI API Key, Finnhub API Key).
    Uses the AppSetting model for persistence. Renders directly in the tab.
    """
    def __init__(self):
        self.openai_input = None
        self.finnhub_input = None
        self.render()

    def render(self):
        session = get_db()
        openai = session.exec(select(AppSetting).where(AppSetting.key == 'openai_api_key')).first()
        finnhub = session.exec(select(AppSetting).where(AppSetting.key == 'finnhub_api_key')).first()
        with ui.card().classes('w-full'):
            self.openai_input = ui.input(label='OpenAI API Key', value=openai.value_str if openai else '').classes('w-full')
            self.finnhub_input = ui.input(label='Finnhub API Key', value=finnhub.value_str if finnhub else '').classes('w-full')
            ui.button('Save', on_click=self.save_settings)

    def save_settings(self):
        try:
            session = get_db()
            # OpenAI
            openai = session.exec(select(AppSetting).where(AppSetting.key == 'openai_api_key')).first()
            if openai:
                openai.value_str = self.openai_input.value
                update_instance(openai, session)
            else:
                openai = AppSetting(key='openai_api_key', value_str=self.openai_input.value)
                add_instance(openai, session)

            # Finnhub
            finnhub = session.exec(select(AppSetting).where(AppSetting.key == 'finnhub_api_key')).first()
            if finnhub:
                finnhub.value_str = self.finnhub_input.value
                update_instance(finnhub, session)
            else:
                finnhub = AppSetting(key='finnhub_api_key', value_str=self.finnhub_input.value)
                add_instance(finnhub, session)
            
            session.commit()
            ui.notify('Settings saved successfully', type='positive')
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
                if meta["type"] == "str":
                    inp = ui.input(label=label, value=value or "").classes('w-full')
                elif meta["type"] == "bool":
                    inp = ui.checkbox(text=label, value=bool(value) if value is not None else False)
                else:
                    inp = ui.input(label=label, value=value or "").classes('w-full')
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
                    {'name': 'user_description', 'label': 'User Notes', 'field': 'user_description'},
                    {'name': 'enabled', 'label': 'Enabled', 'field': 'enabled', 'align': 'center'},
                    {'name': 'virtual_equity', 'label': 'Virtual Equity', 'field': 'virtual_equity', 'align': 'right'},
                    {'name': 'account_id', 'label': 'Account ID', 'field': 'account_id'},
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
                
                # Ensure user_description is displayed properly (truncate if too long for table)
                user_desc = instance.user_description or ''
                if len(user_desc) > 50:
                    row['user_description'] = user_desc[:47] + '...'
                else:
                    row['user_description'] = user_desc
                
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
                    self.user_description_textarea.value = expert_instance.user_description or ''
                    self.enabled_checkbox.value = expert_instance.enabled
                    self.virtual_equity_input.value = str(expert_instance.virtual_equity)
                    
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
                    ui.tab('Instruments', icon='trending_up')
                    ui.tab('Expert Settings', icon='settings')
                
                with ui.tab_panels(settings_tabs, value='Instruments').classes('w-full').style('flex: 1; overflow-y: auto'):
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
                
                # Save button
                with ui.row().classes('w-full justify-end mt-4'):
                    ui.button('Cancel', on_click=self.dialog.close).props('flat')
                    ui.button('Save', on_click=lambda: self._save_expert(expert_instance))
        
        self.dialog.open()
    
    def _load_expert_instrument_config(self, expert_instance):
        """Load instrument configuration for an existing expert."""
        try:
            # Get the expert class and create an instance
            expert_class = self._get_expert_class(expert_instance.expert)
            if expert_class:
                expert = expert_class(expert_instance.id)
                
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
        for expert_class in experts:
            if expert_class.__name__ == expert_type:
                return expert_class
        return None
    
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
            if expert_instance:
                expert = expert_class(expert_instance.id)
                settings_def = expert.get_settings_definitions()
                current_settings = expert.get_all_settings() # TODO FIXME {'enabled_instruments': None}
            else:
                # For new instances, create a temporary expert to get definitions
                settings_def = expert_class.get_settings_definitions()
                current_settings = {}
            
            self.expert_settings_inputs = {}
            
            if settings_def and len(settings_def.keys()) > 0:
                for key, meta in settings_def.items():
                    label = meta.get("description", key)
                    current_setting = current_settings.get(key)
                    
                    if meta["type"] == "str":
                        value = current_setting.value_str if current_setting else meta.get("default", "")
                        inp = ui.input(label=label, value=value).classes('w-full')
                    elif meta["type"] == "bool":
                        value = bool(current_setting.value_str) if current_setting else meta.get("default", False)
                        inp = ui.checkbox(text=label, value=value)
                    elif meta["type"] == "float":
                        value = current_setting.value_float if current_setting else meta.get("default", 0.0)
                        inp = ui.input(label=label, value=str(value)).classes('w-full')
                    else:
                        value = current_setting.value_str if current_setting else meta.get("default", "")
                        inp = ui.input(label=label, value=value).classes('w-full')
                    
                    inp.move(self.expert_settings_container)
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
                expert_instance.user_description = self.user_description_textarea.value or None
                expert_instance.enabled = self.enabled_checkbox.value
                expert_instance.virtual_equity = float(self.virtual_equity_input.value)
                expert_instance.account_id = account_id
                
                update_instance(expert_instance)
                logger.info(f"Updated expert instance: {expert_instance.id}")
                
                expert_id = expert_instance.id
            else:
                # Create new instance
                new_instance = ExpertInstance(
                    expert=self.expert_select.value,
                    user_description=self.user_description_textarea.value or None,
                    enabled=self.enabled_checkbox.value,
                    virtual_equity=float(self.virtual_equity_input.value),
                    account_id=account_id
                )
                
                expert_id = add_instance(new_instance)
                logger.info(f"Created new expert instance: {expert_id}")
            
            # Save expert-specific settings
            self._save_expert_settings(expert_id)
            
            # Save instrument configuration
            self._save_instrument_configuration(expert_id)
            
            self.dialog.close()
            self._update_table_rows()
            ui.notify('Expert saved successfully!', type='positive')
            
        except Exception as e:
            logger.error(f"Error saving expert: {e}", exc_info=True)
            ui.notify(f"Error saving expert: {e}", type='negative')
    
    def _save_expert_settings(self, expert_id):
        """Save expert-specific settings."""
        if not hasattr(self, 'expert_settings_inputs') or not self.expert_settings_inputs:
            return
        
        expert_class = self._get_expert_class(self.expert_select.value)
        if not expert_class:
            return
        
        expert = expert_class(expert_id)
        settings_def = expert.get_settings_definitions()
        
        for key, inp in self.expert_settings_inputs.items():
            meta = settings_def.get(key, {})
            
            if meta.get("type") == "bool":
                expert.save_setting(key, inp.value, setting_type="bool")
            elif meta.get("type") == "float":
                expert.save_setting(key, float(inp.value or 0), setting_type="float")
            else:
                expert.save_setting(key, inp.value, setting_type="str")
        
        logger.debug(f'Saved expert settings for instance {expert_id}')
    
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


def content() -> None:
    logger.debug('Initializing settings page')
    with ui.tabs() as tabs:
        ui.tab('Global Settings')
        ui.tab('Account Settings')
        ui.tab('Expert Settings')
        ui.tab('Trade Settings')
        ui.tab('Instruments')
    logger.info('Settings page tabs initialized')
            
    with ui.tab_panels(tabs, value='Global Settings').classes('w-full'):
        with ui.tab_panel('Global Settings'):
            AppSettingsTab()
        with ui.tab_panel('Account Settings'):
            AccountDefinitionsTab()
        with ui.tab_panel('Expert Settings'):
            ExpertSettingsTab()
        with ui.tab_panel('Trade Settings'):
            ui.label('Trade Settings')
        with ui.tab_panel('Instruments'):
            InstrumentSettingsTab()