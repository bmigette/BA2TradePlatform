"""
Model Registry - Centralized dictionary of all supported LLM models.

This module provides a unified registry of all LLM models supported by the platform,
with provider-specific model names, labels, and configuration details.

Usage:
    from ba2_trade_platform.core.models_registry import MODELS, get_model_for_provider, get_models_by_label
    
    # Get a model's provider-specific name
    model_name = get_model_for_provider("gpt5", "nagaai")  # Returns "gpt-5-2025-08-07"
    
    # Get all models with a specific label
    cheap_models = get_models_by_label("low_cost")
"""

from typing import Dict, List, Any, Optional, Literal, Set
from ..logger import logger


# Label definitions for model categorization
LABEL_LOW_COST = "low_cost"       # Budget-friendly models
LABEL_HIGH_COST = "high_cost"     # Premium models with better performance
LABEL_THINKING = "thinking"       # Models with reasoning/thinking capabilities
LABEL_WEBSEARCH = "websearch"     # Models with web search capabilities
LABEL_FAST = "fast"               # Fast response models
LABEL_VISION = "vision"           # Models with vision/image capabilities
LABEL_CODING = "coding"           # Models optimized for code generation
LABEL_TOOL_CALLING = "tool_calling"  # Models that support tool/function calling


# Provider definitions
PROVIDER_OPENAI = "openai"
PROVIDER_NAGAAI = "nagaai"      # NagaAI/NagaAC use same API endpoint
PROVIDER_GOOGLE = "google"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_XAI = "xai"            # xAI (Grok models)
PROVIDER_MOONSHOT = "moonshot"  # Moonshot AI (Kimi models)
PROVIDER_DEEPSEEK = "deepseek"  # DeepSeek
PROVIDER_BEDROCK = "bedrock"    # AWS Bedrock


# Provider configuration
PROVIDER_CONFIG: Dict[str, Dict[str, Any]] = {
    PROVIDER_OPENAI: {
        "display_name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key_setting": "openai_api_key",
        "langchain_class": "ChatOpenAI",
    },
    PROVIDER_NAGAAI: {
        "display_name": "NagaAI",
        "base_url": "https://api.naga.ac/v1",
        "api_key_setting": "naga_ai_api_key",
        "langchain_class": "ChatOpenAI",  # Uses OpenAI-compatible API
    },
    PROVIDER_GOOGLE: {
        "display_name": "Google",
        "base_url": None,  # Google uses default endpoint
        "api_key_setting": "google_api_key",
        "langchain_class": "ChatGoogleGenerativeAI",
    },
    PROVIDER_ANTHROPIC: {
        "display_name": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key_setting": "anthropic_api_key",
        "langchain_class": "ChatAnthropic",
    },
    PROVIDER_OPENROUTER: {
        "display_name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_setting": "openrouter_api_key",
        "langchain_class": "ChatOpenAI",  # Uses OpenAI-compatible API
    },
    PROVIDER_XAI: {
        "display_name": "xAI",
        "base_url": None,  # ChatXAI uses default endpoint
        "api_key_setting": "xai_api_key",
        "langchain_class": "ChatXAI",  # Dedicated langchain-xai package
        "api_key_env_var": "XAI_API_KEY",
    },
    PROVIDER_MOONSHOT: {
        "display_name": "Moonshot",
        "base_url": None,  # MoonshotChat uses default endpoint
        "api_key_setting": "moonshot_api_key",
        "langchain_class": "MoonshotChat",  # langchain_community.chat_models.moonshot
        "api_key_env_var": "MOONSHOT_API_KEY",
    },
    PROVIDER_DEEPSEEK: {
        "display_name": "DeepSeek",
        "base_url": None,  # ChatDeepSeek uses default endpoint
        "api_key_setting": "deepseek_api_key",
        "langchain_class": "ChatDeepSeek",  # Dedicated langchain-deepseek package
        "api_key_env_var": "DEEPSEEK_API_KEY",
    },
    PROVIDER_BEDROCK: {
        "display_name": "AWS Bedrock",
        "base_url": None,  # ChatBedrockConverse uses AWS SDK
        "api_key_setting": "aws_access_key_id",  # Uses multiple keys: aws_access_key_id, aws_secret_access_key
        "langchain_class": "ChatBedrockConverse",  # langchain-aws package
        "requires_additional_settings": ["aws_secret_access_key", "aws_bedrock_region"],
    },
}


