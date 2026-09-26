"""The expert dialog on an instance with NO stored setting rows (bug report 2026-09-22).

``ExtendableSettingsInterface.settings`` pre-fills every defined key with ``None`` when no row
exists. The dialog read with ``settings_source.get(key, <literal>)``; the key is present, so it
got ``None``, the checkboxes held ``None``, and Save died in ``coerce_bool(None)`` on
``allow_hedging`` (since retired by the netting rule; the same trap held for every builtin
bool). The fix resolves every displayed value from the DECLARED default
(``get_merged_settings_definitions()``), never from a literal in the UI.

The tab is built with ``object.__new__`` and given plain stand-in widgets (the pattern of
test_market_condition_ui_guard): the real load/save methods run without a NiceGUI client.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import ba2_trade_platform.core.utils as core_utils
import ba2_trade_platform.ui.pages.settings as settings_page
from ba2_trade_platform.ui.pages.settings import ExpertSettingsTab
from ba2_trade_platform.ui.utils.setting_display import (
    SettingHasNoDisplayValue, resolve_setting_for_display, unset_bool_settings,
)
from tests.conftest import MockExpert
from tests.factories import create_account_definition, create_expert_instance

BOOL_CONTROLS = {
    "enable_buy": "enable_buy_checkbox",
    "enable_sell": "enable_sell_checkbox",
    "allow_automated_trade_opening": "allow_automated_trade_opening_checkbox",
    "allow_automated_trade_modification": "allow_automated_trade_modification_checkbox",
}
VALUE_CONTROLS = {
    "max_virtual_equity_per_instrument_percent": "max_virtual_equity_per_instrument_input",
    "min_available_balance_pct": "min_available_balance_pct_input",
    "risk_per_trade_pct": "risk_per_trade_pct_input",
    "atr_multiplier": "atr_multiplier_input",
    "atr_period": "atr_period_input",
    "min_stop_loss_pct": "min_stop_loss_pct_input",
    "sizing_mode": "sizing_mode_select",
    "risk_manager_model": "risk_manager_model_input",
    "dynamic_instrument_selection_model": "dynamic_instrument_selection_model_input",
    "risk_manager_mode": "risk_manager_mode_select",
    "smart_risk_manager_user_instructions": "smart_risk_manager_user_instructions_input",
    "smart_risk_manager_max_iterations": "smart_risk_manager_max_iterations_input",
    "smart_risk_manager_analysis_window_hours": "smart_risk_manager_analysis_window_hours_input",
}
UNLOADED = object()   # what a widget holds before the loader touches it


def _defs():
    return MockExpert.get_merged_settings_definitions()


@pytest.fixture
def instance():
    acc = create_account_definition()
    return create_expert_instance(acc.id)


@pytest.fixture
def tab(monkeypatch, instance):
    monkeypatch.setattr(core_utils, "get_expert_instance_from_id", lambda i: MockExpert(i))
    t = object.__new__(ExpertSettingsTab)
    t._imported_expert_settings = None
    for attr in list(BOOL_CONTROLS.values()) + list(VALUE_CONTROLS.values()):
        setattr(t, attr, SimpleNamespace(value=UNLOADED))
    t.risk_atr_settings_container = SimpleNamespace(set_visibility=lambda v: None)
    t.expert_select = SimpleNamespace(value="MockExpert")
    t._get_expert_class = lambda name: MockExpert
    return t


def _no_rows(instance_id):
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import ExpertSetting
    with get_db() as s:
        return not s.exec(select(ExpertSetting).where(ExpertSetting.instance_id == instance_id)).all()


# ------------------------------------------------------------------------------ the loader
def test_every_builtin_checkbox_shows_its_declared_default_never_None(tab, instance):
    assert _no_rows(instance.id)
    tab._load_general_settings(instance)
    defs = _defs()
    for key, attr in BOOL_CONTROLS.items():
        value = getattr(tab, attr).value
        assert value is not UNLOADED, f"{key} was never loaded"
        assert isinstance(value, bool), f"{key} shows {value!r}, not a bool"
        assert value == defs[key]["default"], key


def test_every_other_builtin_control_shows_its_declared_default(tab, instance):
    """Including the ones whose UI literal had drifted from the declaration (smart RM max
    iterations was 10 vs 20, the model literals were 'nagaai/gpt5')."""
    tab._load_general_settings(instance)
    defs = _defs()
    for key, attr in VALUE_CONTROLS.items():
        shown = getattr(tab, attr).value
        assert shown is not UNLOADED and shown is not None, f"{key} shows {shown!r}"
        assert str(shown) == str(defs[key]["default"]), f"{key}: {shown!r} != {defs[key]['default']!r}"


def test_the_ui_follows_the_declaration_not_a_literal(tab, instance, monkeypatch):
    """Flip declared defaults: the dialog must follow. A literal in the UI would not."""
    defs = _defs()   # also ensures the builtin dict exists before patching it
    sell, buy = defs["enable_sell"]["default"], defs["enable_buy"]["default"]
    monkeypatch.setitem(MockExpert._builtin_settings["enable_sell"], "default", not sell)
    monkeypatch.setitem(MockExpert._builtin_settings["enable_buy"], "default", not buy)
    monkeypatch.setitem(MockExpert._builtin_settings["smart_risk_manager_max_iterations"],
                        "default", 37)
    tab._load_general_settings(instance)
    assert tab.enable_sell_checkbox.value is (not sell)
    assert tab.enable_buy_checkbox.value is (not buy)
    assert tab.smart_risk_manager_max_iterations_input.value == 37


def test_a_stored_value_beats_the_default(tab, instance):
    expert = MockExpert(instance.id)
    expert.save_setting("enable_sell", True, setting_type="bool")
    expert.save_setting("enable_buy", False, setting_type="bool")
    tab._load_general_settings(instance)
    assert tab.enable_sell_checkbox.value is True
    assert tab.enable_buy_checkbox.value is False


# ------------------------------------------------------------------------- legacy migration
def test_legacy_automatic_trading_is_never_migrated_into_live_permissions(tab, instance):
    """LIVE SAFETY (review 2026-09-22). Only the two declared keys gate automated trading
    (TradeManager, and -- since its legacy read was removed -- TradingAgents too, see
    test_tradingagents_auto_trade_gate); an expert with an old automatic_trading=true row and
    no new-key rows trades with them at their declared default (False). A reachable migration
    would tick both boxes and a no-edit save would switch automated trading ON. The dialog
    must show the declared defaults, never True, and a no-edit save writes nothing."""
    MockExpert(instance.id).save_setting("automatic_trading", "true", setting_type="str")
    defs = _defs()
    assert defs["allow_automated_trade_opening"]["default"] is False
    assert defs["allow_automated_trade_modification"]["default"] is False
    tab._load_general_settings(instance)
    assert tab.allow_automated_trade_opening_checkbox.value is False
    assert tab.allow_automated_trade_modification_checkbox.value is False
    tab._save_expert_settings(instance.id)
    assert [r[0] for r in _rows(instance.id)] == ["automatic_trading"]
    expert = MockExpert(instance.id)
    assert expert.get_setting_with_interface_default("allow_automated_trade_opening") is False
    assert expert.get_setting_with_interface_default("allow_automated_trade_modification") is False


def test_legacy_automatic_trading_is_ignored_once_the_new_keys_are_stored(tab, instance):
    expert = MockExpert(instance.id)
    expert.save_setting("automatic_trading", "true", setting_type="str")
    expert.save_setting("allow_automated_trade_opening", False, setting_type="bool")
    expert.save_setting("allow_automated_trade_modification", False, setting_type="bool")
    tab._load_general_settings(instance)
    assert tab.allow_automated_trade_opening_checkbox.value is False
    assert tab.allow_automated_trade_modification_checkbox.value is False


# ------------------------------------------------------------------------------ the save
def test_load_then_save_with_no_rows_writes_nothing(tab, instance):
    """The reported crash: load an instance with no rows, save it. It saves, and -- a no-edit
    save being a no-op -- the declared defaults stay DECLARED, not frozen into rows."""
    tab._load_general_settings(instance)
    tab._save_expert_settings(instance.id)
    assert _no_rows(instance.id)
    expert = MockExpert(instance.id)
    for key in list(BOOL_CONTROLS) + ["smart_risk_manager_max_iterations"]:
        assert expert.get_setting_with_interface_default(key) == _defs()[key]["default"], key


def test_a_checkbox_holding_None_is_refused_by_name_before_any_write(tab, instance, monkeypatch):
    notes = []
    monkeypatch.setattr(settings_page.ui, "notify", lambda msg, **kw: notes.append((msg, kw)))
    tab._load_general_settings(instance)
    tab.enable_sell_checkbox.value = None
    tab.expert_settings_inputs = {}
    tab._save_expert(instance)
    assert notes, "no notification"
    msg, kw = notes[-1]
    assert "enable_sell" in msg and kw.get("type") == "negative"
    assert _no_rows(instance.id), "a refused save must write nothing"


def test_an_expert_specific_bool_holding_None_is_refused_too(tab, instance, monkeypatch):
    notes = []
    monkeypatch.setattr(settings_page.ui, "notify", lambda msg, **kw: notes.append((msg, kw)))
    monkeypatch.setattr(MockExpert, "get_settings_definitions", classmethod(
        lambda cls: {"my_flag": {"type": "bool", "required": False, "default": True}}))
    tab._load_general_settings(instance)
    tab.expert_settings_inputs = {"my_flag": SimpleNamespace(value=None)}
    tab._save_expert(instance)
    assert notes and "my_flag" in notes[-1][0]
    assert _no_rows(instance.id)


# ------------------------------------------------------------------------------ the helper
def test_helper_contract():
    defs = {
        "b": {"type": "bool", "default": True},
        "b_nodef": {"type": "bool", "required": False},
        "s_req": {"type": "str", "required": True},
        "f": {"type": "float", "default": 2.5},
    }
    assert resolve_setting_for_display(defs, {"b": None}, "b") is True
    assert resolve_setting_for_display(defs, {}, "b") is True
    assert resolve_setting_for_display(defs, {"b": "false"}, "b") is False   # imported spelling
    assert resolve_setting_for_display(defs, {"b": "1"}, "b") is True
    assert resolve_setting_for_display(defs, {"f": None}, "f") == 2.5
    assert resolve_setting_for_display(defs, {"f": 0.0}, "f") == 0.0         # a stored 0 is a value
    assert resolve_setting_for_display(defs, {}, "s_req") is None
    assert resolve_setting_for_display(defs, {"f": "None"}, "f") == 2.5      # historical str(None)
    assert resolve_setting_for_display(defs, {"b": "None"}, "b") is True
    with pytest.raises(SettingHasNoDisplayValue, match="b_nodef"):
        resolve_setting_for_display(defs, {}, "b_nodef")
    with pytest.raises(KeyError):
        resolve_setting_for_display(defs, {}, "undeclared")
    assert unset_bool_settings({"b": None, "f": None, "b_nodef": False}, defs) == ["b"]


# ------------------------------------------------------------------------------ account form
def test_account_save_refuses_a_bool_left_unset_by_name(monkeypatch):
    """AlpacaAccount.paper_account is a required bool with NO default: a new account's form
    shows it indeterminate (None) instead of a silent False (= live), and the save refuses
    until it is set -- naming it, before anything is written."""
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import AccountDefinition
    from ba2_trade_platform.ui.pages.settings import AccountDefinitionsTab

    notes = []
    monkeypatch.setattr(settings_page.ui, "notify", lambda msg, **kw: notes.append((msg, kw)))
    t = object.__new__(AccountDefinitionsTab)
    t.type_select = SimpleNamespace(value="Alpaca")
    t.name_input = SimpleNamespace(value="acc")
    t.desc_input = SimpleNamespace(value="")
    t.settings_inputs = {"paper_account": SimpleNamespace(value=None),
                         "api_key": SimpleNamespace(value="k"),
                         "api_secret": SimpleNamespace(value="s")}
    t.save_account(None)
    assert notes and "paper_account" in notes[-1][0] and notes[-1][1].get("type") == "negative"
    with get_db() as s:
        assert not s.exec(select(AccountDefinition)).all(), "a refused save must write nothing"


# ------------------------------------------------------------------- a load that failed
def test_a_failed_load_refuses_the_save_and_writes_nothing(tab, instance, monkeypatch):
    """A load failure leaves the form on NEW-EXPERT defaults; saving would write them over the
    real settings. The failure is shown, and the save is refused before any write."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import ExpertInstance

    notes = []
    monkeypatch.setattr(settings_page.ui, "notify", lambda msg, **kw: notes.append((msg, kw)))

    def _broken(_id):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(core_utils, "get_expert_instance_from_id", _broken)
    tab._load_general_settings(instance)
    assert notes and notes[-1][1].get("type") == "negative"
    assert "settings unreadable" in notes[-1][0]

    before = get_instance(ExpertInstance, instance.id)
    tab.alias_input = SimpleNamespace(value="SHOULD-NOT-BE-WRITTEN")
    tab._save_expert(instance)
    assert "could not be loaded" in notes[-1][0] and notes[-1][1].get("type") == "negative"
    assert _no_rows(instance.id)
    after = get_instance(ExpertInstance, instance.id)
    assert after.alias == before.alias


