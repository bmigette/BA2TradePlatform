"""
Utility functions for the BA2 Trade Platform core functionality.
"""

from typing import Optional, TYPE_CHECKING
from .db import get_instance
from .models import ExpertInstance
from ..modules.experts import get_expert_class

if TYPE_CHECKING:
    from .MarketExpertInterface import MarketExpertInterface


def get_expert_instance_from_id(expert_instance_id: int) -> Optional["MarketExpertInterface"]:
    """
    Get an expert instance with the appropriate class instantiated from the database.
    
    This function:
    1. Retrieves the ExpertInstance from the database by ID
    2. Determines the expert type from the database record
    3. Dynamically imports and instantiates the appropriate expert class
    4. Returns the instantiated expert object ready to use
    
    Args:
        expert_instance_id (int): The ID of the expert instance in the database
        
    Returns:
        Optional[MarketExpertInterface]: The instantiated expert instance, or None if not found
        
    Example:
        >>> expert = get_expert_instance_from_id(1)
        >>> if expert:
        ...     recommendations = expert.get_enabled_instruments()
        ...     analysis_result = expert.run_analysis("AAPL", market_analysis)
    """
    # Get the expert instance record from database
    expert_instance = get_instance(ExpertInstance, expert_instance_id)
    if not expert_instance:
        return None
    
    # Get the expert class based on the type stored in database
    expert_class = get_expert_class(expert_instance.expert)
    if not expert_class:
        raise ValueError(f"Unknown expert type: {expert_instance.expert}")
    
    # Instantiate and return the expert with the database ID
    return expert_class(expert_instance_id)
