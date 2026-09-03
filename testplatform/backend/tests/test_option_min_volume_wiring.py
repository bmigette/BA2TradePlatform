"""The min_volume liquidity gate must actually FIRE, not merely exist (2026-07-25).

Backstory this guards against: min_volume was plumbed end-to-end -- passes_liquidity, all
three selectors, the _OptionEntryAction ctor, the rule-builder aliases, the evaluator's
forwarded-key list -- with unit tests at every layer, and was still completely INERT, because
no caller ever set a value. Layer-by-layer tests all passed while the feature did nothing.

So these tests assert the WIRING, not the mechanism: that a real grid strategy config carries
the key, and that it survives the rule-builder into the action the engine constructs.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import pytest

import ba2test_launcher as L


ALL_OPTION_KINDS = sorted(L._OPTION_STRATS)


@pytest.mark.parametrize("kind", ALL_OPTION_KINDS)
def test_every_option_strategy_config_carries_min_volume(kind):
    """Not one hand-picked template -- EVERY pure-option strategy, so a newly added structure
    cannot silently ship without the gate."""
    cfg = L._option_entry_action_for(kind)
    assert cfg.get("option_min_volume") == L._OPTION_MIN_VOLUME_DEFAULT, (
        f"{kind} would select contracts the fill engine's participation cap rejects")


def test_min_volume_is_configurable_and_can_be_disabled(monkeypatch):
    monkeypatch.setattr(L, "_OPTION_MIN_VOLUME", 250)
    assert L._option_entry_action_for("O_IC")["option_min_volume"] == 250
    # 0 disables the gate entirely (the unconditional premium floor still applies).
    monkeypatch.setattr(L, "_OPTION_MIN_VOLUME", 0)
    assert "option_min_volume" not in L._option_entry_action_for("O_IC")


def test_min_volume_survives_the_rule_builder_into_the_action():
    """The end-to-end hop that was missing: strategy config -> rule dict -> action kwargs.
    A key that the rule builder drops is exactly as inert as one nobody sets."""
    from ba2_common.core.rule_builders import _OPTION_ACTION_PARAM_KEYS

    aliases = {canon: alts for canon, alts in _OPTION_ACTION_PARAM_KEYS}
    assert "min_volume" in aliases, "rule_builders cannot map option_min_volume -> min_volume"
    assert "option_min_volume" in aliases["min_volume"]


def test_evaluator_forwards_min_volume_to_the_action():
    """TradeActionEvaluator only forwards keys on its allow-list; an omission here would strip
    the value between the rule and the action."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS

    assert "min_volume" in _OPTION_ENTRY_PARAM_KEYS


def test_the_default_is_consistent_with_the_fill_engines_participation_cap():
    """The floor is derived from the engine's own cap, not picked arbitrarily: an order needs
    the bar to trade >= qty / participation contracts. At a 10% cap, 25 supports a 2-lot."""
    from app.services.backtest.backtest_account import _OPTION_FILL_MAX_VOLUME_PARTICIPATION

    supported_lots = L._OPTION_MIN_VOLUME_DEFAULT * _OPTION_FILL_MAX_VOLUME_PARTICIPATION
    assert supported_lots >= 2, (
        f"min_volume {L._OPTION_MIN_VOLUME_DEFAULT} only supports {supported_lots:g} contracts "
        f"at the engine's {_OPTION_FILL_MAX_VOLUME_PARTICIPATION:.0%} participation cap")

