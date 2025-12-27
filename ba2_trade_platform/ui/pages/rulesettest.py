"""
Ruleset Test Page - Test rulesets against existing expert recommendations

This module provides functionality to test trading rulesets against historical
expert recommendations to evaluate their effectiveness and behavior.
"""

from nicegui import ui
from typing import List, Dict, Any, Optional
from sqlmodel import select
from datetime import datetime, timezone

from ...core.models import Ruleset, ExpertRecommendation, ExpertInstance, AccountDefinition, TradingOrder
from ...core.TradeActionEvaluator import TradeActionEvaluator
from ...core.types import OrderRecommendation, AnalysisUseCase
from ...core.db import get_db, get_all_instances, get_instance
from ...modules.accounts.AlpacaAccount import AlpacaAccount
from ...logger import logger
from ..components.RuleEvaluationDisplay import render_rule_evaluations


class RulesetTestTab:
    """UI component for testing rulesets against expert recommendations."""
    
    def __init__(self, initial_ruleset_id: Optional[int] = None, market_analysis_id: Optional[int] = None):
        self.initial_ruleset_id = initial_ruleset_id
        self.market_analysis_id = market_analysis_id
        self.ruleset_select = None
        self.account_select = None
        self.expert_select = None
        # Test recommendation input fields
        self.symbol_input = None
        self.action_select = None
        self.profit_input = None
        self.confidence_input = None
        self.risk_select = None
        self.time_horizon_select = None
        self.results_container = None
        self.evaluation_results = []
        self.evaluate_all_conditions_checkbox = None
        self.force_generate_actions_checkbox = None
        
        # If no initial ruleset_id or market_analysis_id provided, try to extract from URL
        if self.initial_ruleset_id is None or self.market_analysis_id is None:
            self._extract_params_from_url()
        
        self.render()
    
    def _extract_params_from_url(self):
        """Extract ruleset_id and market_analysis_id from URL query parameters."""
        try:
            # Use a simpler approach: get query params via JavaScript but synchronously
            async def get_url_params():
                try:
                    # Get ruleset_id
                    ruleset_result = await ui.run_javascript(
                        "new URLSearchParams(window.location.search).get('ruleset_id')"
                    )
                    if ruleset_result:
                        self.initial_ruleset_id = int(ruleset_result)
                        if self.ruleset_select:
                            self.ruleset_select.value = self.initial_ruleset_id
                            self._on_ruleset_change()
                    
                    # Get market_analysis_id
                    ma_result = await ui.run_javascript(
                        "new URLSearchParams(window.location.search).get('market_analysis_id')"
                    )
                    if ma_result:
                        self.market_analysis_id = int(ma_result)
                        self._load_market_analysis_parameters()
                        
                except (ValueError, TypeError) as e:
                    logger.debug(f"Could not parse parameters from URL: {e}")
            
            # Schedule the parameter extraction
            ui.timer(0.1, get_url_params, once=True)
                
        except Exception as e:
            logger.debug(f"Error extracting URL parameters: {e}")
    
    def render(self):
        """Render the ruleset test interface."""
        try:
            # Use a container that uses full width but respects the left drawer
            with ui.element('div').classes('w-full').style('width: calc(100vw - 400px); margin-left: 0; padding: 1rem;'):
                ui.label('🧪 Ruleset Testing').classes('text-h5 mb-4')
                ui.label('Test rulesets against existing expert recommendations to evaluate their effectiveness.').classes('text-grey-7 mb-6')
                
                # Test parameters section
                with ui.row().classes('w-full gap-6 mb-6'):
                    with ui.card().classes('flex-1 p-4'):
                        ui.label('Test Parameters').classes('text-h6 mb-4')
                        
                        # Ruleset selection
                        ui.label('Select Ruleset:').classes('text-sm font-medium mb-1')
                        self.ruleset_select = ui.select(
                            options=self._get_ruleset_options(),
                            value=self.initial_ruleset_id,
                            with_input=True,
                            on_change=self._on_ruleset_change
                        ).classes('w-full mb-3')
                        
                        # Account selection
                        ui.label('Select Account:').classes('text-sm font-medium mb-1')
                        self.account_select = ui.select(
                            options=self._get_account_options(),
                            with_input=True,
                            on_change=self._on_account_change
                        ).classes('w-full mb-3')
                        
                        # Expert selection
                        ui.label('Select Expert Instance:').classes('text-sm font-medium mb-1')
                        self.expert_select = ui.select(
                            options=self._get_expert_options(),
                            with_input=True,
                            on_change=self._on_expert_change
                        ).classes('w-full mb-3')
                        
                        # Test recommendation parameters
                        ui.label('Test Recommendation Parameters').classes('text-sm font-medium mb-2 mt-4')
                        
                        # Symbol input
                        ui.label('Symbol:').classes('text-sm mb-1')
                        self.symbol_input = ui.input(
                            placeholder='e.g., AAPL',
                            value='AAPL'
                        ).classes('w-full mb-2')
                        
                        # Recommended Action
                        ui.label('Recommended Action:').classes('text-sm mb-1')
                        from ...core.types import OrderRecommendation
                        self.action_select = ui.select(
                            options={rec.value: rec.value for rec in OrderRecommendation},
                            value=OrderRecommendation.BUY.value
                        ).classes('w-full mb-2')
                        
                        # Expected profit percent
                        ui.label('Expected Profit Percent:').classes('text-sm mb-1')
                        self.profit_input = ui.number(
                            value=20.0,
                            format='%.1f',
                            suffix='%'
                        ).classes('w-full mb-2')
                        
                        # Confidence
                        ui.label('Confidence:').classes('text-sm mb-1')
                        self.confidence_input = ui.number(
                            value=80.0,
                            format='%.1f',
                            suffix='%',
                            min=0,
                            max=100
                        ).classes('w-full mb-2')
                        
                        # Risk Level
                        ui.label('Risk Level:').classes('text-sm mb-1')
                        from ...core.types import RiskLevel
                        self.risk_select = ui.select(
                            options={level.value: level.value for level in RiskLevel},
                            value=RiskLevel.MEDIUM.value
                        ).classes('w-full mb-2')
                        
                        # Time Horizon
                        ui.label('Time Horizon:').classes('text-sm mb-1')
                        from ...core.types import TimeHorizon
                        self.time_horizon_select = ui.select(
                            options={horizon.value: horizon.value for horizon in TimeHorizon},
                            value=TimeHorizon.MEDIUM_TERM.value
                        ).classes('w-full mb-4')
                        
                        # Debug option: Evaluate all conditions
                        ui.separator().classes('my-4')
                        ui.label('Debug Options:').classes('text-sm font-medium mb-2')
                        self.evaluate_all_conditions_checkbox = ui.checkbox(
                            'Evaluate all conditions (don\'t stop at first failure)',
                            value=False
                        ).classes('mb-2')
                        ui.label('Enable this to see all condition results, even after the first failure.').classes('text-xs text-grey-6 mb-3')
                        
                        self.force_generate_actions_checkbox = ui.checkbox(
                            'Force generate actions (even if conditions fail)',
                            value=False
                        ).classes('mb-2')
                        ui.label('Enable this to see what actions would be generated, regardless of condition results.').classes('text-xs text-grey-6 mb-4')
                        
                        # Test button
                        ui.button(
                            'Run Test',
                            on_click=self._run_test,
                            icon='play_arrow'
                        ).classes('bg-primary text-white px-6 py-2')
                    
                    with ui.card().classes('flex-1 p-4'):
                        ui.label('Ruleset Description').classes('text-h6 mb-4')
                        with ui.element('div').classes('w-full h-80 p-4 rounded border overflow-auto').style('background: rgba(26, 31, 46, 0.8); border-color: rgba(255, 255, 255, 0.1);'):
                            with ui.element('pre').classes('text-sm whitespace-pre-wrap').style('color: #a0aec0;'):
                                self.ruleset_description = ui.label('Select a ruleset to see its description.')
                
                # Trigger initial description load if ruleset was pre-selected
                # Note: Don't trigger here if initial_ruleset_id is None, as the async URL extraction will handle it
                if self.initial_ruleset_id and self.initial_ruleset_id in self._get_ruleset_options():
                    ui.timer(0.1, self._on_ruleset_change, once=True)
                
                # Results section
                ui.separator().classes('my-6')
                
                ui.label('Test Results').classes('text-h6 mb-4')
                self.results_container = ui.column().classes('w-full max-w-none')
                
                with self.results_container:
                    ui.label('No test results yet. Select parameters and click "Run Test" to begin.').classes('text-grey-5 text-center py-8')
                
        except Exception as e:
            logger.error(f"Error rendering ruleset test tab: {e}", exc_info=True)
            ui.label(f'Error rendering test interface: {str(e)}').classes('text-red-500')
    
    def _load_market_analysis_parameters(self):
        """Load test parameters from a market analysis result."""
        try:
            if not self.market_analysis_id:
                return
            
            from ...core.models import MarketAnalysis, ExpertRecommendation
            
            # Get the market analysis
            analysis = get_instance(MarketAnalysis, self.market_analysis_id)
            if not analysis:
                logger.warning(f"Market analysis {self.market_analysis_id} not found")
                ui.notify(f"Market analysis {self.market_analysis_id} not found", type='warning')
                return
            
            # Load parameters from analysis
            if self.symbol_input:
                self.symbol_input.value = analysis.symbol
            
            # Set expert instance from analysis
            if self.expert_select and analysis.expert_instance_id:
                self.expert_select.value = analysis.expert_instance_id
                self._on_expert_change()
            
            # Get the expert instance to find the account
            if analysis.expert_instance_id:
                expert_instance = get_instance(ExpertInstance, analysis.expert_instance_id)
                if expert_instance and self.account_select:
                    self.account_select.value = expert_instance.account_id
                    self._on_account_change()
                    
                    # Get the ruleset based on analysis subtype
                    # ENTER_MARKET uses enter_market_ruleset_id, OPEN_POSITIONS uses open_positions_ruleset_id
                    from ...core.types import AnalysisUseCase
                    if analysis.subtype == AnalysisUseCase.ENTER_MARKET:
                        ruleset_id = expert_instance.enter_market_ruleset_id
                    elif analysis.subtype == AnalysisUseCase.OPEN_POSITIONS:
                        ruleset_id = expert_instance.open_positions_ruleset_id
                    else:
                        ruleset_id = expert_instance.enter_market_ruleset_id  # Default to enter market
                    
                    if ruleset_id and self.ruleset_select:
                        self.ruleset_select.value = ruleset_id
                        self._on_ruleset_change()
            
            # Try to get recommendation data from the analysis
            with get_db() as session:
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.market_analysis_id == self.market_analysis_id
                ).order_by(ExpertRecommendation.created_at.desc()).limit(1)
                
                recommendation = session.exec(statement).first()
                
                if recommendation:
                    # Load recommendation parameters
                    if self.action_select and hasattr(recommendation.recommended_action, 'value'):
                        self.action_select.value = recommendation.recommended_action.value
                    
                    if self.profit_input and recommendation.expected_profit_percent is not None:
                        self.profit_input.value = recommendation.expected_profit_percent
                    
                    if self.confidence_input and recommendation.confidence:
                        self.confidence_input.value = recommendation.confidence  # Already stored as 1-100
                    
                    if self.risk_select and recommendation.risk_level  is not None:
                        if hasattr(recommendation.risk_level, 'value'):
                            self.risk_select.value = recommendation.risk_level.value
                        else:
                            self.risk_select.value = str(recommendation.risk_level)
                    
                    if self.time_horizon_select and recommendation.time_horizon:
                        if hasattr(recommendation.time_horizon, 'value'):
                            self.time_horizon_select.value = recommendation.time_horizon.value
                        else:
                            self.time_horizon_select.value = str(recommendation.time_horizon)
                    
                    ui.notify(f"Loaded parameters from market analysis {self.market_analysis_id}", type='positive')
                    logger.info(f"Successfully loaded parameters from market analysis {self.market_analysis_id}")
                else:
                    ui.notify(f"No recommendation found for analysis {self.market_analysis_id}, using default parameters", type='info')
                    logger.debug(f"No recommendation found for market analysis {self.market_analysis_id}")
        
        except Exception as e:
            logger.error(f"Error loading market analysis parameters: {e}", exc_info=True)
            ui.notify(f"Error loading analysis parameters: {str(e)}", type='negative')
    
    def _get_ruleset_options(self) -> Dict[int, str]:
        """Get available rulesets for selection."""
        try:
            rulesets = get_all_instances(Ruleset)
            options = {}
            for ruleset in rulesets:
                label = f"{ruleset.name} ({ruleset.type.value})" if ruleset.type else ruleset.name
                options[ruleset.id] = label
            return options
        except Exception as e:
            logger.error(f"Error getting ruleset options: {e}", exc_info=True)
            return {}
    
    def _get_account_options(self) -> Dict[int, str]:
        """Get available accounts for selection."""
        try:
            accounts = get_all_instances(AccountDefinition)
            options = {}
            for account in accounts:
                label = f"{account.name} ({account.provider})"
                options[account.id] = label
            return options
        except Exception as e:
            logger.error(f"Error getting account options: {e}", exc_info=True)
            return {}
    
    def _get_expert_options(self) -> Dict[int, str]:
        """Get available expert instances for selection."""
        try:
            experts = get_all_instances(ExpertInstance)
            options = {}
            for expert in experts:
                label = expert.user_description or f"{expert.expert} (ID: {expert.id})"
                options[expert.id] = label
            return options
        except Exception as e:
            logger.error(f"Error getting expert options: {e}", exc_info=True)
            return {}
    
    def _on_ruleset_change(self):
        """Handle ruleset selection change."""
        try:
            if self.ruleset_select.value:
                # Update ruleset description
                evaluator = self._create_evaluator()
                if evaluator:
                    description = evaluator.get_ruleset_description(self.ruleset_select.value)
                    self.ruleset_description.text = description or "No description available."
                else:
                    self.ruleset_description.text = "Cannot load description without account selection."
        except Exception as e:
            logger.error(f"Error handling ruleset change: {e}", exc_info=True)

    def _on_account_change(self):
        """Handle account selection change."""
        try:
            # Update ruleset description if ruleset is selected
            if self.ruleset_select.value:
                self._on_ruleset_change()
        except Exception as e:
            logger.error(f"Error handling account change: {e}", exc_info=True)
    
    def _on_expert_change(self):
        """Handle expert selection change."""
        try:
            if self.expert_select.value:
                logger.debug(f"Expert selection changed to: {self.expert_select.value}")
                # No need to update recommendations since we're using manual input fields
        except Exception as e:
            logger.error(f"Error handling expert change: {e}", exc_info=True)
    
    def _create_evaluator(self) -> Optional[TradeActionEvaluator]:
        """Create a TradeActionEvaluator instance."""
        try:
            if not self.account_select.value:
                return None
            
            # Get the debug flags from checkboxes
            evaluate_all = self.evaluate_all_conditions_checkbox.value if self.evaluate_all_conditions_checkbox else False
            force_actions = self.force_generate_actions_checkbox.value if self.force_generate_actions_checkbox else False
            
            # Create account interface
            account = AlpacaAccount(self.account_select.value)
            evaluator = TradeActionEvaluator(
                account, 
                evaluate_all_conditions=evaluate_all,
                force_generate_actions=force_actions
            )
            return evaluator
        except Exception as e:
            logger.error(f"Error creating evaluator: {e}", exc_info=True)
            return None
    
    def _run_test(self):
        """Run the ruleset test with selected parameters."""
        try:
            # Validate selections
            if not all([
                self.ruleset_select.value,
                self.account_select.value,
                self.expert_select.value,
                self.symbol_input.value,
                self.action_select.value is not None,
                self.profit_input.value is not None,
                self.confidence_input.value is not None
            ]):
                ui.notify("Please fill in all required parameters", type='warning')
                return
            
            # Get values from selections and inputs
            ruleset_id = self.ruleset_select.value
            
            # Create test recommendation object (not saved to DB)
            from ...core.types import OrderRecommendation, RiskLevel, TimeHorizon
            test_recommendation = type('TestRecommendation', (), {
                'id': None,  # No ID since it's not in DB
                'symbol': self.symbol_input.value.upper().strip(),
                'recommended_action': OrderRecommendation(self.action_select.value),
                'expected_profit_percent': self.profit_input.value,
                'confidence': self.confidence_input.value,
                'risk_level': RiskLevel(self.risk_select.value),
                'time_horizon': TimeHorizon(self.time_horizon_select.value),
                'created_at': datetime.now(),
                'instance_id': self.expert_select.value,
                'price_at_date': 100.0,  # Default price for testing
                'details': 'Test recommendation for ruleset evaluation',
                'market_analysis_id': None
            })()
            
            recommendation = test_recommendation
            
            # Create evaluator
            evaluator = self._create_evaluator()
            if not evaluator:
                ui.notify("Failed to create evaluator", type='error')
                return
            
            # Check for existing orders (optional - for more realistic testing)
            existing_order = self._get_existing_order(recommendation.symbol, recommendation.instance_id)
            
            # Run evaluation
            ui.notify("Running ruleset evaluation...", type='info')
            
            results = evaluator.evaluate(
                instrument_name=recommendation.symbol,
                expert_recommendation=recommendation,
                ruleset_id=ruleset_id,
                existing_order=existing_order
            )
            
            # Get detailed evaluation results
            evaluation_details = evaluator.get_evaluation_details()
            
            # Display results
            self._display_results(recommendation, results, existing_order, evaluation_details)
            
            ui.notify(f"Test completed! Found {len(results)} action(s)", type='positive')
            
        except Exception as e:
            logger.error(f"Error running test: {e}", exc_info=True)
            ui.notify(f"Error running test: {str(e)}", type='negative')
    
    def _get_existing_order(self, symbol: str, expert_instance_id: int) -> Optional[TradingOrder]:
        """Get an existing order for the symbol (for more realistic testing)."""
        try:
            with get_db() as session:
                statement = (
                    select(TradingOrder)
                    .where(TradingOrder.symbol == symbol)
                    .order_by(TradingOrder.created_at.desc())
                    .limit(1)
                )
                order = session.exec(statement).first()
                return order
        except Exception as e:
            logger.error(f"Error getting existing order: {e}", exc_info=True)
            return None
    
    def _display_results(self, recommendation: ExpertRecommendation, results: List[Dict[str, Any]], existing_order: Optional[TradingOrder], evaluation_details: Dict[str, Any]):
        """Display the test results including detailed condition evaluation."""
        try:
            self.results_container.clear()
            
            with self.results_container:
                # Test summary
                with ui.card().classes('w-full mb-4 p-4'):
                    ui.label('📊 Test Summary').classes('text-h6 mb-3')
                    
                    with ui.row().classes('w-full gap-6'):
                        with ui.column():
                            ui.label('Test Parameters:').classes('text-sm font-medium mb-2')
                            ui.label(f'Symbol: {recommendation.symbol}').classes('text-sm')
                            ui.label(f'Recommendation: {recommendation.recommended_action.value}').classes('text-sm')
                            ui.label(f'Confidence: {recommendation.confidence:.1f}%' if recommendation.confidence else 'Confidence: N/A').classes('text-sm')
                            ui.label(f'Created: {recommendation.created_at.strftime("%Y-%m-%d %H:%M")}' if recommendation.created_at else 'Created: Unknown').classes('text-sm')
                        
                        with ui.column():
                            ui.label('Test Results:').classes('text-sm font-medium mb-2')
                            ui.label(f'Actions Triggered: {len(results)}').classes('text-sm')
                            # Action definitions don't have success/failure status until executed
                            ui.label(f'Action Definitions Found: {len(results)}').classes('text-sm')
                            error_results = sum(1 for r in results if 'error' in r)
                            ui.label(f'Errors: {error_results}').classes('text-sm')
                            ui.label(f'Existing Order: {"Yes" if existing_order else "No"}').classes('text-sm')
                        
                        with ui.column():
                            ui.label('Condition Analysis:').classes('text-sm font-medium mb-2')
                            summary = evaluation_details.get('summary', {})
                            ui.label(f'Total Rules: {summary.get("total_rules", 0)}').classes('text-sm')
                            ui.label(f'Rules Executed: {summary.get("executed_rules", 0)}').classes('text-sm')
                            ui.label(f'Total Conditions: {summary.get("total_conditions", 0)}').classes('text-sm')
                            ui.label(f'Conditions Passed: {summary.get("passed_conditions", 0)}').classes('text-sm')
                            ui.label(f'Conditions Failed: {summary.get("failed_conditions", 0)}').classes('text-sm')
                            
                            # Show debug mode status
                            debug_modes = []
                            if self.evaluate_all_conditions_checkbox and self.evaluate_all_conditions_checkbox.value:
                                debug_modes.append('Evaluate All')
                            if self.force_generate_actions_checkbox and self.force_generate_actions_checkbox.value:
                                debug_modes.append('Force Actions')
                            
                            if debug_modes:
                                with ui.row().classes('items-center gap-2 mt-2'):
                                    ui.icon('bug_report', size='sm').classes('text-orange-600')
                                    ui.label(f'Debug: {" + ".join(debug_modes)}').classes('text-xs text-orange-600 font-medium')
                
                # Rule and Condition Evaluation Details
                ui.separator().classes('my-4')
                
                # Use reusable component to display rule evaluations
                # Combine results and evaluation_details for the component
                evaluation_display_data = evaluation_details.copy()
                evaluation_display_data['actions'] = results
                render_rule_evaluations(evaluation_display_data, show_actions=True, compact=False)            
        except Exception as e:
            logger.error(f"Error displaying results: {e}", exc_info=True)
            with self.results_container:
                ui.label(f'Error displaying results: {str(e)}').classes('text-red-500')


def content(ruleset_id: Optional[int] = None, market_analysis_id: Optional[int] = None):
    """Render the ruleset test page content."""
    try:
        RulesetTestTab(initial_ruleset_id=ruleset_id, market_analysis_id=market_analysis_id)
    except Exception as e:
        logger.error(f"Error rendering ruleset test page: {e}", exc_info=True)
        ui.label(f'Error loading ruleset test page: {str(e)}').classes('text-red-500')