from ba2_common.core.position_sizing import compute_risk_based_quantity, derive_stop_for_quantity


def test_risk_quantity_by_explicit_stop():
    # equity 100k, risk 1% => $1000 risk; stop $2 below $100 entry => 500 shares.
    out = compute_risk_based_quantity(100_000, 100.0, 1.0, stop_price=98.0)
    assert out["quantity"] == 500
    assert out["risk_per_share"] == 2.0


def test_risk_quantity_floored_by_min_stop_pct():
    # tight $0.50 stop on $100 => 0.5% stop, but min_stop_pct 7% floors risk/share to $7 => 142.
    out = compute_risk_based_quantity(100_000, 100.0, 1.0, stop_price=99.5, min_stop_pct=7.0)
    assert out["quantity"] == 142
    assert out.get("stop_floored") is True


def test_risk_quantity_capped_by_notional():
    out = compute_risk_based_quantity(1_000_000, 100.0, 1.0, stop_price=98.0,
                                      max_position_value=10_000)
    assert out["quantity"] == 100
    assert out["capped_by"] == "notional"


def test_derive_stop_reduces_qty_to_keep_min_stop():
    # 1000 shares of $100 at 1% of 100k = $1000 budget => $1 stop = 1% < 7% min,
    # so qty reduces to risk_dollars/min_stop_dist = 1000/7 = 142.
    out = derive_stop_for_quantity(100_000, 100.0, 1000, 1.0, is_long=True, min_stop_pct=7.0)
    assert out["quantity"] == 142
    assert out["rejected"] is False
    assert out["sl_price"] < 100.0


def test_a_zero_notional_ceiling_permits_no_position():
    """0 is the MOST restrictive value max_position_value takes, and it is falsy — so the
    truthiness guard skipped the cap on exactly the input that forbids the trade. The audit's
    case: equity 10k, price 100, stop 90, risk 1% sizes to 10 shares, and a $0 ceiling left
    all 10 standing."""
    out = compute_risk_based_quantity(10_000, 100.0, 1.0, stop_price=90.0,
                                      max_position_value=0)
    assert out["quantity"] == 0
    assert out["reason"]


def test_an_overdrawn_balance_permits_no_position():
    """A negative available_balance is an account that can spend nothing; the old `>= 0`
    guard skipped the cash cap for it, reading overdrawn as unlimited."""
    out = compute_risk_based_quantity(10_000, 100.0, 1.0, stop_price=90.0,
                                      available_balance=-1)
    assert out["quantity"] == 0
    assert out["reason"]


def test_an_absent_notional_ceiling_still_means_no_ceiling():
    """None must keep meaning "uncapped" — the fix must not turn a missing limit into 0."""
    out = compute_risk_based_quantity(10_000, 100.0, 1.0, stop_price=90.0,
                                      max_position_value=None)
    assert out["quantity"] == 10
    assert out["capped_by"] is None
