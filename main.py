import os
import warnings
import argparse

def parse_arguments():
    """Parse command-line arguments for configuration overrides."""
    # Import config here to get default values, but avoid triggering db initialization
    import ba2_trade_platform.config as config
    
    parser = argparse.ArgumentParser(
        description='BA2 Trade Platform - Algorithmic Trading Platform',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    parser.add_argument(
        '--db-file',
        type=str,
        default=config.DB_FILE,
        help='Path to the SQLite database file'
    )
    
    parser.add_argument(
        '--cache-folder',
        type=str,
        default=config.CACHE_FOLDER,
        help='Path to the cache folder for temporary data'
    )
    
    parser.add_argument(
        '--log-folder',
        type=str,
        default=config.LOG_FOLDER,
        help='Path to the log folder'
    )
    
    parser.add_argument(
        '--port',
        type=int,
        default=config.HTTP_PORT,
        help='HTTP port for the web interface'
    )
    
    return parser.parse_args()

def initialize_system():
    """Initialize the system components."""
    # Import these after config has been updated from command-line args
    from ba2_trade_platform.core.db import init_db, get_db
    from ba2_trade_platform.core.WorkerQueue import initialize_worker_queue
    from ba2_trade_platform.core.SmartRiskManagerQueue import initialize_smart_risk_manager_queue
    from ba2_trade_platform.core.JobManager import get_job_manager
    from ba2_trade_platform.logger import logger
    import ba2_trade_platform.config as config
    
    logger.info("Initializing BA2 Trade Platform...")
    
    # Create log folder if not exists
    os.makedirs(config.LOG_FOLDER, exist_ok=True)
    
    # Create cache folder if not exists
    os.makedirs(config.CACHE_FOLDER, exist_ok=True)
    
    # Create database folder if not exists
    db_dir = os.path.dirname(config.DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    
    logger.info(f"Using database file: {config.DB_FILE}")
    logger.info(f"Using cache folder: {config.CACHE_FOLDER}")
    logger.info(f"Using log folder: {config.LOG_FOLDER}")
    logger.info(f"Web interface will start on port: {config.HTTP_PORT}")
    
    # Initialize database
    init_db()
    # Force sync all transactions based on current order states
    # This ensures transaction states are correct after restart
    logger.info("Force syncing all transactions based on order states...")
    from ba2_trade_platform.core.TradeManager import get_trade_manager
    trade_manager = get_trade_manager()
    trade_manager.force_sync_all_transactions()

    # Initialize and start job manager for scheduled analysis
    logger.info("Starting job manager for scheduled analysis...")
    job_manager = get_job_manager()
    
    # Clear any running analysis from previous session
    job_manager.clear_running_analysis_on_startup()
    
    # Execute account refresh as an immediate background job (non-blocking)
    job_manager.execute_account_refresh_immediately()
    
    job_manager.start()

    # Initialize worker queue system
    logger.info("Initializing worker queue system...")
    initialize_worker_queue()
    
    # Initialize Smart Risk Manager queue system
    logger.info("Initializing Smart Risk Manager queue system...")
    initialize_smart_risk_manager_queue()
    
    logger.info("BA2 Trade Platform initialization complete")
    
    # Log application startup activity
    try:
        from ba2_trade_platform.core.db import log_activity
        from ba2_trade_platform.core.types import ActivityLogSeverity, ActivityLogType
        import platform
        import sys
        
        log_activity(
            severity=ActivityLogSeverity.INFO,
            activity_type=ActivityLogType.APPLICATION_STATUS_CHANGE,
            description="BA2 Trade Platform started successfully",
            data={
                "status": "started",
                "python_version": sys.version,
                "platform": platform.platform(),
                "database": config.DB_FILE,
                "port": config.HTTP_PORT
            },
            source_expert_id=None,
            source_account_id=None
        )
        logger.info("Application startup logged to activity monitor")
    except Exception as e:
        logger.warning(f"Failed to log application startup activity: {e}")

if __name__ in {"__main__", "__mp_main__"}:
    # Parse command-line arguments FIRST, before any imports that trigger initialization
    args = parse_arguments()
    
    # Import config module and update with command-line arguments
    import ba2_trade_platform.config as config
    config.DB_FILE = args.db_file
    config.CACHE_FOLDER = args.cache_folder
    config.LOG_FOLDER = args.log_folder
    config.HTTP_PORT = args.port
    
    # Suppress NiceGUI RuntimeWarning for unawaited coroutines in input.py
    # This is expected behavior in NiceGUI when synchronously updating input values
    warnings.filterwarnings('ignore', message='coroutine.*was never awaited', category=RuntimeWarning, module='nicegui.elements.input')
    
    initialize_system()
    
    # Start the UI
    import ba2_trade_platform.ui.main