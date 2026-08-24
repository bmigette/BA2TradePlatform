"""Regression: get_selected_account_id must survive non-UI contexts.

Several dashboard widgets (ProfitPerExpertChart, FloatingPLPerExpertWidget,
BalanceUsagePerExpertChart, ...) compute their data inside asyncio.to_thread,
where NiceGUI's app.storage.user is unavailable ("app.storage.user can only be
used within a UI context"). The old get_selected_account_id swallowed that error
and returned None, so those widgets silently aggregated ALL accounts instead of
honoring the dropdown filter. get_selected_account_id must instead fall back to
the last value seen/set in a UI context (2026-06-25 prod investigation).
"""
import pytest

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


def test_switching_between_two_accounts_reads_back_the_new_one(monkeypatch):
    """The reported case: Alpaca is selected, then TastyTrade. Every later read --
    UI context or threaded -- must answer TastyTrade, never the previous account."""
    _reset_cache()
    store = {}
    monkeypatch.setattr(afc, "app", _FakeApp(_DictStorage(store)))
    afc.set_selected_account_id(1)          # Alpaca
    assert afc.get_selected_account_id() == 1
    afc.set_selected_account_id(2)          # TastyTrade
    assert afc.get_selected_account_id() == 2
    monkeypatch.setattr(afc, "app", _FakeApp(_RaisingStorage()))
    assert afc.get_selected_account_id() == 2


def test_set_persists_the_coerced_value_so_storage_and_the_mirror_agree(monkeypatch):
    """``set`` used to mirror the COERCED id but persist the RAW one.

    The mirror is what threaded readers get and ``app.storage.user`` is what the
    next page build gets, so the two must not be allowed to hold different
    representations of the same choice. ``app.storage.user`` is also serialised to
    JSON on every write (``nicegui/persistence/file_persistent_dict.py:39``), which
    is where a stray ``"2"`` would become permanent.
    """
    _reset_cache()
    store = {}
    monkeypatch.setattr(afc, "app", _FakeApp(_DictStorage(store)))

    afc.set_selected_account_id("2")

    assert store[afc.ACCOUNT_FILTER_KEY] == 2
    assert isinstance(store[afc.ACCOUNT_FILTER_KEY], int)    # the int 2, not the string
    assert afc._last_known_account_id == 2


@pytest.mark.parametrize("raw", ["None", ""])
def test_set_persists_none_for_all_rather_than_a_sentinel_string(monkeypatch, raw):
    _reset_cache()
    store = {}
    monkeypatch.setattr(afc, "app", _FakeApp(_DictStorage(store)))

    afc.set_selected_account_id(raw)        # a legacy/"stringified" All

    assert store[afc.ACCOUNT_FILTER_KEY] is None
    assert afc.get_selected_account_id() is None


def test_the_mirror_is_updated_even_when_the_storage_write_fails(monkeypatch):
    """``set`` mirrors BEFORE it writes, on purpose.

    If the write raises (no UI context) the choice would otherwise be lost
    entirely, and every threaded reader would keep serving the previous account.
    """
    _reset_cache()
    monkeypatch.setattr(afc, "app", _FakeApp(_RaisingStorage()))

    afc.set_selected_account_id(2)

    assert afc._last_known_account_id == 2
    assert afc.get_selected_account_id() == 2


def test_a_ui_context_read_of_all_beats_a_stale_mirror(monkeypatch):
    """The mirror is a FALLBACK, never an override.

    ``_last_known_account_id`` is process-global: it is shared by every session and
    tab, and a background widget's read leaves whatever it saw behind. If a
    UI-context read preferred it whenever the session stored "All", picking "All"
    would be impossible and one session could serve another session's account --
    which is the only way the Portfolio Allocation gate could name the wrong
    account's experts WITHOUT a stale page.
    """
    _reset_cache()
    afc._last_known_account_id = 5              # left behind by an earlier read
    monkeypatch.setattr(afc, "app", _FakeApp(_DictStorage({})))

    assert afc.get_selected_account_id() is None
    assert afc._last_known_account_id is None   # and the stale value is cleared


def test_the_filter_options_always_offer_all_first(monkeypatch):
    """Without the ("All", None) entry there is no way back to the unfiltered view."""
    from types import SimpleNamespace

    afc._accounts_cache['data'] = None
    afc._accounts_cache['timestamp'] = 0
    monkeypatch.setattr(afc, 'get_all_instances', lambda _model: [
        SimpleNamespace(id=1, name='Alcapa Live', provider='Alpaca'),
        SimpleNamespace(id=2, name='Tasty', provider='TastyTrade'),
    ])

    options = afc.get_accounts_for_filter()

    assert options[0] == ('All', None)
    assert options[1:] == [('Alcapa Live (Alpaca)', 1), ('Tasty (TastyTrade)', 2)]
    afc._accounts_cache['data'] = None          # do not leak the stub into other tests
    afc._accounts_cache['timestamp'] = 0


def _reset_expert_cache():
    """The module keeps a 60s TTL cache keyed by account id; empty it."""
    afc._expert_ids_cache['data'] = {}
    afc._expert_ids_cache['timestamp'] = 0


def test_expert_ids_are_scoped_to_the_account_that_owns_them():
    """The same cross-account leak as the Portfolio Allocation gate, one page over.

    Overview, Live Trades and Market Analysis all narrow their queries with this
    list. Dropping the ``account_id`` filter would show every account's experts
    under whichever single account is selected.
    """
    from tests.factories import create_account_definition, create_expert_instance

    _reset_expert_cache()
    alpaca = create_account_definition(name='Alcapa Live', provider='MockAccount')
    tasty = create_account_definition(name='Tasty', provider='MockAccount')
    screener = create_expert_instance(account_id=alpaca.id, expert='FMPPScreener')
    retired = create_expert_instance(account_id=alpaca.id, expert='MockExpert',
                                     enabled=False)

    assert sorted(afc.get_expert_ids_for_account(alpaca.id)) == \
        sorted([screener.id, retired.id])
    _reset_expert_cache()
    assert afc.get_expert_ids_for_account(tasty.id) == []


def test_all_accounts_means_no_expert_filter_not_an_empty_one():
    """``None`` ("All") must return ``None`` -- "do not filter" -- never ``[]``.

    ``[]`` reads downstream as "this account owns no experts", which renders an
    empty dashboard instead of every account's data.
    """
    _reset_expert_cache()
    assert afc.get_expert_ids_for_account(None) is None


def test_a_string_selection_reads_back_as_an_int_on_both_paths(monkeypatch):
    """Whatever representation goes in, both readers answer the same int."""
    _reset_cache()
    store = {}
    monkeypatch.setattr(afc, "app", _FakeApp(_DictStorage(store)))
    afc.set_selected_account_id("2")
    assert afc.get_selected_account_id() == 2                # storage path
    monkeypatch.setattr(afc, "app", _FakeApp(_RaisingStorage()))
    assert afc.get_selected_account_id() == 2                # mirror path