def test_a_successful_reload_clears_the_load_error(tab, instance):
    tab._general_settings_load_error = "stale"
    tab._load_general_settings(instance)
    assert tab._general_settings_load_error is None


# ------------------------------------------------------------------- numeric save fallbacks
def test_a_cleared_numeric_field_saves_the_DECLARED_default(tab, instance, monkeypatch):
    """Not a literal: move the declared default and the save follows it."""
    _defs()
    monkeypatch.setitem(MockExpert._builtin_settings["atr_period"], "default", 33)
    monkeypatch.setitem(MockExpert._builtin_settings["smart_risk_manager_max_iterations"], "default", 41)
    tab._load_general_settings(instance)
    tab.atr_period_input.value = ""                          # cleared text input
    tab.smart_risk_manager_max_iterations_input.value = None  # cleared ui.number
    tab._save_expert_settings(instance.id)
    stored = MockExpert(instance.id).settings
    assert stored["atr_period"] == 33
    assert stored["smart_risk_manager_max_iterations"] == 41


def test_an_empty_numeric_field_with_no_declared_default_is_refused_by_name(tab, instance, monkeypatch):
    notes = []
    monkeypatch.setattr(settings_page.ui, "notify", lambda msg, **kw: notes.append((msg, kw)))
    monkeypatch.setattr(MockExpert, "get_settings_definitions", classmethod(
        lambda cls: {"my_ratio": {"type": "float", "required": False}}))
    tab._load_general_settings(instance)
    tab.expert_settings_inputs = {"my_ratio": SimpleNamespace(value="")}
    tab._save_expert(instance)
    assert notes and "my_ratio" in notes[-1][0] and notes[-1][1].get("type") == "negative"
    assert _no_rows(instance.id)


