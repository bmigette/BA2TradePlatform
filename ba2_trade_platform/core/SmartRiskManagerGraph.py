"""
Smart Risk Manager Graph

LangGraph-based agentic workflow for autonomous portfolio risk management.
Analyzes portfolio status, recent market analyses, and executes risk management actions.
"""

from typing import Dict, Any, List, Annotated, TypedDict
from datetime import datetime, timezone
from operator import add

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
from .models import SmartRiskManagerJob, SmartRiskManagerJobAnalysis, MarketAnalysis
from .db import get_db, add_instance, update_instance, get_instance
from .models import AppSetting
from .SmartRiskManagerToolkit import SmartRiskManagerToolkit
from .utils import get_expert_instance_from_id


# ==================== DEBUG CALLBACK ====================

class SmartRiskManagerDebugCallback(BaseCallbackHandler):
    """Custom callback handler for human-readable debug output."""
    
    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], **kwargs) -> None:
        """Log LLM call start."""
        logger.debug("\n" + "=" * 80)
        logger.debug("🤖 LLM CALL START")
        logger.debug("=" * 80)
        if prompts:
            logger.debug(f"\n📝 Prompt:\n{prompts[0]}")
    
    def on_llm_end(self, response, **kwargs) -> None:
        """Log LLM response in human-readable format."""
        logger.debug("\n" + "=" * 80)
        logger.debug("✅ LLM RESPONSE")
        logger.debug("=" * 80)
        
        if hasattr(response, 'generations') and response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    if hasattr(gen, 'text'):
                        logger.debug(f"\n{gen.text}\n")
                    if hasattr(gen, 'message') and hasattr(gen.message, 'content'):
                        logger.debug(f"\n{gen.message.content}\n")
        
        logger.debug("=" * 80 + "\n")
    
    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, **kwargs) -> None:
        """Log tool call start."""
        tool_name = serialized.get("name", "unknown")
        logger.debug(f"\n🔧 TOOL CALL: {tool_name}")
        logger.debug(f"   Input: {input_str}")
    
    def on_tool_end(self, output: str, **kwargs) -> None:
        """Log tool call result."""
        logger.debug(f"   ✅ Output: {output}\n")


# ==================== HELPER FUNCTIONS ====================

def _extract_recommendation_from_summary(summary: str) -> Dict[str, Any]:
    """
    Extract recommendation details from TradingAgents analysis summary.
    
    Args:
        summary: The analysis summary text
        
    Returns:
        Dict with action, confidence, expected_profit, term, key_factors (or empty dict if not found)
    """
    import re
    
    if not summary or not isinstance(summary, str):
        return {}
    
    try:
        result = {}
        
        # Extract Signal/Action (e.g., "Signal: BUY", "Action: SELL", "Signal: HOLD")
        action_match = re.search(r'(?:Signal|Action):\s*(\w+)', summary, re.IGNORECASE)
        if action_match:
            result['action'] = action_match.group(1).upper()
        
        # Extract Confidence (e.g., "Confidence: 75.0%")
        conf_match = re.search(r'Confidence:\s*([\d.]+)%', summary)
        if conf_match:
            result['confidence'] = float(conf_match.group(1))
        
        # Extract Expected Profit (e.g., "Expected Profit: 5.50%")
        profit_match = re.search(r'Expected Profit:\s*([-\d.]+)%', summary)
        if profit_match:
            result['expected_profit'] = float(profit_match.group(1))
        else:
            result['expected_profit'] = 0.0  # Default for HOLD
        
        # Extract Time Horizon/Term (e.g., "Time Horizon: MEDIUM_TERM")
        term_match = re.search(r'Time Horizon:\s*(\w+)', summary, re.IGNORECASE)
        if term_match:
            result['term'] = term_match.group(1).replace('_', ' ').title()
        
        # Extract Risk Level (optional, can be used as key factor)
        risk_match = re.search(r'Risk Level:\s*(\w+)', summary, re.IGNORECASE)
        if risk_match:
            risk_level = risk_match.group(1)
            result['key_factors'] = f"Risk: {risk_level}"
        
        return result if result else {}
        
    except Exception as e:
        logger.debug(f"Error extracting recommendation from summary: {e}")
        return {}


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


def create_llm(model: str, temperature: float, base_url: str, api_key: str) -> ChatOpenAI:
    """
    Create a ChatOpenAI instance with proper configuration and debug callback.
    
    Args:
        model: Model name (e.g., 'gpt-4o-mini')
        temperature: Temperature for generation
        base_url: Base URL for API endpoint
        api_key: API key for authentication
        
    Returns:
        Configured ChatOpenAI instance with debug callback
    """
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        base_url=base_url,
        api_key=api_key,
        callbacks=[SmartRiskManagerDebugCallback()]
    )


# ==================== PROMPTS ====================
# All prompts are defined here for easy maintenance and customization

SYSTEM_INITIALIZATION_PROMPT = """You are the Smart Risk Manager, an AI assistant responsible for monitoring and managing portfolio risk.

## YOUR MISSION
{user_instructions}

## YOUR CAPABILITIES
You have access to a complete toolkit for portfolio analysis and risk management:
- Portfolio status and position analysis tools
- Market analysis research tools
- Trading action tools (close positions, adjust quantities, update stop loss/take profit)

## YOUR WORKFLOW
1. Analyze the current portfolio status and identify risks
2. Research recent market analyses for positions that need attention
3. Make informed decisions about which actions to take
4. Execute trading actions with clear reasoning
5. Iterate and refine until portfolio risk is acceptable

## IMPORTANT GUIDELINES
- Always provide clear reasoning for your decisions
- Consider both the portfolio-level risk AND individual position risks
- Use market analyses to inform your decisions
- Take conservative actions when uncertain
- Document your reasoning in every action

You will be guided through each step of the process. Let's begin.
"""

