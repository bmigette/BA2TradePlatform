"""
LLM Usage Tracker - Centralized token usage tracking for all LLM calls.

This module provides utilities to track token usage across:
- LangChain LLM calls (via callbacks)
- Direct API calls from data providers
- Smart Risk Manager
- Dynamic Instrument Selection
- Any other model usage

Usage:
    # For LangChain - automatic via ModelFactory
    llm = ModelFactory.create_llm("openai/gpt4o")  # Tracking happens automatically
    
    # For non-LangChain - manual tracking
    from ba2_trade_platform.core.LLMUsageTracker import track_llm_usage
    
    with track_llm_usage(
        model_selection="openai/gpt4o",
        use_case="Data Provider - News",
        expert_instance_id=11,
        symbol="AAPL"
    ) as tracker:
        # Make your API call
        response = some_api_call()
        
        # Report usage
        tracker.record(input_tokens=100, output_tokens=50)
"""

from typing import Optional, Any, Dict
from datetime import datetime, timezone
from contextlib import contextmanager
import time

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from ..logger import logger


class LLMUsageContext:
    """Context manager for tracking LLM usage in non-LangChain code."""
    
    def __init__(
        self,
        model_selection: str,
        use_case: str,
        expert_instance_id: Optional[int] = None,
        account_id: Optional[int] = None,
        symbol: Optional[str] = None,
        market_analysis_id: Optional[int] = None,
        smart_risk_manager_job_id: Optional[int] = None
    ):
        self.model_selection = model_selection
        self.use_case = use_case
        self.expert_instance_id = expert_instance_id
        self.account_id = account_id
        self.symbol = symbol
        self.market_analysis_id = market_analysis_id
        self.smart_risk_manager_job_id = smart_risk_manager_job_id
        
        self.start_time = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.error = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = int((time.time() - self.start_time) * 1000) if self.start_time else None
        
        if exc_type:
            self.error = str(exc_val)
        
        # Save to database
        self._save_usage(duration_ms)
        
        return False  # Don't suppress exceptions
    
    def record(self, input_tokens: int = 0, output_tokens: int = 0):
        """Record token usage. Call this after getting response from API."""
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
    
    def _save_usage(self, duration_ms: Optional[int]):
        """Save usage data to database."""
        try:
            from .models import LLMUsageLog
            from .db import add_instance
            from .models_registry import parse_model_selection
            
            # Parse model selection to get provider and model name
            try:
                provider, friendly_name = parse_model_selection(self.model_selection)
                
                # Get provider model name
                from .ModelFactory import ModelFactory
                model_info = ModelFactory.get_model_info(self.model_selection)
                provider_model_name = model_info.get('provider_model_name', friendly_name)
            except Exception as e:
                logger.warning(f"Could not parse model selection '{self.model_selection}': {e}")
                provider = "unknown"
                provider_model_name = self.model_selection
            
            # Calculate total tokens
            total_tokens = self.input_tokens + self.output_tokens
            
            # Estimate cost (TODO: Add pricing table)
            estimated_cost = self._estimate_cost(
                provider,
                provider_model_name,
                self.input_tokens,
                self.output_tokens
            )
            
            # Create usage log
            usage_log = LLMUsageLog(
                expert_instance_id=self.expert_instance_id,
                account_id=self.account_id,
                use_case=self.use_case,
                model_selection=self.model_selection,
                provider=provider,
                provider_model_name=provider_model_name,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=estimated_cost,
                duration_ms=duration_ms,
                symbol=self.symbol,
                market_analysis_id=self.market_analysis_id,
                smart_risk_manager_job_id=self.smart_risk_manager_job_id,
                error=self.error
            )
            
            add_instance(usage_log)
            
            logger.debug(
                f"Logged LLM usage: {self.use_case} | {self.model_selection} | "
                f"{total_tokens} tokens (in:{self.input_tokens}, out:{self.output_tokens})"
            )
            
        except Exception as e:
            logger.error(f"Failed to log LLM usage: {e}", exc_info=True)
    
    def _estimate_cost(
        self,
        provider: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int
    ) -> Optional[float]:
        """Estimate cost based on current pricing. Returns cost in USD."""
        # TODO: Implement pricing table
        # For now, return None - pricing can be added later
        return None


@contextmanager
def track_llm_usage(
    model_selection: str,
    use_case: str,
    expert_instance_id: Optional[int] = None,
    account_id: Optional[int] = None,
    symbol: Optional[str] = None,
    market_analysis_id: Optional[int] = None,
    smart_risk_manager_job_id: Optional[int] = None
):
    """
    Context manager for tracking LLM usage in non-LangChain code.
    
    Example:
        with track_llm_usage(
            model_selection="openai/gpt4o",
            use_case="Data Provider - News",
            expert_instance_id=11,
            symbol="AAPL"
        ) as tracker:
            response = api_call()
            tracker.record(input_tokens=100, output_tokens=50)
    """
    tracker = LLMUsageContext(
        model_selection=model_selection,
        use_case=use_case,
        expert_instance_id=expert_instance_id,
        account_id=account_id,
        symbol=symbol,
        market_analysis_id=market_analysis_id,
        smart_risk_manager_job_id=smart_risk_manager_job_id
    )
    
    with tracker:
        yield tracker


