from datetime import date
from ba2_trade_platform.core.option_types import OptionContract
from ba2_trade_platform.core.types import OptionRight
from ba2_trade_platform.core import option_selector as sel


TODAY = date(2026, 1, 1)


def _c(strike, *, right=OptionRight.CALL, expiry=date(2026, 2, 20), delta=0.5,
       bid=2.0, ask=2.2, oi=1000):
    return OptionContract(
        symbol=f"X{int(strike)}{right.value[0].upper()}", underlying="X",
        option_type=right, strike=float(strike), expiry=expiry,
        bid=bid, ask=ask, last=(bid + ask) / 2 if bid and ask else None,
        implied_volatility=0.3, delta=delta, gamma=0.0, theta=0.0, vega=0.0,
        open_interest=oi, volume=100)


def test_passes_liquidity():
    c = _c(100, oi=50, bid=2.0, ask=2.1)   # spread_pct = 0.1/2.05*100 ~= 4.88
    assert sel.passes_liquidity(c, min_open_interest=10, max_spread_pct=10) is True
    assert sel.passes_liquidity(c, min_open_interest=100, max_spread_pct=10) is False  # OI too low
    assert sel.passes_liquidity(c, min_open_interest=10, max_spread_pct=2) is False    # spread too wide
    # missing OI fails an OI filter; no filters => passes
    assert sel.passes_liquidity(_c(100, oi=None), min_open_interest=10, max_spread_pct=None) is False
    assert sel.passes_liquidity(_c(100, oi=None), min_open_interest=None, max_spread_pct=None) is True


def test_filter_dte():
    chain = [_c(100, expiry=date(2026, 1, 10)),   # 9 dte
             _c(100, expiry=date(2026, 2, 20)),   # 50 dte
             _c(100, expiry=date(2026, 6, 1))]    # ~151 dte
    out = sel.filter_dte(chain, TODAY, dte_min=30, dte_max=60)
    assert [c.expiry for c in out] == [date(2026, 2, 20)]


def test_select_single_delta():
    chain = [_c(95, delta=0.65), _c(100, delta=0.50), _c(105, delta=0.30)]
    pick = sel.select_single(chain, method="delta", strike_param=0.30, spot=100,
                             option_type=OptionRight.CALL, dte_min=10, dte_max=90, today=TODAY)
    assert pick.strike == 105.0  # nearest |delta| to 0.30


def test_select_single_percent_otm_call():
    chain = [_c(100), _c(105), _c(110)]
    # 5% OTM from spot 100 -> target strike 105
    pick = sel.select_single(chain, method="percent_otm", strike_param=5.0, spot=100,
                             option_type=OptionRight.CALL, dte_min=10, dte_max=90, today=TODAY)
    assert pick.strike == 105.0


def test_select_single_consensus_target():
    chain = [_c(100), _c(110), _c(120)]
    pick = sel.select_single(chain, method="consensus_target", strike_param=None, spot=100,
                             target_price=112, option_type=OptionRight.CALL,
                             dte_min=10, dte_max=90, today=TODAY)
    assert pick.strike == 110.0  # nearest to 112


def test_select_single_filters_illiquid_and_returns_none():
    chain = [_c(100, oi=5, bid=2.0, ask=4.0)]  # low OI + wide spread
    pick = sel.select_single(chain, method="delta", strike_param=0.5, spot=100,
                             option_type=OptionRight.CALL, dte_min=10, dte_max=90, today=TODAY,
                             min_open_interest=100, max_spread_pct=10)
    assert pick is None


def test_select_single_wrong_type_excluded():
    chain = [_c(100, right=OptionRight.PUT, delta=-0.5)]
    pick = sel.select_single(chain, method="delta", strike_param=0.5, spot=100,
                             option_type=OptionRight.CALL, dte_min=10, dte_max=90, today=TODAY)
    assert pick is None


