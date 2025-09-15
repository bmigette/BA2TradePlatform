from nicegui import ui

APP_NAME = "BA2 Trade Platform"

DARK_BG = "#23272f"
DARK_SIDEBAR = "#1a1d23"
DARK_TOPBAR = "#23272f"
DARK_TEXT = "#f4f4f4"
ACCENT = "#3a3f4b"

ui.colors(primary=ACCENT, background=DARK_BG, text=DARK_TEXT)

with ui.header().style(f'background: {DARK_TOPBAR}; color: {DARK_TEXT};'):
    ui.label(APP_NAME).style('font-size: 1.5rem; font-weight: bold; margin-left: 1rem;')

with ui.left_drawer().style(f'background: {DARK_SIDEBAR}; color: {DARK_TEXT}; width: 220px;'):
    ui.label("Menu").style('font-size: 1.1rem; font-weight: bold; margin: 1rem 0 1rem 1rem;')
    with ui.column().classes('gap-2'):
        ui.button('Home', on_click=lambda: ui.open('/'), color=ACCENT, text_color=DARK_TEXT).classes('w-full')
        ui.button('Settings', on_click=lambda: ui.open('/settings'), color=ACCENT, text_color=DARK_TEXT).classes('w-full')

@ui.page('/')
def home():
    with ui.card().style(f'background: {DARK_BG}; color: {DARK_TEXT}; margin: 2rem;'):
        ui.label('Welcome to BA2 Trade Platform!').style('font-size: 1.2rem; font-weight: bold;')
        ui.label('This is the Home page.')

@ui.page('/settings')
def settings():
    with ui.card().style(f'background: {DARK_BG}; color: {DARK_TEXT}; margin: 2rem;'):
        ui.label('Settings').style('font-size: 1.2rem; font-weight: bold;')
        ui.label('Configure your preferences here.')

ui.run(title=APP_NAME, dark=True)