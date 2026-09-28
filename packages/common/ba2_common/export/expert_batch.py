"""The ``expert_batch`` v1.0 export format (live expert settings + rules), as built by
ba2_trade_platform/core/expert_batch_export_import.py. Moved here (site plan P0a) so the public
site emits the same file from an imported live-DB copy. Pure."""
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

EXPORT_TYPE = "expert_batch"
EXPORT_VERSION = "1.0"

#: The two slots an expert can point a ruleset at, and the payload key naming each one.
RULESET_SLOTS = (
    ("enter_market_ruleset_id", "enter_market_ruleset_name"),
    ("open_positions_ruleset_id", "open_positions_ruleset_name"),
)


def build_expert_batch_entry(*, expert_type: str, alias: Optional[str],
                             user_description: Optional[str], enabled: Any,
                             virtual_equity_pct: Any, priority: Any, account_id: Any,
                             ruleset_names: Dict[str, Optional[str]],
                             rulesets_export: Optional[Dict[str, Any]],
                             expert_settings: Dict[str, Any],
                             symbol_settings: Dict[str, Any]) -> Dict[str, Any]:
    """One ``experts[]`` entry. ``ruleset_names`` maps each RULESET_SLOTS name key to the
    ruleset's name (or None); ``rulesets_export`` is a ``rulesets_export_envelope`` or None."""
    entry: Dict[str, Any] = {
        "expert_type": expert_type,
        "general": {
            "alias": alias or "",
            "user_description": user_description,
            "enabled": enabled,
            "virtual_equity_pct": virtual_equity_pct,
            "priority": priority,
            "account_id": account_id,
        },
    }
    for _id_attr, name_key in RULESET_SLOTS:
        entry[name_key] = ruleset_names.get(name_key)
    entry["rulesets"] = rulesets_export
    entry["expert_settings"] = dict(expert_settings)
    entry["symbol_settings"] = symbol_settings
    return entry


def build_batch_envelope(entries: Iterable[Dict[str, Any]],
                         exported_at: Optional[str] = None) -> Dict[str, Any]:
    return {
        "export_version": EXPORT_VERSION,
        "export_type": EXPORT_TYPE,
        "export_timestamp": exported_at or datetime.now().isoformat(),
        "experts": list(entries),
    }
