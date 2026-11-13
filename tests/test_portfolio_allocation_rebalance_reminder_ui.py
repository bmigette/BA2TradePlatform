"""The "rebalance due" reminder on the Portfolio Allocation page, driven through the real ``content()`` (the page
tests' harness: a fake broker account, a bare NiceGUI client). Its button must be the toolbar's own Review and Submit
path -- the same ``_open_allocation_flow`` dry run -- never a second order path, and whatever it opens must be built in
the never-cleared dialog host, not in the reminder's own (cleared) slot."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def _utc_today():
    return datetime.now(timezone.utc).date()


def _record_run(account_id, days_ago):
    row = record_allocation_run(account_id, ALLOCATION_MODE_REBALANCE, {}, order_ids=[1])
    with get_db() as session:
        stored = session.exec(select(PortfolioAllocationRun).where(PortfolioAllocationRun.id == row.id)).one()
        # stored as naive UTC, like the live writer's DateTime.now(timezone.utc) once SQLite has it
        stored.created_at = datetime.combine(_utc_today() - timedelta(days=days_ago), datetime.min.time())
        session.add(stored)
        session.commit()


def _capture_timers(monkeypatch):
    """Record every ``ui.timer`` with the slot it was built in instead of scheduling it (no loop runs here)."""
    from nicegui import context
    timers = []

    def fake(_interval, callback, *args, once=False, **kwargs):
        timers.append(SimpleNamespace(callback=callback, parent=context.slot.parent, once=once))
        return SimpleNamespace(deactivate=lambda: None, activate=lambda: None, cancel=lambda: None)

    monkeypatch.setattr(page.ui, 'timer', fake)
    return timers


def _fire_once_timers(client, timers):
    """Run each one-shot timer's callback IN THE SLOT IT WAS BUILT IN, as NiceGUI does."""
    due = [t for t in timers if t.once]
    timers[:] = [t for t in timers if not t.once]

    async def _run():
        with client:
            for t in due:
                with t.parent:
                    result = t.callback()
                    if asyncio.iscoroutine(result):
                        await result
    asyncio.run(_run())
    return due


def _press_reminder_review(root, monkeypatch):
    timers = _capture_timers(monkeypatch)
    pagetests._press(pagetests._marked(_reminder(root), page.MARKER_REBAL_REMINDER_REVIEW)[0])
    return timers


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
    timers = _press_reminder_review(root, monkeypatch)
    assert calls == []                                  # deferred: nothing runs in the click's own slot
    assert len(_fire_once_timers(nicegui_client, timers)) == 1
    from_reminder = list(calls)
    pagetests._press(pagetests._review_button(root))
    assert from_reminder == [(account_id, page.VALUATION_MODE_MARKET)]
    assert calls == [from_reminder[0], from_reminder[0]]          # identical to the toolbar's call


def test_the_preview_opened_from_the_reminder_survives_the_refresh_that_redraws_it(
        monkeypatch, nicegui_client, account_id):
    """Review 2026-11-13: a click handler builds in the clicked button's slot, and the reminder's slot is CLEARED by
    every refresh (the one that ends a Submit included). The preview must be built in the never-cleared dialog host."""
    from nicegui import ui
    _quiet(monkeypatch)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    old_reminder = _reminder(root)
    opened = {}

    async def _flow(acct, mode, refresh, **kwargs):
        opened['dialog'] = ui.dialog()              # built wherever the flow runs, as open_allocation_wizard is
        opened['refresh'] = refresh

    monkeypatch.setattr(page, '_open_allocation_flow', _flow)
    timers = _press_reminder_review(root, monkeypatch)
    assert len(timers) == 1
    assert timers[0].parent is not old_reminder and timers[0].parent not in old_reminder.descendants()
    _fire_once_timers(nicegui_client, timers)
    dialog = opened['dialog']
    assert dialog not in old_reminder.descendants()

    pagetests._run_in_client(nicegui_client, opened['refresh'])      # e.g. the refresh at the end of Submit

    assert old_reminder.is_deleted                                    # the reminder WAS redrawn...
    assert len(pagetests._marked(nicegui_client.layout, page.MARKER_REBAL_REMINDER)) == 1
    assert not dialog.is_deleted                                      # ...and the preview is still there


