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
import pytest

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


# --- resting protective stop (2026-08-06) -----------------------------------------------------
# FactorRanker was the ONLY expert with no broker-side protection: 22 live positions across all 6
# instances had no stop of any kind, because apply_stop_losses has no live caller and never had
# one. The per-bar pass in daily_engine is the SIMULATOR standing in for an exchange, not a
# cadence to port into production -- so the stop is now a real order and the broker enforces it.

class _Expert:
    def __init__(self, risk_pct=1.0, equity=100_000.0):
        self._risk, self._equity = risk_pct, equity
    def get_setting_with_interface_default(self, key, **k):
        return self._risk if key == "risk_per_trade_pct" else None
    def get_virtual_equity(self):
        return self._equity


class _Trans:
    def __init__(self, qty, price):
        self.open_qty, self.open_price = qty, price


def _pm(expert):
    from ba2_experts.FactorRanker.portfolio import FactorPortfolioManager
    pm = FactorPortfolioManager.__new__(FactorPortfolioManager)
    pm.expert = expert
    pm.expert_instance_id = 99
    pm.account_id = 1
    return pm


def test_stop_price_is_the_same_rule_stop_loss_sells_applies():
    """The resting price must be the exact boundary of the equity-loss inequality, so the order
    and the simulated rule cannot disagree."""
    from ba2_experts.FactorRanker.portfolio import stop_loss_sells
    pm = _pm(_Expert(risk_pct=1.0, equity=100_000.0))
    # 100 shares @ 50 -> budget 1000 -> stop at 50 - 1000/100 = 40
    sl = pm.protective_stop_price("AAA", [_Trans(100, 50.0)])
    assert sl == pytest.approx(40.0)
    # just BELOW the stop the rule fires; just above it does not
    assert stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": sl - 0.01}, 100_000.0, 1.0) == {"AAA": 100}
    assert stop_loss_sells({"AAA": (50.0, 100)}, {"AAA": sl + 0.01}, 100_000.0, 1.0) == {}


def test_pending_buy_is_folded_into_the_price():
    """A stop priced at entry must reflect the position about to exist, not the empty one."""
    pm = _pm(_Expert(risk_pct=1.0, equity=100_000.0))
    sl = pm.protective_stop_price("AAA", [], extra_qty=100, extra_price=50.0)
    assert sl == pytest.approx(40.0)


def test_adding_to_a_position_reprices_the_stop():
    """avg cost AND qty both move, so the stop must move -- a stale stop is the bug this fixes."""
    pm = _pm(_Expert(risk_pct=1.0, equity=100_000.0))
    before = pm.protective_stop_price("AAA", [_Trans(100, 50.0)])
    after = pm.protective_stop_price("AAA", [_Trans(100, 50.0)], extra_qty=100, extra_price=60.0)
    # avg cost 55, qty 200 -> 55 - 1000/200 = 50.0
    assert before == pytest.approx(40.0)
    assert after == pytest.approx(50.0)


def test_no_stop_is_invented_from_missing_inputs():
    """Better unprotected-and-warned than protected at a fabricated price."""
    assert _pm(_Expert(risk_pct=0)).protective_stop_price("AAA", [_Trans(100, 50.0)]) is None
    assert _pm(_Expert(equity=0)).protective_stop_price("AAA", [_Trans(100, 50.0)]) is None
    assert _pm(_Expert()).protective_stop_price("AAA", []) is None


def test_stop_below_zero_is_refused():
    """A risk budget larger than the position's whole value implies a negative stop -- not a
    valid order; the caller warns and leaves it unprotected rather than sending nonsense."""
    pm = _pm(_Expert(risk_pct=99.0, equity=1_000_000.0))
    assert pm.protective_stop_price("AAA", [_Trans(10, 5.0)]) is None


# --- an add is never protected by a stop at/above market (review 2026-09-29) ------------------
# The add's stop is priced by the rule on the position the add CREATES. When that lands at or
# above the current price, placing it would put a sell stop above market; the add is refused
# before anything is sent for the name, the rest of the rebalance proceeds, and the rebalance
# then RAISES ProtectiveStopError naming the refused names.

class _BuyAccount:
    def __init__(self, price):
        self.price, self.submitted = price, []

    def get_instrument_current_price(self, symbol):
        return self.price

    def submit_order(self, order, sl_price=None, is_closing_order=False, **kw):
        self.submitted.append((order.symbol, order.quantity, sl_price))
        return order


class _HeldTrans(_Trans):
    def __init__(self, qty, price, id_=1):
        super().__init__(qty, price)
        self.id = id_


def test_an_add_whose_rule_stop_is_above_market_is_refused_before_it_is_sent():
    from ba2_experts.FactorRanker.portfolio import ProtectiveStopError

    # 100 held at 100, now 50: adding 10 gives a blended cost of 95.45 and a 1% budget of
    # $1,000 over 110 shares -> rule stop 86.36, far above the $50 market.
    pm = _pm(_Expert(risk_pct=1.0, equity=100_000.0))
    pm.account = _BuyAccount(price=50.0)

    with pytest.raises(ProtectiveStopError, match="AAA"):
        pm._submit_buy("AAA", 10, [_HeldTrans(100, 100.0)])
    assert pm.account.submitted == []


def test_an_add_whose_rule_stop_is_below_market_is_sent_with_it():
    pm = _pm(_Expert(risk_pct=1.0, equity=100_000.0))
    pm.account = _BuyAccount(price=100.0)

    pm._submit_buy("AAA", 10, [_HeldTrans(100, 100.0)])

    (symbol, qty, sl), = pm.account.submitted
    # The stop covers the position the add creates: 110 shares at cost 100, $1,000 budget.
    assert (symbol, qty) == ("AAA", 10)
    assert sl == pytest.approx(100.0 - 1_000.0 / 110)


def test_the_rebalance_finishes_the_other_names_then_raises_for_the_refused_add():
    from ba2_experts.FactorRanker.portfolio import ProtectiveStopError

    pm = _pm(_Expert(risk_pct=1.0, equity=100_000.0))
    pm.account = _BuyAccount(price=50.0)
    held = {"AAA": 100.0}
    pm.get_holdings = lambda: (held, {"AAA": [_HeldTrans(100, 100.0)]})
    pm._quantity_units = lambda symbols: {}

    # AAA: 100 held, target $10,000 at $50 -> +100 (refused, rule stop above market);
    # BBB: new name, $5,000 at $50 -> 100 shares, sent.
    with pytest.raises(ProtectiveStopError, match="refused 1 add"):
        pm.rebalance({"AAA": 0.1, "BBB": 0.05})

    assert [(s, q) for s, q, _ in pm.account.submitted] == [("BBB", 100)]
