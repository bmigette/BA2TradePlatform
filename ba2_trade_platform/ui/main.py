from nicegui import ui
from .pages import overview, settings, marketanalysis
from .layout import layout_render
from pathlib import Path



# Example 1: use a custom page decorator directly and putting the content creation into a separate function
@ui.page('/')
def index_page() -> None:
    with layout_render('Overview'):
        overview.content()

@ui.page('/marketanalysis')
def marketanalysis_page() -> None:
    with layout_render('Market Analysis'):
        marketanalysis.content()

@ui.page('/settings')
def settings_page() -> None:
    with layout_render('Settings'):
        settings.content()

STATICPATH = Path(__file__).parent / 'static'
FAVICO = (STATICPATH / 'favicon.ico')
ui.run(title="BA2 Trade Platform", reload=True, favicon=FAVICO)