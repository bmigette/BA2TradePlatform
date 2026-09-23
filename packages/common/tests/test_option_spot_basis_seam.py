"""Plan Part E2/E3: every option builder reads the spot and the share cover IN THE OPTION BASIS.

Option strikes are quoted AS TRADED. A backtest's equity book is split-ADJUSTED (FMP), so its
``get_instrument_current_price`` answers $55.17 for NFLX on 2024-05-01 while the chain is
struck around $553. The fix routes every option spot read through ONE account method,
``OptionsAccountInterface.get_option_underlying_price`` (live: ``get_instrument_current_price``
unchanged; backtest: close x as-traded factor), and every share-cover count through
``equity_shares_per_option_share``.

This file RUNS every entry action against an account whose two answers differ by 10x and
records the ``spot=`` every selector call received: one stale ``get_instrument_current_price``
read anywhere in a builder shows up as 55.17.
"""
from datetime import date
from types import SimpleNamespace

import pytest

import ba2_common.core.TradeActions as TA
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import (
    ExpertActionType, OptionRight, OrderDirection, get_option_entry_action_values,
)

ADJUSTED = 55.17        # FMP close, NFLX 2024-05-01 (back-adjusted for the 2025 10:1)
AS_TRADED = 551.7       # the same close in the chain's basis


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "spotbasis.sqlite"))
    db.init_db()
    yield


class _Acct(OptionsAccountInterface):
    """An account whose equity book is split-adjusted 10:1 against its option chain."""

    def __init__(self, factor=10.0):
        self.id = 1
        self.factor = factor
        self.submitted = []

    def decision_label(self):
        return date(2024, 5, 2)

    def get_balance(self):
        return 10_000_000.0

    def get_option_tradable_balance(self):
        return self.get_balance()

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=10_000_000.0, equity=10_000_000.0,
                               net_liquidation=10_000_000.0)

    def get_positions(self):
        return [{"symbol": "NFLX", "qty": 2000.0, "asset_class": "us_equity"}]

    # The equity book's price (adjusted) and the option basis price (as traded).
    def get_instrument_current_price(self, symbol, price_type=None):
        return ADJUSTED

    def get_option_underlying_price(self, symbol, price_type=None):
        return ADJUSTED * self.factor

    def equity_shares_per_option_share(self, underlying):
        return self.factor

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        spot = AS_TRADED
        for s in range(450, 651, 5):
            if option_type == OptionRight.CALL:
                otm = max(float(s) - spot, 0.0)
                intrinsic = max(spot - float(s), 0.0)
                delta = max(0.02, min(0.98, 0.5 - 0.005 * (float(s) - spot)))
            else:
                otm = max(spot - float(s), 0.0)
                intrinsic = max(float(s) - spot, 0.0)
                delta = -max(0.02, min(0.98, 0.5 - 0.005 * (spot - float(s))))
            bid = max(0.5, 20.0 - 0.3 * otm) + intrinsic
            out.append(OptionContract(
                symbol=f"{underlying}{s}{option_type.value[0].upper()}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 5, 24), bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=1000, volume=500,
                delta=round(delta, 4)))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None, **kw):
        self.submitted.append((option_strategy, quantity, legs))
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
        return 10_000_000.0


_REC = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                       expected_profit_percent=None, recommended_action=None)


@pytest.fixture
def spy(monkeypatch):
    seen = []

    def wrap(name, real):
        def inner(*a, **kw):
            if "spot" in kw:
                seen.append((name, kw["spot"]))
            return real(*a, **kw)
        return inner

    for name in ("select_single", "select_vertical_spread", "select_wing"):
        real = getattr(TA, name, None)
        if real is not None:
            monkeypatch.setattr(TA, name, wrap(name, real))
    return seen


def _run(action_type, acct, *, held=2000.0, monkeypatch=None, **extra):
    if monkeypatch is not None:
        monkeypatch.setattr(TA._OptionEntryAction, "_held_equity_shares", lambda self: held)
    act = TA.create_action(
        ExpertActionType(action_type), "NFLX", acct, SimpleNamespace(), None, _REC,
        strike_method="percent_otm", strike_param=5.0, dte_min=10, dte_max=40,
        sizing=2.0, min_open_interest=10, max_spread_pct=90.0, min_volume=25,
        wing_width_pct=10.0, short_dte_min=1, short_dte_max=9, **extra)
    act.submit_to_broker = True
    return act, act.execute()


@pytest.mark.parametrize("action_type", sorted(get_option_entry_action_values()))
def test_every_builder_selects_against_the_option_basis_spot(action_type, spy, monkeypatch):
    _run(action_type, _Acct(), monkeypatch=monkeypatch)
    spots = {round(float(s), 4) for _, s in spy if s is not None}
    assert spots, f"{action_type}: no spot-taking selection happened"
    assert spots == {AS_TRADED}, (
        f"{action_type} selected against {sorted(spots)} -- a builder read the equity "
        f"book's (adjusted) price instead of get_option_underlying_price")


