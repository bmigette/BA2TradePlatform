from nicegui import ui
from . import svg 

def sidemenu() -> None:
    with ui.list().props(' separator ').classes('text-white text-bold w-full'):
        ui.separator()
        with ui.item(on_click=lambda: ui.navigate.to('/')):
            with ui.item_section():
                ui.icon('home')
            with ui.item_section():
                ui.item_label('Home')
        with ui.item(on_click=lambda: ui.navigate.to('/settings')).classes('w-full'):
            with ui.item_section():
                ui.icon('settings')
            with ui.item_section():
                ui.item_label('Settings')



def topmenu() -> None:
    with ui.link(target='https://github.com/bmigette/BA2TradePlatform').classes('max-[365px]:hidden').tooltip('GitHub'):
        svg.github().classes('fill-white scale-125 m-1')
