"""The classic manager's run record must say what it ALLOCATED ON, not only what it decided.

Requested 2026-09-08: "the risk manager UI details for classic are very simple. I'd like to
add the size and the score / weight it used to allocate."

Before this, a run recorded symbol / outcome / quantity / reason. That answers "was it
funded", and cannot answer "why was THIS one funded and that one not" -- the manager funds
in score order until the budget runs out, so a refused symbol's score IS its explanation.
Two numbers do the allocating and neither was written down:

  score   compute_order_priority_score(expected_profit_percent, confidence) -- the sort key
  weight  the per-instrument weight% the sized quantity is multiplied by
"""
import pytest

from ba2_common.core import TradeRiskManagement as trm
from ba2_common.core.TradeRiskManagement import compute_order_priority_score


class _Order:
    def __init__(self, oid, symbol, side="BUY", quantity=None):
        self.id = oid
        self.symbol = symbol
        self.side = side
        self.quantity = quantity


class _Rec:
    def __init__(self, profit=None, confidence=None):
        self.expected_profit_percent = profit
        self.confidence = confidence


@pytest.fixture
def recorded(monkeypatch):
    """Run ``_record_classic_run`` and hand back the decisions it would have persisted."""
    captured = {}

    def _fake_record_run(**kwargs):
        captured.update(kwargs)
        return 1

    import ba2_common.core.risk_manager_run as rmr
    monkeypatch.setattr(rmr, "record_run", _fake_record_run)
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: False)

    def _run(*, pending, recs, funded=(), unfunded=(), permission=(),
             prices=None, weights=None, traces=None):
        mgr = object.__new__(trm.TradeRiskManagement)
        import logging
        mgr.logger = logging.getLogger("test")
        mgr._record_classic_run(
            expert_instance_id=1, account_id=1, started_at=None,
            pending_orders=list(pending), dropped_by_permission=list(permission),
            orders_with_recommendations=list(recs),
            orders_to_update=list(funded), orders_to_delete=list(unfunded),
            symbol_prices=prices or {},
            context={"max_per_instrument": 1000.0},
            instrument_weights=weights or {},
            traces=traces or {},
        )
        return {d["symbol"]: d for d in captured.get("decisions", [])}

    return _run


def test_a_funded_row_carries_the_score_that_ranked_it(recorded):
    order = _Order(1, "AAA", quantity=10)
    rows = recorded(pending=[order], recs=[(order, _Rec(profit=30.0, confidence=40.0))],
                    funded=[order], prices={"AAA": 5.0})
    row = rows["AAA"]
    assert row["outcome"] == "FUNDED"
    assert row["score"] == pytest.approx(compute_order_priority_score(30.0, 40.0), abs=1e-4)
    # ITS INPUTS TOO, so the score can be re-derived rather than trusted.
    assert row["confidence"] == 40.0 and row["profit_pct"] == 30.0
    assert row["cost"] == 50.0, "the size in money, comparable with the per-instrument cap"


def test_a_REFUSED_row_carries_its_score_too(recorded):
    """THE POINT of the change. The manager funds in score order until the budget is
    gone, so the score of a symbol that lost is the reason it lost."""
    won, lost = _Order(1, "WIN", quantity=10), _Order(2, "LOSE")
    rows = recorded(
        pending=[won, lost],
        recs=[(won, _Rec(profit=30.0, confidence=90.0)),
              (lost, _Rec(profit=2.0, confidence=10.0))],
        funded=[won], unfunded=[lost], prices={"WIN": 5.0, "LOSE": 5.0})

    assert rows["LOSE"]["outcome"] != "FUNDED"
    assert "score" in rows["LOSE"], "a refusal with no score cannot explain itself"
    assert rows["LOSE"]["score"] < rows["WIN"]["score"]


def test_the_weight_that_multiplied_the_size_is_recorded(recorded):
    order = _Order(1, "AAA", quantity=10)
    rows = recorded(pending=[order], recs=[(order, _Rec(profit=10.0, confidence=50.0))],
                    funded=[order], prices={"AAA": 5.0},
                    weights={"AAA": {"weight": 60.0}})
    assert rows["AAA"]["weight"] == 60.0


def test_an_unweighted_symbol_records_no_weight_rather_than_100(recorded):
    """ABSENT means "not configured", which the UI draws as a dash. Writing 100 would
    claim a setting the user never made."""
    order = _Order(1, "AAA", quantity=10)
    rows = recorded(pending=[order], recs=[(order, _Rec(profit=10.0, confidence=50.0))],
                    funded=[order], prices={"AAA": 5.0}, weights={})
    assert "weight" not in rows["AAA"]


