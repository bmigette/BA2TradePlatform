"""The entry-quote concession must actually convert a NO-FILL into a FILL (F3).

THE MEASURED DEFECT
-------------------
Option entry limits are quoted at the ANALYSIS bar's close, but the default ``next_bar_open``
fill model makes the NEXT bar's open cross that quote. And the quote is a MID: the historical
option store has ``bid == ask`` on every populated chain row (the parquet store has no bid/ask
column at all), so ``contract.ask``/``contract.bid`` are both just the close, while the
tradeable spread is MODELLED at fill time by ``option_spread_pct``. A seller therefore fills
only when the premium RISES by a whole modelled half-spread overnight -- backwards for decaying
OTM premium, so the DAY order expires and premium sellers structurally almost never trade.
Head-to-head on INTC Feb-Dec 2024: O_CSP got 6 trades under ``next_bar_open``, 9 under
``same_bar_close``; an earlier AAPL probe got 0 against 17.

WHAT THESE PIN
--------------
The CLOSURE across the two halves that must agree, composed from the REAL pieces -- the
account's ``option_modelled_half_spread`` seam, the shared ``entry_limit_with_concession``
arithmetic, and the real ``_option_fill_price``:

  * the seam answers with EXACTLY ``_option_half_spread`` on the AS-OF bar (not the fill bar --
    that would be look-ahead, and the two can differ when the thin-volume widening flips);
  * at 0.0 an entry quoted on a FLAT premium curve does NOT fill -- today's behaviour, and the
    reason the trade counts above are what they are;
  * at 1.0 the SAME entry on the SAME bars DOES fill, because the quote already sits at the
    touch ``_option_cross`` charges;
  * both directions: the buyer's concession and the seller's are mirror images.

"Flat premium" is the discriminating fixture on purpose. It isolates the SPREAD as the only
thing standing between the quote and the fill: with open == close, a concession of zero can
never clear and a concession of one always does, so the test cannot pass for the wrong reason
(a favourable overnight move).
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.services.backtest.backtest_account import BacktestAccount
from ba2_common.core.option_entry_quote import entry_limit_with_concession
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection, OrderType

_OCC = "AAPL240315C00180000"
_AS_OF_DAY = date(2024, 3, 5)
_FILL_DAY = date(2024, 3, 6)
_AS_OF = datetime(2024, 3, 5)

#: A FLAT premium: the analysis close and the next bar's open are the same number, so the only
#: thing between the quote and the fill is the modelled spread.
_PX = 4.00
_VOL = 500.0                 # >= _OPTION_SPREAD_LIQUID_VOLUME (no thin widening) and >= 10x
                             # the 1-contract order (the participation cap is not the gate)
_HALF = 0.10                 # 5% of 4.00 = 0.20 full width -> 0.10 per side
_SPOT = 181.0                # keeps every premium here arbitrage-consistent vs strike 180


class _StubOptions:
    """One contract, one bar per day. ``volumes`` may differ per day so the AS-OF vs FILL-DAY
    distinction is observable rather than assumed."""

    def __init__(self, px=_PX, volumes=None):
        self.px = px
        self.volumes = volumes or {_AS_OF_DAY: _VOL, _FILL_DAY: _VOL}

    def get_bar(self, occ_symbol, day):
        if occ_symbol != _OCC or day not in self.volumes:
            return None
        return {"open": self.px, "high": self.px, "low": self.px, "close": self.px,
                "volume": self.volumes[day], "strike": 180.0, "option_type": "call"}


class _StubPrice:
    def now(self):
        return _AS_OF

    def next_bar_date(self, symbol, as_of):
        return _FILL_DAY

    def bar_at(self, symbol, day):
        return {"open": _SPOT, "high": _SPOT, "low": _SPOT, "close": _SPOT}


def _acct(volumes=None, **cfg):
    base = {"starting_cash": 100_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
            "fill_model": "next_bar_open", "option_spread_pct": 5.0}
    base.update(cfg)
    a = BacktestAccount(id=1, price_source=_StubPrice(), settings=base)
    a._options = _StubOptions(volumes=volumes)
    return a


def _order(order_type, side, limit):
    return SimpleNamespace(
        id=1, symbol="AAPL", underlying_symbol="AAPL", contract_symbol=_OCC,
        order_type=order_type, limit_price=limit, side=side, quantity=1.0,
        multiplier=100, strike=180.0, option_type=OptionRight.CALL,
        position_intent="buy_to_open" if side == OrderDirection.BUY else "sell_to_open",
        parent_order_id=None,
    )


def _quoted(acct, side, fraction):
    """The limit the REAL entry path would submit: the builder's quote (the analysis close,
    because the cache's bid == ask == close) conceded through the REAL shared arithmetic,
    sized by the account's own modelled spread."""
    leg = OptionLeg(contract_symbol=_OCC, side=side)
    half = acct.option_modelled_half_spread(_OCC)
    return entry_limit_with_concession(_PX, [leg], [half], fraction)


# =========================================================================== #
# 1. the seam answers with the simulator's own model, on the AS-OF bar
# =========================================================================== #
def test_the_seam_is_exactly_the_fill_engines_own_half_spread():
    """Not a second cost model: the number an entry concedes against must be the number the
    fill will charge."""
    a = _acct()
    bar = a._options.get_bar(_OCC, _AS_OF_DAY)
    assert a.option_modelled_half_spread(_OCC) == a._option_half_spread(_PX, bar) == _HALF


