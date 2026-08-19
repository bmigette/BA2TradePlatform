"""Every SYMBOL360-wired expert's EXPORT_RELEVANT_SETTINGS allowlist must be a
genuine subset of that expert's OWN get_settings_definitions() keys -- a typo'd
key here doesn't crash anything (the UI would just silently omit it from the
settings expander), so nothing else catches it. This test exists specifically
to catch that class of silent mistake."""
from ba2_experts.DeterministicScorer import DeterministicScorer
from ba2_experts.FactorRanker import FactorRanker
from ba2_experts.FinnHubRating import FinnHubRating
from ba2_experts.FMPEarningsDrift import FMPEarningsDrift
from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
from ba2_experts.FMPRating import FMPRating

WIRED_EXPERTS = (
    DeterministicScorer, FactorRanker, FinnHubRating,
    FMPEarningsDrift, FMPInsiderClusterBuy, FMPRating,
)


def test_every_wired_expert_declares_a_curated_allowlist():
    """A future expert wired without curating EXPORT_RELEVANT_SETTINGS falls
    back to ALL of its own settings, which may be fine -- but this pins the
    current expectation that all six already-wired experts have consciously
    curated theirs (per the SYMBOL360 settings-panel clutter fix), so a
    regression (someone deletes the attribute) is caught."""
    for cls in WIRED_EXPERTS:
        assert cls.EXPORT_RELEVANT_SETTINGS is not None, (
            f"{cls.__name__} lost its curated EXPORT_RELEVANT_SETTINGS "
            f"(falls back to ALL own settings, likely re-introducing clutter)")


def test_every_relevant_setting_is_a_real_own_setting_key():
    """Every key in EXPORT_RELEVANT_SETTINGS must exist in the expert's OWN
    get_settings_definitions() (never a base-class builtin key, which
    get_settings_definitions() never includes) -- catches typos and stale
    entries left behind after a setting is renamed/removed."""
    for cls in WIRED_EXPERTS:
        own_keys = set(cls.get_settings_definitions().keys())
        relevant = set(cls.export_relevant_settings())
        missing = relevant - own_keys
        assert not missing, (
            f"{cls.__name__}.EXPORT_RELEVANT_SETTINGS references key(s) not in "
            f"its own get_settings_definitions(): {missing}")


def test_no_relevant_setting_is_dict_or_list_typed():
    """A dict/list-valued setting would be silently dropped by the UI's own
    filter anyway (universe configs, schedules, ...) -- if one ever ends up
    in an allowlist it's a wasted/misleading entry, not a crash, so nothing
    else would flag it."""
    for cls in WIRED_EXPERTS:
        defs = cls.get_settings_definitions()
        for key in cls.export_relevant_settings():
            default = defs[key].get("default")
            assert not isinstance(default, (dict, list)), (
                f"{cls.__name__}.EXPORT_RELEVANT_SETTINGS includes '{key}', "
                f"whose default is a {type(default).__name__} -- the UI "
                f"settings expander would silently drop it anyway")
