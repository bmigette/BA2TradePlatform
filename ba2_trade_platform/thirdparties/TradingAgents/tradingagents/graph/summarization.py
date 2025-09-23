"""
Final summarization agent for TradingAgents
Generates ExpertRecommendation-compatible JSON output
"""
import json
from typing import Dict, Any, Optional
from datetime import datetime


def create_expert_recommendation_summary(final_state: Dict[str, Any], symbol: str, current_price: float = None) -> Dict[str, Any]:
    """
    Create an ExpertRecommendation-compatible summary from TradingAgents final state
    
    Args:
        final_state: The final state from TradingAgents graph execution
        symbol: Stock symbol
        current_price: Current stock price
        
    Returns:
        Dictionary compatible with ExpertRecommendation model
    """
    
    # Extract the final trade decision
    final_decision = final_state.get("final_trade_decision", {})
    
    # Extract investment plan for additional context
    investment_plan = final_state.get("investment_plan", {})
    
    # Parse the decision to extract action
    decision_text = str(final_decision)
    
    # Determine recommended action based on decision
    recommended_action = "HOLD"  # Default
    confidence = 0.5  # Default confidence
    expected_profit_percent = 0.0  # Default
    
    # Parse decision text to extract action
    decision_lower = decision_text.lower()
    
    if "buy" in decision_lower or "long" in decision_lower:
        recommended_action = "BUY"
    elif "sell" in decision_lower or "short" in decision_lower:
        recommended_action = "SELL"
    elif "hold" in decision_lower:
        recommended_action = "HOLD"
    
    # Try to extract confidence from investment plan or other sources
    try:
        if isinstance(investment_plan, dict):
            # Look for confidence indicators in investment plan
            plan_text = str(investment_plan).lower()
            
            if "high confidence" in plan_text or "very confident" in plan_text:
                confidence = 0.8
            elif "moderate confidence" in plan_text or "confident" in plan_text:
                confidence = 0.6
            elif "low confidence" in plan_text:
                confidence = 0.3
        
        # Try to extract profit expectations
        # Look for percentage mentions in decision or plan
        import re
        
        combined_text = f"{decision_text} {investment_plan}"
        
        # Look for profit/return percentages
        profit_matches = re.findall(r'(\d+(?:\.\d+)?)%?\s*(?:profit|return|gain)', combined_text.lower())
        if profit_matches:
            expected_profit_percent = float(profit_matches[0])
        
        # Look for price targets
        price_matches = re.findall(r'\$?(\d+(?:\.\d+)?)', combined_text)
        if price_matches and current_price:
            target_price = float(price_matches[-1])  # Use last mentioned price as target
            if target_price > current_price:
                expected_profit_percent = ((target_price - current_price) / current_price) * 100
            elif target_price < current_price and recommended_action == "SELL":
                expected_profit_percent = ((current_price - target_price) / current_price) * 100
                
    except Exception as e:
        # Use a simple print as logger might not be initialized in this context
        pass  # Silent failure for profit parsing
    
    # Create detailed analysis summary
    details_parts = []
    
    # Add market analysis
    if "market_report" in final_state:
        details_parts.append(f"Market Analysis: {final_state['market_report'][:500]}...")
    
    # Add sentiment analysis
    if "sentiment_report" in final_state:
        details_parts.append(f"Sentiment Analysis: {final_state['sentiment_report'][:500]}...")
    
    # Add news analysis
    if "news_report" in final_state:
        details_parts.append(f"News Analysis: {final_state['news_report'][:500]}...")
    
    # Add fundamentals
    if "fundamentals_report" in final_state:
        details_parts.append(f"Fundamentals Analysis: {final_state['fundamentals_report'][:500]}...")
    
    # Add investment debate summary
    if "investment_debate_state" in final_state:
        debate_state = final_state["investment_debate_state"]
        if "judge_decision" in debate_state:
            details_parts.append(f"Investment Debate Decision: {debate_state['judge_decision'][:300]}...")
    
    # Add risk assessment
    if "risk_debate_state" in final_state:
        risk_state = final_state["risk_debate_state"]
        if "judge_decision" in risk_state:
            details_parts.append(f"Risk Assessment: {risk_state['judge_decision'][:300]}...")
    
    # Add final decision reasoning
    details_parts.append(f"Final Decision: {final_decision}")
    
    details = " | ".join(details_parts)
    
    # Ensure details is not too long for database storage
    if len(details) > 2000:
        details = details[:1997] + "..."
    
    # Create the recommendation
    recommendation = {
        "symbol": symbol,
        "recommended_action": recommended_action,
        "expected_profit_percent": expected_profit_percent,
        "price_at_date": current_price or 0.0,
        "details": details,
        "confidence": confidence
    }
    
    return recommendation


def create_langgraph_summarization_node(config: Dict[str, Any]):
    """
    Create a LangGraph node that performs final summarization
    
    Args:
        config: Configuration dictionary
        
    Returns:
        Function that can be used as a LangGraph node
    """
    from langchain_core.messages import HumanMessage
    
    def summarization_node(state: Dict[str, Any]) -> Dict[str, Any]:
        """
        LangGraph node that creates final recommendation summary
        """
        from .. import logger as ta_logger
        
        ta_logger.log_step_start("Final Summarization", f"Symbol: {state.get('company_of_interest', 'Unknown')}")
        
        try:
            symbol = state.get("company_of_interest", "UNKNOWN")
            current_price = state.get("current_price", 0.0)
            
            # Create recommendation summary
            recommendation = create_expert_recommendation_summary(state, symbol, current_price)
            
            # Store in database if market_analysis_id is available
            if hasattr(state, 'market_analysis_id') and state.market_analysis_id:
                from ..db_storage import store_analysis_output
                store_analysis_output(
                    market_analysis_id=state.market_analysis_id,
                    name="final_recommendation",
                    output_type="recommendation",
                    text=json.dumps(recommendation, indent=2)
                )
            
            # Update state with recommendation
            state["expert_recommendation"] = recommendation
            
            ta_logger.info(f"Generated recommendation: {recommendation['recommended_action']} for {symbol} with {recommendation['confidence']:.2f} confidence")
            ta_logger.log_step_complete("Final Summarization", True)
            
            return state
            
        except Exception as e:
            ta_logger.error(f"Error in summarization node: {str(e)}")
            ta_logger.log_step_complete("Final Summarization", False)
            
            # Return state with default recommendation on error
            state["expert_recommendation"] = {
                "symbol": state.get("company_of_interest", "UNKNOWN"),
                "recommended_action": "HOLD",
                "expected_profit_percent": 0.0,
                "price_at_date": 0.0,
                "details": f"Error generating recommendation: {str(e)}",
                "confidence": 0.0
            }
            return state
    
    return summarization_node


def add_summarization_to_graph(graph_builder, config: Dict[str, Any]):
    """
    Add the summarization node to an existing LangGraph
    
    Args:
        graph_builder: The LangGraph StateGraph builder
        config: Configuration dictionary
    """
    
    # Create and add the summarization node
    summarization_node = create_langgraph_summarization_node(config)
    graph_builder.add_node("final_summarization", summarization_node)
    
    # The node should be added as the last step before END
    # This would need to be integrated into the existing graph structure
    # based on how the current graph is built
    
    return graph_builder