def test_a_missing_score_input_is_omitted_not_zeroed(recorded):
    """An expert that publishes no profit estimate has no profit_pct -- 0.0 would read as
    "estimated zero profit", which ranks very differently and is a real outcome."""
    order = _Order(1, "AAA", quantity=10)
    rows = recorded(pending=[order], recs=[(order, _Rec(profit=None, confidence=70.0))],
                    funded=[order], prices={"AAA": 5.0})
    assert "profit_pct" not in rows["AAA"]
    assert rows["AAA"]["confidence"] == 70.0
    assert "score" in rows["AAA"], "the score still exists -- the fallback band ranks it"


def test_a_symbol_with_no_recommendation_records_no_score(recorded):
    """It was never ranked, so it has no score. That is the whole reason it was refused."""
    order = _Order(1, "AAA")
    rows = recorded(pending=[order], recs=[], unfunded=[])
    assert rows["AAA"]["outcome"] == "REFUSED_NO_RECOMMENDATION"
    assert "score" not in rows["AAA"]


def test_a_settings_failure_costs_the_weight_and_not_the_run():
    """The sizing has already happened by the time the record is written; an annotation
    must never be able to lose it."""
    class _Boom:
        def _get_enabled_instruments_config(self):
            raise RuntimeError("settings are down")

    assert trm.TradeRiskManagement._safe_instrument_config(_Boom()) == {}


# =========================================================================================
# THE LIVE PATH, AND WHAT IT ALLOCATED ON
#
# Everything above drives ``_record_classic_run`` -- the DB-pending-order path. The LIVE
# enter path is ``size_candidate_orders`` -> ``_record_candidate_run``, and 5edf15de added
# the ranking fields to the DB path ONLY: production run 10 (2026-09-11, 3 funded of 3)
# carries symbol/outcome/reason/quantity/side/price/cost and nothing else, so the dialog
# draws "-" for Score and Weight on every row an operator has ever looked at.
#
# The fix is ONE builder for both paths, fed by a per-order SIZING TRACE captured where the
# operands exist (inside the sizing core) rather than re-derived afterwards from the
# numbers -- re-deriving is how "which limit bound this order" becomes a guess.
# =========================================================================================
import logging

from ba2_common.core.types import OrderDirection


class _SizingOrder:
    """A candidate order: transient, no ``.id`` -- correlated by python identity."""

    def __init__(self, symbol, side=OrderDirection.BUY, data=None, oid=None):
        self.id = oid
        self.symbol = symbol
        self.side = side
        self.quantity = None
        self.stop_price = None
        self.data = data or {}


class _FakeExpert:
    def __init__(self, settings=None, instruments=None, equity=100_000.0):
        self._settings = dict(settings or {})
        self._instruments = dict(instruments or {})
        self._equity = equity

    def get_setting_with_interface_default(self, name, log_warning=True):
        return self._settings.get(name)          # a test double, not a settings read

    def _get_enabled_instruments_config(self):
        return self._instruments

    def get_virtual_balance(self):
        return self._equity


class _FakeAccount:
    def __init__(self, prices):
        self._prices = dict(prices)

    def get_instrument_current_price(self, symbols):
        return {s: self._prices[s] for s in symbols if s in self._prices}


def _manager():
    mgr = object.__new__(trm.TradeRiskManagement)
    mgr.logger = logging.getLogger("test")
    mgr.indicator_provider = None
    mgr.as_of = None
    return mgr


def _size(pairs, *, balance, cap, prices, expert=None, allocations=None):
    """Drive the REAL sizing core and hand back the trace it captured, keyed by symbol."""
    mgr = _manager()
    traces, context = {}, {}
    mgr._calculate_order_quantities(
        pairs, balance, cap, dict(allocations or {}), _FakeAccount(prices),
        expert or _FakeExpert(), traces=traces, context=context)
    return {o.symbol: traces[id(o)] for o, _rec in pairs if id(o) in traces}, context


def _pair(symbol, *, profit=10.0, confidence=80.0, data=None):
    order = _SizingOrder(symbol, data=data)
    return order, _Rec(profit=profit, confidence=confidence)


# -----------------------------------------------------------------------------------------
# The trace: which constraint actually produced the quantity
# -----------------------------------------------------------------------------------------

