"""The market-condition profile is an EXPERT SETTING (plan Task 12).

Two refusals live here, both of which close a failure that is otherwise SILENT:

* :func:`parse_profile_setting` -- the one reader of the ``market_condition_profile`` setting
  string. Live, the backtest seam, the launcher and the deploy importer all go through it, so an
  unregistered or repeated profile name is the same ValueError everywhere instead of an empty
  tuple somewhere.
* :func:`assert_market_fields_served` -- a ruleset whose market leaf names a field NO listed
  profile serves. Nothing raises at save time without this: the leaf's reader is simply not
  built, and the gate reads ``no_context``/raises on its first live evaluation, which is a
  deployed strategy that never enters.
"""
from __future__ import annotations

import pytest

from ba2_common.core.market_condition_rules import (
    PROFILE_SETTING,
    assert_market_fields_served,
    parse_profile_setting,
)
from ba2_common.core.market_conditions import PROFILES


# --------------------------------------------------------------------------- parse
@pytest.mark.parametrize("raw", ["", None, "   ", ",", " , "])
def test_an_empty_setting_names_no_profile(raw):
    assert parse_profile_setting(raw) == ()


def test_a_single_name_and_a_comma_list_keep_their_order():
    assert parse_profile_setting("ohlcv-v1") == ("ohlcv-v1",)
    assert parse_profile_setting(" ta-structure-v1 , ohlcv-v1 ") == ("ta-structure-v1", "ohlcv-v1")


def test_every_registered_profile_parses():
    for name in PROFILES:
        assert parse_profile_setting(name) == (name,)


def test_an_unregistered_name_is_refused_naming_the_setting():
    with pytest.raises(ValueError) as e:
        parse_profile_setting("ohlcv-v9")
    assert PROFILE_SETTING in str(e.value) and "ohlcv-v9" in str(e.value)
    assert "ohlcv-v1" in str(e.value)          # the message says what IS registered


def test_none_is_not_a_profile_name_in_the_setting():
    """The LAUNCHER's CLI spells "gates off" as ``none``; the SETTING spells it as empty. A
    ``none`` that parsed to "no profiles" here would make ``market_condition_profile=none``
    look configured on a settings page while serving nothing."""
    with pytest.raises(ValueError, match="empty"):
        parse_profile_setting("none")


def test_a_repeated_name_is_refused():
    with pytest.raises(ValueError, match="repeat"):
        parse_profile_setting("ohlcv-v1,ohlcv-v1")


def test_a_non_string_setting_is_refused():
    with pytest.raises(ValueError, match=PROFILE_SETTING):
        parse_profile_setting(["ohlcv-v1"])


# --------------------------------------------------------------------------- served fields
def _rules(field, leaf_id="o_lc-market-adx"):
    return [{"id": "o_lc-entry", "conditions": {"all": [
        {"id": "o_lc-flat", "field": "has_no_position", "op": "=="},
        {"id": leaf_id, "field": field, "op": "<", "value": 25.0},
    ]}}]


def test_a_leaf_served_by_the_listed_profile_passes():
    assert_market_fields_served(_rules("underlying_adx_14"), ("ohlcv-v1",)) is None


def test_a_leaf_of_a_second_profile_needs_that_profile_listed():
    rules = _rules("structure_state", leaf_id="o_lc-market-state")
    assert_market_fields_served(rules, ("ohlcv-v1", "ta-structure-v1")) is None
    with pytest.raises(ValueError) as e:
        assert_market_fields_served(rules, ("ohlcv-v1",))
    msg = str(e.value)
    assert "o_lc-market-state" in msg and "structure_state" in msg
    assert PROFILE_SETTING in msg and "ohlcv-v1" in msg


def test_an_empty_setting_with_a_gated_ruleset_is_refused():
    with pytest.raises(ValueError) as e:
        assert_market_fields_served(_rules("underlying_adx_14"), ())
    msg = str(e.value)
    assert "o_lc-market-adx" in msg and PROFILE_SETTING in msg
    # The message must say the setting is EMPTY, not merely list an empty tuple.
    assert "empty" in msg


def test_a_ruleset_with_no_market_leaf_is_served_by_any_setting():
    plain = [{"id": "r", "conditions": {"all": [{"field": "has_no_position", "op": "=="}]}}]
    assert assert_market_fields_served(plain, ()) is None
    assert assert_market_fields_served(plain, ("ohlcv-v1",)) is None


def test_a_field_name_this_registry_does_not_know_is_unserved_not_ignored():
    """``STRICT_FIELD_NAMES`` carries every deployable name, registered here or not. A payload
    naming one this build's registry retired must be REFUSED, not treated as a plain condition."""
    from ba2_common.core.market_condition_rules import STRICT_FIELD_NAMES

    retired = sorted(STRICT_FIELD_NAMES - {f.name for p in PROFILES.values() for f in p.fields})
    if not retired:
        pytest.skip("every strict name is registered in this build")
    with pytest.raises(ValueError, match=retired[0]):
        assert_market_fields_served(_rules(retired[0]), tuple(PROFILES))


def test_the_pairs_form_carries_the_same_message():
    """The live save path collects ``(label, field)`` from persisted EventAction TRIGGERS (which
    key on ``event_type``), not from a condition tree -- so the refusal is shared at that level."""
    from ba2_common.core.market_condition_rules import assert_fields_served

    with pytest.raises(ValueError) as e:
        assert_fields_served([("cond_3", "underlying_adx_14")], (), where="ruleset 12")
    assert "ruleset 12" in str(e.value) and "cond_3" in str(e.value)
