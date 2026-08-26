"""Historical option-data providers used to BUILD the offline options cache.

Distinct from the broker-side OptionsAccountInterface (orders / live quotes / buying
power): these are bulk history sources feeding the vendor-agnostic option_chain /
option_bar tables. See OptionsDataProviderInterface for why the seam exists.

HISTORY FLOORS ARE PER VENDOR. ``options_history_floor`` is the one place that answers
"how far back can option data go?", and it answers it for a NAMED vendor because there is
no such thing as a global answer: Alpaca stops at a measured 2024-01-18 while dxfeed (via
TastyTrade) reaches back to 2022-10. Enforcing one vendor's limit on another refuses
windows that vendor can serve; enforcing the LOWER of them admits windows the store is
empty for, which is worse -- a backtest that trades on nothing and reports it as a result.

The floors are DELEGATED to the provider classes rather than tabulated here. A copy would
drift, and it would also freeze the TastyTrade floor at import time even though that one
is deliberately env-overridable for a probe that moves it.
"""
from datetime import date
from typing import Dict, Type

from ba2_common.core.interfaces.OptionsDataProviderInterface import (
    OptionsDataProviderInterface,
)

from .alpaca import AlpacaOptionsProvider
from .parquet_store import OptionHistoryParquetStore, PartitionState
from .tastytrade import TastyTradeOptionsProvider
from .thetadata import ThetaDataOptionsProvider

__all__ = ["AlpacaOptionsProvider", "OptionHistoryParquetStore", "PartitionState",
           "TastyTradeOptionsProvider", "ThetaDataOptionsProvider",
           "OPTIONS_HISTORY_PROVIDERS", "options_history_floor"]


#: Vendor name -> provider class. Every class here constructs with NO credentials (see each
#: ``__init__``), which is what lets ``options_history_floor`` ask the vendor its own limit
#: without opening a connection.
OPTIONS_HISTORY_PROVIDERS: Dict[str, Type[OptionsDataProviderInterface]] = {
    AlpacaOptionsProvider.name: AlpacaOptionsProvider,
    TastyTradeOptionsProvider.name: TastyTradeOptionsProvider,
    ThetaDataOptionsProvider.name: ThetaDataOptionsProvider,
}


def options_history_floor(provider: str) -> date:
    """The earliest date ``provider`` has ANY option data for.

    Asked of the provider class on every call rather than cached: the TastyTrade floor is
    env-overridable (``TASTYTRADE_OPTIONS_HISTORY_FLOOR``) and ThetaData's is relative to
    today, so a snapshot taken at import time would be wrong by construction.

    An UNKNOWN vendor RAISES. Its floor is unknown, and an unknown floor is not an absent
    one -- returning some permissive default here would let a caller admit a window on the
    strength of a vendor nobody has measured.
    """
    cls = OPTIONS_HISTORY_PROVIDERS.get(str(provider or "").strip().lower())
    if cls is None:
        raise ValueError(
            f"Unknown options-history provider {provider!r}: its history floor is UNKNOWN, "
            f"and an unknown floor is not an absent one. Known vendors: "
            f"{sorted(OPTIONS_HISTORY_PROVIDERS)}.")
    return cls().history_floor()
