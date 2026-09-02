"""OPT-S2 — ``get_strike_method_action_values()`` must match what the BUILDERS read.

Thirteen of the twenty entry builders pass ``self.strike_method`` into the selector.
The other seven hard-code ``method="percent_otm"`` at every selection site, so
``self.strike_method`` is set on the shared base and never read.

That list is what the rule editor uses to decide whether to offer a Strike Method at all,
so it cannot be maintained by hand. This file pins it by RUNNING every entry action twice —
once configured ``percent_otm``, once ``delta`` — and recording the ``method=`` every
selector call actually received:

  * an action on the list must FOLLOW the configuration, and
  * an action off the list must ignore it, staying on ``percent_otm`` in both runs.

Reading the source for ``method=self.strike_method`` would prove only that the text exists;
one of these eight structures selects through three different call sites, and a builder that
honoured the setting on two of them and hard-coded the third would read as compliant.
"""
from datetime import date
from types import SimpleNamespace

import pytest

import ba2_common.core.TradeActions as TA
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import (
    ExpertActionType, OptionRight, get_option_action_values,
    get_strike_method_action_values, honours_strike_method,
)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "strikemethod.sqlite"))
    db.init_db()
    yield


class _Acct(OptionsAccountInterface):
    def __init__(self, spot=100.0):
        self.id = 1
        self._spot = spot
        self.submitted = []

    def _as_of_date(self):
        return date(2024, 6, 1)

    def get_balance(self):
        return 1_000_000.0

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=1_000_000.0, equity=1_000_000.0,
                               net_liquidation=1_000_000.0)

    def get_positions(self):
        return [{"symbol": "AAPL", "qty": 200.0, "asset_class": "us_equity"}]

    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot

    def get_current_price(self, symbol=None):
        return self._spot

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(70, 131, 5):
            if option_type == OptionRight.CALL:
                otm = max(float(s) - self._spot, 0.0)
                intrinsic = max(self._spot - float(s), 0.0)
                delta = max(0.02, min(0.98, 0.5 - 0.02 * (float(s) - self._spot)))
            else:
                otm = max(self._spot - float(s), 0.0)
                intrinsic = max(float(s) - self._spot, 0.0)
                delta = -max(0.02, min(0.98, 0.5 - 0.02 * (self._spot - float(s))))
            bid = max(0.2, 5.0 - 0.08 * otm) + intrinsic
            out.append(OptionContract(
                symbol=f"{underlying}{s}{option_type.value[0].upper()}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 6, 21), bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=1000, volume=500,
                delta=round(delta, 4)))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(option_strategy)
        return SimpleNamespace(id=len(self.submitted), data={})

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None,
                              transaction_id=None):
        return None

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return 1_000_000.0


ENTRY_ACTION_VALUES = sorted(set(get_option_action_values())
                             - {ExpertActionType.CLOSE_OPTION.value})

_REC = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                       expected_profit_percent=None, recommended_action=None)


@pytest.fixture
def spy(monkeypatch):
    """Record the ``method=`` every selector entry point actually received."""
    seen = []

    def wrap(name, real):
        def inner(*a, **kw):
            # Only calls that CHOOSE a strike by method are in scope. ``select_wing``
            # places a leg a fixed width away from another strike and takes no ``method``
            # at all — that is the ``wing_width_pct`` mechanism, pinned elsewhere.
            if "method" in kw:
                seen.append((name, kw["method"]))
            return real(*a, **kw)
        return inner

    for name in ("select_single", "select_vertical_spread", "select_wing"):
        real = getattr(TA, name, None)
        if real is not None:
            monkeypatch.setattr(TA, name, wrap(name, real))
    monkeypatch.setattr(TA._OptionEntryAction, "_held_equity_shares", lambda self: 200.0)
    return seen


def _methods_used(action_type, configured, spy):
    acct = _Acct()
    act = TA.create_action(
        ExpertActionType(action_type), "AAPL", acct, SimpleNamespace(), None, _REC,
        strike_method=configured, strike_param=5.0, dte_min=10, dte_max=40,
        sizing=2.0, min_open_interest=10, max_spread_pct=90.0, min_volume=25,
        wing_width_pct=10.0,
        # The PMCC's SECOND expiry window, passed unconditionally exactly as
        # ``wing_width_pct`` is: every other builder swallows it. It has to be strictly
        # nearer than ``dte_min`` or the builder refuses at the WINDOW, before any
        # selection, and this file measures selector calls. The fixture chain publishes one
        # expiry (2024-06-21, 20 DTE), so the overlay leg finds no contract inside [1,9] —
        # which is fine and is the point: BOTH of the PMCC's ``select_single`` calls are
        # still made with the configured method, and that is the quantity under test.
        short_dte_min=1, short_dte_max=9)
    act.submit_to_broker = True
    act.execute()
    return [m for _, m in spy]


@pytest.mark.parametrize("action_type", ENTRY_ACTION_VALUES)
def test_the_strike_method_list_matches_what_the_builder_reads(action_type, spy):
    used = _methods_used(action_type, "percent_otm", spy)
    assert used, f"{action_type}: no contract selection happened at all"
    baseline = set(used)

    spy.clear()
    used_delta = _methods_used(action_type, "delta", spy)
    assert used_delta, f"{action_type}: no contract selection on the delta run"
    changed = set(used_delta)

    if honours_strike_method(action_type):
        assert changed == {"delta"}, (
            f"{action_type} is listed as honouring strike_method, but configuring 'delta' "
            f"still selected with {sorted(changed)} — the list promises the editor an "
            f"option the builder does not read")
    else:
        assert baseline == changed == {"percent_otm"}, (
            f"{action_type} is NOT listed as honouring strike_method, yet the configured "
            f"method reached the selector ({sorted(changed)}). Either the builder was "
            f"fixed — add it to get_strike_method_action_values() — or it honours the "
            f"setting on some legs and not others, which is worse than ignoring it.")


def test_the_list_is_a_strict_subset_of_the_entry_actions():
    """A name on the list that is not an entry action would silently offer nothing."""
    assert set(get_strike_method_action_values()) <= set(ENTRY_ACTION_VALUES)
    # 12 on 2026-09-02 morning: the long STRANGLE learned the method (design 2026-08-31
    # section 2's "strangle width delta 0.25-0.45"). 13 the same day, +1 for ``open_pmcc``
    # (plan Task 6): both its legs are delta picks made with ``method=self.strike_method``,
    # one per expiry window. Entry actions went 19 -> 20 with it, so the hard-coded
    # remainder is unchanged at 20 - 13 = 7 (straddle, short straddle, short strangle, iron
    # condor, jade lizard, call butterfly, put ratio spread).
    assert len(get_strike_method_action_values()) == 13
    assert len(ENTRY_ACTION_VALUES) == 20
    assert len(set(ENTRY_ACTION_VALUES) - set(get_strike_method_action_values())) == 7
