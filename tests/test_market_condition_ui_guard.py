"""The trade UI's market-condition guard (plan Task 12, quality review M7/M8).

The gates and the data that feeds them are two halves of one strategy held in two places: the
enter-market RULESET carries the leaves, the expert SETTING says which profile is served. The
expert dialog can change either half on its own, so it is a door onto the same failure the deploy
importer guards: an instance that comes up enabled, scheduled, correct-looking, and unable to
enter -- indistinguishable from a strategy that found no setup.

Three things are pinned here:

* the refusal fires in BOTH directions (a gated ruleset attached under an empty setting; the
  setting cleared under a ruleset that is already gated);
* the OPEN-POSITIONS slot refuses a market leaf outright, whatever the profile says. Outside the
  entry decision pass the live resolver has no context, so such a leaf reads ``no_context`` and
  its rule never fires -- an exit or protective-order adjustment that silently stops happening.
  The importer gets that refusal from ``trade_rules_to_live_export``; the dialog can attach an
  EXISTING gated ruleset to that slot without converting anything, so it needs its own;
* a profile the dialog could not READ is never written back as the widget's empty default, and a
  save that repoints a ruleset while the setting is unknowable is refused rather than unjudged.

The tab is built with ``object.__new__`` and given only the attributes the guard reads: the real
methods are exercised, without a NiceGUI page/slot context.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import ba2_trade_platform.ui.pages.settings as settings_page
from ba2_trade_platform.ui.pages.settings import ExpertSettingsTab

ADX = "underlying_adx_14"
STATE = "structure_state"

#: What ``market_condition_fields_in_ruleset`` returns for a gated ruleset.
GATED = (("entry.cond_1", ADX),)


@pytest.fixture
def tab(monkeypatch):
    """An ExpertSettingsTab with a working profile select and a stubbed ruleset reader."""
    import ba2_common.core.market_condition_live as live

    rulesets: dict = {}
    monkeypatch.setattr(live, "market_condition_fields_in_ruleset",
                        lambda rid: rulesets.get(rid, ()))

    t = object.__new__(ExpertSettingsTab)
    t.market_condition_profile_select = SimpleNamespace(value="", options=[])
    t._market_condition_profile_loaded = True
    t.rulesets = rulesets
    return t


def _set_profile(tab, value):
    tab.market_condition_profile_select.value = value


# --------------------------------------------------------------------------- the entry door
def test_an_ungated_ruleset_passes_under_any_setting(tab):
    tab.rulesets[7] = ()
    assert tab._refuse_unserved_market_gates(7, None) is None
    _set_profile(tab, "ohlcv-v1")
    assert tab._refuse_unserved_market_gates(7, None) is None


def test_a_gated_ruleset_attached_under_an_empty_setting_is_refused(tab):
    """Direction one: the operator picks a gated ruleset and leaves the profile alone."""
    tab.rulesets[7] = GATED
    with pytest.raises(ValueError) as e:
        tab._refuse_unserved_market_gates(7, None)
    msg = str(e.value)
    assert "entry.cond_1" in msg and ADX in msg
    assert "market_condition_profile" in msg and "empty" in msg


def test_the_setting_cleared_under_a_gated_ruleset_is_refused(tab):
    """Direction two, and the more dangerous one: the ruleset is already live and gated, and the
    operator clears the profile while editing something else on the same dialog."""
    tab.rulesets[7] = GATED
    _set_profile(tab, "ohlcv-v1")
    assert tab._refuse_unserved_market_gates(7, None) is None
    _set_profile(tab, "")
    with pytest.raises(ValueError, match="empty"):
        tab._refuse_unserved_market_gates(7, None)


def test_a_profile_that_does_not_serve_the_leaf_is_refused(tab):
    """Two registered profiles: naming the wrong one is not the same as naming none, and the
    message says which field is unserved."""
    tab.rulesets[7] = GATED
    _set_profile(tab, "ta-structure-v1")
    with pytest.raises(ValueError) as e:
        tab._refuse_unserved_market_gates(7, None)
    assert ADX in str(e.value) and "ta-structure-v1" in str(e.value)


def test_a_comma_list_serving_the_leaf_passes(tab):
    tab.rulesets[7] = ((("entry.cond_1"), ADX), ("entry.cond_2", STATE))
    _set_profile(tab, "ohlcv-v1,ta-structure-v1")
    assert tab._refuse_unserved_market_gates(7, None) is None


# --------------------------------------------------------------------------- the exit door (M7)
def test_a_market_leaf_on_the_open_positions_ruleset_is_refused_whatever_the_profile(tab):
    """No profile makes this legal: outside the entry pass the resolver has no context, the rule
    never fires, and the position's exit or protective-order adjustment silently stops."""
    tab.rulesets[9] = GATED
    for profile in ("", "ohlcv-v1", "ohlcv-v1,ta-structure-v1"):
        _set_profile(tab, profile)
        with pytest.raises(ValueError) as e:
            tab._refuse_unserved_market_gates(None, 9)
        assert "open-positions" in str(e.value) and "entry.cond_1" in str(e.value)