def test_a_5pct_otm_call_on_nflx_2024_lands_near_580_not_58(spy, monkeypatch):
    acct = _Acct()
    _run(ExpertActionType.BUY_CALL.value, acct, monkeypatch=monkeypatch)
    (_, qty, legs), = acct.submitted
    assert 575.0 <= legs[0].strike <= 585.0, legs[0].strike


def test_live_default_is_get_instrument_current_price_unchanged():
    class _Live(_Acct):
        get_option_underlying_price = OptionsAccountInterface.get_option_underlying_price
        equity_shares_per_option_share = OptionsAccountInterface.equity_shares_per_option_share

        def get_instrument_current_price(self, symbol, price_type=None):
            return ("px", symbol, price_type)

    a = _Live()
    assert a.get_option_underlying_price("NFLX") == ("px", "NFLX", None)
    assert a.get_option_underlying_price("NFLX", "mid") == ("px", "NFLX", "mid")
    assert a.equity_shares_per_option_share("NFLX") == 1.0
    assert a.option_shares_in_equity_units("NFLX", 300) == 300


# ---- E3: cover counted in AS-TRADED shares ---------------------------------------------------
@pytest.mark.parametrize("held,expected", [(1000.0, 1), (1999.0, 1), (2000.0, 2), (999.0, 0)])
def test_covered_call_on_a_10to1_adjusted_book_writes_one_contract_per_1000_shares(
        held, expected, monkeypatch):
    """1,000 adjusted NFLX shares in 2024 are 100 real shares: ONE contract, not ten."""
    acct = _Acct()
    monkeypatch.setattr(OptionsAccountInterface, "check_cover_for_covered_call",
                        lambda self, legs, q, s: __import__(
                            "ba2_common.core.interfaces.OptionsAccountInterface",
                            fromlist=["CoverCapacity"]).CoverCapacity(True))
    _, result = _run(ExpertActionType.SELL_COVERED_CALL.value, acct, held=held,
                     monkeypatch=monkeypatch)
    if expected == 0:
        assert not acct.submitted
    else:
        (_, qty, _legs), = acct.submitted
        assert qty == expected


def test_protective_put_counts_contracts_in_as_traded_shares(monkeypatch):
    acct = _Acct()
    _run(ExpertActionType.BUY_PROTECTIVE_PUT.value, acct, held=3000.0, monkeypatch=monkeypatch)
    (_, qty, _legs), = acct.submitted
    assert qty == 3


def _cc_leg():
    return OptionLeg(contract_symbol="NFLX240524C00580000", side=OrderDirection.SELL,
                     position_intent="sell_to_open", option_type=OptionRight.CALL,
                     strike=580.0, expiry=date(2024, 5, 24), underlying="NFLX")


@pytest.mark.parametrize("contracts,held,ok", [(1, 1000, True), (2, 1000, False),
                                               (1, 999, False), (2, 2000, True)])
def test_the_account_wide_cover_guard_compares_in_one_unit(contracts, held, ok):
    acct = _Acct()
    acct.held_shares_for_cover = lambda u: held
    acct.shares_pledged_to_short_calls = lambda u: 0
    verdict = acct.check_cover_for_covered_call([_cc_leg()], contracts, "covered_call")
    assert verdict.ok is ok, verdict.reason


def test_an_open_pledge_is_charged_in_the_books_unit():
    """One open short call (100 as-traded shares) pledges 1,000 adjusted shares."""
    acct = _Acct()
    acct.held_shares_for_cover = lambda u: 2000
    acct.shares_pledged_to_short_calls = lambda u: 100
    assert acct.check_cover_for_covered_call([_cc_leg()], 1, "covered_call").ok
    assert not acct.check_cover_for_covered_call([_cc_leg()], 2, "covered_call").ok


def test_the_overlay_equity_lot_is_one_contracts_deliverable_in_the_books_unit():
    """O_CC / O_PP's lot_size=100 is 100 AS-TRADED shares: 1,000 on a 10:1-adjusted book,
    and the configured 100 on a live account (the interface default, k = 1)."""
    from ba2_common.core.types import OrderRecommendation

    class _Live(_Acct):
        equity_shares_per_option_share = OptionsAccountInterface.equity_shares_per_option_share

    assert TA.BuyAction("NFLX", _Acct(), OrderRecommendation.BUY,
                        lot_size=100)._equity_lot_size() == 1000
    lot = TA.BuyAction("NFLX", _Live(), OrderRecommendation.BUY, lot_size=100)._equity_lot_size()
    assert lot == 100 and type(lot) is int
