from sqlmodel import  Field, Session, SQLModel, create_engine

from ..config import DB_FILE
from ..logger import logger
from sqlalchemy import String, Float, JSON, select
import os


# Create engine and session
logger.debug(f"Database file path: {DB_FILE}")

engine = create_engine(f"sqlite:///{DB_FILE}", connect_args={"check_same_thread": False})


def init_db():
    """
    Import models and create all database tables if they do not exist.
    Ensures the database directory exists before table creation.
    """
    logger.debug("Importing models for table creation")
    from . import models  # Import the models module to register all models
    logger.debug("Models imported successfully")
    # Create directory for database if it doesn't exist
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    SQLModel.metadata.create_all(engine)

def get_db_gen():
    """
    Yields a database session for use in dependency injection or context management.
    Closes the session after use.

    Yields:
        Session: An active SQLModel session.
    """
    with Session(engine) as session:
        try: 
            yield session
        finally:
            session.close()

def get_db():
    """
    Returns a new database session. Caller is responsible for closing the session.

    Returns:
        Session: An active SQLModel session.
    """
    return Session(engine)

def add_instance(instance, session: Session | None = None):
    """
    Add a new instance to the database.
    If a session is provided, use it; otherwise, create a new session.
    Commits the transaction after adding.

    Args:
        instance: The instance to add.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.

    Returns:
        The added instance.
    """
    try:
        if session:
            session.add(instance)
            session.commit()
            logger.info(f"Added instance: {instance}")
            return instance.id
        else:
            with Session(engine) as session:
                session.add(instance)
                session.commit()
                logger.info(f"Added instance: {instance}")
                return instance.id
    except Exception as e:
        logger.error(f"Error adding instance: {e}", exc_info=True)
        raise

def update_instance(instance, session: Session | None = None):
    """
    Update an existing instance in the database.
    If a session is provided, use it; otherwise, create a new session.
    Commits and refreshes the instance after updating.

    Args:
        instance: The instance to update.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.

    Returns:
        The updated instance.
    """
    try:
        if session:
            session.add(instance)
            session.commit()
            session.refresh(instance)
            logger.info(f"Updated instance: {instance}")
        else:
            with Session(engine) as session:
                session.add(instance)
                session.commit()
                session.refresh(instance)
                logger.info(f"Updated instance: {instance}")
    except Exception as e:
        logger.error(f"Error updating instance: {e}", exc_info=True)
        raise

def delete_instance(instance, session: Session | None = None):
    """
    Delete an instance from the database.
    If a session is provided, use it; otherwise, create a new session.
    Commits the transaction after deleting.

    Args:
        instance: The instance to delete.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.

    Returns:
        True if deletion was successful.
    """
    try:
        instance_id = instance.id
        if session:
            session.delete(instance)
            session.commit()
            logger.info(f"Deleted instance with id: {instance_id}")
            return True
        else:
            with Session(engine) as session:
                session.delete(instance)
                session.commit()
                logger.info(f"Deleted instance with id: {instance_id}")
                return True
    except Exception as e:
        logger.error(f"Error deleting instance: {e}", exc_info=True)
        raise

def get_instance(model_class, instance_id):
    """
    Retrieve a single instance by model class and primary key ID.

    Args:
        model_class: The SQLModel class to query.
        instance_id: The primary key value of the instance.

    Returns:
        The instance if found, otherwise None.
    """
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
    """
    Retrieve all instances of a given model class from the database.

    Args:
        model_class: The SQLModel class to query.

    Returns:
        List of all instances of the model class.
    """
    try:
        with Session(engine) as session:
            statement = select(model_class)
            results = session.exec(statement)
            instances = results.all()
            logger.info(f"Retrieved {len(instances)} instances of {model_class.__name__}")
            return [i[0] for i in instances] # https://stackoverflow.com/questions/1958219/how-to-convert-sqlalchemy-row-object-to-a-python-dict
    except Exception as e:
        logger.error(f"Error retrieving all instances: {e}", exc_info=True)
        raise