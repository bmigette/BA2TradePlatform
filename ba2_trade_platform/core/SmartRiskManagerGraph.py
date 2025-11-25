"""
Smart Risk Manager Graph

LangGraph-based agentic workflow for autonomous portfolio risk management.
Analyzes portfolio status, recent market analyses, and executes risk management actions.
"""

from typing import Dict, Any, List, Annotated, TypedDict, Optional
from datetime import datetime, timezone
from operator import add
import sys
from io import StringIO
from contextlib import contextmanager

from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END, START
from langgraph.prebuilt import ToolNode
from sqlmodel import select
import json
import os
import langchain

from ..logger import logger
from .. import config as config_module
from .models import SmartRiskManagerJob, MarketAnalysis, Transaction
from .db import get_db, add_instance, update_instance, get_instance
from .models import AppSetting
from .SmartRiskManagerToolkit import SmartRiskManagerToolkit
from .utils import get_expert_instance_from_id
from .interfaces import MarketExpertInterface


# ==================== HELPER FUNCTIONS ====================

def truncate_tool_call_id(call_id: str, max_length: int = 64) -> str:
    """
    Truncate tool call ID to comply with OpenAI's length limit.
    
    OpenAI enforces a 64-character limit on tool call IDs, but LangGraph can generate
    longer IDs when parallel tool calls are enabled. This function creates a deterministic
    shortened ID using a hash suffix to ensure uniqueness.
    
    Args:
        call_id: Original call_id from tool_call
        max_length: Maximum allowed length (default 64 for OpenAI)
        
    Returns:
        Truncated call_id if original exceeds max_length, otherwise original
    """
    if len(call_id) <= max_length:
        return call_id
    
    import hashlib
    # Create deterministic shortened ID using hash
    # Use first 40 chars + hash of full ID to ensure uniqueness
    hash_suffix = hashlib.sha256(call_id.encode()).hexdigest()[:24]
    truncated_id = f"{call_id[:40]}_{hash_suffix}"[:max_length]
    
    logger.debug(f"Truncated call_id from {len(call_id)} to {len(truncated_id)} chars")
    return truncated_id


# ==================== DEBUG CALLBACK ====================

@contextmanager
def suppress_langchain_stdout():
    """
    Context manager to suppress LangChain's internal stdout/stderr output.
    
    LangChain's agent executor and tool binding sometimes print "AI:" and "Tool:" 
    to stdout even with debug=False and verbose=False. This suppresses that noise
    while preserving our custom logger output.
    """
    # Save original stdout/stderr
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    
    try:
        # Redirect to null
        sys.stdout = StringIO()
        sys.stderr = StringIO()
        yield
    finally:
        # Restore original stdout/stderr
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def smart_risk_manager_tool(func):
    """
    Decorator for Smart Risk Manager tools that provides streamlined logging.
    
    Logs all tool calls and their results in a consistent, easy-to-read format:
    - Tool calls with arguments
    - Tool results (success/failure)
    - Any errors encountered
    
    Usage:
        @tool
        @smart_risk_manager_tool
        def my_tool(arg1, arg2):
            return {"result": "data"}
    """
    from functools import wraps
    
    @wraps(func)
    def wrapper(*args, **kwargs):
        tool_name = func.__name__.replace("_tool", "").replace("_", " ").title()
        
        # Format arguments for logging (limit size for readability)
        args_str = ", ".join([f"{repr(arg)[:1000]}" for arg in args])
        kwargs_str = ", ".join([f"{k}={repr(v)[:1000]}" for k, v in kwargs.items()])
        all_args = ", ".join(filter(None, [args_str, kwargs_str]))
        
        logger.debug(f"AI SMART Manager Toolcall: {tool_name} - Args: {all_args}")
        logger.debug("-" * 80)
        
        try:
            result = func(*args, **kwargs)
            
            # Format result for logging (limit size)
            if isinstance(result, dict):
                result_preview = f"dict with keys: {list(result.keys())}"
                if "success" in result:
                    result_preview += f" | success={result['success']}"
                if "message" in result:
                    result_preview += f" | message={result['message'][:1000]}"
            elif isinstance(result, str):
                result_preview = result[:1000] if len(result) > 1000 else result
            elif isinstance(result, (list, tuple)):
                result_preview = f"{type(result).__name__} with {len(result)} items"
            else:
                result_preview = repr(result)[:1000]
            
            logger.debug(f"Result: {result_preview}")
            logger.debug("-" * 80 + "\n")
            
            return result
            
        except Exception as e:
            logger.error(f"AI SMART Manager Tool Error: {tool_name} - {e}", exc_info=True)
            logger.debug("-" * 80 + "\n")
            raise
    
    return wrapper


class SmartRiskManagerDebugCallback(BaseCallbackHandler):
    """Custom callback handler for human-readable debug output."""
    
    def __init__(self):
        super().__init__()
        self.call_count = 0  # Track LLM call iterations
        self.logged_message_count = 0  # Track how many messages we've already logged
    
    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], **kwargs) -> None:
        """Log LLM call start - only log NEW messages after first iteration."""
        self.call_count += 1
        
        # Try to extract model name from various sources
        model_name = (
            kwargs.get('invocation_params', {}).get('model_name') or
            kwargs.get('invocation_params', {}).get('model') or
            serialized.get('kwargs', {}).get('model_name') or
            serialized.get('kwargs', {}).get('model') or
            serialized.get('id', ['unknown'])[-1] if isinstance(serialized.get('id'), list) else 'unknown'
        )
        
        logger.debug("=" * 80)
        logger.debug(f"🤖 LLM CALL #{self.call_count} - Model: {model_name}")
        logger.debug("=" * 80)
        
        # On first call, log the full system prompt
        # On subsequent calls, only log new messages (conversation continuation)
        if self.call_count == 1:
            logger.debug(f"📝 Initial Prompt (System + User):")
            if prompts:
                for prompt in prompts:
                    logger.debug(prompt)
            self.logged_message_count = len(prompts) if prompts else 0
        else:
            # Only log if there are new prompts beyond what we've seen
            logger.debug(f"📝 Continuing conversation (call #{self.call_count})...")
    
    def on_llm_end(self, response, **kwargs) -> None:
        """Log LLM response in human-readable format."""
        logger.debug("=" * 80)
        logger.debug("✅ LLM RESPONSE")
        logger.debug("=" * 80)
        
        if hasattr(response, 'generations') and response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    if hasattr(gen, 'text') and gen.text:
                        logger.debug(f"\n{gen.text}\n")
                    if hasattr(gen, 'message'):
                        message = gen.message
                        # Use _log_message style formatting
                        self._log_message(message)
        
        logger.debug("=" * 80 + "\n")
    
    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, **kwargs) -> None:
        """Log tool call start."""
        tool_name = serialized.get("name", "unknown")
        logger.debug(f"\n🔧 TOOL CALL: {tool_name}")
        logger.debug(f"   Input: {input_str}")
    
    def on_tool_end(self, output: str, **kwargs) -> None:
        """Log tool call result."""
        logger.debug(f"   ✅ Output: {output}\n")
    
    def _log_message(self, message) -> None:
        """
        Log a LangChain message object in structured format.
        Based on TradingAgents' _log_message pattern.
        
        Args:
            message: A LangChain BaseMessage object to log
        """
        try:
            from langchain_core.messages import (
                HumanMessage, AIMessage, ToolMessage, SystemMessage, BaseMessage
            )
            
            if isinstance(message, ToolMessage):
                # Format Tool Messages
                logger.debug(f"{'=' * 80}")
                logger.debug(f"Tool Message")
                logger.debug(f"{'=' * 80}")
                logger.debug(f"Tool: {message.tool_calls[0]['name'] if hasattr(message, 'tool_calls') and message.tool_calls else 'Unknown'}")
                logger.debug(f"Tool ID: {message.tool_call_id if hasattr(message, 'tool_call_id') else 'N/A'}")
                if hasattr(message, 'content') and message.content:
                    content = message.content if isinstance(message.content, str) else str(message.content)
                    # Log as error if content starts with "Error:"
                    if content.startswith("Error:"):
                        logger.error(f"Result: {content}")
                    else:
                        logger.debug(f"Result: {content}")
                logger.debug(f"{'=' * 80}")
            elif isinstance(message, AIMessage):
                # Format AI Messages
                logger.debug(f"{'=' * 80}")
                logger.debug(f"AI Message")
                logger.debug(f"{'=' * 80}")
                if hasattr(message, 'content') and message.content:
                    content = message.content if isinstance(message.content, str) else str(message.content)
                    logger.debug(f"Content: {content}")
                if hasattr(message, 'tool_calls') and message.tool_calls:
                    logger.debug(f"Tool Calls: {len(message.tool_calls)}")
                    for i, tc in enumerate(message.tool_calls, 1):
                        logger.debug(f"  {i}. {tc.get('name', 'Unknown')} - {tc.get('id', 'Unknown')}")
                logger.debug(f"{'=' * 80}")
            elif isinstance(message, HumanMessage):
                # Format Human Messages
                logger.debug(f"Human Message: {str(message.content)}")
            elif isinstance(message, SystemMessage):
                # Format System Messages
                logger.debug(f"System Message: {str(message.content)}")
            else:
                # Generic message handling
                msg_type = message.__class__.__name__
                logger.debug(f"{msg_type}: {str(message)}")
                
        except Exception as e:
            logger.warning(f"Error logging message: {e}")
            try:
                logger.debug(f"Message (fallback): {str(message)}")
            except:
                pass


# ==================== HELPER FUNCTIONS ====================

def _format_relative_time(timestamp_str: str) -> str:
    """
    Convert ISO timestamp to relative time (e.g., '2 hours ago').
    
    Args:
        timestamp_str: ISO format timestamp string
        
    Returns:
        Relative time string like '2 hours ago', '1 day ago', etc.
    """
    try:
        from datetime import datetime, timezone
        
        # Parse timestamp
        if isinstance(timestamp_str, str):
            # Handle both with and without timezone
            if 'Z' in timestamp_str or '+' in timestamp_str or timestamp_str.endswith('+00:00'):
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            else:
                dt = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
        else:
            return ""
        
        # Calculate difference
        now = datetime.now(timezone.utc)
        diff = now - dt
        
        seconds = diff.total_seconds()
        
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes} min ago" if minutes == 1 else f"{minutes} mins ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour ago" if hours == 1 else f"{hours} hours ago"
        elif seconds < 604800:
            days = int(seconds / 86400)
            return f"{days} day ago" if days == 1 else f"{days} days ago"
        else:
            weeks = int(seconds / 604800)
            return f"{weeks} week ago" if weeks == 1 else f"{weeks} weeks ago"
            
    except Exception as e:
        logger.debug(f"Error formatting relative time: {e}")
        return ""


def get_api_key_from_database(key_name: str) -> str:
    """
    Get API key from database AppSettings.
    
    Args:
        key_name: Setting key to retrieve (e.g., 'openai_api_key', 'naga_ai_api_key')
        
    Returns:
        API key value
        
    Raises:
        ValueError: If API key not found in database
    """
    try:
        from sqlmodel import select
        from ba2_trade_platform.core.db import get_db
        
        with get_db() as session:
            statement = select(AppSetting).where(AppSetting.key == key_name)
            setting = session.exec(statement).first()
            
        if not setting or not setting.value_str:
            raise ValueError(f"API key '{key_name}' not found in database")
        return setting.value_str
    except Exception as e:
        logger.error(f"Failed to get API key '{key_name}': {e}")
        raise ValueError(f"API key '{key_name}' not configured")


def mark_job_as_failed(job_id: int, error_message: str) -> None:
    """
    Helper function to mark SmartRiskManagerJob as FAILED in database.
    
    Args:
        job_id: ID of the SmartRiskManagerJob
        error_message: Error message to store
    """
    try:
        with get_db() as session:
            job = session.get(SmartRiskManagerJob, job_id)
            if job:
                job.status = "FAILED"
                job.error_message = error_message
                session.add(job)
                session.commit()
                logger.info(f"Marked SmartRiskManagerJob {job_id} as FAILED")
    except Exception as db_error:
        logger.error(f"Failed to update job status in database: {db_error}")


def create_llm(model: str, temperature: float, base_url: str, api_key: str, model_kwargs: dict = None) -> ChatOpenAI:
    """
    Create a ChatOpenAI instance with proper configuration and debug callback.
    
    Args:
        model: Model name (e.g., 'gpt-4o-mini' or 'gpt-5.1-2025-11-13' or 'Provider/ModelName{param:value}')
        temperature: Temperature for generation
        base_url: Base URL for API endpoint
        api_key: API key for authentication
        model_kwargs: Optional model-specific parameters (e.g., {"reasoning_effort": "low"})
        
    Returns:
        Configured ChatOpenAI instance with debug callback
    """
    from .. import config as config_module
    from .utils import parse_model_config
    
    # Parse model configuration (handles provider, model name, and parameters)
    parsed = parse_model_config(model)
    
    # Merge any provided model_kwargs with parsed parameters
    final_model_kwargs = parsed['model_kwargs'].copy()
    if model_kwargs:
        final_model_kwargs.update(model_kwargs)
    
    # Extract parameters that should be passed directly to ChatOpenAI (not in model_kwargs)
    # These are special parameters that ChatOpenAI recognizes at the class level
    direct_params = ['reasoning', 'temperature', 'max_tokens', 'top_p', 'frequency_penalty', 'presence_penalty']
    
    # Build ChatOpenAI initialization parameters
    llm_params = {
        "model": parsed['model'],
        "temperature": temperature,
        "base_url": base_url,
        "api_key": api_key,
        "streaming": config_module.OPENAI_ENABLE_STREAMING,
        "callbacks": [SmartRiskManagerDebugCallback()]
    }
    
    # Separate direct params from model_kwargs
    remaining_model_kwargs = {}
    for key, value in final_model_kwargs.items():
        if key in direct_params:
            llm_params[key] = value
        else:
            remaining_model_kwargs[key] = value
    
    # Only add model_kwargs if there are remaining parameters
    if remaining_model_kwargs:
        llm_params["model_kwargs"] = remaining_model_kwargs
    
    return ChatOpenAI(**llm_params)


# ==================== PROMPTS ====================
# All prompts are defined here for easy maintenance and customization

SYSTEM_INITIALIZATION_PROMPT = """You are the Smart Risk Manager, an AI assistant responsible for monitoring and managing portfolio risk.

## YOUR MISSION
{user_instructions}

## YOUR TRADING PERMISSIONS
**CRITICAL - Know Your Boundaries:**
- **BUY orders:** {buy_status}
- **SELL orders:** {sell_status}
- **Automated trading:** {auto_trading_status}

{trading_focus_guidance}

## YOUR COMPREHENSIVE TOOLKIT
You have access to ALL the tools and data needed to make well-informed risk management decisions:

**Portfolio Analysis Tools:**
- Complete portfolio status with P&L, positions, equity, and balance
- Individual position details with entry prices, current prices, stop loss, take profit
- Real-time bid/ask prices for all instruments
- Position-level and portfolio-level profit/loss tracking

**Market Research Tools:**
- Recent market analyses (last 72 hours) with expert recommendations
- Detailed analysis outputs including:
  * Technical indicators (MACD, RSI, EMA, SMA, ATR, Bollinger Bands, support/resistance, volume, price patterns)
  * Fundamental data (earnings calls, cash flow, balance sheets, income statements, valuation ratios, insider transactions)
  * Social sentiment analysis (mentions, sentiment scores, trending topics, community engagement)
  * News analysis (recent articles, sentiment scores, market-moving events, press releases)
  * Macroeconomic data (GDP, inflation, interest rates, Fed policy, unemployment, economic calendar)
- Investment debates (bull vs bear arguments) and risk debates (risky/safe/neutral perspectives)
- Historical analyses for deeper symbol research

**Trading Action Tools:**
- Close positions completely
- Adjust position quantities (partial close or add)
- Update stop loss prices
- Update take profit prices
- Open new positions (when enabled)

## CRITICAL: YOU HAVE SUFFICIENT DATA
The tools above provide COMPREHENSIVE coverage of technical, fundamental, sentiment, news, and macro factors. You have everything needed to make clear, confident risk management decisions. Do not hesitate or defer decisions due to lack of information—research the available analyses and act decisively based on the complete picture they provide.

## YOUR WORKFLOW
1. Analyze the current portfolio status and identify risks
2. Research recent market analyses for positions that need attention (use batch tools for efficiency)
3. Make informed decisions about which actions to take based on comprehensive data
4. Execute trading actions with clear reasoning
5. Iterate and refine until portfolio risk is acceptable

## IMPORTANT GUIDELINES
- Always provide clear reasoning for your decisions
- Consider both the portfolio-level risk AND individual position risks
- Use market analyses to inform your decisions—they contain all the data you need
- Take conservative actions when uncertain
- Document your reasoning in every action
- Act decisively when the data supports action
- **RESPECT YOUR TRADING PERMISSIONS** - Focus on actions you're allowed to take

You will be guided through each step of the process. Let's begin.
"""

PORTFOLIO_ANALYSIS_PROMPT = """Analyze the current portfolio status and identify key risks and opportunities.

## CRITICAL: YOU HAVE FULL AUTONOMY
You are an autonomous risk management system. Do NOT ask for approval or permission.
You will analyze, then the system will automatically proceed to research and action phases.
Simply provide your assessment - no approval required.

## CURRENT PORTFOLIO STATUS
{portfolio_status}

## 🚨 IMPORTANT: VALID TRANSACTION IDs 🚨
**ONLY the transaction IDs listed in "FILLED Positions:" above are valid for actions.**
- Do NOT reference transaction IDs from previous sessions, closed positions, or failed transactions
- Do NOT attempt to modify transactions that belong to other experts
- When planning actions, ONLY use transaction IDs you see explicitly listed in the current portfolio summary

## TASK
Review the portfolio and create an initial assessment covering:
1. Overall portfolio health (P&L, concentration, diversification)
2. Positions with concerning P&L (large losses or excessive gains)
3. Positions that may need stop loss or take profit adjustments
4. Any risk concentrations (too much exposure to one symbol)
5. Initial thoughts on what actions may be needed

Be concise but thorough. This assessment will guide the next research phase which will happen automatically.
"""

# DECISION_LOOP_PROMPT removed - no longer using decision loop node