def test_the_exit_door_is_checked_even_when_the_entry_ruleset_is_ungated(tab):
    """It is checked FIRST, so an ungated entry ruleset cannot short-circuit past it."""
    tab.rulesets[7] = ()
    tab.rulesets[9] = GATED
    with pytest.raises(ValueError, match="open-positions"):
        tab._refuse_unserved_market_gates(7, 9)


def test_an_ordinary_open_positions_ruleset_passes(tab):
    tab.rulesets[7] = GATED
    tab.rulesets[9] = ()
    _set_profile(tab, "ohlcv-v1")
    assert tab._refuse_unserved_market_gates(7, 9) is None


# ------------------------------------------------- a profile the dialog could not read (I1)
def test_an_unreadable_profile_is_never_written_back_as_the_empty_default(tab):
    """``_save_expert_settings`` writes the setting only when the dialog READ it. Sharing the
    instrument widget's handler meant a read failure left the select on '' and saved that --
    silently clearing a gated expert's profile because of an unrelated failure."""
    assert tab._market_condition_profile_savable() is True
    tab._market_condition_profile_loaded = False
    assert tab._market_condition_profile_savable() is False

    bare = object.__new__(ExpertSettingsTab)          # no widget at all
    assert bare._market_condition_profile_savable() is False


def test_an_unsavable_profile_is_judged_against_the_STORED_value(tab, monkeypatch):
    """The save is about to skip the write, so the stored setting is what the instance ends up
    with -- and that is what the ruleset must be checked against, not the widget's default."""
    tab.rulesets[7] = GATED
    tab._market_condition_profile_loaded = False
    _set_profile(tab, "")                             # the widget shows its default
    monkeypatch.setattr(settings_page, "get_expert_instance_from_id",
                        lambda i: SimpleNamespace(settings={"market_condition_profile": "ohlcv-v1"}))
    # Stored profile serves the leaf -> the save is fine even though the widget says nothing.
    assert tab._refuse_unserved_market_gates(7, None, 3) is None

    monkeypatch.setattr(settings_page, "get_expert_instance_from_id",
                        lambda i: SimpleNamespace(settings={"market_condition_profile": ""}))
    with pytest.raises(ValueError, match="empty"):
        tab._refuse_unserved_market_gates(7, None, 3)


def test_a_save_that_can_judge_nothing_is_refused_rather_than_proceeding(tab, monkeypatch):
    """Neither half is knowable: the widget was never loaded and the stored value cannot be read
    either. A save that repoints a ruleset must not proceed unjudged."""
    tab.rulesets[7] = GATED
    tab._market_condition_profile_loaded = False

    def _broken(_id):
        raise RuntimeError("instance is not loadable")

    monkeypatch.setattr(settings_page, "get_expert_instance_from_id", _broken)
    with pytest.raises(ValueError) as e:
        tab._refuse_unserved_market_gates(7, None, 3)
    assert "cannot verify the market-condition gates" in str(e.value)
    assert "market_condition_profile" in str(e.value)


def test_a_new_instance_is_judged_on_the_widget_and_needs_no_stored_value(tab):
    """The create branch passes no instance id: there is nothing stored to fall back to, and the
    widget is exactly what will be written."""
    tab.rulesets[7] = GATED
    _set_profile(tab, "ohlcv-v1")
    assert tab._refuse_unserved_market_gates(7, None) is None
    _set_profile(tab, "")
    with pytest.raises(ValueError, match="empty"):
        tab._refuse_unserved_market_gates(7, None)


# --------------------------------------------------------------------------- the save wiring
def test_the_save_path_reads_the_guard_and_the_savable_flag():
    """Both call sites in ``_save_expert`` pass BOTH ruleset slots, and the settings write is
    behind ``_market_condition_profile_savable``. Read from the source: driving the real dialog
    needs a NiceGUI client, and the ordering is what the guard is worth."""
    import inspect

    src = inspect.getsource(ExpertSettingsTab._save_expert)
    calls = [line.strip() for line in src.splitlines()
             if "_refuse_unserved_market_gates(" in line]
    assert len(calls) == 2, calls
    # The edit branch passes both rulesets and the instance id; the create branch both rulesets.
    assert any("expert_instance.enter_market_ruleset_id" in c for c in calls)
    assert any("enter_market_id, open_positions_id" in c for c in calls)

    saved = inspect.getsource(ExpertSettingsTab._save_expert_settings)
    guard = saved.index("_market_condition_profile_savable()")
    write = saved.index("save_setting(MARKET_CONDITION_PROFILE_SETTING")
    assert guard < write


