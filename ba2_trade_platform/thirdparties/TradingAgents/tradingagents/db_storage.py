"""
Database utilities for storing TradingAgents analysis outputs
"""
from typing import Optional, Dict, Any, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from ba2_trade_platform.core.types import MarketAnalysisStatus
from datetime import datetime, timezone
from . import logger as ta_logger





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
                        ta_logger.warning(f"Unknown status '{status}', using as-is")
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
                
                # Update the trading_agent_graph section with new state
                analysis.state['trading_agent_graph'].update(state)
                
                # Explicitly mark the state field as modified for SQLAlchemy
                from sqlalchemy.orm import attributes
                attributes.flag_modified(analysis, "state")
                
            update_instance(analysis)
            ta_logger.debug(f"Updated MarketAnalysis {analysis_id} status to {status}")
    except Exception as e:
        ta_logger.error(f"Error updating MarketAnalysis {analysis_id}: {e}")


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
        ta_logger.error(f"Error storing AnalysisOutput: {e}")
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
                ta_logger.info(f"[AGENT_REPORT] Stored {agent_name} report to database")
        
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
        ta_logger.error(f"Error retrieving outputs for MarketAnalysis {analysis_id}: {e}")
        return []


class LoggingToolNode:
    """Custom ToolNode wrapper that logs tool calls to database."""
    
    def __init__(self, tools, market_analysis_id=None):
        from langgraph.prebuilt import ToolNode
        self.tool_node = ToolNode(tools)
        self.market_analysis_id = market_analysis_id
        self.tools = {tool.name: tool for tool in tools}
    
    def __call__(self, state):
        """Execute tools and log the calls."""
        # Get tool calls from the last message
        messages = state.get("messages", [])
        if not messages:
            return self.tool_node.invoke(state)
        
        last_message = messages[-1]
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            # Log each tool call before execution
            for tool_call in last_message.tool_calls:
                tool_name = tool_call.get('name', 'unknown_tool')
                tool_args = tool_call.get('args', {})
                
                ta_logger.info(f"[TOOL_CALL] Executing {tool_name} with args: {tool_args}")
                
                # Store tool call information immediately
                if self.market_analysis_id:
                    store_analysis_output(
                        market_analysis_id=self.market_analysis_id,
                        name=f"tool_call_{tool_name}",
                        output_type="tool_call_input",
                        text=f"Tool: {tool_name}\nArguments: {tool_args}\nTimestamp: {datetime.now(timezone.utc).isoformat()}"
                    )
        
        # Execute the actual tool node using invoke method
        result = self.tool_node.invoke(state)
        
        # Log tool outputs
        if self.market_analysis_id and hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            result_messages = result.get("messages", [])
            tool_messages = [msg for msg in result_messages if hasattr(msg, 'content') and hasattr(msg, 'tool_call_id')]
            
            for tool_msg in tool_messages:
                # Find the corresponding tool call
                tool_call_id = getattr(tool_msg, 'tool_call_id', None)
                matching_call = None
                for tool_call in last_message.tool_calls:
                    if tool_call.get('id') == tool_call_id:
                        matching_call = tool_call
                        break
                
                tool_name = matching_call.get('name', 'unknown_tool') if matching_call else 'unknown_tool'
                output_content = tool_msg.content if hasattr(tool_msg, 'content') else str(tool_msg)
                
                ta_logger.info(f"[TOOL_RESULT] {tool_name} returned: {output_content[:2000]}...")
                
                # Store tool output
                store_analysis_output(
                    market_analysis_id=self.market_analysis_id,
                    name=f"tool_output_{tool_name}",
                    output_type="tool_call_output",
                    text=f"Tool: {tool_name}\nOutput: {output_content}\nTimestamp: {datetime.now(timezone.utc).isoformat()}"
                )
        
        return result


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
            ta_logger.info(f"[{agent_type}] Tool call: {tool_name}({inputs}) -> {output}")
    
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
            ta_logger.info(f"[{agent_type}] Report: {report_content}")
    
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
            ta_logger.info(f"Analysis status: {status}" + (f" - {state}" if state else ""))
    
    def finalize_analysis(self, final_status: Union[str, 'MarketAnalysisStatus'] = None):
        """Mark the analysis as completed"""
        if final_status is None:
            from ba2_trade_platform.core.types import MarketAnalysisStatus
            final_status = MarketAnalysisStatus.COMPLETED
            
        if self.market_analysis_id:
            self.update_analysis_status(final_status)
        else:
            ta_logger.info(f"Analysis completed with status: {final_status}")