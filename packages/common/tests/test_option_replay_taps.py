"""Option read + sizing taps, auto-applied to every OptionsAccountInterface override.

A tap is pure passthrough: the wrapped method runs once, its value comes back by
identity, and nothing is recorded outside a capture scope (or while replaying). A
recording failure is counted in CaptureHealth and never reaches the caller.
"""
from datetime import date, datetime, timezone

import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.replay import codec
from ba2_common.core.replay.context import CaptureHealth, capture_scope
from ba2_common.core.replay.observe import OBSERVED_ATTRIBUTE, tapped_boundary
from ba2_common.core.replay.option_taps import OPTION_PROVIDER, OPTION_TAPPED_METHODS
from ba2_common.core.replay.schemas import ReplayStatus
from ba2_common.core.types import OptionRight

UTC_NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)

NINE = (
    "get_option_chain", "get_option_quote", "get_atm_implied_volatility",
    "get_option_underlying_price", "option_capital_equity", "get_account_snapshot",
    "check_option_buying_power", "get_positions", "check_short_put_assignment_capacity",
)

CONTRACT = OptionContract(
    symbol="AAPL261218C00200000", underlying="AAPL", option_type=OptionRight.CALL,
    strike=200.0, expiry=date(2026, 12, 18), bid=1.0, ask=1.2, delta=0.31)


class _Store:
    def __init__(self):
        self.health = CaptureHealth()
        self.submitted = []

    def submit(self, record, objects=None, observations=()):
        self.submitted.append((record, dict(objects or {}), list(observations)))


def _meta():
    return {
        "analysis_id": "a1", "attempt_id": "att-1", "session_id": "s1",
        "expert_class": "TradeManager", "expert_instance_id": 7, "symbol": "AAPL",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
        "scheduled_at": UTC_NOW, "started_at": UTC_NOW,
    }


class _Base:
    """Stands in for AccountInterface: owns the inherited get_account_snapshot."""

    def get_account_snapshot(self):
        return {"equity": 1000.0}


class _Acct(_Base, OptionsAccountInterface):
    """A concrete account overriding all nine methods; counts calls."""

    id = 42

    def __init__(self):
        self.calls = []
        self.chain = [CONTRACT]
        self.quote = OptionQuote(symbol=CONTRACT.symbol, bid=1.0, ask=1.2)

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        self.calls.append("chain")
        return self.chain

    def get_option_quote(self, contract_symbol):
        self.calls.append("quote")
        return self.quote

    def get_atm_implied_volatility(self, underlying):
        self.calls.append("iv")
        return 0.3

    def get_option_positions(self):
        return []

    def _submit_option_order_impl(self, trading_order, legs, quantity, limit_price):
        raise NotImplementedError

    def close_option_position(self, position, quantity=None, limit_price=None):
        raise NotImplementedError

    def get_option_underlying_price(self, symbol, price_type=None):
        return 201.5

    def option_capital_equity(self):
        return 1000.0

    def get_account_snapshot(self):
        return {"equity": 1000.0}

    def check_option_buying_power(self, required):
        return True

    def get_positions(self):
        return []

    def check_short_put_assignment_capacity(self, *, strike, contracts, multiplier=100,
                                            long_puts=()):
        return "ok"

    def get_instrument_current_price(self, symbol, price_type="bid"):
        return 1.0


def _decode(pending):
    encoded = codec.encode(pending.payload)
    return codec.decode(encoded.kind, encoded.data, encoded.meta)


def test_table_lists_the_nine_methods():
    assert set(OPTION_TAPPED_METHODS) == set(NINE)
    assert all(provider == OPTION_PROVIDER for provider, _ in OPTION_TAPPED_METHODS.values())


def test_every_override_carries_the_marker():
    for name in NINE:
        assert hasattr(getattr(_Acct, name), OBSERVED_ATTRIBUTE), name


def test_interface_concrete_sizing_helpers_are_tapped_in_place():
    for name in ("option_capital_equity", "check_option_buying_power",
                 "get_option_underlying_price", "check_short_put_assignment_capacity"):
        assert tapped_boundary(getattr(OptionsAccountInterface, name)) is not None, name


def test_inherited_untapped_method_is_tapped_on_the_concrete_class():
    class Inheriting(_Base, OptionsAccountInterface):
        id = 1

        def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                             strike_min=None, strike_max=None):
            return []

        def get_option_quote(self, contract_symbol):
            return None

        def get_atm_implied_volatility(self, underlying):
            return None

        def get_option_positions(self):
            return []

        def _submit_option_order_impl(self, *a, **k):
            raise NotImplementedError

        def close_option_position(self, *a, **k):
            raise NotImplementedError

        def get_positions(self):
            return []

    assert tapped_boundary(Inheriting.get_account_snapshot) is not None
    # the base's own body is untouched
    assert tapped_boundary(_Base.get_account_snapshot) is None
    store = _Store()
    with capture_scope(store, _meta()):
        assert Inheriting().get_account_snapshot() == {"equity": 1000.0}
    assert [o.observation.method for o in store.submitted[0][2]] == ["get_account_snapshot"]


def test_abstract_methods_stay_abstract():
    with pytest.raises(TypeError):
        OptionsAccountInterface()
    assert getattr(OptionsAccountInterface.get_option_chain, "__isabstractmethod__", False)


