"""Unit tests for the PURE per-name EQUITY-loss stop math (``stop_loss_sells``).

FactorRanker is a BYPASS expert: it sizes by weight and skips the classic risk
manager, so between rebalances a held name has NO downside protection. The product
fix is a per-name stop that reuses ``risk_per_trade_pct`` as a max-loss-per-name cap
measured in % of TOTAL EQUITY (NOT a % of the stock's price):

    a name is stopped when  held_qty * (avg_entry_cost - price) >= equity * risk_pct/100

These tests pin the pure math (no DB / no account needed) the way the existing
``rebalance_deltas`` math would be tested: dollar-loss vs the equity cap, boundary
(>=), skips, and a concrete equity-scaling case.
"""
from ba2_experts.FactorRanker.portfolio import stop_loss_sells


def test_loss_exactly_at_cap_is_stopped():
    # equity 100k, risk 1% -> cap $1000. qty 100, cost 50, price 40 -> loss = 100*(50-40)=1000 == cap.
    out = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 40.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {"AAA": 100}


def test_loss_just_under_cap_is_not_stopped():
    # price 40.01 -> loss = 100*(50-40.01) = 999.0 < 1000 cap -> not stopped.
    out = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 40.01}, equity=100_000.0, risk_pct=1.0)
    assert out == {}


def test_name_above_entry_not_stopped():
    # trading ABOVE entry -> unrealized GAIN, never stopped.
    out = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 60.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {}


def test_name_at_entry_not_stopped():
    out = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 50.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {}


def test_missing_price_skipped():
    out = stop_loss_sells({"AAA": (50.0, 100)}, {}, equity=100_000.0, risk_pct=1.0)
    assert out == {}


def test_nonpositive_price_skipped():
    out = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 0.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {}
    out = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": -5.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {}


def test_nonpositive_cost_skipped():
    out = stop_loss_sells({"AAA": (0.0, 100)}, {"AAA": 1.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {}
    out = stop_loss_sells({"AAA": (-5.0, 100)}, {"AAA": 1.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {}


def test_nonpositive_qty_skipped():
    out = stop_loss_sells({"AAA": (50.0, 0)}, {"AAA": 1.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {}
    out = stop_loss_sells({"AAA": (50.0, -10)}, {"AAA": 1.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {}


def test_nonpositive_risk_pct_no_stops():
    assert stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 1.0}, equity=100_000.0, risk_pct=0.0) == {}
    assert stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 1.0}, equity=100_000.0, risk_pct=-1.0) == {}


def test_nonpositive_equity_no_stops():
    assert stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 1.0}, equity=0.0, risk_pct=1.0) == {}
    assert stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 1.0}, equity=-1.0, risk_pct=1.0) == {}


def test_multiple_names_only_breached_returned_full_qty():
    positions = {
        "AAA": (50.0, 100),   # cap $1000: price 40 -> loss 1000 -> STOP
        "BBB": (50.0, 100),   # price 45 -> loss 500 < 1000 -> keep
        "CCC": (20.0, 200),   # price 16 -> loss 200*4 = 800 < 1000 -> keep
        "DDD": (10.0, 500),   # price 8 -> loss 500*2 = 1000 == cap -> STOP
    }
    prices = {"AAA": 40.0, "BBB": 45.0, "CCC": 16.0, "DDD": 8.0}
    out = stop_loss_sells(positions, prices, equity=100_000.0, risk_pct=1.0)
    assert out == {"AAA": 100, "DDD": 500}


def test_returned_qty_is_int():
    out = stop_loss_sells({"AAA": (50.0, 100.0)}, {"AAA": 40.0}, equity=100_000.0, risk_pct=1.0)
    assert out == {"AAA": 100}
    assert all(isinstance(v, int) for v in out.values())


def test_equity_scaling_5pct_weight_needs_20pct_price_drop():
    """Concrete equity-scaling case (from the spec). equity=100000, risk_pct=1.0 -> cap $1000.
    A 5%-weight name (qty 100, cost $50 = $5000 value) needs a ~20% price drop to lose $1000:
    price $40 (20% drop) stops; price $41 (18% drop, loss $900) does NOT.
    """
    stops_at_40 = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 40.0}, equity=100_000.0, risk_pct=1.0)
    assert stops_at_40 == {"AAA": 100}
    no_stop_at_41 = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": 41.0}, equity=100_000.0, risk_pct=1.0)
    assert no_stop_at_41 == {}


def test_price_none_explicit_skipped():
    out = stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": None}, equity=100_000.0, risk_pct=1.0)
    assert out == {}