def test_an_unparsable_numeric_field_is_refused_not_zeroed(tab, instance, monkeypatch):
    notes = []
    monkeypatch.setattr(settings_page.ui, "notify", lambda msg, **kw: notes.append((msg, kw)))
    tab._load_general_settings(instance)
    tab.atr_multiplier_input.value = "abc"
    tab.expert_settings_inputs = {}
    tab._save_expert(instance)
    assert notes and "atr_multiplier" in notes[-1][0]
    assert _no_rows(instance.id)


def test_numeric_helper_contract():
    from ba2_trade_platform.ui.utils.setting_display import (
        NumericSettingNotSavable, numeric_setting_for_save,
    )
    defs = {"i": {"type": "int", "default": 14}, "f": {"type": "float"}}
    assert numeric_setting_for_save(defs, "i", "", int) == 14
    assert numeric_setting_for_save(defs, "i", None, int) == 14
    assert numeric_setting_for_save(defs, "i", "20", int) == 20
    assert numeric_setting_for_save(defs, "i", 20.0, int) == 20          # ui.number's whole float
    big = 2 ** 53 + 1                                                      # a float round-trip loses it
    assert numeric_setting_for_save(defs, "i", str(big), int) == big
    assert numeric_setting_for_save(defs, "f", "0", float) == 0.0      # a typed 0 is a value
    assert numeric_setting_for_save(defs, "i", "14.0", int) == 14         # GA-deployed int gene
    assert numeric_setting_for_save(defs, "i", " 14.00 ", int) == 14
    assert numeric_setting_for_save(defs, "i", "9007199254740993", int) == 9007199254740993
    assert numeric_setting_for_save(defs, "i", "9007199254740993.0", int) == 9007199254740993
    for bad in [("i", "14.5", int), ("i", 14.5, int), ("i", "abc", int), ("i", "inf", int),
                ("f", "", float),
                ("f", "x", float), ("f", "nan", float)]:
        with pytest.raises(NumericSettingNotSavable, match=bad[0]):
            numeric_setting_for_save(defs, *bad)