# Model registry
# Key: Friendly model name (used throughout the platform)
# Value: Model configuration dictionary
MODELS: Dict[str, Dict[str, Any]] = {
    # =========================================================================
    # GPT-5 Family
    # =========================================================================
    "gpt5": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5",
        "description": "OpenAI's latest flagship model with advanced reasoning",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-5-2025-08-07",
            PROVIDER_NAGAAI: "gpt-5-2025-08-07",
            PROVIDER_OPENROUTER: "openai/gpt-5-2025-08-07",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_TOOL_CALLING],
    },
    "gpt5_mini": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5 Mini",
        "description": "Smaller, faster GPT-5 variant with good performance",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-5-mini-2025-08-07",
            PROVIDER_NAGAAI: "gpt-5-mini-2025-08-07",
            PROVIDER_OPENROUTER: "openai/gpt-5-mini-2025-08-07",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    "gpt5_nano": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5 Nano",
        "description": "Ultra-light GPT-5 variant for simple tasks",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-5-nano-2025-08-07",
            PROVIDER_NAGAAI: "gpt-5-nano-2025-08-07",
            PROVIDER_OPENROUTER: "openai/gpt-5-nano-2025-08-07",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    "gpt5_chat": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5 Chat",
        "description": "GPT-5 optimized for conversational use",
        "provider_names": {
            PROVIDER_NAGAAI: "gpt-5-chat-latest",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    "gpt5_codex": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5 Codex",
        "description": "GPT-5 variant optimized for code generation",
        "provider_names": {
            PROVIDER_NAGAAI: "gpt-5-codex",
        },
        "labels": [LABEL_CODING, LABEL_TOOL_CALLING],
    },
    
    # =========================================================================
    # GPT-5.1 Family (with reasoning effort parameter)
    # =========================================================================
    "gpt5.1": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5.1",
        "description": "Advanced GPT-5.1 with configurable reasoning effort",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-5.1-2025-11-13",
            PROVIDER_NAGAAI: "gpt-5.1-2025-11-13",
            PROVIDER_OPENROUTER: "openai/gpt-5.1-2025-11-13",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_TOOL_CALLING],
        "supports_parameters": ["reasoning_effort"],  # Supports {reasoning=effort:low/medium/high}
    },
    
    # =========================================================================
    # GPT-4o Family
    # =========================================================================
    "gpt4o": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-4o",
        "description": "OpenAI's multimodal flagship model",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-4o",
            PROVIDER_NAGAAI: "gpt-4o",
            PROVIDER_OPENROUTER: "openai/gpt-4o",
        },
        "labels": [LABEL_HIGH_COST, LABEL_VISION, LABEL_TOOL_CALLING],
    },
    "gpt4o_mini": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-4o Mini",
        "description": "Smaller, cost-effective GPT-4o variant",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-4o-mini",
            PROVIDER_NAGAAI: "gpt-4o-mini",
            PROVIDER_OPENROUTER: "openai/gpt-4o-mini",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_VISION, LABEL_TOOL_CALLING],
    },
    
    # =========================================================================
    # O-Series (Reasoning Models)
    # =========================================================================
    "o1": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "O1",
        "description": "OpenAI's most capable reasoning model",
        "provider_names": {
            PROVIDER_OPENAI: "o1",
            PROVIDER_NAGAAI: "o1",
            PROVIDER_OPENROUTER: "openai/o1",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_TOOL_CALLING],
    },
    "o1_mini": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "O1 Mini",
        "description": "Smaller, faster O1 variant",
        "provider_names": {
            PROVIDER_OPENAI: "o1-mini",
            PROVIDER_NAGAAI: "o1-mini",
            PROVIDER_OPENROUTER: "openai/o1-mini",
        },
        "labels": [LABEL_LOW_COST, LABEL_THINKING, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    "o3_mini": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "O3 Mini",
        "description": "OpenAI's latest mini reasoning model",
        "provider_names": {
            PROVIDER_OPENAI: "o3-mini",
            PROVIDER_NAGAAI: "o3-mini",
            PROVIDER_OPENROUTER: "openai/o3-mini",
        },
        "labels": [LABEL_LOW_COST, LABEL_THINKING, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    "o4_mini": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "O4 Mini",
        "description": "Latest O4 mini reasoning model",
        "provider_names": {
            PROVIDER_OPENAI: "o4-mini",
            PROVIDER_NAGAAI: "o4-mini",
            PROVIDER_OPENROUTER: "openai/o4-mini",
        },
        "labels": [LABEL_LOW_COST, LABEL_THINKING, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    
    # =========================================================================
    # Grok Family (xAI)
    # =========================================================================
    "grok4": {
        "native_provider": PROVIDER_XAI,
        "display_name": "Grok-4",
        "description": "xAI's Grok-4 with reasoning capabilities",
        "provider_names": {
            PROVIDER_XAI: "grok-4-0709",
            PROVIDER_NAGAAI: "grok-4-0709",
            PROVIDER_OPENROUTER: "x-ai/grok-4-0709",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_WEBSEARCH, LABEL_TOOL_CALLING],
    },
    "grok4_fast": {
        "native_provider": PROVIDER_XAI,
        "display_name": "Grok-4 Fast",
        "description": "Fast Grok-4 variant without extended reasoning",
        "provider_names": {
            PROVIDER_XAI: "grok-4-fast-non-reasoning",
            PROVIDER_NAGAAI: "grok-4-fast-non-reasoning",
            PROVIDER_OPENROUTER: "x-ai/grok-4-fast",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_WEBSEARCH, LABEL_TOOL_CALLING],
    },
    "grok4_fast_reasoning": {
        "native_provider": PROVIDER_XAI,
        "display_name": "Grok-4 Fast Reasoning",
        "description": "Fast Grok-4 with reasoning enabled",
        "provider_names": {
            PROVIDER_XAI: "grok-4-fast-reasoning",
            PROVIDER_NAGAAI: "grok-4-fast-reasoning",
            PROVIDER_OPENROUTER: "x-ai/grok-4-fast-reasoning",
        },
        "labels": [LABEL_THINKING, LABEL_FAST, LABEL_WEBSEARCH, LABEL_TOOL_CALLING],
    },
    "grok4.1_fast_reasoning": {
        "native_provider": PROVIDER_XAI,
        "display_name": "Grok-4.1 Fast Reasoning",
        "description": "Latest Grok-4.1 with fast reasoning",
        "provider_names": {
            PROVIDER_XAI: "grok-4.1-fast-reasoning",
            PROVIDER_NAGAAI: "grok-4.1-fast-reasoning",
            PROVIDER_OPENROUTER: "x-ai/grok-4.1-fast-reasoning",
        },
        "labels": [LABEL_THINKING, LABEL_FAST, LABEL_WEBSEARCH, LABEL_TOOL_CALLING],
    },
    "grok3": {
        "native_provider": PROVIDER_XAI,
        "display_name": "Grok-3",
        "description": "xAI's Grok-3 model",
        "provider_names": {
            PROVIDER_XAI: "grok-3",
            PROVIDER_NAGAAI: "grok-3",
            PROVIDER_OPENROUTER: "x-ai/grok-3",
        },
        "labels": [LABEL_THINKING, LABEL_WEBSEARCH, LABEL_TOOL_CALLING],
    },
    "grok3_mini": {
        "native_provider": PROVIDER_XAI,
        "display_name": "Grok-3 Mini",
        "description": "Smaller, faster Grok-3 variant",
        "provider_names": {
            PROVIDER_XAI: "grok-3-mini",
            PROVIDER_NAGAAI: "grok-3-mini",
            PROVIDER_OPENROUTER: "x-ai/grok-3-mini",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    
    # =========================================================================
    # Qwen Family (Alibaba)
    # =========================================================================
    "qwen3_max": {
        "native_provider": PROVIDER_NAGAAI,
        "display_name": "Qwen3 Max",
        "description": "Alibaba's most capable Qwen3 model",
        "provider_names": {
            PROVIDER_NAGAAI: "qwen3-max",
            PROVIDER_OPENROUTER: "qwen/qwen3-max",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_TOOL_CALLING],
    },
    "qwen3_80b": {
        "native_provider": PROVIDER_NAGAAI,
        "display_name": "Qwen3 80B",
        "description": "Qwen3 80B instruct model",
        "provider_names": {
            PROVIDER_NAGAAI: "qwen3-next-80b-a3b-instruct",
            PROVIDER_OPENROUTER: "qwen/qwen3-80b-instruct",
        },
        "labels": [LABEL_HIGH_COST, LABEL_TOOL_CALLING],
    },
    "qwen3_80b_thinking": {
        "native_provider": PROVIDER_NAGAAI,
        "display_name": "Qwen3 80B Thinking",
        "description": "Qwen3 80B with thinking/reasoning capabilities",
        "provider_names": {
            PROVIDER_NAGAAI: "qwen3-next-80b-a3b-thinking",
            PROVIDER_OPENROUTER: "qwen/qwen3-80b-thinking",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_TOOL_CALLING],
    },
    
    # =========================================================================
    # DeepSeek Family
    # =========================================================================
    "deepseek_v3.2": {
        "native_provider": PROVIDER_DEEPSEEK,
        "display_name": "DeepSeek V3.2",
        "description": "DeepSeek's latest v3.2 model",
        "provider_names": {
            PROVIDER_DEEPSEEK: "deepseek-chat",
            PROVIDER_NAGAAI: "deepseek-v3.2",
            PROVIDER_OPENROUTER: "deepseek/deepseek-chat",
        },
        "labels": [LABEL_LOW_COST, LABEL_CODING, LABEL_TOOL_CALLING],
    },
    "deepseek_chat": {
        "native_provider": PROVIDER_DEEPSEEK,
        "display_name": "DeepSeek Chat",
        "description": "DeepSeek chat model",
        "provider_names": {
            PROVIDER_DEEPSEEK: "deepseek-chat",
            PROVIDER_NAGAAI: "deepseek-chat-v3.1",
            PROVIDER_OPENROUTER: "deepseek/deepseek-chat",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    "deepseek_reasoner": {
        "native_provider": PROVIDER_DEEPSEEK,
        "display_name": "DeepSeek Reasoner",
        "description": "DeepSeek's reasoning model (R1)",
        "provider_names": {
            PROVIDER_DEEPSEEK: "deepseek-reasoner",
            PROVIDER_NAGAAI: "deepseek-reasoner-0528",
            PROVIDER_OPENROUTER: "deepseek/deepseek-reasoner",
        },
        "labels": [LABEL_LOW_COST, LABEL_THINKING, LABEL_TOOL_CALLING],
    },
    "deepseek_coder": {
        "native_provider": PROVIDER_DEEPSEEK,
        "display_name": "DeepSeek Coder",
        "description": "DeepSeek's code-specialized model",
        "provider_names": {
            PROVIDER_DEEPSEEK: "deepseek-coder",
            PROVIDER_NAGAAI: "deepseek-coder",
            PROVIDER_OPENROUTER: "deepseek/deepseek-coder",
        },
        "labels": [LABEL_LOW_COST, LABEL_CODING, LABEL_TOOL_CALLING],
    },
    
    # =========================================================================
    # Kimi (Moonshot AI)
    # =========================================================================
    "kimi_k2": {
        "native_provider": PROVIDER_MOONSHOT,
        "display_name": "Kimi K2",
        "description": "Moonshot AI's Kimi K2 flagship model",
        "provider_names": {
            PROVIDER_MOONSHOT: "kimi-k2-0711-preview",
            PROVIDER_NAGAAI: "kimi-k2",
            PROVIDER_OPENROUTER: "moonshot/kimi-k2",
        },
        "labels": [LABEL_LOW_COST, LABEL_TOOL_CALLING],
    },
    "kimi_k2_thinking": {
        "native_provider": PROVIDER_MOONSHOT,
        "display_name": "Kimi K2 Thinking",
        "description": "Moonshot AI's Kimi K2 with thinking capabilities",
        "provider_names": {
            PROVIDER_MOONSHOT: "kimi-k2-0711-thinking-preview",
            PROVIDER_NAGAAI: "kimi-k2-thinking",
            PROVIDER_OPENROUTER: "moonshot/kimi-k2-thinking",
        },
        "labels": [LABEL_LOW_COST, LABEL_THINKING, LABEL_TOOL_CALLING],
    },
    "kimi_k1.5": {
        "native_provider": PROVIDER_MOONSHOT,
        "display_name": "Kimi K1.5",
        "description": "Moonshot AI's Kimi K1.5 model",
        "provider_names": {
            PROVIDER_MOONSHOT: "moonshot-v1-128k",
            PROVIDER_NAGAAI: "kimi-k1.5",
            PROVIDER_OPENROUTER: "moonshot/moonshot-v1-128k",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_TOOL_CALLING],
    },
    
    # =========================================================================
    # Google Gemini Family
    # =========================================================================
    "gemini_2.5_pro": {
        "native_provider": PROVIDER_GOOGLE,
        "display_name": "Gemini 2.5 Pro",
        "description": "Google's most capable Gemini model with thinking",
        "provider_names": {
            PROVIDER_GOOGLE: "gemini-2.5-pro-preview-06-05",
            PROVIDER_NAGAAI: "gemini-2.5-pro-preview-06-05",
            PROVIDER_OPENROUTER: "google/gemini-2.5-pro-preview",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_VISION, LABEL_TOOL_CALLING],
    },
    "gemini_2.5_flash": {
        "native_provider": PROVIDER_GOOGLE,
        "display_name": "Gemini 2.5 Flash",
        "description": "Fast Gemini 2.5 variant with thinking",
        "provider_names": {
            PROVIDER_GOOGLE: "gemini-2.5-flash-preview-05-20",
            PROVIDER_NAGAAI: "gemini-2.5-flash-preview-05-20",
            PROVIDER_OPENROUTER: "google/gemini-2.5-flash-preview",
        },
        "labels": [LABEL_LOW_COST, LABEL_THINKING, LABEL_FAST, LABEL_VISION, LABEL_TOOL_CALLING],
    },
    "gemini_2.0_flash": {
        "native_provider": PROVIDER_GOOGLE,
        "display_name": "Gemini 2.0 Flash",
        "description": "Fast Gemini 2.0 for everyday tasks",
        "provider_names": {
            PROVIDER_GOOGLE: "gemini-2.0-flash",
            PROVIDER_NAGAAI: "gemini-2.0-flash",
            PROVIDER_OPENROUTER: "google/gemini-2.0-flash",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_VISION, LABEL_TOOL_CALLING],
    },
    
    # =========================================================================
    # Anthropic Claude Family
    # =========================================================================
    "claude_4_opus": {
        "native_provider": PROVIDER_ANTHROPIC,
        "display_name": "Claude 4 Opus",
        "description": "Anthropic's most capable model for complex tasks",
        "provider_names": {
            PROVIDER_ANTHROPIC: "claude-opus-4-20250514",
            PROVIDER_NAGAAI: "claude-opus-4-20250514",
            PROVIDER_OPENROUTER: "anthropic/claude-opus-4-20250514",
            PROVIDER_BEDROCK: "anthropic.claude-opus-4-20250514-v1:0",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_CODING, LABEL_TOOL_CALLING],
    },
    "claude_4_sonnet": {
        "native_provider": PROVIDER_ANTHROPIC,
        "display_name": "Claude 4 Sonnet",
        "description": "Balanced Claude 4 variant for most tasks",
        "provider_names": {
            PROVIDER_ANTHROPIC: "claude-sonnet-4-20250514",
            PROVIDER_NAGAAI: "claude-sonnet-4-20250514",
            PROVIDER_OPENROUTER: "anthropic/claude-sonnet-4-20250514",
            PROVIDER_BEDROCK: "anthropic.claude-sonnet-4-20250514-v1:0",
        },
        "labels": [LABEL_THINKING, LABEL_CODING, LABEL_TOOL_CALLING],
    },
    "claude_3.5_sonnet": {
        "native_provider": PROVIDER_ANTHROPIC,
        "display_name": "Claude 3.5 Sonnet",
        "description": "Previous generation Claude Sonnet",
        "provider_names": {
            PROVIDER_ANTHROPIC: "claude-3-5-sonnet-20241022",
            PROVIDER_NAGAAI: "claude-3-5-sonnet-20241022",
            PROVIDER_OPENROUTER: "anthropic/claude-3.5-sonnet",
            PROVIDER_BEDROCK: "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        "labels": [LABEL_CODING, LABEL_TOOL_CALLING],
    },
    "claude_3.5_haiku": {
        "native_provider": PROVIDER_ANTHROPIC,
        "display_name": "Claude 3.5 Haiku",
        "description": "Fast, lightweight Claude model",
        "provider_names": {
            PROVIDER_ANTHROPIC: "claude-3-5-haiku-20241022",
            PROVIDER_NAGAAI: "claude-3-5-haiku-20241022",
            PROVIDER_OPENROUTER: "anthropic/claude-3.5-haiku",
            PROVIDER_BEDROCK: "anthropic.claude-3-5-haiku-20241022-v1:0",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_TOOL_CALLING],
    },
}


# ============================================================================
# Helper Functions
# ============================================================================

def get_model_info(friendly_name: str) -> Optional[Dict[str, Any]]:
    """
    Get full model information by friendly name.
    
    Args:
        friendly_name: The friendly model name (e.g., "gpt5", "grok4")
        
    Returns:
        Model configuration dictionary or None if not found
    """
    return MODELS.get(friendly_name)


def get_model_for_provider(friendly_name: str, provider: str) -> Optional[str]:
    """
    Get the provider-specific model name.
    
    Args:
        friendly_name: The friendly model name (e.g., "gpt5")
        provider: The provider name (e.g., "nagaai", "openai", "native")
        
    Returns:
        Provider-specific model name or None if not available for that provider
        
    Example:
        >>> get_model_for_provider("gpt5", "nagaai")
        'gpt-5-2025-08-07'
        >>> get_model_for_provider("gpt5", "native")  # Returns OpenAI name since native_provider is openai
        'gpt-5-2025-08-07'
    """
    model_info = MODELS.get(friendly_name)
    if not model_info:
        return None
    
    # Handle "native" provider - use the model's native provider
    if provider.lower() == "native":
        provider = model_info["native_provider"]
    
    return model_info.get("provider_names", {}).get(provider.lower())


def get_models_by_label(label: str) -> List[str]:
    """
    Get all models that have a specific label.
    
    Args:
        label: The label to filter by (e.g., "low_cost", "thinking")
        
    Returns:
        List of friendly model names with that label
    """
    return [name for name, info in MODELS.items() if label in info.get("labels", [])]


def get_models_by_provider(provider: str) -> List[str]:
    """
    Get all models available for a specific provider.
    
    Args:
        provider: The provider name (e.g., "nagaai", "openai")
        
    Returns:
        List of friendly model names available for that provider
    """
    provider = provider.lower()
    return [
        name for name, info in MODELS.items() 
        if provider in info.get("provider_names", {})
    ]


def get_all_labels() -> Set[str]:
    """
    Get all unique labels across all models.
    
    Returns:
        Set of all label strings
    """
    labels = set()
    for info in MODELS.values():
        labels.update(info.get("labels", []))
    return labels


def get_all_providers() -> List[str]:
    """
    Get all available providers.
    
    Returns:
        List of provider names
    """
    return list(PROVIDER_CONFIG.keys())


def get_provider_config(provider: str) -> Optional[Dict[str, Any]]:
    """
    Get configuration for a specific provider.
    
    Args:
        provider: The provider name
        
    Returns:
        Provider configuration dictionary or None
    """
    return PROVIDER_CONFIG.get(provider.lower())


def format_model_string(friendly_name: str, provider: str) -> str:
    """
    Format a model selection as provider/model string.
    
    Args:
        friendly_name: The friendly model name (e.g., "gpt5")
        provider: The provider name (e.g., "nagaai", "native")
        
    Returns:
        Formatted string like "nagaai/gpt5" or "native/gpt5"
        
    Note:
        The friendly name is always used, not the provider-specific name.
        The ModelFactory will resolve the actual model name when creating the LLM.
    """
    return f"{provider.lower()}/{friendly_name}"


def parse_model_selection(model_string: str) -> tuple[str, str]:
    """
    Parse a model selection string back to provider and friendly name.
    
    Args:
        model_string: String like "nagaai/gpt5" or "native/gpt5"
        
    Returns:
        Tuple of (provider, friendly_name)
    """
    if "/" in model_string:
        provider, friendly_name = model_string.split("/", 1)
        return provider.lower(), friendly_name
    else:
        # Assume native provider if no prefix
        return "native", model_string


def get_model_display_info(friendly_name: str) -> Dict[str, Any]:
    """
    Get display information for a model (for UI purposes).
    
    Args:
        friendly_name: The friendly model name
        
    Returns:
        Dictionary with display information including:
        - display_name: Human-readable name
        - description: Model description
        - native_provider: The model's native provider
        - available_providers: List of providers that support this model
        - labels: List of labels
    """
    model_info = MODELS.get(friendly_name)
    if not model_info:
        return {
            "display_name": friendly_name,
            "description": "Unknown model",
            "native_provider": "unknown",
            "available_providers": [],
            "labels": [],
        }
    
    return {
        "display_name": model_info.get("display_name", friendly_name),
        "description": model_info.get("description", ""),
        "native_provider": model_info.get("native_provider", "unknown"),
        "available_providers": list(model_info.get("provider_names", {}).keys()),
        "labels": model_info.get("labels", []),
    }
