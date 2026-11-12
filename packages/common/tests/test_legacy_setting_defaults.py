"""A stored backtest config that predates a default change resolves the value it RAN with.

DeterministicScorer's yc_scale definition default moved 0.005 -> 0.5 on 2026-11-09. No stored
optimization block or row states yc_scale (the launcher's DS spec carries only sizing_mode), so
every result before the change ran 0.005 by default -- and would silently re-run / deploy at 0.5.
"""
import pytest

from ba2_common.core.legacy_setting_defaults import (
    LEGACY_SETTING_DEFAULTS, materialise_current_defaults, pin_legacy_setting_defaults,
)


def _ds_definitions(name):
    from ba2_experts import get_expert_class
    return get_expert_class(name).get_settings_definitions()


def test_the_table_records_the_ds_change():
    assert LEGACY_SETTING_DEFAULTS["DeterministicScorer"] == {"yc_scale": 0.005}


@pytest.mark.parametrize("expert,keys", sorted(LEGACY_SETTING_DEFAULTS.items()))
def test_every_entry_is_a_real_setting_whose_default_really_changed(expert, keys):
    defs = _ds_definitions(expert)
    for key, legacy in keys.items():
        assert key in defs, f"{expert}.{key} is not a declared setting"
        assert defs[key]["default"] != legacy, (
            f"{expert}.{key}: the current default equals the legacy value -- the entry pins nothing")


def test_a_legacy_config_without_the_key_resolves_the_old_value():
    stored = {"sizing_mode": "risk_atr", "macro_mode": "gate"}
    out = pin_legacy_setting_defaults("DeterministicScorer", stored)
    assert out == {"sizing_mode": "risk_atr", "macro_mode": "gate", "yc_scale": 0.005}
    assert stored == {"sizing_mode": "risk_atr", "macro_mode": "gate"}   # not mutated


def test_an_explicit_value_always_wins():
    for v in (0.5, 0.005, 0.3):
        assert pin_legacy_setting_defaults("DeterministicScorer", {"yc_scale": v})["yc_scale"] == v


def test_other_experts_are_untouched():
    s = {"top_n": 5}
    assert pin_legacy_setting_defaults("FactorRanker", s) == s
    assert pin_legacy_setting_defaults("FactorRanker", s) is not s


def test_a_new_job_states_the_current_default_so_it_is_never_read_as_legacy():
    new = materialise_current_defaults("DeterministicScorer", {"sizing_mode": "risk_atr"},
                                       _ds_definitions)
    assert new == {"sizing_mode": "risk_atr", "yc_scale": 0.5}
    # ...and that stated value survives the legacy pin (an explicit value wins)
    assert pin_legacy_setting_defaults("DeterministicScorer", new)["yc_scale"] == 0.5


def test_materialise_keeps_an_explicit_value_and_skips_other_experts():
    assert materialise_current_defaults("DeterministicScorer", {"yc_scale": 0.005},
                                        _ds_definitions) == {"yc_scale": 0.005}

    def _never(name):
        raise AssertionError("an expert without a changed default needs no definitions")
    assert materialise_current_defaults("FactorRanker", {"top_n": 5}, _never) == {"top_n": 5}


def test_materialise_refuses_definitions_without_the_key():
    with pytest.raises(KeyError):
        materialise_current_defaults("DeterministicScorer", {}, lambda name: {})
