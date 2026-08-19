"""FMPInsiderClusterBuy.export_symbol_data: cluster/buyer/seller metric rows.

Fixtures (FakeInsider/FakeOHLCV/THREE_BUYERS/TWO_BUYERS) are duplicated from
test_insider_cluster_gather_process.py rather than imported -- this mirrors
test_deterministic_scorer_export.py's convention (its own local fixtures,
not shared via conftest.py) for the *_export.py test files in this directory.

No skip test here: FMPInsiderClusterBuy._process never sets skip=True (see
FMPInsiderClusterBuy.py -- unlike DeterministicScorer, it has no "insufficient
data" branch; a non-dict/empty insider response just resolves to an empty,
non-cluster HOLD via detect_insider_cluster's own zero-value defaults).
"""
from datetime import datetime, timezone

import pandas as pd

from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)

THREE_BUYERS = {
    "start_date": "2026-05-14T00:00:00", "end_date": "2026-06-13T00:00:00",
    "transactions": [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
    ],
}
TWO_BUYERS = {
    "start_date": "2026-05-14T00:00:00", "end_date": "2026-06-13T00:00:00",
    "transactions": [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
    ],
}


class FakeInsider:
    def __init__(self, payload):
        self._payload = payload

    def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                 as_of=None, format_type="dict", **kw):
        return self._payload


class FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [100.0]})


def _resolver(insider_payload):
    return lambda cat, name, **kw: {
        "insider": FakeInsider(insider_payload), "ohlcv": FakeOHLCV(),
    }[cat]


def test_export_symbol_data_shows_cluster_rows():
    result = FMPInsiderClusterBuy.export_symbol_data(
        "AAPL", overrides={"min_insiders": 3, "min_total_value": 200_000.0,
                          "expected_profit_percent": 10.0},
        as_of=NOW, providers_resolver=_resolver(THREE_BUYERS))
    assert result.error is None, result.error
    assert result.overall_signal == "buy"
    assert result.skipped is False

    labels = {m.label for m in result.metrics}
    assert "Cluster detected" in labels
    assert "Open-market buyers" in labels
    assert "Buy value" in labels
    assert "Sell value" in labels
    assert "Buyer: A" in labels
    assert "Buyer: B" in labels
    assert "Buyer: C" in labels

    cluster_row = next(m for m in result.metrics if m.label == "Cluster detected")
    assert cluster_row.value is True
    assert cluster_row.display == "Yes"
    assert cluster_row.signal == "buy"

    buyers = next(m for m in result.metrics if m.label == "Open-market buyers")
    assert buyers.value == 3
    assert buyers.display == "3"

    buy_value = next(m for m in result.metrics if m.label == "Buy value")
    assert buy_value.value == 300_000
    assert buy_value.display == "$300,000"

    sell_value = next(m for m in result.metrics if m.label == "Sell value")
    assert sell_value.value == 0.0
    assert sell_value.display == "$0"
    assert sell_value.signal is None   # no selling against the cluster

    buyer_a = next(m for m in result.metrics if m.label == "Buyer: A")
    assert buyer_a.value == 100_000
    assert buyer_a.display == "$100,000"
    assert buyer_a.signal == "buy"


def test_export_symbol_data_not_a_cluster_is_hold():
    """Only 2 distinct buyers (< min_insiders=3): detect_insider_cluster
    returns is_cluster=False -> HOLD. The individual buyer rows still list
    the 2 real purchases that DID happen (they're below the cluster bar, not
    nonexistent) -- only the top-line signal/cluster-detected fields flip."""
    result = FMPInsiderClusterBuy.export_symbol_data(
        "AAPL", overrides={"min_insiders": 3, "min_total_value": 200_000.0,
                          "expected_profit_percent": 10.0},
        as_of=NOW, providers_resolver=_resolver(TWO_BUYERS))
    assert result.error is None, result.error
    assert result.overall_signal == "hold"
    assert result.skipped is False

    cluster_row = next(m for m in result.metrics if m.label == "Cluster detected")
    assert cluster_row.value is False
    assert cluster_row.display == "No"
    assert cluster_row.signal == "neutral"

    buyers = next(m for m in result.metrics if m.label == "Open-market buyers")
    assert buyers.value == 2

    labels = {m.label for m in result.metrics}
    assert "Buyer: A" in labels
    assert "Buyer: B" in labels
    assert "Buyer: C" not in labels
