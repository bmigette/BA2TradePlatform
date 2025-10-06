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
    
    def __init__(self, account: AccountInterface, instrument_name: Optional[str] = None,
                 existing_transactions: Optional[List[Any]] = None):
        """
        Initialize the evaluator with an account interface.
        
        Args:
            account: Account interface for executing trades and accessing account data
            instrument_name: Instrument symbol for looking up existing transactions
            existing_transactions: List of existing Transaction objects for open_positions use case
        """
        self.account = account
        self.instrument_name = instrument_name
        self.existing_transactions = existing_transactions or []
        # Track condition evaluation results for detailed reporting
        self.condition_evaluations = []
        self.rule_evaluations = []
        # Store TradeAction objects for reuse between evaluate() and execute()
        self.trade_actions = []
        # Store instrument name for transaction lookup
        self.instrument_name = None
    
    def evaluate(self, instrument_name: str, expert_recommendation: ExpertRecommendation,
                 ruleset_id: int, existing_order: Optional[TradingOrder] = None) -> List[Dict[str, Any]]:
        """
        Evaluate trading conditions from a ruleset and create TradeAction objects for applicable actions.
        
        Args:
            instrument_name: Name of the instrument being evaluated
            expert_recommendation: The expert recommendation triggering evaluation
            ruleset_id: ID of the ruleset to evaluate
            existing_order: Optional existing order related to this evaluation
            
        Returns:
            List of action summaries for display, each containing:
            - action_type: ExpertActionType
            - description: str describing what the action does
            - condition_evaluations: List of condition evaluation results
            - rule_evaluations: List of rule evaluation results
        """
        try:
            # Clear previous evaluation tracking and stored actions
            self.condition_evaluations = []
            self.rule_evaluations = []
            self.trade_actions = []
            
            # Store instrument name for later use in execute()
            self.instrument_name = instrument_name
            
            # Get the ruleset
            ruleset = get_instance(Ruleset, ruleset_id)
            if not ruleset:
                logger.error(f"Ruleset with ID {ruleset_id} not found")
                return []
            
            logger.info(f"Evaluating ruleset '{ruleset.name}' for {instrument_name}")
            
            # Get all event actions for this ruleset
            with get_db() as session:
                    from .models import RulesetEventActionLink
                    statement = (
                        select(EventAction)
                        .join(RulesetEventActionLink, EventAction.id == RulesetEventActionLink.eventaction_id)
                        .where(RulesetEventActionLink.ruleset_id == ruleset_id)
                        .order_by(RulesetEventActionLink.order_index)
                    )
                    event_actions = session.exec(statement).all()
            
            if not event_actions:
                logger.info(f"No event actions found for ruleset {ruleset_id}")
                return []
            
            action_summaries = []
            
            # Process each event action
            for event_action in event_actions:
                logger.debug(f"Processing event action: {event_action.name}")
                
                # Evaluate conditions (triggers) for this event action
                conditions_met = self._evaluate_conditions(
                    event_action, instrument_name, expert_recommendation, existing_order
                )
                
                if conditions_met:
                    logger.info(f"Conditions met for event action: {event_action.name}")
                    
                    # Create and store TradeAction objects
                    action_summaries.extend(
                        self._create_and_store_trade_actions(
                            event_action, instrument_name, expert_recommendation, existing_order
                        )
                    )
                    
                    # Check if we should continue processing more event actions
                    if not event_action.continue_processing:
                        logger.debug(f"Stopping processing after {event_action.name} (continue_processing=False)")
                        break
                else:
                    logger.debug(f"Conditions not met for event action: {event_action.name}")
            
            # Add evaluation tracking to each action summary
            for action_summary in action_summaries:
                if "error" not in action_summary:
                    action_summary["condition_evaluations"] = self.condition_evaluations.copy()
                    action_summary["rule_evaluations"] = self.rule_evaluations.copy()
            
            logger.info(f"✅ Evaluation complete: {len(action_summaries)} action(s) created, {len(self.trade_actions)} stored for execution")
            
            return action_summaries
            
        except Exception as e:
            logger.error(f"Error evaluating ruleset {ruleset_id}: {e}", exc_info=True)
            return [{"error": f"Error evaluating ruleset: {str(e)}"}]
    
    def execute(self) -> List[Dict[str, Any]]:
        """
        Execute the TradeAction objects that were previously created and stored by evaluate().
        
        Two execution modes:
        1. entering_markets: Execute BUY/SELL actions first, then use created orders for TP/SL
        2. open_positions: Use existing transactions to find orders for TP/SL adjustments
        
        Returns:
            List of action execution results
        """
        action_results = []
        created_order_ids = []  # Track orders created during this execution
        
        logger.info(f"🚀 EXECUTE() CALLED with {len(self.trade_actions)} trade actions for {self.instrument_name}")
        
        try:
            if not self.trade_actions:
                logger.warning("No trade actions to execute. Call evaluate() first.")
                return [{
                    "action_type": None,
                    "success": False,
                    "message": "No trade actions to execute. Call evaluate() first.",
                    "data": None,
                    "description": "No actions available for execution"
                }]
            
            # Sort actions by priority (order-creating actions first, then adjustments)
            sorted_actions = self._sort_actions_by_priority(self.trade_actions)
            
            # Categorize actions
            order_creating_actions = []
            adjustment_actions = []
            
            for action in sorted_actions:
                action_type = self._get_action_type_from_action(action)
                if action_type in [ExpertActionType.BUY, ExpertActionType.SELL, ExpertActionType.CLOSE]:
                    order_creating_actions.append(action)
                elif action_type in [ExpertActionType.ADJUST_TAKE_PROFIT, ExpertActionType.ADJUST_STOP_LOSS]:
                    adjustment_actions.append(action)
            
            logger.info(f"📊 Categorized actions: {len(order_creating_actions)} order-creating, {len(adjustment_actions)} adjustments")
            
            # Validate: If we have adjustment actions, we need either order-creating actions OR existing transactions
            if adjustment_actions and not order_creating_actions and not self.existing_transactions:
                error_msg = f"Cannot execute TP/SL adjustment actions without a BUY/SELL action or existing open transactions for {self.instrument_name or 'unknown instrument'}"
                logger.error(error_msg)
                return [{
                    "action_type": ExpertActionType.ADJUST_TAKE_PROFIT,
                    "success": False,
                    "message": error_msg,
                    "data": None,
                    "description": "Missing order dependency"
                }]
            
            # Phase 1: Execute order-creating actions (entering_markets use case)
            for i, action in enumerate(order_creating_actions):
                try:
                    logger.info(f"Phase 1 - Creating order: {action.get_description()}")
                    
                    execution_result = action.execute()
                    action_type = self._get_action_type_from_action(action)
                    
                    result_dict = {
                        "action_type": execution_result.get('action_type') or action_type,
                        "success": execution_result.get('success', False),
                        "message": execution_result.get('message', ''),
                        "data": execution_result.get('data', {}),
                        "description": action.get_description()
                    }
                    
                    action_results.append(result_dict)
                    
                    # Capture created order ID for use in phase 2
                    if result_dict['success'] and result_dict.get('data', {}).get('order_id'):
                        created_order_ids.append(result_dict['data']['order_id'])
                        logger.info(f"Created order {result_dict['data']['order_id']} - will be used for TP/SL adjustments")
                    
                    logger.info(f"Order creation result: {result_dict['success']} - {result_dict['message']}")
                    
                except Exception as e:
                    logger.error(f"Error creating order: {e}", exc_info=True)
                    action_results.append({
                        "action_type": self._get_action_type_from_action(action),
                        "success": False,
                        "message": f"Error executing action: {str(e)}",
                        "data": None,
                        "description": action.get_description()
                    })
            
            # Phase 1.5: Create transactions for newly created orders (required for TP/SL)
            if created_order_ids and adjustment_actions:
                logger.info(f"Creating transactions for {len(created_order_ids)} new orders before TP/SL adjustments")
                from .db import get_instance, update_instance
                from .models import TradingOrder
                
                for order_id in created_order_ids:
                    order = get_instance(TradingOrder, order_id)
                    if order and not order.transaction_id:
                        try:
                            # Create transaction for the order using account interface private method
                            self.account._create_transaction_for_order(order)
                            # Update the order in database with new transaction_id
                            update_instance(order)
                            logger.info(f"Created transaction {order.transaction_id} for order {order_id}")
                        except Exception as e:
                            logger.error(f"Error creating transaction for order {order_id}: {e}", exc_info=True)
            
            # Phase 2: Execute adjustment actions (TP/SL)
            if adjustment_actions:
                # Determine which orders to adjust
                orders_to_adjust = []
                
                if created_order_ids:
                    # entering_markets: Use newly created orders (refresh from DB to get transaction_id)
                    from .db import get_instance
                    from .models import TradingOrder
                    
                    for order_id in created_order_ids:
                        order = get_instance(TradingOrder, order_id)
                        if order:
                            orders_to_adjust.append(order)
                            logger.info(f"Will adjust TP/SL for newly created order {order_id} (transaction: {order.transaction_id})")
                    
                elif self.existing_transactions:
                    # open_positions: Use orders from existing transactions
                    from .db import get_instance
                    from .models import TradingOrder
                    
                    for transaction in self.existing_transactions:
                        if hasattr(transaction, 'order_id') and transaction.order_id:
                            order = get_instance(TradingOrder, transaction.order_id)
                            if order:
                                orders_to_adjust.append(order)
                                logger.info(f"Will adjust TP/SL for existing transaction order {order.id}")
                
                if not orders_to_adjust:
                    logger.warning("No orders available for TP/SL adjustments")
                
                # Execute adjustment actions for each order
                for order in orders_to_adjust:
                    for action in adjustment_actions:
                        try:
                            # Update action's existing_order to the current order
                            action.existing_order = order
                            
                            logger.info(f"Phase 2 - Adjusting order {order.id}: {action.get_description()}")
                            
                            execution_result = action.execute()
                            action_type = self._get_action_type_from_action(action)
                            
                            result_dict = {
                                "action_type": execution_result.get('action_type') or action_type,
                                "success": execution_result.get('success', False),
                                "message": execution_result.get('message', ''),
                                "data": execution_result.get('data', {}),
                                "description": action.get_description()
                            }
                            
                            action_results.append(result_dict)
                            
                            logger.info(f"Adjustment result: {result_dict['success']} - {result_dict['message']}")
                            
                        except Exception as e:
                            logger.error(f"Error executing adjustment action: {e}", exc_info=True)
                            action_results.append({
                                "action_type": self._get_action_type_from_action(action),
                                "success": False,
                                "message": f"Error executing action: {str(e)}",
                                "data": None,
                                "description": action.get_description()
                            })
            
        except Exception as e:
            logger.error(f"Error in execute: {e}", exc_info=True)
            action_results.append({
                "action_type": None,
                "success": False,
                "message": f"Error executing actions: {str(e)}",
                "data": None,
                "description": "Action execution failed due to error"
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
            rule_evaluation = {
                "rule_name": event_action.name,
                "rule_id": event_action.id,
                "conditions": [],
                "all_conditions_met": True,
                "executed": False
            }
            
            triggers = event_action.triggers
            if not triggers:
                logger.debug(f"No triggers defined for event action {event_action.name}")
                rule_evaluation["all_conditions_met"] = True
                rule_evaluation["executed"] = True
                self.rule_evaluations.append(rule_evaluation)
                return True  # No conditions means always true
            
            # Process each trigger condition
            for trigger_key, trigger_config in triggers.items():
                logger.debug(f"Evaluating trigger: {trigger_key}")
                
                condition_evaluation = {
                    "trigger_key": trigger_key,
                    "event_type": None,
                    "operator": trigger_config.get('operator'),
                    "value": trigger_config.get('value'),
                    "condition_result": False,
                    "condition_description": None,
                    "error": None
                }
                
                # Parse trigger configuration
                event_type_str = trigger_config.get('event_type')
                if not event_type_str:
                    logger.warning(f"No event_type specified for trigger {trigger_key}")
                    condition_evaluation["error"] = "No event_type specified"
                    condition_evaluation["condition_description"] = "Invalid trigger configuration"
                    rule_evaluation["conditions"].append(condition_evaluation)
                    continue
                
                try:
                    event_type = ExpertEventType(event_type_str)
                    condition_evaluation["event_type"] = event_type_str
                except ValueError:
                    logger.error(f"Invalid event type: {event_type_str}")
                    condition_evaluation["error"] = f"Invalid event type: {event_type_str}"
                    condition_evaluation["condition_description"] = "Invalid event type"
                    rule_evaluation["conditions"].append(condition_evaluation)
                    continue
                
                # Create condition instance
                condition = self._create_condition_from_trigger(
                    event_type, trigger_config, instrument_name, 
                    expert_recommendation, existing_order
                )
                
                if not condition:
                    logger.warning(f"Could not create condition for trigger {trigger_key}")
                    condition_evaluation["error"] = "Could not create condition"
                    condition_evaluation["condition_description"] = "Condition creation failed"
                    rule_evaluation["conditions"].append(condition_evaluation)
                    rule_evaluation["all_conditions_met"] = False
                    continue
                
                # Get condition description
                condition_evaluation["condition_description"] = condition.get_description()
                
                # Evaluate condition
                condition_result = condition.evaluate()
                condition_evaluation["condition_result"] = condition_result
                
                logger.debug(f"Condition description: {condition.get_description()}")
                logger.debug(f"Condition {trigger_key} result: {condition_result}")
                
                rule_evaluation["conditions"].append(condition_evaluation)
                self.condition_evaluations.append(condition_evaluation.copy())
                
                if not condition_result:
                    logger.debug(f"Condition {trigger_key} not met, stopping evaluation")
                    rule_evaluation["all_conditions_met"] = False
                    self.rule_evaluations.append(rule_evaluation)
                    return False
            
            logger.debug(f"All conditions met for event action {event_action.name}")
            rule_evaluation["executed"] = True
            self.rule_evaluations.append(rule_evaluation)
            return True
            
        except Exception as e:
            logger.error(f"Error evaluating conditions for event action {event_action.name}: {e}", exc_info=True)
            # Add error to rule evaluation
            rule_evaluation = {
                "rule_name": event_action.name,
                "rule_id": event_action.id,
                "conditions": [],
                "all_conditions_met": False,
                "executed": False,
                "error": str(e)
            }
            self.rule_evaluations.append(rule_evaluation)
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
    
    def _create_and_store_trade_actions(self, event_action: EventAction, instrument_name: str,
                                      expert_recommendation: ExpertRecommendation,
                                      existing_order: Optional[TradingOrder]) -> List[Dict[str, Any]]:
        """
        Create TradeAction objects for an event action and store them for later execution.
        
        Args:
            event_action: The event action containing actions to create
            instrument_name: Instrument name
            expert_recommendation: Expert recommendation
            existing_order: Optional existing order
            
        Returns:
            List of action summaries
        """
        action_summaries = []
        
        try:
            actions = event_action.actions
            if not actions:
                logger.debug(f"No actions defined for event action {event_action.name}")
                return action_summaries
            
            # Create order recommendation from expert recommendation
            # Use recommended_action which is the correct attribute name in ExpertRecommendation model
            # The recommended_action is already an OrderRecommendation enum, so use it directly
            order_recommendation = expert_recommendation.recommended_action
            
            # Process each action
            for action_key, action_config in actions.items():
                logger.debug(f"Creating TradeAction: {action_key}")
                
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
                
                # Create TradeAction instance
                trade_action = self._create_trade_action(
                    action_type, action_config, instrument_name,
                    order_recommendation, existing_order, expert_recommendation
                )
                
                if trade_action:
                    # Store the action for later execution
                    self.trade_actions.append(trade_action)
                    
                    # Create summary for display
                    action_summary = {
                        "action_type": action_type,
                        "description": trade_action.get_description()
                    }
                    
                    action_summaries.append(action_summary)
                    logger.info(f"Created and stored TradeAction: {action_type.value} for {instrument_name}")
                else:
                    logger.warning(f"Failed to create TradeAction for {action_type}")
                    action_summaries.append({
                        "error": f"Failed to create TradeAction for {action_type}",
                        "description": f"Action creation failed for {action_type_str}"
                    })
            
        except Exception as e:
            logger.error(f"Error creating trade actions for event action {event_action.name}: {e}", exc_info=True)
            action_summaries.append({
                "error": f"Error creating trade actions: {str(e)}",
                "description": "Action creation failed due to error"
            })
        
        return action_summaries
    
    def _create_trade_action(self, action_type: ExpertActionType, action_config: Dict[str, Any],
                           instrument_name: str, order_recommendation: OrderRecommendation,
                           existing_order: Optional[TradingOrder],
                           expert_recommendation: ExpertRecommendation) -> Optional[TradeAction]:
        """
        Create a TradeAction instance from action configuration.
        
        Args:
            action_type: Type of action to create
            action_config: Configuration for the action
            instrument_name: Instrument name
            order_recommendation: Order recommendation
            existing_order: Optional existing order
            expert_recommendation: Expert recommendation for linking
            
        Returns:
            TradeAction instance or None if creation failed
        """
        try:
            # Extract additional parameters for specific action types
            kwargs = {}
            
            if action_type == ExpertActionType.ADJUST_TAKE_PROFIT:
                # Extract reference_value and percent (value) for TP calculation
                kwargs['reference_value'] = action_config.get('reference_value')
                kwargs['percent'] = action_config.get('value')  # 'value' in config is the percentage
                # Also allow direct take_profit_price if provided
                kwargs['take_profit_price'] = action_config.get('take_profit_price')
            elif action_type == ExpertActionType.ADJUST_STOP_LOSS:
                # Extract reference_value and percent (value) for SL calculation
                kwargs['reference_value'] = action_config.get('reference_value')
                kwargs['percent'] = action_config.get('value')  # 'value' in config is the percentage
                # Also allow direct stop_loss_price if provided
                kwargs['stop_loss_price'] = action_config.get('stop_loss_price')
            
            # Create action using factory function, passing expert_recommendation
            action = create_action(
                action_type=action_type,
                instrument_name=instrument_name,
                account=self.account,
                order_recommendation=order_recommendation,
                existing_order=existing_order,
                expert_recommendation=expert_recommendation,
                **kwargs
            )
            
            return action
            
        except Exception as e:
            logger.error(f"Error creating trade action for {action_type}: {e}", exc_info=True)
            return None
    
    def _get_action_type_from_action(self, action: TradeAction) -> Optional[ExpertActionType]:
        """
        Get the ExpertActionType from a TradeAction instance.
        
        Args:
            action: TradeAction instance
            
        Returns:
            Corresponding ExpertActionType or None
        """
        try:
            # Map action classes to action types
            action_type_map = {
                'BuyAction': ExpertActionType.BUY,
                'SellAction': ExpertActionType.SELL,
                'CloseAction': ExpertActionType.CLOSE,
                'AdjustTakeProfitAction': ExpertActionType.ADJUST_TAKE_PROFIT,
                'AdjustStopLossAction': ExpertActionType.ADJUST_STOP_LOSS,
            }
            
            class_name = action.__class__.__name__
            return action_type_map.get(class_name)
            
        except Exception as e:
            logger.error(f"Error getting action type from action: {e}", exc_info=True)
            return None
    
    def _sort_actions_by_priority(self, actions: List[TradeAction]) -> List[TradeAction]:
        """
        Sort actions by execution priority to ensure order-creating actions execute first.
        
        Priority order (lower number = higher priority):
        1. BUY, SELL (create orders)
        2. CLOSE (closes positions)
        3. ADJUST_TAKE_PROFIT, ADJUST_STOP_LOSS (require existing orders)
        
        Args:
            actions: List of TradeAction objects to sort
            
        Returns:
            Sorted list of TradeAction objects
        """
        try:
            # Define priority levels for each action type
            priority_map = {
                ExpertActionType.BUY: 1,
                ExpertActionType.SELL: 1,
                ExpertActionType.CLOSE: 2,
                ExpertActionType.ADJUST_TAKE_PROFIT: 3,
                ExpertActionType.ADJUST_STOP_LOSS: 3,
            }
            
            def get_priority(action: TradeAction) -> int:
                """Get priority value for an action (lower = higher priority)"""
                action_type = self._get_action_type_from_action(action)
                return priority_map.get(action_type, 99)  # Unknown actions go last
            
            # Sort by priority (stable sort preserves original order for equal priorities)
            sorted_actions = sorted(actions, key=get_priority)
            
            # Log the sorted order for debugging
            if sorted_actions:
                logger.info(f"Sorted {len(sorted_actions)} actions by priority:")
                for i, action in enumerate(sorted_actions):
                    action_type = self._get_action_type_from_action(action)
                    priority = get_priority(action)
                    logger.debug(f"  {i+1}. Priority {priority}: {action_type.value if action_type else 'Unknown'} - {action.get_description()}")
            
            return sorted_actions
            
        except Exception as e:
            logger.error(f"Error sorting actions by priority: {e}", exc_info=True)
            return actions  # Return unsorted list if sorting fails
    
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
            
            # Get event actions and their descriptions (custom order)
            with get_db() as session:
                from .models import RulesetEventActionLink
                statement = (
                    select(EventAction)
                    .join(RulesetEventActionLink, EventAction.id == RulesetEventActionLink.eventaction_id)
                    .where(RulesetEventActionLink.ruleset_id == ruleset_id)
                    .order_by(RulesetEventActionLink.order_index)
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
    
    def get_evaluation_details(self) -> Dict[str, Any]:
        """
        Get detailed information about the last evaluation run.
        
        Returns:
            Dictionary containing:
            - condition_evaluations: List of individual condition results
            - rule_evaluations: List of rule-level results
            - summary: High-level summary of the evaluation
        """
        try:
            total_conditions = len(self.condition_evaluations)
            passed_conditions = sum(1 for cond in self.condition_evaluations if cond.get("condition_result", False))
            failed_conditions = total_conditions - passed_conditions
            
            total_rules = len(self.rule_evaluations)
            executed_rules = sum(1 for rule in self.rule_evaluations if rule.get("executed", False))
            
            summary = {
                "total_conditions": total_conditions,
                "passed_conditions": passed_conditions,
                "failed_conditions": failed_conditions,
                "total_rules": total_rules,
                "executed_rules": executed_rules,
                "rules_with_all_conditions_met": sum(1 for rule in self.rule_evaluations if rule.get("all_conditions_met", False))
            }
            
            return {
                "condition_evaluations": self.condition_evaluations.copy(),
                "rule_evaluations": self.rule_evaluations.copy(),
                "summary": summary
            }
            
        except Exception as e:
            logger.error(f"Error getting evaluation details: {e}", exc_info=True)
            return {
                "condition_evaluations": [],
                "rule_evaluations": [],
                "summary": {"error": str(e)}
            }