def test_an_order_capped_by_the_instrument_limit_says_so_with_its_operands():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0})
    t = traces["AAA"]

    assert t["binding"] == "instrument_cap"
    assert t["rank"] == 1
    assert t["price"] == 100.0
    assert t["cap_available"] == 1_000.0
    assert t["max_qty_by_instrument"] == pytest.approx(10.0)
    assert t["max_qty_by_balance"] == pytest.approx(1_000.0)
    assert t["quantity"] == 10
    assert t["cost"] == pytest.approx(1_000.0)
    assert t["balance_before"] == 100_000.0
    assert t["balance_after"] == pytest.approx(99_000.0)


def test_an_order_capped_by_the_remaining_balance_says_balance():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=550.0, cap=1_000_000.0, prices={"AAA": 100.0})
    t = traces["AAA"]

    assert t["binding"] == "balance"
    assert t["quantity"] == 5
    assert t["max_qty_by_balance"] == pytest.approx(5.5)


def test_the_weight_that_shrank_the_size_is_the_binding_constraint():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0},
                      expert=_FakeExpert(instruments={"AAA": {"weight": 50.0}}))
    t = traces["AAA"]

    assert t["weight"] == 50.0
    assert t["quantity"] == 5, "10 shares by cap, halved by the 50% weight"
    assert t["binding"] == "weight"


def test_a_symbol_too_expensive_for_its_cap_is_an_early_skip_against_the_cap():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=50.0, prices={"AAA": 100.0})
    t = traces["AAA"]

    assert t["binding"] == "early_skip_cap"
    assert t["quantity"] == 0
    assert t["cap_available"] == 50.0
    assert "max_qty_by_instrument" not in t, "it never reached the sizing arithmetic"


def test_a_symbol_the_remaining_balance_cannot_reach_is_an_early_skip_against_balance():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=50.0, cap=1_000_000.0, prices={"AAA": 100.0})

    assert traces["AAA"]["binding"] == "early_skip_balance"


def test_a_symbol_with_no_price_records_no_price_rather_than_a_guessed_limit():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={})

    assert traces["AAA"]["binding"] == "no_price"
    assert "price" not in traces["AAA"]


def test_a_lot_constrained_order_names_the_lot_size():
    pair = _pair("AAA", data={"lot_size": 100})
    traces, _ = _size([pair], balance=100_000.0, cap=15_000.0, prices={"AAA": 100.0})
    t = traces["AAA"]

    assert t["quantity"] == 100, "150 shares by cap, rounded down to one whole lot"
    assert t["binding"] == "lot_size"


def test_risk_atr_sizing_carries_the_risk_operands():
    pair = _pair("AAA")
    expert = _FakeExpert(settings={"sizing_mode": "risk_atr", "risk_per_trade_pct": 1.0,
                                   "min_stop_loss_pct": 5.0}, equity=100_000.0)
    traces, context = _size([pair], balance=100_000.0, cap=1_000_000.0,
                            prices={"AAA": 100.0}, expert=expert)
    t = traces["AAA"]

    assert t["binding"] == "risk_atr"
    assert t["risk_budget_pct"] == pytest.approx(1.0)
    assert t["risk_dollars"] == pytest.approx(1_000.0)
    assert t["stop_price"] == pytest.approx(pair[0].stop_price)
    assert t["stop_distance_pct"] > 0
    assert t["qty_by_risk"] >= t["quantity"]
    assert context["sizing_mode"] == "risk_atr"
    # The cash clamp reads the commission in THIS mode too; a run whose record omitted it
    # would read as "no commission was charged".
    assert context["commission_per_trade"] == 0.0


def test_the_balance_chains_from_one_order_to_the_next():
    """``balance_after`` of the first IS ``balance_before`` of the second. Without that
    the reader cannot follow the money down the ranking, which is the whole record."""
    first, second = _pair("AAA"), _pair("BBB")
    traces, _ = _size([first, second], balance=100_000.0, cap=1_000.0,
                      prices={"AAA": 100.0, "BBB": 100.0})

    assert traces["AAA"]["rank"] == 1 and traces["BBB"]["rank"] == 2
    assert traces["BBB"]["balance_before"] == pytest.approx(traces["AAA"]["balance_after"])


def test_an_existing_position_shows_up_as_the_allocation_that_shrank_the_cap():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0},
                      allocations={"AAA": 400.0})
    t = traces["AAA"]

    assert t["existing_allocation"] == 400.0
    assert t["cap_available"] == pytest.approx(600.0)
    assert t["quantity"] == 6


