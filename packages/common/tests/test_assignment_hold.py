"""The member-scoped assigned-stock hold decider (plan 2026-09-25 option-stage2-build, E2).

``ba2_common.core.assignment_hold`` decides, per assignment, whether the delivered stock is
held (the wheel member) or liquidated (every other member), from the assigned position's
entry tag. With no member tag set it must answer exactly what the run-level flag always did.
"""
import pytest

from ba2_common.core.assignment_hold import (
    HOLD_ASSIGNED_STOCK_TAGS_KEY,
    AssignedStockHoldPolicy,
    UntaggedAssignmentRefused,
    assigned_stock_entry_data,
    holds_assigned_stock,
    resolve_assigned_stock_hold_policy,
)
from ba2_common.core.entry_tag import ORDER_ENTRY_TAG_KEY

WHEEL, CSP = 7, 3


# --------------------------------------------------------------------------------------- #
# no tag set: the run-level flag, byte-identical
# --------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("cfg, expected", [
    ({}, False),
    ({"hold_assigned_stock": False}, False),
    ({"hold_assigned_stock": True}, True),
    ({"hold_assigned_stock": None}, False),
    ({"hold_assigned_stock": True, HOLD_ASSIGNED_STOCK_TAGS_KEY: None}, True),
])
def test_without_a_tag_set_the_run_level_flag_decides_for_every_position(cfg, expected):
    policy = resolve_assigned_stock_hold_policy(cfg)
    assert policy == AssignedStockHoldPolicy(run_level=expected)
    # ...whatever the position's tag, and an untagged position is NOT refused
    for tag in (None, WHEEL, CSP):
        assert holds_assigned_stock(policy, tag, where="x") is expected


# --------------------------------------------------------------------------------------- #
# the member-scoped form
# --------------------------------------------------------------------------------------- #
def test_each_assignment_follows_its_own_members_policy():
    policy = resolve_assigned_stock_hold_policy({HOLD_ASSIGNED_STOCK_TAGS_KEY: [WHEEL]})
    assert policy.member_tags == frozenset({WHEEL})
    assert holds_assigned_stock(policy, WHEEL, where="wheel put") is True
    assert holds_assigned_stock(policy, CSP, where="csp put") is False


def test_an_untagged_assignment_in_a_member_scoped_run_is_refused_loudly():
    policy = resolve_assigned_stock_hold_policy({HOLD_ASSIGNED_STOCK_TAGS_KEY: [WHEEL]})
    with pytest.raises(UntaggedAssignmentRefused, match="AAPL put.*no entry tag"):
        holds_assigned_stock(policy, None, where="AAPL put")


def test_tags_survive_a_json_or_ga_round_trip():
    policy = resolve_assigned_stock_hold_policy({HOLD_ASSIGNED_STOCK_TAGS_KEY: (7.0, 12)})
    assert policy.member_tags == frozenset({7, 12})


@pytest.mark.parametrize("raw", [[], "7", 7, {"7": 1}, [0], [-1], [True], [1.5], ["7"],
                                 [None], [7, 7]])
def test_a_malformed_tag_set_is_refused_by_name(raw):
    with pytest.raises(ValueError, match="hold_assigned_stock_tags|entry_tag"):
        resolve_assigned_stock_hold_policy({HOLD_ASSIGNED_STOCK_TAGS_KEY: raw})


def test_the_run_level_flag_and_a_tag_set_together_are_refused():
    with pytest.raises(ValueError, match="state one"):
        resolve_assigned_stock_hold_policy(
            {"hold_assigned_stock": True, HOLD_ASSIGNED_STOCK_TAGS_KEY: [WHEEL]})


def test_a_false_run_level_flag_beside_a_tag_set_is_not_a_conflict():
    """The launcher writes ``hold_assigned_stock`` on every run; False states nothing."""
    policy = resolve_assigned_stock_hold_policy(
        {"hold_assigned_stock": False, HOLD_ASSIGNED_STOCK_TAGS_KEY: [WHEEL]})
    assert policy.member_tags == frozenset({WHEEL})


# --------------------------------------------------------------------------------------- #
# the delivered stock inherits the tag
# --------------------------------------------------------------------------------------- #
def test_the_delivered_stock_order_carries_the_assigned_positions_tag():
    assert assigned_stock_entry_data(WHEEL) == {ORDER_ENTRY_TAG_KEY: WHEEL}


def test_an_untagged_position_delivers_untagged_stock():
    assert assigned_stock_entry_data(None) is None


@pytest.mark.parametrize("bad", [0, -2, True, "7", 1.5])
def test_a_corrupt_tag_is_never_copied_onto_the_stock(bad):
    with pytest.raises(ValueError, match="entry_tag"):
        assigned_stock_entry_data(bad)
