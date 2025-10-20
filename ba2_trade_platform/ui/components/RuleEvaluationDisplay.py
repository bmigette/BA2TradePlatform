"""
Reusable component for displaying rule evaluation results.

This component displays the detailed breakdown of rule and condition evaluations,
including operands, calculated values, and action configurations.
"""

from nicegui import ui
from typing import Dict, Any, List, Optional
from ...logger import logger
from ...core.types import get_action_type_display_label


def render_rule_evaluations(evaluation_details: Dict[str, Any], show_actions: bool = True, compact: bool = False):
    """
    Render rule evaluation details including conditions and actions.
    
    Args:
        evaluation_details: Dictionary containing rule_evaluations, condition_evaluations, and optionally actions
        show_actions: Whether to show action results (default True)
        compact: Whether to use compact display mode (default False)
    """
    try:
        rule_evaluations = evaluation_details.get('rule_evaluations', [])
        
        if not compact:
            ui.label('📋 Rule and Condition Evaluation Details').classes('text-h6 mb-3')
        
        if rule_evaluations:
            for rule_eval in rule_evaluations:
                _render_single_rule(rule_eval, compact)
        else:
            with ui.card().classes('w-full p-4 text-center bg-grey-50 border border-grey-200'):
                ui.label('No rule evaluation data available').classes('text-grey-600')
        
        # Show actions if requested and available
        if show_actions:
            actions = evaluation_details.get('actions', [])
            if actions:
                if not compact:
                    ui.separator().classes('my-4')
                    ui.label('🎯 Action Results').classes('text-h6 mb-3')
                _render_actions(actions, compact)
                
    except Exception as e:
        logger.error(f"Error rendering rule evaluations: {e}", exc_info=True)
        ui.label(f'Error displaying evaluation details: {str(e)}').classes('text-red-500')


def _render_single_rule(rule_eval: Dict[str, Any], compact: bool = False):
    """Render a single rule evaluation with its conditions."""
    rule_name = rule_eval.get('rule_name', 'Unknown Rule')
    all_conditions_met = rule_eval.get('all_conditions_met', False)
    executed = rule_eval.get('executed', False)
    conditions = rule_eval.get('conditions', [])
    rule_error = rule_eval.get('error')
    
    # Color coding based on rule status
    if rule_error:
        card_color = 'bg-red-100 border-red-300'
        status_color = 'bg-red-500'
        status_text = 'ERROR'
    elif executed:
        card_color = 'bg-green-100 border-green-300'
        status_color = 'bg-green-500'
        status_text = 'EXECUTED'
    elif all_conditions_met:
        card_color = 'bg-blue-100 border-blue-300'
        status_color = 'bg-blue-500'
        status_text = 'CONDITIONS MET'
    else:
        card_color = 'bg-orange-100 border-orange-300'
        status_color = 'bg-orange-500'
        status_text = 'CONDITIONS NOT MET'
    
    with ui.card().classes(f'w-full mb-3 border {card_color}'):
        with ui.row().classes('w-full items-center justify-between p-2'):
            with ui.column().classes('flex-1'):
                ui.label(f'Rule: {rule_name}').classes('font-medium' if not compact else 'text-sm font-medium')
                if rule_error:
                    ui.label(f'Error: {rule_error}').classes('text-sm text-red-600')
                else:
                    ui.label(f'Conditions: {len(conditions)} total').classes('text-sm text-grey-7')
            
            ui.badge(status_text).classes(f'{status_color} text-white px-2 py-1')
        
        # Show individual conditions
        if conditions:
            with ui.expansion('Condition Details', icon='list').classes('w-full'):
                for condition in conditions:
                    _render_condition(condition, compact)


def _render_condition(condition: Dict[str, Any], compact: bool = False):
    """Render a single condition with its operands and result."""
    trigger_key = condition.get('trigger_key', 'Unknown')
    event_type = condition.get('event_type', 'Unknown')
    operator = condition.get('operator', '')
    value = condition.get('value', '')
    reference_value = condition.get('reference_value', '')
    calculated_value = condition.get('calculated_value')
    left_operand = condition.get('left_operand')
    right_operand = condition.get('right_operand')
    result = condition.get('condition_result', False)
    description = condition.get('condition_description', 'No description')
    cond_error = condition.get('error')
    
    # Condition status styling
    if cond_error:
        cond_class = 'text-red-600'
        result_icon = '❌'
        result_text = 'ERROR'
    elif result:
        cond_class = 'text-green-600'
        result_icon = '✅'
        result_text = 'PASSED'
    else:
        cond_class = 'text-orange-600'
        result_icon = '❌'
        result_text = 'FAILED'
    
    with ui.row().classes('w-full items-center p-2 border-b border-grey-200'):
        ui.label(result_icon).classes('text-lg mr-2' if not compact else 'text-sm mr-1')
        with ui.column().classes('flex-1'):
            # Build condition label with operator and values
            condition_label = f'{trigger_key}: {event_type}'
            if operator and value is not None:
                condition_label += f' {operator} {value}'
            if reference_value:
                condition_label += f' (ref: {reference_value})'
            
            # Add calculated value if available
            if calculated_value is not None:
                condition_label += f' [actual: {calculated_value:.2f}]'
            
            # Add operands for better clarity
            if left_operand is not None and right_operand is not None:
                # Show operands for comparison conditions
                if operator:
                    condition_label += f' [{left_operand:.2f} {operator} {right_operand:.2f}]'
                else:
                    # Flag conditions (like new_target_higher)
                    condition_label += f' [current: ${left_operand:.2f}, target: ${right_operand:.2f}]'
            
            ui.label(condition_label).classes(f'font-medium {cond_class}' if not compact else f'text-sm {cond_class}')
            ui.label(description).classes('text-sm text-grey-6' if not compact else 'text-xs text-grey-6')
            if cond_error:
                ui.label(f'Error: {cond_error}').classes('text-sm text-red-500' if not compact else 'text-xs text-red-500')
        
        ui.badge(result_text).classes(f'text-white px-2 py-1 {"bg-green-500" if result else "bg-red-500" if cond_error else "bg-orange-500"}')


