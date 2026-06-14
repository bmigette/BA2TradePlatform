"""Re-export shim: implementation lives in ba2_common.core.interfaces (Phase 6 migration).

Kept so existing ``from ba2_trade_platform.core.interfaces import X`` and
``from ba2_trade_platform.core.interfaces.X import X`` imports resolve unchanged.
The single source of truth is now ba2_common.core.interfaces.

Submodule-shadowing note: every interface here is also an in-tree submodule SHIM
file (core/interfaces/MarketNewsInterface.py etc.) that re-exports the class. When
any live module imports such a submodule, Python binds the *submodule object* onto
this package's namespace (a one-time setattr on first import), which would shadow
the same-named *class*. To stay robust regardless of import order, we EAGERLY
import every interface submodule shim FIRST (so the one-time submodule setattr
happens now), then bind the class/function names LAST so they win — mirroring the
package __init__'s own ordering. Subsequent submodule imports are sys.modules cache
hits and do NOT re-setattr, so the class bindings survive.
"""
# 1) Eagerly import every interface submodule shim so their one-time submodule
#    setattr-on-parent happens BEFORE we bind the class names below.
from . import ReadOnlyAccountInterface as _m1  # noqa: F401
from . import AccountInterface as _m2  # noqa: F401
from . import OptionsAccountInterface as _m3  # noqa: F401
from . import MarketExpertInterface as _m4  # noqa: F401
from . import ExtendableSettingsInterface as _m5  # noqa: F401
from . import SmartRiskExpertInterface as _m6  # noqa: F401
from . import LiveExpertInterface as _m7  # noqa: F401
from . import DataProviderInterface as _m8  # noqa: F401
from . import MarketIndicatorsInterface as _m9  # noqa: F401
from . import CompanyFundamentalsOverviewInterface as _m10  # noqa: F401
from . import CompanyFundamentalsDetailsInterface as _m11  # noqa: F401
from . import MarketNewsInterface as _m12  # noqa: F401
from . import MacroEconomicsInterface as _m13  # noqa: F401
from . import CompanyInsiderInterface as _m14  # noqa: F401
from . import MarketDataProviderInterface as _m15  # noqa: F401
from . import SocialMediaDataProviderInterface as _m16  # noqa: F401
from . import ScreenerProviderInterface as _m17  # noqa: F401

# 2) Bind the actual classes/functions LAST so they win over the submodule objects.
from ba2_common.core.interfaces import (  # noqa: F401
    ReadOnlyAccountInterface,
    AccountInterface,
    OptionsAccountInterface,
    MarketExpertInterface,
    BacktestInterface,
    ExtendableSettingsInterface,
    SmartRiskExpertInterface,
    LiveExpertInterface,
    DataProviderInterface,
    MarketIndicatorsInterface,
    CompanyFundamentalsOverviewInterface,
    CompanyFundamentalsDetailsInterface,
    MarketNewsInterface,
    MacroEconomicsInterface,
    CompanyInsiderInterface,
    MarketDataProviderInterface,
    SocialMediaDataProviderInterface,
    ScreenerProviderInterface,
    LLMServiceInterface,
    LLMServiceNotConfigured,
    set_llm_service,
    get_llm_service,
)

__all__ = [
    "ReadOnlyAccountInterface",
    "AccountInterface",
    "OptionsAccountInterface",
    "MarketExpertInterface",
    "BacktestInterface",
    "ExtendableSettingsInterface",
    "SmartRiskExpertInterface",
    "LiveExpertInterface",
    "DataProviderInterface",
    "MarketIndicatorsInterface",
    "CompanyFundamentalsOverviewInterface",
    "CompanyFundamentalsDetailsInterface",
    "MarketNewsInterface",
    "MacroEconomicsInterface",
    "CompanyInsiderInterface",
    "MarketDataProviderInterface",
    "SocialMediaDataProviderInterface",
    "ScreenerProviderInterface",
    "LLMServiceInterface",
    "LLMServiceNotConfigured",
    "set_llm_service",
    "get_llm_service",
]