def test_the_seam_reads_the_AS_OF_bar_not_the_fill_bar():
    """A quote struck from the FILL day's spread would be look-ahead. Here the fill day is
    thin (volume 3 < 100 -> _OPTION_SPREAD_THIN_MULT doubles the width) and the as-of day is
    not, so the two answers differ and the test can tell which one was used."""
    a = _acct(volumes={_AS_OF_DAY: _VOL, _FILL_DAY: 3.0})
    fill_bar = a._options.get_bar(_OCC, _FILL_DAY)
    assert a._option_half_spread(_PX, fill_bar) == pytest.approx(2 * _HALF)
    assert a.option_modelled_half_spread(_OCC) == pytest.approx(_HALF)


def test_the_seam_is_silent_when_it_cannot_model():
    """No provider / unknown contract -> None, and the caller then leaves the builder's quote
    exactly as it was."""
    a = _acct()
    assert a.option_modelled_half_spread("NOPE240315C00180000") is None
    a._options = None
    assert a.option_modelled_half_spread(_OCC) is None


def test_the_seam_is_zero_when_no_spread_is_configured():
    """``option_spread_pct`` unset is the documented exact no-op of the spread model, so the
    concession has nothing to give up and the gene cannot move a quote."""
    a = _acct(option_spread_pct=0.0)
    assert a.option_modelled_half_spread(_OCC) == 0.0
    assert _quoted(a, OrderDirection.SELL, 1.0) == _PX


# =========================================================================== #
# 2. THE HEADLINE: 1.0 fills where 0.0 does not, on a flat premium
# =========================================================================== #
def test_a_seller_quoting_the_mid_does_NOT_fill():
    """Today's behaviour. Quote 4.00, next bar's crossed bid 3.90 -> 3.90 < 4.00, no fill."""
    a = _acct()
    limit = _quoted(a, OrderDirection.SELL, 0.0)
    assert limit == _PX
    assert a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit), _AS_OF) is None


def test_a_seller_conceding_the_whole_spread_DOES_fill():
    """The fix. Quote 3.90 (the modelled bid), next bar's crossed bid 3.90 -> fills at 3.90."""
    a = _acct()
    limit = _quoted(a, OrderDirection.SELL, 1.0)
    assert limit == pytest.approx(_PX - _HALF)
    assert a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit),
        _AS_OF) == pytest.approx(_PX - _HALF)


def test_a_buyer_quoting_the_mid_does_NOT_fill():
    a = _acct()
    limit = _quoted(a, OrderDirection.BUY, 0.0)
    assert limit == _PX
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit), _AS_OF) is None


def test_a_buyer_conceding_the_whole_spread_DOES_fill():
    a = _acct()
    limit = _quoted(a, OrderDirection.BUY, 1.0)
    assert limit == pytest.approx(_PX + _HALF)
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit),
        _AS_OF) == pytest.approx(_PX + _HALF)


def test_the_fill_price_is_the_touch_not_the_conceded_limit_when_they_differ():
    """A concession buys a FILL, never a worse price than the market: conceding beyond the
    touch (here the premium DROPS overnight, so the buyer's crossed ask is below its limit)
    still fills at the crossed price, not at the limit."""
    a = _acct()
    a._options = _StubOptions(px=3.50)
    limit = 4.10                              # yesterday's conceded quote, today's ask is lower
    got = a._option_fill_price(_order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit), _AS_OF)
    assert got == pytest.approx(3.50 + a._option_half_spread(3.50, {"volume": _VOL}))
    assert got < limit


@pytest.mark.parametrize("fraction,fills", [(0.0, False), (0.25, False), (0.5, False),
                                            (0.75, False), (1.0, True)])
def test_the_band_is_monotone_on_a_flat_premium(fraction, fills):
    """Every GA level behaves consistently: on a perfectly flat premium only the FULL cross
    clears, and nothing in between fills where a larger concession would not."""
    a = _acct()
    limit = _quoted(a, OrderDirection.SELL, fraction)
    got = a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit), _AS_OF)
    assert (got is not None) is fills


# =========================================================================== #
# 3. the default cannot move an existing result
# =========================================================================== #
@pytest.mark.parametrize("side,order_type", [
    (OrderDirection.BUY, OrderType.BUY_LIMIT),
    (OrderDirection.SELL, OrderType.SELL_LIMIT),
])
@pytest.mark.parametrize("px", [3.50, 4.00, 4.50])
def test_the_default_reproduces_the_unconceded_quote_exactly(side, order_type, px):
    """Across a premium that falls, holds and rises overnight, the 0.0 quote IS the builder's
    own number and the fill outcome is whatever it always was."""
    a = _acct()
    a._options = _StubOptions(px=px)
    limit = _quoted(a, side, 0.0)
    assert limit == _PX                      # the builder's analysis-close quote, untouched
    assert a._option_fill_price(_order(order_type, side, limit), _AS_OF) == \
        a._option_fill_price(_order(order_type, side, _PX), _AS_OF)