# ------------------------------------------------------------------- rendering with a fake ui
class _El:
    """A permissive NiceGUI stand-in: holds ``value``, chains any method, is a context."""

    def __init__(self, kind="", *args, **kwargs):
        self.kind, self.args, self.kwargs = kind, args, kwargs
        self.value = kwargs.get("value")

    def __getattr__(self, name):
        return lambda *a, **k: self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeUI:
    def __init__(self):
        self.notes, self.labels = [], []

    def notify(self, msg, **kw):
        self.notes.append((msg, kw))

    def label(self, text="", *a, **k):
        self.labels.append(text)
        return _El("label")

    def __getattr__(self, name):
        return lambda *a, **k: _El(name, *a, **k)


@pytest.fixture
def fake_ui(monkeypatch):
    f = _FakeUI()
    monkeypatch.setattr(settings_page, "ui", f)
    return f


def test_expert_form_bool_without_a_default_is_one_field_not_a_truncated_form(tab, instance, fake_ui, monkeypatch):
    """Through the real loader: a bool with no stored value and no declared default cannot be
    shown. It is left indeterminate with a visible error, the field after it still renders,
    and Save is refused before any write. (An unreadable stored spelling cannot reach the
    form: the settings loader already reads it as False, loudly.)"""
    monkeypatch.setattr(MockExpert, "get_settings_definitions", classmethod(lambda cls: {
        "flag_a": {"type": "bool", "required": False, "description": "flag_a"},
        "after": {"type": "str", "required": False, "default": "x", "description": "after"},
    }))
    tab.expert_settings_container = _El()
    tab._render_expert_settings(instance)
    assert set(tab.expert_settings_inputs) == {"flag_a", "after"}, "form was truncated"
    assert tab.expert_settings_inputs["flag_a"].value is None
    assert any("flag_a" in m and kw.get("type") == "negative" for m, kw in fake_ui.notes)
    assert tab._expert_settings_load_error

    tab._save_expert(instance)
    assert "could not be loaded" in fake_ui.notes[-1][0]
    assert _no_rows(instance.id)


def test_expert_form_that_fails_outright_refuses_the_save(tab, instance, fake_ui, monkeypatch):
    real_defs = MockExpert.__dict__["get_settings_definitions"]

    def _boom(cls):
        raise RuntimeError("definitions exploded")
    monkeypatch.setattr(MockExpert, "get_settings_definitions", classmethod(_boom))
    tab.expert_settings_container = _El()
    tab._render_expert_settings(instance)
    assert tab._expert_settings_load_error
    assert any("Save is refused" in t for t in fake_ui.labels)
    monkeypatch.setattr(MockExpert, "get_settings_definitions", real_defs)  # only the flag refuses now
    tab.expert_settings_inputs = {}
    tab._save_expert(instance)
    assert "could not be loaded" in fake_ui.notes[-1][0]
    assert _no_rows(instance.id)


def _account_tab():
    from ba2_trade_platform.ui.pages.settings import AccountDefinitionsTab
    t = object.__new__(AccountDefinitionsTab)
    t.dynamic_settings_container = _El()
    return t


def test_account_form_shows_declared_defaults_and_an_indeterminate_bool(fake_ui):
    """New Alpaca account: paper_account has no default -> indeterminate (None), never a
    silent False (= live). New IBKR account: paper_account's declared default is True (the
    old form showed False)."""
    t = _account_tab()
    t._render_dynamic_settings("Alpaca", None)
    assert t.settings_inputs["paper_account"].value is None
    assert t.settings_inputs["margin_factor"].value == 1.8
    t._render_dynamic_settings("IBKR", None)
    assert t.settings_inputs["paper_account"].value is True
    assert t.settings_inputs["host"].value == "127.0.0.1"


def test_account_settings_that_cannot_be_read_refuse_the_save(fake_ui, monkeypatch):
    """A DB read failure used to return {} -> the form showed DEFAULTS and Save wrote them
    over the real settings."""
    from sqlmodel import select as real_select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import AccountSetting

    acc = create_account_definition(provider="Alpaca")
    account_cls = settings_page.providers["Alpaca"]
    iface = account_cls.__new__(account_cls)
    iface.id = acc.id
    iface.save_setting("paper_account", True)

    def _boom(*a, **k):
        raise RuntimeError("db unreadable")
    monkeypatch.setattr(settings_page, "select", _boom)
    t = _account_tab()
    t._render_dynamic_settings("Alpaca", acc)
    monkeypatch.setattr(settings_page, "select", real_select)
    assert t._account_settings_load_error
    assert any("db unreadable" in m and kw.get("type") == "negative" for m, kw in fake_ui.notes)

    t.type_select = SimpleNamespace(value="Alpaca")
    t.name_input = SimpleNamespace(value=acc.name)
    t.desc_input = SimpleNamespace(value="")
    t.settings_inputs["paper_account"].value = False
    t.save_account(acc)
    assert "could not be loaded" in fake_ui.notes[-1][0]
    with get_db() as s:
        rows = s.exec(real_select(AccountSetting).where(AccountSetting.account_id == acc.id)).all()
        assert [(r.key, r.value_json) for r in rows] == [("paper_account", "true")]


# ------------------------------------------------------------------- the no-edit round trip
def _rows(instance_id):
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import ExpertSetting
    with get_db() as s:
        rows = s.exec(select(ExpertSetting).where(ExpertSetting.instance_id == instance_id)).all()
        return sorted((r.key, r.value_str, r.value_float, repr(r.value_json)) for r in rows)


