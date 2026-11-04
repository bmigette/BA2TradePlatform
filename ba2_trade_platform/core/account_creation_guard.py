"""Rule: no ACCOUNT can be added while the FMP API key is missing.

The platform depends on FMP (screeners, the price feed, the market regime): an instance
without the key logs "FMP API key not configured" and classifies the regime UNCLASSIFIED, so an
account on it cannot trade correctly. This is the ONE place that rule lives; every path that
creates an ``AccountDefinition`` (UI today, API / import tools later) calls
``require_fmp_key_for_account_creation()`` before the write.

It gates ADDING an account only. Saving settings with an empty FMP key stays allowed (API keys
may be blank in the settings UI), and editing / deleting existing accounts is unaffected.
"""
from sqlmodel import select

from .db import get_db
from .models import AppSetting

FMP_KEY_SETTING = "FMP_API_KEY"

FMP_KEY_REQUIRED_MESSAGE = (
    "Add an FMP API key in Settings -> API keys before adding an account: "
    "the platform needs FMP for prices, screeners and the market regime."
)


class FmpKeyRequired(Exception):
    """Raised when an account is added while no FMP API key is stored."""

    def __init__(self, message: str = FMP_KEY_REQUIRED_MESSAGE):
        super().__init__(message)
        self.message = message


def fmp_key_is_configured() -> bool:
    """True when the stored FMP_API_KEY is non-blank. A DB read error propagates (never 'no key')."""
    with get_db() as session:
        row = session.exec(select(AppSetting).where(AppSetting.key == FMP_KEY_SETTING)).first()
    value = None if row is None else row.value_str
    return bool(value and value.strip())


def require_fmp_key_for_account_creation() -> None:
    """Raise ``FmpKeyRequired`` unless a non-blank FMP API key is stored."""
    if not fmp_key_is_configured():
        raise FmpKeyRequired()
