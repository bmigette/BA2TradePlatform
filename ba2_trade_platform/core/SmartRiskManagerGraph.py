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
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END, START
from langgraph.prebuilt import ToolNode
from sqlmodel import select
import json
import os

from ..logger import logger
from .. import config as config_module
from .models import SmartRiskManagerJob, SmartRiskManagerJobAnalysis, MarketAnalysis
from .db import get_db, add_instance, update_instance, get_instance
from .models import AppSetting
from .SmartRiskManagerToolkit import SmartRiskManagerToolkit
from .utils import get_expert_instance_from_id


# ==================== HELPER FUNCTIONS ====================

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


def create_llm(model: str, temperature: float, base_url: str, api_key: str) -> ChatOpenAI:
    """
    Create a ChatOpenAI instance with proper configuration.
    
    Args:
        model: Model name (e.g., 'gpt-4o-mini')
        temperature: Temperature for generation
        base_url: Base URL for API endpoint
        api_key: API key for authentication
        
    Returns:
        Configured ChatOpenAI instance
    """
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        base_url=base_url,
        api_key=api_key
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

## AVAILABLE RECENT ANALYSES
{recent_analyses_summary}

## TASK
1. Identify which market analyses you want to investigate in detail
2. For each analysis, specify which output keys you want to read (use get_analysis_outputs to see available keys)
3. Use get_analysis_output_detail to read the full content of specific outputs

Focus on analyses that will help you make informed risk management decisions.

## WHAT TO LOOK FOR
- Analyst sentiment and recommendations
- Technical indicators and price targets
- Fundamental concerns or opportunities
- Risk factors mentioned by analysts

Provide your research plan and I'll help you execute it.
"""

ACTION_PROMPT = """You are ready to execute risk management actions.

## CURRENT SITUATION
{portfolio_summary}

## RESEARCH FINDINGS
{agent_scratchpad}

## AVAILABLE ACTIONS
You have access to these trading tools:
- **close_position(transaction_id, reason)** - Close an entire position
- **adjust_quantity(transaction_id, new_quantity, reason)** - Partial close or add to position
- **update_stop_loss(transaction_id, new_sl_price, reason)** - Update stop loss
- **update_take_profit(transaction_id, new_tp_price, reason)** - Update take profit
- **open_new_position(symbol, direction, quantity, tp_price, sl_price, reason)** - Open new position (use cautiously)

## GUIDELINES FOR ACTIONS
{user_instructions}

## YOUR TASK
1. Decide which action(s) to take
2. For EACH action, provide clear reasoning that references your research
3. Be specific about parameters (transaction_id, prices, quantities)
4. Execute the actions using the appropriate tools

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
        return toolkit.get_analysis_output_detail(analysis_id, output_key)
    
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
            initial_portfolio_value=float(portfolio_status["account_virtual_equity"]),
            final_portfolio_value=float(portfolio_status["account_virtual_equity"]),
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
        raise


