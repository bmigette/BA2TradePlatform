"""
DEPRECATED: This module is deprecated and should not be used.

The Final Summarization Agent in tradingagents.graph.summarization now handles
recommendation generation as the final step in the graph workflow.

This provides better integration and avoids duplication.
"""
import warnings
from langchain_core.prompts import ChatPromptTemplate
import json
from datetime import datetime
from typing import Dict, Any, Optional
from ...prompts import get_prompt
from ba2_trade_platform.core.types import OrderRecommendation, RiskLevel, TimeHorizon


def _get_error_recommendation(symbol: str, current_price: float, error_message: str, update_market_analysis_state=None) -> Dict[str, Any]:
    """
    Create a standardized error recommendation and optionally update MarketAnalysis state.
    
    Args:
        symbol: The trading symbol
        current_price: Current price of the asset
        error_message: The error message to include in details
        update_market_analysis_state: Optional function to update MarketAnalysis state with error
        
    Returns:
        Dict containing standardized error recommendation
    """
    error_recommendation = {
        "symbol": symbol,
        "recommended_action": OrderRecommendation.ERROR.value,
        "expected_profit_percent": 0.0,
        "price_at_date": current_price,
        "confidence": 0.0,
        "details": f"Error generating recommendation: {str(error_message)}",
        "risk_level": RiskLevel.HIGH.value,
        "time_horizon": TimeHorizon.SHORT_TERM.value,
        "key_factors": ["Analysis error occurred"],
        "stop_loss": 0.0,
        "take_profit": 0.0
    }
    
    # Update MarketAnalysis state with error if function provided
    if update_market_analysis_state is not None:
        try:
            update_market_analysis_state({"error": str(error_message), "error_timestamp": datetime.now().isoformat()})
        except Exception as state_error:
            # Don't let state update errors interfere with returning the error recommendation
            pass
    
    return error_recommendation


def create_recommendation_agent(llm):
    """
    DEPRECATED: Use Final Summarization Agent in graph workflow instead.
    
    This function is kept for backward compatibility but should not be used
    in new code. The graph's Final Summarization Agent provides the same
    functionality with better integration.
    """
    warnings.warn(
        "create_recommendation_agent is deprecated. Use Final Summarization Agent in graph workflow instead.",
        DeprecationWarning,
        stacklevel=2
    )
    """
    Create an AI agent that generates expert recommendations in JSON format
    based on the complete trading analysis state.
    """
    
    def recommendation_agent_node(state: Dict[str, Any], symbol: str, current_price: float = 0.0):
        """
        AI agent that synthesizes all analysis reports into a structured recommendation
        
        Args:
            state: Complete analysis state from TradingAgents
            symbol: Stock symbol being analyzed
            current_price: Current stock price
            
        Returns:
            Dict containing structured recommendation
        """
        from ... import logger as ta_logger
        
        ta_logger.debug(f"Starting recommendation synthesis for {symbol} at ${current_price:.2f}")
        
        # Get the recommendation prompt
        system_prompt = get_prompt("recommendation_agent")
        
        # Prepare context from all analysis reports
        context_data = {
            "symbol": symbol,
            "current_price": current_price,
            "trade_date": state.get("trade_date", datetime.now().strftime("%Y-%m-%d")),
            "market_report": state.get("market_report", "No market analysis available"),
            "news_report": state.get("news_report", "No news analysis available"),
            "fundamentals_report": state.get("fundamentals_report", "No fundamentals analysis available"),
            "sentiment_report": state.get("sentiment_report", "No sentiment analysis available"),
            "macro_report": state.get("macro_report", "No macro analysis available"),
            "final_trade_decision": state.get("final_trade_decision", "No final decision available"),
            "investment_plan": state.get("investment_plan", "No investment plan available")
        }
        
        # Add investment debate information if available
        if "investment_debate_state" in state:
            debate_state = state["investment_debate_state"]
            context_data.update({
                "bull_arguments": debate_state.get("bull_history", []),
                "bear_arguments": debate_state.get("bear_history", []),
                "judge_decision": debate_state.get("judge_decision", "No judge decision available")
            })
        
        # Add risk analysis information if available
        if "risk_debate_state" in state:
            risk_state = state["risk_debate_state"]
            context_data.update({
                "risk_analysis": risk_state.get("judge_decision", "No risk analysis available")
            })
        
        # Format the analysis context for the LLM
        analysis_context = f"""
SYMBOL: {context_data['symbol']}
CURRENT PRICE: ${context_data['current_price']:.2f}
ANALYSIS DATE: {context_data['trade_date']}

=== MARKET ANALYSIS ===
{context_data['market_report']}

=== NEWS ANALYSIS ===
{context_data['news_report']}

=== FUNDAMENTALS ANALYSIS ===
{context_data['fundamentals_report']}

=== SENTIMENT ANALYSIS ===
{context_data['sentiment_report']}

=== MACRO ECONOMIC ANALYSIS ===
{context_data['macro_report']}

=== INVESTMENT DEBATE SUMMARY ===
Bull Arguments: {context_data.get('bull_arguments', 'None')}
Bear Arguments: {context_data.get('bear_arguments', 'None')}
Judge Decision: {context_data.get('judge_decision', 'None')}

=== RISK ANALYSIS ===
{context_data.get('risk_analysis', 'None')}

=== FINAL TRADE DECISION ===
{context_data['final_trade_decision']}

=== INVESTMENT PLAN ===
{context_data['investment_plan']}
"""
        
        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", "Please analyze the following comprehensive trading analysis and generate a structured JSON recommendation:\n\n{analysis_context}")
        ])
        
        # Generate the recommendation using the LLM
        chain = prompt | llm
        
        try:
            ta_logger.debug(f"Invoking LLM chain for recommendation generation for {symbol}")
            result = chain.invoke({"analysis_context": analysis_context})
            
            # Parse the JSON response
            if hasattr(result, 'content'):
                response_text = result.content
            else:
                response_text = str(result)
            
            ta_logger.debug(f"LLM response for {symbol} (first 200 chars): {response_text[:200]}...")
            
            # Try to extract JSON from the response
            try:
                # Look for JSON in the response
                start_idx = response_text.find('{')
                end_idx = response_text.rfind('}') + 1
                
                if start_idx != -1 and end_idx > start_idx:
                    json_str = response_text[start_idx:end_idx]
                    recommendation = json.loads(json_str)
                    ta_logger.debug(f"Successfully parsed JSON recommendation for {symbol}")
                else:
                    # Fallback: create structured response from text
                    ta_logger.warning(f"No valid JSON found in LLM response for {symbol}, using fallback")
                    recommendation = _create_fallback_recommendation(response_text, symbol, current_price)
                    
            except json.JSONDecodeError as e:
                # Create fallback recommendation
                ta_logger.warning(f"JSON decode error for {symbol}: {e}, using fallback")
                recommendation = _create_fallback_recommendation(response_text, symbol, current_price)
            
            # Ensure all required fields are present
            recommendation = _validate_and_complete_recommendation(recommendation, symbol, current_price)
            
            ta_logger.info(f"Final recommendation for {symbol}: {recommendation['recommended_action']} with {recommendation['confidence']:.1f}% confidence")
            
            return recommendation
            
        except Exception as e:
            # Return error recommendation
            ta_logger.error(f"Error generating recommendation for {symbol}: {e}")
            return _get_error_recommendation(symbol, current_price, str(e))
    
    return recommendation_agent_node


