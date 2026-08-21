"""Per-account scoping of the allocation deletes -- previously untested.

Instrument LABELS are global (they live on the ``instrument`` row) but the weights
and comments under them are per account, in ``portfolio_allocation_symbol``. Two
accounts routinely manage the same label, so every delete in the store carries an
``account_id`` predicate. Dropping either of those predicates left the whole suite
green while one user's edit silently erased another's: a Section-F mutation run
removed them both and 137/137 still passed.
"""
from ba2_trade_platform.core.portfolio_allocation_store import (
    get_managed_labels, get_symbol_rows, remove_managed_label,
    remove_symbols_from_label, set_managed_label, set_symbol_weight,
)
from ba2_trade_platform.core.utils import add_label_to_instruments, get_symbols_by_label
from tests.factories import create_account_definition


def _two_accounts():
    return (create_account_definition(name='Alice', provider='MockAccount').id,
            create_account_definition(name='Bob', provider='MockAccount').id)


def test_removing_a_symbol_keeps_the_other_accounts_weight_for_it():
    alice, bob = _two_accounts()
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')
    for account in (alice, bob):
        set_managed_label(account, 'ARK26', target_pct=100.0)
        set_symbol_weight(account, 'ARK26', 'AAPL', weight_pct=70.0, comment='mine')

    remove_symbols_from_label(alice, 'ARK26', ['AAPL'])

    assert get_symbol_rows(alice, 'ARK26') == {}
    assert get_symbol_rows(bob, 'ARK26')['AAPL'].weight_pct == 70.0
    assert get_symbol_rows(bob, 'ARK26')['AAPL'].comment == 'mine'


def test_removing_a_symbol_from_one_label_keeps_its_row_under_another():
    """A symbol may sit in several managed labels; they hold separate weights."""
    alice, _ = _two_accounts()
    add_label_to_instruments(['AAPL'], 'ARK26')
    add_label_to_instruments(['AAPL'], 'HighRisk')
    set_managed_label(alice, 'ARK26', target_pct=50.0)
    set_managed_label(alice, 'HighRisk', target_pct=50.0)
    set_symbol_weight(alice, 'ARK26', 'AAPL', weight_pct=40.0)
    set_symbol_weight(alice, 'HighRisk', 'AAPL', weight_pct=60.0)

    remove_symbols_from_label(alice, 'ARK26', ['AAPL'])

    assert get_symbol_rows(alice, 'ARK26') == {}
    assert get_symbol_rows(alice, 'HighRisk')['AAPL'].weight_pct == 60.0


def test_removing_a_symbol_drops_the_global_instrument_label_for_everyone():
    """The label itself IS global -- the store logs it, and this pins the meaning
    so nobody 'fixes' the shared side by accident."""
    alice, _ = _two_accounts()
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_managed_label(alice, 'ARK26', target_pct=100.0)

    remove_symbols_from_label(alice, 'ARK26', ['AAPL'])
    assert get_symbols_by_label(['ARK26']) == {'ARK26': []}


def test_unmanaging_a_label_leaves_the_other_accounts_configuration_alone():
    alice, bob = _two_accounts()
    add_label_to_instruments(['AAPL'], 'ARK26')
    for account in (alice, bob):
        set_managed_label(account, 'ARK26', target_pct=33.0, comment='shared name')
        set_symbol_weight(account, 'ARK26', 'AAPL', weight_pct=80.0)

    assert remove_managed_label(alice, 'ARK26') is True

    assert [r.label for r in get_managed_labels(alice)] == []
    assert get_symbol_rows(alice, 'ARK26') == {}
    bob_label = next(r for r in get_managed_labels(bob) if r.label == 'ARK26')
    assert bob_label.target_pct == 33.0
    assert get_symbol_rows(bob, 'ARK26')['AAPL'].weight_pct == 80.0


def test_unmanaging_a_label_the_account_never_managed_reports_false():
    alice, _ = _two_accounts()
    assert remove_managed_label(alice, 'ARK26') is False
