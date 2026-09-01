"""``types.get_strike_method_action_values`` must equal what the builders actually do.

Only ELEVEN of the nineteen ``_OptionEntryAction`` subclasses pass ``method=self.strike_method``
to the selector; the other eight hard-code ``method="percent_otm"`` and leave
``self.strike_method`` a dead attribute (OPT-S2). Anything that OFFERS a strike-method choice
-- the rule editor, or the GA's strike-method gene -- must offer it for exactly those eleven, or
it hands out a knob that silently does nothing (a 0.30 "delta" becomes a strike 0.30 PERCENT
OTM, i.e. at the money).

A hand-maintained list cannot be trusted on its own: the whole point of this registry is that
it will lag when a builder is fixed. So this file derives the set from the ACTION CLASSES' OWN
SOURCE and requires the two to agree.
"""
import inspect
import re

import pytest

from ba2_common.core import TradeActions
from ba2_common.core.types import (
    ExpertActionType,
    get_option_action_values,
    get_strike_method_action_values,
    honours_strike_method,
)

_CLOSE = ExpertActionType.CLOSE_OPTION.value


def _entry_action_classes():
    """Every concrete option ENTRY action class, keyed by its action_type value."""
    out = {}
    for _name, obj in vars(TradeActions).items():
        if not inspect.isclass(obj) or not hasattr(obj, "_action_type_value"):
            continue
        try:
            src = inspect.getsource(obj._action_type_value)
        except (OSError, TypeError):
            continue
        m = re.search(r"ExpertActionType\.(\w+)\.value", src)
        if not m:
            continue
        value = getattr(ExpertActionType, m.group(1)).value
        if value == _CLOSE:
            continue
        out[value] = obj
    return out


def _source_honours(cls) -> bool:
    """True when the action's OWN code passes ``self.strike_method`` to a selector.

    WALKS THE MRO, stopping at ``_OptionEntryAction`` (2026-09-01). Reading only
    ``inspect.getsource(cls)`` assumed one class per builder, which stopped being true when
    the two 1x2 backspreads landed: they share ``_BackspreadAction``, whose ``_build_and_submit``
    holds the single ``select_vertical_spread`` call both of them make, so the concrete classes'
    own bodies carry no selector call at all and the derivation silently reported them as
    NOT honouring a method they demonstrably follow (the sibling
    ``test_option_strike_method_honoured.py``, which RUNS each builder and watches the
    selector, disagreed -- which is the whole point of having both).

    THE STOP IS LOAD-BEARING. ``_OptionEntryAction`` itself contains the string, in
    ``_pick_refusal_message``'s ``describe_pick_failure`` call, so walking all the way up
    would make every entry action "honour" it and turn this guard into a tautology --
    ``test_the_split_is_non_trivial_in_both_directions`` is what would catch that.
    """
    for klass in cls.__mro__:
        if klass is TradeActions._OptionEntryAction:
            break
        try:
            src = inspect.getsource(klass)
        except (OSError, TypeError):
            continue
        if "method=self.strike_method" in src:
            return True
    return False


def test_the_probe_finds_every_option_entry_action():
    """Guards the mutation that makes the derivation trivially agree by finding nothing."""
    found = set(_entry_action_classes())
    expected = set(get_option_action_values()) - {_CLOSE}
    assert found == expected, (
        f"the source probe missed {sorted(expected - found)} and invented "
        f"{sorted(found - expected)}"
    )


def test_the_registry_matches_what_the_builders_actually_read():
    derived = {v for v, cls in _entry_action_classes().items() if _source_honours(cls)}
    declared = set(get_strike_method_action_values())
    assert derived == declared, (
        f"get_strike_method_action_values() is out of date. Builders that DO read "
        f"strike_method but are not declared: {sorted(derived - declared)}. Declared but do "
        f"NOT read it (a knob that silently does nothing): {sorted(declared - derived)}"
    )


def test_the_split_is_non_trivial_in_both_directions():
    """Both halves must be non-empty, or the registry is a tautology."""
    all_entries = set(_entry_action_classes())
    declared = set(get_strike_method_action_values())
    assert declared, "no action honours strike_method -- the registry is empty"
    assert all_entries - declared, (
        "every action honours strike_method -- if OPT-S2 was fixed, delete this registry and "
        "the gene guard that consumes it rather than leaving a permanently-true filter"
    )


@pytest.mark.parametrize("value", sorted(get_strike_method_action_values()))
def test_declared_values_are_real_option_actions(value):
    assert value in get_option_action_values()
    assert honours_strike_method(value)


@pytest.mark.parametrize(
    "value",
    sorted(set(get_option_action_values()) - set(get_strike_method_action_values())),
)
def test_the_rest_report_false(value):
    assert not honours_strike_method(value)
