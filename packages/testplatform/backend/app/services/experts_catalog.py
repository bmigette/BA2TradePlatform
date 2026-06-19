"""Expert catalog: the list of backtestable experts + their settings definitions.

The frontend needs to render the expert picker and, per expert, the settings
form. Both come straight from the expert classes themselves:

  * the class -> module map is sourced from
    ``app.services.backtest.daily_backtest_handler._SUPPORTED_EXPERTS`` (the single
    source of truth for which experts this phase ships);
  * ``bypasses_classic_rm`` / ``uses_risk_manager`` are class attributes on
    ``MarketExpertInterface``;
  * the settings definitions come from the classmethod
    ``get_merged_settings_definitions()`` which merges the shared builtin settings
    (``sizing_mode``, ``risk_per_trade_pct``, ATR knobs, ...) with the expert's own
    ``get_settings_definitions()``.

None of this needs a DB or a full expert ``__init__``: ``get_merged_settings_definitions``
is a classmethod that lazily fills ``_builtin_settings`` via ``_ensure_builtin_settings``,
so we only import the class and read class-level attrs / call the classmethod.
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List

from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS


def _load_class(class_name: str):
    """Import and return the expert class for ``class_name``.

    Raises ``KeyError`` if the class isn't one of the supported experts.
    """
    module_path = _SUPPORTED_EXPERTS[class_name]  # KeyError -> unknown expert
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def list_experts() -> List[Dict[str, Any]]:
    """Return the supported experts with their RM-routing flags, sorted by class.

    Each entry: ``{class, label, bypasses_classic_rm, uses_risk_manager}``.
    """
    experts: List[Dict[str, Any]] = []
    for class_name in sorted(_SUPPORTED_EXPERTS):
        cls = _load_class(class_name)
        experts.append(
            {
                "class": class_name,
                "label": class_name,
                "bypasses_classic_rm": bool(getattr(cls, "bypasses_classic_rm", False)),
                "uses_risk_manager": bool(getattr(cls, "uses_risk_manager", True)),
            }
        )
    return experts


def settings_definitions(class_name: str) -> Dict[str, Any]:
    """Return the merged settings definitions for ``class_name``.

    Raises ``KeyError`` if the class is unknown.
    """
    cls = _load_class(class_name)
    return cls.get_merged_settings_definitions()
