"""
Database configuration and session management
"""

from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# The test platform's app DB is DATA -> it lives in the test/ bucket of BA2_HOME
# (NOT inside the repo). Build the absolute default from ba2_common.config.TEST_DIR
# (single source of truth for the layout). DATABASE_URL env still wins.
from ba2_common.config import TEST_DIR as _TEST_DIR

os.makedirs(_TEST_DIR, exist_ok=True)
_default_db_path = os.path.join(_TEST_DIR, "dl_forecasting.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_default_db_path}")

# SQLite connection args for better concurrency
sqlite_connect_args = {
    "check_same_thread": False,
    "timeout": 30,  # Wait up to 30 seconds for locks
}

_is_sqlite = DATABASE_URL.startswith("sqlite")

# Create engine.
# For SQLite use NullPool so every session gets its own file handle — no pool
# to exhaust when many worker threads open sessions concurrently.
# For other DBs keep the default QueuePool and test connections before use.
engine = create_engine(
    DATABASE_URL,
    connect_args=sqlite_connect_args if _is_sqlite else {},
    poolclass=NullPool if _is_sqlite else None,
    echo=False,
    pool_pre_ping=not _is_sqlite,
)


def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Set SQLite pragmas for better concurrency."""
    cursor = dbapi_connection.cursor()
    # Enable WAL mode for concurrent reads during writes
    cursor.execute("PRAGMA journal_mode=WAL")
    # Set busy timeout (in milliseconds)
    cursor.execute("PRAGMA busy_timeout=30000")
    # Synchronous mode - NORMAL is a good balance of safety and speed
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


# Apply SQLite pragmas on connection
if DATABASE_URL.startswith("sqlite"):
    event.listen(engine, "connect", _set_sqlite_pragma)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create base class for models
Base = declarative_base()


def get_db():
    """
    Dependency function to get database session.
    Use with FastAPI Depends.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database - create all tables, then reconcile the resume tables' columns.

    ``create_all`` never alters an existing table, so a DB holding an EARLIER shape of the
    migration-033 tables (``strategy_optimization_runners`` without ``run_token``...) would keep it
    and the handler would fail on "no such column". The 033 upgrade is idempotent and only ADDs
    missing nullable columns, so every CLI start converges on the model's schema too."""
    Base.metadata.create_all(bind=engine)
    reconcile_resume_tables(engine)


def reconcile_resume_tables(bind) -> bool:
    """Run migration 033's idempotent upgrade on ``bind`` (SQLite only). True when it changed
    something."""
    if bind.dialect.name != "sqlite":
        return False
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "db_migrate", "033_add_strategy_optimization_runners.py")
    spec = importlib.util.spec_from_file_location("_migration_033", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = bind.raw_connection()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            changed = mod.upgrade(raw.cursor(), raw)
        if changed:
            print(f"init_db: migration 033{out.getvalue().rstrip()}", flush=True)
        return changed
    finally:
        raw.close()
