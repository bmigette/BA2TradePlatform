"""
Model Factory - Helper class to create LLM instances for various providers.

This module provides a unified factory for creating LangChain-compatible LLM instances
based on the model registry. It handles provider-specific configuration, API keys,
and model parameters automatically.

Usage:
    from ba2_trade_platform.core.ModelFactory import ModelFactory
    
    # Create an LLM from a selection string
    llm = ModelFactory.create_llm("nagaai/gpt5", temperature=0.7)
    
    # Create with custom callbacks
    llm = ModelFactory.create_llm("native/gpt5", callbacks=[my_callback])
    
    # Get model information
    info = ModelFactory.get_model_info("openai/gpt4o")
"""

from typing import Any, Dict, List, Optional
import json
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.callbacks import BaseCallbackHandler

from .models_registry import (
    MODELS, PROVIDER_CONFIG, 
    get_model_for_provider, get_provider_config, parse_model_selection,
    get_model_default_kwargs,
    PROVIDER_OPENAI, PROVIDER_NAGAAI, PROVIDER_GOOGLE, PROVIDER_ANTHROPIC, PROVIDER_OPENROUTER,
    PROVIDER_XAI, PROVIDER_MOONSHOT, PROVIDER_DEEPSEEK
)
from ..config import get_app_setting, OPENAI_ENABLE_STREAMING, OPENAI_BACKEND_URL
from ..logger import logger