def test_the_reminder_button_obeys_the_what_if_interlock(monkeypatch, nicegui_client, account_id):
    """It runs ``_review`` itself, so the simulated-base refusal applies to it as to the toolbar."""
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
    timers = _press_reminder_review(nicegui_client.layout, monkeypatch)
    _fire_once_timers(nicegui_client, timers)
    assert calls == []
    assert any(t == 'warning' for _m, t in sent)


def test_a_recent_run_is_not_due_and_draws_no_button(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    _record_run(account_id, days_ago=10)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    reminder = _reminder(root)
    assert not pagetests._marked(reminder, page.MARKER_REBAL_REMINDER_REVIEW)
    assert any('10 day(s) ago' in t and 'next reminder is due on' in t for t in _texts_in(reminder))


def test_an_old_run_is_due_with_its_utc_date_and_age(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    _record_run(account_id, days_ago=120)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    reminder = _reminder(root)
    when = (_utc_today() - timedelta(days=120)).isoformat()
    assert any('Rebalance due' in t and f'{when} (UTC)' in t and '120 day(s) ago' in t for t in _texts_in(reminder))
    assert pagetests._marked(reminder, page.MARKER_REBAL_REMINDER_REVIEW)


def _fire_all(element, event_type):
    """Every listener of event_type, in registration order, as the browser event does: ui.number's own
    blur listener (it clamps to min/max) runs before the page's save."""
    from nicegui.events import GenericEventArguments
    fired = 0
    for listener in list(element._event_listeners.values()):
        if listener.type.split('.')[0] == event_type:
            pagetests._drive(listener.handler, GenericEventArguments(sender=element, client=None, args={}))
            fired += 1
    assert fired, f'no {event_type!r} listener'


def _interval_box(client):
    return pagetests._marked(_reminder(client.layout), page.MARKER_REBAL_REMINDER_INTERVAL)[0]


def test_the_interval_saves_on_blur_or_enter_not_per_keystroke(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    _record_run(account_id, days_ago=120)
    pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    box = _interval_box(nicegui_client)
    with nicegui_client:
        box.set_value(1)                  # the "1" of a "12" being typed: no save
    assert store.get_reminder_months(account_id) == store.DEFAULT_REMINDER_MONTHS
    with nicegui_client:
        box.set_value(12)
        _fire_all(box, 'blur')
    assert store.get_reminder_months(account_id) == 12
    assert not pagetests._marked(_reminder(nicegui_client.layout), page.MARKER_REBAL_REMINDER_REVIEW)
    box = _interval_box(nicegui_client)
    with nicegui_client:
        box.set_value(2)
        _fire_all(box, 'keydown')   # keydown.enter
    assert store.get_reminder_months(account_id) == 2
    assert pagetests._marked(_reminder(nicegui_client.layout), page.MARKER_REBAL_REMINDER_REVIEW)   # 120 d > 2 mo


def test_an_unchanged_blur_writes_nothing_out_of_range_clamps_and_empty_is_refused(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    saved = []
    real = page.set_reminder_months
    monkeypatch.setattr(page, 'set_reminder_months', lambda *a: saved.append(a) or real(*a))
    box = _interval_box(nicegui_client)
    with nicegui_client:
        _fire_all(box, 'blur')
    assert saved == []
    with nicegui_client:
        box.set_value(0)                   # the box clamps to its min before the save sees it
        _fire_all(box, 'blur')
    assert store.get_reminder_months(account_id) == 1
    sent = pagetests._capture_notifications(monkeypatch)
    box = _interval_box(nicegui_client)
    with nicegui_client:
        box.set_value(None)                # a cleared box is refused, loudly, and nothing is stored
        _fire_all(box, 'blur')
    assert store.get_reminder_months(account_id) == 1
    assert any(t == 'warning' for _m, t in sent)
