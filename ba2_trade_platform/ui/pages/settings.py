import logging
import pandas as pd
from nicegui import ui
from typing import Optional
from sqlmodel import select


from ...core.models import AccountDefinition, AccountSetting, AppSetting, Instrument
from ...logger import logger
from ...core.db import get_db, get_all_instances, delete_instance, add_instance, update_instance, get_instance
from ...modules.accounts import providers
from ...core.AccountInterface import AccountInterface
from ...core.types import InstrumentType
from yahooquery import Ticker, search as yq_search
from nicegui.events import UploadEventArguments
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

    async def fetch_info(self): # TODO: This blocks ui execution, need to check how to do async

        logger.debug('Fetching info for all instruments (parallel)')
        session = get_db()
        statement = select(Instrument)
        results = session.exec(statement)
        instruments = results.all()
        session.close()  # We'll use a new session per thread

        updated = 0
        errors = 0

        def fetch_and_update(instrument_id, instrument_name):
            local_session = get_db()
            instrument = local_session.get(Instrument, instrument_id)
            try:
                logger.debug(f'Fetching info for instrument: {instrument_name}')
                ticker = Ticker(instrument_name)
                try:
                    profile = ticker.asset_profile # {'HUM': 'Invalid Crumb'}
                    if profile and instrument_name.upper() in profile:
                        profile_data = profile[instrument_name.upper()]
                        sector = profile_data.get("sector")
                        if sector:
                            if self._add_to_list_field(instrument, 'categories', sector, local_session):
                                logger.debug(f'Added sector {sector} to instrument {instrument_name}')
                                local_session.commit()
                                updated_flag = True
                            else:
                                updated_flag = False
                        else:
                            logger.debug(f'No sector found for instrument {instrument_name}')
                            updated_flag = False
                    else:
                        self._add_to_list_field(instrument, 'labels', 'not_found', local_session)
                        logger.warning(f'Instrument {instrument_name} not found in asset profile')
                        local_session.commit()
                        updated_flag = False

                    # Update company_name using ticker.price[name.upper()]["longname"]
                    try:
                        price_info = ticker.price
                        longname = None
                        if price_info and instrument_name.upper() in price_info:
                            longname = price_info[instrument_name.upper()].get("longName")
                        if longname:
                            instrument.company_name = longname
                            local_session.add(instrument)
                            local_session.commit()
                            logger.debug(f'Updated company_name for {instrument_name}: {longname}')
                            updated_flag = True
                    except Exception as name_error:
                        logger.warning(f'Could not update company_name for {instrument_name}: {name_error}')
                    if updated_flag:
                        if 'not_found' in instrument.labels:
                            labels = list(instrument.labels)
                            labels.remove('not_found')
                            instrument.labels = labels
                            local_session.add(instrument)
                            local_session.commit()
                    return updated_flag
                except Exception as profile_error:
                    self._add_to_list_field(instrument, 'labels', 'not_found', local_session)
                    logger.warning(f'Could not get asset profile for {instrument_name}: {profile_error}', exc_info=True)
                    local_session.commit()
                    return False
            except Exception as e:
                self._add_to_list_field(instrument, 'labels', 'not_found', local_session)
                logger.error(f"Error fetching info for {instrument_name}: {e}", exc_info=True)
                local_session.commit()
            finally:
                local_session.close()
            return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(fetch_and_update, instrument.id, instrument.name)
                for instrument in instruments
            ]
            for future in as_completed(futures):
                try:
                    if future.result():
                        updated += 1
                except Exception as e:
                    errors += 1
                    logger.error(f"Error in fetch_info thread: {e}")

        logger.info(f'Fetched info for {updated} instruments. Errors: {errors}')
        ui.notify(f'Fetched info for {updated} instruments. Errors: {errors}', type='positive' if errors == 0 else 'warning')
        self._update_table_rows()

    def import_instruments(self):
        logger.debug('Importing instruments from a text file')

        def handle_upload(e: UploadEventArguments):
            try:
                # Read content from the uploaded file
                e.content.seek(0)  # Ensure we're at the beginning of the file
                content = e.content.read().decode('utf-8')
                names = [line.strip() for line in content.splitlines() if line.strip()]
                session = get_db()
                added = 0
                existing_names = {inst.name for inst in session.exec(select(Instrument)).all()}
                for name in names:
                    if name not in existing_names:
                        inst = Instrument(name=name, instrument_type='stock', categories=[], labels=[])
                        add_instance(inst, session)
                        added += 1
                        existing_names.add(name)
                session.commit()
                logger.info(f'Imported {added} new instruments from file')
                ui.notify(f'Imported {added} new instruments.', type='positive')
                self._update_table_rows()
            except Exception as e:
                logger.error(f'Import failed: {e}')
                ui.notify(f'Import failed: {e}', type='negative')

        ui.upload(label='Upload instrument list (.txt)', on_upload=handle_upload, max_files=1, auto_upload=True)

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
                logger.error(f'Error deleting instrument {instrument_id}: {e}')
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
            logger.error(f"Error deleting account: {str(e)}")
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
            ui.label('Expert Settings')
        with ui.tab_panel('Trade Settings'):
            ui.label('Trade Settings')
        with ui.tab_panel('Instruments'):
            InstrumentSettingsTab()