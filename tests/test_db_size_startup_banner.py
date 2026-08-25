"""The "your database is huge" banner: shown ONCE per app start, above 1 GB, naming the
cleanup tool.

WHY IT IS EASY TO SHIP BROKEN. The user's database is 399 MB, so this never fires for
them today. A banner that only fires above a threshold nobody crosses is a feature whose
bugs are discovered a year later, by which point the database is 3 GB and the banner
still says nothing. So BOTH sides are asserted — it fires above, it is silent below —
plus the third case that a one-sided test always forgets: a size that cannot be measured
at all.

THE RENDER-PATH RULE, inherited from the header balance next to it: this header is built
on EVERY page navigation. Anything on the render path that touches the filesystem is a
stat per navigation, and on a slow or network-mounted volume a hang per navigation. The
measurement therefore happens in a ``ui.timer`` after the HTML exists, off the event loop
via ``asyncio.to_thread``, and every failure is swallowed.

Never ``caplog``: ``logger.py`` sets ``propagate = False``.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from ba2_trade_platform.ui import layout


GB = 1_000_000_000


@pytest.fixture(autouse=True)
def _reset_banner_latch():
    """The banner fires once per PROCESS, so the latch outlives a test unless reset."""
    layout.reset_db_size_banner()
    yield
    layout.reset_db_size_banner()


@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-db-size-banner'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _capture(monkeypatch, module, level):
    messages = []
    monkeypatch.setattr(module.logger, level, lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _texts(root):
    return [el._text for el in root.descendants(include_self=True) if el._text]


def _banner_timer(root):
    from nicegui import ui
    timers = [el for el in root.descendants() if isinstance(el, ui.timer)
              and layout.DB_SIZE_BANNER_TIMER_MARKER in (el._markers or ())]
    assert timers, 'the layout must schedule a database-size check'
    return timers[0]


def _banner(root):
    found = [el for el in root.descendants()
             if layout.DB_SIZE_BANNER_MARKER in (el._markers or ())]
    assert found, 'the layout must reserve a slot for the database-size banner'
    return found[0]


def _render_and_check(client, monkeypatch, size_bytes):
    """Render the layout, then run its size check with a canned measurement."""
    from nicegui import ui

    calls = []

    def _measure():
        calls.append(threading.current_thread())
        if isinstance(size_bytes, Exception):
            raise size_bytes
        return size_bytes

    monkeypatch.setattr(layout, 'database_file_size_bytes', _measure)

    with client:
        with layout.layout_render('Test'):
            ui.label('body')

    assert calls == [], 'the RENDER path must not touch the filesystem'

    timer = _banner_timer(client.layout)

    async def _drive():
        with client:
            await timer.callback()

    asyncio.run(_drive())
    return calls


# ---------------------------------------------------------------------------
# The pure decision
# ---------------------------------------------------------------------------

def test_a_database_over_the_threshold_produces_a_banner():
    text = layout.db_size_banner_text(2 * GB, GB)
    assert text is not None
    assert '2.00 GB' in text


def test_the_banner_names_the_cleanup_tool_and_where_to_find_it():
    """A banner that only says "your database is big" is alarming, not actionable."""
    text = layout.db_size_banner_text(3 * GB, GB)
    assert 'Batch Database Cleanup' in text
    assert 'Settings' in text
    assert 'Cleanup' in text


def test_a_database_under_the_threshold_is_silent():
    assert layout.db_size_banner_text(399_265_792, GB) is None


def test_exactly_at_the_threshold_fires():
    """Same inclusive convention as the vacuum gate: at the line counts as over it."""
    assert layout.db_size_banner_text(GB, GB) is not None
    assert layout.db_size_banner_text(GB - 1, GB) is None


def test_an_unmeasurable_size_is_silent():
    """A size we could not read is NOT evidence of a large database.

    The inverse choice — warn when unsure — trains the reader to dismiss the banner, and
    it would fire on every backtest ``:memory:`` engine, which has no file at all. The
    operator is told in the LOG instead (asserted below); the UI stays quiet.
    """
    assert layout.db_size_banner_text(None, GB) is None


# ---------------------------------------------------------------------------
# The threshold is configurable
# ---------------------------------------------------------------------------

def test_the_threshold_defaults_to_one_gigabyte():
    from ba2_common.core.db_maintenance import resolve_db_size_warn_bytes
    assert resolve_db_size_warn_bytes() == GB


def test_the_threshold_is_configurable(monkeypatch):
    from ba2_common.core.db_maintenance import (
        DB_SIZE_WARN_GB_ENV, resolve_db_size_warn_bytes,
    )
    monkeypatch.setenv(DB_SIZE_WARN_GB_ENV, "0.25")
    assert resolve_db_size_warn_bytes() == 250_000_000
    assert layout.db_size_banner_text(300_000_000, resolve_db_size_warn_bytes()) is not None


@pytest.mark.parametrize("bad", ["abc", "0", "-2", "", "None"])
def test_a_bad_threshold_is_refused(monkeypatch, bad):
    from ba2_common.core.db_maintenance import (
        DB_SIZE_WARN_GB_ENV, resolve_db_size_warn_bytes,
    )
    monkeypatch.setenv(DB_SIZE_WARN_GB_ENV, bad)
    with pytest.raises(ValueError):
        resolve_db_size_warn_bytes()


# ---------------------------------------------------------------------------
# Rendered behaviour
# ---------------------------------------------------------------------------

def test_the_banner_appears_when_the_database_is_too_big(nicegui_client, monkeypatch):
    _render_and_check(nicegui_client, monkeypatch, 2 * GB)

    texts = ' '.join(_texts(nicegui_client.layout))
    assert 'Batch Database Cleanup' in texts
    assert _banner(nicegui_client.layout).visible is True


def test_the_banner_stays_silent_when_the_database_is_small(nicegui_client, monkeypatch):
    _render_and_check(nicegui_client, monkeypatch, 399_265_792)

    texts = ' '.join(_texts(nicegui_client.layout))
    assert 'Batch Database Cleanup' not in texts
    assert _banner(nicegui_client.layout).visible is False


def test_an_unreadable_size_shows_nothing_but_says_so_in_the_log(nicegui_client, monkeypatch):
    warnings = _capture(monkeypatch, layout, 'warning')
    _render_and_check(nicegui_client, monkeypatch, None)

    assert 'Batch Database Cleanup' not in ' '.join(_texts(nicegui_client.layout))
    assert any('size' in m.lower() for m in warnings), warnings


def test_the_check_runs_off_the_event_loop_thread(nicegui_client, monkeypatch):
    """A stat on a hung network volume must not freeze every open page."""
    calls = _render_and_check(nicegui_client, monkeypatch, 2 * GB)
    assert len(calls) == 1
    assert calls[0] is not threading.current_thread()


def test_the_banner_is_shown_once_per_app_start_not_once_per_page(nicegui_client, monkeypatch):
    """THE requirement. The header is rebuilt on every navigation; the banner is not."""
    from nicegui import ui

    calls = []
    monkeypatch.setattr(layout, 'database_file_size_bytes',
                        lambda: (calls.append(1), 2 * GB)[1])

    banners = []
    for _ in range(4):
        with nicegui_client:
            with layout.layout_render('Test'):
                ui.label('body')
        timer = _banner_timer(nicegui_client.layout)

        async def _drive(t=timer):
            with nicegui_client:
                await t.callback()

        asyncio.run(_drive())
        banners.append(sum('Batch Database Cleanup' in (t or '')
                           for t in _texts(nicegui_client.layout)))

    assert len(calls) == 1, f'the database was measured {len(calls)} times, once per render'
    assert banners[-1] == 1, 'the banner must be drawn exactly once, not once per page'


def test_a_size_check_that_explodes_does_not_break_the_page(nicegui_client, monkeypatch):
    errors = _capture(monkeypatch, layout, 'error')
    _render_and_check(nicegui_client, monkeypatch, OSError('the volume went away'))

    assert 'body' in _texts(nicegui_client.layout), 'the page content must still be drawn'
    assert any('the volume went away' in m for m in errors), errors


def test_a_bad_threshold_setting_does_not_break_the_page(nicegui_client, monkeypatch):
    from ba2_common.core.db_maintenance import DB_SIZE_WARN_GB_ENV

    monkeypatch.setenv(DB_SIZE_WARN_GB_ENV, 'enormous')
    errors = _capture(monkeypatch, layout, 'error')
    _render_and_check(nicegui_client, monkeypatch, 2 * GB)

    assert 'body' in _texts(nicegui_client.layout)
    assert errors, 'a refused threshold must be reported, not swallowed'
