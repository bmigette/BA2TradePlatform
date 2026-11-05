"""Settings-page card for the management API: token generation and the remote-access switch.

The token is written the moment you press Generate (it is NOT part of 'Save All Settings') and shown ONCE in
a dialog; afterwards only its last four characters are displayed. Regenerating invalidates every client that
holds the old token (a confirm dialog says so). The 'Allow API access from other machines' switch
(AppSetting 'api_allow_remote', default OFF) is likewise saved immediately and read per request."""
from nicegui import ui

from ...api.auth import get_allow_remote, get_api_token, rotate_api_token, set_allow_remote


def token_status_text() -> str:
    token = get_api_token()
    if token is None:
        return "No API token set: API writes and non-local API reads are refused (403)."
    return f"API token is set (ends in ...{token[-4:]})."


def generate_token_for_ui() -> str:
    return rotate_api_token()


def set_allow_remote_for_ui(allow: bool) -> None:
    set_allow_remote(allow)


def needs_remote_confirmation(currently_allowed: bool, wanted: bool) -> bool:
    """Only the off -> on flip asks for confirmation (switching access off never needs it)."""
    return wanted and not currently_allowed


def render_api_token_card() -> None:
    with ui.card().classes('w-full mb-4'):
        ui.label('Management API').classes('text-xl font-bold mb-2')
        ui.label('HTTP API under /api/v1 (docs at /api/v1/docs). Writes always need '
                 '"Authorization: Bearer <token>"; reads need no token from 127.0.0.1.'
                 ).classes('text-sm text-gray-500 mb-2')
        status = ui.label(token_status_text()).classes('text-sm')

        def _show_new_token():
            token = generate_token_for_ui()
            status.set_text(token_status_text())
            with ui.dialog() as dialog, ui.card():
                ui.label('New API token (shown once -- copy it now)').classes('font-bold')
                ui.input(value=token).props('readonly outlined').classes('w-96')
                ui.button('Copy', icon='content_copy',
                          on_click=lambda: ui.run_javascript(f'navigator.clipboard.writeText({token!r})'))
                ui.button('Close', on_click=dialog.close)
            dialog.open()

        def _on_click():
            if get_api_token() is None:
                _show_new_token()
                return
            with ui.dialog() as confirm, ui.card():
                ui.label('Generate a new token? The current token stops working immediately.')
                with ui.row():
                    ui.button('Cancel', on_click=confirm.close)
                    ui.button('Generate', color='negative',
                              on_click=lambda: (confirm.close(), _show_new_token()))
            confirm.open()

        ui.button('Generate token', icon='key', on_click=_on_click)
        remote_switch = ui.switch('Allow API access from other machines', value=get_allow_remote())

        def _on_remote_change(e):
            if not needs_remote_confirmation(get_allow_remote(), bool(e.value)):
                set_allow_remote_for_ui(bool(e.value))      # on -> off (or unchanged): no confirmation
                return
            with ui.dialog() as confirm, ui.card():
                ui.label('Expose the management API to other machines? Anyone on the network who holds the '
                         'API token can then read and change this instance.')

                def _cancel():
                    confirm.close()
                    remote_switch.set_value(False)

                with ui.row():
                    ui.button('Cancel', on_click=_cancel)
                    ui.button('Allow', color='negative',
                              on_click=lambda: (set_allow_remote_for_ui(True), confirm.close()))
            confirm.open()

        remote_switch.on_value_change(_on_remote_change)
        ui.label('OFF (default): only this machine (127.0.0.1) can reach the API. ON exposes the API to the '
                 'LAN; callers still need the token. Saved immediately, no restart needed.'
                 ).classes('text-xs text-gray-500')
