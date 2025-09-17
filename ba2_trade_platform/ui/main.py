from nicegui import ui
from .pages import home
from .layout import layout_render
    


# Example 1: use a custom page decorator directly and putting the content creation into a separate function
@ui.page('/')
def index_page() -> None:
    with layout_render('Homepage'):
        home.content()

ui.run()