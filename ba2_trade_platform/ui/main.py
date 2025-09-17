from nicegui import ui

with ui.header().classes(replace='row items-center duration-200 p-0 px-4 no-wrap bg-[#36454F]') as header:
    ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat color=white')
    with ui.tabs() as tabs:
        ui.tab('A')
        ui.tab('B')
        ui.tab('C')

with ui.footer(value=False) as footer:
    ui.label('Footer')

with ui.left_drawer().classes('bg-[#36454F] text-white') as left_drawer:
    with ui.list().props('dense separator'):
        ui.item('Home').props('link')
        ui.item('Profile').props('link')
        ui.item('Settings').props('link')
        ui.item('Help').props('link')

with ui.page_sticky(position='bottom-right', x_offset=20, y_offset=20):
    ui.button(on_click=footer.toggle, icon='contact_support').props('fab')

with ui.tab_panels(tabs, value='A').classes('w-full'):
    with ui.tab_panel('A'):
        ui.label('Content of A')
    with ui.tab_panel('B'):
        ui.label('Content of B')
    with ui.tab_panel('C'):
        ui.label('Content of C')

ui.run()