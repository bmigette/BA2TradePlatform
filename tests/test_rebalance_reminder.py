"""The allocator's "rebalance due" reminder: the pure due rule (``ui/utils/rebalance_reminder.py``) and its store
(``core/allocator_rebalance_reminder.py``: the interval and the last completed rebalance run)."""
from datetime import date, datetime

import pytest
from sqlmodel import select

from ba2_trade_platform.core import allocator_rebalance_reminder as store
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import PortfolioAllocationRun
from ba2_trade_platform.core.portfolio_allocation import (ALLOCATION_MODE_INVEST_LABEL,
                                                         ALLOCATION_MODE_REBALANCE)
from ba2_trade_platform.core.portfolio_allocation_store import record_allocation_run
from ba2_trade_platform.ui.utils.rebalance_reminder import add_months, rebalance_reminder
from tests.factories import create_account_definition


# ---------------------------------------------------------------------------
# the pure rule
# ---------------------------------------------------------------------------

def test_no_run_is_due_and_says_so_without_a_date():
    r = rebalance_reminder(None, 3, date(2026, 11, 13))
    assert r.due and r.last_run_date is None and r.days_elapsed is None and r.due_date is None
    assert 'No completed rebalance run' in r.text
    assert '20' not in r.text.split('interval')[0]           # no invented date before the interval clause


def test_due_exactly_at_the_interval_and_not_a_day_before():
    last = date(2026, 8, 13)
    before = rebalance_reminder(last, 3, date(2026, 11, 12))
    at = rebalance_reminder(last, 3, date(2026, 11, 13))
    assert not before.due and before.due_date == date(2026, 11, 13) and before.days_elapsed == 91
    assert at.due and at.days_elapsed == 92
    assert '2026-08-13' in at.text and '92 day(s) ago' in at.text
    assert 'next reminder is due on 2026-11-13' in before.text


def test_a_datetime_counts_by_its_date_and_months_clip_to_the_month_end():
    r = rebalance_reminder(datetime(2026, 1, 31, 15, 30), 1, date(2026, 2, 28))
    assert r.last_run_date == date(2026, 1, 31) and r.due_date == date(2026, 2, 28) and r.due
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert add_months(date(2026, 5, 15), 12) == date(2027, 5, 15)


@pytest.mark.parametrize('bad', [0, -1, 1.5, '3', None, True])
def test_an_unusable_interval_raises(bad):
    with pytest.raises(ValueError):
        rebalance_reminder(date(2026, 1, 1), bad, date(2026, 11, 13))


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

@pytest.fixture
def account_id():
    return create_account_definition(name='Manual', provider='MockAccount').id


def _run(account_id, mode, order_ids, created_at):
    row = record_allocation_run(account_id, mode, {}, order_ids=order_ids)
    with get_db() as session:
        stored = session.exec(select(PortfolioAllocationRun).where(PortfolioAllocationRun.id == row.id)).one()
        stored.created_at = created_at
        session.add(stored)
        session.commit()
    return row.id


def test_interval_defaults_to_three_months_and_is_stored_validated(account_id):
    assert store.get_reminder_months(account_id) == store.DEFAULT_REMINDER_MONTHS == 3
    assert store.set_reminder_months(account_id, 6) == 6
    assert store.get_reminder_months(account_id) == 6
    assert store.set_reminder_months(account_id, 12.0) == 12           # a number box sends floats
    assert store.get_reminder_months(account_id) == 12
    for bad in (0, 37, 2.5, None, 'x', True):
        with pytest.raises(ValueError):
            store.set_reminder_months(account_id, bad)
    assert store.get_reminder_months(account_id) == 12                 # nothing invalid was written


def test_last_run_is_the_newest_rebalance_that_sent_an_order(account_id):
    assert store.last_rebalance_run_at(account_id) is None
    _run(account_id, ALLOCATION_MODE_REBALANCE, [11], datetime(2026, 5, 2, 14, 0))
    _run(account_id, ALLOCATION_MODE_REBALANCE, [12, 13], datetime(2026, 8, 3, 14, 0))
    _run(account_id, ALLOCATION_MODE_INVEST_LABEL, [14], datetime(2026, 10, 1, 14, 0))    # not a rebalance
    _run(account_id, ALLOCATION_MODE_REBALANCE, [], datetime(2026, 10, 5, 14, 0))         # no order went out
    other = create_account_definition(name='Other', provider='MockAccount').id
    _run(other, ALLOCATION_MODE_REBALANCE, [15], datetime(2026, 11, 1, 14, 0))            # another account
    assert store.last_rebalance_run_at(account_id) == datetime(2026, 8, 3, 14, 0)


def test_store_and_rule_together(account_id):
    _run(account_id, ALLOCATION_MODE_REBALANCE, [1], datetime(2026, 8, 3, 9, 0))
    store.set_reminder_months(account_id, 3)
    r = rebalance_reminder(store.last_rebalance_run_at(account_id), store.get_reminder_months(account_id),
                           date(2026, 11, 13))
    assert r.due and r.last_run_date == date(2026, 8, 3) and r.days_elapsed == 102


def test_dates_are_compared_in_utc():
    """Review 2026-11-13: created_at is UTC; an aware time is converted, a naive one IS UTC, and the text says UTC."""
    from datetime import timedelta, timezone
    new_york = timezone(timedelta(hours=-5))
    r = rebalance_reminder(datetime(2026, 8, 12, 21, 30, tzinfo=new_york), 3, date(2026, 11, 13))
    assert r.last_run_date == date(2026, 8, 13) and r.due and r.days_elapsed == 92      # 02:30 UTC on 13 Aug
    naive = rebalance_reminder(datetime(2026, 8, 12, 21, 30), 3, date(2026, 11, 12))
    assert naive.last_run_date == date(2026, 8, 12) and naive.due
    assert '2026-08-12 (UTC)' in naive.text
