"""The ENTRY-QUOTE concession gene (F3): direction, default, and the live no-op.

WHAT IS BROKEN WITHOUT IT
-------------------------
An option entry quotes off the ANALYSIS bar and the default ``next_bar_open`` fill model makes
the NEXT bar cross that quote. In the historical option store the chain's quote is degenerate
(``bid == ask`` on every populated row; the parquet store has no bid/ask at all), so
``contract.ask`` and ``contract.bid`` are BOTH the analysis close -- a MID -- while the
tradeable spread is modelled at fill time. A seller therefore fills only if the premium RISES
by a whole modelled half-spread overnight, which for decaying OTM premium is exactly backwards:
the DAY order expires and premium sellers structurally almost never trade.

WHAT THESE PIN
--------------
1. The DEFAULT is an exact no-op -- ``entry_cross`` unset, None or 0.0 leaves the builder's own
   limit untouched, byte for byte, so no existing option result moves.
2. The DIRECTION, separately per shape and per side, because getting it backwards would make
   fills strictly RARER while looking like a fix:
      * a BUYER gives up by quoting HIGHER,
      * a SELLER gives up by quoting LOWER,
      * a multi-leg gives up by raising its NET in the ``+debit / -credit`` convention (pay
        more debit / take less credit -- the same direction in that one convention).
3. LIVE IS UNTOUCHED: an account with no modelled spread (every live one) concedes nothing,
   whatever the gene says.

The fill-side half of the story -- that 1.0 fills where 0.0 does not -- is pinned against the
real simulator in ``testplatform/backend/tests/backtest/test_option_entry_cross_fill.py``.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_entry_quote import (
    ENTRY_CROSS_FULL,
    ENTRY_CROSS_NEUTRAL,
    entry_limit_with_concession,
    quote_concession,
    validated_entry_cross,
)
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import ExpertActionType, OptionRight, OrderDirection

TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 7, 1)


def _leg(side, ratio=1, sym="X"):
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=ratio)


# =========================================================================== #
# 1. the pure arithmetic: the default, and the direction per shape
# =========================================================================== #
def test_the_neutral_value_is_zero_and_is_an_exact_no_op():
    """THE HEADLINE. 0.0 is the value that recovers today's behaviour; it must return the
    caller's own float, not a re-derived one, so no result can move by a rounding step."""
    assert ENTRY_CROSS_NEUTRAL == 0.0
    for limit in (0.37, 4.0, -1.25, 12.3456789):
        assert entry_limit_with_concession(
            limit, [_leg(OrderDirection.BUY)], [0.5], ENTRY_CROSS_NEUTRAL) == limit
        assert entry_limit_with_concession(
            limit, [_leg(OrderDirection.SELL)], [0.5], ENTRY_CROSS_NEUTRAL) == limit


def test_a_buyer_gives_up_by_quoting_higher():
    assert entry_limit_with_concession(
        4.00, [_leg(OrderDirection.BUY)], [0.10], 1.0) == pytest.approx(4.10)
    assert entry_limit_with_concession(
        4.00, [_leg(OrderDirection.BUY)], [0.10], 0.5) == pytest.approx(4.05)


def test_a_seller_gives_up_by_quoting_lower():
    """The direction that matters most: the measured defect is premium SELLERS never filling."""
    assert entry_limit_with_concession(
        4.00, [_leg(OrderDirection.SELL)], [0.10], 1.0) == pytest.approx(3.90)
    assert entry_limit_with_concession(
        4.00, [_leg(OrderDirection.SELL)], [0.10], 0.5) == pytest.approx(3.95)


def test_the_two_sides_move_in_OPPOSITE_directions():
    """Stated on its own, because a single shared sign would pass half the tests above by
    accident on a symmetric fixture."""
    buy = entry_limit_with_concession(4.00, [_leg(OrderDirection.BUY)], [0.10], 1.0)
    sell = entry_limit_with_concession(4.00, [_leg(OrderDirection.SELL)], [0.10], 1.0)
    assert buy > 4.00 > sell


def test_a_multileg_DEBIT_gives_up_by_paying_more():
    """+debit convention: a 2.00 net debit quoting away from the mid becomes a bigger debit."""
    legs = [_leg(OrderDirection.BUY, sym="A"), _leg(OrderDirection.SELL, sym="B")]
    assert entry_limit_with_concession(2.00, legs, [0.10, 0.06], 1.0) == pytest.approx(2.16)


def test_a_multileg_CREDIT_gives_up_by_taking_less():
    """-credit convention: a 2.00 net credit (-2.00) becomes a 1.84 credit (-1.84). The number
    goes UP in both cases -- that is the whole reason the stack keeps one signed convention."""
    legs = [_leg(OrderDirection.SELL, sym="A"), _leg(OrderDirection.BUY, sym="B")]
    got = entry_limit_with_concession(-2.00, legs, [0.10, 0.06], 1.0)
    assert got == pytest.approx(-1.84)
    assert got > -2.00                       # less credit, i.e. a concession


