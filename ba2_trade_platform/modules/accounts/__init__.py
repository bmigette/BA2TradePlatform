from .AlpacaAccount import AlpacaAccount
from .IBKRAccount import IBKRAccount
from .TastyTradeAccount import TastyTradeAccount

# Registry of account provider classes
providers = {
    "Alpaca": AlpacaAccount,
    "IBKR": IBKRAccount,
    "TastyTrade": TastyTradeAccount,
}

# Aliases for backward compatibility (used by get_account_class, not shown in UI dropdowns)
_aliases = {
    "InteractiveBrokers": IBKRAccount,
}

def get_account_class(provider_name):
    """
    Get the account class by provider name.
    
    Args:
        provider_name (str): The name of the account provider (e.g., "Alpaca", "IBKR")
        
    Returns:
        class: The account class for the provider, or None if not found
    """
    return providers.get(provider_name) or _aliases.get(provider_name)