RESEARCH_PROMPT = """You are a research specialist for portfolio risk management.

## CRITICAL: YOU HAVE FULL AUTONOMY
You can call ANY tool MULTIPLE TIMES without asking for approval or permission.
Your job is to research thoroughly, then RECOMMEND SPECIFIC ACTIONS.

## YOUR MISSION
Investigate market analyses to gather detailed information that will help make risk management decisions.
When you have gathered enough information, you MUST recommend specific trading actions using the recommend_actions_tool tool.

## FOCUS YOUR RESEARCH
**IMPORTANT**: You should primarily focus your research and actions on:
1. **Symbols with recent analyses** (last 72 hours) - These have fresh market intelligence
2. **Symbols with open positions** - These require active risk management

When exploring opportunities or managing risks, start by reviewing what symbols have recent analyses available.
Don't spend time researching symbols without recent market intelligence unless there's a specific reason.

## PORTFOLIO CONTEXT
{agent_scratchpad}

## POSITION SIZE LIMITS - CRITICAL
**Maximum allocation per instrument:**
- **Percentage limit:** {max_position_pct}% of total portfolio equity per symbol
- **Dollar limit:** ${max_position_equity:.2f} maximum per symbol (based on current equity)

When recommending new positions or quantity adjustments:
- Calculate position value = quantity × current_price
- Ensure position value ≤ ${max_position_equity:.2f}
- For existing positions, consider current position value before adding more
- Example: If AAPL is at $150 and limit is $10,000, max quantity = 66 shares

## YOUR AVAILABLE RESEARCH TOOLS
You have full access to these research tools - use them freely and repeatedly:

**Market Analysis Research:**
- **get_analysis_outputs_tool(analysis_id: int)** - See what output keys are available for an analysis
  * analysis_id: ID of the MarketAnalysis to inspect
  * Returns: Dict with analysis_id, symbol, expert, and list of output_keys

- **get_analysis_output_detail_tool(analysis_id: int, output_key: str)** - Get detailed content from a specific analysis output
  * analysis_id: ID of the MarketAnalysis
  * output_key: Key of the output to retrieve (e.g., 'final_trade_decision', 'technical_analysis')
  * Returns: Dict with analysis_id, output_key, and full content

- **get_analysis_outputs_batch_tool(analysis_ids: List[int], output_keys: List[str], max_tokens: int = 50000)** - Efficiently fetch multiple outputs at once (RECOMMENDED for speed)
  * analysis_ids: List of MarketAnalysis IDs to fetch from (e.g., [123, 124, 125])
  * output_keys: List of output keys to fetch from each analysis (e.g., ["analysis_summary", "market_report"])
  * max_tokens: Maximum tokens in response (default: 50000)
  * Returns: Dict with outputs list, truncated status, and metadata

- **get_all_recent_analyses_tool(max_age_hours: int = 72)** - Get ALL recent analyses across all symbols without filtering
  * max_age_hours: Maximum age of analyses in hours (default: 72)
  * Returns: List of all recent analyses grouped by symbol
  * Use when: You want to discover what analyses are available without knowing specific symbols

- **get_historical_analyses_tool(symbol: str, limit: int = 10, offset: int = 0)** - Look up past analyses for a symbol
  * symbol: Instrument symbol to query
  * limit: Maximum number of analyses to return (default: 10)
  * offset: Number of analyses to skip for pagination (default: 0)
  * Returns: List of analysis dictionaries with id, symbol, expert, created_at

**Price Information:**
- **get_current_price_tool(symbol: str)** - Get real-time bid price for any symbol
  * symbol: Instrument symbol
  * Returns: Current bid price as float

**Action Recommendation Tools (MANDATORY - use these to recommend specific actions):**

**🚨 CRITICAL: Transaction ID Rules - READ THIS FIRST 🚨**
- **ONLY use transaction IDs from the CURRENT PORTFOLIO SUMMARY above**
- Each open position in the portfolio summary is shown as "Transaction #XXX: SYMBOL"
- Example: "Transaction #248: CRWD" means the transaction_id is 248
- **You CANNOT modify transactions from other experts** - attempting to do so will FAIL
- **You CANNOT modify CLOSED or FAILED transactions** - only OPENED transactions listed in portfolio summary are valid
- **If unsure about a transaction ID, DO NOT use it** - only use IDs you see explicitly listed in the "FILLED Positions:" section above
- Attempting to use invalid transaction IDs wastes iterations and produces errors - always verify IDs before recommending actions

- **recommend_close_position(transaction_id: int, reason: str, confidence: int)** - Recommend closing an existing position
  * transaction_id: ID of the open transaction/position to close (from portfolio summary)
  * reason: Clear explanation referencing your research findings (e.g., 'Analysis #456 shows bearish reversal')
  * confidence: Your confidence level (1-100) in this recommendation
  * Use when: Stop loss hit, take profit reached, changed market conditions, portfolio rebalancing
  * Returns: Confirmation with total action count

- **recommend_adjust_quantity(transaction_id: int, new_quantity: int, reason: str, confidence: int)** - Recommend changing position size
  * transaction_id: ID of the position to adjust (from portfolio summary)
  * new_quantity: New absolute quantity for the position (MUST be whole number like 10, not 10.5)
  * reason: Clear explanation for the quantity adjustment
  * confidence: Your confidence level (1-100)
  * Use when: Scaling position up/down based on research
  * Returns: Confirmation with total action count

- **recommend_update_stop_loss(transaction_id: int, new_sl_price: float, reason: str, confidence: int)** - Recommend updating stop loss level
  * transaction_id: ID of the position to update stop loss for
  * new_sl_price: New stop loss price level
  * reason: Clear explanation for the stop loss adjustment
  * confidence: Your confidence level (1-100)
  * Use when: Tightening or loosening stop loss based on market conditions
  * Returns: Confirmation with total action count

- **recommend_update_take_profit(transaction_id: int, new_tp_price: float, reason: str, confidence: int)** - Recommend updating take profit level
  * transaction_id: ID of the position to update take profit for
  * new_tp_price: New take profit price level
  * reason: Clear explanation for the take profit adjustment
  * confidence: Your confidence level (1-100)
  * Use when: Adjusting profit targets based on research
  * Returns: Confirmation with total action count

- **recommend_open_buy_position(symbol: str, quantity: int, reason: str, confidence: int, tp_price: float = None, sl_price: float = None)** - Recommend opening a new BUY (long) position
  * symbol: Instrument symbol to buy (e.g., 'AAPL', 'MSFT')
  * quantity: Number of shares/units to buy (MUST be a whole number, e.g., 10, not 10.5)
  * reason: Clear explanation for opening this position based on research
  * confidence: Your confidence level (1-100)
  * tp_price: Optional take profit price level
  * sl_price: Optional stop loss price level
  * Use when: Strong buying opportunity identified
  * Returns: Confirmation with total action count
  * **CRITICAL: TP/SL are set automatically when you provide tp_price/sl_price parameters. DO NOT call separate recommend_update_take_profit() or recommend_update_stop_loss() after opening a position - this creates duplicate orders!**

- **recommend_open_sell_position(symbol: str, quantity: int, reason: str, confidence: int, tp_price: float = None, sl_price: float = None)** - Recommend opening a new SELL (short) position
  * symbol: Instrument symbol to sell short (e.g., 'AAPL', 'MSFT')
  * quantity: Number of shares/units to sell short (MUST be a whole number, e.g., 10, not 10.5)
  * reason: Clear explanation for opening this short position based on research
  * confidence: Your confidence level (1-100)
  * tp_price: Optional take profit price level
  * sl_price: Optional stop loss price level
  * Use when: Strong short-selling opportunity identified
  * Returns: Confirmation with total action count
  * **CRITICAL: TP/SL are set automatically when you provide tp_price/sl_price parameters. DO NOT call separate recommend_update_take_profit() or recommend_update_stop_loss() after opening a position - this creates duplicate orders!**

**Pending Actions Management Tools:**
- **get_pending_actions_tool()** - See all currently recommended actions queued for execution
  * Returns: Formatted list of pending actions with their action numbers
  * Use when: You want to review what you've already recommended or check for duplicates
  * Important: Actions are executed AFTER research completes, not immediately

- **modify_pending_tp_sl_tool(symbol: str, new_tp_price: float = None, new_sl_price: float = None, reason: str)** - Modify TP/SL for pending position
  * symbol: Symbol of pending open position action (e.g., 'AAPL')
  * new_tp_price: New take profit price (None to leave unchanged)
  * new_sl_price: New stop loss price (None to leave unchanged)  
  * reason: Explanation for the modification
  * Use when: You want to adjust TP/SL levels for positions you've already recommended but haven't been executed yet

- **cancel_pending_action_tool(action_number: int)** - Cancel a specific pending action
  * action_number: 1-based action number from get_pending_actions_tool() output (e.g., 1 for "Action #1")
  * Returns: Confirmation with updated action list and renumbered actions
  * Use when: You want to remove an action you no longer want to execute

**Summary Tool (MANDATORY - call this last):**
- **finish_research_tool(summary: str)** - **REQUIRED**: Call this last with your summary after recommending actions
  * summary: Concise summary of key findings from your research (2-3 paragraphs)
  * Returns: Confirmation message

## TOOL USAGE ISSUES
**If you cannot use a tool due to ambiguity or other issues:**
- **Explicitly mention the problem** in your response before calling finish_research_tool()
- Example: "Cannot recommend closing transaction ID 123 - insufficient information about current position status"
- Example: "Cannot determine appropriate quantity for AAPL - missing current equity information"
- **Still call finish_research_tool()** with a summary that includes the issues encountered

## YOUR WORKFLOW

**You can call recommendation tools UNLIMITED TIMES** - there is NO LIMIT on the number of actions you can recommend.
- Call tools as many times as needed to address ALL risks and opportunities
- Each call adds to the accumulated list of recommended actions
- Don't stop at arbitrary limits - recommend every action that's warranted

**Typical workflow:**
1. Research analyses using research tools (call them as many times as needed)
2. Call recommendation tools (recommend_close_position, recommend_open_buy_position, etc.) for EVERY action needed
   - **NO LIMIT**: Call 5 times, 10 times, 20 times - whatever is needed
   - Actions accumulate - they don't override each other
   - Address ALL positions and opportunities, not just a few
3. Call finish_research_tool() with your summary when you've addressed everything

**Example workflow with multiple actions:**
```
Step 1: get_analysis_outputs_batch_tool([101, 102, 103, 104, 105]) → find 5 positions needing attention
Step 2: recommend_close_position(transaction_id=101, reason="...", confidence=85)
Step 3: recommend_close_position(transaction_id=102, reason="...", confidence=80)
Step 4: recommend_update_stop_loss(transaction_id=103, new_sl_price=150.0, reason="...", confidence=75)
Step 5: recommend_update_take_profit(transaction_id=104, new_tp_price=200.0, reason="...", confidence=80)
Step 6: get_historical_analyses_tool("AAPL") → discover strong buy signal
Step 7: recommend_open_buy_position(symbol="AAPL", quantity=10, reason="...", confidence=75)
Step 8: get_historical_analyses_tool("TSLA") → discover another opportunity
Step 9: recommend_open_buy_position(symbol="TSLA", quantity=5, reason="...", confidence=70)
Step 10: finish_research_tool("Addressed all 5 existing positions, opened 2 new opportunities...")
```

**CRITICAL: Complete your mandate**
- If you identify 10 positions that need action, recommend 10 actions
- If you find 20 opportunities, recommend 20 actions
- **Do NOT stop prematurely** - address everything before calling finish_research_tool()

## 🚨 CRITICAL: ACTION EXECUTION WORKFLOW 🚨
**IMPORTANT: Your recommendations are NOT executed immediately!**
- **Research phase**: You recommend actions (they are queued for later execution)
- **Action phase**: A separate process executes all your recommendations
- **Why you don't see results**: Actions happen AFTER your research session completes
- **Portfolio won't update**: You won't see new positions or closed positions during research
- **This is normal**: The system executes your recommendations automatically after you finish

**Managing your recommendations:**
- Use `get_pending_actions_tool()` to review what you've recommended so far
- Use `modify_pending_tp_sl_tool()` to adjust TP/SL levels for pending positions  
- Use `cancel_pending_action_tool()` to remove actions you no longer want
- **Don't worry about immediate results** - focus on making the right recommendations

## AVOID DUPLICATE POSITIONS - CRITICAL
**NEVER recommend opening a new position for a symbol that already has an open position:**
- ❌ WRONG: If AAPL position exists, do NOT call recommend_open_buy_position("AAPL", ...)
- ❌ WRONG: If TSLA short position exists, do NOT call recommend_open_sell_position("TSLA", ...)
- ✅ CORRECT: Use recommend_adjust_quantity() to increase an existing position instead
- ✅ CORRECT: Check the portfolio status for existing positions before recommending new ones

**NEVER recommend opposite direction positions on the same symbol:**
- ❌ WRONG: If AAPL BUY position exists, do NOT recommend open_sell_position("AAPL", ...)
- ❌ WRONG: If TSLA SELL position exists, do NOT recommend open_buy_position("TSLA", ...)
- ✅ CORRECT: Close existing position first, then open new position in opposite direction if needed

**Before recommending ANY new position:**
1. Check if the symbol already has an open position in the portfolio (any direction)
2. If it exists with SAME direction, use recommend_adjust_quantity(transaction_id, new_quantity, reason, confidence) to add to it
3. If it exists with OPPOSITE direction, recommend closing it first if you want to reverse the position
4. If it does NOT exist, then you can use recommend_open_buy_position() or recommend_open_sell_position()

**Example - CORRECT workflow:**
```
Current positions: AAPL (transaction_id=123, quantity=10, direction=BUY)
Want to add 5 more AAPL shares (same direction):
✅ CORRECT: recommend_adjust_quantity(123, 15, "Adding to position based on strong signal", 80)
❌ WRONG: recommend_open_buy_position("AAPL", 5, ...) - This would recommend a duplicate!

Want to short AAPL (opposite direction):
❌ WRONG: recommend_open_sell_position("AAPL", 10, ...) - Cannot have both BUY and SELL on same symbol!
✅ CORRECT: First recommend_close_position(123, "Reversing position", 75), then recommend_open_sell_position("AAPL", 10, ...)
```

**Why this matters:**
- Multiple positions for the same symbol complicate risk management
- Each position has separate stop loss and take profit orders
- Harder to track total exposure to a single symbol
- Opposite direction positions (long + short) create confusing hedged positions
- The action node should not have to manage duplicate positions

## 🚨 CRITICAL: AVOID DUPLICATE TP/SL CALLS 🚨
**NEVER call separate TP/SL adjustment tools after opening a position with TP/SL parameters (unless you want to change their value after creation):**

**❌ WRONG WORKFLOW - Creates Duplicate Orders:**
```
Step 1: recommend_open_buy_position(symbol="AAPL", quantity=10, tp_price=200.0, sl_price=150.0, ...)
Step 2: recommend_update_take_profit(transaction_id=XXX, new_tp_price=200.0, ...) ← DUPLICATE!
Step 3: recommend_update_stop_loss(transaction_id=XXX, new_sl_price=150.0, ...)   ← DUPLICATE!
```
**This creates 3 separate TP/SL orders for the same position, causing order cancellations and confusion.**

**✅ CORRECT WORKFLOW - Single TP/SL Creation:**
```
# Option 1: Include TP/SL in position opening (RECOMMENDED)
recommend_open_buy_position(symbol="AAPL", quantity=10, tp_price=200.0, sl_price=150.0, reason="...", confidence=75)

# Option 2: Open position without TP/SL, then set them separately
recommend_open_buy_position(symbol="AAPL", quantity=10, reason="...", confidence=75)
recommend_update_take_profit(transaction_id=XXX, new_tp_price=200.0, reason="...", confidence=75)
recommend_update_stop_loss(transaction_id=XXX, new_sl_price=150.0, reason="...", confidence=75)
```

**REMEMBER**: When you specify `tp_price` and `sl_price` in `recommend_open_buy_position()` or `recommend_open_sell_position()`, the TP/SL orders are created automatically. DO NOT make additional calls to set the same levels.

**Only use separate TP/SL adjustment tools when:**
- ✅ Modifying EXISTING positions that already have TP/SL levels
- ✅ Adding TP/SL to positions that were opened without them
- ✅ Changing TP/SL levels based on new market conditions

## CRITICAL: USE DEDICATED RECOMMENDATION TOOLS
**DO NOT just write action recommendations in text.** You MUST call the specific recommendation tools.

Example of CORRECT workflow:
```
1. Research: call get_analysis_outputs_batch_tool() to fetch data
2. Research: call get_current_price_tool() for relevant symbols  
3. Decide: Based on findings, identify specific trades
4. Recommend: recommend_open_buy_position(symbol="UBER", quantity=10, reason="...", confidence=75)
5. Recommend: recommend_close_position(transaction_id=123, reason="...", confidence=80)
6. Summarize: finish_research_tool("Found 2 high-confidence trades...")
```

Example of WRONG workflow (DO NOT DO THIS):
```
❌ finish_research_tool("I recommend buying UBER and closing position 123")  # NO! Actions not logged!
```

**IMMEDIATE ACTION PRIORITY:**
When you identify positions or opportunities that meet the user's risk criteria, you should recommend IMMEDIATE actions rather than waiting or deferring.
Examples of situations requiring immediate action:
- Stop loss levels are breached or close to being breached
- Take profit targets are reached
- Position size exceeds risk limits
- High-confidence signals (>70%) for new opportunities
- Portfolio concentration risks detected
- Significant P&L changes requiring rebalancing

Do NOT wait for "better conditions" or "more data" when risk criteria are already met.
The purpose of risk management is to ACT when triggers are hit, not to analyze indefinitely.

Focus on:
- Positions currently held in the portfolio
- High-confidence recommendations (BUY/SELL with >70% confidence)
- Recent analyses with significant profit expectations
- Risk factors and stop loss recommendations
- User's specific risk management instructions
{expert_instructions}

**REMEMBER**: 
1. Use research tools as many times as needed (no approval required)
2. Call specific recommendation tools (recommend_close_position, recommend_open_buy_position, etc.) for EVERY action
3. If you encounter tool usage issues, explicitly mention them before calling finish_research_tool()
4. Call finish_research_tool() at the end with a summary"""

ACTION_PROMPT = """You are ready to execute risk management actions.

## CRITICAL INSTRUCTION
You have been directed to the action node because a decision has been made to take action.
Your job is to EXECUTE the actions that have been determined necessary, NOT to reconsider whether to act.
To execute actions, you must call the appropriate tool as instructed below.

## CURRENT SITUATION
{portfolio_summary}

## RESEARCH FINDINGS & ACTION RATIONALE
{agent_scratchpad}

## YOUR TRADING PERMISSIONS - CRITICAL
**Know what you can and cannot do:**
- **BUY orders:** {buy_status}
- **SELL orders:** {sell_status}
- **Automated trading:** {auto_trading_status}
- **Enabled instruments:** {enabled_instruments}
- **Max position size:** {max_position_pct}% of equity per symbol

{trading_focus_guidance}

**You MUST respect these restrictions. Do not attempt actions that violate them.**

## AVAILABLE TRADING TOOLS
You have access to these trading tools. Call them directly to execute actions:

**Position Management:**
- **close_position(transaction_id: int, reason: str)** - Close an entire position {close_position_note}
  * transaction_id: ID of the Transaction to close
  * reason: Explanation for closing (for audit trail)
  * Returns: Dict with success, message, order_id, transaction_id

- **adjust_quantity(transaction_id: int, new_quantity: float, reason: str)** - Partial close or add to position {adjust_quantity_note}
  * transaction_id: ID of the position to adjust
  * new_quantity: New absolute quantity (lower = partial close, higher = add to position)
  * reason: Explanation for adjustment (for audit trail)
  * Returns: Dict with success, message, order_id, old_quantity, new_quantity

**Stop Loss / Take Profit:**
- **update_stop_loss(transaction_id: int, new_sl_price: float, reason: str)** - Update stop loss price {update_sl_tp_note}
  * transaction_id: ID of the position
  * new_sl_price: New stop loss price (must be below current price for BUY, above for SELL)
  * reason: Explanation for change (for audit trail)
  * Returns: Dict with success, message, old_sl_price, new_sl_price

- **update_take_profit(transaction_id: int, new_tp_price: float, reason: str)** - Update take profit price {update_sl_tp_note}
  * transaction_id: ID of the position
  * new_tp_price: New take profit price (must be above current price for BUY, below for SELL)
  * reason: Explanation for change (for audit trail)
  * Returns: Dict with success, message, old_tp_price, new_tp_price

**Open New Positions:**
- **open_buy_position(symbol: str, quantity: float, tp_price: float = None, sl_price: float = None, reason: str = "")** - Open new LONG (BUY) position {open_position_note}
  * symbol: Instrument symbol to buy
  * quantity: Number of shares/units to buy
  * tp_price: Optional take profit price (must be above entry price)
  * sl_price: Optional stop loss price (must be below entry price)
  * reason: Explanation for opening this position (for audit trail)
  * Returns: Dict with success, message, transaction_id, order_id, symbol, quantity, direction
  * **CRITICAL: When you provide tp_price/sl_price, TP/SL orders are created automatically. DO NOT call update_take_profit() or update_stop_loss() afterwards - this creates duplicate orders!**

- **open_sell_position(symbol: str, quantity: float, tp_price: float = None, sl_price: float = None, reason: str = "")** - Open new SHORT (SELL) position {open_position_note}
  * symbol: Instrument symbol to sell short
  * quantity: Number of shares/units to sell short
  * tp_price: Optional take profit price (must be below entry price)
  * sl_price: Optional stop loss price (must be above entry price)
  * reason: Explanation for opening this position (for audit trail)
  * Returns: Dict with success, message, transaction_id, order_id, symbol, quantity, direction
  * **CRITICAL: When you provide tp_price/sl_price, TP/SL orders are created automatically. DO NOT call update_take_profit() or update_stop_loss() afterwards - this creates duplicate orders!**

**Price Information:**
- **get_current_price(symbol: str)** - Get current bid price for an instrument
  * symbol: Instrument symbol
  * Returns: Current bid price as float

## TP/SL PRICE GUIDELINES
When setting Take Profit (TP) or Stop Loss (SL) prices:
- **Minimum distance:** TP and SL must be at least {min_tp_sl_pct}% away from the entry/current price
- **BUY positions:** TP must be ABOVE entry price, SL must be BELOW entry price
- **SELL positions:** TP must be BELOW entry price, SL must be ABOVE entry price
- **Optional parameters:** TP and SL are optional. If prices don't meet requirements, the position will still open but without that order
- **Manual adjustment:** You can always adjust TP/SL later using update_take_profit() or update_stop_loss() tools

## GUIDELINES FOR ACTIONS
{user_instructions}

## YOUR TASK
Based on the research findings and rationale above, execute the appropriate trading actions.

**CRITICAL - UNLIMITED ACTIONS:**
- **NO LIMIT on number of tool calls** - call tools as many times as needed
- You can execute 5, 10, 20+ actions in a single session - whatever is required
- Address ALL recommended actions, not just a few
- You can make multiple passes: execute some actions, then continue with more if needed

**Tool execution guidelines:**
- ✅ CORRECT: Use your tool-calling capability to execute open_buy_position(), close_position(), etc.
- ✅ CORRECT: Call multiple tools in sequence to complete all actions
- ✅ CORRECT: If you have 10 positions to close, call close_position() 10 times
- ❌ WRONG: Do NOT write Python code blocks or markdown showing what to do
- ❌ WRONG: Do NOT describe actions in text - EXECUTE them using tool calls
- ❌ WRONG: Do NOT stop after just a few actions if more are needed

DO NOT second-guess the decision to act - you are here because action is warranted.
DO NOT defer or wait for better conditions - implement ALL risk management actions now.
DO NOT write explanatory code - USE THE TOOLS DIRECTLY to execute trades.
**DO NOT stop prematurely** - if you were given 15 recommended actions, execute all 15.

## 🚨 CRITICAL: NEW POSITIONS HAVE NO TRANSACTION ID 🚨
- **When opening NEW positions, there is NO transaction_id yet** - the position must be opened first!
- **NEVER call update_take_profit() or update_stop_loss() on positions you just opened**
- **For new positions**: Use tp_price/sl_price parameters in open_buy_position() or open_sell_position()
- **For existing positions**: Use the transaction_id from the portfolio summary and call separate TP/SL tools
- **Example WRONG**: open_buy_position("AAPL", 10) then update_take_profit(999, 200.0) ← transaction 999 doesn't exist!
- **Example CORRECT**: open_buy_position("AAPL", 10, tp_price=200.0, sl_price=150.0) ← TP/SL included in position opening

## AVOID DUPLICATE POSITIONS - CRITICAL
**NEVER open a new position for a symbol that already has an open position:**
- ❌ WRONG: If AAPL position exists, do NOT call open_buy_position("AAPL", ...)
- ❌ WRONG: If TSLA short position exists, do NOT call open_sell_position("TSLA", ...)
- ✅ CORRECT: Use adjust_quantity() to increase an existing position instead
- ✅ CORRECT: Check the "FILLED Positions" list above before opening new positions

**NEVER open opposite direction positions on the same symbol:**
- ❌ WRONG: If AAPL BUY position exists, do NOT call open_sell_position("AAPL", ...)
- ❌ WRONG: If TSLA SELL position exists, do NOT call open_buy_position("TSLA", ...)
- ✅ CORRECT: Close existing position first, then open new position in opposite direction if needed

**Before opening ANY new position:**
1. Check if the symbol is already in the "FILLED Positions" list above (any direction)
2. If it exists with SAME direction, use adjust_quantity(transaction_id, new_quantity, reason) to add to it
3. If it exists with OPPOSITE direction, close it first if you want to reverse the position
4. If it does NOT exist, then you can use open_buy_position() or open_sell_position()

**Example - CORRECT workflow:**
```
Current positions: AAPL (transaction_id=123, quantity=10, direction=BUY)
Want to add 5 more AAPL shares (same direction):
✅ CORRECT: adjust_quantity(123, 15, "Adding to position based on strong signal")
❌ WRONG: open_buy_position("AAPL", 5, ...) - This creates a duplicate!

Want to short AAPL (opposite direction):
❌ WRONG: open_sell_position("AAPL", 10, ...) - Cannot have both BUY and SELL on same symbol!
✅ CORRECT: First close_position(123, "Reversing position"), then open_sell_position("AAPL", 10, ...)
```

**Why this matters:**
- Multiple positions for the same symbol complicate risk management
- Each position has separate stop loss and take profit orders
- Harder to track total exposure to a single symbol
- Opposite direction positions (long + short) create confusing hedged positions
- Can lead to unexpected behavior when closing positions

## 🚨 CRITICAL: AVOID DUPLICATE TP/SL CALLS 🚨
**NEVER call separate TP/SL adjustment tools after opening a position with TP/SL parameters:**

**❌ WRONG WORKFLOW - Creates Duplicate Orders:**
```
Step 1: open_buy_position(symbol="AAPL", quantity=10, tp_price=200.0, sl_price=150.0, ...)
Step 2: update_take_profit(transaction_id=XXX, new_tp_price=200.0, ...)  ← DUPLICATE!
Step 3: update_stop_loss(transaction_id=XXX, new_sl_price=150.0, ...)    ← DUPLICATE!
```
**This creates 3 separate TP/SL orders for the same position, causing order cancellations and confusion.**

**✅ CORRECT WORKFLOW - Single TP/SL Creation:**
```
# Option 1: Include TP/SL in position opening (RECOMMENDED)
open_buy_position(symbol="AAPL", quantity=10, tp_price=200.0, sl_price=150.0, reason="...")

# Option 2: Open position without TP/SL, then set them separately
open_buy_position(symbol="AAPL", quantity=10, reason="...")
update_take_profit(transaction_id=XXX, new_tp_price=200.0, reason="...")
update_stop_loss(transaction_id=XXX, new_sl_price=150.0, reason="...")
```

**REMEMBER**: When you specify `tp_price` and `sl_price` in `open_buy_position()` or `open_sell_position()`, the TP/SL orders are created automatically. DO NOT make additional calls to set the same levels.

## HANDLING FAILED ACTIONS - CRITICAL
**If an action fails, CONTINUE with remaining actions:**
- ✅ CORRECT: Log the failure, note it in your response, and CONTINUE to the next action
- ✅ CORRECT: If closing position 123 fails, still attempt to close positions 124, 125, 126, etc.
- ✅ CORRECT: If opening a new position fails, still update stop losses on existing positions
- ❌ WRONG: Do NOT stop the entire session because one action failed
- ❌ WRONG: Do NOT give up on all actions if the first one fails

**Failure handling pattern:**
1. Attempt action using tool call
2. If tool returns success=False or raises an error, note it in your response
3. IMMEDIATELY continue to the next action on your list
4. Complete ALL actions you can successfully execute
5. Summarize successes and failures at the end

**Example workflow with failures:**
```
Action 1: close_position(123) → ✅ Success
Action 2: close_position(124) → ❌ Failed (position not found)
Action 3: close_position(125) → ✅ Success (CONTINUE despite previous failure)
Action 4: update_stop_loss(126) → ✅ Success
Action 5: open_buy_position("AAPL") → ❌ Failed (insufficient balance)
Action 6: update_take_profit(127) → ✅ Success (CONTINUE despite previous failure)
Summary: Executed 4 out of 6 actions successfully. 2 failures noted for review.
```

**Key principle:** Maximize the number of successful actions completed, even if some fail along the way.

## TOOL USAGE ISSUES
**If you cannot use a tool due to ambiguity or other issues:**
- **Explicitly state the problem** in your response
- Example: "Cannot close position - transaction_id 123 not found in current portfolio"
- Example: "Cannot open buy position for AAPL - BUY orders are disabled"
- Example: "Cannot set take profit at $150 - current price $149 does not meet {min_tp_sl_pct}% minimum distance"
- **Attempt alternative actions** if possible (e.g., if you can't open a position, try closing risky ones instead)
- **Document the issue** clearly so it can be reviewed

**Remember:** When you want to open a position, you must CALL THE TOOL open_buy_position() or open_sell_position() directly.
The system will capture your tool calls and execute them. Text descriptions or code examples will NOT be executed.

For EACH action, provide clear reasoning that references your research findings.
"""

