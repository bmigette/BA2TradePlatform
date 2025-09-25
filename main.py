import os
from ba2_trade_platform.core.db import init_db, get_db
from ba2_trade_platform.core.WorkerQueue import initialize_worker_queue
from ba2_trade_platform.core.JobManager import get_job_manager
from ba2_trade_platform.logger import logger
from ba2_trade_platform.config import LOG_FOLDER

def initialize_system():
    """Initialize the system components."""
    logger.info("Initializing BA2 Trade Platform...")
    
    # Create log folder if not exists
    os.makedirs(LOG_FOLDER, exist_ok=True)
    
    # Initialize database
    init_db()

    # Initialize and start job manager for scheduled analysis
    logger.info("Starting job manager for scheduled analysis...")
    job_manager = get_job_manager()
    
    # Clear any running analysis from previous session
    job_manager.clear_running_analysis_on_startup()
    
    job_manager.start()

    # Initialize worker queue system
    logger.info("Initializing worker queue system...")
    initialize_worker_queue()
    
    logger.info("BA2 Trade Platform initialization complete")

if __name__ in {"__main__", "__mp_main__"}:
    initialize_system()
    
    # Start the UI
    import ba2_trade_platform.ui.main