# ------------------------------------------- the RULES editor, the third door (final review I2)
#
# The MENU half of this section moved out on 2026-09-22. ``_authorable_trigger_types`` filtered
# the fifteen market fields out of the Trigger Type list and ``_trigger_type_options`` added a
# persisted one back so a deployed rule could still be opened; both are gone, replaced by the
# categorised picker, which offers every field with the profile it needs named beside it. What
# those tests pinned -- an unknown key renders instead of raising, the persisted value survives
# every stored shape -- is pinned in tests/test_rule_trigger_picker.py, against the picker.
# The REFUSAL half stays here: it is what actually stops a gate reaching an exit ruleset.

class _RulesTab:
    """Just enough of the rules editor to exercise the refusal: the real method, bound."""

    def __init__(self, rules, name="exit rules"):
        from ba2_trade_platform.ui.pages.settings import TradeSettingsTab

        self.ruleset_name_input = SimpleNamespace(value=name)
        self._rules = rules
        self._refuse = TradeSettingsTab._refuse_market_gates_on_exit_ruleset.__get__(self)


@pytest.fixture
def rules_tab(monkeypatch):
    """A rules editor whose ``get_instance(EventAction, id)`` returns stub rules."""
    import ba2_trade_platform.ui.pages.settings as sp

    store = {}
    monkeypatch.setattr(sp, "get_instance", lambda model, rid: store.get(rid))
    return store


def _rule(rid, name, event_type):
    return SimpleNamespace(id=rid, name=name,
                           triggers={"cond_0": {"event_type": event_type}})


def test_a_market_gate_on_an_open_positions_ruleset_is_refused(rules_tab):
    """market_condition_rules calls this the worst outcome in the whole design: outside the entry
    pass the gate reads no_context, the rule never fires, and the position's exit or protective
    order adjustment silently stops happening."""
    rules_tab[1] = _rule(1, "stop-loss", "profit_loss_percent")
    rules_tab[2] = _rule(2, "gated", ADX)
    tab = _RulesTab(rules_tab)
    with pytest.raises(ValueError) as e:
        tab._refuse("open_positions", [1, 2])
    msg = str(e.value)
    assert "gated.cond_0" in msg and "open-positions / exit ruleset" in msg
    assert "stop-loss" not in msg


def test_an_ordinary_open_positions_ruleset_saves(rules_tab):
    rules_tab[1] = _rule(1, "stop-loss", "profit_loss_percent")
    assert _RulesTab(rules_tab)._refuse("open_positions", [1]) is None


def test_the_same_gated_rule_on_an_ENTER_MARKET_ruleset_is_allowed(rules_tab):
    """The gate belongs on the entry ruleset; only the exit slot refuses it. (Whether the
    expert's profile SERVES it is the expert dialog's question, not this one's.)"""
    rules_tab[2] = _rule(2, "gated", ADX)
    tab = _RulesTab(rules_tab)
    assert tab._refuse("enter_market", [2]) is None
    assert tab._refuse(None, [2]) is None
    assert tab._refuse("", [2]) is None


def test_a_selected_rule_that_no_longer_exists_does_not_break_the_save(rules_tab):
    rules_tab[1] = _rule(1, "stop-loss", "profit_loss_percent")
    assert _RulesTab(rules_tab)._refuse("open_positions", [1, 999]) is None


def test_the_refusal_runs_before_any_write_in_save_ruleset():
    """Read from the source: a refusal after ``update_instance``/``add_instance`` would leave the
    ruleset repointed at rules it just refused."""
    import inspect

    from ba2_trade_platform.ui.pages.settings import TradeSettingsTab

    src = inspect.getsource(TradeSettingsTab._save_ruleset)
    guard = src.index("_refuse_market_gates_on_exit_ruleset(")
    writes = [src.index(c) for c in ("update_instance(ruleset)", "add_instance(new_ruleset)")]
    assert guard < min(writes)


def test_both_halves_of_the_exit_refusal_still_hold(rules_tab):
    """N1 relaxed the MENU only. The exit-slot refusal is what actually stops a gate reaching an
    open-positions ruleset, and it is unchanged."""
    rules_tab[2] = _rule(2, "gated", ADX)
    with pytest.raises(ValueError, match="open-positions / exit ruleset"):
        _RulesTab(rules_tab)._refuse("open_positions", [2])