PORTFOLIO_ANALYSIS_PROMPT = """Analyze the current portfolio status and identify key risks and opportunities.

## CURRENT PORTFOLIO STATUS
{portfolio_status}

## TASK
Review the portfolio and create an initial assessment covering:
1. Overall portfolio health (P&L, concentration, diversification)
2. Positions with concerning P&L (large losses or excessive gains)
3. Positions that may need stop loss or take profit adjustments
4. Any risk concentrations (too much exposure to one symbol)
5. Initial thoughts on what actions may be needed

Be concise but thorough. This assessment will guide your next steps.
"""

DECISION_LOOP_PROMPT = """Based on the information you have gathered so far, decide your next action.

## CONTEXT
User Instructions: {user_instructions}
Iteration: {iteration_count}/{max_iterations}

## PORTFOLIO STATUS SUMMARY
{portfolio_summary}

## WHAT YOU KNOW SO FAR
{agent_scratchpad}

## ACTIONS TAKEN SO FAR
{actions_log_summary}

## YOUR OPTIONS
1. **research_more** - You need to investigate specific market analyses in more detail before making decisions
2. **take_action** - You have enough information and are ready to execute trading actions
3. **finish** - You have completed all necessary risk management actions

## YOUR DECISION
Choose one of the three options above and explain your reasoning. What do you need to do next and why?
"""

RESEARCH_PROMPT = """You need to gather more detailed information from market analyses.

## AVAILABLE RECENT ANALYSES (Last 72 hours, all symbols)
{recent_analyses_summary}

## YOUR TASK
Review the recent analyses listed above and investigate the ones most relevant to your risk management decisions.

### Step 1: Identify analyses to investigate
Look at the symbols and timestamps above. Prioritize:
- Analyses for positions you currently hold
- Analyses for symbols mentioned in your risk management instructions
- Recent analyses with relevant expert insights

### Step 2: Get available outputs for selected analyses
For each analysis you want to investigate, use `get_analysis_outputs_tool(analysis_id)` to see what outputs are available (technical analysis, fundamentals, sentiment, etc.)

### Step 3: Read detailed outputs
**RECOMMENDED**: Use `get_analysis_outputs_batch_tool(requests)` to fetch multiple outputs efficiently in one call.
- Automatically handles token limits and truncation
- Example: `get_analysis_outputs_batch_tool([{{"analysis_id": 123, "output_keys": ["analysis_summary", "market_report"]}}, {{"analysis_id": 124, "output_keys": ["news_report"]}}])`

**ALTERNATIVE**: Use `get_analysis_output_detail_tool(analysis_id, output_key)` to read outputs one at a time.

### Step 4 (Optional): Get historical context
If you need more historical context for a specific symbol, use `get_historical_analyses_tool(symbol, limit=10)` to see older analyses for deeper research.

## WHAT TO LOOK FOR
- Analyst sentiment and recommendations
- Technical indicators and price targets  
- Fundamental concerns or opportunities
- Risk factors mentioned by analysts
- Trend changes or momentum shifts

Execute your research plan using the available tools."""

