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
        else:
            with Session(engine) as session:
                session.add(instance)
                session.commit()
                session.refresh(instance)
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
                logger.error(f"Instance with id {instance_id} not found.", exc_info=True)
                return None
            #logger.debug(f"Retrieved instance: {instance}")
            return instance
    except Exception as e:
        logger.error(f"Error retrieving instance: {e}", exc_info=True)
        raise
    
def get_all_instances(model_class, session: Session | None = None):
    """
    Retrieve all instances of a given model class from the database.

    Args:
        model_class: The SQLModel class to query.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.

    Returns:
        List of all instances of the model class.
    """
    try:
        if session:
            statement = select(model_class)
            results = session.exec(statement)
            instances = results.all()
        else:
            with Session(engine) as session:
                statement = select(model_class)
                results = session.exec(statement)
                instances = results.all()
        #logger.debug(f"Retrieved {len(instances)} instances of {model_class.__name__}")
        return [i[0] for i in instances]
    except Exception as e:
        logger.error(f"Error retrieving all instances: {e}", exc_info=True)
        raise


def get_setting(key: str) -> str | None:
    """
    Retrieve an AppSetting value by key.

    Args:
        key: The setting key to retrieve.

    Returns:
        The value_str field of the AppSetting if found, otherwise None.
    """
    try:
        from .models import AppSetting
        with Session(engine) as session:
            statement = select(AppSetting).where(AppSetting.key == key)
            result = session.exec(statement).first()
            if result:
                #logger.info(f"Retrieved setting {key}: {result[0].value_str}")
                return result[0].value_str
            else:
                logger.warning(f"Setting {key} not found in database")
                return None
    except Exception as e:
        logger.error(f"Error retrieving setting {key}: {e}", exc_info=True)
        return None


def reorder_ruleset_rules(ruleset_id: int, rule_order: list[int]) -> bool:
    """
    Reorder the rules in a ruleset by updating the order_index field.
    
    Args:
        ruleset_id: The ID of the ruleset to reorder
        rule_order: List of eventaction_ids in the desired order
        
    Returns:
        True if successful, False otherwise
    """
    try:
        from .models import RulesetEventActionLink
        with Session(engine) as session:
            # Update each link with its new order index
            for index, eventaction_id in enumerate(rule_order):
                # Use SQLAlchemy Core update for better performance and compatibility
                from sqlalchemy import update
                stmt = update(RulesetEventActionLink).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.eventaction_id == eventaction_id
                ).values(order_index=index)
                
                result = session.execute(stmt)
                if result.rowcount == 0:
                    logger.error(f"Link not found for ruleset {ruleset_id}, eventaction {eventaction_id}")
                    return False
            
            session.commit()
            logger.info(f"Reordered rules for ruleset {ruleset_id}")
            return True
            
    except Exception as e:
        logger.error(f"Error reordering ruleset rules: {e}", exc_info=True)
        return False


def move_rule_up(ruleset_id: int, eventaction_id: int) -> bool:
    """
    Move a rule up one position in the ruleset order.
    
    Args:
        ruleset_id: The ID of the ruleset
        eventaction_id: The ID of the eventaction to move up
        
    Returns:
        True if successful, False otherwise
    """
    try:
        from .models import RulesetEventActionLink
        from sqlalchemy import update
        with Session(engine) as session:
            # Get the current order index
            current_result = session.exec(
                select(RulesetEventActionLink.order_index).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.eventaction_id == eventaction_id
                )
            ).first()
            
            if not current_result or current_result == 0:
                return False  # Already at top or not found
            
            current_order = current_result
            target_order = current_order - 1
            
            # Get the eventaction_id that's currently at the target position
            above_result = session.exec(
                select(RulesetEventActionLink.eventaction_id).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.order_index == target_order
                )
            ).first()
            
            if above_result:
                # Swap the order indexes using SQLAlchemy Core updates
                # Move current rule to target position
                stmt1 = update(RulesetEventActionLink).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.eventaction_id == eventaction_id
                ).values(order_index=target_order)
                
                # Move above rule to current position
                stmt2 = update(RulesetEventActionLink).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.eventaction_id == above_result
                ).values(order_index=current_order)
                
                session.execute(stmt1)
                session.execute(stmt2)
                session.commit()
                logger.info(f"Moved rule {eventaction_id} up in ruleset {ruleset_id}")
                return True
            
            return False
            
    except Exception as e:
        logger.error(f"Error moving rule up: {e}", exc_info=True)
        return False


def move_rule_down(ruleset_id: int, eventaction_id: int) -> bool:
    """
    Move a rule down one position in the ruleset order.
    
    Args:
        ruleset_id: The ID of the ruleset
        eventaction_id: The ID of the eventaction to move down
        
    Returns:
        True if successful, False otherwise
    """
    try:
        from .models import RulesetEventActionLink
        from sqlalchemy import update
        with Session(engine) as session:
            # Get the current order index
            current_result = session.exec(
                select(RulesetEventActionLink.order_index).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.eventaction_id == eventaction_id
                )
            ).first()
            
            if not current_result:
                return False  # Not found
            
            current_order = current_result
            
            # Get the max order index for this ruleset
            max_order = session.exec(
                select(RulesetEventActionLink.order_index).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id
                ).order_by(RulesetEventActionLink.order_index.desc())
            ).first()
            
            if not max_order or current_order >= max_order:
                return False  # Already at bottom
            
            target_order = current_order + 1
            
            # Get the eventaction_id that's currently at the target position
            below_result = session.exec(
                select(RulesetEventActionLink.eventaction_id).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.order_index == target_order
                )
            ).first()
            
            if below_result:
                # Swap the order indexes using SQLAlchemy Core updates
                # Move current rule to target position
                stmt1 = update(RulesetEventActionLink).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.eventaction_id == eventaction_id
                ).values(order_index=target_order)
                
                # Move below rule to current position
                stmt2 = update(RulesetEventActionLink).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id,
                    RulesetEventActionLink.eventaction_id == below_result
                ).values(order_index=current_order)
                
                session.execute(stmt1)
                session.execute(stmt2)
                session.commit()
                logger.info(f"Moved rule {eventaction_id} down in ruleset {ruleset_id}")
                return True
            
            return False
            
    except Exception as e:
        logger.error(f"Error moving rule down: {e}", exc_info=True)
        return False