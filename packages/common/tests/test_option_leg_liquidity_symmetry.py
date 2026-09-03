"""Every leg of every option structure must be selected under the SAME liquidity gates.

WHY (2026-08-23 structure audit): ``min_volume`` was threaded to the protective/long WINGS
of the multi-leg structures but silently omitted on the risk-bearing SHORT legs — iron
condor shorts, short straddle, short strangle (x3 call sites), jade lizard shorts, the
butterfly BODY (the 2x short leg) and its lower long wing, and the ratio spread's long put.
That is the exact inverse of what a liquidity gate is for: the leg you can be assigned on
was the one chosen without a tradability floor.

The butterfly's lower wing was the worst case — an inline
``passes_liquidity(c, min_oi, max_spread)`` with only three positional args, so
``min_volume`` defaulted to ``None``. Measured against the real cache it picked BAC K=32.5
with ask=None (-> "Missing quotes for butterfly") and INTC K=41.5 at a five-week-STALE
$2.23 (-> a bogus -0.98 debit) where the volume-gated candidate was K=42.0 at $4.92.

This test spies on every selector entry point instead of asserting per-structure outcomes,
so a NEW structure or a NEW leg cannot reintroduce the asymmetry.
"""
from datetime import date
from types import SimpleNamespace

import pytest

import ba2_common.core.TradeActions as TA
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import (
    ExpertActionType, OptionRight, get_option_entry_action_values,
)
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

MIN_VOL = 25
MIN_OI = 10
MAX_SPREAD = 90.0


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "legsym.sqlite"))
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
        """The double's balance IS its CASH — the intent this file has always tested.

        Completed for OPT-L5: ``cash_available_for_delivery`` reads
        ``AccountSnapshot.cash`` and must never fall back to total equity, so a double
        that published only ``get_balance()`` left the delivery gate unmeasurable.
        """
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=1_000_000.0, equity=1_000_000.0, net_liquidation=1_000_000.0)

    def get_positions(self):
        # The BROKER's view of the same 200 shares the ``spy`` fixture makes
        # ``_held_equity_shares`` report. The covered-call cover gate reads this feed
        # (account-wide) rather than the expert's own filled buys, so a double publishing
        # only the platform's view has its covered call refused as uncovered — and this
        # file is about the LIQUIDITY GATES, which are consulted before that refusal but
        # whose spy assertions need the action to run to completion.
        return [{"symbol": "AAPL", "qty": 200.0, "asset_class": "us_equity"}]

    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot

    def get_current_price(self, symbol=None):
        return self._spot

    #: TWO expiries, and the near one exists for exactly one structure. Every single-expiry
    #: builder asks for a window that contains only 2024-06-21 and therefore sees the SAME
    #: list it always saw; the PMCC is the one action that fetches TWICE, once per leg, and
    #: without a second date its overlay window comes back empty and the builder refuses
    #: before selecting anything -- which would make this audit report "no selection
    #: happened" for a builder that is working correctly.
    NEAR_EXPIRY = date(2024, 6, 6)
    FAR_EXPIRY = date(2024, 6, 21)

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for expiry in (self.NEAR_EXPIRY, self.FAR_EXPIRY):
            if not (expiry_min <= expiry <= expiry_max):
                continue
            for s in range(70, 131, 5):
                if option_type == OptionRight.CALL:
                    otm = max(float(s) - self._spot, 0.0)
                    intrinsic = max(self._spot - float(s), 0.0)
                else:
                    otm = max(self._spot - float(s), 0.0)
                    intrinsic = max(float(s) - self._spot, 0.0)
                bid = max(0.2, 5.0 - 0.08 * otm) + intrinsic
                out.append(OptionContract(
                    symbol=f"{underlying}{expiry:%y%m%d}{s}{option_type.value[0].upper()}",
                    underlying=underlying, option_type=option_type, strike=float(s),
                    expiry=expiry, bid=round(bid, 4), ask=round(bid + 0.2, 4),
                    last=round(bid, 4), open_interest=1000, volume=500))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        order = SimpleNamespace(id=len(self.submitted) + 1, data={})
        self.submitted.append(option_strategy)
        return order

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None

    def check_option_buying_power(self, required):
        return True

    def available_option_buying_power(self):
        return 1_000_000.0


# The actions that OPEN a structure from a chain. ``roll_pmcc_short`` is excluded with
# ``close_option`` by the shared classifier: it manages a position that already exists and
# re-selects its overlay from the spec the ENTRY stamped, so running it from a bare set of
# entry kwargs asks it to build something it is not there to build.
ENTRY_ACTION_VALUES = sorted(get_option_entry_action_values())

#: The PMCC is the one structure that needs a SECOND expiry window and a PER-LEG strike pair
#: (its short must sit ABOVE its long, which one shared target cannot express). Every other
#: builder ignores both keys.
_PER_ACTION_EXTRAS = {
    ExpertActionType.OPEN_PMCC.value: dict(short_dte_min=1, short_dte_max=9,
                                           strike_param=[5.0, 15.0]),
}

# Recommendation stub: _consensus_target() reads data/price_at_date/expected_profit_percent.
_REC = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                       expected_profit_percent=None, recommended_action=None)


@pytest.fixture
def spy(monkeypatch):
    """Record the liquidity gates every selector call actually received."""
    calls = []

    def wrap_kw(name, real):
        def inner(*a, **kw):
            calls.append((name, kw.get("min_open_interest"), kw.get("max_spread_pct"),
                          kw.get("min_volume")))
            return real(*a, **kw)
        return inner

    real_pl = TA.passes_liquidity

    def wrap_pl(c, min_oi=None, max_spread=None, min_volume=None):
        calls.append(("passes_liquidity", min_oi, max_spread, min_volume))
        return real_pl(c, min_oi, max_spread, min_volume)

    for n in ("select_single", "select_vertical_spread", "select_wing"):
        monkeypatch.setattr(TA, n, wrap_kw(n, getattr(TA, n)))
    monkeypatch.setattr(TA, "passes_liquidity", wrap_pl)
    # covered_call / protective_put bail out BEFORE selecting anything when the expert holds
    # no shares; give them a 200-share lot so their selection path is exercised too.
    monkeypatch.setattr(TA._OptionEntryAction, "_held_equity_shares", lambda self: 200.0)
    return calls


@pytest.mark.parametrize("action_type", ENTRY_ACTION_VALUES)
def test_every_leg_of_every_structure_is_gated_identically(action_type, spy):
    acct = _Acct()
    kwargs = dict(
        strike_method="percent_otm", strike_param=5.0, dte_min=10, dte_max=40,
        sizing=20.0, min_open_interest=MIN_OI, max_spread_pct=MAX_SPREAD,
        min_volume=MIN_VOL, wing_width_pct=10.0)
    kwargs.update(_PER_ACTION_EXTRAS.get(action_type, {}))
    act = TA.create_action(
        ExpertActionType(action_type), "AAPL", acct, SimpleNamespace(), None,
        _REC, **kwargs)
    act.submit_to_broker = True
    act.execute()

    assert spy, f"{action_type}: no contract selection happened at all"
    bad = [c for c in spy if c[1:] != (MIN_OI, MAX_SPREAD, MIN_VOL)]
    assert not bad, (
        f"{action_type}: {len(bad)} of {len(spy)} selector calls ran with different "
        f"liquidity gates than configured ({MIN_OI}, {MAX_SPREAD}, {MIN_VOL}): {bad}")
