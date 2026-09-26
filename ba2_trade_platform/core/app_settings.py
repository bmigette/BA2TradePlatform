"""The declared definitions of the application settings (the ``AppSetting`` key/value table).

Experts and accounts declare their settings (``get_settings_definitions``); the app settings
had no declaration, so the App Settings tab and the runtime each carried their own literals
(worker count 4, account refresh interval 5 minutes, AWS Bedrock region ``us-east-1``). They
agreed only by coincidence. This table is the ONE source of those defaults: the tab shows them
and the runtime readers (WorkerQueue, JobManager, ModelFactory) fall back to them.

Same shape as the expert/account definitions: ``type``, ``required``, ``description`` and,
where one exists, ``default``. A setting without a ``default`` (every API key) has none: it
is empty until set. Live-only (its readers are in-tree), so it lives in-tree.
"""
from typing import Any, Dict

APP_SETTINGS_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    # LLM provider API keys
    "openai_api_key": {"type": "str", "required": False, "description": "OpenAI API Key"},
    "openai_admin_api_key": {"type": "str", "required": False,
                             "description": "OpenAI Admin API Key (for usage data)"},
    "naga_ai_api_key": {"type": "str", "required": False, "description": "NagaAI API Key"},
    "naga_ai_admin_api_key": {"type": "str", "required": False,
                              "description": "NagaAI Admin API Key (for usage data)"},
    "anthropic_api_key": {"type": "str", "required": False, "description": "Anthropic API Key"},
    "anthropic_admin_api_key": {"type": "str", "required": False,
                                "description": "Anthropic Admin API Key (for spend/usage data)"},
    "google_api_key": {"type": "str", "required": False, "description": "Google API Key"},
    "openrouter_api_key": {"type": "str", "required": False, "description": "OpenRouter API Key"},
    "xai_api_key": {"type": "str", "required": False, "description": "xAI API Key"},
    "xai_admin_api_key": {"type": "str", "required": False,
                          "description": "xAI Admin API Key (for billing/usage data)"},
    "xai_team_id": {"type": "str", "required": False, "description": "xAI Team ID"},
    "moonshot_api_key": {"type": "str", "required": False, "description": "Moonshot API Key"},
    "deepseek_api_key": {"type": "str", "required": False, "description": "DeepSeek API Key"},
    "aws_access_key_id": {"type": "str", "required": False, "description": "AWS Access Key ID"},
    "aws_secret_access_key": {"type": "str", "required": False,
                              "description": "AWS Secret Access Key"},
    "aws_bedrock_region": {"type": "str", "required": False, "default": "us-east-1",
                           "description": "AWS Bedrock region"},
    # Data provider API keys
    "finnhub_api_key": {"type": "str", "required": False, "description": "Finnhub API Key"},
    "fred_api_key": {"type": "str", "required": False, "description": "FRED API Key"},
    "alpha_vantage_api_key": {"type": "str", "required": False,
                              "description": "Alpha Vantage API Key"},
    "FMP_API_KEY": {"type": "str", "required": False,
                    "description": "Financial Modeling Prep (FMP) API Key"},
    # Broker API keys
    "alpaca_api_key": {"type": "str", "required": False, "description": "Alpaca API Key"},
    "alpaca_api_secret": {"type": "str", "required": False, "description": "Alpaca API Secret"},
    # System settings
    "worker_count": {"type": "int", "required": False, "default": 4, "min": 1, "max": 20,
                     "description": "Worker Count"},
    "account_refresh_interval": {"type": "int", "required": False, "default": 5, "min": 1,
                                 "max": 1440,
                                 "description": "Account Refresh Interval (minutes)"},
}


def app_setting_default(key: str) -> Any:
    """The DECLARED default of app setting ``key``. Explicit access: a key with no declared
    default is a KeyError, never a guess."""
    return APP_SETTINGS_DEFINITIONS[key]["default"]