def test_every_dialog_setting_survives_a_no_edit_save_byte_identical(tab, instance, fake_ui, monkeypatch):
    """Every setting the loader and the expert-specific form carry, stored at a NON-default
    value; open, save without touching anything; the rows must not change."""
    expert = MockExpert(instance.id)
    defs = _defs()
    non_default = {
        "enable_buy": False, "enable_sell": True,
        "allow_automated_trade_opening": True, "allow_automated_trade_modification": True,
        "max_virtual_equity_per_instrument_percent": 12.5, "min_available_balance_pct": 7.25,
        "risk_per_trade_pct": 1.75, "atr_multiplier": 2.6, "atr_period": 21,
        "min_stop_loss_pct": 4.5, "sizing_mode": "risk_atr",
        "risk_manager_model": "NagaAI/grok-4.5", "dynamic_instrument_selection_model": "NagaAI/kimi-k3",
        "risk_manager_mode": "smart", "smart_risk_manager_user_instructions": "be careful",
        "smart_risk_manager_max_iterations": 7, "smart_risk_manager_analysis_window_hours": 48,
        "test_setting": "custom", "test_int_setting": 99,
    }
    for key, value in non_default.items():
        assert value != defs[key].get("default"), key
        expert.save_setting(key, value)
    before = _rows(instance.id)
    assert len(before) == len(non_default)

    tab.expert_settings_container = _El()
    tab._render_expert_settings(instance)
    tab._load_general_settings(instance)
    assert not tab._general_settings_load_error and not tab._expert_settings_load_error
    written = []
    real_save = MockExpert.save_setting

    def _spy(self, key, value, setting_type=None):
        written.append(key)
        return real_save(self, key, value, setting_type=setting_type)
    monkeypatch.setattr(MockExpert, "save_setting", _spy)
    tab._save_expert_settings(instance.id)
    assert set(non_default) <= set(written), "the save did not rewrite every setting"
    assert _rows(instance.id) == before


def test_a_stored_retired_setting_neither_breaks_the_dialog_nor_is_rewritten(tab, instance):
    """allow_hedging was retired by the netting rule (RETIRED_EXPERT_SETTINGS): its rows stay,
    undeclared. The loader resolves only DECLARED keys, so the row cannot raise the
    no-definition KeyError; the save leaves it exactly as it was."""
    from ba2_trade_platform.core.interfaces.MarketExpertInterface import RETIRED_EXPERT_SETTINGS
    assert "allow_hedging" in RETIRED_EXPERT_SETTINGS
    assert "allow_hedging" not in _defs()
    MockExpert(instance.id).save_setting("allow_hedging", False, setting_type="bool")
    hedging_row = [r for r in _rows(instance.id) if r[0] == "allow_hedging"]
    tab._load_general_settings(instance)
    assert not tab._general_settings_load_error
    tab._save_expert_settings(instance.id)
    assert [r for r in _rows(instance.id) if r[0] == "allow_hedging"] == hedging_row


# ------------------------------------------------------------------- GA-deployed int genes
def test_a_whole_float_in_an_int_field_displays_as_an_int_and_saves(tab, instance):
    """GA deploys store int genes as floats (atr_period = 21.0). The field shows "21", and the
    save writes 21 -- it used to show "21.0", which the int parser then refused."""
    MockExpert(instance.id).save_setting("atr_period", 21.0, setting_type="float")
    tab._load_general_settings(instance)
    assert tab.atr_period_input.value == "21"
    tab._save_expert_settings(instance.id)
    assert MockExpert(instance.id).settings["atr_period"] == 21


def test_an_int_field_showing_a_whole_decimal_still_saves(tab, instance):
    tab._load_general_settings(instance)
    tab.atr_period_input.value = "14.0"
    tab._save_expert_settings(instance.id)
    assert MockExpert(instance.id).settings["atr_period"] == 14


# ------------------------------------------------------------------- expert-specific numerics
def test_expert_numeric_fields_show_stored_else_declared_else_empty(tab, instance, fake_ui, monkeypatch):
    """No invented 0: a numeric with no stored value and no declared default shows EMPTY, and
    the save refuses it by name; a stored 0 shows 0; a whole float in an int field shows as
    an int."""
    monkeypatch.setattr(MockExpert, "get_settings_definitions", classmethod(lambda cls: {
        "no_default_f": {"type": "float", "required": False},
        "zero_i": {"type": "int", "required": False, "default": 5},
        "gene_i": {"type": "int", "required": False, "default": 5},
        "defaulted_f": {"type": "float", "required": False, "default": 2.5},
    }))
    expert = MockExpert(instance.id)
    expert.save_setting("zero_i", 0)
    expert.save_setting("gene_i", 21.0, setting_type="float")
    rows_before = _rows(instance.id)
    tab.expert_settings_container = _El()
    tab._render_expert_settings(instance)
    shown = {k: inp.value for k, inp in tab.expert_settings_inputs.items()}
    assert shown == {"no_default_f": "", "zero_i": "0", "gene_i": "21", "defaulted_f": "2.5"}

    tab._load_general_settings(instance)
    tab._save_expert(instance)
    assert "no_default_f" in fake_ui.notes[-1][0] and fake_ui.notes[-1][1].get("type") == "negative"
    assert _rows(instance.id) == rows_before, "a refused save must write nothing"


# ------------------------------------------------------------------- screener numerics
def test_screener_numeric_fields_show_stored_else_declared_default(tab, instance, fake_ui):
    defs = _defs()
    MockExpert(instance.id).save_setting("screener_max_stocks", 0)
    tab.instruments_content_container = _El()
    tab._render_screener_settings(instance)
    inputs = tab.screener_settings_inputs
    assert inputs["screener_max_stocks"].value == "0", "a stored 0 must not become the default"
    for key, inp in inputs.items():
        meta = defs[key]
        if meta["type"] in ("int", "float") and key != "screener_max_stocks":
            default = meta.get("default")
            expected = "" if default is None else settings_page.display_text(defs, key, default)
            assert inp.value == expected, key


