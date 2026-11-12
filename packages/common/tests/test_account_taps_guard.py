"""Guard: every concrete account class is tapped, overrides included.

``ReadOnlyAccountInterface.__init_subclass__`` wraps the sizing reads and the effect calls of every
subclass at class creation. A subclass that overrides one of them must come out tapped; a new
untapped override must FAIL here rather than silently record nothing on a live account. The real
broker classes are pinned in ``tests/test_account_taps_guard.py`` (root; they import the live
modules). This file pins the mechanism on test doubles, and that the taps change nothing when
capture is off.
"""
from datetime import datetime, timezone

import pytest

from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_common.core.replay.account_taps import (
    EFFECT_METHODS, SIZING_READ_TAPPED_METHODS, effect_boundary, tap_account_methods, untapped_methods,
)
from ba2_common.core.replay.context import capture_scope
from ba2_common.core.replay.observe import tapped_boundary
from ba2_common.core.replay.option_taps import OPTION_TAPPED_METHODS
from ba2_common.core.replay.schemas import ReplayStatus
from tests._account_tap_doubles import RecordingStore, StubAccount
from tests.test_max_loss_persisted_at_submit import FakeAccount as OptionsFakeAccount  # noqa: F401

UTC_NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)


def _meta():
    return {
        "analysis_id": "a1", "attempt_id": "att-1", "session_id": "s1",
        "expert_class": "X", "expert_instance_id": 7, "symbol": "XYZ",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
        "scheduled_at": UTC_NOW, "started_at": UTC_NOW,
    }


def _all_subclasses(base):
    seen, stack = [], list(base.__subclasses__())
    while stack:
        klass = stack.pop()
        if klass not in seen:
            seen.append(klass)
            stack.extend(klass.__subclasses__())
    return seen


class _OverridesEverything(StubAccount):
    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False): return 1
    def close_transaction(self, transaction_id): return {}
    def reduce_transaction(self, transaction_id, quantity): return {}
    def adjust_tp(self, transaction, new_tp_price, source=""): return True
    def adjust_sl(self, transaction, new_sl_price, source=""): return True
    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""): return True
    def get_account_snapshot(self): return None
    def get_positions(self): return []
    def get_instrument_current_price(self, symbol_or_symbols, price_type="bid"): return 1.0
    def has_pending_closing_order(self, transaction_id): return False
    def get_fractionable(self, symbols): return {s: True for s in symbols}


def test_every_known_account_subclass_is_tapped():
    classes = _all_subclasses(ReadOnlyAccountInterface)
    assert _OverridesEverything in classes and StubAccount in classes
    problems = [problem for klass in classes for problem in untapped_methods(klass)]
    assert problems == []


def test_a_class_overriding_every_tapped_method_comes_out_tapped():
    for name, (provider, _identity) in SIZING_READ_TAPPED_METHODS.items():
        resolved = getattr(_OverridesEverything, name)
        boundary = tapped_boundary(resolved)
        assert boundary is not None, name
        expected = OPTION_TAPPED_METHODS[name][0] if (
            name in OPTION_TAPPED_METHODS and False) else provider
        assert boundary == (expected, name)
    for name in EFFECT_METHODS:
        assert effect_boundary(getattr(_OverridesEverything, name)) == name


def test_an_untapped_override_is_caught_by_the_guard():
    class Sneaky(StubAccount):
        pass

    def plain(self, transaction_id, quantity):          # bypass the hook: a hand-rolled override
        return {}

    Sneaky.reduce_transaction = plain
    assert any("Sneaky.reduce_transaction" in p for p in untapped_methods(Sneaky))

    class Quote(StubAccount):
        pass

    Quote.get_instrument_current_price = lambda self, symbol_or_symbols, price_type="bid": 1.0
    assert any("Quote.get_instrument_current_price" in p for p in untapped_methods(Quote))


def test_retapping_is_idempotent_and_never_double_wraps():
    before = {name: getattr(_OverridesEverything, name) for name in EFFECT_METHODS}
    tap_account_methods(_OverridesEverything)
    tap_account_methods(_OverridesEverything)
    for name in EFFECT_METHODS:
        assert getattr(_OverridesEverything, name) is before[name]
        assert not hasattr(getattr(_OverridesEverything, name).__wrapped__, "_replay_effect_boundary")


def test_a_deeper_subclass_chain_is_not_wrapped_twice():
    class Deeper(_OverridesEverything):
        pass

    assert Deeper.submit_order is _OverridesEverything.submit_order
    assert Deeper.get_fractionable is _OverridesEverything.get_fractionable
    assert untapped_methods(Deeper) == []


