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
    """Initialize database - create all tables"""
    Base.metadata.create_all(bind=engine)