# ------------------------------------------------------------------- instrument selection read
def test_a_failed_instrument_selection_read_refuses_the_save(tab, instance, monkeypatch):
    """The select used to fall back to 'static' at DEBUG level, and the save wrote it back:
    a no-edit save switched a screener expert to static."""
    notes = []
    monkeypatch.setattr(settings_page.ui, "notify", lambda msg, **kw: notes.append((msg, kw)))
    tab._load_general_settings(instance)
    tab._instrument_selection_load_error = "RuntimeError: unreadable"
    tab.expert_settings_inputs = {}
    tab._save_expert(instance)
    assert "could not be loaded" in notes[-1][0] and notes[-1][1].get("type") == "negative"
    assert _no_rows(instance.id)


# ------------------------------------------------------------------- account numerics
@pytest.fixture
def int_account(monkeypatch):
    """An account provider with int settings (IBKR's shape: a defaulted port, a client id).
    IBKRAccount itself is abstract, so it cannot be built with __new__ here."""
    alpaca = settings_page.providers["Alpaca"]

    class IntAccount(alpaca):
        @classmethod
        def get_settings_definitions(cls):
            return {**alpaca.get_settings_definitions(),
                    "port": {"type": "int", "required": True, "default": 7497, "description": "port"},
                    "client_id": {"type": "int", "required": True, "description": "client id"}}

    monkeypatch.setitem(settings_page.providers, "IntAccount", IntAccount)
    return IntAccount


def test_account_int_field_shows_a_stored_zero(fake_ui, int_account):
    """`value or ""` blanked a stored 0 (IBKR client_id 0 is a real id)."""
    acc = create_account_definition(provider="IntAccount")
    iface = int_account.__new__(int_account)
    iface.id = acc.id
    iface.save_setting("client_id", 0)
    t = _account_tab()
    t._render_dynamic_settings("IntAccount", acc)
    assert t.settings_inputs["client_id"].value == "0"
    assert t.settings_inputs["port"].value == "7497"      # declared default, shown as an int
    t._render_dynamic_settings("IntAccount", None)
    assert t.settings_inputs["client_id"].value == ""     # no value, no default: empty


def _int_account_form(**overrides):
    from ba2_trade_platform.ui.pages.settings import AccountDefinitionsTab
    t = object.__new__(AccountDefinitionsTab)
    t.type_select = SimpleNamespace(value="IntAccount")
    t.name_input = SimpleNamespace(value="acc")
    t.desc_input = SimpleNamespace(value="")
    values = {"api_key": "k", "api_secret": "s", "paper_account": True,
              "port": "7497", "client_id": "3"}
    values.update(overrides)
    t.settings_inputs = {k: SimpleNamespace(value=v) for k, v in values.items()}
    return t


@pytest.mark.parametrize("field, raw", [("port", "abc"), ("port", "74.5"), ("client_id", "")])
def test_account_save_refuses_a_bad_int_by_name_before_any_write(fake_ui, int_account, field, raw):
    """save_setting's int("abc") used to raise halfway through the loop -- for a new account,
    after the AccountDefinition row and earlier settings were written."""
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import AccountDefinition
    _int_account_form(**{field: raw}).save_account(None)
    assert field in fake_ui.notes[-1][0] and fake_ui.notes[-1][1].get("type") == "negative"
    with get_db() as s:
        assert not s.exec(select(AccountDefinition)).all(), "a refused save must write nothing"


def test_account_numeric_values_are_resolved_before_the_write(fake_ui, int_account, monkeypatch):
    """A cleared int saves the DECLARED default; a typed one is written as an int."""
    written = {}
    monkeypatch.setattr(int_account, "save_setting",
                        lambda self, key, value, setting_type=None: written.__setitem__(key, value))
    monkeypatch.setattr(settings_page, "get_account_instance_from_id", lambda *a, **k: None)
    _int_account_form(port="", client_id="0").save_account(None)
    assert written["port"] == 7497
    assert written["client_id"] == 0 and isinstance(written["client_id"], int)


# =================================================================== review of 57091294
# A NO-EDIT SAVE IS A BYTE-FOR-BYTE NO-OP. A control filled from a DECLARED default (no row, or
# a row whose value columns are all NULL) is not written back unless the operator edited it:
# a missing row stays missing and a NULL row stays NULL. Prod experts 12/13 carry NULL
# risk_manager_mode / risk_manager_model rows; 7-11 have no risk_manager_mode /
# min_available_balance_pct / smart_risk_manager_max_iterations rows at all.
def _null_row(instance_id, key):
    """A row with every value column NULL/empty, as prod holds for experts 12/13."""
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import ExpertSetting
    add_instance(ExpertSetting(instance_id=instance_id, key=key, value_str=None, value_float=None))


def _instrument_select(value="static"):
    el = _El("select", value=value)
    el.options = ["static", "dynamic", "screener"]
    return el


NULL_KEYS = ["risk_manager_mode", "risk_manager_model", "smart_risk_manager_max_iterations",
             "atr_multiplier", "enable_sell", "instrument_selection_method", "test_setting"]


def test_a_no_edit_save_leaves_NULL_and_missing_rows_exactly_as_they_were(tab, instance, fake_ui):
    expert = MockExpert(instance.id)
    for key in NULL_KEYS:
        _null_row(instance.id, key)
    expert.save_setting("atr_period", 21)                       # one real stored value
    before = _rows(instance.id)

    tab.instrument_selection_method_select = _instrument_select()
    tab.expert_settings_container = _El()
    tab._render_expert_settings(instance)
    tab._load_general_settings(instance)
    tab._load_instrument_selection_method(instance)
    assert not tab._general_settings_load_error and not tab._expert_settings_load_error
    tab._save_expert_settings(instance.id)
    assert _rows(instance.id) == before


