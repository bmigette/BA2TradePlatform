import logging
from nicegui import ui, Client, app
from .pages import overview, settings, marketanalysis, market_analysis_detail, rulesettest, marketanalysishistory, smart_risk_manager_detail, activity_monitor, live_trades
from .layout import layout_render
from pathlib import Path
from ..logger import logger

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

# Also patch the client's run_javascript method to use higher timeout by default
try:
    from nicegui.client import Client
    original_run_javascript = Client.run_javascript
    
    async def new_run_javascript(self, code, *, timeout=5.0):
        return await original_run_javascript(self, code, timeout=timeout)
    
    Client.run_javascript = new_run_javascript
    logger.info("Successfully patched Client.run_javascript timeout to 5.0 seconds")
    
except Exception as e:
    logger.warning(f"Could not patch Client.run_javascript timeout: {e}")



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
def activity_monitor_page() -> None:
    logger.debug("[ROUTE] /activitymonitor - Loading activity monitor page")
    with layout_render('Activity Monitor'):
        activity_monitor.render()

@ui.page('/livetrades')
def live_trades_page() -> None:
    logger.debug("[ROUTE] /livetrades - Loading live trades page")
    with layout_render('Live Trades'):
        live_trades.content()

STATICPATH = Path(__file__).parent / 'static'
FAVICO = (STATICPATH / 'favicon.ico')

# Get HTTP port from config
from ..config import HTTP_PORT

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
    #uvicorn_logging_level=logging.DEBUG,
)