def check_recent_analyses(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Load recent market analyses for all open positions.
    
    Steps:
    1. Get symbols from open_positions
    2. Call get_recent_analyses() for each symbol
    3. Store in recent_analyses
    4. Add summaries to agent_scratchpad
    """
    logger.info("Checking recent market analyses...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        
        # Create toolkit
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        
        # Get unique symbols
        symbols = list(set(pos["symbol"] for pos in state["open_positions"]))
        
        # Fetch recent analyses
        all_analyses = []
        for symbol in symbols:
            analyses = toolkit.get_recent_analyses(symbol=symbol, max_age_hours=72)
            if analyses:
                all_analyses.extend(analyses)
        
        # Sort by created_at (most recent first)
        all_analyses.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        # Build summary for scratchpad
        analyses_summary = f"\n\n## Recent Market Analyses (Last 72 hours)\n"
        analyses_summary += f"Total analyses available: {len(all_analyses)}\n\n"
        
        for analysis in all_analyses[:10]:  # Show top 10
            analyses_summary += f"- [{analysis['id']}] {analysis['symbol']} - {analysis['expert']} @ {analysis['created_at']}\n"
        
        scratchpad = state["agent_scratchpad"] + analyses_summary
        
        logger.info(f"Found {len(all_analyses)} recent analyses")
        
        return {
            "recent_analyses": all_analyses,
            "agent_scratchpad": scratchpad
        }
        
    except Exception as e:
        logger.error(f"Error checking recent analyses: {e}", exc_info=True)
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
        
        response = llm.invoke([
            *state["messages"],
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
            logger.warning(f"Unclear decision from LLM, defaulting to finish: {response.content[:100]}")
        
        logger.info(f"Agent decision: {next_action}")
        
        return {
            "messages": [HumanMessage(content=decision_prompt), response],
            "next_action": next_action,
            "iteration_count": state["iteration_count"] + 1
        }
        
    except Exception as e:
        logger.error(f"Error in agent decision loop: {e}", exc_info=True)
        raise


def research_node(state: SmartRiskManagerState) -> Dict[str, Any]:
    """
    Research mode - get detailed analysis outputs.
    
    Steps:
    1. LLM selects which analyses to investigate using tools
    2. Execute tool calls to get analysis details
    3. Store results in detailed_outputs_cache
    4. Add findings to agent_scratchpad
    """
    logger.info("Entering research mode...")
    
    try:
        expert_instance_id = state["expert_instance_id"]
        account_id = state["account_id"]
        risk_manager_model = state["risk_manager_model"]
        
        # Create toolkit and tools
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        tools = create_toolkit_tools(toolkit)
        
        # Filter to research-only tools
        research_tools = [t for t in tools if t.name in [
            'get_analysis_outputs_tool',
            'get_analysis_output_detail_tool',
            'get_historical_analyses_tool',
            'get_current_price_tool',
            'calculate_position_metrics_tool'
        ]]
        
        # Create LLM with tools
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        llm = create_llm(risk_manager_model, 0.2, backend_url, api_key)
        llm_with_tools = llm.bind_tools(research_tools)
        
        # Build analyses summary
        recent_analyses_summary = "\n".join(
            f"[{a['id']}] {a['symbol']} - {a['expert']} @ {a['created_at']}"
            for a in state["recent_analyses"][:20]
        )
        
        # Get research plan from LLM
        research_prompt = RESEARCH_PROMPT.format(
            recent_analyses_summary=recent_analyses_summary
        )
        
        # First call: Let LLM decide which tools to use
        response = llm_with_tools.invoke([
            *state["messages"],
            HumanMessage(content=research_prompt)
        ])
        
        messages = [HumanMessage(content=research_prompt), response]
        research_findings = "\n\n## Research Findings\n"
        detailed_cache = state["detailed_outputs_cache"].copy()
        
        # Execute tool calls if LLM made any
        if response.tool_calls:
            logger.info(f"Executing {len(response.tool_calls)} tool calls for research")
            
            for tool_call in response.tool_calls[:5]:  # Limit to 5 tool calls
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                
                # Find and execute the tool
                matching_tool = next((t for t in research_tools if t.name == tool_name), None)
                if matching_tool:
                    try:
                        result = matching_tool.invoke(tool_args)
                        
                        # Cache analysis outputs
                        if tool_name == "get_analysis_output_detail_tool":
                            analysis_id = tool_args["analysis_id"]
                            output_key = tool_args["output_key"]
                            if analysis_id not in detailed_cache:
                                detailed_cache[analysis_id] = {}
                            detailed_cache[analysis_id][output_key] = result.get("content", "")
                        
                        # Add to findings
                        research_findings += f"\n### Tool: {tool_name}\nArgs: {tool_args}\n"
                        research_findings += f"Result: {str(result)[:300]}...\n"
                        
                        # Add tool message
                        messages.append(ToolMessage(
                            content=json.dumps(result),
                            tool_call_id=tool_call["id"]
                        ))
                        
                    except Exception as e:
                        logger.error(f"Error executing tool {tool_name}: {e}")
                        messages.append(ToolMessage(
                            content=f"Error: {str(e)}",
                            tool_call_id=tool_call["id"]
                        ))
        else:
            # No tool calls, use fallback approach (read top 3 analyses)
            logger.info("No tool calls from LLM, using fallback research")
            
            for analysis in state["recent_analyses"][:3]:
                analysis_id = analysis["id"]
                
                # Get available outputs
                outputs = toolkit.get_analysis_outputs(analysis_id)
                
                # Read key outputs
                for output_key in outputs.get("output_keys", [])[:2]:
                    detail = toolkit.get_analysis_output_detail(analysis_id, output_key)
                    
                    # Cache it
                    if analysis_id not in detailed_cache:
                        detailed_cache[analysis_id] = {}
                    detailed_cache[analysis_id][output_key] = detail["content"]
                    
                    # Add to findings
                    research_findings += f"\n### Analysis {analysis_id} - {output_key}\n"
                    research_findings += detail["content"][:500] + "...\n"
        
        scratchpad = state["agent_scratchpad"] + research_findings
        
        logger.info("Research complete")
        
        return {
            "messages": messages,
            "detailed_outputs_cache": detailed_cache,
            "agent_scratchpad": scratchpad,
            "next_action": ""  # Force return to decision loop
        }
        
    except Exception as e:
        logger.error(f"Error in research node: {e}", exc_info=True)
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
        
        # Create toolkit and tools
        toolkit = SmartRiskManagerToolkit(expert_instance_id, account_id)
        tools = create_toolkit_tools(toolkit)
        
        # Filter to action-only tools
        action_tools = [t for t in tools if t.name in [
            'close_position_tool',
            'adjust_quantity_tool',
            'update_stop_loss_tool',
            'update_take_profit_tool',
            'get_current_price_tool',  # Allow price checks
            'calculate_position_metrics_tool'  # Allow calculations
        ]]
        
        # Create LLM with tools
        backend_url = state["backend_url"]
        api_key = state["api_key"]
        llm = create_llm(risk_manager_model, 0.1, backend_url, api_key)
        llm_with_tools = llm.bind_tools(action_tools)
        
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
        action_prompt = ACTION_PROMPT.format(
            portfolio_summary=portfolio_summary,
            agent_scratchpad=state["agent_scratchpad"],
            user_instructions=state["user_instructions"]
        )
        
        # First call: Let LLM decide which actions to take
        response = llm_with_tools.invoke([
            *state["messages"],
            HumanMessage(content=action_prompt)
        ])
        
        messages = [HumanMessage(content=action_prompt), response]
        actions_log = state["actions_log"].copy()
        
        # Execute tool calls if LLM made any
        if response.tool_calls:
            logger.info(f"Executing {len(response.tool_calls)} trading actions")
            
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                
                # Find and execute the tool
                matching_tool = next((t for t in action_tools if t.name == tool_name), None)
                if matching_tool:
                    try:
                        result = matching_tool.invoke(tool_args)
                        
                        # Record action in log (only for actual trading actions)
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
                            
                            logger.info(f"Action executed: {tool_name} - Success: {result.get('success', False)}")
                        
                        # Add tool message
                        messages.append(ToolMessage(
                            content=json.dumps(result),
                            tool_call_id=tool_call["id"]
                        ))
                        
                    except Exception as e:
                        logger.error(f"Error executing action {tool_name}: {e}", exc_info=True)
                        
                        # Record failed action
                        action_record = {
                            "iteration": state["iteration_count"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "action_type": tool_name.replace("_tool", ""),
                            "arguments": tool_args,
                            "error": str(e),
                            "success": False
                        }
                        actions_log.append(action_record)
                        
                        messages.append(ToolMessage(
                            content=f"Error: {str(e)}",
                            tool_call_id=tool_call["id"]
                        ))
        else:
            # No tool calls - log that LLM decided not to take action
            logger.info("LLM decided not to take any actions")
            action_record = {
                "iteration": state["iteration_count"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action_type": "no_action",
                "summary": "Agent decided no actions needed",
                "llm_reasoning": response.content[:500]
            }
            actions_log.append(action_record)
        
        # Refresh portfolio status after actions
        portfolio_status = toolkit.get_portfolio_status()
        open_positions = portfolio_status.get("positions", [])
        
        logger.info(f"Action mode complete. {len(actions_log) - len(state['actions_log'])} new actions recorded")
        
        return {
            "messages": messages,
            "actions_log": actions_log,
            "portfolio_status": portfolio_status,
            "open_positions": open_positions,
            "next_action": ""  # Force return to decision loop
        }
        
    except Exception as e:
        logger.error(f"Error in action node: {e}", exc_info=True)
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
                job.final_portfolio_value = float(final_portfolio["account_virtual_equity"])
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
    
    try:
        # Build graph
        graph = build_smart_risk_manager_graph(expert_instance_id, account_id)
        
        # Initialize state
        initial_state = {
            "expert_instance_id": expert_instance_id,
            "account_id": account_id,
            "user_instructions": "",  # Will be loaded in initialize_context
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
        
        return {
            "success": True,
            "job_id": final_state["job_id"],
            "iterations": final_state["iteration_count"],
            "actions_count": len(final_state["actions_log"]),
            "summary": final_state["agent_scratchpad"].split("## Final Summary\n")[-1] if "## Final Summary\n" in final_state["agent_scratchpad"] else "No summary available"
        }
        
    except Exception as e:
        logger.error(f"Error running Smart Risk Manager: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
