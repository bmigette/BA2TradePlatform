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


# ---------------------------------------------------------------------------------------------
# resolve_sizing_risk_budget_pct — the ONE sizing-budget resolver shared by the classic risk
# manager (TradeRiskManagement._risk_atr_quantity) and the live Smart Risk Manager
# (SmartRiskManagerToolkit). Review finding 1 (2026-09-09): they read different settings, so the
# same config sized 18 shares in a backtest and 180 shares live.
# ---------------------------------------------------------------------------------------------

def _settings_getter(**settings):
    """A get_setting(key) callable over an explicit dict (missing key -> None, as the expert's
    get_setting_with_interface_default returns for an undeclared setting)."""
    return lambda key: settings.get(key)


def test_budget_resolver_prefers_atr_risk_budget_pct():
    from ba2_common.core.position_sizing import resolve_sizing_risk_budget_pct
    got = resolve_sizing_risk_budget_pct(
        _settings_getter(atr_risk_budget_pct=0.5, risk_per_trade_pct=5.0))
    assert got == 0.5


def test_budget_resolver_falls_back_to_risk_per_trade_pct_when_budget_unset():
    """A config that never declared the budget gene must behave exactly as before it existed."""
    from ba2_common.core.position_sizing import resolve_sizing_risk_budget_pct
    got = resolve_sizing_risk_budget_pct(
        _settings_getter(atr_risk_budget_pct=None, risk_per_trade_pct=5.0))
    assert got == 5.0


def test_budget_resolver_defaults_to_one_percent_when_both_unset():
    from ba2_common.core.position_sizing import resolve_sizing_risk_budget_pct
    assert resolve_sizing_risk_budget_pct(_settings_getter()) == 1.0


def test_budget_resolver_reads_zero_as_one_percent():
    """Documented QUIRK, pinned deliberately: ``float(x or 1.0)`` turns a 0 budget into 1.0
    rather than into "no position". Every backtest result depends on this historical behaviour,
    so the shared resolver keeps it instead of quietly fixing it here."""
    from ba2_common.core.position_sizing import resolve_sizing_risk_budget_pct
    got = resolve_sizing_risk_budget_pct(
        _settings_getter(atr_risk_budget_pct=0, risk_per_trade_pct=5.0))
    assert got == 1.0


def test_budget_resolver_coerces_string_settings():
    """Defensive coverage only: both genes are declared float, so the settings store hands them
    back through value_float as real floats. The float() call is what makes a hand-written or
    imported string value behave the same instead of raising deep inside the sizing math."""
    from ba2_common.core.position_sizing import resolve_sizing_risk_budget_pct
    assert resolve_sizing_risk_budget_pct(_settings_getter(atr_risk_budget_pct="0.5")) == 0.5


# ---------------------------------------------------------------------------------------------
# CLASSIC RM PINS. Backtests only ever run the classic risk manager, so its sizing must stay
# byte-identical across the shared-resolver refactor. These two cases are the review probe's
# (reports/margin/reproduce_margin_review.py): $18 000 virtual equity, price 100, stop 95.
# ---------------------------------------------------------------------------------------------

def _classic_pin_quantity(atr_risk_budget_pct):
    from types import SimpleNamespace
    from ba2_common.core import regime_overlay
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement
    from ba2_common.core.types import OrderDirection

    regime_overlay.reset_stressed()  # unstressed -> every regime scale is an exact no-op
    settings = {
        "atr_risk_budget_pct": atr_risk_budget_pct,
        "risk_per_trade_pct": 5.0,
        "atr_multiplier": 2.0,
        "min_stop_loss_pct": 0.0,
        "use_atr_stop": False,
        "commission_per_trade": 0.0,
        "max_virtual_equity_per_instrument_percent": 100.0,
    }
    expert = SimpleNamespace(
        id=1,
        get_virtual_balance=lambda: 18_000.0,
        get_setting_with_interface_default=lambda key, **kw: settings[key],
    )
    account = SimpleNamespace(
        id=1, get_setting_with_interface_default=lambda key, **kw: settings[key])
    order = SimpleNamespace(id=1, symbol="NEW", side=OrderDirection.BUY, quantity=0,
                            stop_price=95.0, limit_price=None, open_price=100.0, data={})
    return TradeRiskManagement()._risk_atr_quantity(
        order, "NEW", 100.0, expert, 18_000.0, 18_000.0, account)


def test_classic_risk_atr_sizes_off_the_budget_gene():
    """PIN: budget 0.5% of $18 000 = $90 risk / $5 stop distance = 18 shares."""
    assert _classic_pin_quantity(0.5) == 18


def test_classic_risk_atr_falls_back_to_risk_per_trade_pct():
    """PIN: no budget gene -> risk_per_trade_pct 5% = $900 / $5 = 180 shares."""
    assert _classic_pin_quantity(None) == 180
