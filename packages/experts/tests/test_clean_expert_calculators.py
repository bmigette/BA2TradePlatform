"""Pure decision-logic tests for the clean FMP experts.

These exercise the extracted pure calculators (no DB, no providers, no LLM):
evaluate_earnings_drift (PEAD) and detect_insider_cluster (insider cluster-buy).
They are the key payoff of the extraction — real decision logic preserved and
unit-testable directly.
"""
from datetime import datetime, timezone

from ba2_experts.FMPEarningsDrift import evaluate_earnings_drift
from ba2_experts.FMPInsiderClusterBuy import detect_insider_cluster

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


def test_earnings_drift_fresh_beat_signals():
    row = {"report_date": "2026-06-10", "reported_eps": 1.20, "estimated_eps": 1.00,
           "surprise_percent": 20.0}
    out = evaluate_earnings_drift(row, NOW, surprise_min_pct=5.0, max_days_since_report=30)
    assert out["is_signal"] is True
    assert out["surprise_pct"] == 20.0
    assert out["days_since_report"] == 3
    assert 55.0 <= out["confidence"] <= 100.0


def test_earnings_drift_stale_report_no_signal():
    row = {"report_date": "2026-04-01", "reported_eps": 1.2, "estimated_eps": 1.0,
           "surprise_percent": 20.0}
    out = evaluate_earnings_drift(row, NOW, surprise_min_pct=5.0, max_days_since_report=30)
    assert out["is_signal"] is False
    assert "not fresh" in out["reason"]


def test_earnings_drift_below_threshold_no_signal():
    row = {"report_date": "2026-06-12", "reported_eps": 1.01, "estimated_eps": 1.00,
           "surprise_percent": 1.0}
    out = evaluate_earnings_drift(row, NOW, surprise_min_pct=5.0, max_days_since_report=30)
    assert out["is_signal"] is False


def test_earnings_drift_no_data():
    out = evaluate_earnings_drift(None, NOW, 5.0, 30)
    assert out["is_signal"] is False and out["reason"] == "no earnings data"


def test_insider_cluster_three_buyers_signals():
    txns = [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
    ]
    out = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
    assert out["is_cluster"] is True
    assert out["buyer_count"] == 3
    assert out["buy_value"] == 300_000
    assert out["confidence"] > 55.0


def test_insider_cluster_two_buyers_no_signal():
    txns = [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
    ]
    out = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
    assert out["is_cluster"] is False and out["buyer_count"] == 2


def test_insider_cluster_sells_reduce_confidence():
    txns = [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "D", "transaction_type": "S-Sale", "value": 150_000},
    ]
    out = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
    assert out["is_cluster"] is True
    assert out["sell_value"] == 150_000
