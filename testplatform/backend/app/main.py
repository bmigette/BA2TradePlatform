"""
Deep Learning Financial Forecasting Platform - Main API Application

This is the entry point for the FastAPI application.
"""

# CRITICAL: Set matplotlib backend before any imports that might use it
# tsai/fastai use matplotlib internally, and the default TkAgg backend
# causes errors when running in a web server (non-main thread)
import matplotlib
matplotlib.use('Agg')  # Use non-GUI backend

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from pathlib import Path
import sys
import time
import json
import math


class NaNSafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles NaN and Inf values by converting them to None."""

    def default(self, obj):
        return super().default(obj)

    def encode(self, obj):
        return super().encode(self._sanitize(obj))

    def _sanitize(self, obj):
        """Recursively sanitize NaN/Inf values in nested structures."""
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        elif isinstance(obj, dict):
            return {k: self._sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._sanitize(item) for item in obj]
        return obj


class NaNSafeJSONResponse(JSONResponse):
    """JSON response that handles NaN and Inf values."""

    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            cls=NaNSafeJSONEncoder,
            separators=(",", ":"),
        ).encode("utf-8")

# Import and initialize logging configuration
from app.logging_config import setup_logging, get_logger

# Set up logging with separate files for debug, info, and error
setup_logging(
    log_dir="logs",
    debug_log="debug.log",
    info_log="info.log",
    error_log="error.log"
)

logger = get_logger(__name__)


class APILoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all API requests and responses."""

    # Endpoints that are polled frequently - don't log to reduce noise
    QUIET_ENDPOINTS = (
        '/progress',
        '/generations',
        '/individuals',
        '/api/dashboard/stats',
    )

    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        path = request.url.path

        # Skip logging for frequently polled endpoints
        should_log = not any(path.endswith(ep) or path == ep for ep in self.QUIET_ENDPOINTS)

        if should_log:
            logger.info(f"API Request: {request.method} {path}")

        # Process the request
        response = await call_next(request)

        # Calculate duration
        duration = time.time() - start_time

        if should_log:
            level = "info"
            tag = ""
            if duration > 4.0:
                level = "warning"
                tag = " [SLOW]"
            elif duration > 1.0:
                tag = " [medium]"

            msg = (
                f"API Response: {request.method} {path} - "
                f"Status: {response.status_code} - Duration: {duration:.3f}s{tag}"
            )
            getattr(logger, level)(msg)

        return response


