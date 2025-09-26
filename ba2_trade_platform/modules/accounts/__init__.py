from .AlpacaAccount import AlpacaAccount

# Registry of account provider classes
providers = {"Alpaca": AlpacaAccount}  # Fixed typo: "Alcapa" -> "Alpaca"

def get_account_class(provider_name):
    """
    Get the account class by provider name.
    
    Args:
        provider_name (str): The name of the account provider (e.g., "Alpaca")
        
    Returns:
        class: The account class for the provider, or None if not found
    """
    return providers.get(provider_name)