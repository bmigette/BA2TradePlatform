"""
Database utilities for storing TradingAgents analysis outputs
"""
from typing import Optional, Dict, Any, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from ba2_trade_platform.core.types import MarketAnalysisStatus
from datetime import datetime, timezone
from . import logger





def update_market_analysis_status(analysis_id: int, status: Union[str, 'MarketAnalysisStatus'], state: Dict[str, Any] = None):
    """
    Update the status and state of a MarketAnalysis record
    
    Args:
        analysis_id: MarketAnalysis ID
        status: New status (string or MarketAnalysisStatus enum)
        state: Updated state data (will be merged with existing state)
    """
    try:
        from ba2_trade_platform.core.db import get_instance, update_instance
        from ba2_trade_platform.core.models import MarketAnalysis
        from ba2_trade_platform.core.types import MarketAnalysisStatus
        
        analysis = get_instance(MarketAnalysis, analysis_id)
        if analysis:
            # Handle both string and enum status
            if isinstance(status, str):
                # Convert string to enum for consistency
                try:
                    analysis.status = MarketAnalysisStatus(status.lower())
                except ValueError:
                    # If conversion fails, try to match with enum values
                    status_upper = status.upper()
                    for enum_status in MarketAnalysisStatus:
                        if enum_status.value.upper() == status_upper:
                            analysis.status = enum_status
                            break
                    else:
                        logger.warning(f"Unknown status '{status}', using as-is")
                        analysis.status = status
            else:
                analysis.status = status
            if state:
                # Initialize state if it doesn't exist
                if analysis.state is None:
                    analysis.state = {}
                
                # Create or update the trading_agent_graph key without overriding other state
                if 'trading_agent_graph' not in analysis.state:
                    analysis.state['trading_agent_graph'] = {}
                
                # Preserve existing keys in trading_agent_graph - merge intelligently
                existing_graph_state = analysis.state['trading_agent_graph']
                
                # Create a merged state that preserves existing keys
                merged_state = existing_graph_state.copy()  # Start with existing state
                
                # Update with new state - this will override existing keys with same names
                # but preserve keys that exist in database but not in new state
                merged_state.update(state)
                
                # Replace the trading_agent_graph with merged state
                analysis.state['trading_agent_graph'] = merged_state
                
                # Explicitly mark the state field as modified for SQLAlchemy
                from sqlalchemy.orm import attributes
                attributes.flag_modified(analysis, "state")
                
            update_instance(analysis)
            logger.debug(f"Updated MarketAnalysis {analysis_id} status to {status}")
    except Exception as e:
        logger.error(f"Error updating MarketAnalysis {analysis_id}: {e}", exc_info=True)


def get_market_analysis_status(analysis_id: int) -> Optional[str]:
    """
    Get the current status of a MarketAnalysis record
    
    Args:
        analysis_id: MarketAnalysis ID
        
    Returns:
        Status string (e.g., "FAILED", "COMPLETED", "RUNNING") or None if not found
    """
    try:
        from ba2_trade_platform.core.db import get_instance
        from ba2_trade_platform.core.models import MarketAnalysis
        
        analysis = get_instance(MarketAnalysis, analysis_id)
        if analysis:
            # Return the status as a string (whether it's an enum or string)
            return analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status)
        return None
    except Exception as e:
        logger.error(f"Error getting MarketAnalysis {analysis_id} status: {e}", exc_info=True)
        return None


def store_analysis_output(market_analysis_id: int, name: str, output_type: str, text: str = None, blob: bytes = None):
    """
    Store an analysis output in the database
    
    Args:
        market_analysis_id: ID of the associated MarketAnalysis
        name: Name/identifier for this output
        output_type: Type of output (e.g., "tool_call", "agent_report", "news_data")
        text: Text content
        blob: Binary content
        
    Returns:
        AnalysisOutput ID if successful, None if failed
    """
    try:
        from ba2_trade_platform.core.db import add_instance
        from ba2_trade_platform.core.models import AnalysisOutput
        
        output = AnalysisOutput(
            market_analysis_id=market_analysis_id,
            name=name,
            type=output_type,
            text=text,
            blob=blob
        )
        
        return add_instance(output)
    except Exception as e:
        logger.error(f"Error storing AnalysisOutput: {e}", exc_info=True)
        return None


