"""
TradeActionEvaluator - Evaluates trading conditions and executes actions based on rulesets

This module provides functionality to evaluate trading conditions against rulesets
and execute appropriate trading actions based on the evaluation results.
"""

from typing import List, Dict, Any, Optional, Tuple
from .TradeConditions import TradeCondition, create_condition
from .TradeActions import TradeAction, create_action
from .AccountInterface import AccountInterface
from .models import Ruleset, EventAction, TradingOrder, TradeActionResult, ExpertRecommendation
from .types import OrderRecommendation, ExpertEventType, ExpertActionType
from .db import get_db, get_instance
from ..logger import logger
from sqlmodel import select


class TradeActionEvaluator:
    """
    Evaluates trade conditions and executes actions based on rulesets.
    
    This class takes trade action and condition parameters along with a ruleset ID,
    creates condition instances from the ruleset, evaluates them against the given
    parameters, and returns a list of actions to execute.
    """
    
    def __init__(self, account: AccountInterface):
        """
        Initialize the evaluator with an account interface.
        
        Args:
            account: Account interface for executing trades and accessing account data
        """
        self.account = account
    
    def evaluate(self, instrument_name: str, expert_recommendation: ExpertRecommendation,
                 ruleset_id: int, existing_order: Optional[TradingOrder] = None) -> List[Dict[str, Any]]:
        """
        Evaluate trading conditions from a ruleset and return applicable actions WITHOUT executing them.
        
        Args:
            instrument_name: Name of the instrument being evaluated
            expert_recommendation: The expert recommendation triggering evaluation
            ruleset_id: ID of the ruleset to evaluate
            existing_order: Optional existing order related to this evaluation
            
        Returns:
            List of action definitions, each containing:
            - action_type: ExpertActionType
            - action_config: Dict with action configuration
            - description: str describing what the action does
            - instrument_name: str
            - expert_recommendation: ExpertRecommendation
            - existing_order: Optional[TradingOrder]
        """
        try:
            # Get the ruleset
            ruleset = get_instance(Ruleset, ruleset_id)
            if not ruleset:
                logger.error(f"Ruleset with ID {ruleset_id} not found")
                return []
            
            logger.info(f"Evaluating ruleset '{ruleset.name}' for {instrument_name}")
            
            # Get all event actions for this ruleset
            with get_db() as session:
                statement = (
                    select(EventAction)
                    .join(EventAction.rulesets)
                    .where(Ruleset.id == ruleset_id)
                )
                event_actions = session.exec(statement).all()
            
            if not event_actions:
                logger.info(f"No event actions found for ruleset {ruleset_id}")
                return []
            
            action_definitions = []
            
            # Process each event action
            for event_action in event_actions:
                logger.debug(f"Processing event action: {event_action.name}")
                
                # Evaluate conditions (triggers) for this event action
                conditions_met = self._evaluate_conditions(
                    event_action, instrument_name, expert_recommendation, existing_order
                )
                
                if conditions_met:
                    logger.info(f"Conditions met for event action: {event_action.name}")
                    
                    # Store action definitions (don't execute them)
                    action_definitions.extend(
                        self._store_action_definitions(
                            event_action, instrument_name, expert_recommendation, existing_order
                        )
                    )
                    
                    # Check if we should continue processing more event actions
                    if not event_action.continue_processing:
                        logger.debug(f"Stopping processing after {event_action.name} (continue_processing=False)")
                        break
                else:
                    logger.debug(f"Conditions not met for event action: {event_action.name}")
            
            return action_definitions
            
        except Exception as e:
            logger.error(f"Error evaluating ruleset {ruleset_id}: {e}", exc_info=True)
            return [{"error": f"Error evaluating ruleset: {str(e)}"}]
    
    def apply_actions(self, action_definitions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Execute a list of action definitions that were previously stored by evaluate().
        
        Args:
            action_definitions: List of action definitions from evaluate()
            
        Returns:
            List of action execution results, each containing:
            - action_type: ExpertActionType
            - success: bool
            - message: str
            - data: Any additional data
            - description: str describing what the action does
        """
        action_results = []
        
        try:
            for action_def in action_definitions:
                # Skip error entries
                if "error" in action_def:
                    action_results.append({
                        "action_type": None,
                        "success": False,
                        "message": action_def["error"],
                        "data": None,
                        "description": action_def.get("description", "Error in action definition")
                    })
                    continue
                
                try:
                    action_type = action_def["action_type"]
                    action_config = action_def["action_config"]
                    instrument_name = action_def["instrument_name"]
                    order_recommendation = action_def["order_recommendation"]
                    existing_order = action_def["existing_order"]
                    
                    # Create action instance
                    action = self._create_action_from_config(
                        action_type, action_config, instrument_name,
                        order_recommendation, existing_order
                    )
                    
                    if not action:
                        logger.warning(f"Could not create action for {action_type}")
                        action_results.append({
                            "action_type": action_type,
                            "success": False,
                            "message": f"Failed to create action {action_type}",
                            "data": None,
                            "description": action_def.get("description", f"Failed to create {action_type.value} action")
                        })
                        continue
                    
                    # Execute action
                    logger.info(f"Executing {action_type.value} action for {instrument_name}")
                    logger.info(f"Action description: {action.get_description()}")
                    
                    execution_result = action.execute()
                    
                    # Convert TradeActionResult to dictionary format for compatibility
                    result_dict = {
                        "action_type": action_type,
                        "success": execution_result.success,
                        "message": execution_result.message,
                        "data": execution_result.data,
                        "description": action.get_description()
                    }
                    
                    action_results.append(result_dict)
                    
                    logger.info(f"Action {action_type} result: {execution_result.success} - {execution_result.message}")
                    
                except Exception as e:
                    logger.error(f"Error executing individual action: {e}", exc_info=True)
                    action_results.append({
                        "action_type": action_def.get("action_type"),
                        "success": False,
                        "message": f"Error executing action: {str(e)}",
                        "data": None,
                        "description": action_def.get("description", "Action execution failed")
                    })
            
        except Exception as e:
            logger.error(f"Error in apply_actions: {e}", exc_info=True)
            action_results.append({
                "action_type": None,
                "success": False,
                "message": f"Error applying actions: {str(e)}",
                "data": None,
                "description": "Action application failed due to error"
            })
        
        return action_results
    
    def _evaluate_conditions(self, event_action: EventAction, instrument_name: str,
                           expert_recommendation: ExpertRecommendation,
                           existing_order: Optional[TradingOrder]) -> bool:
        """
        Evaluate all conditions (triggers) for an event action.
        
        Args:
            event_action: The event action containing triggers to evaluate
            instrument_name: Instrument name
            expert_recommendation: Expert recommendation
            existing_order: Optional existing order
            
        Returns:
            True if all conditions are met, False otherwise
        """
        try:
            triggers = event_action.triggers
            if not triggers:
                logger.debug(f"No triggers defined for event action {event_action.name}")
                return True  # No conditions means always true
            
            # Process each trigger condition
            for trigger_key, trigger_config in triggers.items():
                logger.debug(f"Evaluating trigger: {trigger_key}")
                
                # Parse trigger configuration
                event_type_str = trigger_config.get('event_type')
                if not event_type_str:
                    logger.warning(f"No event_type specified for trigger {trigger_key}")
                    continue
                
                try:
                    event_type = ExpertEventType(event_type_str)
                except ValueError:
                    logger.error(f"Invalid event type: {event_type_str}")
                    continue
                
                # Create condition instance
                condition = self._create_condition_from_trigger(
                    event_type, trigger_config, instrument_name, 
                    expert_recommendation, existing_order
                )
                
                if not condition:
                    logger.warning(f"Could not create condition for trigger {trigger_key}")
                    return False
                
                # Evaluate condition
                condition_result = condition.evaluate()
                logger.debug(f"Condition {trigger_key} result: {condition_result}")
                logger.debug(f"Condition description: {condition.get_description()}")
                
                if not condition_result:
                    logger.debug(f"Condition {trigger_key} not met, stopping evaluation")
                    return False
            
            logger.debug(f"All conditions met for event action {event_action.name}")
            return True
            
        except Exception as e:
            logger.error(f"Error evaluating conditions for event action {event_action.name}: {e}", exc_info=True)
            return False
    
    def _create_condition_from_trigger(self, event_type: ExpertEventType, trigger_config: Dict[str, Any],
                                     instrument_name: str, expert_recommendation: ExpertRecommendation,
                                     existing_order: Optional[TradingOrder]) -> Optional[TradeCondition]:
        """
        Create a condition instance from trigger configuration.
        
        Args:
            event_type: Type of condition to create
            trigger_config: Configuration for the trigger
            instrument_name: Instrument name
            expert_recommendation: Expert recommendation
            existing_order: Optional existing order
            
        Returns:
            TradeCondition instance or None if creation failed
        """
        try:
            # Extract operator and value for numeric conditions
            operator_str = trigger_config.get('operator')
            value = trigger_config.get('value')
            
            # Create condition using factory function
            condition = create_condition(
                event_type=event_type,
                account=self.account,
                instrument_name=instrument_name,
                expert_recommendation=expert_recommendation,
                existing_order=existing_order,
                operator_str=operator_str,
                value=value
            )
            
            return condition
            
        except Exception as e:
            logger.error(f"Error creating condition for {event_type}: {e}", exc_info=True)
            return None
    
    def _store_action_definitions(self, event_action: EventAction, instrument_name: str,
                                expert_recommendation: ExpertRecommendation,
                                existing_order: Optional[TradingOrder]) -> List[Dict[str, Any]]:
        """
        Store action definitions for an event action without executing them.
        
        Args:
            event_action: The event action containing actions to store
            instrument_name: Instrument name
            expert_recommendation: Expert recommendation
            existing_order: Optional existing order
            
        Returns:
            List of action definitions
        """
        action_definitions = []
        
        try:
            actions = event_action.actions
            if not actions:
                logger.debug(f"No actions defined for event action {event_action.name}")
                return action_definitions
            
            # Process each action
            for action_key, action_config in actions.items():
                logger.debug(f"Storing action definition: {action_key}")
                
                # Parse action configuration
                action_type_str = action_config.get('action_type') or action_config.get('type')
                if not action_type_str:
                    logger.warning(f"No action_type specified for action {action_key}")
                    continue
                
                try:
                    action_type = ExpertActionType(action_type_str)
                except ValueError:
                    logger.error(f"Invalid action type: {action_type_str}")
                    continue
                
                # Get action description without creating the action instance
                description = self._get_action_description(
                    action_type, action_config, instrument_name
                )
                
                # Store action definition
                action_definition = {
                    "action_type": action_type,
                    "action_config": action_config.copy(),
                    "description": description,
                    "instrument_name": instrument_name,
                    "expert_recommendation": expert_recommendation,
                    "existing_order": existing_order
                }
                
                action_definitions.append(action_definition)
                logger.info(f"Stored action definition: {action_type.value} for {instrument_name}")
            
        except Exception as e:
            logger.error(f"Error storing action definitions for event action {event_action.name}: {e}", exc_info=True)
            action_definitions.append({
                "error": f"Error storing action definitions: {str(e)}",
                "description": "Action definition storage failed due to error"
            })
        
        return action_definitions
    
    def _get_action_description(self, action_type: ExpertActionType, action_config: Dict[str, Any],
                              instrument_name: str) -> str:
        """
        Get a description of what an action will do without creating the action instance.
        
        Args:
            action_type: Type of action
            action_config: Configuration for the action
            instrument_name: Instrument name
            
        Returns:
            Description string
        """
        try:
            if action_type == ExpertActionType.BUY:
                return f"Buy {instrument_name}"
            elif action_type == ExpertActionType.SELL:
                return f"Sell {instrument_name}"
            elif action_type == ExpertActionType.CLOSE:
                return f"Close position in {instrument_name}"
            elif action_type == ExpertActionType.ADJUST_TAKE_PROFIT:
                take_profit_price = action_config.get('take_profit_price')
                return f"Adjust take profit for {instrument_name}" + (f" to {take_profit_price}" if take_profit_price else "")
            elif action_type == ExpertActionType.ADJUST_STOP_LOSS:
                stop_loss_price = action_config.get('stop_loss_price')
                return f"Adjust stop loss for {instrument_name}" + (f" to {stop_loss_price}" if stop_loss_price else "")
            else:
                return f"{action_type.value} action for {instrument_name}"
        except Exception as e:
            logger.error(f"Error getting action description: {e}", exc_info=True)
            return f"Unknown action for {instrument_name}"

    def _create_action_from_config(self, action_type: ExpertActionType, action_config: Dict[str, Any],
                                  instrument_name: str, order_recommendation: OrderRecommendation,
                                  existing_order: Optional[TradingOrder]) -> Optional[TradeAction]:
        """
        Create an action instance from action configuration.
        
        Args:
            action_type: Type of action to create
            action_config: Configuration for the action
            instrument_name: Instrument name
            order_recommendation: Order recommendation
            existing_order: Optional existing order
            
        Returns:
            TradeAction instance or None if creation failed
        """
        try:
            # Extract additional parameters for specific action types
            kwargs = {}
            
            if action_type == ExpertActionType.ADJUST_TAKE_PROFIT:
                kwargs['take_profit_price'] = action_config.get('take_profit_price')
            elif action_type == ExpertActionType.ADJUST_STOP_LOSS:
                kwargs['stop_loss_price'] = action_config.get('stop_loss_price')
            
            # Create action using factory function
            action = create_action(
                action_type=action_type,
                instrument_name=instrument_name,
                account=self.account,
                order_recommendation=order_recommendation,
                existing_order=existing_order,
                **kwargs
            )
            
            return action
            
        except Exception as e:
            logger.error(f"Error creating action for {action_type}: {e}", exc_info=True)
            return None
    
    def get_ruleset_description(self, ruleset_id: int) -> Optional[str]:
        """
        Get a human-readable description of what a ruleset does.
        
        Args:
            ruleset_id: ID of the ruleset
            
        Returns:
            Description string or None if ruleset not found
        """
        try:
            ruleset = get_instance(Ruleset, ruleset_id)
            if not ruleset:
                return None
            
            description_parts = [f"Ruleset: {ruleset.name}"]
            
            if ruleset.description:
                description_parts.append(f"Description: {ruleset.description}")
            
            # Get event actions and their descriptions
            with get_db() as session:
                statement = (
                    select(EventAction)
                    .join(EventAction.rulesets)
                    .where(Ruleset.id == ruleset_id)
                )
                event_actions = session.exec(statement).all()
            
            if event_actions:
                description_parts.append("Rules:")
                for i, event_action in enumerate(event_actions, 1):
                    description_parts.append(f"  {i}. {event_action.name}")
                    
                    # Describe triggers
                    if event_action.triggers:
                        description_parts.append("     Conditions:")
                        for trigger_key, trigger_config in event_action.triggers.items():
                            event_type_str = trigger_config.get('event_type') or trigger_config.get('type', 'Unknown')
                            operator_str = trigger_config.get('operator', '')
                            value = trigger_config.get('value', '')
                            condition_desc = f"{event_type_str}"
                            if operator_str and value is not None:
                                condition_desc += f" {operator_str} {value}"
                            description_parts.append(f"       - {condition_desc}")
                    
                    # Describe actions
                    if event_action.actions:
                        description_parts.append("     Actions:")
                        for action_key, action_config in event_action.actions.items():
                            action_type_str = action_config.get('action_type') or action_config.get('type', 'Unknown')
                            description_parts.append(f"       - {action_type_str}")
            
            return "\n".join(description_parts)
            
        except Exception as e:
            logger.error(f"Error getting ruleset description for {ruleset_id}: {e}", exc_info=True)
            return f"Error getting description: {str(e)}"
