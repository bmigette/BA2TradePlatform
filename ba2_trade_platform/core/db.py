from sqlmodel import  Field, Session, SQLModel, create_engine

from ..config import DB_FILE
from ..logger import logger
from sqlalchemy import String, Float, JSON
import os


# Create engine and session
logger.debug(f"Database file path: {DB_FILE}")

engine = create_engine(f"sqlite:///{DB_FILE}", connect_args={"check_same_thread": False})


def init_db():
    """Import models and create tables."""
    # Create directory for database if it doesn't exist
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    SQLModel.metadata.create_all(engine)

def get_db_gen():
    """Dependency to get DB session."""
    with Session(engine) as session:
        try: 
            yield session
        finally:
            session.close()

def get_db():
    """Get a DB session without yielding."""
    return Session(engine)