# -----------------------------------------------------------------------------------------
# The run context: the capital mapping the sizing read, and nothing it did not
# -----------------------------------------------------------------------------------------

class _Balances:
    virtual, used, available = 20_000.0, 5_000.0, 15_000.0


def test_the_context_records_the_capital_the_sizing_was_measured_against():
    context = trm.TradeRiskManagement._run_context(
        balances=_Balances(),
        capital={"balance": 10_000.0, "tradable_balance": 20_000.0,
                 "effective_factor": 2.0, "virtual_equity_pct": 100.0},
        max_per_instrument=1_500.0, max_per_instrument_ratio=0.1)

    assert context["equity"] == 10_000.0
    assert context["tradable_balance"] == 20_000.0
    assert context["margin_factor"] == 2.0
    assert context["allocation_pct"] == 100.0
    assert context["virtual_balance"] == 20_000.0
    assert context["used_balance"] == 5_000.0
    assert context["available_balance"] == 15_000.0
    assert context["max_per_instrument"] == 1_500.0
    assert context["max_per_instrument_ratio"] == pytest.approx(0.1)


def test_a_capital_figure_the_sizing_never_read_is_absent_not_zero():
    """An account that publishes no capital description leaves the margin half of the
    line EMPTY. A 0 equity would read as a measured, broke account."""
    context = trm.TradeRiskManagement._run_context(
        balances=_Balances(), capital=None,
        max_per_instrument=1_500.0, max_per_instrument_ratio=0.1)

    for absent in ("equity", "tradable_balance", "margin_factor", "allocation_pct"):
        assert absent not in context
    assert context["available_balance"] == 15_000.0


def test_the_sizing_knobs_are_recorded_from_the_pass_that_used_them():
    expert = _FakeExpert(settings={"diversification_factor": 0.5, "sizing_mode": "notional"})
    _, context = _size([_pair("AAA")], balance=100_000.0, cap=1_000.0,
                       prices={"AAA": 100.0}, expert=expert)

    assert context["sizing_mode"] == "notional"
    assert context["diversification_factor"] == 0.5
    assert context["regime_risk_scale"] == 1.0, "recorded ALWAYS, 1.0 when unstressed"
    assert context["commission_per_trade"] == 0.0


# -----------------------------------------------------------------------------------------
# One builder, both paths
# -----------------------------------------------------------------------------------------

@pytest.fixture
def both_paths(monkeypatch):
    """BOTH recorders behind ONE capture, so a test can compare what they wrote.

    One sink and not two fixtures: each fixture would patch ``record_run`` in turn and the
    second patch would quietly swallow the first path's decisions -- the test would then be
    comparing a row against nothing.
    """
    captured = {}

    def _fake_record_run(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return 1

    monkeypatch.setattr("ba2_common.core.risk_manager_run.record_run", _fake_record_run)
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: False)

    class _Paths:
        @staticmethod
        def candidate(*, candidates, funded=(), unfunded=(), permission=(), prices=None,
                      traces=None):
            _manager()._record_candidate_run(
                expert_instance_id=1, account_id=1, started_at=None,
                candidates=list(candidates), dropped_by_permission=list(permission),
                orders_to_update=list(funded), orders_to_delete=list(unfunded),
                symbol_prices=prices or {}, context={"max_per_instrument": 1000.0},
                traces=traces or {})
            return {d["symbol"]: d for d in captured["decisions"]}

        @staticmethod
        def classic(*, pending, recs, funded=(), unfunded=(), permission=(), prices=None,
                    traces=None):
            _manager()._record_classic_run(
                expert_instance_id=1, account_id=1, started_at=None,
                pending_orders=list(pending), dropped_by_permission=list(permission),
                orders_with_recommendations=list(recs),
                orders_to_update=list(funded), orders_to_delete=list(unfunded),
                symbol_prices=prices or {}, context={"max_per_instrument": 1000.0},
                traces=traces or {})
            return {d["symbol"]: d for d in captured["decisions"]}

    return _Paths


@pytest.fixture
def candidate_recorded(both_paths):
    """``_record_candidate_run`` -- the LIVE enter path -- and its decisions."""
    return both_paths.candidate