ACTION_PROMPT = """You are ready to execute risk management actions.

## CURRENT SITUATION
{portfolio_summary}

## RESEARCH FINDINGS
{agent_scratchpad}

## TRADING RESTRICTIONS
**IMPORTANT**: The following trading restrictions are configured for this expert:
- BUY orders: {buy_status}
- SELL orders: {sell_status}
- Enabled instruments: {enabled_instruments}
- Max position size: {max_position_pct}% of equity per symbol

**You MUST respect these restrictions. Do not attempt actions that violate them.**

## AVAILABLE ACTIONS
You have access to these trading tools:
- **close_position(transaction_id, reason)** - Close an entire position
- **adjust_quantity(transaction_id, new_quantity, reason)** - Partial close or add to position
- **update_stop_loss(transaction_id, new_sl_price, reason)** - Update stop loss
- **update_take_profit(transaction_id, new_tp_price, reason)** - Update take profit
- **open_new_position(symbol, direction, quantity, tp_price, sl_price, reason)** - Open new position (respects restrictions above)

## GUIDELINES FOR ACTIONS
{user_instructions}

## YOUR TASK
1. Decide which action(s) to take
2. For EACH action, provide clear reasoning that references your research
3. Be specific about parameters (transaction_id, prices, quantities)
4. Execute the actions using the appropriate tools
5. RESPECT the trading restrictions listed above

Remember: Every action must include a "reason" parameter explaining your decision.
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
    
    # Agent State
    messages: Annotated[List[BaseMessage], add]  # Message history
    agent_scratchpad: str  # Agent's reasoning notes
    next_action: str  # "research_more", "take_action", "finish"
    
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
    def get_analysis_outputs_tool(analysis_id: int) -> Dict[str, Any]:
        """Get available output keys for a specific market analysis.
        
        Args:
            analysis_id: ID of the MarketAnalysis to get outputs for
            
        Returns:
            Dictionary with analysis_id, symbol, expert, and list of output_keys
        """
        return toolkit.get_analysis_outputs(analysis_id)
    
    @tool
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
    def get_analysis_outputs_batch_tool(requests: List[Dict[str, Any]], max_tokens: int = 100000) -> Dict[str, Any]:
        """Fetch multiple analysis outputs efficiently in a single call.
        
        Use this instead of calling get_analysis_output_detail_tool multiple times.
        Automatically handles truncation if content exceeds max_tokens limit.
        
        Args:
            requests: List of dicts, each with 'analysis_id' (int) and 'output_keys' (list of strings)
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
            requests = [
                {"analysis_id": 123, "output_keys": ["analysis_summary", "market_report"]},
                {"analysis_id": 124, "output_keys": ["news_report"]}
            ]
        """
        return toolkit.get_analysis_outputs_batch(requests, max_tokens)
    
    @tool
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
    def get_current_price_tool(symbol: str) -> float:
        """Get current bid price for an instrument.
        
        Args:
            symbol: Instrument symbol
            
        Returns:
            Current bid price as float
        """
        return toolkit.get_current_price(symbol)
    
    @tool
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
    
    @tool
    def calculate_position_metrics_tool(
        entry_price: float,
        current_price: float,
        quantity: float,
        direction: str
    ) -> Dict[str, float]:
        """Calculate position metrics without modifying anything.
        
        Args:
            entry_price: Entry price of the position
            current_price: Current market price
            quantity: Position size (number of shares/units)
            direction: Position direction: 'buy' or 'sell'
            
        Returns:
            Dict with pnl, pnl_percent, position_value, unrealized_pnl
        """
        return toolkit.calculate_position_metrics(entry_price, current_price, quantity, direction)
    
    return [
        get_analysis_outputs_tool,
        get_analysis_output_detail_tool,
        get_analysis_outputs_batch_tool,
        get_historical_analyses_tool,
        get_current_price_tool,
        close_position_tool,
        adjust_quantity_tool,
        update_stop_loss_tool,
        update_take_profit_tool,
        calculate_position_metrics_tool
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
        
        # Get settings
        settings = expert.settings
        user_instructions = settings.get("smart_risk_manager_user_instructions", 
                                         "Manage portfolio risk conservatively. Close positions with >10% loss or >20% gain.")
        
        # Get model and backend URL from settings with fallbacks
        risk_manager_model = settings.get("risk_manager_model") or "gpt-4o-mini"
        backend_url = os.getenv("OPENAI_BACKEND_URL", "https://api.openai.com/v1")
        api_key_setting_name = "openai_api_key"  # Default to OpenAI
        
        # If using NagaAI models, update backend URL
        if risk_manager_model.startswith("NagaAI/"):
            backend_url = "https://api.naga.ac/v1"
            api_key_setting_name = "naga_ai_api_key"
            risk_manager_model = risk_manager_model.replace("NagaAI/", "")
        elif risk_manager_model.startswith("OpenAI/"):
            risk_manager_model = risk_manager_model.replace("OpenAI/", "")
        
        # Get API key from database
        api_key = get_api_key_from_database(api_key_setting_name)
        
        max_iterations = settings.get("smart_risk_manager_max_iterations", 10)
        
        # Extract relevant expert settings for trading restrictions
        expert_config = {
            "enable_buy": settings.get("enable_buy", True),
            "enable_sell": settings.get("enable_sell", True),
            "enabled_instruments": expert.get_enabled_instruments() if hasattr(expert, 'get_enabled_instruments') else [],
            "max_virtual_equity_per_instrument_percent": settings.get("max_virtual_equity_per_instrument_percent", 100.0)
        }
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Get portfolio status
        portfolio_status = toolkit.get_portfolio_status()
        open_positions = portfolio_status.get("positions", [])
        
        # Create SmartRiskManagerJob record
        job = SmartRiskManagerJob(
            expert_instance_id=expert_instance_id,
            account_id=account_id,
            model_used=risk_manager_model,
            user_instructions=user_instructions,
            initial_portfolio_equity=float(portfolio_status["account_virtual_equity"]),
            final_portfolio_equity=float(portfolio_status["account_virtual_equity"]),
            status="RUNNING"
        )
        job_id = add_instance(job)
        logger.info(f"Created SmartRiskManagerJob {job_id}")
        
        # Create initial system message
        system_msg = SystemMessage(content=SYSTEM_INITIALIZATION_PROMPT.format(
            user_instructions=user_instructions
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
            "messages": [system_msg],
            "agent_scratchpad": "",
            "next_action": "",
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
    1. Calculate portfolio-level metrics
    2. Identify positions with significant P&L
    3. Check risk concentrations
    4. Generate prompt for LLM to assess portfolio health
    """
    logger.info("Analyzing portfolio...")
    
    try:
        portfolio_status = state["portfolio_status"]
        risk_manager_model = state["risk_manager_model"]
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        
        # Create LLM
        llm = create_llm(risk_manager_model, 0.1, backend_url, api_key)
        
        # Build portfolio summary for prompt
        portfolio_summary = f"""
Total Virtual Equity: ${portfolio_status['account_virtual_equity']:.2f}
Available Balance: ${portfolio_status['account_available_balance']:.2f}
Total Positions: {len(state['open_positions'])}

Open Positions:
"""
        for pos in state["open_positions"]:
            portfolio_summary += f"\n- {pos['symbol']}: {pos['quantity']} shares @ ${pos['current_price']:.2f}"
            portfolio_summary += f" | P&L: {pos['pnl_percent']:.2f}% (${pos['pnl']:.2f})"
            if pos.get("stop_loss_price"):
                portfolio_summary += f" | SL: ${pos['stop_loss_price']:.2f}"
            if pos.get("take_profit_price"):
                portfolio_summary += f" | TP: ${pos['take_profit_price']:.2f}"
        
        # Get LLM analysis
        analysis_prompt = PORTFOLIO_ANALYSIS_PROMPT.format(
            portfolio_status=portfolio_summary
        )
        
        response = llm.invoke([
            *state["messages"],
            HumanMessage(content=analysis_prompt)
        ])
        
        # Update scratchpad with analysis
        scratchpad = state["agent_scratchpad"] + "\n\n## Initial Portfolio Analysis\n" + response.content
        
        logger.info("Portfolio analysis complete")
        
        return {
            "messages": [HumanMessage(content=analysis_prompt), response],
            "agent_scratchpad": scratchpad
        }
        
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
    """
    logger.info("Checking recent market analyses for all symbols...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Fetch ALL recent analyses (no symbol filter)
        all_analyses = toolkit.get_recent_analyses(max_age_hours=72)
        
        # Build summary for scratchpad
        analyses_summary = f"\n\n## Recent Market Analyses (Last 72 hours)\n"
        analyses_summary += f"Total analyses available: {len(all_analyses)}\n\n"
        
        # Group by symbol for better readability
        by_symbol = {}
        for analysis in all_analyses:
            sym = analysis['symbol']
            if sym not in by_symbol:
                by_symbol[sym] = []
            by_symbol[sym].append(analysis)
        
        # Show summary grouped by symbol (limit to 20 symbols)
        for idx, (sym, analyses) in enumerate(list(by_symbol.items())[:20]):
            analyses_summary += f"- {sym}: {len(analyses)} analysis(es)\n"
            for analysis in analyses[:2]:  # Show up to 2 analyses per symbol
                # Extract recommendation details from summary if available
                summary_text = analysis.get('summary', '')
                rec_details = _extract_recommendation_from_summary(summary_text)
                
                # Format with recommendation details
                if rec_details:
                    analyses_summary += (
                        f"  [{analysis['analysis_id']}] {analysis['expert_name']} @ {analysis['timestamp']}\n"
                        f"    → {rec_details['action']} | Confidence: {rec_details['confidence']}% | "
                        f"Expected Profit: {rec_details['expected_profit']}% | Term: {rec_details['term']}\n"
                    )
                    if rec_details.get('key_factors'):
                        analyses_summary += f"    Key: {rec_details['key_factors']}\n"
                else:
                    # Fallback to simple format
                    analyses_summary += f"  [{analysis['analysis_id']}] {analysis['expert_name']} @ {analysis['timestamp']}\n"
        
        if len(by_symbol) > 20:
            analyses_summary += f"... and {len(by_symbol) - 20} more symbols\n"
        
        scratchpad = state["agent_scratchpad"] + analyses_summary
        
        logger.info(f"Found {len(all_analyses)} recent analyses across {len(by_symbol)} symbols")
        
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


def agent_decision_loop(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Main agent reasoning loop - decides next action.
    
    Steps:
    1. Build prompt with context
    2. Call LLM with tools available
    3. LLM decides to: research_more, take_action, or finish
    4. Update next_action in state
    5. Increment iteration_count
    """
    logger.info(f"Agent decision loop - iteration {state['iteration_count'] + 1}")
    
    try:
        risk_manager_model = state["risk_manager_model"]
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        
        # Create LLM
        llm = create_llm(risk_manager_model, 0.2, backend_url, api_key)
        
        # Build portfolio summary
        portfolio_status = state["portfolio_status"]
        portfolio_summary = f"Virtual Equity: ${portfolio_status['account_virtual_equity']:.2f} | "
        portfolio_summary += f"Positions: {len(state['open_positions'])}"
        
        # Build actions log summary
        actions_log_summary = "None yet" if not state["actions_log"] else "\n".join(
            f"- {action['action_type']}: {action.get('summary', 'No summary')}"
            for action in state["actions_log"]
        )
        
        # Build decision prompt
        decision_prompt = DECISION_LOOP_PROMPT.format(
            user_instructions=state["user_instructions"],
            iteration_count=state["iteration_count"] + 1,
            max_iterations=state["max_iterations"],
            portfolio_summary=portfolio_summary,
            agent_scratchpad=state["agent_scratchpad"],
            actions_log_summary=actions_log_summary
        )
        
        # Minimal logging - just track iteration
        logger.debug(f"Decision loop iteration {state['iteration_count'] + 1}: {len(state['messages'])} messages in context")
        
        # CRITICAL FIX: Filter out AIMessages with tool_calls and their corresponding ToolMessages
        # The decision loop LLM has no tools bound, so it can't process tool call history
        # We only need the conversational context, not the tool execution details
        filtered_messages = []
        skip_next_tool_messages = set()
        
        for msg in state["messages"]:
            if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
                # Mark tool call IDs to skip corresponding ToolMessages
                for tc in msg.tool_calls:
                    skip_next_tool_messages.add(tc["id"])
                # Create a version without tool calls for context
                filtered_messages.append(AIMessage(content=msg.content))
            elif isinstance(msg, ToolMessage):
                # Skip ToolMessages that correspond to filtered tool calls
                if msg.tool_call_id in skip_next_tool_messages:
                    continue
                filtered_messages.append(msg)
            else:
                filtered_messages.append(msg)
        
        logger.debug(f"Filtered {len(state['messages']) - len(filtered_messages)} tool-related messages from context")
        
        response = llm.invoke([
            *filtered_messages,
            HumanMessage(content=decision_prompt)
        ])
        
        # Parse decision from response
        content = response.content.lower()
        if "research_more" in content or "research more" in content:
            next_action = "research_more"
        elif "take_action" in content or "take action" in content:
            next_action = "take_action"
        elif "finish" in content:
            next_action = "finish"
        else:
            # Default to finish if unclear
            next_action = "finish"
            logger.warning(f"Unclear decision from LLM, defaulting to finish: {response.content}")
        
        logger.info(f"Agent decision: {next_action}")
        
        return {
            "messages": [HumanMessage(content=decision_prompt), response],
            "next_action": next_action,
            "iteration_count": state["iteration_count"] + 1
        }
        
    except Exception as e:
        logger.error(f"Error in agent decision loop: {e}", exc_info=True)
        job_id = state.get("job_id")
        if job_id:
            mark_job_as_failed(job_id, f"Error in agent_decision_loop: {str(e)}")
        raise


