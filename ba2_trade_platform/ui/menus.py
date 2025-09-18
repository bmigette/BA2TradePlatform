from nicegui import ui

def sidemenu() -> None:
    ui.label('sidemenu')

def topmenu() -> None:
    ui.link('Home', '/').classes(replace='text-white')
    ui.link('A', '/a').classes(replace='text-white')
    ui.link('B', '/b').classes(replace='text-white')
    ui.link('C', '/c').classes(replace='text-white')