class LLMUsageCallback(BaseCallbackHandler):
    """
    LangChain callback handler for automatic token usage tracking.
    
    Automatically attached to all LLMs created via ModelFactory.
    """
    
    def __init__(
        self,
        model_selection: str,
        use_case: str = "LangChain LLM Call",
        expert_instance_id: Optional[int] = None,
        account_id: Optional[int] = None,
        symbol: Optional[str] = None,
        market_analysis_id: Optional[int] = None,
        smart_risk_manager_job_id: Optional[int] = None
    ):
        super().__init__()
        self.model_selection = model_selection
        self.use_case = use_case
        self.expert_instance_id = expert_instance_id
        self.account_id = account_id
        self.symbol = symbol
        self.market_analysis_id = market_analysis_id
        self.smart_risk_manager_job_id = smart_risk_manager_job_id
        
        self.start_time = None
    
    def on_llm_start(self, serialized: Dict[str, Any], prompts: list, **kwargs) -> None:
        """Called when LLM starts."""
        self.start_time = time.time()
    
    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        """Called when LLM ends - extract token usage."""
        try:
            duration_ms = int((time.time() - self.start_time) * 1000) if self.start_time else None
            
            # Extract token usage from response
            input_tokens = 0
            output_tokens = 0
            
            # Debug: Log response structure
            logger.debug(f"LLM Response structure for {self.model_selection}:")
            logger.debug(f"  response type: {type(response)}")
            logger.debug(f"  response.__dict__ keys: {list(response.__dict__.keys()) if hasattr(response, '__dict__') else 'N/A'}")
            
            # Check for run field with usage_metadata (newer LangChain)
            if hasattr(response, 'run') and response.run:
                logger.debug(f"  run type: {type(response.run)}")
                if hasattr(response.run, 'usage_metadata'):
                    logger.debug(f"  run.usage_metadata: {response.run.usage_metadata}")
            
            if hasattr(response, 'generations') and response.generations:
                logger.debug(f"  generations count: {len(response.generations)}")
                if response.generations and len(response.generations) > 0 and len(response.generations[0]) > 0:
                    gen = response.generations[0][0]
                    logger.debug(f"  first generation type: {type(gen)}")
                    if hasattr(gen, 'generation_info') and gen.generation_info:
                        logger.debug(f"  generation_info keys: {list(gen.generation_info.keys())}")
                    if hasattr(gen, 'message'):
                        msg = gen.message
                        if hasattr(msg, 'response_metadata') and msg.response_metadata:
                            logger.debug(f"  message.response_metadata keys: {list(msg.response_metadata.keys())}")
                        if hasattr(msg, 'usage_metadata'):
                            logger.debug(f"  message.usage_metadata: {msg.usage_metadata}")
            
            # Log llm_output structure
            logger.debug(f"  llm_output type: {type(response.llm_output)}")
            if response.llm_output:
                logger.debug(f"  llm_output keys: {list(response.llm_output.keys())}")
                logger.debug(f"  llm_output content: {response.llm_output}")
            
            # Method 1: Check llm_output for token_usage (OpenAI format - older LangChain)
            if response.llm_output and 'token_usage' in response.llm_output:
                token_usage = response.llm_output['token_usage']
                input_tokens = token_usage.get('prompt_tokens', 0)
                output_tokens = token_usage.get('completion_tokens', 0)
                logger.debug(f"  ✓ Found tokens via Method 1 (llm_output.token_usage)")
            
            # Method 2: Check llm_output for usage (alternative format)
            elif response.llm_output and 'usage' in response.llm_output:
                usage = response.llm_output['usage']
                input_tokens = usage.get('prompt_tokens', 0) or usage.get('input_tokens', 0)
                output_tokens = usage.get('completion_tokens', 0) or usage.get('output_tokens', 0)
                logger.debug(f"  ✓ Found tokens via Method 2 (llm_output.usage)")
            
            # Method 3: Check message.usage_metadata (newer LangChain with langchain-openai 0.2+)
            elif hasattr(response, 'generations') and response.generations:
                for generation_list in response.generations:
                    for generation in generation_list:
                        if hasattr(generation, 'message') and hasattr(generation.message, 'usage_metadata'):
                            usage_metadata = generation.message.usage_metadata
                            if usage_metadata:
                                input_tokens += usage_metadata.get('input_tokens', 0)
                                output_tokens += usage_metadata.get('output_tokens', 0)
                                logger.debug(f"  ✓ Found tokens via Method 3 (message.usage_metadata)")
                                break
                    if input_tokens > 0 or output_tokens > 0:
                        break
            
            # Method 4: Check generation_info in generations (fallback for older formats)
            if input_tokens == 0 and output_tokens == 0 and hasattr(response, 'generations') and response.generations:
                for generation_list in response.generations:
                    for generation in generation_list:
                        if hasattr(generation, 'generation_info') and generation.generation_info:
                            # Try 'usage' key
                            usage = generation.generation_info.get('usage', {})
                            if usage:
                                input_tokens += usage.get('prompt_tokens', 0) or usage.get('input_tokens', 0)
                                output_tokens += usage.get('completion_tokens', 0) or usage.get('output_tokens', 0)
                                logger.debug(f"  ✓ Found tokens via Method 4 (generation_info.usage)")
                            
                            # Try 'token_usage' key
                            token_usage = generation.generation_info.get('token_usage', {})
                            if token_usage:
                                input_tokens += token_usage.get('prompt_tokens', 0) or token_usage.get('input_tokens', 0)
                                output_tokens += token_usage.get('completion_tokens', 0) or token_usage.get('output_tokens', 0)
                                logger.debug(f"  ✓ Found tokens via Method 4 (generation_info.token_usage)")
            
            # If still no tokens found, log warning for debugging
            if input_tokens == 0 and output_tokens == 0:
                logger.warning(
                    f"No token usage found in LLM response for {self.model_selection}. "
                    f"llm_output keys: {list(response.llm_output.keys()) if response.llm_output else 'None'}"
                )
            
            # Log usage
            self._log_usage(input_tokens, output_tokens, duration_ms, error=None)
            
        except Exception as e:
            logger.error(f"Error in LLMUsageCallback.on_llm_end: {e}", exc_info=True)
    
    def on_llm_error(self, error: Exception, **kwargs) -> None:
        """Called when LLM errors."""
        try:
            duration_ms = int((time.time() - self.start_time) * 1000) if self.start_time else None
            self._log_usage(0, 0, duration_ms, error=str(error))
        except Exception as e:
            logger.error(f"Error in LLMUsageCallback.on_llm_error: {e}", exc_info=True)
    
    def _log_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        duration_ms: Optional[int],
        error: Optional[str]
    ):
        """Save usage data to database."""
        try:
            from .models import LLMUsageLog
            from .db import add_instance
            from .models_registry import parse_model_selection
            
            # Parse model selection
            try:
                provider, friendly_name = parse_model_selection(self.model_selection)
                
                # Get provider model name
                from .ModelFactory import ModelFactory
                model_info = ModelFactory.get_model_info(self.model_selection)
                provider_model_name = model_info.get('provider_model_name', friendly_name)
            except Exception as e:
                logger.warning(f"Could not parse model selection '{self.model_selection}': {e}")
                provider = "unknown"
                provider_model_name = self.model_selection
            
            total_tokens = input_tokens + output_tokens
            
            # Create usage log
            usage_log = LLMUsageLog(
                expert_instance_id=self.expert_instance_id,
                account_id=self.account_id,
                use_case=self.use_case,
                model_selection=self.model_selection,
                provider=provider,
                provider_model_name=provider_model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=None,  # TODO: Add pricing
                duration_ms=duration_ms,
                symbol=self.symbol,
                market_analysis_id=self.market_analysis_id,
                smart_risk_manager_job_id=self.smart_risk_manager_job_id,
                error=error
            )
            
            add_instance(usage_log)
            
            if total_tokens > 0:
                logger.debug(
                    f"Logged LangChain LLM usage: {self.use_case} | {self.model_selection} | "
                    f"{total_tokens} tokens (in:{input_tokens}, out:{output_tokens})"
                )
            
        except Exception as e:
            logger.error(f"Failed to log LangChain LLM usage: {e}", exc_info=True)


def create_usage_callback(
    model_selection: str,
    use_case: str = "LangChain LLM Call",
    expert_instance_id: Optional[int] = None,
    account_id: Optional[int] = None,
    symbol: Optional[str] = None,
    market_analysis_id: Optional[int] = None,
    smart_risk_manager_job_id: Optional[int] = None
) -> LLMUsageCallback:
    """
    Create a usage tracking callback for LangChain LLMs.
    
    This is typically called automatically by ModelFactory.
    """
    return LLMUsageCallback(
        model_selection=model_selection,
        use_case=use_case,
        expert_instance_id=expert_instance_id,
        account_id=account_id,
        symbol=symbol,
        market_analysis_id=market_analysis_id,
        smart_risk_manager_job_id=smart_risk_manager_job_id
    )
