"""Opt-in neutral option entry semantics shared by live and backtest.

Absent settings retain legacy behavior. The mode is persisted with the expert,
so a deployed HOLD experiment has the same eligibility as its backtest.
"""
from ba2_common.core.types import OrderRecommendation

SETTING = "neutral_option_entry_mode"
MODES = ("legacy", "hold", "low_confidence")
NEUTRAL_ACTIONS = frozenset(("open_straddle", "open_strangle", "open_iron_condor"))
ACTIONABLE = frozenset((OrderRecommendation.BUY, OrderRecommendation.SELL,
                       OrderRecommendation.OVERWEIGHT, OrderRecommendation.UNDERWEIGHT))


def entry_mode(settings) -> str:
    # Compatibility default for saved experts predating this optional setting.
    mode = settings[SETTING] if SETTING in settings else "legacy"
    # The settings ORM materializes declared but unsaved optional keys as None.
    if mode is None:
        mode = "legacy"
    if mode not in MODES:
        raise ValueError(f"Invalid {SETTING}: {mode!r}; expected one of {MODES}")
    return mode


def accepts_signal(mode, signal) -> bool:
    if mode == "hold":
        return signal == OrderRecommendation.HOLD
    return signal in ACTIONABLE


def validate_actions(summaries) -> None:
    """Never route equity or mixed rules through the opted-in option-only path."""
    if not summaries or any(
        getattr(s.get("action_type"), "value", s.get("action_type")) not in NEUTRAL_ACTIONS
        or "error" in s for s in summaries
    ):
        raise ValueError("Neutral option entry requires only straddle, strangle or iron-condor entry actions")