def store_tool_call_output(market_analysis_id: int, tool_name: str, inputs: Dict[str, Any], output: str, agent_type: str = "unknown"):
    """
    Store tool call output in the database
    
    Args:
        market_analysis_id: ID of the associated MarketAnalysis
        tool_name: Name of the tool that was called
        inputs: Tool inputs
        output: Tool output
        agent_type: Type of agent that called the tool
        
    Returns:
        AnalysisOutput ID if successful, None if failed
    """
    import json
    
    # Create a comprehensive record of the tool call
    tool_record = {
        "tool_name": tool_name,
        "agent_type": agent_type,
        "inputs": inputs,
        "output": output,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    return store_analysis_output(
        market_analysis_id=market_analysis_id,
        name=f"{agent_type}_{tool_name}",
        output_type="tool_call",
        text=json.dumps(tool_record, indent=2)
    )


def store_agent_report(market_analysis_id: int, agent_type: str, report_content: str):
    """
    Store agent report in the database
    
    Args:
        market_analysis_id: ID of the associated MarketAnalysis
        agent_type: Type of agent (e.g., "fundamental_analyst", "sentiment_analyst")
        report_content: Full report content
        
    Returns:
        AnalysisOutput ID if successful, None if failed
    """
    return store_analysis_output(
        market_analysis_id=market_analysis_id,
        name=f"{agent_type}_report",
        output_type="agent_report",
        text=report_content
    )


def create_logging_agent_wrapper(original_agent_func, agent_name: str, market_analysis_id: int = None):
    """
    Wrap an agent function to log its reports to the database.
    
    Args:
        original_agent_func: Original agent function
        agent_name: Name of the agent (e.g., "news_analyst", "market_analyst")
        market_analysis_id: MarketAnalysis ID to store outputs
        
    Returns:
        Wrapped agent function
    """
    def wrapped_agent(state):
        # Execute the original agent
        result = original_agent_func(state)
        
        # Log the agent's report if it exists and we have a market analysis ID
        if market_analysis_id:
            report_key = f"{agent_name.replace('_analyst', '')}_report"
            if report_key in result and result[report_key]:
                store_agent_report(
                    market_analysis_id=market_analysis_id,
                    agent_type=agent_name,
                    report_content=result[report_key]
                )
                logger.info(f"[AGENT_REPORT] Stored {agent_name} report to database")
        
        return result
    
    return wrapped_agent


def get_market_analysis_outputs(analysis_id: int):
    """
    Retrieve all outputs for a MarketAnalysis
    
    Args:
        analysis_id: MarketAnalysis ID
        
    Returns:
        List of AnalysisOutput objects
    """
    try:
        from ba2_trade_platform.core.db import Session, engine
        from ba2_trade_platform.core.models import AnalysisOutput
        from sqlmodel import select
        
        with Session(engine) as session:
            statement = select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == analysis_id)
            return session.exec(statement).all()
    except Exception as e:
        logger.error(f"Error retrieving outputs for MarketAnalysis {analysis_id}: {e}", exc_info=True)
        return []


class ToolCallFailureError(Exception):
    """Exception raised when tool calls fail repeatedly after max retries."""
    def __init__(self, tool_name: str, message: str, market_analysis_id: int = None):
        self.tool_name = tool_name
        self.market_analysis_id = market_analysis_id
        super().__init__(message)