def _create_fallback_recommendation(response_text: str, symbol: str, current_price: float) -> Dict[str, Any]:
    """Create a fallback recommendation when JSON parsing fails"""
    

    
    fallback_details = response_text[:500] + "..." if len(response_text) > 500 else response_text
    return _get_error_recommendation(symbol, current_price, f"JSON parsing failed: {fallback_details}")


def _validate_and_complete_recommendation(recommendation: Dict[str, Any], symbol: str, current_price: float) -> Dict[str, Any]:
    """Ensure the recommendation has all required fields with valid values"""
    
    # Use error recommendation as defaults
    defaults = _get_error_recommendation(symbol, current_price, "No detailed analysis available")
    
    # Fill in missing fields
    for key, default_value in defaults.items():
        if key not in recommendation or recommendation[key] is None:
            recommendation[key] = default_value
    
    # Validate action
    valid_actions = [OrderRecommendation.BUY.value, OrderRecommendation.SELL.value, OrderRecommendation.HOLD.value]
    if recommendation["recommended_action"] not in valid_actions:
        recommendation["recommended_action"] = OrderRecommendation.HOLD.value # use error
    
    # Validate confidence range
    confidence = recommendation["confidence"]
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 100:
        recommendation["confidence"] = 50.0
    
    # Validate risk level
    valid_risk_levels = [RiskLevel.LOW.value, RiskLevel.MEDIUM.value, RiskLevel.HIGH.value]
    if recommendation["risk_level"] not in valid_risk_levels:
        recommendation["risk_level"] = RiskLevel.MEDIUM.value
    
    # Validate time horizon
    valid_time_horizons = [TimeHorizon.SHORT_TERM.value, TimeHorizon.MEDIUM_TERM.value, TimeHorizon.LONG_TERM.value]
    if recommendation["time_horizon"] not in valid_time_horizons:
        recommendation["time_horizon"] = TimeHorizon.MEDIUM_TERM.value
    
    # Ensure key_factors is a list
    if not isinstance(recommendation["key_factors"], list):
        recommendation["key_factors"] = [str(recommendation["key_factors"])]
    
    return recommendation