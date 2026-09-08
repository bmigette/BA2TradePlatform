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
             prices=None, weights=None):
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