FINALIZATION_PROMPT = """Summarize your risk management session.

## INITIAL PORTFOLIO
{initial_portfolio_summary}

## ACTIONS TAKEN
{actions_log_summary}

## FINAL PORTFOLIO  
{final_portfolio_summary}

## TASK
Create a concise summary of:
1. Key risks identified
2. Actions taken and rationale
3. Current portfolio status
4. Any remaining concerns or recommendations

This summary will be logged for future reference.
"""

# ==================== STATE SCHEMA ====================

class SmartRiskManagerState(TypedDict):
    """State schema for Smart Risk Manager graph."""
    
    # Context
    expert_instance_id: int
    account_id: int
    user_instructions: str
    expert_settings: Dict[str, Any]  # Expert configuration (enable_buy, enable_sell, etc.)
    risk_manager_model: str
    backend_url: str  # API endpoint base URL
    api_key: str  # API key for authentication
    job_id: int  # SmartRiskManagerJob.id for tracking
    
    # Portfolio Data
    portfolio_status: Dict[str, Any]
    open_positions: List[Dict[str, Any]]
    
    # Analysis Data
    recent_analyses: List[Dict[str, Any]]
    detailed_outputs_cache: Dict[int, Dict[str, str]]  # analysis_id -> {output_key: content}
    last_risk_manager_summary: Dict[str, Any]  # Summary from previous SRM run
    
    # Agent State
    messages: List[BaseMessage]  # Message history - NO add reducer to avoid infinite loop
    research_messages: List[BaseMessage]  # Research node's isolated conversation (persists across research iterations)
    agent_scratchpad: str  # Agent's reasoning notes
    research_complete: bool  # Flag set by finish_research_tool to signal research is done
    recommended_actions: List[Dict[str, Any]]  # Actions recommended by research node for action node to execute
    
    # Actions Taken
    actions_log: List[Dict[str, Any]]  # Record of all actions executed
    
    # Loop Control
    iteration_count: int
    max_iterations: int


# ==================== NODE IMPLEMENTATIONS ====================

