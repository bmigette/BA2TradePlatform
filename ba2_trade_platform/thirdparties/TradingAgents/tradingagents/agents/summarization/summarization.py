"""
Final summarization agent for TradingAgents with proper LangGraph integration
Uses LangChain's structured output capabilities to generate JSON recommendations
"""
import json
from typing import Dict, Any, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser, PydanticOutputParser

from ...prompts import get_prompt
from ... import logger as ta_logger


# Pydantic models for structured output
class AnalysisSummary(BaseModel):
    """Summary of different analysis components"""
    market_trend: str = Field(description="BULLISH|BEARISH|NEUTRAL")
    fundamental_strength: str = Field(description="STRONG|MODERATE|WEAK") 
    sentiment_score: float = Field(description="Overall sentiment (-100 to 100)")
    macro_environment: str = Field(description="FAVORABLE|NEUTRAL|UNFAVORABLE")
    technical_signals: str = Field(description="BUY|SELL|NEUTRAL")


class ExpertRecommendation(BaseModel):
    """Structured expert recommendation output"""
    symbol: str = Field(description="Stock ticker symbol")
    recommended_action: str = Field(description="BUY|SELL|HOLD")
    expected_profit_percent: float = Field(description="Expected profit/loss percentage")
    price_at_date: float = Field(description="Current stock price at analysis")
    confidence: float = Field(description="Confidence level (0-100)")
    details: str = Field(description="Detailed explanation of recommendation", max_length=2000)
    risk_level: str = Field(description="LOW|MEDIUM|HIGH")
    time_horizon: str = Field(description="SHORT_TERM|MEDIUM_TERM|LONG_TERM")
    key_factors: list[str] = Field(description="Array of 3-5 key factors driving the decision")
    stop_loss: float = Field(description="Recommended stop loss price")
    take_profit: float = Field(description="Recommended take profit price")
    analysis_summary: AnalysisSummary = Field(description="Summary of analysis components")


def create_final_summarization_agent(llm):
    """
    Create a summarization agent using LangChain's structured output
    
    Args:
        llm: The language model to use
        
    Returns:
        Function that can be used as a LangGraph node
    """
    
    # Set up structured output using Pydantic
    try:
        # Try to use with_structured_output if available (newer LangChain versions)
        structured_llm = llm.with_structured_output(ExpertRecommendation)
        use_structured_output = True
        ta_logger.debug("Using LangChain structured output with Pydantic")
    except AttributeError:
        # Fallback to PydanticOutputParser for older versions
        parser = PydanticOutputParser(pydantic_object=ExpertRecommendation)
        use_structured_output = False
        ta_logger.debug("Using PydanticOutputParser fallback")
    
    def summarization_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
        """
        LangGraph node that creates structured final recommendation
        """
        ta_logger.log_step_start("Final Summarization Agent", f"Symbol: {state.get('company_of_interest', 'Unknown')}")
        
        try:
            symbol = state.get("company_of_interest", "UNKNOWN")
            current_price = state.get("current_price", 0.0)
            
            # Prepare comprehensive analysis context
            analysis_context = _prepare_analysis_context(state, symbol, current_price)
            
            # Get the summarization prompt
            system_prompt = get_prompt("final_summarization")
            
            if use_structured_output:
                # Use structured output directly
                prompt = ChatPromptTemplate.from_messages([
                    ("system", system_prompt),
                    ("human", "Analyze the following comprehensive trading analysis and generate a structured JSON recommendation:\n\n{analysis_context}")
                ])
                
                chain = prompt | structured_llm
                recommendation = chain.invoke({"analysis_context": analysis_context})
                
                # Convert Pydantic model to dict
                if hasattr(recommendation, 'model_dump'):
                    recommendation_dict = recommendation.model_dump()
                else:
                    recommendation_dict = recommendation.dict()
                    
            else:
                # Use parser fallback
                prompt = ChatPromptTemplate.from_messages([
                    ("system", system_prompt + "\n\n" + parser.get_format_instructions()),
                    ("human", "Analyze the following comprehensive trading analysis and generate a structured JSON recommendation:\n\n{analysis_context}")
                ])
                
                chain = prompt | llm | parser
                recommendation = chain.invoke({"analysis_context": analysis_context})
                recommendation_dict = recommendation.dict() if hasattr(recommendation, 'dict') else dict(recommendation)
            
            # Store in state
            state["expert_recommendation"] = recommendation_dict
            state["final_analysis_summary"] = recommendation_dict.get("analysis_summary", {})
            
            ta_logger.info(f"Generated structured recommendation: {recommendation_dict['recommended_action']} for {symbol} with {recommendation_dict['confidence']:.1f}% confidence")
            ta_logger.debug(f"Full recommendation: {json.dumps(recommendation_dict, indent=4)}")
            ta_logger.log_step_complete("Final Summarization Agent", True)
            
            return state
            
        except Exception as e:
            ta_logger.error(f"Error in summarization agent: {str(e)}", exc_info=True)
            ta_logger.log_step_complete("Final Summarization Agent", False)
            
            # Return state with fallback recommendation
            fallback_recommendation = _create_fallback_recommendation(state, symbol, current_price, str(e))
            state["expert_recommendation"] = fallback_recommendation
            return state
    
    return summarization_agent_node


