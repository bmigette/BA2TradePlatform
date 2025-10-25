from nicegui import ui
from . import svg 

def sidemenu() -> None:
    with ui.list().props(' separator ').classes('text-white text-bold w-full'):
        ui.separator()
        with ui.item(on_click=lambda: ui.navigate.to('/')):
            with ui.item_section():
                ui.icon('dashboard')
            with ui.item_section():
                ui.item_label('Overview')
        with ui.item(on_click=lambda: ui.navigate.to('/marketanalysis')):
            with ui.item_section():
                ui.icon('analytics')
            with ui.item_section():
                ui.item_label('Market Analysis')
        with ui.item(on_click=lambda: ui.navigate.to('/serverperf')):
            with ui.item_section():
                ui.icon('computer')
            with ui.item_section():
                ui.item_label('Server Performance')
        with ui.item(on_click=lambda: ui.navigate.to('/settings')).classes('w-full'):
            with ui.item_section():
                ui.icon('settings')
            with ui.item_section():
                ui.item_label('Settings')



def topmenu() -> None:
    with ui.link(target='https://github.com/bmigette/BA2TradePlatform').classes('max-[365px]:hidden').tooltip('GitHub'):
        svg.github().classes('fill-white scale-125 m-1')