def test_the_live_candidate_path_records_what_it_ranked_and_allocated_on(candidate_recorded):
    """THE BUG. Production run 10 shows "-" for Score and Weight on every row because the
    candidate path built its decisions without any of this."""
    pair = _pair("AAA", profit=30.0, confidence=40.0)
    order = pair[0]
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0},
                      expert=_FakeExpert(instruments={"AAA": {"weight": 100.0}}))
    row = candidate_recorded(candidates=[pair], funded=[order], prices={"AAA": 100.0},
                             traces={id(order): traces["AAA"]})["AAA"]

    assert row["outcome"] == "FUNDED"
    assert row["score"] == pytest.approx(compute_order_priority_score(30.0, 40.0), abs=1e-4)
    assert row["confidence"] == 40.0 and row["profit_pct"] == 30.0
    assert row["weight"] == 100.0
    assert row["rank"] == 1
    assert row["binding"] == "instrument_cap"
    assert row["balance_before"] == 100_000.0 and row["balance_after"] == 99_000.0
    assert row["cap_available"] == 1_000.0


def test_both_paths_produce_the_same_row_shape(both_paths):
    """One builder, or the two records drift and the dialog has to know which wrote it."""
    pair = _pair("AAA", profit=30.0, confidence=40.0)
    order = pair[0]
    order.id = 1
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0})
    trace = {id(order): traces["AAA"]}

    db_row = both_paths.classic(pending=[order], recs=[pair], funded=[order],
                                prices={"AAA": 100.0}, traces=trace)["AAA"]
    candidate_row = both_paths.candidate(candidates=[pair], funded=[order],
                                         prices={"AAA": 100.0}, traces=trace)["AAA"]

    assert set(db_row) == set(candidate_row)
    assert db_row == candidate_row


def test_a_permission_refusal_has_no_rank_because_it_was_never_ranked(candidate_recorded):
    pair = _pair("AAA")
    row = candidate_recorded(candidates=[pair], permission=[pair[0]])["AAA"]

    assert row["outcome"] == "REFUSED_PERMISSION"
    assert "rank" not in row and "binding" not in row


def test_a_refused_row_still_carries_the_sizing_it_was_refused_by(candidate_recorded):
    """A symbol the budget ran out on has a rank, a score and a binding constraint --
    that IS the answer to "why not this one"."""
    pair = _pair("AAA")
    order = pair[0]
    traces, _ = _size([pair], balance=50.0, cap=1_000_000.0, prices={"AAA": 100.0})
    row = candidate_recorded(candidates=[pair], unfunded=[order], prices={"AAA": 100.0},
                             traces={id(order): traces["AAA"]})["AAA"]

    assert row["outcome"] == "REFUSED_UNFUNDED"
    assert row["binding"] == "early_skip_balance"
    assert row["rank"] == 1
    assert "quantity" not in row, "a refused symbol was never sized"


def test_a_run_recorded_without_a_trace_still_writes_its_row(candidate_recorded):
    """Old rows, and any path that has no trace, must still record. The new keys are
    ABSENT (the UI draws a dash), never invented."""
    pair = _pair("AAA")
    row = candidate_recorded(candidates=[pair], funded=[pair[0]], prices={"AAA": 100.0})["AAA"]

    assert row["outcome"] == "FUNDED"
    assert "binding" not in row and "rank" not in row


def test_a_broken_trace_costs_the_annotation_and_not_the_sizing():
    """The trace only OBSERVES. A failure to record one must never reach the sizing loop."""
    class _Hostile(dict):
        def __setitem__(self, *a, **kw):
            raise RuntimeError("trace store is on fire")

    pair = _pair("AAA")
    mgr = _manager()
    orders_to_update, _, _ = mgr._calculate_order_quantities(
        [pair], 100_000.0, 1_000.0, {}, _FakeAccount({"AAA": 100.0}), _FakeExpert(),
        traces=_Hostile(), context={})

    assert orders_to_update == [pair[0]] and pair[0].quantity == 10


# =========================================================================================
# REVIEW 2026-09-11: the record must name the constraint that ACTUALLY bound
#
# The first cut recorded the SIZING MODE for every risk_atr order and called it the binding
# constraint. risk_atr sizes off the risk budget and THEN clamps to the per-instrument cap
# and to cash -- the sizer returns which clamp trimmed it (``capped_by``) and the record
# threw that away, so an order cut in half by the cap still read "risk_atr". And the refusal
# SENTENCE was re-derived from the full cap while the branch that refused had compared
# against the cap MINUS what the symbol already holds, so a row could name one limit and
# quote a different number.
# =========================================================================================

