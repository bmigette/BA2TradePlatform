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

from typing import Any, Dict, List, Optional, Type
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.callbacks import BaseCallbackHandler

from .models_registry import (
    MODELS, PROVIDER_CONFIG, 
    get_model_for_provider, get_provider_config, parse_model_selection,
    PROVIDER_OPENAI, PROVIDER_NAGAAI, PROVIDER_GOOGLE, PROVIDER_ANTHROPIC, PROVIDER_OPENROUTER
)
from ..config import get_app_setting, OPENAI_ENABLE_STREAMING
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
        
        # Build LLM based on provider type
        langchain_class = provider_config.get("langchain_class", "ChatOpenAI")
        
        if langchain_class == "ChatOpenAI":
            return cls._create_openai_compatible(
                provider=provider,
                model_name=provider_model_name,
                base_url=provider_config.get("base_url"),
                api_key=api_key,
                temperature=temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=model_kwargs,
                **extra_kwargs
            )
        elif langchain_class == "ChatGoogleGenerativeAI":
            return cls._create_google(
                model_name=provider_model_name,
                api_key=api_key,
                temperature=temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=model_kwargs,
                **extra_kwargs
            )
        elif langchain_class == "ChatAnthropic":
            return cls._create_anthropic(
                model_name=provider_model_name,
                base_url=provider_config.get("base_url"),
                api_key=api_key,
                temperature=temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=model_kwargs,
                **extra_kwargs
            )
        elif langchain_class == "ChatXAI":
            return cls._create_xai(
                model_name=provider_model_name,
                api_key=api_key,
                temperature=temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=model_kwargs,
                **extra_kwargs
            )
        elif langchain_class == "ChatDeepSeek":
            return cls._create_deepseek(
                model_name=provider_model_name,
                api_key=api_key,
                temperature=temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=model_kwargs,
                **extra_kwargs
            )
        elif langchain_class == "MoonshotChat":
            return cls._create_moonshot(
                model_name=provider_model_name,
                api_key=api_key,
                temperature=temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=model_kwargs,
                **extra_kwargs
            )
        elif langchain_class == "ChatBedrockConverse":
            return cls._create_bedrock(
                model_name=provider_model_name,
                temperature=temperature,
                streaming=streaming,
                callbacks=callbacks,
                model_kwargs=model_kwargs,
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
        temperature: float,
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
            "temperature": temperature,
            "api_key": api_key,
            "streaming": streaming,
        }
        
        if base_url:
            llm_params["base_url"] = base_url
        
        if callbacks:
            llm_params["callbacks"] = callbacks
        
        # Process model_kwargs
        if model_kwargs:
            remaining_kwargs = {}
            for key, value in model_kwargs.items():
                if key in direct_params:
                    llm_params[key] = value
                else:
                    remaining_kwargs[key] = value
            
            if remaining_kwargs:
                llm_params["model_kwargs"] = remaining_kwargs
        
        # Add any extra kwargs
        llm_params.update(extra_kwargs)
        
        logger.info(f"Creating ChatOpenAI: model={model_name}, provider={provider}, base_url={base_url}")
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
        
        llm_params = {
            "model": model_name,
            "temperature": temperature,
            "google_api_key": api_key,
            "streaming": streaming,
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
        """Create a Moonshot (Kimi) LLM."""
        try:
            from langchain_community.chat_models.moonshot import MoonshotChat
        except ImportError:
            raise ImportError(
                "langchain-community is required for Moonshot/Kimi models. "
                "Install it with: pip install langchain-community"
            )
        
        llm_params = {
            "model": model_name,
            "moonshot_api_key": api_key,
            "streaming": streaming,
        }
        
        # MoonshotChat may not support temperature directly, check docs
        # For now, we include it if model_kwargs doesn't override
        if temperature != 0.7:  # Only set if non-default
            llm_params["temperature"] = temperature
        
        if callbacks:
            llm_params["callbacks"] = callbacks
        
        if model_kwargs:
            llm_params.update(model_kwargs)
        
        llm_params.update(extra_kwargs)
        
        logger.info(f"Creating MoonshotChat: model={model_name}")
        return MoonshotChat(**llm_params)
    
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
