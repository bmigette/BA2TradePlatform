from nicegui import ui
from .pages import overview, settings, marketanalysis, market_analysis_detail, rulesettest
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

@ui.page('/market_analysis/{analysis_id}')
def market_analysis_detail_page(analysis_id: int) -> None:
    with layout_render(f'Market Analysis Detail'):
        market_analysis_detail.content(analysis_id)

@ui.page('/rulesettest')
def rulesettest_page() -> None:
    with layout_render('Ruleset Test'):
        rulesettest.content()

STATICPATH = Path(__file__).parent / 'static'
FAVICO = (STATICPATH / 'favicon.ico')
ui.run(title="BA2 Trade Platform", reload=False, favicon=FAVICO)