def test_select_vertical_spread_delta():
    # bull call debit spread: long higher-delta (lower strike), short lower-delta (higher strike)
    chain = [_c(100, delta=0.60), _c(105, delta=0.45), _c(110, delta=0.25), _c(115, delta=0.15)]
    res = sel.select_vertical_spread(chain, method="delta", long_param=0.45, short_param=0.25,
                                     spot=100, option_type=OptionRight.CALL,
                                     dte_min=10, dte_max=90, today=TODAY)
    assert res is not None
    long_leg, short_leg = res
    assert long_leg.strike == 105.0 and short_leg.strike == 110.0
    assert long_leg.strike < short_leg.strike            # debit call spread
    assert long_leg.expiry == short_leg.expiry           # same expiry


def test_select_vertical_spread_none_when_no_distinct_legs():
    chain = [_c(100, delta=0.50)]   # only one strike -> can't form a spread
    res = sel.select_vertical_spread(chain, method="delta", long_param=0.45, short_param=0.25,
                                     spot=100, option_type=OptionRight.CALL,
                                     dte_min=10, dte_max=90, today=TODAY)
    assert res is None


def test_select_vertical_spread_put_ordering():
    # bear/put debit spread: long HIGHER strike, short LOWER strike, both puts, same expiry
    chain = [_c(95, right=OptionRight.PUT, delta=-0.30),
             _c(100, right=OptionRight.PUT, delta=-0.45),
             _c(105, right=OptionRight.PUT, delta=-0.60)]
    res = sel.select_vertical_spread(chain, method="delta", long_param=0.45, short_param=0.30,
                                     spot=100, option_type=OptionRight.PUT,
                                     dte_min=10, dte_max=90, today=TODAY)
    assert res is not None
    long_leg, short_leg = res
    assert long_leg.strike == 100.0 and short_leg.strike == 95.0   # long > short for puts
    assert long_leg.expiry == short_leg.expiry


def test_select_vertical_spread_picks_earliest_expiry_with_two_strikes():
    # Early expiry has only ONE strike (can't form a spread); later expiry has two -> use later.
    e_early = date(2026, 1, 20)
    e_late = date(2026, 2, 20)
    chain = [_c(100, expiry=e_early, delta=0.50),                  # lone strike, early
             _c(105, expiry=e_late, delta=0.45),
             _c(110, expiry=e_late, delta=0.25)]
    res = sel.select_vertical_spread(chain, method="delta", long_param=0.45, short_param=0.25,
                                     spot=100, option_type=OptionRight.CALL,
                                     dte_min=5, dte_max=90, today=TODAY)
    assert res is not None
    long_leg, short_leg = res
    assert long_leg.expiry == e_late and short_leg.expiry == e_late
    assert long_leg.strike == 105.0 and short_leg.strike == 110.0


def test_select_vertical_spread_duplicate_strike_not_substituted():
    # Two distinct contracts at strike 105; ensure short isn't silently forced to a far strike.
    e = date(2026, 2, 20)
    chain = [_c(100, expiry=e, delta=0.30),
             _c(105, expiry=e, delta=0.45),   # candidate A @105
             _c(105, expiry=e, delta=0.55),   # candidate B @105 (duplicate strike)
             _c(110, expiry=e, delta=0.25)]
    res = sel.select_vertical_spread(chain, method="delta", long_param=0.45, short_param=0.25,
                                     spot=100, option_type=OptionRight.CALL,
                                     dte_min=5, dte_max=90, today=TODAY)
    assert res is not None
    long_leg, short_leg = res
    # long ~0.45 -> strike 105; short ~0.25 -> strike 110 (NOT forced down to 100)
    assert long_leg.strike == 105.0 and short_leg.strike == 110.0


def test_pick_is_deterministic_under_input_reorder():
    a = _c(95, delta=0.50)
    b = _c(105, delta=0.50)   # equidistant in delta to target 0.50
    p1 = sel.select_single([a, b], method="delta", strike_param=0.50, spot=100,
                           option_type=OptionRight.CALL, dte_min=10, dte_max=90, today=TODAY)
    p2 = sel.select_single([b, a], method="delta", strike_param=0.50, spot=100,
                           option_type=OptionRight.CALL, dte_min=10, dte_max=90, today=TODAY)
    assert p1.strike == p2.strike == 95.0   # lower strike wins the tie, regardless of order
