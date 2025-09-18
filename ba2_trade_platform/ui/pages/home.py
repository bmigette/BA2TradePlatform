from nicegui import ui

def content() -> None:
    with ui.tabs() as tabs:
        ui.tab('A')
        ui.tab('B')
        ui.tab('C')
            
    with ui.tab_panels(tabs, value='A').classes('w-full'):
        with ui.tab_panel('A'):
            ui.label('Content of A')
        with ui.tab_panel('B'):
            ui.label('Content of B')
        with ui.tab_panel('C'):
            ui.label('Content of C')