def create_toolkit_tools(toolkit: SmartRiskManagerToolkit) -> List:
    """
    Create LangChain tools from SmartRiskManagerToolkit methods.
    
    Returns:
        List of LangChain tools for the agent
    """
    
    @tool
    @smart_risk_manager_tool
    def get_analysis_outputs_tool(analysis_id: int) -> Dict[str, Any]:
        """Get available output keys for a specific market analysis.
        
        Args:
            analysis_id: ID of the MarketAnalysis to get outputs for
            
        Returns:
            Dictionary with analysis_id, symbol, expert, and list of output_keys
        """
        return toolkit.get_analysis_outputs(analysis_id)
    
    @tool
    @smart_risk_manager_tool
    def get_analysis_output_detail_tool(analysis_id: int, output_key: str) -> Dict[str, Any]:
        """Get detailed content of a specific analysis output.
        
        Args:
            analysis_id: ID of the MarketAnalysis
            output_key: Key of the output to retrieve (e.g., 'analyst_fundamentals_output')
            
        Returns:
            Dictionary with analysis_id, output_key, and full content
        """
        content = toolkit.get_analysis_output_detail(analysis_id, output_key)
        return {
            "analysis_id": analysis_id,
            "output_key": output_key,
            "content": content
        }
    
    @tool
    @smart_risk_manager_tool
    def get_analysis_outputs_batch_tool(analysis_ids: List[int], output_keys: List[str], max_tokens: int = 50000) -> Dict[str, Any]:
        """Fetch multiple analysis outputs efficiently in a single call.
        
        Use this instead of calling get_analysis_output_detail_tool multiple times.
        Fetches the SAME output keys from ALL specified analyses.
        Automatically handles truncation if content exceeds max_tokens limit.
        
        Args:
            analysis_ids: List of MarketAnalysis IDs to fetch from (e.g., [123, 124, 125])
            output_keys: List of output keys to fetch from each analysis (e.g., ["analysis_summary", "market_report"])
            max_tokens: Maximum tokens in response (default: 100000)
            
        Returns:
            Dictionary with:
                - outputs: List of output dicts with analysis_id, output_key, symbol, content
                - truncated: Whether truncation occurred
                - skipped_items: List of items skipped due to size/errors
                - total_chars: Total characters included
                - total_tokens_estimate: Estimated tokens
                - items_included: Count of outputs included
                - items_skipped: Count of outputs skipped
                
        Example:
            # Fetch analysis_summary and market_report from analyses 123, 124, 125
            result = get_analysis_outputs_batch_tool(
                analysis_ids=[123, 124, 125],
                output_keys=["analysis_summary", "market_report"]
            )
        """
        return toolkit.get_analysis_outputs_batch(analysis_ids, output_keys, max_tokens)
    
    @tool
    @smart_risk_manager_tool
    def get_historical_analyses_tool(symbol: str, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        """Get historical market analyses for a symbol (paginated).
        
        Args:
            symbol: Instrument symbol to query
            limit: Maximum number of analyses to return (default: 10)
            offset: Number of analyses to skip for pagination (default: 0)
            
        Returns:
            List of analysis dictionaries with id, symbol, expert, created_at
        """
        return toolkit.get_historical_analyses(symbol, limit, offset)
    
    @tool
    @smart_risk_manager_tool
    def get_all_recent_analyses_tool(max_age_hours: int = 72) -> List[Dict[str, Any]]:
        """Get ALL recent market analyses across all symbols.
        
        Use this when you want to discover what analyses are available without
        knowing which symbols to check. Perfect for exploring opportunities.
        
        Args:
            max_age_hours: Maximum age of analyses in hours (default: 72)
            
        Returns:
            List of analysis dictionaries grouped by symbol, with id, symbol, expert, created_at, summary
        """
        return toolkit.get_recent_analyses(max_age_hours=max_age_hours)
    
    @tool
    @smart_risk_manager_tool
    def get_current_price_tool(symbol: str) -> float:
        """Get current bid price for an instrument.
        
        Args:
            symbol: Instrument symbol
            
        Returns:
            Current bid price as float
        """
        return toolkit.get_current_price(symbol)
    
    @tool
    @smart_risk_manager_tool
    def close_position_tool(transaction_id: int, reason: str) -> Dict[str, Any]:
        """Close an open position completely.
        
        Args:
            transaction_id: ID of Transaction to close
            reason: Explanation for closing the position (for audit trail)
            
        Returns:
            Result dict with success, message, order_id, transaction_id
        """
        return toolkit.close_position(transaction_id, reason)
    
    @tool
    @smart_risk_manager_tool
    def adjust_quantity_tool(transaction_id: int, new_quantity: float, reason: str) -> Dict[str, Any]:
        """Adjust position size (partial close or add to position).
        
        Args:
            transaction_id: ID of the position to adjust
            new_quantity: New absolute quantity for the position
            reason: Reason for the adjustment (for audit trail)
            
        Returns:
            Result dict with success, message, order_id, old_quantity, new_quantity
        """
        return toolkit.adjust_quantity(transaction_id, new_quantity, reason)
    
    @tool
    @smart_risk_manager_tool
    def update_stop_loss_tool(transaction_id: int, new_sl_price: float, reason: str) -> Dict[str, Any]:
        """Update stop loss order for a position.
        
        Args:
            transaction_id: ID of the position to update stop loss for
            new_sl_price: New stop loss price
            reason: Reason for updating the stop loss (for audit trail)
            
        Returns:
            Result dict with success, message, old_sl_price, new_sl_price
        """
        return toolkit.update_stop_loss(transaction_id, new_sl_price, reason)
    
    @tool
    @smart_risk_manager_tool
    def update_take_profit_tool(transaction_id: int, new_tp_price: float, reason: str) -> Dict[str, Any]:
        """Update take profit order for a position.
        
        Args:
            transaction_id: ID of the position to update take profit for
            new_tp_price: New take profit price
            reason: Reason for updating the take profit (for audit trail)
            
        Returns:
            Result dict with success, message, old_tp_price, new_tp_price
        """
        return toolkit.update_take_profit(transaction_id, new_tp_price, reason)
    
    return [
        get_analysis_outputs_tool,
        get_analysis_output_detail_tool,
        get_analysis_outputs_batch_tool,
        get_historical_analyses_tool,
        get_all_recent_analyses_tool,
        get_current_price_tool,
        close_position_tool,
        adjust_quantity_tool,
        update_stop_loss_tool,
        update_take_profit_tool
    ]


def initialize_context(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Initialize the context with portfolio status and settings.
    
    Steps:
    1. Get expert instance and account
    2. Load user_instructions and risk_manager_model from settings
    3. Call get_portfolio_status()
    4. Initialize empty caches and logs
    5. Set iteration_count = 0, max_iterations = 10
    6. Create initial system message with user instructions
    """
    logger.info(f"Initializing Smart Risk Manager context for expert {state['expert_instance_id']}")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        
        # Get expert instance
        expert = get_expert_instance_from_id(expert_instance_id)
        if not expert:
            raise ValueError(f"Expert instance {expert_instance_id} not found")
        
        # Get settings using interface defaults
        settings = expert.settings
        user_instructions = expert.get_setting_with_interface_default(
            "smart_risk_manager_user_instructions", log_warning=False
        )
        
        # Get model using interface default
        risk_manager_model = expert.get_setting_with_interface_default(
            "risk_manager_model", log_warning=False
        )
        backend_url = os.getenv("OPENAI_BACKEND_URL", "https://api.openai.com/v1")
        api_key_setting_name = "openai_api_key"  # Default to OpenAI
        
        logger.info(f"Smart Risk Manager initialized with model: {risk_manager_model}")
        
        # If using NagaAI models, update backend URL
        if risk_manager_model.startswith("NagaAI/"):
            backend_url = "https://api.naga.ac/v1"
            api_key_setting_name = "naga_ai_api_key"
            risk_manager_model = risk_manager_model.replace("NagaAI/", "")
            logger.info(f"Using NagaAI backend with model: {risk_manager_model}")
        elif risk_manager_model.startswith("OpenAI/"):
            risk_manager_model = risk_manager_model.replace("OpenAI/", "")
            logger.info(f"Using OpenAI backend with model: {risk_manager_model}")
        
        # Get API key from database
        api_key = get_api_key_from_database(api_key_setting_name)
        
        max_iterations = int(expert.get_setting_with_interface_default("smart_risk_manager_max_iterations", log_warning=False))
        
        # Extract relevant expert settings for trading restrictions
        expert_config = {
            "enable_buy": expert.get_setting_with_interface_default("enable_buy"),
            "enable_sell": expert.get_setting_with_interface_default("enable_sell"),
            "enabled_instruments": expert.get_enabled_instruments() if hasattr(expert, 'get_enabled_instruments') else [],
            "max_virtual_equity_per_instrument_percent": expert.get_setting_with_interface_default("max_virtual_equity_per_instrument_percent")
        }
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Get portfolio status
        portfolio_status = toolkit.get_portfolio_status()
        open_positions = portfolio_status.get("open_positions", [])
        
        # Create or update SmartRiskManagerJob record
        job_id = state.get("job_id", 0)
        if job_id:
            # Job already created by queue worker - update it with current context
            job = get_instance(SmartRiskManagerJob, job_id)
            if not job:
                raise ValueError(f"SmartRiskManagerJob {job_id} not found")
            
            # Update job fields that may not have been set by queue
            job.model_used = risk_manager_model
            job.user_instructions = user_instructions
            job.initial_portfolio_equity = float(portfolio_status["account_virtual_equity"])
            job.final_portfolio_equity = float(portfolio_status["account_virtual_equity"])
            job.initial_available_balance = float(portfolio_status["account_available_balance"])
            job.final_available_balance = float(portfolio_status["account_available_balance"])
            job.status = "RUNNING"
            update_instance(job)
            logger.info(f"Updated existing SmartRiskManagerJob {job_id}")
        else:
            # Create new job record (legacy path when called without job_id)
            job = SmartRiskManagerJob(
                expert_instance_id=expert_instance_id,
                account_id=account_id,
                model_used=risk_manager_model,
                user_instructions=user_instructions,
                initial_portfolio_equity=float(portfolio_status["account_virtual_equity"]),
                final_portfolio_equity=float(portfolio_status["account_virtual_equity"]),
                initial_available_balance=float(portfolio_status["account_available_balance"]),
                final_available_balance=float(portfolio_status["account_available_balance"]),
                status="RUNNING"
            )
            job_id = add_instance(job)
            logger.info(f"Created SmartRiskManagerJob {job_id}")
        
        # Prepare trading permission status messages
        enable_buy = expert_config.get("enable_buy")  # Already fetched with interface defaults above
        enable_sell = expert_config.get("enable_sell")  # Already fetched with interface defaults above
        auto_trade_opening = expert.get_setting_with_interface_default("allow_automated_trade_opening")
        auto_trade_modification = expert.get_setting_with_interface_default("allow_automated_trade_modification")
        auto_trading = auto_trade_opening and auto_trade_modification
        
        buy_status = "✅ ENABLED" if enable_buy else "❌ DISABLED"
        sell_status = "✅ ENABLED" if enable_sell else "❌ DISABLED"
        auto_trading_status = "✅ ENABLED" if auto_trading else "❌ DISABLED"
        
        # Generate focused guidance based on permissions
        # Note: auto_trade_modification allows closing/modifying existing positions regardless of enable_buy/enable_sell
        # enable_buy/enable_sell only affect NEW position opening when auto_trade_opening is True
        if auto_trade_modification and auto_trade_opening:
            # Full automation enabled
            if enable_buy and enable_sell:
                trading_focus_guidance = "**Your Focus:** Full automation enabled. You can open new positions (both BUY and SELL), close existing positions, and modify them. Manage the full portfolio lifecycle."
            elif enable_buy:
                trading_focus_guidance = "**Your Focus:** You can open new LONG positions (BUY only), close any existing positions, and modify them. Focus on long entry opportunities and managing all positions."
            elif enable_sell:
                trading_focus_guidance = "**Your Focus:** You can open new SHORT positions (SELL only), close any existing positions, and modify them. Focus on short entry opportunities and managing all positions."
            else:
                trading_focus_guidance = "**Your Focus:** You can close and modify existing positions, but cannot open new ones (both BUY and SELL disabled). Focus on managing existing positions only."
        elif auto_trade_modification:
            # Can modify/close existing positions but not open new ones
            trading_focus_guidance = "**Your Focus:** You can close and modify existing positions (update stop-loss, take-profit, adjust quantities), but cannot open new positions. Focus on managing existing positions: closing losing trades, taking profits, and adjusting protective orders."
        elif auto_trade_opening:
            # Can open new positions but not modify existing ones
            if enable_buy and enable_sell:
                trading_focus_guidance = "**Your Focus:** You can open new positions (both BUY and SELL), but cannot close or modify existing ones. Focus on new entry opportunities only."
            elif enable_buy:
                trading_focus_guidance = "**Your Focus:** You can open new LONG positions (BUY only), but cannot close or modify existing ones. Focus on long entry opportunities only."
            elif enable_sell:
                trading_focus_guidance = "**Your Focus:** You can open new SHORT positions (SELL only), but cannot close or modify existing ones. Focus on short entry opportunities only."
            else:
                trading_focus_guidance = "**Your Focus:** Both BUY and SELL are disabled. You cannot perform any trading actions. Focus on analysis only."
        else:
            # No automation enabled
            trading_focus_guidance = "**Your Focus:** Automated trading is DISABLED. You can only provide analysis and recommendations, but cannot execute any trades."
        
        # Create initial system message
        system_msg = SystemMessage(content=SYSTEM_INITIALIZATION_PROMPT.format(
            user_instructions=user_instructions,
            buy_status=buy_status,
            sell_status=sell_status,
            auto_trading_status=auto_trading_status,
            trading_focus_guidance=trading_focus_guidance
        ))
        
        return {
            "expert_instance_id": expert_instance_id,
            "account_id": account_id,
            "user_instructions": user_instructions,
            "expert_settings": expert_config,
            "risk_manager_model": risk_manager_model,
            "backend_url": backend_url,
            "api_key": api_key,
            "job_id": job_id,
            "portfolio_status": portfolio_status,
            "open_positions": open_positions,
            "recent_analyses": [],
            "detailed_outputs_cache": {},
            "last_risk_manager_summary": {},
            "messages": [system_msg],
            "agent_scratchpad": "",
            "recommended_actions": [],
            "actions_log": [],
            "iteration_count": 0,
            "max_iterations": max_iterations
        }
        
    except Exception as e:
        logger.error(f"Error initializing Smart Risk Manager context: {e}", exc_info=True)
        raise


def analyze_portfolio(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Analyze current portfolio and generate initial assessment.
    
    Steps:
    1. Get summary from last risk manager run (for continuity)
    2. Calculate portfolio-level metrics
    3. Identify positions with significant P&L
    4. Check risk concentrations
    5. Generate prompt for LLM to assess portfolio health
    """
    logger.info("Analyzing portfolio...")
    
    try:
        portfolio_status = state["portfolio_status"]
        risk_manager_model = state["risk_manager_model"]
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Get summary from last risk manager run
        last_run_summary = toolkit.get_last_risk_manager_summary()
        
        # Build context about previous run
        previous_run_context = ""
        if last_run_summary.get("job_id"):
            previous_run_context = f"""

## Previous Risk Manager Run
Run Date: {last_run_summary['run_date']}
Actions Taken: {last_run_summary['actions_taken_count']}
Portfolio Change: ${last_run_summary['initial_equity']:.2f} → ${last_run_summary['final_equity']:.2f}

Previous Research Findings:
{last_run_summary['research_findings'] or 'No research findings available'}

Previous Final Summary:
{last_run_summary['final_summary'] or 'No final summary available'}
"""
        
        # Create LLM
        llm = create_llm(risk_manager_model, 0.1, backend_url, api_key)
        
        # Build portfolio summary for prompt
        num_open_positions = len(state['open_positions'])
        num_pending_positions = portfolio_status.get('risk_metrics', {}).get('num_pending', 0)
        total_positions = num_open_positions + num_pending_positions
        
        portfolio_summary = f"""
Total Virtual Equity: ${portfolio_status['account_virtual_equity']:.2f}
Available Balance (includes pending positions): ${portfolio_status['account_available_balance']:.2f} ({portfolio_status['account_balance_pct_available']:.1f}%)

FILLED Positions (Live Trades - {num_open_positions} total):
"""
        # Collect valid transaction IDs for emphasis
        valid_transaction_ids = []
        for pos in state["open_positions"]:
            valid_transaction_ids.append(str(pos['transaction_id']))
            portfolio_summary += f"\n- Transaction #{pos['transaction_id']}: {pos['symbol']}: {pos['quantity']} shares @ ${pos['current_price']:.2f}"
            portfolio_summary += f" | P&L: {pos['unrealized_pnl_pct']:.2f}% (${pos['unrealized_pnl']:.2f})"
            
            # Show SL/TP status explicitly
            sl_order = pos.get("sl_order")
            tp_order = pos.get("tp_order")
            
            if sl_order and sl_order.get("price"):
                portfolio_summary += f" | SL: ${sl_order['price']:.2f}"
            else:
                portfolio_summary += f" | ⚠️ NO STOP LOSS"
            
            if tp_order and tp_order.get("price"):
                portfolio_summary += f" | TP: ${tp_order['price']:.2f}"
            else:
                portfolio_summary += f" | NO TAKE PROFIT"
        
        # Add pending positions if any
        pending_positions = portfolio_status.get('pending_positions', [])
        if pending_positions:
            portfolio_summary += f"\n\nPENDING Positions (Orders sent to broker but NOT yet filled - {num_pending_positions} total):"
            portfolio_summary += f"\nValue: ${portfolio_status.get('pending_transactions_value', 0):.2f} ({portfolio_status.get('pending_transactions_pct', 0):.1f}% of equity)"
            for pending in pending_positions:
                portfolio_summary += f"\n- Transaction #{pending['transaction_id']}: {pending['symbol']}: {pending['pending_quantity']} shares (est. ${pending['estimated_price']:.2f}, value: ${pending['estimated_value']:.2f})"
        else:
            portfolio_summary += "\n(No pending positions)"
        
        # Add summary line
        portfolio_summary += f"\n\n📊 PORTFOLIO SUMMARY: {num_open_positions} FILLED + {num_pending_positions} PENDING = {total_positions} TOTAL POSITIONS"
        
        # Add prominent reminder about valid transaction IDs (include both filled and pending)
        all_transaction_ids = valid_transaction_ids.copy()
        if pending_positions:
            all_transaction_ids.extend([str(p['transaction_id']) for p in pending_positions])
        
        if all_transaction_ids:
            portfolio_summary += f"\n\n🚨 VALID TRANSACTION IDs FOR ACTIONS: {', '.join(all_transaction_ids)} 🚨"
            portfolio_summary += "\nYou can ONLY modify these transaction IDs. Do NOT use IDs from previous runs or other experts."
        else:
            portfolio_summary += "\n\n(No transactions available)"
        
        # Get LLM analysis
        analysis_prompt = PORTFOLIO_ANALYSIS_PROMPT.format(
            portfolio_status=portfolio_summary + previous_run_context
        )
        
        response = llm.invoke([
            *state["messages"],
            HumanMessage(content=analysis_prompt)
        ])
        
        # Update scratchpad with analysis
        # Handle case where agent_scratchpad might be a list (LangGraph state issue)
        current_scratchpad = state["agent_scratchpad"]
        if isinstance(current_scratchpad, list):
            current_scratchpad = "\n".join(str(item) for item in current_scratchpad)
        
        # Handle case where response.content might be a list
        response_content = response.content
        if isinstance(response_content, list):
            response_content = "\n".join(str(item) for item in response_content)
        
        scratchpad = current_scratchpad + "\n\n## Initial Portfolio Analysis\n" + response_content
        
        # Store last run summary in state for research node access
        # Manually append messages to existing list (no add reducer)
        state_update = {
            "messages": state["messages"] + [HumanMessage(content=analysis_prompt), response],
            "agent_scratchpad": scratchpad,
            "last_risk_manager_summary": last_run_summary
        }
        
        logger.info("Portfolio analysis complete")
        
        return state_update
        
    except Exception as e:
        logger.error(f"Error analyzing portfolio: {e}", exc_info=True)
        job_id = state.get("job_id")
        if job_id:
            mark_job_as_failed(job_id, f"Error in analyze_portfolio: {str(e)}")
        raise


def check_recent_analyses(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Load recent market analyses for ALL symbols (not just open positions).
    
    The risk manager needs to see the full picture of recent research across all instruments
    to identify:
    - New opportunities to open positions
    - Relevant market context for existing positions
    - Sector/market trends affecting multiple symbols
    
    Steps:
    1. Call get_recent_analyses() once to get all recent analyses (no symbol filter)
    2. Store in recent_analyses
    3. Add summaries to agent_scratchpad
    4. Route directly to research_node for detailed investigation
    """
    logger.info("Checking recent market analyses for all symbols...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # PRE-CACHE: Get symbols with recent analyses and bulk-fetch prices BEFORE calling get_recent_analyses
        # This prevents individual price API calls during get_analysis_summary() execution
        from ..core.db import get_db
        from sqlmodel import Session, select
        from ..core.models import MarketAnalysis
        from datetime import datetime, timedelta, timezone
        
        with get_db() as session:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=72)
            symbols_query = select(MarketAnalysis.symbol).where(
                MarketAnalysis.expert_instance_id == expert_instance_id,
                MarketAnalysis.created_at >= cutoff_time
            ).distinct()
            symbols_to_precache = list(session.exec(symbols_query).all())
        
        if symbols_to_precache:
            logger.info(f"Pre-caching prices for {len(symbols_to_precache)} symbols before fetching analyses")
            try:
                # Bulk fetch ALL bid and ask prices in 2 API calls (populates cache)
                toolkit.account.get_instrument_current_price(symbols_to_precache, price_type='bid')
                toolkit.account.get_instrument_current_price(symbols_to_precache, price_type='ask')
                logger.info(f"✅ Successfully pre-cached prices for {len(symbols_to_precache)} symbols (2 API calls)")
            except Exception as precache_err:
                logger.warning(f"Price pre-caching failed: {precache_err}")
        
        # Fetch ALL recent analyses (no symbol filter)
        # Now get_analysis_summary() will use cached prices instead of making individual API calls
        all_analyses = toolkit.get_recent_analyses(max_age_hours=72)
        
        # Build summary for scratchpad
        analyses_summary = f"\n\n## Recent Market Analyses (Last 72 hours)\n"
        analyses_summary += f"Total analyses available: {len(all_analyses)}\n\n"
        
        # Load ExpertRecommendation data from database for all analyses
        # This avoids parsing summaries - we get structured data directly from the model
        analysis_recommendations = {}
        with get_db() as session:
            from sqlmodel import select
            from ..core.models import ExpertRecommendation
            
            analysis_ids = [a['analysis_id'] for a in all_analyses]
            if analysis_ids:
                # Fetch all recommendations for these analyses
                rec_query = select(ExpertRecommendation).where(
                    ExpertRecommendation.market_analysis_id.in_(analysis_ids)
                )
                recommendations = session.exec(rec_query).all()
                
                # Map analysis_id -> recommendation for quick lookup
                for rec in recommendations:
                    if rec.market_analysis_id:
                        analysis_recommendations[rec.market_analysis_id] = rec
        
        # First group by action type (BUY/SELL/HOLD), then by symbol
        by_action_and_symbol = {'BUY': {}, 'SELL': {}, 'HOLD': {}, 'UNKNOWN': {}}
        for analysis in all_analyses:
            sym = analysis['symbol']
            analysis_id = analysis['analysis_id']
            
            # Get recommendation from database instead of parsing summary
            recommendation = analysis_recommendations.get(analysis_id)
            if recommendation:
                action = recommendation.recommended_action.value  # OrderRecommendation enum
                confidence = recommendation.confidence
                expected_profit = recommendation.expected_profit_percent
                risk_level = recommendation.risk_level.value if hasattr(recommendation.risk_level, 'value') else str(recommendation.risk_level)
                time_horizon = recommendation.time_horizon.value if hasattr(recommendation.time_horizon, 'value') else str(recommendation.time_horizon)
                
                # Store structured recommendation data
                rec_details = {
                    'action': action,
                    'confidence': confidence if confidence is not None else 0.0,
                    'expected_profit': expected_profit,
                    'risk_level': risk_level,
                    'term': time_horizon.replace('_', ' ').title()
                }
                
                # Check if SL/TP data exists in recommendation.data
                if recommendation.data:
                    rec_details['sl_price'] = recommendation.data.get('stop_loss_price')
                    rec_details['tp_price'] = recommendation.data.get('take_profit_price')
            else:
                action = 'UNKNOWN'
                rec_details = {}
            
            if action not in by_action_and_symbol:
                by_action_and_symbol[action] = {}
            
            if sym not in by_action_and_symbol[action]:
                by_action_and_symbol[action][sym] = []
            
            # Attach recommendation details to analysis for easier access
            analysis['rec_details'] = rec_details
            by_action_and_symbol[action][sym].append(analysis)
        
        # Batch fetch prices for all symbols using bulk API call (reduces API calls dramatically)
        all_symbols = list(set(analysis['symbol'] for analysis in all_analyses))
        
        if all_symbols:
            logger.info(f"Batch fetching bid/ask prices for {len(all_symbols)} symbols using bulk API")
            try:
                # Fetch ALL bid prices in a single API call
                bid_prices = toolkit.account.get_instrument_current_price(all_symbols, price_type='bid')
                # Fetch ALL ask prices in a single API call
                ask_prices = toolkit.account.get_instrument_current_price(all_symbols, price_type='ask')
                
                logger.info(f"Successfully bulk-fetched prices for {len(all_symbols)} symbols (2 API calls total)")
            except Exception as batch_err:
                logger.warning(f"Error in bulk price fetching: {batch_err}")
                bid_prices = {}
                ask_prices = {}
        else:
            bid_prices = {}
            ask_prices = {}
        
        # Show summary grouped by action type, then symbol
        action_labels = {
            'BUY': '🟢 Strong BUY Signals',
            'SELL': '🔴 SELL Signals',
            'HOLD': '🟡 HOLD Recommendations',
            'UNKNOWN': '⚪ Other Analyses'
        }
        
        for action in ['BUY', 'SELL', 'HOLD', 'UNKNOWN']:
            symbols_dict = by_action_and_symbol[action]
            if not symbols_dict:
                continue
            
            analyses_summary += f"\n### {action_labels[action]}\n"
            
            for sym, analyses in symbols_dict.items():
                # Get price from bulk-fetched cache
                price_info = ""
                bid_price = bid_prices.get(sym)
                ask_price = ask_prices.get(sym)
                
                if bid_price and ask_price:
                    price_info = f" (current price: bid: {bid_price:.2f} / ask: {ask_price:.2f})"
                elif bid_price:
                    price_info = f" (current price: {bid_price:.2f})"
                
                analyses_summary += f"\n**{sym}**{price_info}: {len(analyses)} analysis(es)\n"
                for analysis in analyses[:3]:  # Show up to 3 analyses per symbol (increased from 2 for history)
                    rec_details = analysis['rec_details']
                    
                    # Format timestamp with relative time
                    timestamp_str = analysis['timestamp']
                    relative_time = _format_relative_time(timestamp_str)
                    time_display = f"{timestamp_str} ({relative_time})" if relative_time else timestamp_str
                    
                    # Format with recommendation details from database
                    if rec_details and rec_details.get('action'):
                        # Build SL/TP info if available
                        sl_tp_info = ""
                        sl_price = rec_details.get('sl_price')
                        tp_price = rec_details.get('tp_price')
                        
                        if sl_price or tp_price:
                            parts = []
                            if sl_price:
                                parts.append(f"SL: ${sl_price:.2f}")
                            if tp_price:
                                parts.append(f"TP: ${tp_price:.2f}")
                            sl_tp_info = f" | {' | '.join(parts)}"
                        
                        # Build risk info if available
                        risk_info = ""
                        if rec_details.get('risk_level'):
                            risk_info = f" | Risk: {rec_details['risk_level']}"
                        
                        analyses_summary += (
                            f"  [{time_display}] [Analysis #{analysis['analysis_id']}] {analysis['expert_name']}\n"
                            f"    → Confidence: {rec_details['confidence']:.1f}% | "
                            f"Expected Profit: {rec_details.get('expected_profit', 0.0):.1f}% | "
                            f"Term: {rec_details['term']}{sl_tp_info}{risk_info}\n"
                        )
                    else:
                        # Fallback to simple format (no recommendation data available)
                        analyses_summary += f"  [{time_display}] [Analysis #{analysis['analysis_id']}] {analysis['expert_name']}\n"
        
        # Handle case where agent_scratchpad might be a list
        current_scratchpad = state["agent_scratchpad"]
        if isinstance(current_scratchpad, list):
            current_scratchpad = "\n".join(str(item) for item in current_scratchpad)
        scratchpad = current_scratchpad + analyses_summary
        
        logger.info(f"Found {len(all_analyses)} recent analyses across {len(all_symbols)} symbols - routing to research_node")
        
        return {
            "recent_analyses": all_analyses,
            "agent_scratchpad": scratchpad
        }
        
    except Exception as e:
        logger.error(f"Error checking recent analyses: {e}", exc_info=True)
        job_id = state.get("job_id")
        if job_id:
            mark_job_as_failed(job_id, f"Error in check_recent_analyses: {str(e)}")
        raise


# agent_decision_loop function REMOVED - no longer needed in sequential flow


def initialize_research_agent(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Initialize research LLM and tools ONCE for the entire research session.
    This node runs once before research iterations begin.
    
    Creates:
    - Research tools (bound to toolkit instance)
    - LLM with tools bound
    - Initial system prompt and conversation
    
    Returns these in state so research_node can reuse them across iterations.
    """
    logger.info("Initializing research agent (LLM + tools)...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        risk_manager_model = state["risk_manager_model"]
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        expert_settings = state["expert_settings"]
        portfolio_status = state["portfolio_status"]
        
        # Calculate position size limits
        # Use account_virtual_equity (expert's total allocation), not available_balance
        # Virtual equity = account_balance * virtual_equity_pct (e.g., 5% of total)
        # Available balance = virtual_equity - already_deployed_trades
        # Position sizing should be against virtual_equity (the expert's allocation)
        expert = get_expert_instance_from_id(expert_instance_id)
        if not expert:
            raise ValueError(f"Expert instance {expert_instance_id} not found")
        max_position_pct = expert.get_setting_with_interface_default("max_virtual_equity_per_instrument_percent")
        current_equity = float(portfolio_status.get("account_virtual_equity", 0))
        max_position_equity = current_equity * (max_position_pct / 100.0)
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Track recommended actions in closure scope (persists across research iterations)
        recommended_actions_list = []
        
        # Create research-specific tools with closure over recommended_actions_list
        # These tool definitions will be available to research_node via state
        research_tools = create_research_tools(toolkit, recommended_actions_list)
        
        # Create LLM and bind tools
        llm = create_llm(risk_manager_model, 0.2, backend_url, api_key)
        # Get parallel_tool_calls setting from expert (default False for safety)
        parallel_tool_calls = expert.get_setting_with_interface_default("smart_risk_manager_parallel_tool_calls", log_warning=False)
        llm_with_tools = llm.bind_tools(research_tools, parallel_tool_calls=parallel_tool_calls)
        
        # Get expert-specific instructions if available
        expert_instructions = ""
        try:
            expert_inst = get_expert_instance_from_id(expert_instance_id)
            if expert_inst and hasattr(expert_inst, 'get_expert_specific_instructions'):
                expert_instructions = expert_inst.get_expert_specific_instructions("research_node")
                if expert_instructions:
                    logger.info(f"Added expert-specific instructions for research_node ({len(expert_instructions)} chars)")
        except Exception as e:
            logger.warning(f"Could not get expert-specific instructions: {e}")
        
        # Format expert instructions with newline only if present
        formatted_expert_instructions = f"\n\n{expert_instructions}" if expert_instructions else ""
        
        # Use global RESEARCH_PROMPT with dynamic context
        agent_scratchpad_content = state.get('agent_scratchpad', 'No prior context')
        
        research_system_prompt = RESEARCH_PROMPT.format(
            agent_scratchpad=agent_scratchpad_content,
            expert_instructions=formatted_expert_instructions,
            max_position_pct=max_position_pct,
            max_position_equity=max_position_equity
        )

        # Initialize conversation with system prompt
        research_messages = [
            SystemMessage(content=research_system_prompt),
            HumanMessage(content="Begin your research. Investigate the most relevant analyses and gather detailed information.")
        ]
        
        logger.info(f"Research agent initialized with {len(research_tools)} available tools")
        
        # Store initialized research session data in state
        # NOTE: We DON'T store llm_with_tools or research_tools directly in state
        # Instead, we'll use them via closure in research_node_factory
        return {
            "research_messages": research_messages,
            "research_complete": False,
            "recommended_actions": []  # Will be populated during research
        }
        
    except Exception as e:
        logger.error(f"Error initializing research agent: {e}", exc_info=True)
        raise


def create_research_tools(toolkit: SmartRiskManagerToolkit, recommended_actions_list: List) -> List:
    """
    Create research tools that have access to the toolkit and recommended_actions_list.
    This function is called once during initialization, and the tools persist across iterations.
    """
    
    @tool
    @smart_risk_manager_tool
    def get_analysis_outputs_tool(
        analysis_id: Annotated[int, "ID of the MarketAnalysis to get outputs for"]
    ) -> Dict[str, Any]:
        """Get available output keys for a specific market analysis.
        
        Args:
            analysis_id: ID of the MarketAnalysis to get outputs for
            
        Returns:
            Dictionary with analysis_id, symbol, expert, and list of output_keys
        """
        return toolkit.get_analysis_outputs(analysis_id)
    
    @tool
    @smart_risk_manager_tool
    def get_analysis_output_detail_tool(
        analysis_id: Annotated[int, "ID of the MarketAnalysis"],
        output_key: Annotated[str, "Key of the output to retrieve (e.g., 'final_trade_decision', 'technical_analysis')"]
    ) -> Dict[str, Any]:
        """Get detailed content of a specific analysis output.
        
        Args:
            analysis_id: ID of the MarketAnalysis
            output_key: Key of the output to retrieve (e.g., 'final_trade_decision')
            
        Returns:
            Dictionary with analysis_id, output_key, and full content
        """
        content = toolkit.get_analysis_output_detail(analysis_id, output_key)
        return {
            "analysis_id": analysis_id,
            "output_key": output_key,
            "content": content
        }
    
    @tool
    @smart_risk_manager_tool
    def get_analysis_outputs_batch_tool(
        analysis_ids: Annotated[List[int], "List of MarketAnalysis IDs to fetch from (e.g., [123, 124, 125])"],
        output_keys: Annotated[List[str], "List of output keys to fetch from each analysis (e.g., ['analysis_summary', 'market_report'])"],
        max_tokens: Annotated[Optional[int], "Maximum tokens in response (default: 50000)"] = None
    ) -> Dict[str, Any]:
        """Fetch multiple analysis outputs efficiently in a single call.
        
        Use this instead of calling get_analysis_output_detail_tool multiple times.
        Fetches the SAME output keys from ALL specified analyses.
        Automatically handles truncation if content exceeds max_tokens limit.
        
        Args:
            analysis_ids: List of MarketAnalysis IDs to fetch from (e.g., [123, 124, 125])
            output_keys: List of output keys to fetch from each analysis (e.g., ["analysis_summary", "market_report"])
            max_tokens: Maximum tokens in response (default: 50000)
            
        Returns:
            Dictionary with outputs, truncated status, and metadata
            
        Example:
            # Fetch analysis_summary and market_report from analyses 123, 124, 125
            result = get_analysis_outputs_batch_tool(
                analysis_ids=[123, 124, 125],
                output_keys=["analysis_summary", "market_report"]
            )
        """
        # Use default value if None is passed
        if max_tokens is None:
            max_tokens = 50000
        return toolkit.get_analysis_outputs_batch(analysis_ids, output_keys, max_tokens)
    
    @tool
    @smart_risk_manager_tool
    def get_historical_analyses_tool(
        symbol: Annotated[str, "Instrument symbol to query (e.g., 'AAPL', 'MSFT')"],
        limit: Annotated[Optional[int], "Maximum number of analyses to return (default: 10)"] = None,
        offset: Annotated[Optional[int], "Number of analyses to skip for pagination (default: 0)"] = None
    ) -> List[Dict[str, Any]]:
        """Get historical market analyses for a symbol (paginated).
        
        Args:
            symbol: Instrument symbol to query
            limit: Maximum number of analyses to return (default: 10)
            offset: Number of analyses to skip for pagination (default: 0)
            
        Returns:
            List of analysis dictionaries with id, symbol, expert, created_at
        """
        # Use default values if None is passed
        if limit is None:
            limit = 10
        if offset is None:
            offset = 0
        return toolkit.get_historical_analyses(symbol, limit, offset)
    
    @tool
    @smart_risk_manager_tool
    def get_all_recent_analyses_tool(
        max_age_hours: Annotated[Optional[int], "Maximum age of analyses in hours (default: 72)"] = None
    ) -> List[Dict[str, Any]]:
        """Get ALL recent market analyses across all symbols.
        
        Use this when you want to discover what analyses are available without
        knowing which symbols to check. Perfect for exploring opportunities.
        
        Args:
            max_age_hours: Maximum age of analyses in hours (default: 72)
            
        Returns:
            List of analysis dictionaries grouped by symbol, with id, symbol, expert, created_at, summary
        """
        # Use default value if None is passed
        if max_age_hours is None:
            max_age_hours = 72
        return toolkit.get_recent_analyses(max_age_hours=max_age_hours)
    
    @tool
    @smart_risk_manager_tool
    def get_current_price_tool(
        symbol: Annotated[str, "Instrument symbol (e.g., 'AAPL', 'MSFT')"]
    ) -> float:
        """Get current bid price for an instrument.
        
        Args:
            symbol: Instrument symbol
            
        Returns:
            Current bid price as float
        """
        return toolkit.get_current_price(symbol)
    
    @tool
    @smart_risk_manager_tool
    def finish_research_tool(
        summary: Annotated[str, "Concise summary of key findings from your research (2-3 paragraphs)"]
    ) -> str:
        """Call this when you have gathered enough information and are ready to return to decision making.
        
        Args:
            summary: Concise summary of key findings from your research (2-3 paragraphs)
            
        Returns:
            Confirmation message
        """
        return f"Research complete. Summary recorded: {summary[:100]}..."
    
    @tool
    @smart_risk_manager_tool
    def recommend_close_position(
        transaction_id: Annotated[int, "ID of the open transaction/position to close"],
        reason: Annotated[str, "Clear explanation referencing your research findings (e.g., 'Analysis #456 shows bearish reversal')"],
        confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
    ) -> str:
        """Recommend closing an existing position based on your research.
        
        Use this when research indicates a position should be exited due to:
        - Stop loss being hit or close to being hit
        - Take profit target reached
        - Changed market conditions making the position risky
        - Portfolio rebalancing needs
        
        IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
        
        Returns:
            Confirmation message
        """
        action = {
            "action_type": "close_position",
            "parameters": {"transaction_id": transaction_id},
            "reason": reason,
            "confidence": confidence
        }
        recommended_actions_list.append(action)
        return f"Recorded close position recommendation for transaction {transaction_id}. Total actions: {len(recommended_actions_list)}"
    
    @tool
    @smart_risk_manager_tool
    def recommend_adjust_quantity(
        transaction_id: Annotated[int, "ID of the position to adjust"],
        new_quantity: Annotated[int, "New quantity for the position - MUST be whole number (e.g., 10, not 10.5)"],
        reason: Annotated[str, "Clear explanation for the quantity adjustment"],
        confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
    ) -> str:
        """Recommend adjusting the quantity/size of an existing position.
        
        Use this when research suggests scaling position size up or down.
        
        IMPORTANT: 
        - new_quantity must be a whole number (integer) like 10, not 10.5
        - confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
        
        Returns:
            Confirmation message
        """
        action = {
            "action_type": "adjust_quantity",
            "parameters": {"transaction_id": transaction_id, "new_quantity": new_quantity},
            "reason": reason,
            "confidence": confidence
        }
        recommended_actions_list.append(action)
        return f"Recorded quantity adjustment for transaction {transaction_id} to {new_quantity}. Total actions: {len(recommended_actions_list)}"
    
    @tool
    @smart_risk_manager_tool
    def recommend_update_stop_loss(
        transaction_id: Annotated[int, "ID of the position to update stop loss for"],
        new_sl_price: Annotated[float, "New stop loss price level"],
        reason: Annotated[str, "Clear explanation for the stop loss adjustment"],
        confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
    ) -> str:
        """Recommend updating the stop loss price for an existing position.
        
        Use this when research suggests tightening or loosening stop loss levels.
        
        IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
        
        Returns:
            Confirmation message
        """
        action = {
            "action_type": "update_stop_loss",
            "parameters": {"transaction_id": transaction_id, "new_sl_price": new_sl_price},
            "reason": reason,
            "confidence": confidence
        }
        recommended_actions_list.append(action)
        return f"Recorded stop loss update for transaction {transaction_id} to {new_sl_price}. Total actions: {len(recommended_actions_list)}"
    
    @tool
    @smart_risk_manager_tool
    def recommend_update_take_profit(
        transaction_id: Annotated[int, "ID of the position to update take profit for"],
        new_tp_price: Annotated[float, "New take profit price level"],
        reason: Annotated[str, "Clear explanation for the take profit adjustment"],
        confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
    ) -> str:
        """Recommend updating the take profit price for an existing position.
        
        Use this when research suggests adjusting profit target levels.
        
        IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
        
        Returns:
            Confirmation message
        """
        action = {
            "action_type": "update_take_profit",
            "parameters": {"transaction_id": transaction_id, "new_tp_price": new_tp_price},
            "reason": reason,
            "confidence": confidence
        }
        recommended_actions_list.append(action)
        return f"Recorded take profit update for transaction {transaction_id} to {new_tp_price}. Total actions: {len(recommended_actions_list)}"
    
    @tool
    @smart_risk_manager_tool
    def recommend_open_buy_position(
        symbol: Annotated[str, "Instrument symbol to buy (e.g., 'AAPL', 'MSFT')"],
        quantity: Annotated[int, "Number of shares/units to buy (MUST be a whole number, e.g., 10, not 10.5)"],
        reason: Annotated[str, "Clear explanation for opening this position based on research"],
        confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"],
        tp_price: Annotated[Optional[float], "Take profit price level (optional)"] = None,
        sl_price: Annotated[Optional[float], "Stop loss price level (optional)"] = None
    ) -> str:
        """Recommend opening a new BUY (long) position based on research.
        
        Use this when research indicates a strong buying opportunity.
        
        IMPORTANT: 
        - quantity must be a whole number (integer) like 10, not 10.5
        - confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85)
        
        Returns:
            Confirmation message
        """
        action = {
            "action_type": "open_buy_position",
            "parameters": {
                "symbol": symbol,
                "quantity": quantity,
                "tp_price": tp_price,
                "sl_price": sl_price
            },
            "reason": reason,
            "confidence": confidence
        }
        recommended_actions_list.append(action)
        return f"Recorded buy position recommendation for {symbol} ({quantity} shares). Total actions: {len(recommended_actions_list)}"
    
    @tool
    @smart_risk_manager_tool
    def recommend_open_sell_position(
        symbol: Annotated[str, "Instrument symbol to sell short (e.g., 'AAPL', 'MSFT')"],
        quantity: Annotated[int, "Number of shares/units to sell short (MUST be a whole number, e.g., 10, not 10.5)"],
        reason: Annotated[str, "Clear explanation for opening this short position based on research"],
        confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"],
        tp_price: Annotated[Optional[float], "Take profit price level (optional)"] = None,
        sl_price: Annotated[Optional[float], "Stop loss price level (optional)"] = None
    ) -> str:
        """Recommend opening a new SELL (short) position based on research.
        
        Use this when research indicates a strong short-selling opportunity.
        
        IMPORTANT: 
        - quantity must be a whole number (integer) like 10, not 10.5
        - confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85)
        
        Returns:
            Confirmation message
        """
        """Recommend opening a new SELL (short) position based on research.
        
        Use this when research indicates a strong short-selling opportunity.
        
        IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
        
        Returns:
            Confirmation message
        """
        action = {
            "action_type": "open_sell_position",
            "parameters": {
                "symbol": symbol,
                "quantity": quantity,
                "tp_price": tp_price,
                "sl_price": sl_price
            },
            "reason": reason,
            "confidence": confidence
        }
        recommended_actions_list.append(action)
        return f"Recorded sell position recommendation for {symbol} ({quantity} shares). Total actions: {len(recommended_actions_list)}"
    
    @tool
    @smart_risk_manager_tool
    def get_all_transactions_tool(
        format_type: Annotated[str, "Output format: 'markdown' (default for reading) or 'json' (for structured data)"] = "markdown"
    ) -> str:
        """Get comprehensive view of all transactions: filled + pending + future actions.
        
        This tool provides a complete snapshot including:
        1. **FILLED positions** - Live trades currently in portfolio
        2. **PENDING positions** - Orders sent to broker awaiting fill
        3. **FUTURE actions** - Recommended actions not yet executed (from research analysis)
        
        Use this to see:
        - The full picture of current portfolio state
        - What actions have been recommended so far
        - All transaction IDs available for modification
        
        Format options:
        - "markdown": Human-readable format (recommended for research)
        - "json": Structured data format (for data processing)
        
        Args:
            format_type: "markdown" (default) or "json"
            
        Returns:
            Formatted transaction summary including pending recommended actions
        """
        # Pass the current recommended_actions_list to include pending actions
        return toolkit.get_all_transactions(
            include_pending_actions=True,
            pending_actions=recommended_actions_list.copy(),
            format_type=format_type
        )
    
    @tool
    @smart_risk_manager_tool
    def get_pending_actions_tool() -> str:
        """Get list of all currently recommended actions that are queued for execution.
        
        Use this to:
        - Review what actions you have already recommended
        - Check if you need to modify or cancel any pending actions
        - Avoid recommending duplicate actions
        
        Returns:
            Formatted list of pending actions with their numbers for reference
        """
        if not recommended_actions_list:
            return "No pending actions. All recommendations have been cleared or none have been made yet."
        
        result = "## PENDING ACTIONS (Queued for Execution)\n\n"
        result += f"**Total actions queued: {len(recommended_actions_list)}**\n\n"
        
        for idx, action in enumerate(recommended_actions_list):
            action_num = idx + 1
            action_type = action.get("action_type", "unknown")
            params = action.get("parameters", {})
            reason = action.get("reason", "No reason provided")
            confidence = action.get("confidence", 0)
            
            result += f"**Action #{action_num}: {action_type}**\n"
            
            if action_type == "open_buy_position":
                symbol = params.get("symbol", "N/A")
                quantity = params.get("quantity", 0)
                tp_price = params.get("tp_price")
                sl_price = params.get("sl_price")
                result += f"- Symbol: {symbol}\n"
                result += f"- Quantity: {quantity} shares\n"
                result += f"- TP: ${tp_price:.2f}\n" if tp_price else "- TP: Not set\n"
                result += f"- SL: ${sl_price:.2f}\n" if sl_price else "- SL: Not set\n"
            elif action_type == "open_sell_position":
                symbol = params.get("symbol", "N/A")
                quantity = params.get("quantity", 0)
                tp_price = params.get("tp_price")
                sl_price = params.get("sl_price")
                result += f"- Symbol: {symbol} (SHORT)\n"
                result += f"- Quantity: {quantity} shares\n"
                result += f"- TP: ${tp_price:.2f}\n" if tp_price else "- TP: Not set\n"
                result += f"- SL: ${sl_price:.2f}\n" if sl_price else "- SL: Not set\n"
            elif action_type in ["close_position", "adjust_quantity", "update_stop_loss", "update_take_profit"]:
                transaction_id = params.get("transaction_id", "N/A")
                result += f"- Transaction ID: {transaction_id}\n"
                if action_type == "adjust_quantity":
                    result += f"- New Quantity: {params.get('new_quantity', 'N/A')}\n"
                elif action_type == "update_stop_loss":
                    result += f"- New Stop Loss: ${params.get('new_sl_price', 0):.2f}\n"
                elif action_type == "update_take_profit":
                    result += f"- New Take Profit: ${params.get('new_tp_price', 0):.2f}\n"
            
            result += f"- Reason: {reason}\n"
            result += f"- Confidence: {confidence}%\n\n"
        
        result += "**Note**: These actions will be executed when the research phase completes. "
        result += "Use cancel_pending_action() to remove actions or modify_pending_tp_sl() to adjust TP/SL levels.\n"
        
        return result
    
    @tool
    @smart_risk_manager_tool
    def modify_pending_tp_sl_tool(
        symbol: Annotated[str, "Symbol of the pending position to modify (e.g., 'AAPL')"],
        new_tp_price: Annotated[Optional[float], "New take profit price (optional, use None to remove)"] = None,
        new_sl_price: Annotated[Optional[float], "New stop loss price (optional, use None to remove)"] = None,
        reason: Annotated[str, "Reason for modifying the TP/SL levels"] = "TP/SL adjustment"
    ) -> str:
        """Modify take profit and/or stop loss levels for pending position actions.
        
        Use this to adjust TP/SL levels for positions you have already recommended to open
        but haven't been executed yet. This is useful when market conditions change
        or you want to fine-tune the risk management levels.
        
        Args:
            symbol: Symbol to modify (must have a pending open position action)
            new_tp_price: New take profit price (None to remove TP)
            new_sl_price: New stop loss price (None to remove SL) 
            reason: Explanation for the modification
            
        Returns:
            Confirmation message
        """
        # Find pending open position actions for this symbol
        matching_actions = []
        for idx, action in enumerate(recommended_actions_list):
            if (action.get("action_type") in ["open_buy_position", "open_sell_position"] and 
                action.get("parameters", {}).get("symbol") == symbol):
                matching_actions.append((idx, action))
        
        if not matching_actions:
            return f"❌ No pending open position actions found for symbol {symbol}. Use get_pending_actions_tool() to see current pending actions."
        
        modified_count = 0
        for idx, action in matching_actions:
            # Update the TP/SL prices in the action parameters
            if new_tp_price is not None:
                recommended_actions_list[idx]["parameters"]["tp_price"] = new_tp_price
            if new_sl_price is not None:
                recommended_actions_list[idx]["parameters"]["sl_price"] = new_sl_price
                
            # Update the reason to include modification note
            original_reason = recommended_actions_list[idx].get("reason", "")
            recommended_actions_list[idx]["reason"] = f"{original_reason} | Modified TP/SL: {reason}"
            modified_count += 1
        
        tp_msg = f"TP=${new_tp_price:.2f}" if new_tp_price is not None else "TP=unchanged"
        sl_msg = f"SL=${new_sl_price:.2f}" if new_sl_price is not None else "SL=unchanged"
        
        return f"✅ Modified {modified_count} pending action(s) for {symbol}: {tp_msg}, {sl_msg}. Total actions: {len(recommended_actions_list)}"
    
    @tool
    @smart_risk_manager_tool
    def cancel_pending_action_tool(
        action_number: Annotated[int, "Action number from get_pending_actions_tool() (1-based index)"]
    ) -> str:
        """Cancel a specific pending action by its number.
        
        Use this to remove actions you no longer want to execute.
        The action number corresponds to the "Action #X" shown in get_pending_actions_tool().
        
        Args:
            action_number: 1-based action number (e.g., 1 for "Action #1")
            
        Returns:
            Confirmation message with updated action list
        """
        if not recommended_actions_list:
            return "❌ No pending actions to cancel."
        
        if action_number < 1 or action_number > len(recommended_actions_list):
            return f"❌ Invalid action number {action_number}. Valid range: 1-{len(recommended_actions_list)}. Use get_pending_actions_tool() to see current actions."
        
        # Convert 1-based to 0-based index
        idx = action_number - 1
        cancelled_action = recommended_actions_list.pop(idx)
        
        action_type = cancelled_action.get("action_type", "unknown")
        params = cancelled_action.get("parameters", {})
        
        # Format cancelled action info
        if action_type in ["open_buy_position", "open_sell_position"]:
            symbol = params.get("symbol", "N/A")
            direction = "BUY" if action_type == "open_buy_position" else "SELL"
            action_info = f"{direction} {symbol}"
        else:
            transaction_id = params.get("transaction_id", "N/A")
            action_info = f"{action_type} for transaction {transaction_id}"
        
        result = f"✅ Cancelled Action #{action_number}: {action_info}\n\n"
        
        # Show updated action list with new numbers
        if recommended_actions_list:
            result += "**Updated pending actions (renumbered):**\n"
            for idx, action in enumerate(recommended_actions_list):
                new_action_num = idx + 1
                action_type = action.get("action_type", "unknown")
                params = action.get("parameters", {})
                
                if action_type in ["open_buy_position", "open_sell_position"]:
                    symbol = params.get("symbol", "N/A")
                    direction = "BUY" if action_type == "open_buy_position" else "SELL"
                    result += f"Action #{new_action_num}: {direction} {symbol}\n"
                else:
                    transaction_id = params.get("transaction_id", "N/A")
                    result += f"Action #{new_action_num}: {action_type} for transaction {transaction_id}\n"
        else:
            result += "**No pending actions remaining.**\n"
        
        result += f"\n**Total remaining actions: {len(recommended_actions_list)}**"
        
        return result
    
    return [
        get_analysis_outputs_tool,
        get_analysis_output_detail_tool,
        get_analysis_outputs_batch_tool,
        get_historical_analyses_tool,
        get_all_recent_analyses_tool,
        get_current_price_tool,
        get_all_transactions_tool,
        get_pending_actions_tool,
        modify_pending_tp_sl_tool,
        cancel_pending_action_tool,
        recommend_close_position,
        recommend_adjust_quantity,
        recommend_update_stop_loss,
        recommend_update_take_profit,
        recommend_open_buy_position,
        recommend_open_sell_position,
        finish_research_tool
    ]


def research_node(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Research mode - autonomous agent with INTERNAL iteration loop.
    
    Uses an internal loop instead of graph-based iteration for better performance
    and simpler LLM session management. The LLM session persists naturally across
    all iterations within this single function call.
    
    Steps:
    1. Create isolated conversation context for research
    2. Give research agent access to all research tools
    3. INTERNAL LOOP: Iteratively call tools to gather data (up to max_iterations)
    4. If no actions recommended, ask LLM why
    5. Return summary and recommended actions to action_node
    """
    logger.info("Entering research mode - autonomous research agent with internal loop...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        risk_manager_model = state["risk_manager_model"]
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        expert_settings = state["expert_settings"]
        portfolio_status = state["portfolio_status"]
        max_iterations = state["max_iterations"]
        
        # Calculate position size limits
        # Use account_virtual_equity (expert's total allocation), not available_balance
        # Virtual equity = account_balance * virtual_equity_pct (e.g., 5% of total)
        # Available balance = virtual_equity - already_deployed_trades
        # Position sizing should be against virtual_equity (the expert's allocation)
        expert = get_expert_instance_from_id(expert_instance_id)
        if not expert:
            raise ValueError(f"Expert instance {expert_instance_id} not found")
        max_position_pct = expert.get_setting_with_interface_default("max_virtual_equity_per_instrument_percent")
        current_equity = float(portfolio_status.get("account_virtual_equity", 0))
        max_position_equity = current_equity * (max_position_pct / 100.0)
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Track recommended actions in closure scope (persists across loop iterations)
        recommended_actions_list = []
        
        # Create research-specific tools
        @tool
        @smart_risk_manager_tool
        def get_analysis_outputs_tool(
            analysis_id: Annotated[int, "ID of the MarketAnalysis to get outputs for"]
        ) -> Dict[str, Any]:
            """Get available output keys for a specific market analysis.
            
            Args:
                analysis_id: ID of the MarketAnalysis to get outputs for
                
            Returns:
                Dictionary with analysis_id, symbol, expert, and list of output_keys
            """
            return toolkit.get_analysis_outputs(analysis_id)
        
        @tool
        @smart_risk_manager_tool
        def get_analysis_output_detail_tool(
            analysis_id: Annotated[int, "ID of the MarketAnalysis"],
            output_key: Annotated[str, "Key of the output to retrieve (e.g., 'final_trade_decision', 'technical_analysis')"]
        ) -> Dict[str, Any]:
            """Get detailed content of a specific analysis output.
            
            Args:
                analysis_id: ID of the MarketAnalysis
                output_key: Key of the output to retrieve (e.g., 'final_trade_decision')
                
            Returns:
                Dictionary with analysis_id, output_key, and full content
            """
            content = toolkit.get_analysis_output_detail(analysis_id, output_key)
            return {
                "analysis_id": analysis_id,
                "output_key": output_key,
                "content": content
            }
        
        @tool
        @smart_risk_manager_tool
        def get_analysis_outputs_batch_tool(
            analysis_ids: Annotated[List[int], "List of MarketAnalysis IDs to fetch from (e.g., [123, 124, 125])"],
            output_keys: Annotated[List[str], "List of output keys to fetch from each analysis (e.g., ['analysis_summary', 'market_report'])"],
            max_tokens: Annotated[Optional[int], "Maximum tokens in response (default: 50000)"] = None
        ) -> Dict[str, Any]:
            """Fetch multiple analysis outputs efficiently in a single call.
            
            Use this instead of calling get_analysis_output_detail_tool multiple times.
            Fetches the SAME output keys from ALL specified analyses.
            Automatically handles truncation if content exceeds max_tokens limit.
            
            Args:
                analysis_ids: List of MarketAnalysis IDs to fetch from (e.g., [123, 124, 125])
                output_keys: List of output keys to fetch from each analysis (e.g., ["analysis_summary", "market_report"])
                max_tokens: Maximum tokens in response (default: 50000)
                
            Returns:
                Dictionary with outputs, truncated status, and metadata
                
            Example:
                # Fetch analysis_summary and market_report from analyses 123, 124, 125
                result = get_analysis_outputs_batch_tool(
                    analysis_ids=[123, 124, 125],
                    output_keys=["analysis_summary", "market_report"]
                )
            """
            # Use default value if None is passed
            if max_tokens is None:
                max_tokens = 50000
            return toolkit.get_analysis_outputs_batch(analysis_ids, output_keys, max_tokens)
        
        @tool
        @smart_risk_manager_tool
        def get_historical_analyses_tool(
            symbol: Annotated[str, "Instrument symbol to query (e.g., 'AAPL', 'MSFT')"],
            limit: Annotated[Optional[int], "Maximum number of analyses to return (default: 10)"] = None,
            offset: Annotated[Optional[int], "Number of analyses to skip for pagination (default: 0)"] = None
        ) -> List[Dict[str, Any]]:
            """Get historical market analyses for a symbol (paginated).
            
            Args:
                symbol: Instrument symbol to query
                limit: Maximum number of analyses to return (default: 10)
                offset: Number of analyses to skip for pagination (default: 0)
                
            Returns:
                List of analysis dictionaries with id, symbol, expert, created_at
            """
            # Use default values if None is passed
            if limit is None:
                limit = 10
            if offset is None:
                offset = 0
            return toolkit.get_historical_analyses(symbol, limit, offset)
        
        @tool
        @smart_risk_manager_tool
        def get_all_recent_analyses_tool(
            max_age_hours: Annotated[Optional[int], "Maximum age of analyses in hours (default: 72)"] = None
        ) -> List[Dict[str, Any]]:
            """Get ALL recent market analyses across all symbols.
            
            Use this when you want to discover what analyses are available without
            knowing which symbols to check. Perfect for exploring opportunities.
            
            Args:
                max_age_hours: Maximum age of analyses in hours (default: 72)
                
            Returns:
                List of analysis dictionaries grouped by symbol, with id, symbol, expert, created_at, summary
            """
            # Use default value if None is passed
            if max_age_hours is None:
                max_age_hours = 72
            return toolkit.get_recent_analyses(max_age_hours=max_age_hours)
        
        @tool
        @smart_risk_manager_tool
        def get_current_price_tool(
            symbol: Annotated[str, "Instrument symbol (e.g., 'AAPL', 'MSFT')"]
        ) -> float:
            """Get current bid price for an instrument.
            
            Args:
                symbol: Instrument symbol
                
            Returns:
                Current bid price as float
            """
            return toolkit.get_current_price(symbol)
        
        @tool
        @smart_risk_manager_tool
        def finish_research_tool(
            summary: Annotated[str, "Concise summary of key findings from your research (2-3 paragraphs)"]
        ) -> str:
            """Call this when you have gathered enough information and are ready to return to decision making.
            
            Args:
                summary: Concise summary of key findings from your research (2-3 paragraphs)
                
            Returns:
                Confirmation message
            """
            return f"Research complete. Summary recorded: {summary[:100]}..."
        
        @tool
        @smart_risk_manager_tool
        def recommend_close_position(
            transaction_id: Annotated[int, "ID of the open transaction/position to close"],
            reason: Annotated[str, "Clear explanation referencing your research findings (e.g., 'Analysis #456 shows bearish reversal')"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
        ) -> str:
            """Recommend closing an existing position based on your research.
            
            Use this when research indicates a position should be exited due to:
            - Stop loss being hit or close to being hit
            - Take profit target reached
            - Changed market conditions making the position risky
            - Portfolio rebalancing needs
            
            IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            
            Returns:
                Confirmation message
            """
            action = {
                "action_type": "close_position",
                "parameters": {"transaction_id": transaction_id},
                "reason": reason,
                "confidence": confidence
            }
            recommended_actions_list.append(action)
            return f"Recorded close position recommendation for transaction {transaction_id}. Total actions: {len(recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_adjust_quantity(
            transaction_id: Annotated[int, "ID of the position to adjust"],
            new_quantity: Annotated[int, "New quantity for the position - MUST be whole number (e.g., 10, not 10.5)"],
            reason: Annotated[str, "Clear explanation for the quantity adjustment"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
        ) -> str:
            """Recommend adjusting the quantity/size of an existing position.
            
            Use this when research suggests scaling position size up or down.
            
            IMPORTANT: 
            - new_quantity must be a whole number (integer) like 10, not 10.5
            - confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            
            Returns:
                Confirmation message
            """
            action = {
                "action_type": "adjust_quantity",
                "parameters": {"transaction_id": transaction_id, "new_quantity": new_quantity},
                "reason": reason,
                "confidence": confidence
            }
            recommended_actions_list.append(action)
            return f"Recorded quantity adjustment for transaction {transaction_id} to {new_quantity}. Total actions: {len(recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_update_stop_loss(
            transaction_id: Annotated[int, "ID of the position to update stop loss for"],
            new_sl_price: Annotated[float, "New stop loss price level"],
            reason: Annotated[str, "Clear explanation for the stop loss adjustment"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
        ) -> str:
            """Recommend updating the stop loss price for an existing position.
            
            Use this when research suggests tightening or loosening stop loss levels.
            
            IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            
            Returns:
                Confirmation message
            """
            action = {
                "action_type": "update_stop_loss",
                "parameters": {"transaction_id": transaction_id, "new_sl_price": new_sl_price},
                "reason": reason,
                "confidence": confidence
            }
            recommended_actions_list.append(action)
            return f"Recorded stop loss update for transaction {transaction_id} to {new_sl_price}. Total actions: {len(recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_update_take_profit(
            transaction_id: Annotated[int, "ID of the position to update take profit for"],
            new_tp_price: Annotated[float, "New take profit price level"],
            reason: Annotated[str, "Clear explanation for the take profit adjustment"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
        ) -> str:
            """Recommend updating the take profit price for an existing position.
            
            Use this when research suggests adjusting profit target levels.
            
            IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            
            Returns:
                Confirmation message
            """
            action = {
                "action_type": "update_take_profit",
                "parameters": {"transaction_id": transaction_id, "new_tp_price": new_tp_price},
                "reason": reason,
                "confidence": confidence
            }
            recommended_actions_list.append(action)
            return f"Recorded take profit update for transaction {transaction_id} to {new_tp_price}. Total actions: {len(recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_open_buy_position(
            symbol: Annotated[str, "Instrument symbol to buy (e.g., 'AAPL', 'MSFT')"],
            quantity: Annotated[float, "Number of shares/units to buy"],
            reason: Annotated[str, "Clear explanation for opening this position based on research"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"],
            tp_price: Annotated[Optional[float], "Take profit price level (optional)"] = None,
            sl_price: Annotated[Optional[float], "Stop loss price level (optional)"] = None
        ) -> str:
            """Recommend opening a new BUY (long) position based on research.
            
            Use this when research indicates a strong buying opportunity.
            
            IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            
            Returns:
                Confirmation message
            """
            action = {
                "action_type": "open_buy_position",
                "parameters": {
                    "symbol": symbol,
                    "quantity": quantity,
                    "tp_price": tp_price,
                    "sl_price": sl_price
                },
                "reason": reason,
                "confidence": confidence
            }
            recommended_actions_list.append(action)
            return f"Recorded buy position recommendation for {symbol} ({quantity} shares). Total actions: {len(recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_open_sell_position(
            symbol: Annotated[str, "Instrument symbol to sell short (e.g., 'AAPL', 'MSFT')"],
            quantity: Annotated[float, "Number of shares/units to sell short"],
            reason: Annotated[str, "Clear explanation for opening this short position based on research"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"],
            tp_price: Annotated[Optional[float], "Take profit price level (optional)"] = None,
            sl_price: Annotated[Optional[float], "Stop loss price level (optional)"] = None
        ) -> str:
            """Recommend opening a new SELL (short) position based on research.
            
            Use this when research indicates a strong short-selling opportunity.
            
            IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            
            Returns:
                Confirmation message
            """
            action = {
                "action_type": "open_sell_position",
                "parameters": {
                    "symbol": symbol,
                    "quantity": quantity,
                    "tp_price": tp_price,
                    "sl_price": sl_price
                },
                "reason": reason,
                "confidence": confidence
            }
            recommended_actions_list.append(action)
            return f"Recorded sell position recommendation for {symbol} ({quantity} shares). Total actions: {len(recommended_actions_list)}"
        
        research_tools = [
            get_analysis_outputs_tool,
            get_analysis_output_detail_tool,
            get_analysis_outputs_batch_tool,
            get_historical_analyses_tool,
            get_all_recent_analyses_tool,
            get_current_price_tool,
            recommend_close_position,
            recommend_adjust_quantity,
            recommend_update_stop_loss,
            recommend_update_take_profit,
            recommend_open_buy_position,
            recommend_open_sell_position,
            finish_research_tool
        ]
        
        # Get expert-specific instructions if available
        expert_instructions = ""
        try:
            expert_inst = get_expert_instance_from_id(expert_instance_id)
            if expert_inst and hasattr(expert_inst, 'get_expert_specific_instructions'):
                expert_instructions = expert_inst.get_expert_specific_instructions("research_node")
                if expert_instructions:
                    logger.info(f"Added expert-specific instructions for research_node ({len(expert_instructions)} chars)")
        except Exception as e:
            logger.warning(f"Could not get expert-specific instructions: {e}")
        
        # Format expert instructions with newline only if present
        formatted_expert_instructions = f"\n\n{expert_instructions}" if expert_instructions else ""
        
        # Use global RESEARCH_PROMPT with dynamic context
        agent_scratchpad_content = state.get('agent_scratchpad', 'No prior context')
        
        research_system_prompt = RESEARCH_PROMPT.format(
            agent_scratchpad=agent_scratchpad_content,
            expert_instructions=formatted_expert_instructions,
            max_position_pct=max_position_pct,
            max_position_equity=max_position_equity
        )

        # Initialize conversation with system prompt
        research_messages = [
            SystemMessage(content=research_system_prompt),
            HumanMessage(content="Begin your research. Investigate the most relevant analyses and gather detailed information.")
        ]
        logger.info(f"Research agent initialized with {len(research_tools)} available tools")
        
        # Create LLM with tools ONCE (reused across all loop iterations)
        llm = create_llm(risk_manager_model, 0.2, backend_url, api_key)
        # Get parallel_tool_calls setting from expert (default False for safety)
        parallel_tool_calls = expert.get_setting_with_interface_default("smart_risk_manager_parallel_tool_calls", log_warning=False)
        llm_with_tools = llm.bind_tools(research_tools, parallel_tool_calls=parallel_tool_calls)
        
        detailed_cache = state["detailed_outputs_cache"].copy()
        research_complete = False
        final_summary = ""
        
        # =================== INTERNAL ITERATION LOOP ===================
        for iteration in range(1, max_iterations + 1):
            logger.info("=" * 70)
            logger.info(f"RESEARCH ITERATION {iteration}/{max_iterations}")
            logger.info("=" * 70)
            
            # Get LLM response with tool calls
            try:
                response = llm_with_tools.invoke(research_messages)
            except Exception as e:
                logger.error(f"LLM invocation error on iteration {iteration}: {e}", exc_info=True)
                # Try to continue with error message
                research_messages.append(HumanMessage(content=f"Error occurred: {str(e)}. Please continue with available information."))
                continue
            
            research_messages.append(response)
            
            logger.info(f"Research iteration {iteration}: LLM returned {len(response.tool_calls) if response.tool_calls else 0} tool calls")
            
            # Check if research is complete
            if not response.tool_calls:
                logger.info("Research agent finished without tool calls")
                final_summary = response.content
                research_complete = True
                break
            
            # Execute tool calls
            for tool_call in response.tool_calls:
                tool_name = tool_call.get("name")
                tool_args = tool_call.get("args", {})
                tool_call_id = truncate_tool_call_id(tool_call.get("id"))  # Truncate to comply with OpenAI limit
                
                logger.debug(f"Research tool: {tool_name} with args {tool_args}")
                
                # Check for finish signal
                if tool_name == "finish_research_tool":
                    final_summary = tool_args.get("summary", response.content)
                    research_complete = True
                    research_messages.append(ToolMessage(
                        content="Research complete. Proceeding to action execution.",
                        tool_call_id=tool_call_id,
                        name=tool_name
                    ))
                    break
                
                # Execute tool
                matching_tool = next((t for t in research_tools if t.name == tool_name), None)
                if matching_tool:
                    try:
                        logger.info(f"🔧 Research Tool Call: {tool_name} | Args: {json.dumps(tool_args)}")
                        result = matching_tool.invoke(tool_args)
                        result_preview = str(result)[:200] if not isinstance(result, dict) else f"dict with {len(result)} keys"
                        logger.info(f"✅ Research Tool Result: {tool_name} | {result_preview}")
                        
                        # Cache analysis output details
                        if tool_name == "get_analysis_output_detail_tool":
                            analysis_id = tool_args.get("analysis_id")
                            output_key = tool_args.get("output_key")
                            if analysis_id and output_key:
                                if analysis_id not in detailed_cache:
                                    detailed_cache[analysis_id] = {}
                                detailed_cache[analysis_id][output_key] = result.get("content", "")
                        
                        # Add tool result to conversation
                        result_str = json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
                        research_messages.append(ToolMessage(
                            content=result_str,
                            tool_call_id=tool_call_id,
                            name=tool_name
                        ))
                        
                    except Exception as e:
                        logger.error(f"Error executing research tool {tool_name}: {e}", exc_info=True)
                        research_messages.append(ToolMessage(
                            content=f"Error: {str(e)}",
                            tool_call_id=tool_call_id,
                            name=tool_name
                        ))
                else:
                    logger.warning(f"Tool {tool_name} not found in research_tools")
                    research_messages.append(ToolMessage(
                        content=f"Error: Tool {tool_name} not available",
                        tool_call_id=tool_call_id,
                        name=tool_name
                    ))
            
            # If research complete after tool execution, exit loop
            if research_complete:
                break
            
            # After processing all tool calls, add reminder if not at last iteration
            if iteration < max_iterations:
                # Build iteration counter and actions summary
                iteration_info = f"\n\n{'='*70}\n**ITERATION {iteration} of {max_iterations} COMPLETE**\n{'='*70}\n"
                
                # Build summary of actions recommended so far
                actions_summary = f"\n**ACTIONS RECOMMENDED SO FAR**: {len(recommended_actions_list)} total\n"
                if recommended_actions_list:
                    for idx, action in enumerate(recommended_actions_list, 1):
                        action_type = action.get("action_type", "unknown")
                        confidence = action.get("confidence", 0)
                        reason = action.get("reason", "No reason provided")[:100]  # Truncate long reasons
                        actions_summary += f"  {idx}. {action_type} (confidence: {confidence}%) - {reason}\n"
                    actions_summary += "\n**NEXT STEP**: Call finish_research_tool to proceed with executing these actions.\n"
                else:
                    actions_summary += "  ⚠️ No actions recommended yet.\n"
                    actions_summary += "  - If you need more information, continue researching with available tools.\n"
                    actions_summary += "  - If you have enough information, recommend appropriate actions (open/close/adjust positions).\n"
                    actions_summary += "  - If no actions are needed, explain why in finish_research_tool's summary.\n"
                    actions_summary += "  - Only call finish_research_tool after recommending actions OR explaining why none are needed.\n"
                
                # Add a reminder message to the conversation to prompt completion
                reminder_msg = (
                    iteration_info +
                    actions_summary +
                    "\n**REMINDER**: When you have gathered sufficient information and made your recommendations, "
                    "you MUST call the finish_research_tool to complete your research and proceed to action execution. "
                    "Without calling finish_research_tool, the system will continue iterating."
                )
                # Append reminder to the last tool message
                if research_messages and isinstance(research_messages[-1], ToolMessage):
                    last_tool_msg = research_messages[-1]
                    research_messages[-1] = ToolMessage(
                        content=last_tool_msg.content + reminder_msg,
                        tool_call_id=last_tool_msg.tool_call_id,
                        name=last_tool_msg.name if hasattr(last_tool_msg, 'name') and last_tool_msg.name else "reminder_tool"
                    )
        
        # =================== END OF ITERATION LOOP ===================
        
        logger.info(f"Research loop complete after {iteration} iterations")
        logger.info(f"Research node has {len(recommended_actions_list)} recommended actions")
        logger.info("=" * 70)
        
        # If no actions were recommended, ask the LLM why
        no_action_explanation = ""
        if len(recommended_actions_list) == 0:
            logger.warning("No actions recommended - asking LLM for explanation...")
            
            try:
                # Create a simple LLM without tools for this query
                llm_no_tools = create_llm(risk_manager_model, 0.2, backend_url, api_key)
                
                explanation_prompt = HumanMessage(content="""You completed your research but did not recommend any actions.
                
Please explain:
1. Why did you not recommend any trading actions?
2. What were the key factors in your decision to take no action?
3. What conditions would need to change for you to recommend actions?

Provide a concise 2-3 paragraph explanation.""")
                
                research_messages.append(explanation_prompt)
                explanation_response = llm_no_tools.invoke(research_messages)
                no_action_explanation = explanation_response.content
                
                logger.info("=" * 70)
                logger.info("LLM EXPLANATION FOR NO ACTIONS:")
                logger.info("=" * 70)
                logger.info(no_action_explanation)
                logger.info("=" * 70)
                
            except Exception as e:
                logger.error(f"Failed to get explanation for no actions: {e}", exc_info=True)
                no_action_explanation = f"Failed to get explanation: {str(e)}"
        
        # Update scratchpad with research summary
        # Handle case where agent_scratchpad might be a list
        current_scratchpad = state["agent_scratchpad"]
        if isinstance(current_scratchpad, list):
            current_scratchpad = "\n".join(str(item) for item in current_scratchpad)
        
        if final_summary:
            updated_scratchpad = current_scratchpad + f"\n\n## Research Findings\n{final_summary}\n"
        else:
            updated_scratchpad = current_scratchpad + f"\n\n## Research Findings\nResearch completed after {iteration} iterations.\n"
        
        # Add no-action explanation to scratchpad if present
        if no_action_explanation:
            updated_scratchpad += f"\n\n## No Action Explanation\n{no_action_explanation}\n"
        
        # Store research findings in job record for UI display
        job_id = state.get("job_id")
        if job_id:
            try:
                with get_db() as session:
                    job = session.get(SmartRiskManagerJob, job_id)
                    if job:
                        # Store research findings in graph_state for later retrieval
                        # CRITICAL: Create new dict to ensure SQLAlchemy detects the change
                        current_state = job.graph_state or {}
                        new_state = {
                            **current_state,
                            "research_findings": final_summary or f"Research completed after {iteration} iterations",
                            "recommended_actions_count": len(recommended_actions_list),
                            "no_action_explanation": no_action_explanation if no_action_explanation else None
                        }
                        job.graph_state = new_state
                        session.add(job)
                        session.commit()
                        logger.debug(f"Stored research findings in job {job_id}")
            except Exception as e:
                logger.warning(f"Failed to store research findings in job: {e}")
        
        # Return updated state - always proceed to action_node now (no more graph loop)
        return {
            "messages": state["messages"],  # Keep parent conversation (don't overwrite)
            "detailed_outputs_cache": detailed_cache,
            "agent_scratchpad": updated_scratchpad,
            "research_complete": True,  # Always true when exiting this node
            "recommended_actions": recommended_actions_list,  # Always pass the list (may be empty)
            "iteration_count": state["iteration_count"] + iteration  # Update total count
        }
        
    except Exception as e:
        logger.error(f"Error in research node: {e}", exc_info=True)
        job_id = state.get("job_id")
        if job_id:
            mark_job_as_failed(job_id, f"Error in research_node: {str(e)}")
        raise


def action_node(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Action execution node - Pure Python execution of recommended actions.
    
    NO LLM CALLS - This node only executes actions that were decided by the research_node.
    The research_node does all LLM-based reasoning and decision making, then provides
    a structured list of actions to execute.
    
    Steps:
    1. Get recommended_actions from state (provided by research_node)
    2. Execute each action using toolkit methods
    3. Record results in actions_log
    4. Update portfolio_status with new data
    5. Return to finalize_node
    """
    logger.info("Entering action execution mode (pure Python, no LLM)...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Get recommended actions from research node
        recommended_actions = state.get("recommended_actions", [])
        
        if not recommended_actions:
            logger.warning("No recommended actions provided by research node - skipping action execution")
            # Return with no actions taken - manually append to messages
            return {
                "actions_log": state["actions_log"],
                "iteration_count": state["iteration_count"] + 1,
                "messages": state["messages"] + [
                    HumanMessage(content="No actions to execute."),
                    AIMessage(content="No actions were recommended by the research phase.")
                ]
            }
        
        logger.info(f"Executing {len(recommended_actions)} actions recommended by research node")
        
        # Initialize actions log and detailed reports
        actions_log = state["actions_log"].copy()
        detailed_action_reports = []
        
        # OPTIMIZATION: Group TP/SL actions by transaction_id to combine them into single adjust_tp_sl calls
        tp_sl_grouped_actions = {}  # transaction_id -> {"tp_action": action, "sl_action": action}
        other_actions = []
        
        for action in recommended_actions:
            action_type = action.get("action_type")
            if action_type in ["update_stop_loss", "update_take_profit"]:
                transaction_id = action.get("parameters", {}).get("transaction_id")
                if transaction_id:
                    if transaction_id not in tp_sl_grouped_actions:
                        tp_sl_grouped_actions[transaction_id] = {"tp_action": None, "sl_action": None}
                    
                    if action_type == "update_take_profit":
                        tp_sl_grouped_actions[transaction_id]["tp_action"] = action
                    else:  # update_stop_loss
                        tp_sl_grouped_actions[transaction_id]["sl_action"] = action
                else:
                    # No transaction_id, treat as regular action
                    other_actions.append(action)
            else:
                other_actions.append(action)
        
        # Convert grouped TP/SL actions back to optimized action list
        optimized_actions = []
        for transaction_id, grouped in tp_sl_grouped_actions.items():
            tp_action = grouped["tp_action"]
            sl_action = grouped["sl_action"]
            
            if tp_action and sl_action:
                # Both TP and SL - combine into single adjust_tp_sl action
                combined_action = {
                    "action_type": "adjust_tp_sl",
                    "parameters": {
                        "transaction_id": transaction_id,
                        "new_tp_price": tp_action["parameters"]["new_tp_price"],
                        "new_sl_price": sl_action["parameters"]["new_sl_price"]
                    },
                    "reason": f"TP: {tp_action.get('reason', 'No reason')}, SL: {sl_action.get('reason', 'No reason')}",
                    "confidence": max(tp_action.get("confidence", 0), sl_action.get("confidence", 0))
                }
                optimized_actions.append(combined_action)
                logger.info(f"Optimization: Combined TP and SL adjustments for transaction {transaction_id} into single adjust_tp_sl call")
            elif tp_action:
                # Only TP
                optimized_actions.append(tp_action)
            elif sl_action:
                # Only SL
                optimized_actions.append(sl_action)
        
        # Add all other actions
        optimized_actions.extend(other_actions)
        
        logger.info(f"Optimized {len(recommended_actions)} actions into {len(optimized_actions)} actions (combined TP/SL where possible)")
        
        # Execute each optimized action using toolkit methods directly (Pure Python - No LLM)
        for idx, action in enumerate(optimized_actions):
                action_type = action.get("action_type")
                parameters = action.get("parameters", {})
                reason = action.get("reason", "Recommended by research node")
                confidence = action.get("confidence", 0)
                
                logger.info(f"Executing recommended action {idx+1}/{len(recommended_actions)}: {action_type} (confidence: {confidence}%)")
                
                try:
                    result = None
                    
                    # Execute the appropriate toolkit method based on action_type
                    if action_type == "close_position":
                        transaction_id = parameters["transaction_id"]
                        result = toolkit.close_position(transaction_id, reason)
                        
                    elif action_type == "adjust_quantity":
                        transaction_id = parameters["transaction_id"]
                        new_quantity = parameters["new_quantity"]
                        
                        # CRITICAL: Ensure new_quantity is a whole number (Alpaca requires integers for GTC orders)
                        if not isinstance(new_quantity, int) and new_quantity != int(new_quantity):
                            logger.warning(f"⚠️ Fractional quantity detected for transaction {transaction_id}: {new_quantity} - rounding to {int(new_quantity)}")
                        new_quantity = int(new_quantity)
                        
                        result = toolkit.adjust_quantity(transaction_id, new_quantity, reason)
                        
                    elif action_type == "update_stop_loss":
                        transaction_id = parameters["transaction_id"]
                        new_sl_price = parameters["new_sl_price"]
                        
                        # Validate transaction exists
                        with get_db() as session:
                            transaction = session.get(Transaction, transaction_id)
                            if not transaction:
                                result = {
                                    "success": False,
                                    "message": f"❌ Transaction {transaction_id} not found. Cannot update stop loss on non-existent position. Use tp_price/sl_price parameters when opening NEW positions instead.",
                                    "error_type": "invalid_transaction_id"
                                }
                            else:
                                result = toolkit.update_stop_loss(transaction_id, new_sl_price, reason)
                        
                    elif action_type == "update_take_profit":
                        transaction_id = parameters["transaction_id"]
                        new_tp_price = parameters["new_tp_price"]
                        
                        # Validate transaction exists
                        with get_db() as session:
                            transaction = session.get(Transaction, transaction_id)
                            if not transaction:
                                result = {
                                    "success": False,
                                    "message": f"❌ Transaction {transaction_id} not found. Cannot update take profit on non-existent position. Use tp_price/sl_price parameters when opening NEW positions instead.",
                                    "error_type": "invalid_transaction_id"
                                }
                            else:
                                result = toolkit.update_take_profit(transaction_id, new_tp_price, reason)
                        
                    elif action_type == "adjust_tp_sl":
                        # OPTIMIZATION: Combined TP/SL adjustment in single call
                        transaction_id = parameters["transaction_id"]
                        new_tp_price = parameters["new_tp_price"]
                        new_sl_price = parameters["new_sl_price"]
                        
                        # Get transaction for adjust_tp_sl call
                        with get_db() as session:
                            transaction = session.get(Transaction, transaction_id)
                            if transaction:
                                result = toolkit.account.adjust_tp_sl(transaction, new_tp_price, new_sl_price)
                                
                                if result:
                                    # Create success result dict matching other action formats
                                    result = {
                                        "success": True,
                                        "message": f"Updated both TP to ${new_tp_price:.2f} and SL to ${new_sl_price:.2f}",
                                        "transaction_id": transaction_id,
                                        "old_tp_price": transaction.take_profit,
                                        "new_tp_price": new_tp_price,
                                        "old_sl_price": transaction.stop_loss,
                                        "new_sl_price": new_sl_price
                                    }
                                    logger.info(f"Successfully updated both TP and SL for transaction {transaction_id}")
                                else:
                                    result = {
                                        "success": False,
                                        "message": f"Failed to update TP/SL for transaction {transaction_id} - likely entry order not yet filled",
                                        "transaction_id": transaction_id
                                    }
                                    logger.warning(f"Failed to update TP/SL for transaction {transaction_id}")
                            else:
                                result = {
                                    "success": False,
                                    "message": f"❌ Transaction {transaction_id} not found. Cannot adjust TP/SL on non-existent position. This typically happens when trying to modify a position that was just recommended to open but doesn't exist yet.",
                                    "error_type": "invalid_transaction_id",
                                    "transaction_id": transaction_id
                                }
                        
                    elif action_type == "open_buy_position":
                        symbol = parameters["symbol"]
                        quantity = parameters["quantity"]
                        tp_price = parameters.get("tp_price")
                        sl_price = parameters.get("sl_price")
                        
                        # CRITICAL: Ensure quantity is a whole number (Alpaca requires integers for GTC orders)
                        if not isinstance(quantity, int) and quantity != int(quantity):
                            logger.warning(f"⚠️ Fractional quantity detected for {symbol}: {quantity} - rounding to {int(quantity)}")
                        quantity = int(quantity)
                        
                        result = toolkit.open_buy_position(symbol, quantity, tp_price, sl_price, reason)
                    
                    elif action_type == "open_sell_position":
                        symbol = parameters["symbol"]
                        quantity = parameters["quantity"]
                        tp_price = parameters.get("tp_price")
                        sl_price = parameters.get("sl_price")
                        
                        # CRITICAL: Ensure quantity is a whole number (Alpaca requires integers for GTC orders)
                        if not isinstance(quantity, int) and quantity != int(quantity):
                            logger.warning(f"⚠️ Fractional quantity detected for {symbol}: {quantity} - rounding to {int(quantity)}")
                        quantity = int(quantity)
                        
                        result = toolkit.open_sell_position(symbol, quantity, tp_price, sl_price, reason)
                    
                    else:
                        logger.warning(f"Unknown action_type: {action_type}")
                        result = {"success": False, "message": f"Unknown action_type: {action_type}"}
                    
                    # Record action in log
                    # Create a concise summary from the result
                    if result and result.get("success"):
                        # Build summary based on action type
                        if action_type in ["open_buy_position", "open_sell_position"]:
                            direction = result.get('direction', 'BUY' if action_type == "open_buy_position" else 'SELL')
                            transaction_id = result.get('transaction_id')
                            summary = f"Transaction #{transaction_id}: {result.get('symbol')} {direction} {result.get('quantity')} @ ${result.get('entry_price', 0):.2f}"
                            if result.get('tp_price'):
                                summary += f" TP@${result.get('tp_price'):.2f}"
                            if result.get('sl_price'):
                                summary += f" SL@${result.get('sl_price'):.2f}"
                        elif action_type == "close_position":
                            summary = f"Closed transaction #{result.get('transaction_id')}"
                        elif action_type == "adjust_quantity":
                            summary = f"Adjusted transaction #{parameters.get('transaction_id')} from {result.get('old_quantity')} to {result.get('new_quantity')}"
                        elif action_type == "update_stop_loss":
                            old_sl = result.get('old_sl_price')
                            new_sl = result.get('new_sl_price', 0)
                            old_sl_str = f"${old_sl:.2f}" if old_sl is not None else "None"
                            summary = f"Updated SL for transaction #{parameters.get('transaction_id')} from {old_sl_str} to ${new_sl:.2f}"
                        elif action_type == "update_take_profit":
                            old_tp = result.get('old_tp_price')
                            new_tp = result.get('new_tp_price', 0)
                            old_tp_str = f"${old_tp:.2f}" if old_tp is not None else "None"
                            summary = f"Updated TP for transaction #{parameters.get('transaction_id')} from {old_tp_str} to ${new_tp:.2f}"
                        elif action_type == "adjust_tp_sl":
                            # Combined TP/SL adjustment summary
                            old_tp = result.get('old_tp_price')
                            new_tp = result.get('new_tp_price', 0)
                            old_sl = result.get('old_sl_price')
                            new_sl = result.get('new_sl_price', 0)
                            old_tp_str = f"${old_tp:.2f}" if old_tp is not None else "None"
                            old_sl_str = f"${old_sl:.2f}" if old_sl is not None else "None"
                            summary = f"Updated TP/SL for transaction #{parameters.get('transaction_id')} from TP:{old_tp_str}/SL:{old_sl_str} to TP:${new_tp:.2f}/SL:${new_sl:.2f}"
                        else:
                            summary = result.get('message', 'Completed')
                    else:
                        summary = result.get('message', 'Failed') if result else 'Failed'
                    
                    action_record = {
                        "iteration": state["iteration_count"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "action_type": action_type,
                        "arguments": parameters,
                        "reason": reason,
                        "confidence": confidence,
                        "source": "research_node_recommendation",
                        "result": result,
                        "success": result.get("success", False) if result else False,
                        "summary": summary
                    }
                    actions_log.append(action_record)
                    
                    logger.info(f"✅ Recommended action executed: {action_type} - success={result.get('success', False)}")
                    
                    detailed_action_reports.append({
                        "tool": action_type,
                        "args": parameters,
                        "reason": reason,
                        "confidence": confidence,
                        "result": result,
                        "source": "research_recommendation"
                    })
                    
                except Exception as e:
                    logger.error(f"Error executing recommended action {action_type}: {e}", exc_info=True)
                    action_record = {
                        "iteration": state["iteration_count"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "action_type": action_type,
                        "arguments": parameters,
                        "reason": reason,
                        "confidence": confidence,
                        "source": "research_node_recommendation",
                        "error": str(e),
                        "success": False,
                        "summary": f"Error: {str(e)}"
                    }
                    actions_log.append(action_record)
                    
                    detailed_action_reports.append({
                        "tool": action_type,
                        "args": parameters,
                        "error": str(e),
                        "source": "research_recommendation"
                    })
        
        # Build summary for recommended actions
        optimization_note = f" (optimized from {len(recommended_actions)} recommendations)" if len(optimized_actions) < len(recommended_actions) else ""
        summary_lines = [f"Executed {len(optimized_actions)} actions{optimization_note}:"]
        for r in detailed_action_reports:
            tool_label = r.get("tool")
            if r.get("result"):
                res = r["result"]
                summary_lines.append(f"- {tool_label}: success={res.get('success', 'unknown')}, {res.get('message', 'no message')}")
            else:
                summary_lines.append(f"- {tool_label}: error={r.get('error', 'unknown error')}")
        
        actions_summary = "\n".join(summary_lines)
        
        # Refresh portfolio status after actions
        portfolio_status = toolkit.get_portfolio_status()
        open_positions = portfolio_status.get("open_positions", [])

        logger.info(f"Action execution complete. {len(actions_log) - len(state['actions_log'])} new actions recorded")

        # Return updated state - manually append to messages
        return {
            "messages": state["messages"] + [
                HumanMessage(content="Executing recommended actions from research node"),
                AIMessage(content=actions_summary)
            ],
            "actions_log": actions_log,
            "portfolio_status": portfolio_status,
            "open_positions": open_positions,
            "recommended_actions": [],  # Clear after execution to prevent re-execution
            "iteration_count": state["iteration_count"] + 1
        }
        
    except Exception as e:
        logger.error(f"Error in action node: {e}", exc_info=True)
        job_id = state.get("job_id")
        if job_id:
            mark_job_as_failed(job_id, f"Error in action_node: {str(e)}")
        raise


def finalize(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Finalize and summarize risk management session.
    
    Steps:
    1. Generate summary of all actions taken
    2. Calculate final portfolio metrics
    3. Create final report
    4. Update SmartRiskManagerJob record
    """
    logger.info("Finalizing Smart Risk Manager session...")
    
    try:
        risk_manager_model = state["risk_manager_model"]
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        job_id = state["job_id"]
        
        # Create LLM
        llm = create_llm(risk_manager_model, 0.1, backend_url, api_key)
        
        # Build summaries
        initial_portfolio = state["portfolio_status"]
        initial_summary = f"Virtual Equity: ${initial_portfolio['account_virtual_equity']:.2f} | Available Balance: ${initial_portfolio['account_available_balance']:.2f} | Positions: {len(state['open_positions'])}"
        
        # Get final portfolio status
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        final_portfolio = toolkit.get_portfolio_status()
        num_open_positions = len(final_portfolio.get('open_positions', []))
        num_pending_positions = len(final_portfolio.get('pending_positions', []))
        
        # Calculate balance change
        balance_change = final_portfolio['account_available_balance'] - initial_portfolio['account_available_balance']
        balance_change_str = f"+${balance_change:.2f}" if balance_change >= 0 else f"-${abs(balance_change):.2f}"
        
        final_summary = f"Virtual Equity: ${final_portfolio['account_virtual_equity']:.2f} | Available Balance: ${final_portfolio['account_available_balance']:.2f} ({balance_change_str}) | Positions: {num_open_positions} (+ {num_pending_positions} pending)"
        
        actions_summary = "\n".join(
            f"{i+1}. {action['action_type']}: {action.get('summary', 'No summary')}"
            for i, action in enumerate(state["actions_log"])
        ) if state["actions_log"] else "No actions taken"
        
        # Get final summary from LLM
        finalization_prompt = FINALIZATION_PROMPT.format(
            initial_portfolio_summary=initial_summary,
            actions_log_summary=actions_summary,
            final_portfolio_summary=final_summary
        )
        
        response = llm.invoke([
            *state["messages"],
            HumanMessage(content=finalization_prompt)
        ])
        
        # Handle case where response.content might be a list (convert early for DB storage)
        response_content = response.content
        if isinstance(response_content, list):
            response_content = "\n".join(str(item) for item in response_content)
        
        # Update SmartRiskManagerJob
        with get_db() as session:
            job = session.get(SmartRiskManagerJob, job_id)
            if job:
                job.status = "COMPLETED"
                job.final_portfolio_equity = float(final_portfolio["account_virtual_equity"])
                job.final_available_balance = float(final_portfolio["account_available_balance"])
                job.actions_taken_count = len(state["actions_log"])
                job.actions_summary = response_content  # Use converted string
                job.iteration_count = state["iteration_count"]
                
                # Store complete state including research findings and final summary
                # CRITICAL: Create new dict to ensure SQLAlchemy detects the change
                current_state = job.graph_state or {}
                new_state = {
                    **current_state,  # Preserve research_findings from research_node
                    "open_positions": state["open_positions"],
                    "actions_log": state["actions_log"],
                    "final_scratchpad": state["agent_scratchpad"],
                    "final_summary": response_content  # Use converted string
                }
                
                # Reassign entire field to trigger SQLAlchemy change detection
                job.graph_state = new_state
                session.add(job)
                session.commit()
                logger.info(f"Updated SmartRiskManagerJob {job_id} to COMPLETED with {len(state['actions_log'])} actions")
        
        logger.info("Smart Risk Manager session finalized")
        
        # Handle case where agent_scratchpad might be a list
        current_scratchpad = state["agent_scratchpad"]
        if isinstance(current_scratchpad, list):
            current_scratchpad = "\n".join(str(item) for item in current_scratchpad)
        
        # response_content already converted above, use it directly
        return {
            "messages": state["messages"] + [HumanMessage(content=finalization_prompt), response],
            "agent_scratchpad": current_scratchpad + "\n\n## Final Summary\n" + response_content
        }
        
    except Exception as e:
        logger.error(f"Error finalizing: {e}", exc_info=True)
        
        # Mark job as FAILED
        job_id = state.get("job_id")
        if job_id:
            mark_job_as_failed(job_id, f"Error in finalize: {str(e)}")
        
        raise


# ==================== CONDITIONAL ROUTING ====================

def should_continue_or_finalize(state: SmartRiskManagerState) -> str:
    """
    Determine if we should finalize or continue with the workflow.
    
    Simple iteration limit check - if we've exceeded max iterations, finalize.
    Otherwise, the sequential flow continues.
    
    Returns:
        "finalize" if max iterations reached, otherwise shouldn't be called
    """
    # Check iteration limit
    if state["iteration_count"] >= state["max_iterations"]:
        logger.warning(f"Max iterations ({state['max_iterations']}) reached, finalizing")
        return "finalize"
    
    # This shouldn't be reached in normal flow
    logger.warning("should_continue_or_finalize called unexpectedly, finalizing")
    return "finalize"


# NOTE: should_research_node_continue function removed - research_node now uses internal loop
# and always returns research_complete=True, so no conditional routing needed


# ==================== SMART RISK MANAGER GRAPH CLASS ====================

class SmartRiskManagerGraph:
    """
    LangGraph-based Smart Risk Manager using class-based pattern.
    LLM created once on first research iteration, reused across all iterations.
    """
    
    def __init__(self, expert_instance_id: int, account_id: int):
        """Initialize the graph with toolkit and tools created once."""
        self.expert_instance_id = expert_instance_id
        self.account_id = account_id
        
        # Load expert instance
        self.expert = get_expert_instance_from_id(expert_instance_id)
        if not self.expert:
            raise ValueError(f"Expert instance {expert_instance_id} not found")
        
        # Create toolkit once
        self.toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Track recommended actions (persists across iterations)
        self.recommended_actions_list = []
        
        # Create research tools once
        self.research_tools = self._create_research_tools()
        
        # LLM created on first research iteration (lazy init - needs state values)
        self.llm_with_tools = None
        
        # Build and compile the graph
        self.app = self._build_graph()
    
    def _create_research_tools(self) -> List:
        """Create research tools with access to self.toolkit and self.recommended_actions_list."""
        
        @tool
        @smart_risk_manager_tool
        def get_analysis_outputs_tool(
            analysis_id: Annotated[int, "ID of the MarketAnalysis to get outputs for"]
        ) -> Dict[str, Any]:
            """Get available output keys for a specific market analysis."""
            return self.toolkit.get_analysis_outputs(analysis_id)
        
        @tool
        @smart_risk_manager_tool
        def get_analysis_output_detail_tool(
            analysis_id: Annotated[int, "ID of the MarketAnalysis"],
            output_key: Annotated[str, "Key of the output to retrieve"]
        ) -> Dict[str, Any]:
            """Get detailed content of a specific analysis output."""
            content = self.toolkit.get_analysis_output_detail(analysis_id, output_key)
            return {
                "analysis_id": analysis_id,
                "output_key": output_key,
                "content": content
            }
        
        @tool
        @smart_risk_manager_tool
        def get_analysis_outputs_batch_tool(
            analysis_ids: Annotated[List[int], "List of MarketAnalysis IDs"],
            output_keys: Annotated[List[str], "List of output keys to fetch"],
            max_tokens: Annotated[Optional[int], "Maximum tokens (default: 50000)"] = None
        ) -> Dict[str, Any]:
            """Fetch multiple analysis outputs efficiently in a single call."""
            if max_tokens is None:
                max_tokens = 50000
            return self.toolkit.get_analysis_outputs_batch(analysis_ids, output_keys, max_tokens)
        
        @tool
        @smart_risk_manager_tool
        def get_historical_analyses_tool(
            symbol: Annotated[str, "Instrument symbol"],
            limit: Annotated[Optional[int], "Max results (default: 10)"] = None,
            offset: Annotated[Optional[int], "Skip results (default: 0)"] = None
        ) -> List[Dict[str, Any]]:
            """Get historical market analyses for a symbol (paginated)."""
            if limit is None:
                limit = 10
            if offset is None:
                offset = 0
            return self.toolkit.get_historical_analyses(symbol, limit, offset)
        
        @tool
        @smart_risk_manager_tool
        def get_all_recent_analyses_tool(
            max_age_hours: Annotated[Optional[int], "Max age in hours (default: 72)"] = None
        ) -> List[Dict[str, Any]]:
            """Get ALL recent market analyses across all symbols."""
            if max_age_hours is None:
                max_age_hours = 72
            return self.toolkit.get_recent_analyses(max_age_hours=max_age_hours)
        
        @tool
        @smart_risk_manager_tool
        def get_current_price_tool(
            symbol: Annotated[str, "Instrument symbol"]
        ) -> float:
            """Get current bid price for an instrument."""
            return self.toolkit.get_current_price(symbol)
        
        @tool
        @smart_risk_manager_tool
        def finish_research_tool(
            summary: Annotated[str, "Concise summary of key findings"]
        ) -> str:
            """Call this when research is complete and ready for action execution."""
            return f"Research complete. Summary recorded: {summary[:100]}..."
        
        @tool
        @smart_risk_manager_tool
        def recommend_close_position(
            transaction_id: Annotated[int, "ID of the position to close"],
            reason: Annotated[str, "Clear explanation referencing research findings"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
        ) -> str:
            """Recommend closing an existing position.
            
            IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            """
            action = {
                "action_type": "close_position",
                "parameters": {"transaction_id": transaction_id},
                "reason": reason,
                "confidence": confidence
            }
            self.recommended_actions_list.append(action)
            return f"Recorded close position recommendation for transaction {transaction_id}. Total actions: {len(self.recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_adjust_quantity(
            transaction_id: Annotated[int, "ID of the position to adjust"],
            new_quantity: Annotated[int, "New quantity for the position - MUST be whole number (e.g., 10, not 10.5)"],
            reason: Annotated[str, "Clear explanation for adjustment"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
        ) -> str:
            """Recommend adjusting position quantity.
            
            IMPORTANT: 
            - new_quantity must be a whole number (integer) like 10, not 10.5
            - confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            """
            action = {
                "action_type": "adjust_quantity",
                "parameters": {"transaction_id": transaction_id, "new_quantity": new_quantity},
                "reason": reason,
                "confidence": confidence
            }
            self.recommended_actions_list.append(action)
            return f"Recorded quantity adjustment for transaction {transaction_id} to {new_quantity}. Total actions: {len(self.recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_update_stop_loss(
            transaction_id: Annotated[int, "ID of the position"],
            new_sl_price: Annotated[float, "New stop loss price"],
            reason: Annotated[str, "Clear explanation for adjustment"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
        ) -> str:
            """Recommend updating stop loss price.
            
            IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            """
            action = {
                "action_type": "update_stop_loss",
                "parameters": {"transaction_id": transaction_id, "new_sl_price": new_sl_price},
                "reason": reason,
                "confidence": confidence
            }
            self.recommended_actions_list.append(action)
            return f"Recorded stop loss update for transaction {transaction_id} to {new_sl_price}. Total actions: {len(self.recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_update_take_profit(
            transaction_id: Annotated[int, "ID of the position"],
            new_tp_price: Annotated[float, "New take profit price"],
            reason: Annotated[str, "Clear explanation for adjustment"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"]
        ) -> str:
            """Recommend updating take profit price.
            
            IMPORTANT: confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85).
            """
            action = {
                "action_type": "update_take_profit",
                "parameters": {"transaction_id": transaction_id, "new_tp_price": new_tp_price},
                "reason": reason,
                "confidence": confidence
            }
            self.recommended_actions_list.append(action)
            return f"Recorded take profit update for transaction {transaction_id} to {new_tp_price}. Total actions: {len(self.recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_open_buy_position(
            symbol: Annotated[str, "Instrument symbol to buy"],
            quantity: Annotated[int, "Number of shares/units (MUST be a whole number, e.g., 10, not 10.5)"],
            reason: Annotated[str, "Clear explanation based on research"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"],
            tp_price: Annotated[Optional[float], "Take profit price (optional)"] = None,
            sl_price: Annotated[Optional[float], "Stop loss price (optional)"] = None
        ) -> str:
            """Recommend opening a new BUY (long) position.
            
            IMPORTANT: 
            - quantity must be a whole number (integer) like 10, not 10.5
            - confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85)
            """
            action = {
                "action_type": "open_buy_position",
                "parameters": {
                    "symbol": symbol,
                    "quantity": quantity,
                    "tp_price": tp_price,
                    "sl_price": sl_price
                },
                "reason": reason,
                "confidence": confidence
            }
            self.recommended_actions_list.append(action)
            return f"Recorded buy position recommendation for {symbol} ({quantity} shares). Total actions: {len(self.recommended_actions_list)}"
        
        @tool
        @smart_risk_manager_tool
        def recommend_open_sell_position(
            symbol: Annotated[str, "Instrument symbol to sell short"],
            quantity: Annotated[int, "Number of shares/units (MUST be a whole number, e.g., 10, not 10.5)"],
            reason: Annotated[str, "Clear explanation based on research"],
            confidence: Annotated[int, "Your confidence level as an INTEGER from 1-100 (e.g., 85 for 85% confidence, NOT 0.85)"],
            tp_price: Annotated[Optional[float], "Take profit price (optional)"] = None,
            sl_price: Annotated[Optional[float], "Stop loss price (optional)"] = None
        ) -> str:
            """Recommend opening a new SELL (short) position.
            
            IMPORTANT: 
            - quantity must be a whole number (integer) like 10, not 10.5
            - confidence must be an integer from 1-100 (e.g., 85), not a decimal (not 0.85)
            """
            action = {
                "action_type": "open_sell_position",
                "parameters": {
                    "symbol": symbol,
                    "quantity": quantity,
                    "tp_price": tp_price,
                    "sl_price": sl_price
                },
                "reason": reason,
                "confidence": confidence
            }
            self.recommended_actions_list.append(action)
            return f"Recorded sell position recommendation for {symbol} ({quantity} shares). Total actions: {len(self.recommended_actions_list)}"
        
        return [
            get_analysis_outputs_tool,
            get_analysis_output_detail_tool,
            get_analysis_outputs_batch_tool,
            get_historical_analyses_tool,
            get_all_recent_analyses_tool,
            get_current_price_tool,
            recommend_close_position,
            recommend_adjust_quantity,
            recommend_update_stop_loss,
            recommend_update_take_profit,
            recommend_open_buy_position,
            recommend_open_sell_position,
            finish_research_tool
        ]
    
    def _initialize_research_agent(self, state: SmartRiskManagerState) -> Dict[str, Any]:
        """Initialize research LLM and conversation ONCE before research loop starts."""
        logger.info("Initializing research agent (LLM + initial prompt) - ONCE per graph session...")
        
        try:
            risk_manager_model = state["risk_manager_model"]
            backend_url = state["backend_url"]
            api_key = state["api_key"]
            expert_instance_id = state["expert_instance_id"]
            expert_settings = state["expert_settings"]
            portfolio_status = state["portfolio_status"]
            
            # Calculate position size limits
            # Use account_virtual_equity (expert's total allocation), not available_balance
            # Virtual equity = account_balance * virtual_equity_pct (e.g., 5% of total)
            # Available balance = virtual_equity - already_deployed_trades
            # Position sizing should be against virtual_equity (the expert's allocation)
            expert = get_expert_instance_from_id(expert_instance_id)
            if not expert:
                raise ValueError(f"Expert instance {expert_instance_id} not found")
            max_position_pct = expert.get_setting_with_interface_default("max_virtual_equity_per_instrument_percent")
            current_equity = float(portfolio_status.get("account_virtual_equity", 0))
            max_position_equity = current_equity * (max_position_pct / 100.0)
            
            # Get expert-specific instructions
            expert_instructions = ""
            try:
                expert_inst = get_expert_instance_from_id(expert_instance_id)
                if expert_inst and hasattr(expert_inst, 'get_expert_specific_instructions'):
                    expert_instructions = expert_inst.get_expert_specific_instructions("research_node")
                    if expert_instructions:
                        logger.info(f"Added expert-specific instructions for research_node ({len(expert_instructions)} chars)")
            except Exception as e:
                logger.warning(f"Could not get expert-specific instructions: {e}")
            
            formatted_expert_instructions = f"\n\n{expert_instructions}" if expert_instructions else ""
            agent_scratchpad_content = state.get('agent_scratchpad', 'No prior context')
            
            research_system_prompt = RESEARCH_PROMPT.format(
                agent_scratchpad=agent_scratchpad_content,
                expert_instructions=formatted_expert_instructions,
                max_position_pct=max_position_pct,
                max_position_equity=max_position_equity
            )

            # Initialize conversation with system prompt - SENT ONCE
            research_messages = [
                SystemMessage(content=research_system_prompt),
                HumanMessage(content="Begin your research. Investigate the most relevant analyses and gather detailed information.")
            ]
            
            # Create LLM with tools ONCE per graph session
            llm = create_llm(risk_manager_model, 0.2, backend_url, api_key)
            # Get parallel_tool_calls setting from expert (default False for safety)
            parallel_tool_calls = self.expert.get_setting_with_interface_default("smart_risk_manager_parallel_tool_calls", log_warning=False)
            self.llm_with_tools = llm.bind_tools(self.research_tools, parallel_tool_calls=parallel_tool_calls)
            
            logger.info(f"✅ Research agent initialized with {len(self.research_tools)} tools")
            logger.info("✅ LLM with tools created ONCE - will be reused across ALL research iterations")
            logger.info("✅ System prompt sent ONCE - conversation will continue from here")
            
            # Return state with initialized conversation
            return {
                "research_messages": research_messages
            }
        except Exception as e:
            logger.error(f"Error initializing research agent: {e}", exc_info=True)
            job_id = state.get("job_id")
            if job_id:
                mark_job_as_failed(job_id, f"Error in initialize_research_agent: {str(e)}")
            raise
    
    def _research_node(self, state: SmartRiskManagerState) -> Dict[str, Any]:
        """Research node - reuses LLM and continues conversation from previous iteration."""
        logger.info("Research node executing...")
        logger.info("Entering research mode - autonomous research agent starting...")
        
        try:
            # Get research conversation (already initialized by _initialize_research_agent)
            research_messages = state["research_messages"]
            
            detailed_cache = state["detailed_outputs_cache"].copy()
            research_complete = False
            final_summary = ""
            iteration_count = state["iteration_count"]
            max_iterations = state['max_iterations']
            
            # =================== INTERNAL RESEARCH LOOP ===================
            # Loop until research is complete OR max iterations reached
            while not research_complete and iteration_count < max_iterations:
                iteration_count += 1
                
                # =================== ITERATION LOGGING ===================
                logger.info("=" * 70)
                logger.info(f"RESEARCH ITERATION {iteration_count}/{max_iterations}")
                logger.info("=" * 70)
                
                # Get LLM response (REUSING self.llm_with_tools)
                try:
                    response = self.llm_with_tools.invoke(research_messages)
                    research_messages.append(response)
                except Exception as llm_error:
                    # Handle LLM errors (e.g., malformed function calls from DeepSeek reasoner)
                    error_msg = str(llm_error)
                    logger.error(f"LLM invocation error: {error_msg[:500]}")
                    
                    # Check if it's a function calling format error
                    if "validation error" in error_msg.lower() or "invalid function calling" in error_msg.lower():
                        logger.warning("LLM returned malformed function call - treating as research incomplete")
                        # Add error message to conversation to help LLM understand the issue
                        research_messages.append(AIMessage(
                            content=f"⚠️ Previous function call had formatting errors. Please call tools again with correct format. "
                                    f"Remember: function parameters must be valid JSON objects, not strings."
                        ))
                        # Continue to next iteration
                        continue
                    else:
                        # Other errors - re-raise
                        raise
                
                logger.info(f"Research iteration: LLM returned {len(response.tool_calls) if response.tool_calls else 0} tool calls")
                
                # Check if research is complete
                if not response.tool_calls:
                    logger.info("Research agent finished without tool calls")
                    final_summary = response.content
                    research_complete = True
                else:
                    # Execute tool calls
                    for tool_call in response.tool_calls:
                        tool_name = tool_call.get("name")
                        tool_args = tool_call.get("args", {})
                        tool_call_id = truncate_tool_call_id(tool_call.get("id"))  # Truncate to comply with OpenAI limit
                        
                        logger.debug(f"Research tool: {tool_name} with args {tool_args}")
                        
                        if tool_name == "finish_research_tool":
                            final_summary = tool_args.get("summary", response.content)
                            research_complete = True
                            research_messages.append(ToolMessage(
                                content="Research complete. Proceeding to action execution.",
                                tool_call_id=tool_call_id,
                                name=tool_name
                            ))
                            break
                        
                        matching_tool = next((t for t in self.research_tools if t.name == tool_name), None)
                        if matching_tool:
                            try:
                                logger.info(f"🔧 Research Tool Call: {tool_name} | Args: {json.dumps(tool_args)}")
                                result = matching_tool.invoke(tool_args)
                                result_preview = str(result)[:200] if not isinstance(result, dict) else f"dict with {len(result)} keys"
                                logger.info(f"✅ Research Tool Result: {tool_name} | {result_preview}")
                                
                                if tool_name == "get_analysis_output_detail_tool":
                                    analysis_id = tool_args.get("analysis_id")
                                    output_key = tool_args.get("output_key")
                                    if analysis_id and output_key:
                                        if analysis_id not in detailed_cache:
                                            detailed_cache[analysis_id] = {}
                                        detailed_cache[analysis_id][output_key] = result.get("content", "")
                                
                                result_str = json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
                                research_messages.append(ToolMessage(
                                    content=result_str,
                                    tool_call_id=tool_call_id,
                                    name=tool_name
                                ))
                            except Exception as e:
                                logger.error(f"Error executing research tool {tool_name}: {e}", exc_info=True)
                                research_messages.append(ToolMessage(
                                    content=f"Error: {str(e)}",
                                    tool_call_id=tool_call_id,
                                    name=tool_name
                                ))
                        else:
                            logger.warning(f"Tool {tool_name} not found in research_tools")
                            research_messages.append(ToolMessage(
                                content=f"Error: Tool {tool_name} not available",
                                tool_call_id=tool_call_id,
                                name=tool_name
                            ))
                    
                    # Add reminder if research not complete
                    if not research_complete:
                        actions_summary = f"\n\n**ACTIONS RECOMMENDED SO FAR**: {len(self.recommended_actions_list)} total\n"
                        if self.recommended_actions_list:
                            for idx, action in enumerate(self.recommended_actions_list, 1):
                                action_type = action.get("action_type", "unknown")
                                confidence = action.get("confidence", 0)
                                reason = action.get("reason", "No reason provided")[:100]
                                actions_summary += f"  {idx}. {action_type} (confidence: {confidence}%) - {reason}\n"
                            actions_summary += "\n**NEXT STEP**: Call finish_research_tool to proceed with executing these actions.\n"
                        else:
                            actions_summary += "  ⚠️ No actions recommended yet.\n"
                            actions_summary += "  - If you need more information, continue researching with available tools.\n"
                            actions_summary += "  - If you have enough information, recommend appropriate actions (open/close/adjust positions).\n"
                            actions_summary += "  - If no actions are needed, explain why in finish_research_tool's summary.\n"
                            actions_summary += "  - Only call finish_research_tool after recommending actions OR explaining why none are needed.\n"
                        
                        reminder_msg = (
                            actions_summary +
                            "\n**REMINDER**: When you have gathered sufficient information and made your recommendations, "
                            "you MUST call the finish_research_tool to complete your research and proceed to action execution. "
                            "Without calling finish_research_tool, the system will continue iterating."
                        )
                        
                        logger.debug(f"📝 Appending reminder message to last ToolMessage ({len(reminder_msg)} chars)")
                        logger.debug(f"Reminder preview: {reminder_msg[:200]}...")
                        
                        if research_messages and isinstance(research_messages[-1], ToolMessage):
                            last_tool_msg = research_messages[-1]
                            research_messages[-1] = ToolMessage(
                                content=last_tool_msg.content + reminder_msg,
                                tool_call_id=last_tool_msg.tool_call_id,
                                name=last_tool_msg.name if hasattr(last_tool_msg, 'name') and last_tool_msg.name else "reminder_tool"
                            )
                            logger.debug("✅ Reminder message appended to ToolMessage - will be sent to LLM in next iteration")
                
                logger.info(f"Research iteration {iteration_count} complete")
                logger.info(f"Research node has {len(self.recommended_actions_list)} recommended actions")
                logger.info("=" * 70)
            
            # =================== END OF INTERNAL RESEARCH LOOP ===================
            # Log completion reason
            if research_complete:
                logger.info("✅ Research complete - finish_research_tool was called")
            elif iteration_count >= max_iterations:
                logger.warning(f"⚠️ Research terminated - reached max iterations ({max_iterations})")
            
            # Update scratchpad if research complete
            if research_complete and final_summary:
                updated_scratchpad = state["agent_scratchpad"] + f"\n\n## Research Findings\n{final_summary}\n"
                
                job_id = state.get("job_id")
                if job_id:
                    try:
                        with get_db() as session:
                            job = session.get(SmartRiskManagerJob, job_id)
                            if job:
                                current_state = job.graph_state or {}
                                new_state = {
                                    **current_state,
                                    "research_findings": final_summary,
                                    "recommended_actions_count": len(self.recommended_actions_list)
                                }
                                job.graph_state = new_state
                                session.add(job)
                                session.commit()
                                logger.debug(f"Stored research findings in job {job_id}")
                    except Exception as e:
                        logger.warning(f"Failed to store research findings in job: {e}")
            else:
                # Handle case where agent_scratchpad might be a list
                current_scratchpad = state["agent_scratchpad"]
                if isinstance(current_scratchpad, list):
                    current_scratchpad = "\n".join(str(item) for item in current_scratchpad)
                updated_scratchpad = current_scratchpad
            
            return {
                "messages": state["messages"],
                "research_messages": research_messages,
                "detailed_outputs_cache": detailed_cache,
                "agent_scratchpad": updated_scratchpad,
                "research_complete": research_complete,
                "recommended_actions": self.recommended_actions_list,  # Always pass actions (even if research incomplete)
                "iteration_count": iteration_count
            }
        except Exception as e:
            logger.error(f"Error in research node: {e}", exc_info=True)
            job_id = state.get("job_id")
            if job_id:
                mark_job_as_failed(job_id, f"Error in research_node: {str(e)}")
            raise
    
    # NOTE: _should_continue method removed - research_node now uses internal loop
    # and always returns research_complete=True, so no conditional routing needed
    
    def _build_graph(self) -> StateGraph:
        """Build and compile the LangGraph workflow."""
        logger.info(f"Building Smart Risk Manager graph for expert {self.expert_instance_id}, account {self.account_id}")
        
        workflow = StateGraph(SmartRiskManagerState)
        
        # Add nodes
        workflow.add_node("initialize_context", initialize_context)
        workflow.add_node("analyze_portfolio", analyze_portfolio)
        workflow.add_node("check_recent_analyses", check_recent_analyses)
        workflow.add_node("initialize_research_agent", self._initialize_research_agent)  # NEW: Initialize LLM once
        workflow.add_node("research_node", self._research_node)
        workflow.add_node("action_node", action_node)
        workflow.add_node("finalize", finalize)
        
        # Sequential flow
        workflow.set_entry_point("initialize_context")
        workflow.add_edge("initialize_context", "analyze_portfolio")
        workflow.add_edge("analyze_portfolio", "check_recent_analyses")
        workflow.add_edge("check_recent_analyses", "initialize_research_agent")  # Initialize before research loop
        workflow.add_edge("initialize_research_agent", "research_node")  # Go to research (internal loop handles iterations)
        
        # Research node now handles its own internal iteration loop - always proceeds to action_node when done
        workflow.add_edge("research_node", "action_node")
        
        workflow.add_edge("action_node", "finalize")
        workflow.add_edge("finalize", END)
        
        logger.info("Smart Risk Manager graph built successfully")
        return workflow.compile()


# ==================== GRAPH CONSTRUCTION ====================

def build_smart_risk_manager_graph(expert_instance_id: int, account_id: int) -> StateGraph:
    """
    Build the complete LangGraph workflow with SEQUENTIAL FLOW.
    
    Flow: 
    1. initialize → analyze_portfolio → check_recent_analyses 
    2. initialize_research_agent sets up the LLM and tools
    3. research_node handles iterative research using an INTERNAL LOOP (not graph-based iteration)
       - LLM session persists naturally in function scope across all iterations
       - Can make up to max_iterations tool calls
       - If no actions recommended, asks LLM for explanation
       - Always returns research_complete=True when exiting
    4. action_node (pure Python) executes all recommended actions in one pass
    5. finalize
    
    The research_node uses an internal for loop (1 to max_iterations) instead of graph-based
    looping. This is more efficient as it avoids state serialization overhead and maintains
    a single LLM session throughout the research phase.
    
    Args:
        expert_instance_id: ID of the ExpertInstance
        account_id: ID of the AccountDefinition
        
    Returns:
        Compiled StateGraph ready for execution
    """
    logger.info(f"Building Smart Risk Manager graph for expert {expert_instance_id}, account {account_id}")
    
    # Instantiate the class-based graph
    graph_instance = SmartRiskManagerGraph(expert_instance_id, account_id)
    
    # Return the compiled graph
    return graph_instance.app


# ==================== EXECUTION ENTRY POINT ====================

def run_smart_risk_manager(expert_instance_id: int, account_id: int, job_id: int | None = None) -> Dict[str, Any]:
    """
    Main entry point for running the Smart Risk Manager.
    
    Args:
        expert_instance_id: ID of the ExpertInstance
        account_id: ID of the AccountDefinition
        job_id: Optional existing SmartRiskManagerJob ID to update (if None, creates new job)
        
    Returns:
        Final state dictionary with summary and results
    """
    logger.info(f"Starting Smart Risk Manager for expert {expert_instance_id}, account {account_id}")
    
    # Disable LangChain debug mode - we have our own custom logging that shows tool names and arguments
    langchain.debug = False
    langchain.verbose = False
    
    try:
        # Build graph
        graph = build_smart_risk_manager_graph(expert_instance_id, account_id)
        
        # Initialize state
        initial_state = {
            "expert_instance_id": expert_instance_id,
            "account_id": account_id,
            "user_instructions": "",  # Will be loaded in initialize_context
            "expert_settings": {},  # Will be loaded in initialize_context
            "risk_manager_model": "",  # Will be loaded in initialize_context
            "backend_url": "",  # Will be loaded in initialize_context
            "api_key": "",  # Will be loaded in initialize_context
            "job_id": job_id or 0,  # Use provided job_id or create in initialize_context
            "portfolio_status": {},
            "open_positions": [],
            "recent_analyses": [],
            "detailed_outputs_cache": {},
            "messages": [],
            "research_messages": [],  # Research node's isolated conversation
            "agent_scratchpad": "",
            "research_complete": False,  # Flag set by finish_research_tool
            "recommended_actions": [],  # Actions recommended by research node
            "actions_log": [],
            "iteration_count": 0,
            "max_iterations": 10
        }
        
        # Run graph with stdout/stderr suppression to prevent LangChain's "AI:" and "Tool:" noise
        # Our custom logger will still output properly since it writes directly to log files
        with suppress_langchain_stdout():
            final_state = graph.invoke(initial_state)
        
        logger.info("Smart Risk Manager completed successfully")
        
        # Disable debug mode after completion
        langchain.debug = False
        langchain.verbose = False
        
        return {
            "success": True,
            "job_id": final_state["job_id"],
            "iterations": final_state["iteration_count"],
            "actions_count": len(final_state["actions_log"]),
            "summary": final_state["agent_scratchpad"].split("## Final Summary\n")[-1] if "## Final Summary\n" in final_state["agent_scratchpad"] else "No summary available"
        }
        
    except Exception as e:
        # Disable debug mode after error
        langchain.debug = False
        langchain.verbose = False
        
        logger.error(f"Error running Smart Risk Manager: {e}", exc_info=True)
        
        # Try to mark job as FAILED in database if job was created
        job_id = None
        if "job_id" in initial_state and initial_state["job_id"]:
            job_id = initial_state["job_id"]
            mark_job_as_failed(job_id, str(e))
        
        # Log activity for failed smart risk manager execution
        try:
            from .db import log_activity
            from .types import ActivityLogSeverity, ActivityLogType
            
            log_activity(
                severity=ActivityLogSeverity.FAILURE,
                activity_type=ActivityLogType.RISK_MANAGER_RAN,
                description=f"Smart risk manager execution failed: {str(e)}",
                data={
                    "mode": "smart",
                    "job_id": job_id,
                    "error": str(e),
                    "error_type": type(e).__name__
                },
                source_expert_id=expert_instance_id,
                source_account_id=account_id
            )
        except Exception as log_error:
            logger.warning(f"Failed to log smart risk manager failure activity: {log_error}")
        
        return {
            "success": False,
            "job_id": job_id,
            "error": str(e)
        }