def _render_actions(actions: List[Dict[str, Any]], compact: bool = False):
    """Render action results with their configurations."""
    for i, action in enumerate(actions, 1):
        # Check if this is an error result
        is_error = 'error' in action
        card_color = 'bg-red-100 border-red-300' if is_error else 'bg-blue-100 border-blue-300'
        
        with ui.card().classes(f'w-full mb-3 border {card_color}'):
            with ui.row().classes('w-full items-center justify-between p-2'):
                with ui.column().classes('flex-1'):
                    if is_error:
                        ui.label(f'Error {i}: {action.get("error", "Unknown error")}').classes('font-medium text-red-600' if not compact else 'text-sm text-red-600')
                    else:
                        action_type = action.get('action_type', 'Unknown')
                        # Handle enum values
                        if hasattr(action_type, 'value'):
                            action_type = action_type.value
                        # Use user-friendly display label
                        action_display = get_action_type_display_label(action_type) if action_type != 'Unknown' else 'Unknown'
                        ui.label(f'Action {i}: {action_display}').classes('font-medium' if not compact else 'text-sm font-medium')
                        ui.label(action.get('description', 'No description')).classes('text-sm text-grey-7' if not compact else 'text-xs text-grey-7')
                        
                        # Display action-specific values (TP/SL, target_percent, etc.)
                        action_config = action.get('action_config', {})
                        if action_config:
                            params = _build_action_params(action_config)
                            if params:
                                ui.label(' | '.join(params)).classes('text-sm font-medium text-blue-700 mt-1' if not compact else 'text-xs text-blue-700 mt-1')
                
                # Status badge
                if is_error:
                    ui.badge('ERROR').classes('bg-red-500 text-white px-2 py-1')
                else:
                    ui.badge('EXECUTED' if action.get('success') else 'READY').classes('bg-blue-500 text-white px-2 py-1')


def _build_action_params(action_config: Dict[str, Any]) -> List[str]:
    """Build list of action parameter strings for display."""
    params = []
    
    # For TP/SL actions, show calculation details
    if action_config.get('reference_type'):
        params.append(f"Ref: {action_config['reference_type']}")
    if action_config.get('reference_price') is not None:
        params.append(f"Ref Price: ${action_config['reference_price']:.2f}")
    if action_config.get('adjustment_percent') is not None:
        params.append(f"Adjust: {action_config['adjustment_percent']:+.2f}%")
    if action_config.get('calculated_price') is not None:
        params.append(f"→ ${action_config['calculated_price']:.2f}")
    
    # Legacy parameters
    if 'take_profit_percent' in action_config:
        params.append(f"TP: {action_config['take_profit_percent']}%")
    if 'stop_loss_percent' in action_config:
        params.append(f"SL: {action_config['stop_loss_percent']}%")
    if 'quantity_percent' in action_config:
        params.append(f"Qty: {action_config['quantity_percent']}%")
    if 'target_percent' in action_config:
        params.append(f"Target: {action_config['target_percent']}% of equity")
    if 'order_type' in action_config:
        order_type = action_config['order_type']
        if hasattr(order_type, 'value'):
            order_type = order_type.value
        params.append(f"Type: {order_type}")
    if 'limit_price' in action_config:
        params.append(f"Limit: ${action_config['limit_price']}")
    if 'stop_price' in action_config:
        params.append(f"Stop: ${action_config['stop_price']}")
    
    return params


def render_evaluation_summary(evaluation_details: Dict[str, Any]):
    """
    Render a compact summary of evaluation results.
    
    Args:
        evaluation_details: Dictionary containing evaluation summary
    """
    summary = evaluation_details.get('summary', {})
    
    if summary:
        with ui.row().classes('gap-4'):
            ui.label(f"Rules: {summary.get('total_rules', 0)}").classes('text-sm')
            ui.label(f"Executed: {summary.get('executed_rules', 0)}").classes('text-sm text-green-600')
            ui.label(f"Conditions: {summary.get('total_conditions', 0)}").classes('text-sm')
            ui.label(f"Passed: {summary.get('passed_conditions', 0)}").classes('text-sm text-green-600')
            ui.label(f"Failed: {summary.get('failed_conditions', 0)}").classes('text-sm text-orange-600')
