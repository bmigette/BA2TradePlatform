from nicegui import ui
from typing import Optional
from sqlmodel import select

from ...core.models import AccountDefinition, AccountSetting
from ...logger import logger
from ...core.db import get_db

class AccountDefinitionsTab:
    def __init__(self):
        logger.debug('Initializing AccountDefinitionsTab')
        self.dialog = ui.dialog()
        self.accounts_table = None
        self.account_settings()

    def save_account(self, account: Optional[AccountDefinition] = None) -> None:
        try:
            if account:
                account.provider = self.type_select.value
                account.name = self.name_input.value
                account.description = self.desc_input.value
                account.save()
                logger.info(f"Updated account: {account.name}")
            else:
                new_account = AccountDefinition(
                    provider=self.type_select.value,
                    name=self.name_input.value,
                    description=self.desc_input.value
                )
                session = get_db()
                try:
                    session.add(new_account)
                    session.commit()
                finally:
                    session.close()
                logger.info(f"Created new account: {self.name_input.value}")
            self.dialog.close()
            
        except Exception as e:
            logger.error(f"Error saving account: {str(e)}", exc_info=True)
            ui.notify("Error saving account", type="error")

    def delete_account(self, account: AccountDefinition) -> None:
        try:
            account.delete()
            logger.info(f"Deleted account: {account.name}")
            self.accounts_table.refresh()
        except Exception as e:
            logger.error(f"Error deleting account: {str(e)}")
            ui.notify("Error deleting account", type="error")

    def show_dialog(self, account: Optional[AccountDefinition] = None) -> None:
        logger.debug(f'Showing account dialog for account: {account.name if account else "new account"}')
        with self.dialog:
            self.dialog.clear()
               
            with ui.card():
                self.type_select = ui.select(['alpaca'], label='Account Provider').classes('w-full')
                self.name_input = ui.input(label='Account Name')
                self.desc_input = ui.input(label='Description')
                self.type_select.value = account.provider if account else 'alpaca'
                self.name_input.value = account.name if account else ''
                self.desc_input.value = account.description if account else ''
                ui.button('Save', on_click=lambda: self.save_account(account))
        self.dialog.open()

    def account_settings(self) -> None:
        logger.debug('Loading account settings')
        session = get_db()
        try:
            statement = select(AccountDefinition)
            accounts = list(session.exec(statement))
            logger.info(f'Loaded {len(accounts)} accounts')
        finally:
            session.close()

        with ui.card().classes('w-full'):
            ui.button('Add Account', on_click=lambda: self.show_dialog())
            self.accounts_table = ui.table(
                columns=[
                    {'name': 'type', 'label': 'Type', 'field': 'provider'},
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
            
            self.accounts_table.on('edit', lambda msg: ui.notify("edit: "+ str(msg)))
            self.accounts_table.on('del', lambda msg: ui.notify("del: "+ str(msg)))
            # for row in accounts:
            #     with self.accounts_table.add_slot('body-cell-actions', row):
            #         ui.button(icon='edit', on_click=lambda r=row: self.show_dialog(r))
            #         ui.button(icon='delete', on_click=lambda r=row: self.delete_account(r))

def content() -> None:
    logger.debug('Initializing settings page')
    with ui.tabs() as tabs:
        ui.tab('Global Settings')
        ui.tab('Account Settings')
        ui.tab('Expert Settings')
        ui.tab('Trade Settings')
    logger.info('Settings page tabs initialized')
            
    with ui.tab_panels(tabs, value='Global Settings').classes('w-full'):
        with ui.tab_panel('Global Settings'):
            ui.label('Global Settings')
        with ui.tab_panel('Account Settings'):
            AccountDefinitionsTab()
        with ui.tab_panel('Expert Settings'):
            ui.label('Expert Settings')
        with ui.tab_panel('Trade Settings'):
            ui.label('Trade Settings')