def test_an_edited_default_populated_field_IS_written(tab, instance, fake_ui):
    _null_row(instance.id, "risk_manager_mode")                 # NULL row
    tab.expert_settings_container = _El()
    tab._render_expert_settings(instance)
    tab._load_general_settings(instance)
    tab.risk_manager_mode_select.value = "smart"               # NULL row, edited
    tab.atr_period_input.value = "30"                          # missing row, edited
    tab.enable_sell_checkbox.value = not tab.enable_sell_checkbox.value
    tab.expert_settings_inputs["test_int_setting"].value = "77"
    tab._save_expert_settings(instance.id)
    stored = MockExpert(instance.id).settings
    assert stored["risk_manager_mode"] == "smart"
    assert stored["atr_period"] == 30
    assert stored["enable_sell"] is (not _defs()["enable_sell"]["default"])
    assert stored["test_int_setting"] == 77
    written = {r[0] for r in _rows(instance.id)}
    assert written == {"risk_manager_mode", "atr_period", "enable_sell", "test_int_setting"}


def test_a_new_expert_still_writes_what_its_form_shows(tab, instance, fake_ui):
    """Only an EDIT skips default-populated controls; a new expert's form is its definition."""
    defs = _defs()
    for attr in BOOL_CONTROLS.values():
        getattr(tab, attr).value = False
    for key, attr in VALUE_CONTROLS.items():      # what the constructor shows: the defaults
        getattr(tab, attr).value = settings_page.display_text(defs, key, defs[key]["default"])
    tab.expert_settings_container = _El()
    tab._render_expert_settings(None)
    tab._save_expert_settings(instance.id)
    stored = MockExpert(instance.id).settings
    assert stored["enable_buy"] is False
    assert stored["test_int_setting"] == 24


# ------------------------------------------------------------------- instrument_selection_method
def test_an_unset_instrument_selection_method_shows_unset_and_stays_unset(tab, instance, fake_ui):
    """FactorRanker reads an unset method as "defer to universe_source"; an explicit 'static'
    forces a static universe. The select must be able to SHOW unset, and a no-edit save
    must leave the NULL row NULL."""
    _null_row(instance.id, "instrument_selection_method")
    before = _rows(instance.id)
    tab.instrument_selection_method_select = _instrument_select()
    tab._load_general_settings(instance)
    tab._load_instrument_selection_method(instance)
    tab._update_instrument_selection_options()
    select = tab.instrument_selection_method_select
    assert select.value == settings_page.INSTRUMENT_METHOD_UNSET
    assert settings_page.INSTRUMENT_METHOD_UNSET in select.options
    assert "unset" in select.options[settings_page.INSTRUMENT_METHOD_UNSET]
    tab._save_expert_settings(instance.id)
    assert _rows(instance.id) == before


def test_choosing_a_method_over_unset_is_written(tab, instance, fake_ui):
    tab.instrument_selection_method_select = _instrument_select()
    tab._load_general_settings(instance)
    tab._load_instrument_selection_method(instance)            # no row -> unset
    tab._update_instrument_selection_options()
    tab.instrument_selection_method_select.value = "screener"
    tab._save_expert_settings(instance.id)
    assert MockExpert(instance.id).settings["instrument_selection_method"] == "screener"


def test_a_stored_method_hides_the_unset_option(tab, instance, fake_ui):
    MockExpert(instance.id).save_setting("instrument_selection_method", "dynamic")
    tab.instrument_selection_method_select = _instrument_select()
    tab._load_instrument_selection_method(instance)
    tab._update_instrument_selection_options()
    assert tab.instrument_selection_method_select.value == "dynamic"
    assert settings_page.INSTRUMENT_METHOD_UNSET not in tab.instrument_selection_method_select.options


def test_show_dialog_instrument_method_read_failure_refuses_the_save(tab, instance, fake_ui, monkeypatch):
    """The REAL show_dialog catch block: a failed read of the method is shown, the Save
    button is disabled, and _save_expert refuses before any write."""
    real_resolve = settings_page.resolve_setting_for_display

    def _resolve(defs, stored, key):
        if key == "instrument_selection_method":
            raise RuntimeError("method unreadable")
        return real_resolve(defs, stored, key)
    monkeypatch.setattr(settings_page, "resolve_setting_for_display", _resolve)
    tab.dialog = _El()
    tab.show_dialog(instance)
    assert tab._instrument_selection_load_error
    assert "method unreadable" in tab._instrument_selection_load_error
    assert any("method unreadable" in m and kw.get("type") == "negative" for m, kw in fake_ui.notes)
    assert tab._save_button_disabled

    tab._save_expert(instance)
    assert "could not be loaded" in fake_ui.notes[-1][0]
    assert _no_rows(instance.id)


# ------------------------------------------------------------------- the Save button
def test_the_save_button_follows_a_re_render(tab, instance, fake_ui, monkeypatch):
    """Decided once, it stayed disabled (or enabled) whatever the expert type became."""
    tab._save_button = _El("button")
    real_defs = MockExpert.__dict__["get_settings_definitions"]

    def _boom(cls):
        raise RuntimeError("definitions exploded")
    monkeypatch.setattr(MockExpert, "get_settings_definitions", classmethod(_boom))
    tab.expert_settings_container = _El()
    tab._render_expert_settings(instance)
    assert tab._save_button_disabled
    monkeypatch.setattr(MockExpert, "get_settings_definitions", real_defs)
    tab._render_expert_settings(instance)
    assert not tab._save_button_disabled


# ------------------------------------------------------------------- accounts
def _account_rows(account_id):
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import AccountSetting
    with get_db() as s:
        rows = s.exec(select(AccountSetting).where(AccountSetting.account_id == account_id)).all()
        return sorted((r.key, r.value_str, r.value_float, repr(r.value_json)) for r in rows)


def _stored_alpaca_account(name="acc"):
    acc = create_account_definition(provider="Alpaca", name=name)
    cls = settings_page.providers["Alpaca"]
    iface = cls.__new__(cls)
    iface.id = acc.id
    for key, value in (("api_key", "k"), ("api_secret", "s"), ("paper_account", True)):
        iface.save_setting(key, value)
    return acc


