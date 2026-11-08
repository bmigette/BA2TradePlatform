"""The Settings -> Cleanup tab covers the replay recordings store.

The tab calls the SAME ``ba2_common.core.replay.retention`` functions as ``ba2-test replay prune``;
here they run for real over a temp store. Statistics, a preview that changes nothing, a confirmed delete
that spares the running session, and a refusal that is SHOWN (never swallowed).
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.replay import ReplayStatus, ReplayStore, SessionRecord

OLD = datetime.now(timezone.utc) - timedelta(days=60)


class _Dialog:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-replay-cleanup'), request=None)
    yield client
    client.remove_elements(client.elements.values())


@pytest.fixture
def store(tmp_path):
    s = ReplayStore(tmp_path / "store", writer="sync")
    for sid, started in (("old", OLD), ("live", datetime.now(timezone.utc))):
        s.begin_session(SessionRecord(
            session_id=sid, instance_id="i", started_at=started, exchange_tz="America/New_York",
            app_version="x", package_versions={}, source_revision="abc", dirty=False))
    s.publish_session_object({"patch": "only the old session uses this"})
    s.index.update_session_status("old", ReplayStatus.SESSION_FINALIZED, ended_at=OLD)
    for path in (s.root / "objects").rglob("*.json"):
        os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    yield s
    s.close(timeout=2.0)


def _tab(nicegui_client, root, monkeypatch):
    from ba2_trade_platform.ui.pages.settings import BatchCleanupTab

    monkeypatch.setattr(BatchCleanupTab, "_replay_store_root", lambda self: str(root))
    with nicegui_client:
        return BatchCleanupTab()


def _text(nicegui_client):
    return " ".join(el._text for el in nicegui_client.layout.descendants(include_self=True) if el._text)


def _run(nicegui_client, coroutine):
    with nicegui_client:
        return asyncio.run(coroutine)


def test_the_tab_has_a_replay_recordings_section_with_a_default_window(nicegui_client, store, monkeypatch):
    tab = _tab(nicegui_client, store.root, monkeypatch)
    assert tab.replay_days_input.value == 30
    assert "Replay Recordings" in _text(nicegui_client)


def test_statistics_show_sessions_size_objects_and_dates(nicegui_client, store, monkeypatch):
    tab = _tab(nicegui_client, store.root, monkeypatch)
    _run(nicegui_client, tab._refresh_replay_stats())
    shown = _text(nicegui_client)
    assert "Sessions by status: " in shown and "finalized=1" in shown and "open=1" in shown
    assert "Object files" in shown and "Size on disk" in shown
    assert OLD.date().isoformat() in shown


def test_the_preview_reports_the_old_session_and_changes_nothing(nicegui_client, store, monkeypatch):
    tab = _tab(nicegui_client, store.root, monkeypatch)
    tab.replay_days_input.value = 30
    _run(nicegui_client, tab._preview_replay_prune())
    shown = _text(nicegui_client)
    assert "Will delete 1 session(s)" in shown and "old (finalized" in shown
    assert "1 running (protected)" in shown
    assert {s.session_id for s in store.index.list_sessions()} == {"old", "live"}


def test_the_confirmed_delete_removes_old_recordings_and_spares_the_running_session(nicegui_client, store, monkeypatch):
    notes = []
    from ba2_trade_platform.ui.pages import settings as settings_mod
    monkeypatch.setattr(settings_mod.ui, "notify", lambda msg, **kw: notes.append((str(msg), kw.get("type"))))
    tab = _tab(nicegui_client, store.root, monkeypatch)
    tab.replay_days_input.value = 30
    dialog = _Dialog()

    _run(nicegui_client, tab._perform_replay_prune(dialog))

    assert dialog.closed
    assert [s.session_id for s in store.index.list_sessions()] == ["live"]
    assert any("Deleted 1 session(s)" in message and kind == "positive" for message, kind in notes), notes
    assert "Deleted 1 session(s)" in _text(nicegui_client)


def test_a_store_that_is_not_one_is_shown_as_an_error_not_swallowed(nicegui_client, tmp_path, monkeypatch):
    notes = []
    from ba2_trade_platform.ui.pages import settings as settings_mod
    monkeypatch.setattr(settings_mod.ui, "notify", lambda msg, **kw: notes.append((str(msg), kw.get("type"))))
    tab = _tab(nicegui_client, tmp_path / "nothing-here", monkeypatch)

    _run(nicegui_client, tab._refresh_replay_stats())
    assert "not a replay store" in _text(nicegui_client)

    _run(nicegui_client, tab._perform_replay_prune(_Dialog()))
    assert any(kind == "negative" and "not a replay store" in message for message, kind in notes), notes


def test_a_refused_window_is_shown(nicegui_client, store, monkeypatch):
    tab = _tab(nicegui_client, store.root, monkeypatch)
    tab.replay_days_input.value = 0
    _run(nicegui_client, tab._preview_replay_prune())
    assert "keep_days" in _text(nicegui_client)
    assert {s.session_id for s in store.index.list_sessions()} == {"old", "live"}