def test_a_ratio_leg_concedes_in_proportion():
    """The parent's net is Σ ±premium x ratio, so a 1-2 put ratio concedes twice on its
    doubled leg. Weighting by 1 everywhere would under-charge exactly the ratio'd shapes."""
    legs = [_leg(OrderDirection.BUY, ratio=1, sym="A"),
            _leg(OrderDirection.SELL, ratio=2, sym="B")]
    assert quote_concession(legs, [0.10, 0.05], 1.0) == pytest.approx(0.20)


def test_the_concession_scales_linearly_between_the_two_ends():
    legs = [_leg(OrderDirection.BUY)]
    full = quote_concession(legs, [0.10], ENTRY_CROSS_FULL)
    assert quote_concession(legs, [0.10], 0.25) == pytest.approx(0.25 * full)
    assert quote_concession(legs, [0.10], 0.75) == pytest.approx(0.75 * full)


# =========================================================================== #
# 2. the floors
# =========================================================================== #
def test_a_single_leg_sell_never_quotes_below_zero():
    """Mirrors ``_option_cross``'s own ``max(0.0, px - half)``: a modelled spread wider than
    the premium must not ask the account to pay in order to sell."""
    assert entry_limit_with_concession(
        0.05, [_leg(OrderDirection.SELL)], [0.40], 1.0) == 0.0


def test_a_multileg_credit_never_becomes_a_debit():
    """The parent's recorded side is derived from the SIGN of this number
    (``submit_option_order``: ``BUY if limit_price >= 0``), so a concession bigger than the
    structure's own credit must not silently re-sign the order. Clamped instead -- and such an
    order simply will not fill, because the achieved net pays that same spread."""
    legs = [_leg(OrderDirection.SELL, sym="A"), _leg(OrderDirection.BUY, sym="B")]
    assert entry_limit_with_concession(-0.02, legs, [0.10, 0.06], 1.0) == 0.0


# =========================================================================== #
# 3. the band is validated, not silently clamped
# =========================================================================== #
@pytest.mark.parametrize("bad", [-0.01, 1.01, 5.0, -1.0])
def test_a_fraction_outside_the_band_is_refused(bad):
    with pytest.raises(ValueError):
        validated_entry_cross(bad)


@pytest.mark.parametrize("good", [0.0, 0.25, 0.5, 1.0])
def test_every_band_level_is_accepted(good):
    assert validated_entry_cross(good) == good


def test_a_non_numeric_fraction_is_refused():
    with pytest.raises(ValueError):
        validated_entry_cross("0.5")


def test_one_half_spread_per_leg_is_required():
    """A silently truncated zip would under-concede on a 4-leg condor and look correct."""
    with pytest.raises(ValueError):
        quote_concession([_leg(OrderDirection.BUY, sym="A"),
                          _leg(OrderDirection.SELL, sym="B")], [0.10], 1.0)


# =========================================================================== #
# 4. the REAL builders, driven end to end through an account that models a spread
# =========================================================================== #
@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These builders persist TradeActionResult rows; sibling DB-seam tests repoint the global
    seam without restoring it, so take a fresh sqlite per test."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "entry_cross.sqlite"))
    db.init_db()
    yield


class _FakeAccount(OptionsAccountInterface):
    """Options account whose chain is a DEGENERATE quote (bid == ask == last), exactly like the
    historical option store, plus the backtest's ``option_modelled_half_spread`` seam.

    ``half`` is what the simulator's spread model would charge on the as-of bar. Set
    ``models_spread=False`` to get a LIVE-shaped account: same chain, no seam.
    """

    def __init__(self, spot=100.0, balance=50_000_000.0, premium=5.0, half=0.10,
                 models_spread=True):
        self.id = 1
        self.spot = spot
        self._balance = balance
        self.premium = premium
        self.half = half
        self.submitted = []
        if not models_spread:
            # The live shape: no modelled spread to concede against.
            self.option_modelled_half_spread = None

    def option_modelled_half_spread(self, contract_symbol):
        return self.half

    def open_option_orders_book_wide(self):
        return []

    def get_balance(self):
        return self._balance

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self._balance, equity=self._balance,
                               net_liquidation=self._balance)

    def _as_of_date(self):
        return TODAY

    def get_instrument_current_price(self, symbol, price_type=None):
        return self.spot

    def get_current_price(self, symbol=None):
        return self.spot

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(60, 141, 5):
            px = round(self.premium * (0.85 ** (abs(float(s) - self.spot) / 5.0)), 4)
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type == OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=EXPIRY, bid=px, ask=px,      # DEGENERATE, like the cache
                last=px, open_interest=1000, volume=1000))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(quantity=quantity, limit_price=limit_price,
                                   strategy=option_strategy, legs=list(legs)))
        return SimpleNamespace(id=len(self.submitted), data={})

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


def _act(acct, action_type, **kw):
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    a = create_action(ExpertActionType(action_type), "XYZ", acct,
                      SimpleNamespace(), None, rec, **kw)
    a.submit_to_broker = True
    return a