def _account_edit_form(provider, acc):
    t = _account_tab()
    t.dialog = _El()
    t._update_table_rows = lambda: None
    t._render_dynamic_settings(provider, acc)
    t.type_select = SimpleNamespace(value=provider)
    t.name_input = SimpleNamespace(value=acc.name)
    t.desc_input = SimpleNamespace(value=acc.description or "")
    return t


def test_an_account_no_edit_save_leaves_missing_rows_missing(fake_ui, monkeypatch):
    monkeypatch.setattr(settings_page, "get_account_instance_from_id", lambda *a, **k: None)
    acc = _stored_alpaca_account()
    before = _account_rows(acc.id)
    _account_edit_form("Alpaca", acc).save_account(acc)
    assert not any(kw.get("type") == "negative" for _, kw in fake_ui.notes)
    assert _account_rows(acc.id) == before     # margin_factor, data_feed... still missing


def test_an_account_edited_default_field_is_written(fake_ui, monkeypatch):
    monkeypatch.setattr(settings_page, "get_account_instance_from_id", lambda *a, **k: None)
    acc = _stored_alpaca_account()
    t = _account_edit_form("Alpaca", acc)
    t.settings_inputs["margin_factor"].value = 1.5
    t.save_account(acc)
    assert [r for r in _account_rows(acc.id) if r[0] == "margin_factor"][0][2] == 1.5
    assert {r[0] for r in _account_rows(acc.id)} == {"api_key", "api_secret", "paper_account",
                                                     "margin_factor"}


def _new_alpaca_form(**values):
    from ba2_trade_platform.ui.pages.settings import AccountDefinitionsTab
    t = object.__new__(AccountDefinitionsTab)
    t.dialog = _El()
    t._update_table_rows = lambda: None
    t.type_select = SimpleNamespace(value=values.pop("_provider", "Alpaca"))
    t.name_input = SimpleNamespace(value="acc")
    t.desc_input = SimpleNamespace(value="")
    base = {"paper_account": True, "api_key": "k", "api_secret": "s"}
    base.update(values)
    t.settings_inputs = {k: SimpleNamespace(value=v) for k, v in base.items()}
    return t


def _no_accounts():
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import AccountDefinition, AccountSetting
    with get_db() as s:
        return not s.exec(select(AccountDefinition)).all() and not s.exec(select(AccountSetting)).all()


@pytest.mark.parametrize("key", ["api_key", "api_secret"])
def test_account_save_allows_an_empty_credential(fake_ui, key):
    """User decision (2026-09-26): an empty API key/secret is ALLOWED to stay empty -- no
    refusal for empty str settings, required or not."""
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import AccountDefinition
    _new_alpaca_form(**{key: ""}).save_account(None)
    assert not any(key in m and kw.get("type") == "negative" for m, kw in fake_ui.notes)
    with get_db() as s:
        assert len(s.exec(select(AccountDefinition)).all()) == 1


@pytest.fixture
def abstract_account(monkeypatch):
    import abc
    alpaca = settings_page.providers["Alpaca"]

    class HalfAccount(alpaca):
        @abc.abstractmethod
        def not_implemented_yet(self):
            ...

    monkeypatch.setitem(settings_page.providers, "HalfAccount", HalfAccount)
    return HalfAccount


def test_an_abstract_provider_is_refused_by_name_and_no_row_is_created(fake_ui, abstract_account):
    """IBKRAccount lacks 10 abstract methods: add_instance ran, then provider_cls.__new__
    raised, leaving an orphan AccountDefinition on every retry."""
    _new_alpaca_form(_provider="HalfAccount").save_account(None)
    msg, kw = fake_ui.notes[-1]
    assert "HalfAccount" in msg and "not_implemented_yet" in msg and kw.get("type") == "negative"
    assert _no_accounts()


def test_abstract_providers_are_not_offered(abstract_account):
    names, unavailable = settings_page.selectable_account_providers()
    assert "HalfAccount" not in names and "Alpaca" in names
    assert "not_implemented_yet" in unavailable["HalfAccount"]


def test_real_ibkr_is_reported_unavailable_while_it_is_abstract():
    import inspect
    names, unavailable = settings_page.selectable_account_providers()
    if inspect.isabstract(settings_page.providers["IBKR"]):
        assert "IBKR" not in names and "IBKR" in unavailable
    else:
        assert "IBKR" in names


def test_an_account_edit_whose_settings_fail_leaves_the_row_unchanged(fake_ui, monkeypatch):
    """update_instance(account) used to commit name/provider BEFORE any setting was written."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import AccountDefinition
    acc = _stored_alpaca_account(name="before")
    before = _account_rows(acc.id)
    t = _account_edit_form("Alpaca", acc)
    t.name_input.value = "after"
    t.settings_inputs["api_key"].value = "k2"
    cls = settings_page.providers["Alpaca"]

    def _fail(self, *a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(cls, "save_setting", _fail)
    t.save_account(acc)
    assert fake_ui.notes[-1][1].get("type") == "negative"
    assert get_instance(AccountDefinition, acc.id).name == "before"
    assert _account_rows(acc.id) == before


def test_a_new_account_whose_settings_fail_leaves_no_row(fake_ui, monkeypatch):
    cls = settings_page.providers["Alpaca"]

    real_save = cls.save_setting
    calls = []

    def _fail_second(self, key, value, setting_type=None):
        calls.append(key)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real_save(self, key, value, setting_type=setting_type)
    monkeypatch.setattr(cls, "save_setting", _fail_second)
    _new_alpaca_form().save_account(None)
    assert fake_ui.notes[-1][1].get("type") == "negative"
    assert _no_accounts()
