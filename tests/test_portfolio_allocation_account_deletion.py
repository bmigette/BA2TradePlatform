"""Deleting an account must clear its allocation rows.

The live DB runs with PRAGMA foreign_keys = 0, so the declared ondelete="CASCADE"
never fires: an account id that is later reused would otherwise inherit the old
account's managed labels, weights, income ledger and run history.

`AccountDefinitionsTab.delete_account` is a NiceGUI method, but its body is plain
DB work apart from one ui.notify in the failure branch, so it is driven directly
with a bare instance (object.__new__) and a stubbed _update_table_rows.
"""
from datetime import date

import pytest
from sqlmodel import select

from ba2_trade_platform.core import portfolio_allocation_store as store
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountDefinition, PortfolioAllocationConfig
from tests.factories import create_account_definition


def _accounts_tab():
    from ba2_trade_platform.ui.pages.settings import AccountDefinitionsTab
    tab = object.__new__(AccountDefinitionsTab)
    tab._update_table_rows = lambda: None
    return tab


def _delete_account(account):
    _accounts_tab().delete_account(account)


def _seed(account_id):
    store.set_managed_label(account_id, 'ARK26', target_pct=100.0)
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA', weight_pct=100.0)
    store.upsert_income_event(account_id, 'csd-1', date(2026, 8, 1), 'DEPOSIT', 100.0)
    store.record_allocation_run(account_id, 'REBALANCE', {})
    # NON-default on purpose: the default is 'market' (W1), so seeding 'cost' is
    # what makes "a fresh read yields the DEFAULT" a real assertion rather than a
    # tautology that a stranded row would also satisfy.
    store.set_allocation_config(account_id, valuation_mode='cost')


def _config_rows(account_id):
    with get_db() as session:
        return session.exec(
            select(PortfolioAllocationConfig).where(
                PortfolioAllocationConfig.account_id == account_id)
        ).all()


def test_deleting_an_account_clears_all_of_its_allocation_rows():
    account = create_account_definition(name='Doomed')
    account_id = account.id
    _seed(account_id)

    _delete_account(account)

    with get_db() as session:
        assert session.get(AccountDefinition, account_id) is None
    assert store.get_managed_labels(account_id) == []
    assert store.get_symbol_rows(account_id, 'ARK26') == {}
    assert store.get_open_income_events(account_id) == []
    assert store.get_recent_runs(account_id) == []


def test_deleting_an_account_clears_the_unique_config_row_it_would_collide_with():
    """The config row is the one that BREAKS a reused id, not merely dirties it.

    ``portfolio_allocation_config.account_id`` is UNIQUE, so a stranded row is a
    collision the next time the id is handed out -- and until it collides it hands
    the new account the DEAD one's valuation mode, silently reinterpreting every
    percentage on the page. The table is queried directly because
    ``get_allocation_config`` CREATES the row it cannot find, which would mask
    exactly the leak under test; the follow-up assertion then proves a fresh read
    yields the DEFAULT 'market' rather than the deleted account's 'cost'.
    """
    account = create_account_definition(name='Doomed')
    account_id = account.id
    _seed(account_id)
    assert [r.valuation_mode for r in _config_rows(account_id)] == ['cost']

    _delete_account(account)

    assert _config_rows(account_id) == []
    assert store.get_allocation_config(account_id).valuation_mode == 'market'


def test_deleting_an_account_leaves_another_accounts_allocation_intact():
    doomed = create_account_definition(name='Doomed')
    keeper = create_account_definition(name='Keeper')
    _seed(doomed.id)
    _seed(keeper.id)

    _delete_account(doomed)

    assert [r.label for r in store.get_managed_labels(keeper.id)] == ['ARK26']
    assert store.get_open_income_total(keeper.id) == 100.0
    assert [r.valuation_mode for r in _config_rows(keeper.id)] == ['cost']
    assert list(store.get_symbol_rows(keeper.id, 'ARK26')) == ['TSLA']
    assert len(store.get_recent_runs(keeper.id)) == 1


def test_deleting_an_account_that_never_used_allocation_still_deletes_it():
    """No allocation rows is the common case and must not become an error path.

    The cleanup runs unconditionally, so if it ever raised on an account with no
    config row the exception handler would swallow it and the ACCOUNT would
    survive the delete -- a worse bug than the one being fixed.
    """
    account = create_account_definition(name='Never allocated')
    account_id = account.id

    _delete_account(account)

    with get_db() as session:
        assert session.get(AccountDefinition, account_id) is None


def test_a_failed_deletion_notifies_with_a_valid_nicegui_type(monkeypatch):
    """'error' is not a NiceGUI notify type; the four valid ones are
    positive/negative/warning/info. The failure branch has to use one of them or
    the only signal the user gets from a failed delete is a mis-styled toast."""
    from nicegui import ui as nicegui_ui
    from ba2_trade_platform.ui.pages import settings as settings_page

    captured = {}
    monkeypatch.setattr(nicegui_ui, 'notify',
                        lambda message, **kw: captured.update(message=message, **kw))

    def _boom(*args, **kwargs):
        raise RuntimeError('db is on fire')

    monkeypatch.setattr(settings_page, 'delete_instance', _boom)

    account = create_account_definition(name='Doomed')
    _delete_account(account)      # swallowed, not raised

    assert captured['type'] in ('positive', 'negative', 'warning', 'info')
    assert captured['type'] == 'negative'