CSP = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40, sizing=5.0)
LONG_CALL = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
                 sizing=5.0)


def _submitted_limit(acct, action_type, params, **kw):
    res = _act(acct, action_type, **params, **kw).execute()
    assert res["success"] is True, res["message"]
    return acct.submitted[-1]["limit_price"]


def test_the_real_CSP_builder_quotes_LOWER_when_it_concedes():
    """A cash-secured put is the structure the defect was measured on. Its limit is
    ``contract.bid``, which in this (and the real) cache IS the close."""
    base = _submitted_limit(_FakeAccount(), "sell_cash_secured_put", CSP)
    conceded = _submitted_limit(_FakeAccount(), "sell_cash_secured_put", CSP, entry_cross=1.0)
    assert conceded == pytest.approx(base - 0.10)
    assert conceded < base


def test_the_real_long_call_builder_quotes_HIGHER_when_it_concedes():
    base = _submitted_limit(_FakeAccount(), "buy_call", LONG_CALL)
    conceded = _submitted_limit(_FakeAccount(), "buy_call", LONG_CALL, entry_cross=1.0)
    assert conceded == pytest.approx(base + 0.10)
    assert conceded > base


@pytest.mark.parametrize("action_type,params", [
    ("sell_cash_secured_put", CSP),
    ("buy_call", LONG_CALL),
])
@pytest.mark.parametrize("gene", [None, 0.0])
def test_the_default_submits_the_IDENTICAL_limit(action_type, params, gene):
    """The requirement in one line: the default must recover today's behaviour EXACTLY."""
    base = _submitted_limit(_FakeAccount(), action_type, params)
    kw = {} if gene is None else {"entry_cross": gene}
    assert _submitted_limit(_FakeAccount(), action_type, params, **kw) == base


def test_a_LIVE_account_concedes_nothing_even_at_full_cross():
    """No modelled spread -> no concession, whatever the gene says. A live account already
    quotes at the real touch (buy@ask / sell@bid), so conceding again would cross twice."""
    base = _submitted_limit(_FakeAccount(models_spread=False), "sell_cash_secured_put", CSP)
    full = _submitted_limit(_FakeAccount(models_spread=False), "sell_cash_secured_put", CSP,
                            entry_cross=1.0)
    assert full == base


def test_the_concession_is_recorded_on_the_result_only_when_it_bites():
    """Persisted ``TradeActionResult.data`` must keep its exact historical shape on a default
    run, and must show the SIGNED move when the gene is on."""
    acct = _FakeAccount()
    plain = _act(acct, "sell_cash_secured_put", **CSP).execute()
    assert "entry_cross" not in plain["data"]
    assert "entry_quote_concession" not in plain["data"]

    acct2 = _FakeAccount()
    conceded = _act(acct2, "sell_cash_secured_put", entry_cross=1.0, **CSP).execute()
    assert conceded["data"]["entry_cross"] == 1.0
    assert conceded["data"]["entry_quote_concession"] == pytest.approx(-0.10)


def test_a_multileg_credit_builder_concedes_on_EVERY_leg():
    """A bull put spread crosses two quotes to open, so the net gives up two half-spreads."""
    base = _submitted_limit(
        _FakeAccount(), "open_bull_put_spread",
        dict(strike_method="percent_otm", strike_param=5.0, dte_min=10, dte_max=40,
             sizing=2.0, wing_width_pct=5.0))
    conceded = _submitted_limit(
        _FakeAccount(), "open_bull_put_spread",
        dict(strike_method="percent_otm", strike_param=5.0, dte_min=10, dte_max=40,
             sizing=2.0, wing_width_pct=5.0), entry_cross=1.0)
    assert conceded == pytest.approx(base + 0.20)      # 2 legs x 0.10
    assert conceded > base                              # less credit


def test_an_out_of_band_gene_never_reaches_the_broker(monkeypatch):
    """A hand-written 5.0 must not silently run a campaign at five spreads of concession.

    Under the default ``BA2_ERROR_MODE=enforce`` it propagates (the loudest outcome there is);
    the property pinned here is the mode-independent one -- NOTHING is submitted.
    """
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    acct = _FakeAccount()
    with pytest.raises(ValueError):
        _act(acct, "sell_cash_secured_put", entry_cross=5.0, **CSP).execute()
    assert not acct.submitted


# =========================================================================== #
# 5. the wiring hops (config key -> ctor kwarg)
# =========================================================================== #
def test_the_rule_builder_maps_the_strategy_key_onto_the_ctor_kwarg():
    from ba2_common.core.rule_builders import action_from_rule

    cfg = action_from_rule({"action_type": "sell_cash_secured_put",
                            "option_entry_cross": 0.75})["act"]
    assert cfg["entry_cross"] == 0.75


def test_the_evaluator_forwards_it_to_the_action():
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS

    assert "entry_cross" in _OPTION_ENTRY_PARAM_KEYS