def _risk_atr_expert(**settings):
    base = {"sizing_mode": "risk_atr", "risk_per_trade_pct": 1.0, "min_stop_loss_pct": 5.0}
    base.update(settings)
    return _FakeExpert(settings=base, equity=100_000.0)


def test_a_risk_atr_order_trimmed_by_the_instrument_cap_says_instrument_cap():
    """The budget alone buys 200 shares; the cap allows 10. Reporting "risk_atr" would
    send the reader to check a risk budget that was not what limited this order."""
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0},
                      expert=_risk_atr_expert())
    t = traces["AAA"]

    assert t["qty_by_risk"] == 200
    assert t["quantity"] == 10
    assert t["binding"] == "instrument_cap"


def test_a_risk_atr_order_trimmed_by_cash_says_balance():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=550.0, cap=1_000_000.0, prices={"AAA": 100.0},
                      expert=_risk_atr_expert())
    t = traces["AAA"]

    assert t["quantity"] == 5
    assert t["binding"] == "balance"


def test_an_unclamped_risk_atr_order_still_says_risk_atr():
    """The budget itself bound it: no clamp ran, so the mode IS the answer."""
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=1_000_000.0, prices={"AAA": 100.0},
                      expert=_risk_atr_expert())

    assert traces["AAA"]["binding"] == "risk_atr"
    assert traces["AAA"]["quantity"] == 200


def test_the_refusal_sentence_quotes_the_limit_its_binding_names(candidate_recorded):
    """The early-skip compared the price against what was LEFT under the cap -- the cap
    minus this symbol's existing position. A sentence re-derived from the full cap quotes a
    number that had nothing to do with the refusal, next to a Binding column that names the
    one that did."""
    pair = _pair("AAA")
    order = pair[0]
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0},
                      allocations={"AAA": 950.0})
    row = candidate_recorded(candidates=[pair], unfunded=[order], prices={"AAA": 100.0},
                             traces={id(order): traces["AAA"]})["AAA"]

    assert row["binding"] == "early_skip_cap"
    assert "50.00" in row["reason"], row["reason"]
    assert "1,000.00" not in row["reason"], (
        "the full cap was never the limit this order hit")


def test_a_refusal_for_want_of_budget_quotes_the_budget_it_had(candidate_recorded):
    pair = _pair("AAA")
    order = pair[0]
    traces, _ = _size([pair], balance=50.0, cap=1_000_000.0, prices={"AAA": 100.0})
    row = candidate_recorded(candidates=[pair], unfunded=[order], prices={"AAA": 100.0},
                             traces={id(order): traces["AAA"]})["AAA"]

    assert row["binding"] == "early_skip_balance"
    assert "50.00" in row["reason"] and "100.00" in row["reason"], row["reason"]


def test_a_traceless_refusal_keeps_the_sentence_it_always_had(candidate_recorded):
    """Every row production has recorded so far. With no binding and no operands there is
    nothing to branch on, so the old price-against-cap comparison stands."""
    pair = _pair("AAA")
    row = candidate_recorded(candidates=[pair], unfunded=[pair[0]],
                             prices={"AAA": 5_000.0})["AAA"]

    assert "exceeds the 1,000.00 per-instrument cap" in row["reason"], row["reason"]


# -----------------------------------------------------------------------------------------
# The bindings the first cut never exercised
# -----------------------------------------------------------------------------------------

def test_the_diversification_factor_is_named_when_it_reserved_the_rest():
    """Two instruments still have headroom and the factor is below 1, so only a fraction of
    the ceiling is spent on the first -- that fraction, not the ceiling, set the size."""
    first, second = _pair("AAA"), _pair("BBB")
    traces, _ = _size([first, second], balance=100_000.0, cap=1_000.0,
                      prices={"AAA": 100.0, "BBB": 100.0},
                      expert=_FakeExpert(settings={"diversification_factor": 0.5}))

    assert traces["AAA"]["binding"] == "diversification"
    assert traces["AAA"]["quantity"] == 5, "10 by the cap, halved by the factor"


def test_the_one_share_floor_is_named_when_it_rescued_the_order():
    """Rounding gave zero and the floor put one share back. Neither ceiling decided that."""
    first, second = _pair("AAA"), _pair("BBB")
    traces, _ = _size([first, second], balance=100_000.0, cap=150.0,
                      prices={"AAA": 100.0, "BBB": 100.0},
                      expert=_FakeExpert(settings={"diversification_factor": 0.5}))

    assert traces["AAA"]["quantity"] == 1
    assert traces["AAA"]["binding"] == "min_one_share"


