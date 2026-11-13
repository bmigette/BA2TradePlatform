"""The "rebalance due" reminder on the Portfolio Allocation page, driven through the real ``content()`` (the page
tests' harness: a fake broker account, a bare NiceGUI client). Its button must be the toolbar's own Review and Submit
path -- the same ``_open_allocation_flow`` dry run -- never a second order path."""
from datetime import date, datetime, timedelta

from sqlmodel import select

import tests.test_portfolio_allocation_page as pagetests
from tests.test_portfolio_allocation_page import (  # noqa: F401 -- fixtures
    account_id, nicegui_client, run_to_thread_inline)
from ba2_trade_platform.core import allocator_rebalance_reminder as store
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import PortfolioAllocationRun
from ba2_trade_platform.core.portfolio_allocation import ALLOCATION_MODE_REBALANCE
from ba2_trade_platform.core.portfolio_allocation_store import record_allocation_run
from ba2_trade_platform.ui.pages import portfolio_allocation as page


def _quiet(monkeypatch):
    monkeypatch.setattr(page.svc, 'refresh_symbol_stats', lambda *a, **k: None)


def _reminder(root):
    found = pagetests._marked(root, page.MARKER_REBAL_REMINDER)
    assert len(found) == 1, f'expected one reminder, found {len(found)}'
    return found[0]


def _texts_in(element):
    return [el._text for el in element.descendants() if getattr(el, '_text', None)]


def _record_run(account_id, days_ago):
    row = record_allocation_run(account_id, ALLOCATION_MODE_REBALANCE, {}, order_ids=[1])
    with get_db() as session:
        stored = session.exec(select(PortfolioAllocationRun).where(PortfolioAllocationRun.id == row.id)).one()
        stored.created_at = datetime.combine(date.today() - timedelta(days=days_ago), datetime.min.time())
        session.add(stored)
        session.commit()


def test_with_no_run_the_reminder_says_so_and_carries_review(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    reminder = _reminder(root)
    assert any('No completed rebalance run' in t for t in _texts_in(reminder))
    buttons = pagetests._marked(reminder, page.MARKER_REBAL_REMINDER_REVIEW)
    assert len(buttons) == 1 and buttons[0]._props.get('label') == page.REVIEW_BUTTON_LABEL
    interval = pagetests._marked(reminder, page.MARKER_REBAL_REMINDER_INTERVAL)[0]
    assert interval.value == store.DEFAULT_REMINDER_MONTHS


def test_the_reminder_button_opens_the_existing_dry_run_flow(monkeypatch, nicegui_client, account_id):
    """The SAME path as the toolbar's Review and Submit: ``_open_allocation_flow`` for this account and mode, which
    solves and opens the order preview; orders go out only from that preview's Submit."""
    _quiet(monkeypatch)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    calls = []

    async def _spy(acct, mode, refresh, **kwargs):
        calls.append((acct, mode))

    monkeypatch.setattr(page, '_open_allocation_flow', _spy)
    pagetests._press(pagetests._marked(_reminder(root), page.MARKER_REBAL_REMINDER_REVIEW)[0])
    toolbar_calls = list(calls)
    pagetests._press(pagetests._review_button(root))
    assert toolbar_calls == [(account_id, page.VALUATION_MODE_MARKET)]
    assert calls == [toolbar_calls[0], toolbar_calls[0]]          # identical to the toolbar's call


def test_the_reminder_button_obeys_the_what_if_interlock(monkeypatch, nicegui_client, account_id):
    """It is ``_review`` itself, so the simulated-base refusal applies to it as to the toolbar."""
    _quiet(monkeypatch)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    sent = pagetests._capture_notifications(monkeypatch)
    calls = []

    async def _spy(*a, **k):
        calls.append(a)

    monkeypatch.setattr(page, '_open_allocation_flow', _spy)
    switch = pagetests._marked(root, page.MARKER_SIM_TOGGLE)[0]
    base = pagetests._marked(root, page.MARKER_SIM_INPUT)[0]
    with nicegui_client:
        pagetests._drive_value(base, 50_000.0)
        pagetests._drive_value(switch, True)
    root = nicegui_client.layout
    pagetests._press(pagetests._marked(_reminder(root), page.MARKER_REBAL_REMINDER_REVIEW)[0])
    assert calls == []
    assert any(t == 'warning' for _m, t in sent)


def test_a_recent_run_is_not_due_and_draws_no_button(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    _record_run(account_id, days_ago=10)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    reminder = _reminder(root)
    assert not pagetests._marked(reminder, page.MARKER_REBAL_REMINDER_REVIEW)
    assert any('10 day(s) ago' in t and 'next reminder is due on' in t for t in _texts_in(reminder))


def test_an_old_run_is_due_with_its_date_and_age(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    _record_run(account_id, days_ago=120)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    reminder = _reminder(root)
    when = (date.today() - timedelta(days=120)).isoformat()
    assert any('Rebalance due' in t and when in t and '120 day(s) ago' in t for t in _texts_in(reminder))
    assert pagetests._marked(reminder, page.MARKER_REBAL_REMINDER_REVIEW)


def test_changing_the_interval_stores_it_and_redraws(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    _record_run(account_id, days_ago=120)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    interval = pagetests._marked(_reminder(root), page.MARKER_REBAL_REMINDER_INTERVAL)[0]
    with nicegui_client:
        pagetests._drive_value(interval, 6)
    assert store.get_reminder_months(account_id) == 6
    reminder = _reminder(nicegui_client.layout)
    assert not pagetests._marked(reminder, page.MARKER_REBAL_REMINDER_REVIEW)        # 120 days < 6 months
    sent = pagetests._capture_notifications(monkeypatch)
    with nicegui_client:
        pagetests._drive_value(pagetests._marked(reminder, page.MARKER_REBAL_REMINDER_INTERVAL)[0], 0)
    assert store.get_reminder_months(account_id) == 6
    assert any(t == 'warning' for _m, t in sent)
