"""A stored schedule the live scheduler REFUSES (unknown day key) is shown as refused in the UI, not
silently rendered at its defaults (re-review item 1)."""
from types import SimpleNamespace

import pytest


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-schedule-refusal'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _texts(client):
    return [getattr(e, 'text', None) for e in client.elements.values() if getattr(e, 'text', None)]


TYPO = {"days": {"monday": True, "wensday": False}, "times": ["09:30"]}
CLEAN = {"days": {d: True for d in ("monday", "tuesday", "wednesday", "thursday", "friday",
                                    "saturday", "sunday")}, "times": ["09:30"]}


def test_the_settings_editor_banner_shows_the_refusal_and_clears_when_clean(nicegui_client):
    from nicegui import ui
    from ba2_trade_platform.ui.pages.settings import ExpertSettingsTab
    with nicegui_client:
        label = ui.label('')
        label.set_visibility(False)
    fake = SimpleNamespace(enter_market_schedule_refusal_label=label)
    ExpertSettingsTab._show_schedule_refusal(fake, 'enter_market', TYPO)
    assert label.visible and "schedule refused: unknown day key 'wensday'" in label.text
    ExpertSettingsTab._show_schedule_refusal(fake, 'enter_market', CLEAN)
    assert not label.visible


def test_the_scheduled_jobs_views_banner_lists_each_refused_schedule(nicegui_client):
    from ba2_trade_platform.ui.pages.marketanalysis import _render_schedule_refusals
    with nicegui_client:
        _render_schedule_refusals([])
        assert not _texts(nicegui_client)
        _render_schedule_refusals(["Expert instance 4, execution_schedule_enter_market: schedule refused: "
                                   "unknown day key 'wensday'"])
    texts = _texts(nicegui_client)
    assert any("NOT running" in t for t in texts)
    assert any("Expert instance 4" in t and "'wensday'" in t for t in texts)
