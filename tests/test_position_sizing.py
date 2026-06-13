"""Tests for risk-based (ATR) position sizing — the institutional rule that caps
the dollar loss per trade and lets the stop distance set the share count
(B2 strategy-doc improvement #1)."""

import pytest

from ba2_trade_platform.core.position_sizing import (
    compute_risk_based_quantity as size,
    derive_stop_for_quantity,
)


class TestStopBasedSizing:
    def test_basic_stop_distance(self):
        # Doc example: entry 50, stop 45 -> risk/share 5; 2% of 10k = 200 -> 40 shares.
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=2.0, stop_price=45.0)
        assert r["quantity"] == 40
        assert r["risk_per_share"] == pytest.approx(5.0)
        assert r["risk_dollars"] == pytest.approx(200.0)

    def test_one_percent_risk(self):
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0, stop_price=45.0)
        assert r["quantity"] == 20  # 100 / 5

    def test_tighter_stop_more_shares(self):
        wide = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0, stop_price=45.0)
        tight = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0, stop_price=48.0)
        assert tight["quantity"] > wide["quantity"]  # 100/2=50 vs 100/5=20

    def test_short_stop_above_price(self):
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0, stop_price=53.0)
        assert r["quantity"] == 33  # 100 / 3


class TestAtrSizing:
    def test_atr_used_when_no_stop(self):
        # risk/share = 2 * ATR(2.5) = 5 -> 100/5 = 20
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0,
                 atr=2.5, atr_multiplier=2.0)
        assert r["quantity"] == 20
        assert r["risk_per_share"] == pytest.approx(5.0)

    def test_higher_atr_fewer_shares(self):
        low = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0, atr=1.0, atr_multiplier=2.0)
        high = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0, atr=5.0, atr_multiplier=2.0)
        assert high["quantity"] < low["quantity"]

    def test_stop_preferred_over_atr(self):
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0,
                 stop_price=49.0, atr=5.0, atr_multiplier=2.0)
        assert r["risk_per_share"] == pytest.approx(1.0)  # uses the stop, not ATR

    def test_no_stop_no_atr_returns_zero(self):
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0)
        assert r["quantity"] == 0 and "ATR" in r["reason"]


class TestCaps:
    def test_notional_cap_trims(self):
        # risk would allow 200 shares (1% of 1M / 5) but cap is 5k notional / 50 = 100.
        r = size(equity=1_000_000, current_price=50.0, risk_per_trade_pct=1.0,
                 stop_price=45.0, max_position_value=5_000)
        assert r["quantity"] == 100
        assert r["capped_by"] == "notional"

    def test_balance_cap_trims(self):
        r = size(equity=1_000_000, current_price=50.0, risk_per_trade_pct=1.0,
                 stop_price=45.0, max_position_value=100_000, available_balance=2_000)
        assert r["quantity"] == 40  # 2000 / 50
        assert r["capped_by"] == "balance"

    def test_lot_size_rounds_down(self):
        r = size(equity=10_000, current_price=10.0, risk_per_trade_pct=2.0,
                 stop_price=9.0, lot_size=100)
        # risk/share 1 -> 200 shares, already a multiple of 100
        assert r["quantity"] == 200
        r2 = size(equity=10_000, current_price=10.0, risk_per_trade_pct=1.5,
                  stop_price=9.0, lot_size=100)
        assert r2["quantity"] % 100 == 0  # 150 -> 100


class TestMinStopFloor:
    def test_tight_stop_floored(self):
        # 1% stop (49.5) would buy 200 shares; min 7% floor (3.5/share) caps at ~28.
        floored = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0,
                       stop_price=49.5, min_stop_pct=7.0)
        unfloored = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0,
                         stop_price=49.5)
        assert floored["quantity"] < unfloored["quantity"]
        assert floored["risk_per_share"] == pytest.approx(3.5)  # 7% of 50
        assert floored.get("stop_floored") is True

    def test_wide_stop_not_floored(self):
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0,
                 stop_price=44.0, min_stop_pct=7.0)
        assert r["risk_per_share"] == pytest.approx(6.0)  # 12% stop > 7% floor
        assert not r.get("stop_floored")

    def test_low_atr_floored(self):
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0,
                 atr=0.5, atr_multiplier=2.0, min_stop_pct=7.0)
        assert r["risk_per_share"] == pytest.approx(3.5)  # 1.0 implied -> floored to 3.5


class TestGuards:
    def test_zero_equity(self):
        assert size(equity=0, current_price=50, risk_per_trade_pct=1.0, stop_price=45)["quantity"] == 0

    def test_zero_price(self):
        assert size(equity=10_000, current_price=0, risk_per_trade_pct=1.0, stop_price=45)["quantity"] == 0

    def test_risk_budget_too_small(self):
        # 1% of 100 = $1 budget, risk/share $5 -> can't afford 1 share
        r = size(equity=100, current_price=50.0, risk_per_trade_pct=1.0, stop_price=45.0)
        assert r["quantity"] == 0 and "too small" in r["reason"]

    def test_stop_equal_to_price_falls_back_to_atr(self):
        r = size(equity=10_000, current_price=50.0, risk_per_trade_pct=1.0,
                 stop_price=50.0, atr=2.5, atr_multiplier=2.0)
        assert r["quantity"] == 20  # zero distance -> ATR path


class TestDeriveStopForQuantity:
    def test_stop_at_2pct_loss_price(self):
        # 100 shares @ $50, 2% of 10k = $200 risk -> stop_dist $2 -> SL $48 (4% > 7%? no, 4% < 7%)
        # 4% < 7% min -> qty reduced. risk$200 / (7%*50=3.5) = 57 shares.
        d = derive_stop_for_quantity(10_000, 50.0, 100, 2.0, is_long=True, min_stop_pct=7.0)
        assert d["quantity"] == 57
        assert d["stop_pct"] == pytest.approx(7.02, abs=0.1)
        assert d["sl_price"] < 50

    def test_small_qty_keeps_wide_stop(self):
        # 10 shares @ $50, $200 risk -> stop_dist $20 -> 40% stop (> 7%) -> keep qty 10
        d = derive_stop_for_quantity(10_000, 50.0, 10, 2.0, is_long=True, min_stop_pct=7.0)
        assert d["quantity"] == 10
        assert d["sl_price"] == pytest.approx(30.0)  # 50 - 20

    def test_short_stop_above_entry(self):
        d = derive_stop_for_quantity(10_000, 50.0, 10, 2.0, is_long=False, min_stop_pct=7.0)
        assert d["sl_price"] == pytest.approx(70.0)  # 50 + 20

    def test_reject_when_one_share_too_risky(self):
        # equity 100, 2% = $2 budget; min stop 7% of $50 = $3.5 > $2 even for 1 share -> reject
        d = derive_stop_for_quantity(100, 50.0, 1, 2.0, is_long=True, min_stop_pct=7.0)
        assert d["rejected"] is True and "rejected" in d["reason"]

    def test_no_min_stop_uses_exact_2pct(self):
        d = derive_stop_for_quantity(10_000, 50.0, 100, 2.0, is_long=True, min_stop_pct=0.0)
        assert d["quantity"] == 100
        assert d["sl_price"] == pytest.approx(48.0)  # 50 - 2

    def test_invalid_inputs_rejected(self):
        assert derive_stop_for_quantity(0, 50, 10, 2.0, True)["rejected"]
        assert derive_stop_for_quantity(10_000, 0, 10, 2.0, True)["rejected"]
        assert derive_stop_for_quantity(10_000, 50, 0, 2.0, True)["rejected"]
