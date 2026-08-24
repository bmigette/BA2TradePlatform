from nicegui import ui, Client, app
from .pages import overview, settings, marketanalysis, market_analysis_detail, rulesettest, marketanalysishistory, smart_risk_manager_detail, activity_monitor, live_trades, tools, portfolio_allocation
from .layout import layout_render
from . import api_routes
from pathlib import Path
from ..logger import logger

# Plain HTTP API routes (non-UI): DB reload callback etc. `app` here is the FastAPI instance
# NiceGUI wraps, same object `app.on_shutdown` below hooks into.
app.include_router(api_routes.router)

# Configure NiceGUI JavaScript timeout globally
# This affects all JavaScript requests throughout the application
try:
    # Try to set javascript timeout on Client class
    if hasattr(Client, 'javascript_timeout'):
        Client.javascript_timeout = 5.0
        logger.info("Set Client.javascript_timeout to 5.0 seconds")
    
except Exception as e:
    logger.warning(f"Could not set JavaScript timeout: {e}")

# Patch the JavaScriptRequest class to use a longer default timeout
try:
    from nicegui.javascript_request import JavaScriptRequest
    
    # Store the original __init__ method
    original_init = JavaScriptRequest.__init__
    
    # Create a new __init__ that defaults to 5 second timeout
    def new_init(self, request_id, *, timeout=5.0):
        return original_init(self, request_id, timeout=timeout)
    
    # Replace the __init__ method
    JavaScriptRequest.__init__ = new_init
    logger.info("Successfully patched JavaScriptRequest timeout to 5.0 seconds")
    
except Exception as e:
    logger.warning(f"Could not patch JavaScript request timeout: {e}")

# NOT patched: Client.run_javascript. An `async def` wrapper used to be installed here
# to force the same 5s timeout, and it broke every FIRE-AND-FORGET call in the app --
# the header account switch (ui/layout.py) and TradingAgentsUI's post-save reload both
# stopped reloading the browser, so the account filter changed in the widget while every
# page body kept showing the previous account (2026-08 report).
#
# ui.run_javascript() returns an AwaitableResponse whose __init__ already schedules the
# send (nicegui/awaitable_response.py:21 -> _fire -> fire_and_forget), which is why a
# call that is deliberately not awaited still reaches the browser. Wrapping the method in
# a coroutine replaces that object with a bare coroutine; nobody awaits it, so the
# AwaitableResponse is never even constructed and the JavaScript is never enqueued.
#
# The wrapper bought nothing in exchange: ui.run_javascript passes `timeout=` explicitly
# on every call (nicegui/functions/javascript.py:24), so the wrapper's own default never
# applied. Call sites that need a longer timeout must pass it themselves:
#   await ui.run_javascript(code, timeout=5.0)
# See tests/test_ui_account_switch_reload.py, which fails if this comes back.



# Example 1: use a custom page decorator directly and putting the content creation into a separate function
@ui.page('/')
def index_page() -> None:
    logger.debug("[ROUTE] / - Loading overview page")
    with layout_render('Overview'):
        overview.content()

@ui.page('/marketanalysis')
def marketanalysis_page() -> None:
    logger.debug("[ROUTE] /marketanalysis - Loading market analysis page")
    with layout_render('Market Analysis'):
        marketanalysis.content()

@ui.page('/settings')
def settings_page() -> None:
    logger.debug("[ROUTE] /settings - Loading settings page")
    with layout_render('Settings'):
        settings.content()

@ui.page('/market_analysis/{analysis_id}')
def market_analysis_detail_page(analysis_id: int) -> None:
    logger.debug(f"[ROUTE] /market_analysis/{analysis_id} - Loading market analysis detail page")
    with layout_render(f'Market Analysis Detail'):
        market_analysis_detail.content(analysis_id)

@ui.page('/rulesettest')
def rulesettest_page() -> None:
    logger.debug("[ROUTE] /rulesettest - Loading ruleset test page")
    # Get query parameters from the request
    from nicegui import app
    ruleset_id = None
    try:
        if hasattr(app, 'storage') and hasattr(app.storage, 'user'):
            # Try to get from query params - this depends on how NiceGUI exposes them
            pass
        # For now, we'll let the component handle URL extraction
    except:
        pass
    
    with layout_render('Ruleset Test'):
        rulesettest.content(ruleset_id)

@ui.page('/marketanalysishistory/{symbol}')
def market_analysis_history_page(symbol: str) -> None:
    logger.debug(f"[ROUTE] /marketanalysishistory/{symbol} - Loading market analysis history page")
    with layout_render(f'Market Analysis History - {symbol}'):
        marketanalysishistory.render_market_analysis_history(symbol)

@ui.page('/smartriskmanagerdetail/{job_id}')
def smart_risk_manager_detail_page(job_id: int) -> None:
    logger.debug(f"[ROUTE] /smartriskmanagerdetail/{job_id} - Loading Smart Risk Manager detail page")
    with layout_render(f'Smart Risk Manager Job #{job_id}'):
        smart_risk_manager_detail.content(job_id)

@ui.page('/activitymonitor')
async def activity_monitor_page() -> None:
    logger.debug("[ROUTE] /activitymonitor - Loading activity monitor page")
    with layout_render('Activity Monitor'):
        await activity_monitor.render()

@ui.page('/livetrades')
async def live_trades_page() -> None:
    logger.debug("[ROUTE] /livetrades - Loading live trades page")
    with layout_render('Live Trades'):
        await live_trades.content()

@ui.page('/tools')
def tools_page() -> None:
    logger.debug("[ROUTE] /tools - Loading tools page")
    with layout_render('Tools'):
        tools.content()

@ui.page('/portfolioallocation')
async def portfolio_allocation_page() -> None:
    logger.debug("[ROUTE] /portfolioallocation - Loading portfolio allocation page")
    with layout_render('Portfolio Allocation'):
        await portfolio_allocation.content()

STATICPATH = Path(__file__).parent / 'static'
FAVICO = (STATICPATH / 'favicon.ico')

# Get HTTP port from config
from ..config import HTTP_PORT, STORAGE_SECRET

# Register shutdown handler to log application stop
def on_shutdown():
    """Log application shutdown activity."""
    try:
        # Shutdown Instrument Auto Adder service
        from ..core.InstrumentAutoAdder import shutdown_instrument_auto_adder
        shutdown_instrument_auto_adder()
        logger.info("InstrumentAutoAdder service shutdown completed")
        
        from ..core.db import log_activity
        from ..core.types import ActivityLogSeverity, ActivityLogType
        
        log_activity(
            severity=ActivityLogSeverity.INFO,
            activity_type=ActivityLogType.APPLICATION_STATUS_CHANGE,
            description="BA2 Trade Platform stopped",
            data={
                "status": "stopped"
            },
            source_expert_id=None,
            source_account_id=None
        )
        logger.info("Application shutdown logged to activity monitor")
    except Exception as e:
        logger.warning(f"Failed to log application shutdown activity: {e}")

app.on_shutdown(on_shutdown)

# Configure NiceGUI with increased timeouts
ui.run(
    title="BA2 Trade Platform", 
    reload=False, 
    favicon=FAVICO,
    port=HTTP_PORT,
    # Increase reconnect timeout (this is the supported parameter in NiceGUI 2.24.1)
    reconnect_timeout=5.0,
    # Increase binding refresh interval to reduce pressure
    binding_refresh_interval=0.5,
    # Storage secret for app.storage.user (account filter persistence)
    storage_secret=STORAGE_SECRET,
    # Enable dark mode by default
    dark=True,
    #uvicorn_logging_level=logging.DEBUG,
)