def test_the_one_share_floor_after_weighting_is_named_too():
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0},
                      expert=_FakeExpert(instruments={"AAA": {"weight": 5.0}}))

    assert traces["AAA"]["quantity"] == 1, "10 by the cap, 0.5 after the weight, floored to 1"
    assert traces["AAA"]["binding"] == "min_one_share"


def test_a_weight_that_was_reverted_did_not_bind_anything():
    """A weight above 100% that would breach the cap is discarded and the original size
    kept -- so the limit that stood before it is still the one that bound."""
    pair = _pair("AAA")
    traces, _ = _size([pair], balance=100_000.0, cap=1_000.0, prices={"AAA": 100.0},
                      expert=_FakeExpert(instruments={"AAA": {"weight": 200.0}}))

    assert traces["AAA"]["quantity"] == 10, "the doubled size was reverted"
    assert traces["AAA"]["binding"] == "instrument_cap", (
        "a reverted weight bound nothing; the cap did")


def test_a_trace_that_breaks_MID_ORDER_still_leaves_the_quantity_alone():
    """The hostile-store test covers the trace being opened. This one breaks every write
    AFTER it is open -- i.e. inside the sizing branches themselves, where an escaping
    exception would land in the per-order handler and zero the order."""
    class _Hostile(dict):
        def update(self, *a, **kw):
            raise RuntimeError("trace write is on fire")

    pair = _pair("AAA")
    mgr = _manager()
    mgr._open_trace = lambda traces, order, **fields: _Hostile()

    orders_to_update, _, _ = mgr._calculate_order_quantities(
        [pair], 100_000.0, 1_000.0, {}, _FakeAccount({"AAA": 100.0}), _FakeExpert(),
        traces={}, context={})

    assert orders_to_update == [pair[0]] and pair[0].quantity == 10


def test_a_ranking_that_cannot_be_read_is_reported_not_swallowed(monkeypatch):
    """It costs the score column, which is the point of the record -- so it must show up in
    the log rather than as a silently empty column.

    The module logger is captured by substitution, not with ``caplog``: the app's logger
    does not propagate to the root, so caplog sees nothing while the line is really emitted.
    """
    class _Hostile:
        @property
        def expected_profit_percent(self):
            raise RuntimeError("recommendation is unreadable")

    warnings = []
    monkeypatch.setattr(trm.logger, "warning", warnings.append)

    fields = trm.TradeRiskManagement._ranking_fields(_Hostile(), {}, "AAA")

    assert fields == {}
    assert any("AAA" in w and "unreadable" in w for w in warnings), warnings


# =========================================================================================
# RE-REVIEW 2026-09-11: a refusal must quote what the sizer actually said
#
# ``compute_risk_based_quantity`` can refuse BEFORE it ever divides the budget: no equity, a
# budget that resolves to zero (reachable with regime_risk_scale 0), or no stop, no ATR and
# no min-stop floor to imply one. It names each case in ``result["reason"]`` -- which was
# logged and thrown away -- and the record then asserted a sentence about "the risk budget
# not covering one share at the stop distance recorded beside it" with neither number on the
# row. The row has to say what the sizer said.
# =========================================================================================

def test_a_risk_atr_order_with_no_stop_at_all_records_the_sizers_own_reason():
    """Budget positive (atr_risk_budget_pct), stop synthesis disabled, no ATR and no floor:
    the sizer never reaches the division, so there is no share count to explain."""
    pair = _pair("AAA")
    expert = _FakeExpert(settings={"sizing_mode": "risk_atr", "atr_risk_budget_pct": 1.0,
                                   "risk_per_trade_pct": -1.0, "min_stop_loss_pct": 0.0},
                         equity=100_000.0)
    traces, _ = _size([pair], balance=100_000.0, cap=1_000_000.0, prices={"AAA": 100.0},
                      expert=expert)
    t = traces["AAA"]

    assert pair[0].stop_price is None, "the premise: no stop was synthesised"
    assert t["quantity"] == 0
    assert "qty_by_risk" not in t, "the budget was never divided, so no count exists"
    assert "no stop price" in t["refusal_reason"], t["refusal_reason"]