# Create FastAPI app with NaN-safe JSON response
app = FastAPI(
    title="Deep Learning Financial Forecasting Platform",
    description="Train and evaluate deep learning models for financial forecasting",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    default_response_class=NaNSafeJSONResponse
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add API logging middleware
app.add_middleware(APILoggingMiddleware)


@app.get("/")
async def root():
    """Root endpoint - API information"""
    return {
        "name": "Deep Learning Financial Forecasting Platform API",
        "version": "0.1.0",
        "status": "operational",
        "docs": "/docs",
        "redoc": "/redoc"
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "version": "0.1.0"
    }


def _create_default_indicator_collections(db):
    """Create default indicator collections synchronously during startup."""
    from app.models.indicator_collection import IndicatorCollection

    default_timeframes = ['15m', '1h', '4h', '1d']

    for tf in default_timeframes:
        collection_name = f"All Indicators - {tf.upper()}"

        # Check if already exists
        existing = db.query(IndicatorCollection).filter(
            IndicatorCollection.name == collection_name
        ).first()

        if existing:
            continue

        # Create all indicators for this timeframe
        indicators = []

        # SMA variations
        for period in [10, 20, 50, 100, 200]:
            indicators.append({
                "type": "sma", "name": f"SMA {period}",
                "period": period, "timeframe": tf
            })

        # EMA variations
        for period in [12, 26, 50, 100, 200]:
            indicators.append({
                "type": "ema", "name": f"EMA {period}",
                "period": period, "timeframe": tf
            })

        # RSI, MACD, Bollinger, ATR, Stochastic
        indicators.extend([
            {"type": "rsi", "name": "RSI 14", "period": 14, "timeframe": tf},
            {"type": "macd", "name": "MACD (12,26,9)", "fast": 12, "slow": 26, "signal": 9, "timeframe": tf},
            {"type": "bbands", "name": "Bollinger Bands (20,2)", "period": 20, "std_dev": 2.0, "timeframe": tf},
            {"type": "atr", "name": "ATR 14", "period": 14, "timeframe": tf},
            {"type": "stochastic", "name": "Stochastic (14,3,3)", "k_period": 14, "d_period": 3, "smooth_k": 3, "timeframe": tf},
        ])

        collection = IndicatorCollection(
            name=collection_name,
            description=f"Default collection with all standard indicators at {tf.upper()} timeframe",
            indicators=indicators,
            is_default=True
        )
        db.add(collection)

    db.commit()
    logger.info("Default indicator collections initialized")


@app.on_event("startup")
async def startup_event():
    """Run on application startup"""
    logger.info("Starting Deep Learning Financial Forecasting Platform API")

    # Create necessary directories. Test-bucket artifact dirs (datasets,
    # trained_models, caches, exports) live under ba2_common.config.TEST_DIR and
    # are created on import of app.paths — NOT inside the repo/CWD.
    from app import paths as _paths  # noqa: F401 (import triggers dir creation)
    Path("logs").mkdir(exist_ok=True)

    # Initialize database tables
    # Import all models before init_db to ensure tables are created
    from app.models.database import init_db, DATABASE_URL
    from app.models.optimization_profile import OptimizationProfile  # noqa: F401
    from app.models.model import TrainedModel  # noqa: F401
    init_db()

    # The test platform OWNS its DB path (DATABASE_URL -> test/dl_forecasting.db). Point the
    # shared ba2_common engine at the SAME file so providers/experts read API keys (appsetting)
    # from the one test DB — ba2_common defaults to a neutral path, so this is required. (The
    # LIVE platform does the analogous configure_db to its trade DB.) Per-run backtests still
    # override via configure_db_threadlocal. Only meaningful for on-disk sqlite paths.
    if DATABASE_URL.startswith("sqlite:///"):
        try:
            from ba2_common.core import db as _ba2_db
            _ba2_db.configure_db(DATABASE_URL.replace("sqlite:///", "", 1))
        except Exception as _e:  # noqa: BLE001 — non-fatal; key reads would fail loudly later
            logger.warning(f"could not point ba2_common DB at the test DB: {_e}")

    # Run pending database migrations (ALTER TABLE etc.)
    try:
        import subprocess as _sp
        _sp.run([sys.executable, "scripts/migrate_db.py"], timeout=30,
                capture_output=True, text=True, cwd=str(Path(__file__).parent.parent))
        logger.info("Database migrations checked")
    except Exception as e:
        logger.warning(f"Migration check failed (non-fatal): {e}")

    # Initialize default indicator collections
    from app.models.database import SessionLocal
    from app.models.indicator_collection import IndicatorCollection
    try:
        db = SessionLocal()
        # Check if default collections exist
        existing_defaults = db.query(IndicatorCollection).filter(
            IndicatorCollection.is_default == True
        ).count()
        if existing_defaults == 0:
            logger.info("Initializing default indicator collections...")
            _create_default_indicator_collections(db)
        db.close()
    except Exception as e:
        logger.warning(f"Could not initialize default collections: {e}")

    # Initialize task queue
    from app.services.task_queue import init_task_queue, get_task_queue, init_ohlcv_task_queue, get_ohlcv_task_queue, init_training_task_queue, get_training_task_queue, init_backtest_task_queue, get_backtest_task_queue
    # Main queue: lightweight I/O tasks only (datasets, news)
    init_task_queue(max_workers=4, exclude_task_types=['ohlcv_cache_fetch', 'training_job', 'backtest'])
    logger.info("Main task queue initialized with 4 workers (excludes ohlcv, training, backtest)")

    # Register task handlers on the main queue
    from app.services.dataset_handler import handle_dataset_regeneration
    from app.services.job_handler import handle_training_job
    from app.services.backtest_handler import handle_backtest
    from app.services.news_batch_handler import handle_news_batch_fetch
    # Daily-expert backtest + joint strategy optimizer run IN-PROCESS on the main queue
    # (Decision: they were in-process originally; torch is lazy-imported only on the
    # engine=='ml' path inside the handlers). They are NOT routed to the dedicated
    # subprocess backtest/training queues, whose worker scripts only call handle_backtest /
    # handle_training_job and whitelist task_types ['backtest']/['training_job']. The main
    # queue does NOT exclude 'daily_backtest'/'strategy_optimization', so it consumes them.
    from app.services.backtest.daily_backtest_handler import handle_daily_backtest
    from app.services.strategy_optimization_handler import handle_strategy_optimization
    # Data-build handlers (mirror the ba2-test build commands; driven by /api/data/* endpoints).
    # These run on the main queue (it does NOT exclude these task types). OHLCV builds go to the
    # dedicated OHLCV queue via its existing ohlcv_cache_fetch handler (registered below).
    from app.services.data_build_handler import (
        handle_build_screener_metrics,
        handle_build_options,
        handle_prewarm,
    )
    task_queue = get_task_queue()
    task_queue.register_handler('dataset_regeneration', handle_dataset_regeneration)
    task_queue.register_handler('news_batch_fetch', handle_news_batch_fetch)
    task_queue.register_handler('daily_backtest', handle_daily_backtest)
    task_queue.register_handler('strategy_optimization', handle_strategy_optimization)
    task_queue.register_handler('build_screener_metrics', handle_build_screener_metrics)
    task_queue.register_handler('build_options', handle_build_options)
    task_queue.register_handler('prewarm', handle_prewarm)
    logger.info("Registered main task handlers: dataset_regeneration, news_batch_fetch, daily_backtest, strategy_optimization, build_screener_metrics, build_options, prewarm")

    # Initialize dedicated training queue (2 workers — keeps GPU from being overloaded)
    init_training_task_queue(max_workers=2)
    training_queue = get_training_task_queue()
    training_queue.register_handler('training_job', handle_training_job)
    logger.info("Training task queue initialized with 2 workers (subprocess mode)")

    # Initialize dedicated backtest queue (subprocess mode — CPU-intensive)
    init_backtest_task_queue(max_workers=2)
    backtest_queue = get_backtest_task_queue()
    backtest_queue.register_handler('backtest', handle_backtest)
    logger.info("Backtest task queue initialized with 2 workers (subprocess mode)")

    # Initialize dedicated OHLCV queue (isolated, resizable, won't affect other task types)
    from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch
    init_ohlcv_task_queue(max_workers=3)
    ohlcv_queue = get_ohlcv_task_queue()
    ohlcv_queue.register_handler('ohlcv_cache_fetch', handle_ohlcv_cache_fetch)
    logger.info("OHLCV task queue initialized with 3 workers")

    # Recover interrupted jobs (crashed while running)
    recover_interrupted_jobs()

    # NOTE: DB cleanup (clear_completed_results) is NOT run on startup — it can
    # take minutes on large databases and block the server from starting.
    # Use the POST /api/admin/db-cleanup endpoint instead.

    logger.info("Application startup complete")


def recover_interrupted_jobs():
    """Mark running jobs as stopped on startup (they crashed and can be resumed)."""
    from app.models.database import SessionLocal
    from app.models.task_queue import TaskQueue, TaskStatus

    db = SessionLocal()
    try:
        # Find jobs that were running when the app crashed
        running_jobs = db.query(TaskQueue).filter(
            TaskQueue.status == TaskStatus.RUNNING.value
        ).all()

        for job in running_jobs:
            logger.warning(f"Found interrupted job {job.task_id}, marking as stopped")
            job.status = TaskStatus.STOPPED.value
            job.progress_message = "Interrupted - can be resumed"

        if running_jobs:
            db.commit()
            logger.info(f"Recovered {len(running_jobs)} interrupted jobs - marked as 'stopped'")
        else:
            logger.info("No interrupted jobs found")

    except Exception as e:
        logger.error(f"Failed to recover interrupted jobs: {e}")
        db.rollback()
    finally:
        db.close()


@app.on_event("shutdown")
async def shutdown_event():
    """Run on application shutdown"""
    logger.info("Shutting down Deep Learning Financial Forecasting Platform API")


# Exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors (422) and log them"""
    errors = exc.errors()
    logger.error(
        f"Validation error on {request.method} {request.url.path}: {errors}"
    )
    return JSONResponse(
        status_code=422,
        content={"detail": errors}
    )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "message": str(exc)
        }
    )


