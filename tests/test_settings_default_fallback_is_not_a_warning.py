"""Falling back to a DECLARED default is normal operation, and must not log at WARNING.

At WARNING this one message was 7,592 of 32,562 lines in the prod log -- 23% of it, and more
than double every other warning and error combined. It buried the ones that mattered (a FRED
series missing from the cache, an account-configuration validation failure, FMP 429 backoffs).

It is not a fault: an expert only stores the keys its deploy actually chose. Live prod expert
10 (DeterministicScorer) stores 39 of the class's 130 -- the 20 GA-optimised genes plus its
operational settings -- and the other 91 are internal scorer knobs that were never in the GA
search space, so the backtest resolved them to these same defaults and live matches what was
validated.

What IS still loud: a key with no declared default at all. That one raises.
"""
import logging

import pytest

from ba2_common.core.interfaces.ExtendableSettingsInterface import ExtendableSettingsInterface


class _Thing(ExtendableSettingsInterface):
    """Minimal holder: one declared setting with a default, and no stored values."""

    id = 42

    @classmethod
    def get_settings_definitions(cls):
        return {"tw_mom": {"type": "float", "default": 0.45, "description": "a knob"}}

    @property
    def settings(self):
        return dict(self._stored)

    def __init__(self, stored=None):
        self._stored = dict(stored or {})


@pytest.fixture
def thing():
    return _Thing()


@pytest.fixture(autouse=True)
def _let_caplog_see_it():
    """``ba2_common`` sets propagate=False and owns its own handlers, so caplog's handler on
    the root logger never sees its records. Lift that for the duration, and put it back."""
    lg = logging.getLogger("ba2_common")
    was = lg.propagate
    lg.propagate = True
    try:
        yield
    finally:
        lg.propagate = was


def test_the_fallback_is_logged_at_debug_not_warning(thing, caplog):
    """THE DEFECT: 23% of the prod log, at a severity that said something was wrong."""
    with caplog.at_level(logging.DEBUG, logger="ba2_common"):
        assert thing.get_setting_with_interface_default("tw_mom") == 0.45

    hits = [r for r in caplog.records if "not configured" in r.getMessage()]
    assert hits, "the fallback should still be traceable, just quietly"
    assert all(r.levelno == logging.DEBUG for r in hits), \
        f"expected DEBUG, got {[logging.getLevelName(r.levelno) for r in hits]}"


def test_nothing_is_emitted_above_debug(thing, caplog):
    """The whole point: a normal run must not add to the WARNING count at all."""
    with caplog.at_level(logging.INFO, logger="ba2_common"):
        thing.get_setting_with_interface_default("tw_mom")
    assert [r.getMessage() for r in caplog.records
            if "not configured" in r.getMessage()] == []


def test_log_warning_false_still_silences_it_entirely(thing, caplog):
    with caplog.at_level(logging.DEBUG, logger="ba2_common"):
        thing.get_setting_with_interface_default("tw_mom", log_warning=False)
    assert [r for r in caplog.records if "not configured" in r.getMessage()] == []


def test_a_stored_value_wins_and_says_nothing(caplog):
    with caplog.at_level(logging.DEBUG, logger="ba2_common"):
        assert _Thing({"tw_mom": 0.9}).get_setting_with_interface_default("tw_mom") == 0.9
    assert [r for r in caplog.records if "not configured" in r.getMessage()] == []


def test_a_key_with_no_declared_default_still_raises(thing):
    """The case that IS a fault stays loud -- demoting the benign one must not soften it."""
    with pytest.raises(ValueError, match="not found in .* interface definitions"):
        thing.get_setting_with_interface_default("a_key_nobody_declared")