def test_that_refusal_is_what_the_row_says(candidate_recorded):
    pair = _pair("AAA")
    order = pair[0]
    expert = _FakeExpert(settings={"sizing_mode": "risk_atr", "atr_risk_budget_pct": 1.0,
                                   "risk_per_trade_pct": -1.0, "min_stop_loss_pct": 0.0},
                         equity=100_000.0)
    traces, _ = _size([pair], balance=100_000.0, cap=1_000_000.0, prices={"AAA": 100.0},
                      expert=expert)
    row = candidate_recorded(candidates=[pair], unfunded=[order], prices={"AAA": 100.0},
                             traces={id(order): traces["AAA"]})["AAA"]

    assert "no stop price" in row["reason"], row["reason"]
    assert "stop distance recorded beside it" not in row["reason"], (
        "there is no stop distance on this row to point at")


def test_an_equityless_expert_gets_the_sizers_words_too():
    pair = _pair("AAA")
    expert = _FakeExpert(settings={"sizing_mode": "risk_atr", "risk_per_trade_pct": 1.0,
                                   "min_stop_loss_pct": 5.0}, equity=0.0)
    traces, _ = _size([pair], balance=100_000.0, cap=1_000_000.0, prices={"AAA": 100.0},
                      expert=expert)

    assert traces["AAA"]["refusal_reason"] == "no equity"
    assert "risk_dollars" not in traces["AAA"]


def test_the_budget_sentence_survives_where_the_budget_really_did_bind(candidate_recorded):
    """The other half: the sizer DID divide, and bought nothing. Then the budget and the
    stop distance are both on the row and the sentence can point at them."""
    pair = _pair("AAA")
    order = pair[0]
    expert = _FakeExpert(settings={"sizing_mode": "risk_atr", "risk_per_trade_pct": 0.001,
                                   "min_stop_loss_pct": 5.0}, equity=100_000.0)
    traces, _ = _size([pair], balance=100_000.0, cap=1_000_000.0, prices={"AAA": 100.0},
                      expert=expert)
    row = candidate_recorded(candidates=[pair], unfunded=[order], prices={"AAA": 100.0},
                             traces={id(order): traces["AAA"]})["AAA"]

    assert traces["AAA"]["qty_by_risk"] == 0
    assert row["binding"] == "risk_atr"
    assert "risk budget" in row["reason"], row["reason"]


# -----------------------------------------------------------------------------------------
# An unknown clamp name must not pass for "nothing clamped"
# -----------------------------------------------------------------------------------------

def test_a_clamp_name_the_record_cannot_map_is_reported(monkeypatch):
    """Leaving ``risk_atr`` standing is the safe fallback, but doing it quietly means a new
    clamp in the sizer shows up as "the budget bound it" on every affected row, for as long
    as nobody notices."""
    warnings = []
    monkeypatch.setattr(trm.logger, "warning", warnings.append)
    mgr = _manager()

    assert mgr._clamp_binding("a_new_clamp") is None
    assert any("a_new_clamp" in w for w in warnings), warnings


def test_a_clamp_name_it_knows_is_mapped_quietly(monkeypatch):
    warnings = []
    monkeypatch.setattr(trm.logger, "warning", warnings.append)
    mgr = _manager()

    assert mgr._clamp_binding("notional") == "instrument_cap"
    assert mgr._clamp_binding("balance") == "balance"
    assert mgr._clamp_binding(None) is None
    assert warnings == []


def test_the_map_covers_every_clamp_the_sizer_can_report():
    """Pins the two vocabularies together at the source. A third clamp added to
    ``compute_risk_based_quantity`` fails HERE, at the map, rather than quietly in a run
    record months later."""
    import re
    from pathlib import Path

    source = Path(trm.__file__).resolve().parents[0] / "position_sizing.py"
    produced = set(re.findall(r'out\["capped_by"\]\s*=\s*"([^"]+)"',
                              source.read_text(encoding="utf-8")))

    assert produced == set(trm._RISK_CLAMP_BINDINGS), (
        f"the sizer reports {sorted(produced)}; the record maps "
        f"{sorted(trm._RISK_CLAMP_BINDINGS)}")


def test_an_instrument_config_that_cannot_be_read_is_reported_not_swallowed(monkeypatch):
    """It costs the entire Weight column, which is half of what the record was extended
    for -- and a silently empty column reads as "no weights are configured"."""
    class _Boom:
        def _get_enabled_instruments_config(self):
            raise RuntimeError("settings are down")

    warnings = []
    monkeypatch.setattr(trm.logger, "warning", warnings.append)

    assert trm.TradeRiskManagement._safe_instrument_config(_Boom()) == {}
    assert any("settings are down" in w for w in warnings), warnings