def _prepare_analysis_context(state: Dict[str, Any], symbol: str, current_price: float) -> str:
    """Prepare comprehensive analysis context for the LLM"""
    
    context_parts = [
        f"SYMBOL: {symbol}",
        f"CURRENT PRICE: ${current_price:.2f}",
        f"ANALYSIS DATE: {state.get('trade_date', datetime.now().strftime('%Y-%m-%d'))}",
        ""
    ]
    
    # Add market analysis
    if state.get("market_report"):
        context_parts.extend([
            "=== MARKET ANALYSIS ===",
            str(state["market_report"]),
            ""
        ])
    
    # Add news analysis
    if state.get("news_report"):
        context_parts.extend([
            "=== NEWS ANALYSIS ===", 
            str(state["news_report"]),
            ""
        ])
    
    # Add fundamentals analysis
    if state.get("fundamentals_report"):
        context_parts.extend([
            "=== FUNDAMENTALS ANALYSIS ===",
            str(state["fundamentals_report"]),
            ""
        ])
    
    # Add sentiment analysis
    if state.get("sentiment_report"):
        context_parts.extend([
            "=== SENTIMENT ANALYSIS ===",
            str(state["sentiment_report"]),
            ""
        ])
    
    # Add macro analysis
    if state.get("macro_report"):
        context_parts.extend([
            "=== MACRO ECONOMIC ANALYSIS ===",
            str(state["macro_report"]),
            ""
        ])
    
    # Add investment debate
    if state.get("investment_debate_state"):
        debate_state = state["investment_debate_state"]
        context_parts.extend([
            "=== INVESTMENT DEBATE SUMMARY ===",
            f"Bull Arguments: {debate_state.get('bull_history', [])}",
            f"Bear Arguments: {debate_state.get('bear_history', [])}",
            f"Judge Decision: {debate_state.get('judge_decision', 'None')}",
            ""
        ])
    
    # Add risk analysis
    if state.get("risk_debate_state"):
        risk_state = state["risk_debate_state"]
        context_parts.extend([
            "=== RISK ANALYSIS ===",
            f"Risk Assessment: {risk_state.get('judge_decision', 'None')}",
            ""
        ])
    
    # Add final trade decision
    if state.get("final_trade_decision"):
        context_parts.extend([
            "=== FINAL TRADE DECISION ===",
            str(state["final_trade_decision"]),
            ""
        ])
    
    # Add investment plan
    if state.get("investment_plan"):
        context_parts.extend([
            "=== INVESTMENT PLAN ===",
            str(state["investment_plan"]),
            ""
        ])
    
    return "\n".join(context_parts)


def _create_fallback_recommendation(state: Dict[str, Any], symbol: str, current_price: float, error_msg: str) -> Dict[str, Any]:
    """Create a fallback recommendation when structured output fails"""
    
    return {
        "symbol": symbol,
        "recommended_action": "HOLD",
        "expected_profit_percent": 0.0,
        "price_at_date": current_price,
        "confidence": 0.0,
        "details": f"Error generating structured recommendation: {error_msg}. Defaulting to HOLD.",
        "risk_level": "MEDIUM",
        "time_horizon": "MEDIUM_TERM",
        "key_factors": ["Analysis error occurred"],
        "stop_loss": current_price * 0.95 if current_price > 0 else 0.0,
        "take_profit": current_price * 1.05 if current_price > 0 else 0.0,
        "analysis_summary": {
            "market_trend": "NEUTRAL",
            "fundamental_strength": "MODERATE",
            "sentiment_score": 0.0,
            "macro_environment": "NEUTRAL", 
            "technical_signals": "NEUTRAL"
        }
    }





# Backward compatibility - keep the old function name but use new implementation
def create_langgraph_summarization_node(config: Dict[str, Any]):
    """
    DEPRECATED: Use create_final_summarization_agent instead
    Kept for backward compatibility
    """
    ta_logger.warning("create_langgraph_summarization_node is deprecated. Use create_final_summarization_agent instead.")
    
    # Try to get LLM from config
    try:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=config.get("quick_think_llm", "gpt-3.5-turbo"))
        return create_final_summarization_agent(llm)
    except Exception as e:
        ta_logger.error(f"Could not create summarization agent: {e}", exc_info=True)
        
        def error_node(state: Dict[str, Any]) -> Dict[str, Any]:
            state["expert_recommendation"] = _create_fallback_recommendation(
                state, 
                state.get("company_of_interest", "UNKNOWN"), 
                state.get("current_price", 0.0),
                f"Configuration error: {e}"
            )
            return state
        
        return error_node