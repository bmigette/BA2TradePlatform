"""A non-finite broker figure must not fail the available-balance clamp OPEN.

2026-09-09 review, finding 5 follow-up. ``MarketExpertInterface.get_available_balance``
caps its own virtual-equity figure at the account's REAL spendable balance
(``_get_actual_available_balance``), which reads ``get_account_info()``'s
buying-power-style fields. That read used to do a bare ``float(val)``, so a NaN came
back as a number -- and the clamp is ``if actual < available``, a comparison NaN always
loses. The cap silently stopped applying and the expert kept its larger virtual figure:
the same failure mode as the NaN margin multiplier, one layer up.

A non-finite value is now UNUSABLE (never a substituted number): it is logged at WARNING
and the reader falls through to the next candidate field name, so the existing fallback
order and the ``None`` = "nothing usable, do not clamp" contract are unchanged.

Fixtures follow tests/test_margin_expert_sizing.py: a hand-built account double, the
real ``MarketExpertInterface``, and the DB factories for the ExpertInstance row.
"""
import logging
import sys

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

from tests import factories


NAN = float("nan")


class _Account:
    """Only what get_virtual_balance / get_available_balance read (margin off)."""

    def __init__(self, id_val, *, balance, tradable, account_info):
        self.id = id_val
        self._balance, self._tradable = balance, tradable
        self._account_info = account_info

    def get_balance(self):
        return self._balance

    def get_tradable_balance(self):
        return self._tradable

    def get_account_info(self):
        return self._account_info

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        return {} if isinstance(symbol_or_list, (list, tuple, set)) else None


class _Expert(MarketExpertInterface):
    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "non-finite clamp test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _with_account(account, fn):
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    class _R:
        def get_account_instance(self, account_id):
            return account

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_R())
        return fn()
    finally:
        set_instance_resolver(prev)


@pytest.fixture
def warnings_logged(monkeypatch):
    """MarketExpertInterface's own ``logger.warning`` lines.

    NOT caplog: ba2_common's logger sets ``propagate = False`` so pytest's root handler
    never sees the record and the assertion would pass vacuously (the established idiom
    here: packages/common/tests/test_account_seams._capture_errors).
    """
    module = sys.modules["ba2_common.core.interfaces.MarketExpertInterface"]
    seen = []
    monkeypatch.setattr(module.logger, "warning",
                        lambda msg, *a, **k: seen.append(str(msg)))
    return seen


# ----- the reader, in isolation ---------------------------------------------

def test_a_nan_buying_power_falls_through_to_the_next_field(warnings_logged):
    account = _Account(4, balance=99_000.0, tradable=99_000.0,
                       account_info={"buying_power": NAN, "cash": 2_500.0})

    assert MarketExpertInterface._get_actual_available_balance(account) == 2_500.0
    assert len(warnings_logged) == 1
    assert "non-finite buying_power" in warnings_logged[0] and "Account 4" in warnings_logged[0]


def test_a_nan_buying_power_alone_falls_back_to_get_balance(warnings_logged):
    """Exactly as an absent or non-numeric field does today: nothing usable in the
    info dict, so the equity figure is the (less precise, still real) cap."""
    account = _Account(4, balance=250.0, tradable=250.0,
                       account_info={"buying_power": NAN})

    assert MarketExpertInterface._get_actual_available_balance(account) == 250.0
    assert len(warnings_logged) == 1


def test_finite_figures_are_read_exactly_as_before(warnings_logged):
    account = _Account(4, balance=999.0, tradable=999.0,
                       account_info={"buying_power": 5_000.0})

    assert MarketExpertInterface._get_actual_available_balance(account) == 5_000.0
    assert warnings_logged == []


def test_a_zero_buying_power_is_still_a_measured_zero(warnings_logged):
    """0.0 is an answer -- a fully deployed account -- and must NOT fall through to
    ``cash``, or the clamp would report money that is already spent."""
    account = _Account(4, balance=999.0, tradable=999.0,
                       account_info={"buying_power": 0.0, "cash": 2_500.0})

    assert MarketExpertInterface._get_actual_available_balance(account) == 0.0
    assert warnings_logged == []


# ----- through the real clamp ------------------------------------------------

@pytest.mark.usefixtures("reset_test_db")
def test_the_clamp_binds_on_the_next_usable_field_instead_of_failing_open(warnings_logged):
    """The whole point: virtual says 99k, the NaN buying power says nothing, and the
    account's cash (2.5k) is what the expert may actually spend."""
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)
    account = _Account(acct_def.id, balance=99_000.0, tradable=99_000.0,
                       account_info={"buying_power": NAN, "cash": 2_500.0})

    available = _with_account(account, _Expert(inst.id).get_available_balance)

    assert available == 2_500.0          # not the 99_000.0 the NaN used to let through
    assert any("non-finite buying_power" in msg for msg in warnings_logged)
