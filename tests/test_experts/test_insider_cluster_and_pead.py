"""Unit tests for the pure signal calculators of FMPInsiderClusterBuy and
FMPEarningsDrift."""

from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.modules.experts.FMPInsiderClusterBuy import detect_insider_cluster
from ba2_trade_platform.modules.experts.FMPEarningsDrift import evaluate_earnings_drift


def _txn(name, ttype, value):
    return {"insider_name": name, "transaction_type": ttype, "value": value}


class TestDetectInsiderCluster:
    def test_cluster_detected(self):
        txns = [
            _txn("CEO Alice", "P-Purchase", 150_000),
            _txn("CFO Bob", "P-Purchase", 80_000),
            _txn("Dir Carol", "P-Purchase", 50_000),
        ]
        r = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
        assert r["is_cluster"] is True
        assert r["buyer_count"] == 3
        assert r["buy_value"] == pytest.approx(280_000)
        assert r["confidence"] >= 55.0

    def test_single_big_buyer_is_not_a_cluster(self):
        txns = [_txn("CEO Alice", "P-Purchase", 5_000_000)]
        r = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
        assert r["is_cluster"] is False
        assert r["confidence"] == 0.0

    def test_repeat_buys_by_same_insider_count_once(self):
        txns = [_txn("CEO Alice", "P-Purchase", 100_000) for _ in range(5)]
        r = detect_insider_cluster(txns, min_insiders=2, min_total_value=100_000)
        assert r["buyer_count"] == 1
        assert r["is_cluster"] is False

    def test_awards_and_exercises_ignored(self):
        txns = [
            _txn("CEO Alice", "A-Award", 900_000),
            _txn("CFO Bob", "M-Exempt", 900_000),
            _txn("Dir Carol", "P-Purchase", 50_000),
        ]
        r = detect_insider_cluster(txns, min_insiders=2, min_total_value=100_000)
        assert r["buyer_count"] == 1
        assert r["is_cluster"] is False

    def test_value_threshold_enforced(self):
        txns = [
            _txn("CEO Alice", "P-Purchase", 50_000),
            _txn("CFO Bob", "P-Purchase", 40_000),
            _txn("Dir Carol", "P-Purchase", 30_000),
        ]
        r = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
        assert r["is_cluster"] is False

    def test_concurrent_selling_reduces_confidence(self):
        base = [
            _txn("CEO Alice", "P-Purchase", 200_000),
            _txn("CFO Bob", "P-Purchase", 200_000),
            _txn("Dir Carol", "P-Purchase", 200_000),
        ]
        clean = detect_insider_cluster(base, 3, 200_000)
        with_sales = detect_insider_cluster(
            base + [_txn("VP Dave", "S-Sale", 600_000)], 3, 200_000)
        assert with_sales["confidence"] < clean["confidence"]

    def test_confidence_clamped_to_100(self):
        txns = [_txn(f"Insider {i}", "P-Purchase", 2_000_000) for i in range(10)]
        r = detect_insider_cluster(txns, 3, 200_000)
        assert r["confidence"] <= 100.0


class TestEvaluateEarningsDrift:
    NOW = datetime(2026, 6, 11, tzinfo=timezone.utc)

    def _row(self, days_ago, surprise_pct=None, reported=None, estimated=None):
        d = (self.NOW - timedelta(days=days_ago)).date().isoformat()
        row = {"report_date": d, "reported_eps": reported, "estimated_eps": estimated}
        if surprise_pct is not None:
            row["surprise_percent"] = surprise_pct
        return row

    def test_fresh_beat_signals(self):
        r = evaluate_earnings_drift(self._row(2, surprise_pct=12.0), self.NOW, 5.0, 10)
        assert r["is_signal"] is True
        assert r["confidence"] > 55.0

    def test_stale_beat_does_not_signal(self):
        r = evaluate_earnings_drift(self._row(25, surprise_pct=12.0), self.NOW, 5.0, 10)
        assert r["is_signal"] is False
        assert "not fresh" in r["reason"]

    def test_small_beat_does_not_signal(self):
        r = evaluate_earnings_drift(self._row(2, surprise_pct=2.0), self.NOW, 5.0, 10)
        assert r["is_signal"] is False
        assert "below" in r["reason"]

    def test_miss_does_not_signal(self):
        r = evaluate_earnings_drift(self._row(2, surprise_pct=-8.0), self.NOW, 5.0, 10)
        assert r["is_signal"] is False

    def test_surprise_derived_from_eps_when_missing(self):
        # reported 1.10 vs estimate 1.00 -> +10%
        r = evaluate_earnings_drift(self._row(2, reported=1.10, estimated=1.00),
                                    self.NOW, 5.0, 10)
        assert r["surprise_pct"] == pytest.approx(10.0, abs=0.01)
        assert r["is_signal"] is True

    def test_no_data_is_graceful(self):
        r = evaluate_earnings_drift(None, self.NOW, 5.0, 10)
        assert r["is_signal"] is False
        assert r["reason"] == "no earnings data"

    def test_fresher_reports_score_higher(self):
        fresh = evaluate_earnings_drift(self._row(1, surprise_pct=10.0), self.NOW, 5.0, 10)
        older = evaluate_earnings_drift(self._row(9, surprise_pct=10.0), self.NOW, 5.0, 10)
        assert fresh["confidence"] > older["confidence"]
