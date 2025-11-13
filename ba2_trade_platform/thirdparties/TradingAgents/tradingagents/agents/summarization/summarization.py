"""
Final summarization agent for TradingAgents with proper LangGraph integration
Uses LangChain's structured output capabilities to generate JSON recommendations
"""
import json
import re
from typing import Dict, Any, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser, PydanticOutputParser

from ...prompts import get_prompt
from ... import logger


def clean_json_string(json_str: str) -> str:
    """
    Clean common JSON formatting issues from LLM output
    - Removes trailing commas before closing brackets/braces
    - Removes comments
    """
    # Remove trailing commas before closing brackets/braces
    # Pattern: comma followed by optional whitespace and closing bracket/brace
    json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
    
    # Remove single-line comments (// ...)
    json_str = re.sub(r'//.*$', '', json_str, flags=re.MULTILINE)
    
    # Remove multi-line comments (/* ... */)
    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
    
    return json_str


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
    expected_profit_percent: float = Field(description="Expected profit/loss percentage. Calculate as: For BUY: ((take_profit - price_at_date) / price_at_date) * 100. For SELL: ((price_at_date - stop_loss) / price_at_date) * 100. For HOLD: 0.0")
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
    Create a summarization agent using JsonOutputParser for structured output.
    Works reliably with OpenAI and NagaAI (OpenAI client) without requiring
    provider-specific structured output APIs.
    
    Args:
        llm: The language model to use (OpenAI or NagaAI via OpenAI client)
        
    Returns:
        Function that can be used as a LangGraph node
    """
    
    # Custom JSON parser that cleans common LLM formatting issues
    class CleanJsonOutputParser(JsonOutputParser):
        """JsonOutputParser with JSON cleaning for LLM outputs"""
        
        def parse(self, text: str) -> Any:
            """Parse JSON with cleaning to handle trailing commas and comments"""
            cleaned_text = clean_json_string(text)
            return super().parse(cleaned_text)
    
    # Use custom parser with JSON cleaning
    json_parser = CleanJsonOutputParser(pydantic_object=ExpertRecommendation)
    
    def summarization_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
        """
        LangGraph node that creates structured final recommendation
        """
        logger.log_step_start("Final Summarization Agent", f"Symbol: {state.get('company_of_interest', 'Unknown')}")
        
        try:
            symbol = state.get("company_of_interest", "UNKNOWN")
            current_price = state.get("current_price", 0.0)
            
            # Prepare comprehensive analysis context
            analysis_context = _prepare_analysis_context(state, symbol, current_price)
            
            # Get the summarization prompt
            system_prompt = get_prompt("final_summarization")
            
            # Build prompt with JSON format instructions
            prompt = ChatPromptTemplate.from_messages([
                ("system", system_prompt),
                ("human", "Analyze the following comprehensive trading analysis and generate a structured JSON recommendation.\n\n{format_instructions}\n\nAnalysis:\n{analysis_context}")
            ])
            logger.debug(f"Json format instructions : {json_parser.get_format_instructions()}")
            # Chain: prompt -> LLM -> JSON parser
            chain = prompt | llm | json_parser
            recommendation = chain.invoke({
                "format_instructions": json_parser.get_format_instructions(),
                "analysis_context": analysis_context
            })
            
            # recommendation is already parsed as dict from JsonOutputParser
            recommendation_dict = recommendation
            
            # Store in state
            state["expert_recommendation"] = recommendation_dict
            state["final_analysis_summary"] = recommendation_dict.get("analysis_summary", {})
            
            logger.info(f"Generated structured recommendation: {recommendation_dict['recommended_action']} for {symbol} with {recommendation_dict['confidence']:.1f}% confidence")
            logger.debug(f"Full recommendation: {json.dumps(recommendation_dict, indent=4)}")
            logger.log_step_complete("Final Summarization Agent", True)
            
            return state
            
        except Exception as e:
            logger.error(f"Error in summarization agent: {str(e)}", exc_info=True)
            logger.log_step_complete("Final Summarization Agent", False)
            
            # Log activity for analysis failure
            _log_analysis_failure(state, symbol, str(e))
            
            # Do NOT create fallback recommendation on error - let error handling in TradingAgents.py take over
            # This ensures MarketAnalysis stays FAILED and no ExpertRecommendation is created
            state["expert_recommendation"] = {}
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


def _log_analysis_failure(state: Dict[str, Any], symbol: str, error_msg: str) -> None:
    """Log analysis failure to ActivityLog"""
    try:
        # Only log if we have market_analysis_id (database mode)
        market_analysis_id = state.get("market_analysis_id")
        if not market_analysis_id:
            return
        
        # Import BA2 platform dependencies
        from ba2_trade_platform.core.db import get_instance, log_activity
        from ba2_trade_platform.core.models import MarketAnalysis, ExpertInstance
        from ba2_trade_platform.core.types import ActivityLogSeverity, ActivityLogType
        
        # Get market analysis to retrieve expert_instance_id
        market_analysis = get_instance(MarketAnalysis, market_analysis_id)
        if not market_analysis:
            logger.warning(f"Could not find MarketAnalysis {market_analysis_id} for activity logging")
            return
        
        expert_instance_id = market_analysis.expert_instance_id
        
        # Get expert instance to retrieve account_id
        expert_instance = get_instance(ExpertInstance, expert_instance_id)
        if not expert_instance:
            logger.warning(f"Could not find ExpertInstance {expert_instance_id} for activity logging")
            return
        
        account_id = expert_instance.account_id
        
        # Determine error type for better categorization
        error_type = "JSON Parsing Error" if "json" in error_msg.lower() or "parse" in error_msg.lower() else "Analysis Error"
        
        # Log the failure with analysis ID in description for traceability
        log_activity(
            severity=ActivityLogSeverity.FAILURE,
            activity_type=ActivityLogType.ANALYSIS_FAILED,
            description=f"TradingAgents analysis failed (ID {market_analysis_id}) for {symbol}: {error_type}",
            data={
                "symbol": symbol,
                "error_message": error_msg[:500],  # Truncate to 500 chars
                "error_type": error_type,
                "market_analysis_id": market_analysis_id
            },
            source_account_id=account_id,
            source_expert_id=expert_instance_id
        )
        
        logger.info(f"Logged analysis failure activity for {symbol} (MarketAnalysis #{market_analysis_id})")
        
    except Exception as log_error:
        # Don't let logging failures break the analysis
        logger.warning(f"Failed to log analysis failure activity: {log_error}")


def _create_fallback_recommendation(state: Dict[str, Any], symbol: str, current_price: float, error_msg: str) -> Dict[str, Any]:
    """Create a fallback recommendation when structured output fails"""
    logger.error(f"Creating fallback recommendation due to error: {error_msg}")
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
        "stop_loss": 0.0,
        "take_profit": 0.0,
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
    logger.warning("create_langgraph_summarization_node is deprecated. Use create_final_summarization_agent instead.")
    
    # Try to get LLM from config - require explicit config key, no defaults
    try:
        from langchain_openai import ChatOpenAI
        from ...dataflows.config import get_api_key_from_database
        
        # Will raise KeyError if required keys are not configured
        model = config["quick_think_llm"]
        base_url = config["backend_url"]
        api_key_setting = config.get("api_key_setting", "openai_api_key")
        api_key = get_api_key_from_database(api_key_setting)
        
        # Check if streaming is enabled in config
        from ba2_trade_platform import config as ba2_config
        streaming_enabled = ba2_config.OPENAI_ENABLE_STREAMING
        
        llm = ChatOpenAI(
            model=model, 
            base_url=base_url, 
            api_key=api_key,
            streaming=streaming_enabled
        )
        return create_final_summarization_agent(llm)
    except KeyError as e:
        logger.error(f"Missing required configuration key: {e}", exc_info=True)
        
        def error_node(state: Dict[str, Any]) -> Dict[str, Any]:
            state["expert_recommendation"] = _create_fallback_recommendation(
                state, 
                state.get("company_of_interest", "UNKNOWN"), 
                state.get("current_price", 0.0),
                f"Configuration error: {e}"
            )
            return state
        
        return error_node