def test_an_exempt_class_is_skipped():
    class Wrapper(StubAccount):
        _replay_taps_exempt = True

        def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False): return 1

    assert effect_boundary(Wrapper.submit_order) is None
    assert untapped_methods(Wrapper) == []


def test_options_accounts_keep_the_option_provider_for_the_overlapping_reads():
    """get_account_snapshot / get_positions on an options account stay "broker_options" (old
    recordings, option records byte-identical); the stock-only reads are the shared "broker"."""
    class OptionsStock(OptionsFakeAccount):
        def get_positions(self): return []
        def get_account_snapshot(self): return None

    assert tapped_boundary(OptionsStock.get_positions) == ("broker_options", "get_positions")
    assert tapped_boundary(OptionsStock.get_account_snapshot) == ("broker_options", "get_account_snapshot")
    assert tapped_boundary(StubAccount.get_positions) == ("broker", "get_positions")
    assert untapped_methods(OptionsStock) == []


def test_the_two_new_reads_and_the_overridden_quote_record_under_the_broker_provider():
    account = _OverridesEverything()
    store = RecordingStore()
    with capture_scope(store, _meta()):
        assert account.has_pending_closing_order(5) is False
        assert account.get_fractionable(["B", "A"]) == {"B": True, "A": True}
        assert account.get_instrument_current_price("XYZ", price_type="mid") == 1.0
        assert account.get_account_snapshot() is None
        assert account.get_positions() == []
    recorded = {o.observation.method: o.observation for o in store.submitted[0][2]}
    assert {o.provider for o in recorded.values()} == {"broker"}
    assert recorded["has_pending_closing_order"].request_identity["transaction_id"] == 5
    assert recorded["get_fractionable"].request_identity["symbols"] == ["A", "B"]
    quote = recorded["get_instrument_current_price"]
    assert quote.request_identity["price_type"] == "mid" and quote.tap_depth == 0
    assert set(recorded) == {"has_pending_closing_order", "get_fractionable",
                             "get_instrument_current_price", "get_account_snapshot", "get_positions"}


def test_an_override_that_calls_super_records_the_callers_identity_at_depth_zero():
    """The AlpacaAccount shape: normalise price_type, then call the tapped base method."""
    class Normalising(StubAccount):
        def get_instrument_current_price(self, symbol_or_symbols, price_type="bid"):
            return super().get_instrument_current_price(symbol_or_symbols, price_type="bid")

    store = RecordingStore()
    with capture_scope(store, _meta()):
        assert Normalising().get_instrument_current_price("XYZ", price_type="mid") == 100.0
    quotes = [o.observation for o in store.submitted[0][2]
              if o.observation.method == "get_instrument_current_price"]
    assert sorted((q.tap_depth, q.request_identity["price_type"]) for q in quotes) == [(0, "mid"), (1, "bid")]


def test_with_capture_off_every_tapped_method_is_a_plain_call():
    account = _OverridesEverything()
    assert account.has_pending_closing_order(5) is False
    assert account.get_fractionable(["A"]) == {"A": True}
    assert account.get_instrument_current_price("XYZ", price_type="mid") == 1.0
    assert account.submit_order(object()) == 1
    assert account.close_transaction(3) == {}
    assert account.adjust_tp(object(), 1.0) is True
    stub = StubAccount()
    assert stub.get_positions() == [] and stub.reduce_transaction  # inherited, unchanged
    store = RecordingStore()
    assert not store.submitted


def test_a_read_that_raises_is_recorded_as_an_error_and_reraised():
    class Failing(StubAccount):
        def has_pending_closing_order(self, transaction_id):
            raise ConnectionError("broker down")

    store = RecordingStore()
    with capture_scope(store, _meta()):
        with pytest.raises(ConnectionError, match="broker down"):
            Failing().has_pending_closing_order(5)
    (obs,) = [o.observation for o in store.submitted[0][2]]
    assert obs.method == "has_pending_closing_order"


# --- M1: a tapped name that is not a plain method must not break the import of the account ----------
def test_a_staticmethod_named_like_a_tapped_method_is_logged_and_listed_never_raised(monkeypatch):
    from ba2_common.core.replay import account_taps

    errors = []
    monkeypatch.setattr(account_taps.logger, "error", lambda msg, *a, **k: errors.append(msg))

    class _Static(StubAccount):                    # class creation must NOT raise
        @staticmethod
        def get_balance():
            return 1.0

    try:
        assert any("_Static.get_balance" in m for m in errors)
        problems = untapped_methods(_Static)
        assert any("_Static.get_balance" in p and "not a plain method" in p for p in problems), problems
    finally:
        _Static._replay_taps_exempt = True         # keep the process-wide "all classes tapped" guard clean
