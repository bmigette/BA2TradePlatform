"""Shared entry-tag lookups used by the structure_share condition (plan 2026-11-06, Task 1)."""
from types import SimpleNamespace

import pytest

import ba2_common.core.entry_order as eo
from ba2_common.core.entry_tag import entry_tag_of_actions


def test_transaction_entry_tag_reads_the_entry_orders_tag(monkeypatch):
    monkeypatch.setattr(eo, "entry_order_for_transaction",
                        lambda tid, session=None: SimpleNamespace(id=1, data={"entry_tag": 14}))
    assert eo.transaction_entry_tag(5) == 14


@pytest.mark.parametrize("order", [None, SimpleNamespace(id=1, data=None),
                                   SimpleNamespace(id=1, data={})])
def test_transaction_entry_tag_is_none_for_an_untagged_or_missing_entry(monkeypatch, order):
    monkeypatch.setattr(eo, "entry_order_for_transaction", lambda tid, session=None: order)
    assert eo.transaction_entry_tag(5) is None


def test_a_corrupt_stored_tag_propagates(monkeypatch):
    monkeypatch.setattr(eo, "entry_order_for_transaction",
                        lambda tid, session=None: SimpleNamespace(id=1, data={"entry_tag": "x"}))
    with pytest.raises(ValueError, match="entry_tag"):
        eo.transaction_entry_tag(5)


def test_tag_of_actions_reads_the_entry_action_config():
    actions = {"a0": {"action_type": "buy_call", "entry_tag": 19, "option_dte_min": 80}}
    assert entry_tag_of_actions(actions) == 19


@pytest.mark.parametrize("actions", [None, {}, {"a0": {"action_type": "buy_call"}},
                                     {"a0": "not-a-dict"}])
def test_tag_of_actions_is_none_without_a_tag(actions):
    assert entry_tag_of_actions(actions) is None


def test_two_different_tags_on_one_rule_are_refused():
    actions = {"a0": {"action_type": "buy_call", "entry_tag": 19},
               "a1": {"action_type": "buy_put", "entry_tag": 20}}
    with pytest.raises(ValueError, match="disagree"):
        entry_tag_of_actions(actions)


def test_the_same_tag_twice_is_one_tag():
    actions = {"a0": {"action_type": "buy_call", "entry_tag": 19},
               "a1": {"action_type": "buy_put", "entry_tag": 19}}
    assert entry_tag_of_actions(actions) == 19