def research_node(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Research mode - autonomous agent that iteratively gathers analysis details.
    
    The research agent has its own conversation context and can call tools multiple times
    to gather information iteratively. It continues until it has enough data, then returns
    a summary to the main decision loop.
    
    Steps:
    1. Create isolated conversation context for research
    2. Give research agent access to all research tools
    3. Let agent iteratively call tools to gather data (up to 5 iterations)
    4. Return concise summary to main agent, not full conversation
    """
    logger.info("Entering research mode - autonomous research agent starting...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        risk_manager_model = state["risk_manager_model"]
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Create research-specific tools
        @tool
        def get_analysis_outputs_tool(analysis_id: int) -> Dict[str, Any]:
            """Get available output keys for a specific market analysis.
            
            Args:
                analysis_id: ID of the MarketAnalysis to get outputs for
                
            Returns:
                Dictionary with analysis_id, symbol, expert, and list of output_keys
            """
            return toolkit.get_analysis_outputs(analysis_id)
        
        @tool
        def get_analysis_output_detail_tool(analysis_id: int, output_key: str) -> Dict[str, Any]:
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
        def get_analysis_outputs_batch_tool(requests: List[Dict[str, Any]], max_tokens: int = 100000) -> Dict[str, Any]:
            """Fetch multiple analysis outputs efficiently in a single call.
            
            Use this instead of calling get_analysis_output_detail_tool multiple times.
            Automatically handles truncation if content exceeds max_tokens limit.
            
            Args:
                requests: List of dicts, each with 'analysis_id' (int) and 'output_keys' (list of strings)
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
                requests = [
                    {"analysis_id": 123, "output_keys": ["analysis_summary", "market_report"]},
                    {"analysis_id": 124, "output_keys": ["news_report"]}
                ]
            """
            return toolkit.get_analysis_outputs_batch(requests, max_tokens)
        
        @tool
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
        def get_current_price_tool(symbol: str) -> float:
            """Get current bid price for an instrument.
            
            Args:
                symbol: Instrument symbol
                
            Returns:
                Current bid price as float
            """
            return toolkit.get_current_price(symbol)
        
        @tool
        def finish_research_tool(summary: str) -> str:
            """Call this when you have gathered enough information and are ready to return to decision making.
            
            Args:
                summary: Concise summary of key findings from your research (2-3 paragraphs)
                
            Returns:
                Confirmation message
            """
            return f"Research complete. Summary recorded: {summary[:100]}..."
        
        research_tools = [
            get_analysis_outputs_tool,
            get_analysis_output_detail_tool,
            get_analysis_outputs_batch_tool,
            get_historical_analyses_tool,
            get_current_price_tool,
            finish_research_tool
        ]
        
        # Create LLM with tools for autonomous research
        llm = create_llm(risk_manager_model, 0.2, backend_url, api_key)
        llm_with_tools = llm.bind_tools(research_tools)
        
        # Build initial research prompt with context
        recent_analyses_summary = "\n".join(
            f"[{a['analysis_id']}] {a['symbol']} - {a['expert_name']} @ {a['timestamp']}"
            for a in state["recent_analyses"][:20]
        )
        
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
        
        research_system_prompt = f"""You are a research specialist for portfolio risk management.

## YOUR MISSION
Investigate market analyses to gather detailed information that will help make risk management decisions.

## PORTFOLIO CONTEXT
{state.get('agent_scratchpad', 'No prior context')}

## AVAILABLE ANALYSES
{recent_analyses_summary}

## YOUR TASK
Use the available tools to investigate analyses that are most relevant to the portfolio's risk management needs.
You can call tools multiple times to gather information iteratively. When you have gathered enough information,
call finish_research_tool() with a concise summary of your key findings.

Focus on:
- Positions currently held in the portfolio
- High-confidence recommendations (BUY/SELL with >70% confidence)
- Recent analyses with significant profit expectations
- Risk factors and stop loss recommendations

{expert_instructions}"""

        # Initialize research conversation
        research_messages = [
            SystemMessage(content=research_system_prompt),
            HumanMessage(content="Begin your research. Investigate the most relevant analyses and gather detailed information.")
        ]
        
        # Autonomous research loop (max 5 iterations to prevent infinite loops)
        max_research_iterations = 15
        detailed_cache = state["detailed_outputs_cache"].copy()
        research_complete = False
        final_summary = ""
        
        logger.info(f"Research agent starting with {len(research_tools)} available tools: {[t.name for t in research_tools]}")
        
        for iteration in range(max_research_iterations):
            logger.info(f"Research iteration {iteration + 1}/{max_research_iterations}")
            
            # Get LLM response with tool calls
            response = llm_with_tools.invoke(research_messages)
            research_messages.append(response)
            
            logger.info(f"Research iteration {iteration + 1}: LLM returned {len(response.tool_calls) if response.tool_calls else 0} tool calls")
            
            # Check if research is complete
            if not response.tool_calls:
                logger.info("Research agent finished without tool calls")
                final_summary = response.content
                break
            
            # Execute tool calls
            for tool_call in response.tool_calls:
                tool_name = tool_call.get("name")
                tool_args = tool_call.get("args", {})
                tool_call_id = tool_call.get("id")
                
                logger.debug(f"Research tool: {tool_name} with args {tool_args}")
                
                # Check for finish signal
                if tool_name == "finish_research_tool":
                    final_summary = tool_args.get("summary", response.content)
                    research_complete = True
                    research_messages.append(ToolMessage(
                        content="Research complete. Returning to decision loop.",
                        tool_call_id=tool_call_id
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
                            tool_call_id=tool_call_id
                        ))
                        
                    except Exception as e:
                        logger.error(f"Error executing research tool {tool_name}: {e}", exc_info=True)
                        research_messages.append(ToolMessage(
                            content=f"Error: {str(e)}",
                            tool_call_id=tool_call_id
                        ))
                else:
                    logger.warning(f"Tool {tool_name} not found in research_tools")
                    research_messages.append(ToolMessage(
                        content=f"Error: Tool {tool_name} not available",
                        tool_call_id=tool_call_id
                    ))
            
            if research_complete:
                break
        
        # If we hit max iterations without finish, extract summary from last response
        if not final_summary:
            final_summary = f"Research completed after {max_research_iterations} iterations. " + \
                          (research_messages[-1].content if research_messages else "No findings.")
        
        logger.info(f"Research complete after {iteration + 1} iterations")
        
        # Return ONLY summary to main agent, not the full research conversation
        return {
            "messages": [
                HumanMessage(content="Research findings summary:"),
                AIMessage(content=final_summary)
            ],
            "detailed_outputs_cache": detailed_cache,
            "agent_scratchpad": state["agent_scratchpad"] + f"\n\n## Research Findings\n{final_summary}\n",
            "next_action": ""  # Force return to decision loop
        }
        
    except Exception as e:
        logger.error(f"Error in research node: {e}", exc_info=True)
        job_id = state.get("job_id")
        if job_id:
            mark_job_as_failed(job_id, f"Error in research_node: {str(e)}")
        raise


def action_node(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Action mode - execute trading operations.
    
    Steps:
    1. LLM decides which action(s) to take using tools
    2. Execute tool calls for trading actions
    3. Record results in actions_log
    4. Update portfolio_status with new data
    """
    logger.info("Entering action mode...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        risk_manager_model = state["risk_manager_model"]
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Create action-specific tools directly
        @tool
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
        
        @tool
        def get_current_price_tool(symbol: str) -> float:
            """Get current bid price for an instrument.
            
            Args:
                symbol: Instrument symbol
                
            Returns:
                Current bid price as float
            """
            return toolkit.get_current_price(symbol)
        
        @tool
        def calculate_position_metrics_tool(
            entry_price: float,
            current_price: float,
            quantity: float,
            direction: str
        ) -> Dict[str, float]:
            """Calculate position metrics without modifying anything.
            
            Args:
                entry_price: Entry price of the position
                current_price: Current market price
                quantity: Position size (number of shares/units)
                direction: Position direction: 'buy' or 'sell'
                
            Returns:
                Dict with pnl, pnl_percent, position_value, unrealized_pnl
            """
            return toolkit.calculate_position_metrics(entry_price, current_price, quantity, direction)
        
        action_tools = [
            close_position_tool,
            adjust_quantity_tool,
            update_stop_loss_tool,
            update_take_profit_tool,
            get_current_price_tool,
            calculate_position_metrics_tool
        ]
        
        # Create LLM with tools
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        llm = create_llm(risk_manager_model, 0.1, backend_url, api_key)
        llm_with_tools = llm.bind_tools(action_tools)
        
        logger.info(f"Action agent starting with {len(action_tools)} available tools: {[t.name for t in action_tools]}")
        
        # Build portfolio summary
        portfolio_status = state["portfolio_status"]
        portfolio_summary = f"""
Virtual Equity: ${portfolio_status['account_virtual_equity']:.2f}
Positions: {len(state['open_positions'])}

Open Positions:
"""
        for pos in state["open_positions"]:
            portfolio_summary += f"\n- Transaction {pos['transaction_id']}: {pos['symbol']} "
            portfolio_summary += f"{pos['quantity']} shares @ ${pos['current_price']:.2f} | "
            portfolio_summary += f"P&L: {pos['pnl_percent']:.2f}%"
            if pos.get("stop_loss_price"):
                portfolio_summary += f" | SL: ${pos['stop_loss_price']:.2f}"
            if pos.get("take_profit_price"):
                portfolio_summary += f" | TP: ${pos['take_profit_price']:.2f}"
        
        # Get action plan from LLM
        expert_settings = state["expert_settings"]
        
        # Format trading restrictions
        buy_status = "✅ ENABLED" if expert_settings.get("enable_buy", True) else "❌ DISABLED - DO NOT attempt to open long positions"
        sell_status = "✅ ENABLED" if expert_settings.get("enable_sell", True) else "❌ DISABLED - DO NOT attempt to open short positions"
        enabled_instruments = expert_settings.get("enabled_instruments", [])
        max_position_pct = expert_settings.get("max_virtual_equity_per_instrument_percent", 100.0)
        
        action_prompt = ACTION_PROMPT.format(
            portfolio_summary=portfolio_summary,
            agent_scratchpad=state["agent_scratchpad"],
            user_instructions=state["user_instructions"],
            buy_status=buy_status,
            sell_status=sell_status,
            enabled_instruments=enabled_instruments,
            max_position_pct=max_position_pct
        )
        
        # First call: Let LLM decide which actions to take (tools bound locally)
        response = llm_with_tools.invoke([
            *state["messages"],
            HumanMessage(content=action_prompt)
        ])
        
        logger.info(f"Action agent: LLM returned {len(response.tool_calls) if response.tool_calls else 0} tool calls")

        actions_log = state["actions_log"].copy()
        detailed_action_reports = []

        # Execute all tool calls locally and record results (tools are local to this node)
        if response.tool_calls:
            logger.info(f"Executing {len(response.tool_calls)} trading actions")
            for tool_call in response.tool_calls:
                tool_name = tool_call.get("name")
                tool_args = tool_call.get("args", {})
                tool_call_id = tool_call.get("id")

                logger.debug(f"Action tool call: {tool_name} id={tool_call_id} args={tool_args}")

                matching_tool = next((t for t in action_tools if t.name == tool_name), None)
                if matching_tool:
                    try:
                        logger.info(f"🔧 Action Tool Call: {tool_name} | Args: {json.dumps(tool_args)}")
                        result = matching_tool.invoke(tool_args)
                        logger.info(f"✅ Action Tool Result: {tool_name} | Success: {result.get('success', 'N/A')} | {result.get('message', str(result)[:100])}")

                        # Record action in log (for trading actions)
                        if tool_name in ['close_position_tool', 'adjust_quantity_tool',
                                         'update_stop_loss_tool', 'update_take_profit_tool']:
                            action_record = {
                                "iteration": state["iteration_count"],
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "action_type": tool_name.replace("_tool", ""),
                                "arguments": tool_args,
                                "result": result,
                                "success": result.get("success", False)
                            }
                            actions_log.append(action_record)
                            logger.info(f"Action executed: {tool_name} - success={result.get('success', False)}")

                        # Save a human-readable report for LLM summary
                        detailed_action_reports.append({
                            "tool": tool_name,
                            "args": tool_args,
                            "result": result
                        })

                    except Exception as e:
                        logger.error(f"Error executing action tool {tool_name}: {e}", exc_info=True)
                        action_record = {
                            "iteration": state["iteration_count"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "action_type": tool_name.replace("_tool", ""),
                            "arguments": tool_args,
                            "error": str(e),
                            "success": False
                        }
                        actions_log.append(action_record)
                        detailed_action_reports.append({
                            "tool": tool_name,
                            "args": tool_args,
                            "error": str(e)
                        })
                else:
                    logger.warning(f"Requested tool {tool_name} not available in action_tools")
                    action_record = {
                        "iteration": state["iteration_count"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "action_type": tool_name.replace("_tool", ""),
                        "arguments": tool_args,
                        "error": f"Tool {tool_name} not found",
                        "success": False
                    }
                    actions_log.append(action_record)
                    detailed_action_reports.append({
                        "tool": tool_name,
                        "args": tool_args,
                        "error": f"Tool {tool_name} not found"
                    })
        else:
            # No tool calls - record LLM reasoning as no_action
            logger.info("LLM decided not to take any actions")
            action_record = {
                "iteration": state["iteration_count"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action_type": "no_action",
                "summary": "Agent decided no actions needed",
                "llm_reasoning": response.content
            }
            actions_log.append(action_record)
            detailed_action_reports.append({
                "tool": "no_action",
                "reasoning": response.content
            })

        # Build an AI-summary of actions for the decision loop
        summary_lines = [f"Actions executed: {len(detailed_action_reports)} items"]
        for r in detailed_action_reports:
            tool_label = r.get("tool")
            if r.get("result"):
                res = r["result"]
                summary_lines.append(f"- {tool_label}: success={res.get('success', 'unknown')}, details={str(res)}")
            else:
                summary_lines.append(f"- {tool_label}: error={r.get('error', r.get('reasoning', 'no details'))}")

        actions_summary = "\n".join(summary_lines)

        # Refresh portfolio status after actions
        portfolio_status = toolkit.get_portfolio_status()
        open_positions = portfolio_status.get("positions", [])

        logger.info(f"Action mode complete. {len(actions_log) - len(state['actions_log'])} new actions recorded")

        # Return only HumanMessage + AIMessage (no ToolMessage objects)
        return {
            "messages": [
                HumanMessage(content=action_prompt),
                AIMessage(content=actions_summary)
            ],
            "actions_log": actions_log,
            "portfolio_status": portfolio_status,
            "open_positions": open_positions,
            "next_action": ""  # Force return to decision loop
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
        initial_summary = f"Virtual Equity: ${initial_portfolio['account_virtual_equity']:.2f} | Positions: {len(state['open_positions'])}"
        
        # Get final portfolio status
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        final_portfolio = toolkit.get_portfolio_status()
        final_summary = f"Virtual Equity: ${final_portfolio['account_virtual_equity']:.2f} | Positions: {len(final_portfolio.get('positions', []))}"
        
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
        
        # Update SmartRiskManagerJob
        with get_db() as session:
            job = session.get(SmartRiskManagerJob, job_id)
            if job:
                job.status = "COMPLETED"
                job.final_portfolio_equity = float(final_portfolio["account_virtual_equity"])
                job.actions_taken_count = len(state["actions_log"])
                job.actions_summary = response.content
                job.iteration_count = state["iteration_count"]
                job.graph_state = {
                    "open_positions": state["open_positions"],
                    "actions_log": state["actions_log"],
                    "final_scratchpad": state["agent_scratchpad"]
                }
                session.add(job)
                session.commit()
                logger.info(f"Updated SmartRiskManagerJob {job_id} to COMPLETED")
        
        logger.info("Smart Risk Manager session finalized")
        
        return {
            "messages": [HumanMessage(content=finalization_prompt), response],
            "agent_scratchpad": state["agent_scratchpad"] + "\n\n## Final Summary\n" + response.content
        }
        
    except Exception as e:
        logger.error(f"Error finalizing: {e}", exc_info=True)
        
        # Mark job as FAILED
        job_id = state.get("job_id")
        if job_id:
            mark_job_as_failed(job_id, f"Error in finalize: {str(e)}")
        
        raise


# ==================== CONDITIONAL ROUTING ====================

def should_continue(state: SmartRiskManagerState) -> str:
    """
    Determine which node to execute next based on agent's decision.
    
    Logic:
    - If iteration_count >= max_iterations: return "finalize"
    - If next_action == "research_more": return "research_node"
    - If next_action == "take_action": return "action_node"
    - If next_action == "finish": return "finalize"
    - Else: return "agent_decision_loop"
    """
    # Check iteration limit
    if state["iteration_count"] >= state["max_iterations"]:
        logger.warning(f"Max iterations ({state['max_iterations']}) reached, finalizing")
        return "finalize"
    
    next_action = state.get("next_action", "")
    
    if next_action == "research_more":
        return "research_node"
    elif next_action == "take_action":
        return "action_node"
    elif next_action == "finish":
        return "finalize"
    else:
        # Shouldn't happen, but loop back to decision
        return "agent_decision_loop"


# ==================== GRAPH CONSTRUCTION ====================

def build_smart_risk_manager_graph(expert_instance_id: int, account_id: int) -> StateGraph:
    """
    Build the complete LangGraph workflow.
    
    Args:
        expert_instance_id: ID of the ExpertInstance
        account_id: ID of the AccountDefinition
        
    Returns:
        Compiled StateGraph ready for execution
    """
    logger.info(f"Building Smart Risk Manager graph for expert {expert_instance_id}, account {account_id}")
    
    # Create workflow
    workflow = StateGraph(SmartRiskManagerState)
    
    # Add nodes
    workflow.add_node("initialize_context", initialize_context)
    workflow.add_node("analyze_portfolio", analyze_portfolio)
    workflow.add_node("check_recent_analyses", check_recent_analyses)
    workflow.add_node("agent_decision_loop", agent_decision_loop)
    workflow.add_node("research_node", research_node)
    workflow.add_node("action_node", action_node)
    workflow.add_node("finalize", finalize)
    
    # Add edges
    workflow.set_entry_point("initialize_context")
    workflow.add_edge("initialize_context", "analyze_portfolio")
    workflow.add_edge("analyze_portfolio", "check_recent_analyses")
    workflow.add_edge("check_recent_analyses", "agent_decision_loop")
    
    # Conditional routing from agent_decision_loop
    workflow.add_conditional_edges(
        "agent_decision_loop",
        should_continue,
        {
            "research_node": "research_node",
            "action_node": "action_node",
            "finalize": "finalize",
            "agent_decision_loop": "agent_decision_loop"
        }
    )
    
    # Loop back to decision loop
    workflow.add_edge("research_node", "agent_decision_loop")
    workflow.add_edge("action_node", "agent_decision_loop")
    
    # End
    workflow.add_edge("finalize", END)
    
    logger.info("Smart Risk Manager graph built successfully")
    
    return workflow.compile()


# ==================== EXECUTION ENTRY POINT ====================

def run_smart_risk_manager(expert_instance_id: int, account_id: int) -> Dict[str, Any]:
    """
    Main entry point for running the Smart Risk Manager.
    
    Args:
        expert_instance_id: ID of the ExpertInstance
        account_id: ID of the AccountDefinition
        
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
            "job_id": 0,  # Will be created in initialize_context
            "portfolio_status": {},
            "open_positions": [],
            "recent_analyses": [],
            "detailed_outputs_cache": {},
            "messages": [],
            "agent_scratchpad": "",
            "next_action": "",
            "actions_log": [],
            "iteration_count": 0,
            "max_iterations": 10
        }
        
        # Run graph
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
        
        return {
            "success": False,
            "job_id": job_id,
            "error": str(e)
        }