# Import and include routers
from app.api import datasets, jobs, workers, dashboard, models, backtests, ml, settings, websocket, tasks, indicator_collections, tools, target_sets, strategies, admin, cache, experts, rules, ruleset_meta, data_build

app.include_router(datasets.router, prefix="/api/datasets", tags=["datasets"])
app.include_router(tools.router, prefix="/api/tools", tags=["tools"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["optimization"])
app.include_router(workers.router, prefix="/api/workers", tags=["workers"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(models.router, prefix="/api/models", tags=["models"])
app.include_router(backtests.router, prefix="/api/backtests", tags=["backtesting"])
app.include_router(ml.router, prefix="/api/ml", tags=["machine-learning"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(websocket.router, prefix="/api", tags=["websocket"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["task-queue"])
app.include_router(indicator_collections.router, prefix="/api/indicator-collections", tags=["indicator-collections"])
app.include_router(target_sets.router, prefix="/api/target-sets", tags=["target-sets"])
# rules router carries its own /api/strategies prefix; register BEFORE the strategies
# router so its literal import-rules/export-rules segments win over /{strategy_id}.
app.include_router(rules.router)
app.include_router(strategies.router, prefix="/api/strategies", tags=["strategies"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(cache.router, prefix="/api/cache", tags=["cache"])
app.include_router(data_build.router, prefix="/api/data", tags=["data-build"])
app.include_router(experts.router, tags=["experts"])
# ruleset_meta carries its own /api prefix -> /api/ruleset/vocabulary, /api/ruleset/exit-presets
app.include_router(ruleset_meta.router, tags=["ruleset-meta"])

# Additional routers (will be added as we build features)
# from app.api import models, backtests, profiles, providers, settings
# app.include_router(models.router, prefix="/api/models", tags=["models"])
# app.include_router(backtests.router, prefix="/api/backtests", tags=["backtesting"])
# app.include_router(profiles.router, prefix="/api/profiles", tags=["profiles"])
# app.include_router(providers.router, prefix="/api/providers", tags=["data-providers"])
# app.include_router(settings.router, prefix="/api/settings", tags=["settings"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