class ModelFactory:
    """
    Factory class for creating LLM instances from model selection strings.
    
    The factory handles:
    - Provider-specific LangChain classes (ChatOpenAI, ChatGoogleGenerativeAI, etc.)
    - API key retrieval from app settings
    - Base URL configuration
    - Model parameter injection (reasoning_effort, etc.)
    - Streaming configuration
    """
    
    # Cache for API keys to avoid repeated database lookups
    _api_key_cache: Dict[str, Optional[str]] = {}
    
    @classmethod
    def clear_api_key_cache(cls):
        """Clear the cached API keys. Useful when keys are updated."""
        cls._api_key_cache.clear()
        logger.debug("API key cache cleared")
    
    @classmethod
    def clear_cache(cls):
        """Alias for clear_api_key_cache for consistency with ModelBillingUsage."""
        cls.clear_api_key_cache()
    
    @classmethod
    def _get_api_key(cls, setting_key: str) -> Optional[str]:
        """
        Get API key from app settings with caching.
        
        Args:
            setting_key: The app setting key for the API key
            
        Returns:
            The API key or None if not found
        """
        if setting_key not in cls._api_key_cache:
            cls._api_key_cache[setting_key] = get_app_setting(setting_key)
        return cls._api_key_cache[setting_key]
    
    @classmethod
    def create_llm(
        cls,
        model_selection: str,
        temperature: float = 0.0,
        streaming: Optional[bool] = None,
        callbacks: Optional[List[BaseCallbackHandler]] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        # Token usage tracking parameters
        track_usage: bool = True,
        use_case: str = "LangChain LLM Call",
        expert_instance_id: Optional[int] = None,
        account_id: Optional[int] = None,
        symbol: Optional[str] = None,
        market_analysis_id: Optional[int] = None,
        smart_risk_manager_job_id: Optional[int] = None,
        **extra_kwargs
    ) -> BaseChatModel:
        """
        Create a LangChain chat model instance from a model selection string.
        
        Args:
            model_selection: Selection string in format "provider/friendly_name" 
                           (e.g., "nagaai/gpt5", "native/gpt4o", "openai/gpt5.1")
            temperature: Generation temperature (default 0.0 for deterministic)
            streaming: Enable streaming (default from config.OPENAI_ENABLE_STREAMING)
            callbacks: Optional list of LangChain callbacks
            model_kwargs: Optional additional model parameters (e.g., {"reasoning_effort": "low"})
            track_usage: Enable automatic token usage tracking (default True)
            use_case: Description of the use case for usage tracking
            expert_instance_id: Expert instance ID for usage tracking
            account_id: Account ID for usage tracking
            symbol: Trading symbol for usage tracking
            market_analysis_id: Market analysis ID for usage tracking
            smart_risk_manager_job_id: Smart risk manager job ID for usage tracking
            **extra_kwargs: Additional kwargs passed to the LLM constructor
            
        Returns:
            Configured BaseChatModel instance
            
        Raises:
            ValueError: If model or provider is not found, or API key is missing
            
        Example:
            >>> llm = ModelFactory.create_llm("nagaai/gpt5", temperature=0.7)
            >>> llm = ModelFactory.create_llm("native/gpt5.1", model_kwargs={"reasoning": {"effort": "low"}})
        """
        # Parse the selection string
        provider, friendly_name = parse_model_selection(model_selection)
        logger.debug(f"Creating LLM: provider={provider}, model={friendly_name}")
        
        # Get model info
        model_info = MODELS.get(friendly_name)
        if not model_info:
            raise ValueError(f"Unknown model: {friendly_name}. Check models_registry.py for available models.")
        
        # Resolve "native" provider to the model's actual native provider
        if provider == "native":
            provider = model_info.get("native_provider")
            if not provider:
                raise ValueError(f"Model {friendly_name} has no native provider defined")
            logger.debug(f"Resolved native provider to: {provider}")
        
        # Get the provider-specific model name
        provider_model_name = get_model_for_provider(friendly_name, provider)
        if not provider_model_name:
            available = list(model_info.get("provider_names", {}).keys())
            raise ValueError(
                f"Model {friendly_name} is not available for provider {provider}. "
                f"Available providers: {available}"
            )
        
        # Get provider configuration
        provider_config = get_provider_config(provider)
        if not provider_config:
            raise ValueError(f"Unknown provider: {provider}")
        
        # Get API key
        api_key_setting = provider_config.get("api_key_setting")
        api_key = cls._get_api_key(api_key_setting) if api_key_setting else None
        
        if not api_key:
            raise ValueError(
                f"API key not configured for provider {provider}. "
                f"Please set '{api_key_setting}' in app settings."
            )
        
        # Determine streaming setting
        if streaming is None:
            streaming = OPENAI_ENABLE_STREAMING
        
        # Add usage tracking callback if enabled
        if track_usage:
            from .LLMUsageTracker import create_usage_callback
            
            usage_callback = create_usage_callback(
                model_selection=model_selection,
                use_case=use_case,
                expert_instance_id=expert_instance_id,
                account_id=account_id,
                symbol=symbol,
                market_analysis_id=market_analysis_id,
                smart_risk_manager_job_id=smart_risk_manager_job_id
            )
            
            # Add to callbacks list
            if callbacks is None:
                callbacks = [usage_callback]
            else:
                callbacks = list(callbacks) + [usage_callback]
            
            logger.debug(f"Added usage tracking callback for {model_selection}")
        
        # Check if model supports temperature (O-series models don't)
        supports_temp = not model_info.get("no_temperature", False)
        # Some models require a fixed temperature (e.g., Kimi K2.5 requires 1.0)
        fixed_temp = model_info.get("fixed_temperature")
        if fixed_temp is not None:
            effective_temperature = fixed_temp
        elif supports_temp:
            effective_temperature = temperature
        else:
            effective_temperature = None
        
        # Merge default model_kwargs with user-provided ones (user takes precedence)
        default_kwargs = get_model_default_kwargs(friendly_name) or {}
        if default_kwargs:
            logger.debug(f"Model {friendly_name} has default model_kwargs: {default_kwargs}")
        merged_model_kwargs = {**default_kwargs, **(model_kwargs or {})}
        if merged_model_kwargs:
            logger.debug(f"Final merged model_kwargs: {merged_model_kwargs}")
        
        # Build LLM based on provider type
        langchain_class = provider_config.get("langchain_class", "ChatOpenAI")
        
        if langchain_class == "ChatOpenAI":
            return cls._create_openai_compatible(
                provider=provider,
                model_name=provider_model_name,
                base_url=provider_config.get("base_url"),
                api_key=api_key,
                temperature=effective_temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=merged_model_kwargs if merged_model_kwargs else None,
                **extra_kwargs
            )
        elif langchain_class == "ChatGoogleGenerativeAI":
            return cls._create_google(
                model_name=provider_model_name,
                api_key=api_key,
                temperature=effective_temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=merged_model_kwargs if merged_model_kwargs else None,
                **extra_kwargs
            )
        elif langchain_class == "ChatAnthropic":
            return cls._create_anthropic(
                model_name=provider_model_name,
                base_url=provider_config.get("base_url"),
                api_key=api_key,
                temperature=effective_temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=merged_model_kwargs if merged_model_kwargs else None,
                **extra_kwargs
            )
        elif langchain_class == "ChatXAI":
            return cls._create_xai(
                model_name=provider_model_name,
                api_key=api_key,
                temperature=effective_temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=merged_model_kwargs if merged_model_kwargs else None,
                **extra_kwargs
            )
        elif langchain_class == "ChatDeepSeek":
            return cls._create_deepseek(
                model_name=provider_model_name,
                api_key=api_key,
                temperature=effective_temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=merged_model_kwargs if merged_model_kwargs else None,
                **extra_kwargs
            )
        elif langchain_class == "MoonshotChat":
            return cls._create_moonshot(
                model_name=provider_model_name,
                api_key=api_key,
                temperature=effective_temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=merged_model_kwargs if merged_model_kwargs else None,
                **extra_kwargs
            )
        elif langchain_class == "ChatBedrockConverse":
            return cls._create_bedrock(
                model_name=provider_model_name,
                temperature=effective_temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=merged_model_kwargs if merged_model_kwargs else None,
                **extra_kwargs
            )
        else:
            raise ValueError(f"Unsupported LangChain class: {langchain_class}")
    
    @classmethod
    def _create_openai_compatible(
        cls,
        provider: str,
        model_name: str,
        base_url: Optional[str],
        api_key: str,
        temperature: Optional[float],
        streaming: bool,
        callbacks: Optional[List[BaseCallbackHandler]],
        model_kwargs: Optional[Dict[str, Any]],
        **extra_kwargs
    ) -> BaseChatModel:
        """Create an OpenAI-compatible LLM (OpenAI, NagaAI, OpenRouter, etc.)."""
        from langchain_openai import ChatOpenAI
        
        # Parameters that can be passed directly to ChatOpenAI
        direct_params = ['reasoning', 'max_tokens', 'top_p', 'frequency_penalty', 'presence_penalty']
        
        # Build initialization parameters
        llm_params = {
            "model": model_name,
            "api_key": api_key,
            "streaming": streaming,
            "max_retries": 3,  # Retry on transient network errors (connection drops, timeouts)
            "stream_usage": True,  # Enable usage tracking in streaming mode (langchain-openai 0.2+)
        }
        
        # Only add temperature if model supports it (O-series models don't)
        if temperature is not None:
            llm_params["temperature"] = temperature
        
        if base_url:
            llm_params["base_url"] = base_url
        
        if callbacks:
            llm_params["callbacks"] = callbacks
        
        # Process model_kwargs - use extra_body for non-standard params
        # This ensures provider-specific params like "thinking" are passed correctly
        if model_kwargs:
            extra_body_params = {}
            for key, value in model_kwargs.items():
                if key in direct_params:
                    llm_params[key] = value
                else:
                    # Non-standard params go into extra_body for the OpenAI SDK
                    extra_body_params[key] = value

            if extra_body_params:
                llm_params["extra_body"] = extra_body_params
                # WORKAROUND: langchain-openai has a bug where extra_body is not passed
                # in streaming mode. Force streaming off when using extra_body.
                if streaming:
                    logger.warning(
                        f"Disabling streaming for {model_name} because extra_body is used. "
                        "langchain-openai has a known bug where extra_body is not passed in streaming mode."
                    )
                    llm_params["streaming"] = False

        # Add any extra kwargs
        llm_params.update(extra_kwargs)

        logger.info(f"Creating ChatOpenAI: model={model_name}, provider={provider}, base_url={base_url}, extra_body={llm_params.get('extra_body')}")
        return ChatOpenAI(**llm_params)
    
    @classmethod
    def _create_google(
        cls,
        model_name: str,
        api_key: str,
        temperature: float,
        streaming: bool,
        callbacks: Optional[List[BaseCallbackHandler]],
        model_kwargs: Optional[Dict[str, Any]],
        **extra_kwargs
    ) -> BaseChatModel:
        """Create a Google Gemini LLM."""
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            raise ImportError(
                "langchain-google-genai is required for Google models. "
                "Install it with: pip install langchain-google-genai"
            )
        
        # IMPORTANT: Force disable streaming for Google models
        # LangChain has a known bug where streaming with Gemini causes
        # "string indices must be integers, not 'str'" errors in merge_content()
        # when the model returns mixed content types during streaming.
        # See: https://github.com/langchain-ai/langchain/issues/
        if streaming:
            logger.warning(
                f"Streaming disabled for Google model {model_name} due to LangChain "
                "compatibility issues with Gemini's streaming content format."
            )
        
        llm_params = {
            "model": model_name,
            "temperature": temperature,
            "google_api_key": api_key,
            "streaming": False,  # Always disabled - see comment above
        }
        
        if callbacks:
            llm_params["callbacks"] = callbacks
        
        # Google has specific parameter handling
        if model_kwargs:
            # Some parameters go directly, others in model_kwargs
            for key, value in model_kwargs.items():
                if key in ['max_output_tokens', 'top_p', 'top_k']:
                    llm_params[key] = value
        
        llm_params.update(extra_kwargs)
        
        logger.info(f"Creating ChatGoogleGenerativeAI: model={model_name}")
        return ChatGoogleGenerativeAI(**llm_params)
    
    @classmethod
    def _create_anthropic(
        cls,
        model_name: str,
        base_url: Optional[str],
        api_key: str,
        temperature: float,
        streaming: bool,
        callbacks: Optional[List[BaseCallbackHandler]],
        model_kwargs: Optional[Dict[str, Any]],
        **extra_kwargs
    ) -> BaseChatModel:
        """Create an Anthropic Claude LLM."""
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise ImportError(
                "langchain-anthropic is required for Anthropic models. "
                "Install it with: pip install langchain-anthropic"
            )
        
        llm_params = {
            "model": model_name,
            "temperature": temperature,
            "anthropic_api_key": api_key,
            "streaming": streaming,
            "max_retries": 3,  # Retry on transient network errors
        }
        
        if base_url:
            llm_params["anthropic_api_url"] = base_url
        
        if callbacks:
            llm_params["callbacks"] = callbacks
        
        if model_kwargs:
            llm_params["model_kwargs"] = model_kwargs
        
        llm_params.update(extra_kwargs)
        
        logger.info(f"Creating ChatAnthropic: model={model_name}")
        return ChatAnthropic(**llm_params)
    
    @classmethod
    def _create_xai(
        cls,
        model_name: str,
        api_key: str,
        temperature: float,
        streaming: bool,
        callbacks: Optional[List[BaseCallbackHandler]],
        model_kwargs: Optional[Dict[str, Any]],
        **extra_kwargs
    ) -> BaseChatModel:
        """Create an xAI (Grok) LLM."""
        try:
            from langchain_xai import ChatXAI
        except ImportError:
            raise ImportError(
                "langchain-xai is required for xAI/Grok models. "
                "Install it with: pip install langchain-xai"
            )
        
        llm_params = {
            "model": model_name,
            "temperature": temperature,
            "xai_api_key": api_key,
            "streaming": streaming,
            "max_retries": 5,  # Higher retry count for xAI's connection stability issues
            "timeout": 300,  # 5 minute timeout for long responses
            "stream_usage": True,  # Try to enable usage tracking
        }
        
        if callbacks:
            llm_params["callbacks"] = callbacks
        
        if model_kwargs:
            llm_params["model_kwargs"] = model_kwargs
        
        llm_params.update(extra_kwargs)
        
        logger.info(f"Creating ChatXAI: model={model_name}")
        return ChatXAI(**llm_params)
    
    @classmethod
    def _create_deepseek(
        cls,
        model_name: str,
        api_key: str,
        temperature: float,
        streaming: bool,
        callbacks: Optional[List[BaseCallbackHandler]],
        model_kwargs: Optional[Dict[str, Any]],
        **extra_kwargs
    ) -> BaseChatModel:
        """Create a DeepSeek LLM."""
        try:
            from langchain_deepseek import ChatDeepSeek
        except ImportError:
            raise ImportError(
                "langchain-deepseek is required for DeepSeek models. "
                "Install it with: pip install langchain-deepseek"
            )
        
        llm_params = {
            "model": model_name,
            "temperature": temperature,
            "api_key": api_key,
            "streaming": streaming,
            "max_retries": 3,  # Retry on transient network errors
        }
        
        if callbacks:
            llm_params["callbacks"] = callbacks
        
        if model_kwargs:
            llm_params["model_kwargs"] = model_kwargs
        
        llm_params.update(extra_kwargs)
        
        logger.info(f"Creating ChatDeepSeek: model={model_name}")
        return ChatDeepSeek(**llm_params)
    
    @classmethod
    def _create_moonshot(
        cls,
        model_name: str,
        api_key: str,
        temperature: float,
        streaming: bool,
        callbacks: Optional[List[BaseCallbackHandler]],
        model_kwargs: Optional[Dict[str, Any]],
        **extra_kwargs
    ) -> BaseChatModel:
        """Create a Moonshot (Kimi) LLM.

        For thinking mode (kimi-k2.5 default), uses ChatKimiThinking which
        subclasses ChatDeepSeek and preserves reasoning_content for multi-turn
        tool calls by re-injecting it into outgoing assistant messages.

        For non-thinking mode (kimi-k2.5-nonthinking), uses ChatOpenAI.
        """
        from .models_registry import PROVIDER_CONFIG, PROVIDER_MOONSHOT
        base_url = PROVIDER_CONFIG[PROVIDER_MOONSHOT].get("base_url")

        # Check if thinking mode is enabled or disabled
        thinking_config = (model_kwargs or {}).get("thinking", {})
        thinking_type = thinking_config.get("type", "enabled") if isinstance(thinking_config, dict) else "enabled"
        thinking_enabled = thinking_type != "disabled"

        if thinking_enabled:
            from .ChatKimiThinking import ChatKimiThinking

            llm_params = {
                "model": model_name,
                "api_key": api_key,
                "api_base": base_url,
                "thinking_enabled": True,
                "max_retries": 3,
                "streaming": False,  # Streaming + extra_body has langchain-openai issues
            }

            if temperature is not None:
                llm_params["temperature"] = temperature

            if callbacks:
                llm_params["callbacks"] = callbacks

            # Pass model_kwargs excluding 'thinking' (handled by the class)
            if model_kwargs:
                filtered = {k: v for k, v in model_kwargs.items() if k != "thinking"}
                if filtered:
                    llm_params["model_kwargs"] = filtered

            llm_params.update(extra_kwargs)

            logger.info(f"Creating ChatKimiThinking: model={model_name}, thinking=enabled")
            return ChatKimiThinking(**llm_params)
        else:
            # Use standard ChatOpenAI for non-thinking mode (faster, simpler)
            from langchain_openai import ChatOpenAI

            llm_params = {
                "model": model_name,
                "api_key": api_key,
                "streaming": streaming,
                "base_url": base_url,
                "max_retries": 3,
                "stream_usage": True,
            }

            if temperature is not None:
                llm_params["temperature"] = temperature

            if callbacks:
                llm_params["callbacks"] = callbacks

            # For non-thinking mode, pass thinking disabled via extra_body
            extra_body_params = {"thinking": {"type": "disabled"}}
            if model_kwargs:
                direct_params = ['max_tokens', 'top_p', 'frequency_penalty', 'presence_penalty']
                for key, value in model_kwargs.items():
                    if key in direct_params:
                        llm_params[key] = value
                    elif key != "thinking":  # Skip thinking, already handled
                        extra_body_params[key] = value

            llm_params["extra_body"] = extra_body_params
            # Disable streaming when using extra_body (langchain-openai bug)
            if streaming:
                logger.warning(
                    f"Disabling streaming for {model_name} because extra_body is used."
                )
                llm_params["streaming"] = False

            llm_params.update(extra_kwargs)

            logger.info(f"Creating Moonshot via ChatOpenAI: model={model_name}, thinking=disabled")
            return ChatOpenAI(**llm_params)
    
    @classmethod
    def _create_bedrock(
        cls,
        model_name: str,
        temperature: float,
        streaming: bool,
        callbacks: Optional[List[BaseCallbackHandler]],
        model_kwargs: Optional[Dict[str, Any]],
        **extra_kwargs
    ) -> BaseChatModel:
        """Create an AWS Bedrock LLM using ChatBedrockConverse.
        
        AWS Bedrock requires:
        - aws_access_key_id: AWS access key ID
        - aws_secret_access_key: AWS secret access key  
        - aws_bedrock_region: AWS region (e.g., 'us-east-1', 'us-west-2')
        
        These are retrieved from app settings.
        """
        try:
            from langchain_aws import ChatBedrockConverse
        except ImportError:
            raise ImportError(
                "langchain-aws is required for AWS Bedrock models. "
                "Install it with: pip install langchain-aws"
            )
        
        # Get AWS credentials from app settings
        aws_access_key = cls._get_api_key("aws_access_key_id")
        aws_secret_key = cls._get_api_key("aws_secret_access_key")
        aws_region = cls._get_api_key("aws_bedrock_region")
        
        if not aws_access_key:
            raise ValueError(
                "AWS Access Key ID not configured. "
                "Please set 'aws_access_key_id' in app settings."
            )
        if not aws_secret_key:
            raise ValueError(
                "AWS Secret Access Key not configured. "
                "Please set 'aws_secret_access_key' in app settings."
            )
        if not aws_region:
            # Default to us-east-1 if not configured
            aws_region = "us-east-1"
            logger.warning(f"AWS region not configured, defaulting to {aws_region}")
        
        llm_params = {
            "model": model_name,
            "region_name": aws_region,
            "aws_access_key_id": aws_access_key,
            "aws_secret_access_key": aws_secret_key,
            "temperature": temperature,
            "streaming": streaming,
        }
        
        if callbacks:
            llm_params["callbacks"] = callbacks
        
        # Handle model_kwargs - Bedrock uses additional_model_request_fields
        if model_kwargs:
            # Some params can go directly, others need to go to additional_model_request_fields
            additional_fields = {}
            for key, value in model_kwargs.items():
                if key in ['max_tokens', 'top_p', 'top_k', 'stop_sequences']:
                    llm_params[key] = value
                else:
                    additional_fields[key] = value
            
            if additional_fields:
                llm_params["additional_model_request_fields"] = additional_fields
        
        llm_params.update(extra_kwargs)
        
        logger.info(f"Creating ChatBedrockConverse: model={model_name}, region={aws_region}")
        return ChatBedrockConverse(**llm_params)
    
    @classmethod
    def get_model_info(cls, model_selection: str) -> Dict[str, Any]:
        """
        Get information about a model selection.
        
        Args:
            model_selection: Selection string like "nagaai/gpt5"
            
        Returns:
            Dictionary with model information including:
            - friendly_name: The model's friendly name
            - provider: The resolved provider
            - provider_model_name: The provider-specific model name
            - display_name: Human-readable model name
            - description: Model description
            - labels: Model labels (low_cost, thinking, etc.)
            - base_url: Provider's API base URL
            - api_key_setting: App setting key for API key
        """
        provider, friendly_name = parse_model_selection(model_selection)
        
        model_info = MODELS.get(friendly_name)
        if not model_info:
            return {
                "error": f"Unknown model: {friendly_name}",
                "friendly_name": friendly_name,
                "provider": provider,
            }
        
        # Resolve native provider
        resolved_provider = provider
        if provider == "native":
            resolved_provider = model_info.get("native_provider", "unknown")
        
        provider_model_name = get_model_for_provider(friendly_name, resolved_provider)
        provider_config = get_provider_config(resolved_provider) or {}
        
        return {
            "friendly_name": friendly_name,
            "provider": resolved_provider,
            "provider_model_name": provider_model_name,
            "display_name": model_info.get("display_name", friendly_name),
            "description": model_info.get("description", ""),
            "labels": model_info.get("labels", []),
            "base_url": provider_config.get("base_url"),
            "api_key_setting": provider_config.get("api_key_setting"),
            "supports_parameters": model_info.get("supports_parameters", []),
        }
    
    @classmethod
    def validate_model_selection(cls, model_selection: str) -> tuple[bool, Optional[str]]:
        """
        Validate a model selection string.
        
        Args:
            model_selection: Selection string to validate
            
        Returns:
            Tuple of (is_valid, error_message)
            - (True, None) if valid
            - (False, "error message") if invalid
        """
        try:
            provider, friendly_name = parse_model_selection(model_selection)
            
            # Check model exists
            model_info = MODELS.get(friendly_name)
            if not model_info:
                return False, f"Unknown model: {friendly_name}"
            
            # Resolve native provider
            if provider == "native":
                provider = model_info.get("native_provider")
                if not provider:
                    return False, f"Model {friendly_name} has no native provider"
            
            # Check provider has this model
            provider_model_name = get_model_for_provider(friendly_name, provider)
            if not provider_model_name:
                available = list(model_info.get("provider_names", {}).keys())
                return False, f"Model {friendly_name} not available for {provider}. Available: {available}"
            
            # Check API key is configured
            provider_config = get_provider_config(provider)
            if not provider_config:
                return False, f"Unknown provider: {provider}"
            
            api_key_setting = provider_config.get("api_key_setting")
            if api_key_setting:
                api_key = cls._get_api_key(api_key_setting)
                if not api_key:
                    return False, f"API key '{api_key_setting}' not configured for {provider}"
            
            return True, None
            
        except Exception as e:
            return False, str(e)
    
    @classmethod
    def list_available_models(cls, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all available models, optionally filtered by provider.
        
        Args:
            provider: Optional provider to filter by
            
        Returns:
            List of model info dictionaries
        """
        models = []
        
        for friendly_name, model_info in MODELS.items():
            available_providers = list(model_info.get("provider_names", {}).keys())
            
            # Filter by provider if specified
            if provider:
                resolved_provider = provider.lower()
                if resolved_provider == "native":
                    # For native, include if native provider is available
                    native_provider = model_info.get("native_provider")
                    if native_provider not in available_providers:
                        continue
                elif resolved_provider not in available_providers:
                    continue
            
            models.append({
                "friendly_name": friendly_name,
                "display_name": model_info.get("display_name", friendly_name),
                "description": model_info.get("description", ""),
                "native_provider": model_info.get("native_provider"),
                "available_providers": available_providers,
                "labels": model_info.get("labels", []),
            })
        
        return models
    
    @classmethod
    def do_llm_call_with_websearch(
        cls,
        model_selection: str,
        prompt: str,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> str:
        """
        Make an LLM call with web search enabled.
        
        This function handles provider-specific web search implementations:
        - OpenAI: Uses Responses API with web_search_preview tool
        - NagaAI: Uses Chat Completions API with web_search_options
        - xAI (Grok): Uses native Live Search API with search_parameters
        - Google (Gemini): Uses Google Search grounding
        
        Args:
            model_selection: Model selection string (e.g., "OpenAI/gpt4o", "NagaAI/grok4")
            prompt: The prompt to send to the model
            max_tokens: Maximum tokens in the response (default: 4096)
            temperature: Temperature for generation (default: 1.0)
            
        Returns:
            The text response from the model
            
        Raises:
            ValueError: If the model/provider doesn't support web search
            
        Example:
            >>> result = ModelFactory.do_llm_call_with_websearch("OpenAI/gpt4o", "What are today's top AI news?")
        """
        from openai import OpenAI
        
        # Parse the model selection string
        provider, friendly_name = parse_model_selection(model_selection)
    
        # Try to resolve friendly name to actual model name via registry
        model_info = MODELS.get(friendly_name)
        if model_info:
            # If provider is "native", resolve to the model's native provider
            if provider == "native":
                native_provider = model_info.get("native_provider")
                if native_provider:
                    provider = native_provider
                    logger.debug(f"Resolved 'native' provider to '{provider}' for model '{friendly_name}'")

            # Get provider-specific model name from registry
            actual_model = get_model_for_provider(friendly_name, provider)
            if actual_model:
                model_name = actual_model
                logger.debug(f"Resolved model '{friendly_name}' to '{model_name}' for provider '{provider}'")
            else:
                model_name = friendly_name
                logger.warning(f"Model '{friendly_name}' not found in registry for provider '{provider}', using as-is")

            # Check for fixed_temperature (e.g., kimi-k2.5 requires temperature=1)
            fixed_temp = model_info.get("fixed_temperature")
            if fixed_temp is not None:
                temperature = fixed_temp
                logger.debug(f"Using fixed_temperature={temperature} for model '{friendly_name}'")

            # Check for fixed_top_p (e.g., kimi-k2.5 requires top_p=0.95)
            fixed_top_p = model_info.get("fixed_top_p")
        else:
            model_name = friendly_name
            fixed_top_p = None
            logger.debug(f"Model '{friendly_name}' not in registry, using as actual model name")

        # Get provider config
        provider_config = get_provider_config(provider)

        # Use fixed_top_p if set, otherwise default to 1.0
        top_p = fixed_top_p if fixed_top_p is not None else 1.0

        # Determine API type and configuration based on provider
        if provider == PROVIDER_OPENAI:
            # OpenAI uses Responses API with web_search_preview tool
            base_url = OPENAI_BACKEND_URL
            api_key = get_app_setting('openai_api_key') or "dummy-key"
            return cls._call_openai_websearch(model_name, prompt, base_url, api_key, max_tokens, temperature)
        
        elif provider == PROVIDER_NAGAAI:
            # NagaAI uses Chat Completions API with web_search_options
            base_url = provider_config.get('base_url', 'https://api.naga.ac/v1') if provider_config else 'https://api.naga.ac/v1'
            api_key_setting = provider_config.get('api_key_setting', 'naga_ai_api_key') if provider_config else 'naga_ai_api_key'
            api_key = get_app_setting(api_key_setting) or "dummy-key"
            return cls._call_nagaai_websearch(model_name, prompt, base_url, api_key, max_tokens, temperature, top_p)
        
        elif provider == PROVIDER_XAI:
            # xAI (Grok) - use native Live Search API with search_parameters
            xai_config = get_provider_config(PROVIDER_XAI)
            base_url = xai_config.get('base_url', 'https://api.x.ai/v1') if xai_config else 'https://api.x.ai/v1'
            api_key_setting = xai_config.get('api_key_setting', 'xai_api_key') if xai_config else 'xai_api_key'
            api_key = get_app_setting(api_key_setting) or "dummy-key"
            return cls._call_xai_websearch(model_name, prompt, base_url, api_key, max_tokens, temperature)
        
        elif provider == PROVIDER_GOOGLE:
            # Google Gemini uses Google Search grounding
            api_key = get_app_setting('google_api_key') or "dummy-key"
            return cls._call_google_websearch(model_name, prompt, api_key, max_tokens, temperature)
        
        elif provider == PROVIDER_MOONSHOT:
            # Moonshot AI (Kimi) uses $web_search builtin tool
            moonshot_config = get_provider_config(PROVIDER_MOONSHOT)
            base_url = moonshot_config.get('base_url', 'https://api.moonshot.cn/v1') if moonshot_config else 'https://api.moonshot.cn/v1'
            api_key_setting = moonshot_config.get('api_key_setting', 'moonshot_api_key') if moonshot_config else 'moonshot_api_key'
            api_key = get_app_setting(api_key_setting) or "dummy-key"
            # Get default_model_kwargs for the model (e.g., thinking: {type: "disabled"})
            extra_body = model_info.get("default_model_kwargs") if model_info else None
            return cls._call_kimi_websearch(model_name, prompt, base_url, api_key, max_tokens, temperature, top_p, extra_body)
        
        else:
            # Model does not support web search - raise error instead of fallback
            from .models_registry import model_supports_websearch
            
            # Check if model has websearch label
            if not model_supports_websearch(friendly_name):
                error_msg = (
                    f"Model '{friendly_name}' does not support web search. "
                    f"Web search is only available for models with the 'websearch' label. "
                    f"Please select a different model that supports web search in your settings."
                )
                logger.error(error_msg)
                raise ValueError(error_msg)
            else:
                # Model has websearch label but provider not recognized
                error_msg = (
                    f"Provider '{provider}' does not have web search implementation. "
                    f"Supported providers with web search: OpenAI, NagaAI, xAI, Google, Moonshot. "
                    f"Model '{friendly_name}' cannot be used for web search with provider '{provider}'."
                )
                logger.error(error_msg)
                raise ValueError(error_msg)


    @classmethod
    def _call_openai_websearch(cls, model: str, prompt: str, base_url: str, api_key: str, max_tokens: int, temperature: float) -> str:
        """Call OpenAI Responses API with web search."""
        from openai import OpenAI
        
        client = OpenAI(base_url=base_url, api_key=api_key)
        
        try:
            # Note: OpenAI Responses API doesn't support temperature parameter
            response = client.responses.create(
                model=model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": prompt,
                            }
                        ],
                    }
                ],
                text={"format": {"type": "text"}},
                tools=[
                    {
                        "type": "web_search_preview",
                        "user_location": {"type": "approximate"},
                        "search_context_size": "low",
                    }
                ],
                max_output_tokens=max_tokens,
                top_p=1,
                store=True,
            )
            
            # Extract text from response
            result_text = ""
            if hasattr(response, 'output') and response.output:
                for item in response.output:
                    if hasattr(item, 'content'):
                        if isinstance(item.content, list):
                            for content_item in item.content:
                                if hasattr(content_item, 'text'):
                                    result_text += content_item.text + "\n\n"
                        elif hasattr(item.content, 'text'):
                            result_text += item.content.text + "\n\n"
                    elif hasattr(item, 'text'):
                        result_text += item.text + "\n\n"
                    elif isinstance(item, str):
                        result_text += item + "\n\n"
            
            return result_text.strip()
            
        except Exception as e:
            logger.error(f"Error calling OpenAI Responses API with web search: {e}", exc_info=True)
            raise


    @classmethod
    def _call_nagaai_websearch(cls, model: str, prompt: str, base_url: str, api_key: str, max_tokens: int, temperature: float, top_p: float = 1.0) -> str:
        """Call NagaAI Chat Completions API with web_search_options."""
        from openai import OpenAI

        client = OpenAI(base_url=base_url, api_key=api_key)

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                web_search_options={},  # Enable web search with default options
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p
            )
            
            # Extract text from chat completion response
            if hasattr(response, 'choices') and response.choices:
                choice = response.choices[0]
                if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
                    return choice.message.content or ""
            
            return ""
            
        except Exception as e:
            logger.error(f"Error calling NagaAI Chat API with web search: {e}", exc_info=True)
            raise


    @classmethod
    def _call_xai_websearch(cls, model: str, prompt: str, base_url: str, api_key: str, max_tokens: int, temperature: float) -> str:
        """Call xAI API with Live Search using native xai_sdk."""
        try:
            from xai_sdk import Client
            from xai_sdk.chat import user
            from xai_sdk.search import SearchParameters
        except ImportError:
            logger.error("xai-sdk package not installed. Install with: pip install xai-sdk")
            raise ValueError("xAI Live Search requires xai-sdk package")
        
        try:
            # Create xAI client with API key
            client = Client(api_key=api_key)
            
            # Create chat with Live Search enabled
            chat = client.chat.create(
                model=model,
                search_parameters=SearchParameters(mode="on"),  # Enable Live Search
            )
            
            # Add user message and get response
            chat.append(user(prompt))
            response = chat.sample()
            
            return response.content if response.content else ""
            
        except Exception as e:
            logger.error(f"Error calling xAI Live Search API: {e}", exc_info=True)
            raise


    @classmethod
    def _call_google_websearch(cls, model: str, prompt: str, api_key: str, max_tokens: int, temperature: float) -> str:
        """Call Google Gemini API with Google Search grounding."""
        try:
            # Use the new google-genai SDK (not google-generativeai) for proper GoogleSearch support
            from google import genai
            from google.genai import types
            
            client = genai.Client(api_key=api_key)
            
            # Create Google Search tool using the new SDK
            google_search_tool = types.Tool(google_search=types.GoogleSearch())
            
            # Generate content with Google Search grounding
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[google_search_tool],
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                )
            )
            
            return response.text if response.text else ""
            
        except ImportError:
            logger.error("google-genai package not installed. Install with: pip install google-genai")
            raise ValueError("Google Gemini support requires google-genai package")
        except Exception as e:
            logger.error(f"Error calling Google Gemini API with search: {e}", exc_info=True)
            raise

    @classmethod
    def _call_kimi_websearch(cls, model: str, prompt: str, base_url: str, api_key: str, max_tokens: int, temperature: float, top_p: float = 1.0, extra_body: dict = None) -> str:
        """
        Call Kimi (Moonshot AI) API with $web_search builtin tool.

        Kimi uses a unique tool_calls pattern where the $web_search builtin function
        is declared with type="builtin_function" and executed by the Kimi backend.
        See: https://platform.moonshot.cn/docs/guide/use-web-search

        Args:
            extra_body: Optional extra body parameters (e.g., {"thinking": {"type": "disabled"}})

        Note: We use httpx directly instead of the OpenAI SDK because the SDK strips
        out Kimi-specific fields like 'reasoning_content' which are required for
        thinking mode tool calls.

        IMPORTANT: The $web_search builtin tool has a known API limitation where
        reasoning_content is NOT returned in tool_call responses even when thinking
        is enabled, but the API expects it in follow-up messages. Therefore, we
        force thinking mode DISABLED for websearch calls.
        Regular tool calls (via ChatKimiThinking) work fine with thinking mode.
        """
        import httpx

        # Use httpx directly to preserve Kimi-specific fields like reasoning_content
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        api_url = f"{base_url}/chat/completions"

        # IMPORTANT: Force thinking mode DISABLED for $web_search builtin tool
        # The $web_search builtin has an API limitation where reasoning_content
        # is NOT returned in tool_call responses even when thinking is enabled,
        # but the API expects it in follow-up messages, causing error:
        # "thinking is enabled but reasoning_content is missing in assistant tool call message"
        # This is different from regular tool calls which work fine with thinking.
        # See: https://github.com/anomalyco/opencode/issues/10996
        extra_body = {"thinking": {"type": "disabled"}}
        temperature = 0.6  # Instant mode temperature
        logger.debug("Using instant mode for Kimi $web_search (API limitation with builtin tool)")

        try:
            messages = [
                {"role": "user", "content": prompt}
            ]

            # Define the $web_search builtin tool
            tools = [
                {
                    "type": "builtin_function",  # Special type for Kimi builtin functions
                    "function": {
                        "name": "$web_search",  # $ prefix indicates builtin function
                    },
                }
            ]

            finish_reason = None
            max_iterations = 5  # Prevent infinite loops
            iteration = 0

            with httpx.Client(timeout=120.0) as client:
                while finish_reason != "stop" and iteration < max_iterations:
                    iteration += 1

                    # Build request body
                    request_body = {
                        "model": model,
                        "messages": messages,
                        "tools": tools,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "top_p": top_p,
                    }
                    # Add extra_body params directly (e.g., thinking mode control)
                    if extra_body:
                        request_body.update(extra_body)

                    response = client.post(api_url, headers=headers, json=request_body)
                    response.raise_for_status()
                    response_data = response.json()

                    choice = response_data["choices"][0]
                    finish_reason = choice.get("finish_reason")
                    message = choice.get("message", {})

                    if finish_reason == "tool_calls":
                        # Model wants to use web search - add assistant message
                        assistant_msg = {
                            "role": "assistant",
                            "content": message.get("content") or "",
                            "tool_calls": message.get("tool_calls", [])
                        }

                        # Include reasoning_content if present (for thinking mode)
                        if "reasoning_content" in message and message["reasoning_content"]:
                            assistant_msg["reasoning_content"] = message["reasoning_content"]

                        messages.append(assistant_msg)

                        # Process each tool call from the raw response dict
                        for tool_call in message.get("tool_calls", []):
                            tool_call_name = tool_call.get("function", {}).get("name", "")
                            tool_call_args = tool_call.get("function", {}).get("arguments", "")
                            tool_call_arguments = json.loads(tool_call_args) if tool_call_args else {}

                            if tool_call_name == "$web_search":
                                # For $web_search, just return the arguments back
                                # The Kimi backend will execute the actual search
                                tool_result = tool_call_arguments
                            else:
                                tool_result = {"error": f"Unknown tool: {tool_call_name}"}

                            # Add tool result to messages
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.get("id", ""),
                                "name": tool_call_name,
                                "content": json.dumps(tool_result),
                            })

                    elif finish_reason == "stop":
                        # Model finished generating response
                        content = message.get("content")
                        return content if content else ""

                    else:
                        # Unexpected finish reason
                        logger.warning(f"Unexpected finish_reason from Kimi: {finish_reason}")
                        content = message.get("content")
                        return content if content else ""

                # Max iterations reached
                logger.warning(f"Kimi websearch reached max iterations ({max_iterations})")
                return ""

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error calling Kimi API: {e.response.status_code} - {e.response.text}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Error calling Kimi API with web search: {e}", exc_info=True)
            raise


# Convenience function for quick LLM creation
def create_llm(model_selection: str, **kwargs) -> BaseChatModel:
    """
    Convenience function to create an LLM from a model selection string.
    
    This is a shortcut for ModelFactory.create_llm().
    
    Args:
        model_selection: Selection string like "nagaai/gpt5" or "native/gpt4o"
        **kwargs: Additional arguments passed to ModelFactory.create_llm()
        
    Returns:
        Configured BaseChatModel instance
        
    Example:
        >>> llm = create_llm("nagaai/gpt5", temperature=0.7)
        >>> response = llm.invoke("Hello!")
    """
    return ModelFactory.create_llm(model_selection, **kwargs)
