"""execute_cleanup / preview_cleanup must never delete analyses an open position still needs.

Two defects pinned here:

1. Protection covered only ``Transaction.status == OPENED``. A WAITING (entry not yet
   filled) or CLOSING (exit resting) transaction still holds a position, so its
   analysis/recommendation chain must be protected too. CLOSED is not protected.
2. The protection query FAILED OPEN: any exception returned an empty set, so every
   analysis looked unprotected. A failed protection query must abort the run loudly.

Never ``caplog`` -- ``logger.py`` sets ``propagate = False``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, func, select

from ba2_trade_platform.core import cleanup as cleanup_mod
from ba2_trade_platform.core.models import (
    AnalysisOutput,
    ExpertRecommendation,
    MarketAnalysis,
    TradingOrder,
    Transaction,
)
from ba2_trade_platform.core.types import (
    MarketAnalysisStatus,
    OrderDirection,
    OrderRecommendation,
    OrderType,
    RiskLevel,
    TimeHorizon,
    TransactionStatus,
)

OLD = datetime.now(timezone.utc) - timedelta(days=400)


def _build_chain(engine, txn_status):
    """Old analysis -> output + recommendation -> order -> Transaction(txn_status)."""
    with Session(engine) as s:
        analysis = MarketAnalysis(expert_instance_id=1, symbol="AAPL",
                                  status=MarketAnalysisStatus.COMPLETED, created_at=OLD)
        s.add(analysis)
        s.commit()
        s.refresh(analysis)
        s.add(AnalysisOutput(market_analysis_id=analysis.id, name="o", type="t", text="x"))
        rec = ExpertRecommendation(
            instance_id=1, market_analysis_id=analysis.id, symbol="AAPL",
            recommended_action=OrderRecommendation.BUY, expected_profit_percent=5.0,
            price_at_date=100.0, details="d", confidence=70.0,
            risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM)
        s.add(rec)
        txn = Transaction(symbol="AAPL", quantity=1.0, side=OrderDirection.BUY,
                          status=txn_status, expert_id=1)
        s.add(txn)
        s.commit()
        s.refresh(rec)
        s.refresh(txn)
        s.add(TradingOrder(account_id=1, symbol="AAPL", quantity=1.0,
                           side=OrderDirection.BUY, order_type=OrderType.MARKET,
                           good_for=1, filled_qty=0.0, comment="c",
                           transaction_id=txn.id, expert_recommendation_id=rec.id))
        s.commit()
        return analysis.id


def _count(engine, model):
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(model)).one()


def _capture(monkeypatch, level):
    messages = []
    monkeypatch.setattr(cleanup_mod.logger, level, lambda msg, *a, **k: messages.append(str(msg)))
    return messages


@pytest.mark.parametrize("status", [TransactionStatus.WAITING, TransactionStatus.OPENED,
                                    TransactionStatus.CLOSING])
def test_held_position_statuses_protect_the_analysis(test_engine, status):
    aid = _build_chain(test_engine, status)
    with Session(test_engine) as s:
        assert cleanup_mod._get_analysis_ids_with_open_transactions(s, [aid]) == {aid}

    result = cleanup_mod.execute_cleanup(days_to_keep=30)

    assert result['success'] is True
    assert result['analyses_deleted'] == 0
    assert result['analyses_protected'] == 1
    assert _count(test_engine, MarketAnalysis) == 1
    assert _count(test_engine, ExpertRecommendation) == 1
    assert _count(test_engine, AnalysisOutput) == 1


def test_closed_transaction_does_not_protect(test_engine):
    aid = _build_chain(test_engine, TransactionStatus.CLOSED)
    with Session(test_engine) as s:
        assert cleanup_mod._get_analysis_ids_with_open_transactions(s, [aid]) == set()

    result = cleanup_mod.execute_cleanup(days_to_keep=30)

    assert result['success'] is True
    assert result['analyses_deleted'] == 1
    assert _count(test_engine, MarketAnalysis) == 0


@pytest.fixture
def broken_protection_query(monkeypatch):
    """Make ONLY the protection join raise, like a locked database would."""
    real_exec = Session.exec

    def exec_(self, statement, *a, **k):
        if "JOIN" in str(statement):
            raise OperationalError("SELECT ...", {}, Exception("database is locked"))
        return real_exec(self, statement, *a, **k)

    monkeypatch.setattr(Session, "exec", exec_)


def test_helper_raises_instead_of_returning_empty_set(test_engine, broken_protection_query,
                                                      monkeypatch):
    aid = _build_chain(test_engine, TransactionStatus.OPENED)
    errors = _capture(monkeypatch, "error")
    with Session(test_engine) as s:
        with pytest.raises(OperationalError):
            cleanup_mod._get_analysis_ids_with_open_transactions(s, [aid])
    assert errors, "the failure must be logged at ERROR"


def test_execute_cleanup_aborts_without_deleting_when_protection_fails(
        test_engine, broken_protection_query, monkeypatch):
    _build_chain(test_engine, TransactionStatus.OPENED)
    errors = _capture(monkeypatch, "error")

    result = cleanup_mod.execute_cleanup(days_to_keep=30)

    assert result['success'] is False
    assert result['analyses_deleted'] == 0
    assert result['errors'] and "locked" in " ".join(result['errors'])
    assert errors
    assert _count(test_engine, MarketAnalysis) == 1
    assert _count(test_engine, ExpertRecommendation) == 1
    assert _count(test_engine, AnalysisOutput) == 1


def test_preview_cleanup_raises_when_protection_fails(test_engine, broken_protection_query):
    _build_chain(test_engine, TransactionStatus.OPENED)
    with pytest.raises(OperationalError):
        cleanup_mod.preview_cleanup(days_to_keep=30)
