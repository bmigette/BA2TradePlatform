"""A whole-share BUY added to an EXISTING holding rounds half-up (live 2026-10-02).

IYRI held 1.047 shares and wanted +0.9987; NIHI wanted +0.8821. Both got
``no order (whole)`` -- the delta was floored to 0, and every run recomputed the same
delta and floored it away again. The operator's rule (2026-08-31) is half-up from both
sides: "buy 1 share if we want to buy 0.6, but 0 if we want 0.3".

Flat positions are NOT rounded in ``_round_delta_shares``: they keep flooring so the
sub-unit case still reaches ``size_sub_unit_target`` (decision D1).
"""
import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import LabelTarget, PositionState, SymbolTarget
from ba2_common.core.account_types import MarginInfo

PRICE = 100.0
BASE = 10_000.0
WHOLE = MarginInfo(symbol="AAA", bp_factor=1.0, fractionable=False)
FRAC = MarginInfo(symbol="AAA", bp_factor=1.0, fractionable=True, min_trade_increment=0.001)


def _solve(target_shares, held, *, mode=pa.VALUATION_MODE_MARKET, margin=WHOLE,
           allow_fractional=False, cost_basis=None, bp=1_000_000.0):
    """One symbol, target ``target_shares`` at PRICE (the weight is % of BASE)."""
    pct = target_shares * PRICE / BASE * 100.0
    basis = held * PRICE if cost_basis is None else cost_basis
    labels = [LabelTarget("L", 100, [SymbolTarget("AAA", pct)])]
    current = {"AAA": PositionState("AAA", quantity=held, cost_basis=basis, price=PRICE)}
    plan = pa.compute_allocation(
        BASE, bp, labels, current, {"AAA": margin},
        allow_fractional=allow_fractional, default_bp_factor=1.0, valuation_mode=mode)
    return next(r for r in plan.rows if r.symbol == "AAA")


def _round(delta, held, margin=WHOLE, allow_fractional=False):
    return pa._round_delta_shares(delta, margin, allow_fractional=allow_fractional,
                                  current_quantity=held)


def _rounded_up_reason(row):
    return [r for r in row.reasons if "bought" in r and "nearest whole share" in r]


class TestHelper:
    def test_add_rounds_half_up(self):
        assert _round(+0.9987, 1.047) == 1.0
        assert _round(+0.8821, 2.04341) == 1.0
        assert _round(+0.5, 1.0) == 1.0

    def test_add_below_half_is_zero(self):
        assert _round(+0.49, 1.0) == 0.0

    def test_add_above_one_share_rounds_to_nearest(self):
        assert _round(+2.6, 1.0) == 3.0
        assert _round(+2.4, 1.0) == 2.0

    def test_flat_buy_still_floors_so_d1_is_reached(self):
        assert _round(+0.6, 0.0) == 0.0
        assert _round(+0.9987, 0.0) == 0.0
        assert _round(+2.6, 0.0) == 2.0

    def test_sell_unchanged(self):
        assert _round(-0.8773, 2.047) == -1.0
        assert _round(-0.4, 5.0) == 0.0
        assert _round(-3.0, 2.047) == -2.0

    def test_fractional_grid_unchanged(self):
        got = _round(+0.9987, 1.047, FRAC, allow_fractional=True)
        assert got == pytest.approx(0.998, abs=1e-9)


@pytest.mark.parametrize("mode", [pa.VALUATION_MODE_MARKET, pa.VALUATION_MODE_COST])
class TestPlanBothModes:
    def test_iyri_adds_one_share_and_says_so(self, mode):
        row = _solve(2.0457, 1.047, mode=mode)           # +0.9987
        assert row.delta_quantity == 1.0
        assert row.is_buy
        assert _rounded_up_reason(row), row.reasons
        assert not any("rounds to 0" in r for r in row.reasons)

    def test_nihi_adds_one_share(self, mode):
        row = _solve(2.04341 + 0.8821, 2.04341, mode=mode)
        assert row.delta_quantity == 1.0
        assert _rounded_up_reason(row)

    def test_under_half_a_share_is_no_order_with_the_existing_reason(self, mode):
        row = _solve(1.047 + 0.49, 1.047, mode=mode)
        assert row.delta_quantity == 0.0
        assert any("rounds to 0 on the tradeable grid - no order" in r for r in row.reasons)
        assert not _rounded_up_reason(row)

    def test_add_2_6_goes_to_3_and_2_4_goes_to_2(self, mode):
        up = _solve(3.6, 1.0, mode=mode)
        down = _solve(3.4, 1.0, mode=mode)
        assert up.delta_quantity == 3.0
        assert _rounded_up_reason(up)
        assert down.delta_quantity == 2.0
        assert not _rounded_up_reason(down)       # rounded DOWN: nothing extra bought

    def test_exact_whole_add_is_not_announced(self, mode):
        row = _solve(3.0, 1.0, mode=mode)
        assert row.delta_quantity == 2.0
        assert not _rounded_up_reason(row)

    def test_flat_sub_unit_still_goes_through_d1(self, mode):
        row = _solve(0.6, 0.0, mode=mode)
        assert row.delta_quantity == 1.0
        assert row.sizing_outcome == pa.SIZING_OUTCOME_BUMPED
        assert any("BUMPED UP" in r for r in row.reasons)
        assert not _rounded_up_reason(row)

    def test_flat_below_half_is_skipped_by_d1(self, mode):
        row = _solve(0.3, 0.0, mode=mode)
        assert row.delta_quantity == 0.0
        assert row.sizing_outcome == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE

    def test_flat_multi_share_buy_floors_then_converges(self, mode):
        first = _solve(2.6, 0.0, mode=mode)
        assert first.delta_quantity == 2.0
        second = _solve(2.6, 2.0, mode=mode)          # next run: an add
        assert second.delta_quantity == 1.0

    def test_sell_unchanged_and_announced(self, mode):
        row = _solve(1.3, 2.047, mode=mode)           # -0.747
        assert row.delta_quantity == -1.0
        assert any("sold" in r and "nearest whole share" in r for r in row.reasons)
        assert not _rounded_up_reason(row)

    def test_fractionable_symbol_unchanged(self, mode):
        row = _solve(2.0457, 1.047, mode=mode, margin=FRAC, allow_fractional=True)
        assert row.delta_quantity == pytest.approx(0.998, abs=1e-9)
        assert not _rounded_up_reason(row)

    def test_symmetry_shortfall_and_excess_both_round_to_one_share_without_churn(self, mode):
        short = _solve(2.6, 2.0, mode=mode)            # 0.6 short -> buy 1
        excess = _solve(2.4, 3.0, mode=mode)           # 0.6 over  -> sell 1
        assert short.delta_quantity == 1.0
        assert excess.delta_quantity == -1.0
        # Second run, after each trade has filled: 0.4 off in the other direction, which
        # rounds to nothing from either side, so no opposite order.
        assert _solve(2.6, 3.0, mode=mode).delta_quantity == 0.0
        assert _solve(2.4, 2.0, mode=mode).delta_quantity == 0.0


def test_over_buy_that_does_not_fit_buying_power_is_scaled_loudly():
    """Downstream check: the round-up is real spend, so the scaler sees it. Here it
    cannot fit, and the row says so rather than quietly sending the extra share."""
    row = _solve(2.0457, 1.047, bp=50.0)             # one share costs 100, only 50 free
    assert row.delta_quantity == 0.0
    assert row.skipped
    assert any("scaled" in r.lower() or "buying power" in r.lower() for r in row.reasons), \
        row.reasons