class LoggingToolNode:
    """Custom ToolNode wrapper that logs tool calls, stores JSON data, and handles retries with reflection."""
    
    # Maximum consecutive failures before failing the analysis
    MAX_CONSECUTIVE_FAILURES = 3
    
    def __init__(self, tools, market_analysis_id=None, model_info: str = None):
        from langgraph.prebuilt import ToolNode
        from langchain_core.tools import tool
        
        self.market_analysis_id = market_analysis_id
        self.model_info = model_info  # Model info for logging (e.g., "NagaAI/grok-4" or "OpenAI/gpt-5")
        self.original_tools = {t.name: t for t in tools}
        
        # Track consecutive failures per tool for retry logic
        self.consecutive_failures: Dict[str, int] = {}
        
        # Wrap each tool to intercept results and store JSON
        wrapped_tools = []
        for original_tool in tools:
            wrapped = self._wrap_tool(original_tool)
            wrapped_tools.append(wrapped)
        
        # Create ToolNode with wrapped tools
        self.tool_node = ToolNode(wrapped_tools)
    
    def _format_message_header(self, content: str) -> str:
        """Format ssage with model information header."""
        if not self.model_info:
            return content
        return f"""================================== AI Message ({self.model_info}) ==================================

{content}

"""
    
    def _wrap_tool(self, original_tool):
        """Wrap a tool to intercept its result and store JSON before returning."""
        from langchain_core.tools import StructuredTool
        
        tool_name = original_tool.name
        market_analysis_id = self.market_analysis_id
        model_info = self.model_info
        
        # Create wrapped function
        def wrapped_func(**kwargs):
            """Wrapper that stores JSON and returns text for agent."""
            # Log tool call with model information if available
            if model_info:
                logger.info(f"[TOOL_CALL] Executing {tool_name} with args: {kwargs} | WebSearchModel: {model_info}")
            else:
                logger.info(f"[TOOL_CALL] Executing {tool_name} with args: {kwargs}")
            
            if market_analysis_id:
                store_analysis_output(
                    market_analysis_id=market_analysis_id,
                    name=f"tool_call_{tool_name}",
                    output_type="tool_call_input",
                    text=f"Tool: {tool_name}\nArguments: {kwargs}\nTimestamp: {datetime.now(timezone.utc).isoformat()}"
                )
            
            # Call original tool
            result = original_tool.invoke(kwargs)
            
            # Check if result is dict with internal format
            if isinstance(result, dict) and result.get('_internal'):
                text_for_agent = result.get('text_for_agent', '')
                json_for_storage = result.get('json_for_storage')
                is_error = result.get('_error', False)
                
                # Handle error
                if is_error:
                    logger.error(f"[TOOL_ERROR] {tool_name} returned critical error")
                    if market_analysis_id:
                        update_market_analysis_status(
                            market_analysis_id,
                            "FAILED",
                            {"error": text_for_agent, "failed_tool": tool_name, "timestamp": datetime.now(timezone.utc).isoformat()}
                        )
                else:
                    logger.info(f"[TOOL_RESULT] {tool_name} returned: {text_for_agent[:500]}...")
                
                # Store outputs with model header
                if market_analysis_id:
                    # Format message with model header
                    formatted_output = f"Tool: {tool_name}\nOutput:\n\n{self._format_message_header(text_for_agent)}\n\nTimestamp: {datetime.now(timezone.utc).isoformat()}"
                    
                    # Store text format
                    store_analysis_output(
                        market_analysis_id=market_analysis_id,
                        name=f"tool_output_{tool_name}",
                        output_type="tool_call_output_error" if is_error else "tool_call_output",
                        text=formatted_output
                    )
                    
                    # Store JSON format if provided
                    if json_for_storage:
                        import json
                        store_analysis_output(
                            market_analysis_id=market_analysis_id,
                            name=f"tool_output_{tool_name}_json",
                            output_type="tool_call_output_json",
                            text=json.dumps(json_for_storage, indent=2)
                        )
                        logger.info(f"[JSON_STORED] Saved JSON parameters for {tool_name}")
                
                # Return only text for agent (without model header)
                return text_for_agent
            else:
                # Simple text result
                logger.info(f"[TOOL_RESULT] {tool_name} returned: {str(result)[:500]}...")
                if market_analysis_id:
                    formatted_output = f"Tool: {tool_name}\nOutput:\n\n{self._format_message_header(str(result))}\n\nTimestamp: {datetime.now(timezone.utc).isoformat()}"
                    store_analysis_output(
                        market_analysis_id=market_analysis_id,
                        name=f"tool_output_{tool_name}",
                        output_type="tool_call_output",
                        text=formatted_output
                    )
                return result
        
        # Create new tool using StructuredTool.from_function
        wrapped_tool = StructuredTool.from_function(
            func=wrapped_func,
            name=original_tool.name,
            description=original_tool.description,
            args_schema=original_tool.args_schema if hasattr(original_tool, 'args_schema') else None
        )
        
        return wrapped_tool
    
    def _check_concatenated_tool_name(self, tool_name: str) -> None:
        """Check for concatenated tool names caused by buggy LLM providers and raise error.
        
        Some LLM providers (e.g., Grok/NagaAI) can return corrupted tool names like
        'get_ohlcv_dataget_indicator_dataget_indicator_data' when the model tries
        to call multiple tools. This is an API-level bug where tool names get concatenated
        instead of being returned as separate tool calls.
        
        Args:
            tool_name: The tool name to check
            
        Raises:
            ToolCallFailureError: If concatenated tool name is detected
        """
        # Valid tool names we recognize
        valid_tools = list(self.original_tools.keys())
        
        # Log all tool call names for debugging (helps catch the bug early)
        logger.debug(f"[TOOL_CHECK] Checking tool name: '{tool_name}' (len={len(tool_name)}) against valid tools: {valid_tools}")
        
        # If tool name is already valid, return
        if tool_name in valid_tools:
            return
        
        # Log when we detect an unknown tool name (this is where concatenation might be happening)
        logger.warning(f"[TOOL_CHECK] Unknown tool name detected: '{tool_name}' (len={len(tool_name)})")
        
        # Check if the tool name is a concatenation of ANY valid tools (not just the same one)
        # Example: "get_ohlcv_dataget_indicator_dataget_indicator_data" contains multiple valid tool names
        for valid_tool in valid_tools:
            # Check if the corrupted name starts with a valid tool and is longer
            if tool_name.startswith(valid_tool) and len(tool_name) > len(valid_tool):
                remainder = tool_name[len(valid_tool):]
                # Check if ANY valid tool name appears in the remainder (not just the same one)
                for other_tool in valid_tools:
                    if remainder.startswith(other_tool) or other_tool in remainder:
                        model_str = f" (Model: {self.model_info})" if self.model_info else ""
                        error_msg = (
                            f"CRITICAL: Detected concatenated/corrupted tool name from LLM provider{model_str}: '{tool_name}'. "
                            f"This is a known BUG in the NagaAI/Grok API where multiple tool calls get concatenated "
                            f"into a single string instead of being returned as separate tool calls. "
                            f"Workaround: Try using a different LLM provider (OpenAI, Anthropic) or wait for NagaAI to fix this bug."
                        )
                        logger.error(error_msg)
                        
                        # Mark analysis as failed
                        if self.market_analysis_id:
                            update_market_analysis_status(
                                self.market_analysis_id,
                                "FAILED",
                                {
                                    "error": "NagaAI API bug: Corrupted tool name (tool names concatenated)",
                                    "corrupted_tool_name": tool_name,
                                    "suggestion": "Use a different LLM provider (OpenAI, Anthropic) or wait for NagaAI bug fix",
                                    "timestamp": datetime.now(timezone.utc).isoformat()
                                }
                            )
                        
                        raise ToolCallFailureError(
                            tool_name=tool_name,
                            message=error_msg,
                            market_analysis_id=self.market_analysis_id
                        )
    
    def __call__(self, state):
        """Execute tools via ToolNode with call_id truncation for OpenAI compatibility.
        
        OpenAI enforces a 64-character limit on tool call IDs, but LangGraph can generate
        longer IDs when parallel tool calls are enabled. This method truncates call_ids
        in ALL AIMessages in the conversation history before passing to ToolNode.
        
        Also fixes concatenated tool names caused by buggy LLM providers.
        """
        import hashlib
        import copy
        from langchain_core.messages import AIMessage
        
        # Check if we need to truncate call_ids in ANY message
        if "messages" not in state or not state["messages"]:
            return self.tool_node.invoke(state)
        
        messages_modified = False
        new_messages = []
        
        # Process all messages in history to truncate any long call_ids and fix concatenated tool names
        for message in state["messages"]:
            # Only process AIMessage with tool_calls
            if isinstance(message, AIMessage) and hasattr(message, "tool_calls") and message.tool_calls:
                needs_modification = False
                modified_calls = []
                
                for tool_call in message.tool_calls:
                    original_id = tool_call.get("id", "")
                    original_name = tool_call.get("name", "")
                    modified_call = copy.deepcopy(tool_call)
                    
                    # Check for concatenated tool names (e.g., 'get_indicator_dataget_indicator_data...')
                    # This will raise ToolCallFailureError if detected
                    self._check_concatenated_tool_name(original_name)
                    
                    # Check if call_id exceeds OpenAI's 64-char limit
                    if len(original_id) > 64:
                        needs_modification = True
                        messages_modified = True
                        # Create deterministic shortened ID using hash
                        hash_suffix = hashlib.sha256(original_id.encode()).hexdigest()[:24]
                        truncated_id = f"{original_id[:40]}_{hash_suffix}"[:64]
                        
                        logger.debug(f"Truncated call_id from {len(original_id)} to {len(truncated_id)} chars")
                        modified_call["id"] = truncated_id
                    
                    modified_calls.append(modified_call)
                
                # If any modifications were made in this message, create new AIMessage
                if needs_modification:
                    # Create new additional_kwargs without tool_calls (if present)
                    new_additional_kwargs = {}
                    if hasattr(message, "additional_kwargs") and message.additional_kwargs:
                        for key, value in message.additional_kwargs.items():
                            if key != "tool_calls":
                                new_additional_kwargs[key] = value
                    
                    # Create new AIMessage with modified tool calls
                    new_message = AIMessage(
                        content=message.content,
                        tool_calls=modified_calls,
                        id=message.id if hasattr(message, "id") else None,
                        additional_kwargs=new_additional_kwargs
                    )
                    new_messages.append(new_message)
                else:
                    new_messages.append(message)
            else:
                # Non-AIMessage or no tool_calls - keep as is
                new_messages.append(message)
        
        # If any modifications were made, use new state
        if messages_modified:
            new_state = state.copy()
            new_state["messages"] = new_messages
            
            logger.info(f"Modified tool call IDs in conversation history to comply with OpenAI 64-char limit")
            
            # Use modified state
            result = self.tool_node.invoke(new_state)
        else:
            # No modifications needed, use original state
            result = self.tool_node.invoke(state)
        
        # Check result for tool errors and track consecutive failures
        result = self._check_and_track_failures(result, state)
        
        return result
    
    def _check_and_track_failures(self, result: Dict[str, Any], original_state: Dict[str, Any]) -> Dict[str, Any]:
        """Check tool results for errors and track consecutive failures.
        
        If a tool fails MAX_CONSECUTIVE_FAILURES times in a row, raises ToolCallFailureError
        to fail the analysis.
        
        Args:
            result: The result from ToolNode.invoke()
            original_state: The original state passed to __call__
            
        Returns:
            The result, potentially with enhanced error messages
            
        Raises:
            ToolCallFailureError: If max consecutive failures reached
        """
        from langchain_core.messages import ToolMessage
        
        if "messages" not in result:
            return result
        
        enhanced_messages = []
        
        for message in result["messages"]:
            if isinstance(message, ToolMessage):
                tool_name = message.name if hasattr(message, 'name') else 'unknown'
                content = message.content if hasattr(message, 'content') else ''
                
                # Check if this is an error message (Field required, validation errors, etc.)
                is_error = (
                    'Field required' in str(content) or
                    'Error invoking tool' in str(content) or
                    'validation error' in str(content).lower()
                )
                
                if is_error:
                    # Increment failure counter
                    self.consecutive_failures[tool_name] = self.consecutive_failures.get(tool_name, 0) + 1
                    failure_count = self.consecutive_failures[tool_name]
                    
                    logger.warning(f"Tool '{tool_name}' failed (attempt {failure_count}/{self.MAX_CONSECUTIVE_FAILURES}): {content[:200]}")
                    
                    # Check if we've exceeded max retries
                    if failure_count >= self.MAX_CONSECUTIVE_FAILURES:
                        error_msg = (
                            f"CRITICAL: Tool '{tool_name}' has failed {failure_count} consecutive times. "
                            f"Analysis cannot proceed. Last error: {content}"
                        )
                        logger.error(error_msg)
                        
                        # Mark analysis as failed in database
                        if self.market_analysis_id:
                            update_market_analysis_status(
                                self.market_analysis_id,
                                "FAILED",
                                {
                                    "error": f"Tool {tool_name} failed after {failure_count} retries",
                                    "last_error": str(content)[:500],
                                    "failed_tool": tool_name,
                                    "timestamp": datetime.now(timezone.utc).isoformat()
                                }
                            )
                        
                        # Raise exception to stop the graph
                        raise ToolCallFailureError(
                            tool_name=tool_name,
                            message=error_msg,
                            market_analysis_id=self.market_analysis_id
                        )
                    
                    # Add reflection instructions to help the model correct itself
                    reflection_hint = self._generate_reflection_hint(tool_name, content, failure_count)
                    enhanced_content = f"{content}\n\n{reflection_hint}"
                    
                    # Create new ToolMessage with enhanced content
                    enhanced_message = ToolMessage(
                        content=enhanced_content,
                        tool_call_id=message.tool_call_id if hasattr(message, 'tool_call_id') else '',
                        name=tool_name
                    )
                    enhanced_messages.append(enhanced_message)
                else:
                    # Success - reset failure counter for this tool
                    if tool_name in self.consecutive_failures:
                        logger.info(f"Tool '{tool_name}' succeeded after {self.consecutive_failures[tool_name]} previous failures")
                        self.consecutive_failures[tool_name] = 0
                    enhanced_messages.append(message)
            else:
                enhanced_messages.append(message)
        
        # Return result with enhanced messages
        result["messages"] = enhanced_messages
        return result
    
    def _generate_reflection_hint(self, tool_name: str, error_content: str, failure_count: int) -> str:
        """Generate a reflection hint to help the model correct its tool usage.
        
        Args:
            tool_name: Name of the tool that failed
            error_content: The error message content
            failure_count: Number of consecutive failures
            
        Returns:
            Reflection hint string
        """
        remaining_attempts = self.MAX_CONSECUTIVE_FAILURES - failure_count
        
        # Parse the error to provide specific guidance
        hints = []
        
        if 'Field required' in error_content:
            # Extract which field is required
            import re
            field_match = re.search(r"(\w+):\s*Field required", error_content)
            if field_match:
                field_name = field_match.group(1)
                hints.append(f"The '{field_name}' parameter is REQUIRED but was not provided.")
                
                # Provide specific guidance based on the field
                if field_name == 'indicator':
                    hints.append("You MUST specify which indicator to calculate. Valid indicators include: rsi, macd, macds, macdh, boll, boll_ub, boll_lb, atr, close_50_sma, close_200_sma, close_10_ema, vwma")
                    hints.append("Example: get_indicator_data(indicator='rsi')")
                elif field_name == 'frequency':
                    hints.append("You MUST specify the frequency. Valid values are: 'annual' or 'quarterly'")
                    hints.append("Example: get_balance_sheet(frequency='quarterly', end_date='2025-12-10')")
                elif field_name == 'end_date':
                    hints.append("You MUST specify the end_date in YYYY-MM-DD format.")
                    hints.append("Example: get_company_news(end_date='2025-12-10')")
        
        if 'kwargs {}' in error_content or 'kwargs: {}' in error_content:
            hints.append("You called the tool with NO arguments at all. Tools require specific parameters.")
        
        # Add urgency based on remaining attempts
        if remaining_attempts == 1:
            urgency = "⚠️ FINAL ATTEMPT: If you fail again, the analysis will be marked as FAILED."
        else:
            urgency = f"⚠️ WARNING: You have {remaining_attempts} attempts remaining before the analysis fails."
        
        reflection = f"""
=== TOOL USAGE CORRECTION REQUIRED ===
{urgency}

PROBLEM: Your tool call to '{tool_name}' failed because required parameters were missing.

{"".join(f"• {hint}" + chr(10) for hint in hints)}
INSTRUCTIONS:
1. Review the tool's required parameters carefully
2. The symbol parameter is OPTIONAL (defaults to the company being analyzed)
3. Other parameters like 'indicator', 'end_date', 'frequency' ARE REQUIRED
4. Call the tool again with ALL required parameters

DO NOT repeat the same mistake. Provide the required parameters in your next tool call.
=== END CORRECTION ===
"""
        return reflection


