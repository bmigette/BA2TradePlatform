"""Default-off HOLD admission; all signal selection belongs to trade rules."""
from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool

SETTING = "evaluate_entry_rules_on_hold"


def evaluate_hold_entries(settings) -> bool:
    # Compatibility default for saved experts predating this optional setting.
    value = settings[SETTING] if SETTING in settings else False
    # The settings ORM materializes declared but unsaved optional keys as None.
    return False if value is None else coerce_bool(value)
