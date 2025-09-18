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
    logger.debug("Importing models for table creation")
    from . import models  # Import the models module to register all models
    logger.debug("Models imported successfully")
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

def add_instance(instance):
    """Add a new instance to the database."""
    try:
        with Session(engine) as session:
            session.add(instance)
            session.commit()
            logger.info(f"Added instance: {instance}")
            return instance
    except Exception as e:
        logger.error(f"Error adding instance: {e}", exc_info=True)
        raise

def update_instance(instance):
    """Update an existing instance in the database."""
    try:
        with Session(engine) as session:
            session.add(instance)
            session.commit()
            session.refresh(instance)
            logger.info(f"Updated instance: {instance}")
            return instance
    except Exception as e:
        logger.error(f"Error updating instance: {e}", exc_info=True)
        raise

def delete_instance(instance):
    """Delete an instance from the database."""
    try:
        with Session(engine) as session:
            instance_id = instance.id
            session.delete(instance)
            session.commit()
            logger.info(f"Deleted instance with id: {instance_id}")
            return True
    except Exception as e:
        logger.error(f"Error deleting instance: {e}", exc_info=True)
        raise

def get_instance(model_class, instance_id):
    """Retrieve a single instance by model and ID."""
    try:
        with Session(engine) as session:
            instance = session.get(model_class, instance_id)
            if not instance:
                logger.error(f"Instance with id {instance_id} not found.")
                return None
            logger.info(f"Retrieved instance: {instance}")
            return instance
    except Exception as e:
        logger.error(f"Error retrieving instance: {e}", exc_info=True)
        raise

def get_all_instances(model_class):
    """Retrieve all instances of a model."""
    try:
        with Session(engine) as session:
            instances = session.query(model_class).all()
            logger.info(f"Retrieved {len(instances)} instances of {model_class.__name__}")
            return instances
    except Exception as e:
        logger.error(f"Error retrieving all instances: {e}", exc_info=True)
        raise