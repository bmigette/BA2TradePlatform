"""The services hold to what the INLINE Overview code computed (``stats_golden.json``, captured by
``capture_stats.py`` before the extraction)."""
import json
from pathlib import Path

import pytest

from ba2_trade_platform.services import stats
from tests.golden_overview.seed import NOW, POSITIONS, seed_world

GOLDEN = json.loads(Path(__file__).with_name("stats_golden.json").read_text())


@pytest.fixture
def ids():
    got = seed_world()
    assert got == GOLDEN["ids"]
    return got


def _scopes(ids):
    return {"all": None, "acct1": ids["acct1"], "acct2": ids["acct2"]}


def test_trade_performance_equals_the_inline_widget(ids):
    for name, acct in _scopes(ids).items():
        got = stats.trade_performance(acct, now=NOW)
        want = GOLDEN["trade_performance"][name]
        assert {k: got[k] for k in want} == want, name


def test_order_counts_equal_the_inline_widget(ids):
    for name, acct in _scopes(ids).items():
        got = [[r["account"], r["open"], r["pending"], r["error"]] for r in stats.order_counts(acct)]
        assert got == GOLDEN["order_counts"][name], name


def test_profit_per_expert_equals_the_inline_chart(ids):
    for name, acct in _scopes(ids).items():
        got = [[k, v] for k, v in stats.profit_per_expert(acct).items()]
        assert got == GOLDEN["profit_per_expert"][name], name


def test_position_distribution_equals_the_inline_chart(ids):
    for grouping in ("labels", "categories"):
        got = [[k, v] for k, v in stats.position_distribution(POSITIONS, grouping).items()]
        assert got == GOLDEN["distribution"][grouping], grouping


def test_closed_trade_without_a_computable_pnl_is_reported_not_zeroed(ids):
    got = stats.trade_performance(ids["acct1"], now=NOW)
    assert got["unmeasured_30d"] == 1 and got["unmeasured_7d"] == 1      # IBM: no close price


def test_a_position_with_no_market_value_is_skipped_not_fatal(ids):
    skipped = []
    got = stats.position_distribution(POSITIONS + [{"symbol": "AAPL", "market_value": None}], "labels", skipped)
    assert [[k, v] for k, v in got.items()] == GOLDEN["distribution"]["labels"]      # the survivors are unchanged
    assert {"symbol": "AAPL", "reason": "TypeError"} in skipped


def test_any_exception_on_one_row_skips_that_row_only_in_both_charts(ids, monkeypatch):
    """The charts' own behaviour at 75431922: a per-row ``except Exception``, whatever its type."""
    skipped = []
    got = stats.position_distribution(POSITIONS + [{"symbol": "AAPL", "market_value": object()}], "labels", skipped)
    assert [[k, v] for k, v in got.items()] == GOLDEN["distribution"]["labels"] and skipped

    real = stats.calculate_transaction_pnl

    def flaky(tx):
        if tx.symbol == "AAPL":
            raise RuntimeError("odd row")
        return real(tx)
    monkeypatch.setattr(stats, "calculate_transaction_pnl", flaky)
    skipped = []
    got = stats.profit_per_expert(ids["acct1"], skipped)
    assert {"transaction": 1, "reason": "RuntimeError"} in skipped
    assert "Alpha-1" in got and got["Alpha-1"] == GOLDEN["profit_per_expert"]["acct1"][1][1] - 100.0   # AAPL's +100 is out
