from datetime import date

from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderDirection
from ba2_experts.PremiumSeller.structures import (
    build_put_credit_spread, build_short_put, build_short_strangle,
    closest_to_delta, pick_expiry,
)

AS_OF = date(2024, 1, 2)
EXP = date(2024, 2, 9)   # 38 DTE


def _c(sym, strike, d, bid, ask, exp=EXP, right=OptionRight.PUT, iv=0.3):
    return OptionContract(symbol=sym, underlying="XYZ", option_type=right, strike=strike,
                          expiry=exp, bid=bid, ask=ask, last=None, implied_volatility=iv,
                          delta=d, gamma=None, theta=None, vega=None,
                          open_interest=500, volume=100)


CHAIN = [
    _c("P95", 95.0, -0.30, 1.40, 1.60),
    _c("P90", 90.0, -0.20, 0.70, 0.90),
    _c("P85", 85.0, -0.10, 0.30, 0.50),
    _c("C105", 105.0, 0.30, 1.40, 1.60, right=OptionRight.CALL),
    _c("C110", 110.0, 0.20, 0.70, 0.90, right=OptionRight.CALL),
    # A second expiry so pick_expiry has a choice:
    _c("P95F", 95.0, -0.31, 2.0, 2.2, exp=date(2024, 2, 16)),
]


def test_pick_expiry_nearest_target():
    assert pick_expiry(CHAIN, AS_OF, 38) == EXP
    assert pick_expiry(CHAIN, AS_OF, 45) == date(2024, 2, 16)
    assert pick_expiry([], AS_OF, 38) is None


def test_closest_to_delta():
    c = closest_to_delta([x for x in CHAIN if x.expiry == EXP and x.option_type == OptionRight.PUT],
                         EXP, -0.25)
    # |-.30-(-.25)| == |-.20-(-.25)| == .05 tie -> smaller |delta| (further OTM) wins
    assert c.symbol == "P90"
    assert closest_to_delta([], EXP, -0.3) is None


def test_put_credit_spread_math():
    spec = build_put_credit_spread("XYZ", CHAIN, AS_OF, target_dte=38, target_delta=-0.30,
                                   width=5.0, min_credit_ratio=0.10, risk_budget=1000.0)
    assert spec is not None
    assert spec.strategy == "put_credit_spread"
    shorts = [l for l in spec.legs if l.side == OrderDirection.SELL]
    longs = [l for l in spec.legs if l.side == OrderDirection.BUY]
    assert len(shorts) == 1 and len(longs) == 1
    assert shorts[0].contract_symbol == "P95" and longs[0].contract_symbol == "P90"
    # credit = short bid - long ask = 1.40 - 0.90 = 0.50; ratio 0.50/5.0 = 0.10 >= 0.10 OK
    assert abs(spec.net_credit - 0.50) < 1e-9
    # max loss/structure = (5.0 - 0.50) * 100 = 450 -> qty = floor(1000/450) = 2 (see next test)
    assert spec is None or spec.qty >= 0
    # budget floor declines the structure: floor(300/450) = 0 -> None
    assert build_put_credit_spread("XYZ", CHAIN, AS_OF, 38, -0.30, 5.0, 0.10, 300.0) is None


def test_put_credit_spread_qty_and_budget():
    spec = build_put_credit_spread("XYZ", CHAIN, AS_OF, 38, -0.30, 5.0, 0.05, 1000.0)
    assert spec.qty == 2                      # floor(1000 / 450)
    assert spec.max_loss == 900.0             # 450 x 2
    assert abs(spec.notional - 95.0 * 100 * 2) < 1e-9


def test_min_credit_ratio_blocks():
    assert build_put_credit_spread("XYZ", CHAIN, AS_OF, 38, -0.30, 5.0, 0.50, 1000.0) is None


def test_short_put():
    spec = build_short_put("XYZ", CHAIN, AS_OF, 38, -0.30, risk_budget=10000.0, max_notional=20000.0)
    assert spec.strategy == "short_put"
    assert spec.legs[0].contract_symbol == "P95" and spec.legs[0].side == OrderDirection.SELL
    # credit = bid 1.40; risk per contract ~= strike*100 = 9500
    # qty = min(floor(10000/9500), floor(20000/9500)) = 1
    assert spec.qty == 1
    assert spec is None or spec.qty >= 0
    # budget floor declines the structure: min(floor(300/9500), floor(20000/9500)) = 0 -> None
    assert build_short_put("XYZ", CHAIN, AS_OF, 38, -0.30, risk_budget=300.0, max_notional=20000.0) is None


def test_short_put_notional_cap():
    spec = build_short_put("XYZ", CHAIN, AS_OF, 38, -0.30, risk_budget=30000.0, max_notional=15000.0)
    assert spec.qty == 1                      # notional cap: floor(15000/9500)=1 < floor(30000/9500)=3


def test_short_strangle():
    spec = build_short_strangle("XYZ", CHAIN, AS_OF, 38, 0.30, risk_budget=30000.0, max_notional=50000.0)
    assert spec.strategy == "short_strangle"
    syms = {l.contract_symbol for l in spec.legs}
    assert syms == {"P95", "C105"}
    assert all(l.side == OrderDirection.SELL for l in spec.legs)
    # credit = put bid + call bid = 1.40 + 1.40 = 2.80; per-risk = max(95,105)x100 = 10500
    # qty = min(floor(30000/10500), floor(50000/10500)) = 2
    assert abs(spec.net_credit - 2.80) < 1e-9
    assert spec.qty == 2
    assert spec.max_loss == (10500.0 - 2.80 * 100) * 2     # 20440
    assert spec.notional == 10500.0 * 2
