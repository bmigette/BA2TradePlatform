"""
Database utilities for storing TradingAgents analysis outputs
"""
from typing import Optional, Dict, Any
from datetime import datetime, timezone


def create_market_analysis(symbol: str, source_expert_instance_id: int, status: str = "started", state: Dict[str, Any] = None) -> Optional[int]:
    """
    Create a new MarketAnalysis record
    
    Args:
        symbol: Stock symbol being analyzed
        source_expert_instance_id: ID of the expert instance running the analysis
        status: Current status of the analysis
        state: Analysis state data
        
    Returns:
        MarketAnalysis ID if successful, None if failed
    """
    try:
        from ba2_trade_platform.core.db import add_instance
        from ba2_trade_platform.core.models import MarketAnalysis
        
        analysis = MarketAnalysis(
            symbol=symbol,
            source_expert_instance_id=source_expert_instance_id,
            status=status,
            state=state or {}
        )
        
        return add_instance(analysis)
    except Exception as e:
        from . import logger as ta_logger
        ta_logger.error(f"Error creating MarketAnalysis: {e}")
        return None


def update_market_analysis_status(analysis_id: int, status: str, state: Dict[str, Any] = None):
    """
    Update the status and state of a MarketAnalysis record
    
    Args:
        analysis_id: MarketAnalysis ID
        status: New status
        state: Updated state data
    """
    try:
        from ba2_trade_platform.core.db import get_instance, update_instance
        from ba2_trade_platform.core.models import MarketAnalysis
        
        analysis = get_instance(MarketAnalysis, analysis_id)
        if analysis:
            analysis.status = status
            if state:
                analysis.state.update(state)
            update_instance(analysis)
    except Exception as e:
        from . import logger as ta_logger
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
        from . import logger as ta_logger
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
        from . import logger as ta_logger
        ta_logger.error(f"Error retrieving outputs for MarketAnalysis {analysis_id}: {e}")
        return []


class DatabaseStorageMixin:
    """
    Mixin class to add database storage capabilities to TradingAgents
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.market_analysis_id = None
        self.expert_instance_id = None
    
    def initialize_market_analysis(self, symbol: str, expert_instance_id: int):
        """Initialize a new MarketAnalysis for this run"""
        self.expert_instance_id = expert_instance_id
        self.market_analysis_id = create_market_analysis(
            symbol=symbol,
            source_expert_instance_id=expert_instance_id,
            status="running"
        )
        return self.market_analysis_id
    
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
    
    def log_agent_report(self, agent_type: str, report_content: str):
        """Log an agent report to the database"""
        if self.market_analysis_id:
            store_agent_report(
                market_analysis_id=self.market_analysis_id,
                agent_type=agent_type,
                report_content=report_content
            )
    
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
    
    def finalize_analysis(self, final_status: str = "completed"):
        """Mark the analysis as completed"""
        self.update_analysis_status(final_status)