"""Regression: get_selected_account_id must survive non-UI contexts.

Several dashboard widgets (ProfitPerExpertChart, FloatingPLPerExpertWidget,
BalanceUsagePerExpertChart, ...) compute their data inside asyncio.to_thread,
where NiceGUI's app.storage.user is unavailable ("app.storage.user can only be
used within a UI context"). The old get_selected_account_id swallowed that error
and returned None, so those widgets silently aggregated ALL accounts instead of
honoring the dropdown filter. get_selected_account_id must instead fall back to
the last value seen/set in a UI context (2026-06-25 prod investigation).
"""
import ba2_trade_platform.ui.account_filter_context as afc


class _DictStorage:
    """app.storage stand-in whose .user is a plain dict (a UI context)."""
    def __init__(self, d):
        self._d = d

    @property
    def user(self):
        return self._d


class _RaisingStorage:
    """app.storage stand-in whose .user raises (a non-UI / threaded context)."""
    @property
    def user(self):
        raise RuntimeError("app.storage.user can only be used within a UI context")


class _FakeApp:
    def __init__(self, storage):
        self.storage = storage


def _reset_cache():
    afc._last_known_account_id = None


def test_get_falls_back_to_cache_outside_ui_context(monkeypatch):
    _reset_cache()
    store = {}
    monkeypatch.setattr(afc, "app", _FakeApp(_DictStorage(store)))
    afc.set_selected_account_id(2)
    assert afc.get_selected_account_id() == 2

    # Now a background thread reads it: app.storage.user is unavailable.
    monkeypatch.setattr(afc, "app", _FakeApp(_RaisingStorage()))
    assert afc.get_selected_account_id() == 2  # cached fallback, NOT None


def test_get_returns_none_when_no_selection_and_no_cache(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(afc, "app", _FakeApp(_RaisingStorage()))
    assert afc.get_selected_account_id() is None


def test_setting_all_resets_cache_to_none(monkeypatch):
    _reset_cache()
    store = {}
    monkeypatch.setattr(afc, "app", _FakeApp(_DictStorage(store)))
    afc.set_selected_account_id(2)
    afc.set_selected_account_id(None)  # user picked "All"
    monkeypatch.setattr(afc, "app", _FakeApp(_RaisingStorage()))
    assert afc.get_selected_account_id() is None


def test_ui_context_get_refreshes_cache(monkeypatch):
    """A successful UI-context read must update the cache so a later threaded
    read reflects a selection that was persisted (e.g. across restart) without an
    explicit set in this process."""
    _reset_cache()
    monkeypatch.setattr(afc, "app", _FakeApp(_DictStorage({afc.ACCOUNT_FILTER_KEY: 1})))
    assert afc.get_selected_account_id() == 1
    monkeypatch.setattr(afc, "app", _FakeApp(_RaisingStorage()))
    assert afc.get_selected_account_id() == 1