class DatabaseStorageMixin:
    """
    Mixin class to add database storage capabilities to TradingAgents
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.market_analysis_id = None
    
    def log_tool_call(self, tool_name: str, inputs: Dict[str, Any], output: str, agent_type: str = "unknown"):
        """Log a tool call to the database"""
        if self.market_analysis_id:
            store_tool_call_output(
                market_analysis_id=self.market_analysis_id,
                tool_name=tool_name,
                inputs=inputs,
                output=str(output),
                agent_type=agent_type
            )
        else:
            # Log to console if no market analysis available
            logger.info(f"[{agent_type}] Tool call: {tool_name}({inputs}) -> {output}")
    
    def log_agent_report(self, agent_type: str, report_content: str):
        """Log an agent report to the database"""
        if self.market_analysis_id:
            store_agent_report(
                market_analysis_id=self.market_analysis_id,
                agent_type=agent_type,
                report_content=report_content
            )
        else:
            # Log to console if no market analysis available
            logger.info(f"[{agent_type}] Report: {report_content}")
    
    def store_analysis_output(self, market_analysis_id: int, name: str, output_type: str, text: str = None, blob: bytes = None):
        """
        Store an analysis output in the database
        
        Args:
            market_analysis_id: ID of the associated MarketAnalysis
            name: Name/identifier for this output
            output_type: Type of output (e.g., "tool_call", "agent_report", "news_data")
            text: Text content
            blob: Binary content
            
        Returns:
            AnalysisOutput ID if successful, None if failed
        """
        return store_analysis_output(market_analysis_id, name, output_type, text, blob)
    
    def update_analysis_status(self, status: str, state: Dict[str, Any] = None):
        """Update the analysis status"""
        if self.market_analysis_id:
            update_market_analysis_status(self.market_analysis_id, status, state)
        else:
            # Log to console if no market analysis available
            logger.info(f"Analysis status: {status}" + (f" - {state}" if state else ""))
    
    def finalize_analysis(self, final_status: Union[str, 'MarketAnalysisStatus'] = None):
        """Mark the analysis as completed"""
        if final_status is None:
            from ba2_trade_platform.core.types import MarketAnalysisStatus
            final_status = MarketAnalysisStatus.COMPLETED
            
        if self.market_analysis_id:
            self.update_analysis_status(final_status)
        else:
            logger.info(f"Analysis completed with status: {final_status}")
