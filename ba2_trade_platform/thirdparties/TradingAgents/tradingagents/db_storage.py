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


class LoggingToolNode:
    """Custom ToolNode wrapper that logs tool calls and stores JSON data."""
    
    def __init__(self, tools, market_analysis_id=None):
        from langgraph.prebuilt import ToolNode
        from langchain_core.tools import tool
        
        self.market_analysis_id = market_analysis_id
        self.original_tools = {t.name: t for t in tools}
        
        # Wrap each tool to intercept results and store JSON
        wrapped_tools = []
        for original_tool in tools:
            wrapped = self._wrap_tool(original_tool)
            wrapped_tools.append(wrapped)
        
        # Create ToolNode with wrapped tools
        self.tool_node = ToolNode(wrapped_tools)
    
    def _wrap_tool(self, original_tool):
        """Wrap a tool to intercept its result and store JSON before returning."""
        from langchain_core.tools import StructuredTool
        
        tool_name = original_tool.name
        market_analysis_id = self.market_analysis_id
        
        # Create wrapped function
        def wrapped_func(**kwargs):
            """Wrapper that stores JSON and returns text for agent."""
            # Log tool call
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
                
                # Store outputs
                if market_analysis_id:
                    # Store text format
                    store_analysis_output(
                        market_analysis_id=market_analysis_id,
                        name=f"tool_output_{tool_name}",
                        output_type="tool_call_output_error" if is_error else "tool_call_output",
                        text=f"Tool: {tool_name}\nOutput: {text_for_agent}\nTimestamp: {datetime.now(timezone.utc).isoformat()}"
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
                
                # Return only text for agent
                return text_for_agent
            else:
                # Simple text result
                logger.info(f"[TOOL_RESULT] {tool_name} returned: {str(result)[:500]}...")
                if market_analysis_id:
                    store_analysis_output(
                        market_analysis_id=market_analysis_id,
                        name=f"tool_output_{tool_name}",
                        output_type="tool_call_output",
                        text=f"Tool: {tool_name}\nOutput: {result}\nTimestamp: {datetime.now(timezone.utc).isoformat()}"
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
    
    def __call__(self, state):
        """Execute tools via ToolNode."""
        return self.tool_node.invoke(state)


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