def test_chain_records_one_observation_that_round_trips():
    account = _Acct()
    store = _Store()
    with capture_scope(store, _meta()):
        result = account.get_option_chain(
            "AAPL", date(2026, 12, 1), date(2026, 12, 31), OptionRight.CALL, 150.0, None)
        assert result is account.chain
    observations = store.submitted[0][2]
    assert len(observations) == 1
    observation = observations[0]
    assert observation.observation.provider == OPTION_PROVIDER
    assert observation.observation.method == "get_option_chain"
    assert observation.observation.request_identity == {
        "account_class": "_Acct", "account_id": 42, "underlying": "AAPL",
        "expiry_min": "2026-12-01", "expiry_max": "2026-12-31",
        "option_type": OptionRight.CALL.value, "strike_min": 150.0, "strike_max": None,
    }
    assert _decode(observation) == [CONTRACT]
    assert account.calls == ["chain"]


def test_quote_payload_round_trips():
    account = _Acct()
    store = _Store()
    with capture_scope(store, _meta()):
        account.get_option_quote(CONTRACT.symbol)
    observation = store.submitted[0][2][0]
    assert observation.observation.request_identity["contract_symbol"] == CONTRACT.symbol
    assert _decode(observation) == account.quote


def test_underlying_price_identity_carries_price_type_only_when_the_signature_has_it():
    account = _Acct()
    store = _Store()
    with capture_scope(store, _meta()):
        account.get_option_underlying_price("AAPL")
        account.get_option_underlying_price("AAPL", "ask")
    first, second = (o.observation.request_identity for o in store.submitted[0][2])
    assert first["underlying"] == "AAPL" and first["price_type"] is None
    assert second["price_type"] == "ask"


def test_sizing_reads_are_recorded_with_their_arguments():
    account = _Acct()
    store = _Store()
    with capture_scope(store, _meta()):
        account.option_capital_equity()
        account.get_account_snapshot()
        account.get_positions()
        account.check_option_buying_power(250.0)
        account.check_short_put_assignment_capacity(
            strike=100.0, contracts=2, long_puts=[(90.0, 2)])
    by_method = {o.observation.method: o.observation.request_identity
                 for o in store.submitted[0][2]}
    assert by_method["option_capital_equity"] == {"account_class": "_Acct", "account_id": 42}
    assert by_method["check_option_buying_power"]["required"] == 250.0
    assert by_method["check_short_put_assignment_capacity"] == {
        "account_class": "_Acct", "account_id": 42, "strike": 100.0, "contracts": 2,
        "multiplier": 100, "long_puts": [[90.0, 2]],
    }


def test_no_identity_value_is_a_wall_clock_instant():
    account = _Acct()
    store = _Store()
    with capture_scope(store, _meta()):
        account.get_option_chain("AAPL", date(2026, 12, 1), date(2026, 12, 31))
        account.get_atm_implied_volatility("AAPL")
    now_year = str(datetime.now().year)
    for pending in store.submitted[0][2]:
        for value in pending.observation.request_identity.values():
            assert "T" not in str(value) or not str(value).startswith(now_year + "-"), value


def test_outside_a_scope_it_is_a_plain_passthrough_and_records_nothing():
    account = _Acct()
    result = account.get_option_chain("AAPL", date(2026, 12, 1), date(2026, 12, 31))
    assert result is account.chain
    assert account.calls == ["chain"]


def test_identity_failure_returns_the_value_and_counts_one_failure():
    class Odd(_Acct):
        # parameter names the identity function cannot map -> KeyError in identity()
        def get_option_quote(self, occ):
            return "quote"

    account = Odd()
    store = _Store()
    with capture_scope(store, _meta()):
        assert account.get_option_quote("X") == "quote"
    assert store.submitted[0][0].capture_failures == {"other": 1}
    assert store.health.count(CaptureHealth.OTHER) == 1
    assert store.submitted[0][2] == []


def test_wrapped_exception_propagates_untouched_and_unrecorded():
    class Boom(_Acct):
        def get_atm_implied_volatility(self, underlying):
            raise RuntimeError("broker down")

    store = _Store()
    with capture_scope(store, _meta()):
        with pytest.raises(RuntimeError, match="broker down"):
            Boom().get_atm_implied_volatility("AAPL")
    assert store.submitted[0][2] == []


def test_subclass_of_a_subclass_does_not_double_wrap():
    class Child(_Acct):
        pass

    for name in NINE:
        assert getattr(Child, name) is getattr(_Acct, name), name

    store = _Store()
    with capture_scope(store, _meta()):
        Child().get_option_chain("AAPL", date(2026, 12, 1), date(2026, 12, 31))
    assert len(store.submitted[0][2]) == 1


def test_exempt_class_is_left_alone():
    class Wrapper(OptionsAccountInterface):
        _replay_taps_exempt = True

        def get_option_chain(self, *a, **k):
            return []

        def get_option_quote(self, contract_symbol):
            return None

        def get_atm_implied_volatility(self, underlying):
            return None

        def get_option_positions(self):
            return []

        def _submit_option_order_impl(self, *a, **k):
            raise NotImplementedError

        def close_option_position(self, *a, **k):
            raise NotImplementedError

    assert tapped_boundary(Wrapper.get_option_quote) is None
    assert tapped_boundary(Wrapper.get_option_chain) is None
