"""ExpertRecommendation.subtype column (P1d / gap #5): the backtest rec writer stamps which
use-case produced each rec (ENTER_MARKET vs OPEN_POSITIONS) so the OPEN_POSITIONS manage pass can
select by subtype directly. The column is nullable + ORM-round-trips by enum NAME (mirrors
MarketAnalysis.subtype); unstamped callers leave it None (the live selection's all-rec fallback
still finds those)."""
from datetime import datetime

from app.services.backtest.backtest_db import backtest_trading_db
from app.services.backtest.daily_engine import _recommendation_to_expert_recommendation
from ba2_common.core.db import get_instance
from ba2_common.core.models import ExpertRecommendation
from ba2_common.core.types import AnalysisUseCase, OrderRecommendation, Recommendation

_D = datetime(2024, 1, 3)


def _rec(action=OrderRecommendation.BUY):
    return Recommendation(signal=action, confidence=80.0, current_price=100.0,
                          details="t", expected_profit_percent=10.0)


def test_writer_stamps_enter_and_open_positions_subtype():
    with backtest_trading_db("subtype-writer"):
        enter_id = _recommendation_to_expert_recommendation(
            _rec(), expert_instance_id=1, symbol="AAPL", as_of=_D,
            subtype=AnalysisUseCase.ENTER_MARKET)
        manage_id = _recommendation_to_expert_recommendation(
            _rec(OrderRecommendation.HOLD), expert_instance_id=1, symbol="MSFT", as_of=_D,
            allow_hold=True, subtype=AnalysisUseCase.OPEN_POSITIONS)
        # ORM round-trip by enum NAME
        assert get_instance(ExpertRecommendation, enter_id).subtype == AnalysisUseCase.ENTER_MARKET
        assert get_instance(ExpertRecommendation, manage_id).subtype == AnalysisUseCase.OPEN_POSITIONS


def test_writer_defaults_subtype_none_when_unstamped():
    with backtest_trading_db("subtype-none"):
        rid = _recommendation_to_expert_recommendation(
            _rec(), expert_instance_id=1, symbol="AAPL", as_of=_D)
        assert get_instance(ExpertRecommendation, rid).subtype is None


def test_subtype_column_exists_via_create_all():
    from sqlmodel import Session, text
    from ba2_common.core.db import get_db
    with backtest_trading_db("subtype-col"):
        with Session(get_db().bind) as s:
            cols = [r[1] for r in s.exec(text("PRAGMA table_info(expertrecommendation)"))]
            assert "subtype" in cols
