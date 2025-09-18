from contextlib import contextmanager

from .menus import topmenu, sidemenu

from nicegui import ui


@contextmanager
def layout_render(navigation_title: str):
    """Custom page frame to share the same styling and behavior across all pages"""
    ui.colors(primary='#36454F', secondary='#53B689', accent='#111B1E', positive='#53B689')
    with ui.footer(value=False) as footer:
        ui.label('Footer')

    with ui.left_drawer().classes('bg-[#36454F] text-white') as left_drawer:
        sidemenu()

    with ui.page_sticky(position='bottom-right', x_offset=20, y_offset=20):
        ui.button(on_click=footer.toggle, icon='contact_support').props('fab')

    with ui.header().classes('text-xl no-wrap'):
        ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat color=white')
        ui.space()
        ui.label(navigation_title)
        ui.space()
        with ui.row():
            topmenu()
    with ui.column().classes('items-center'):
        yield
