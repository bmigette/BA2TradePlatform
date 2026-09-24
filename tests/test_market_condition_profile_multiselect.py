"""The expert dialog's market-condition profile is a MULTI-select over a comma-list setting.

A strategy may gate on fields from several profiles at once (the stage-1 option grid runs
``ohlcv-v1,ta-structure-v1``). The dialog used to be a single select that could only SHOW such a
value (appended as one opaque option), never CREATE it. These tests pin that it now:

* offers every registered profile, with "off" meaning nothing selected (no ``""`` option);
* shows a stored comma list as the individual profiles, and a stored empty value as nothing;
* saves the SAME string for the same selection whatever the click order (option order), so the
  saved form matches what the launcher and the deploy payloads write;
* keeps a stored name this build no longer registers visible and saveable (the save guard, which
  parses strictly, is what refuses it -- the dialog must not silently drop it);
* feeds the save guard a value it accepts when both profiles serve a ruleset's gates.

The tab is built with ``object.__new__``, like ``test_market_condition_ui_guard``: the real methods
run without a NiceGUI page context.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ba2_trade_platform.ui.pages.settings import ExpertSettingsTab

OHLCV = "ohlcv-v1"
TA = "ta-structure-v1"


@pytest.fixture
def tab(monkeypatch):
    import ba2_common.core.market_condition_live as live

    rulesets: dict = {}
    monkeypatch.setattr(live, "market_condition_fields_in_ruleset",
                        lambda rid: rulesets.get(rid, ()))
    t = object.__new__(ExpertSettingsTab)
    t.market_condition_profile_select = SimpleNamespace(
        value=[], options=t._market_condition_profile_options())
    t._market_condition_profile_loaded = True
    t.rulesets = rulesets
    return t


def test_options_are_the_registered_profiles_without_an_off_entry(tab):
    options = tab._market_condition_profile_options()
    assert OHLCV in options and TA in options
    assert "" not in options, "off is 'nothing selected' in a multi-select, not an option"


def test_a_stored_comma_list_is_shown_as_its_individual_profiles(tab):
    tab._fill_market_condition_profile(f"{OHLCV},{TA}")
    assert tab.market_condition_profile_select.value == [OHLCV, TA]
    assert tab._market_condition_profile_value() == f"{OHLCV},{TA}"


@pytest.mark.parametrize("stored", ["", None, " , "])
def test_an_empty_setting_is_shown_as_nothing_selected_and_saves_empty(tab, stored):
    tab._fill_market_condition_profile(stored)
    assert tab.market_condition_profile_select.value == []
    assert tab._market_condition_profile_value() == ""


def test_the_saved_string_does_not_depend_on_click_order(tab):
    tab.market_condition_profile_select.value = [TA, OHLCV]
    first = tab._market_condition_profile_value()
    tab.market_condition_profile_select.value = [OHLCV, TA]
    assert tab._market_condition_profile_value() == first
    options = tab._market_condition_profile_options()
    assert first == ",".join(o for o in options if o in (OHLCV, TA))


def test_a_single_profile_saves_as_that_profile(tab):
    tab.market_condition_profile_select.value = [TA]
    assert tab._market_condition_profile_value() == TA


def test_an_unregistered_stored_name_stays_visible_and_is_written_back(tab):
    """Dropping it would silently change the gating the operator sees; the save guard parses the
    saved string strictly and is what refuses it."""
    tab._fill_market_condition_profile(f"{OHLCV},legacy-v0")
    assert "legacy-v0" in tab.market_condition_profile_select.options
    assert tab.market_condition_profile_select.value == [OHLCV, "legacy-v0"]
    assert tab._market_condition_profile_value() == f"{OHLCV},legacy-v0"


def test_the_save_guard_accepts_both_profiles_for_a_ruleset_gated_on_both(tab):
    """ADX is an ohlcv-v1 field, structure_state a ta-structure-v1 field: only the pair serves."""
    tab.rulesets[7] = (("entry.adx", "underlying_adx_14"), ("entry.structure", "structure_state"))
    tab.market_condition_profile_select.value = [OHLCV]
    with pytest.raises(ValueError):
        tab._refuse_unserved_market_gates(7, None)
    tab.market_condition_profile_select.value = [OHLCV, TA]
    assert tab._refuse_unserved_market_gates(7, None) is None


def test_the_save_guard_refuses_an_unregistered_name_loudly(tab):
    tab.rulesets[7] = (("entry.adx", "underlying_adx_14"),)
    tab._fill_market_condition_profile(f"{OHLCV},legacy-v0")
    with pytest.raises(ValueError, match="legacy-v0"):
        tab._refuse_unserved_market_gates(7, None)
