from .AlpacaAccount import AlpacaAccount
from .IBKRAccount import IBKRAccount

# Registry of account provider classes
providers = {
    "Alpaca": AlpacaAccount,
    "IBKR": IBKRAccount,
    "InteractiveBrokers": IBKRAccount,  # Alias
}

def get_account_class(provider_name):
    """
    Get the account class by provider name.
    
    Args:
        provider_name (str): The name of the account provider (e.g., "Alpaca", "IBKR")
        
    Returns:
        class: The account class for the provider, or None if not found
    """
    return providers.get(provider_name)