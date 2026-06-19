import pytest
from datetime import datetime, timezone
from ba2_common.core.interfaces import MarketExpertInterface, BacktestInterface
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.types import Recommendation, OrderRecommendation


class _StubExpert(MarketExpertInterface):
    SETTING_MODEL = None  # not used in this stub path

    def __init__(self):
        # Bypass the DB-reading base __init__: this stub exercises only the
        # _gather/_process/analyze_as_of seam, not settings persistence.
        pass

    @classmethod
    def description(cls):
        return "stub"

    def render_market_analysis(self, ma):
        return ""

    def run_analysis(self, symbol, market_analysis):
        # run_analysis is @abstractmethod on MarketExpertInterface; a no-op
        # concrete impl is required for the stub to be instantiable.
        return None

    def _gather(self, providers, as_of):
        return {"current_price": 100.0, "as_of": as_of}

    def _process(self, bundle, settings, as_of=None):
        return Recommendation(OrderRecommendation.BUY, 70.0, bundle["current_price"],
                              "stub-details", settings.get("expected_profit_percent"))


def test_analyze_as_of_runs_gather_then_process():
    ctx = BacktestContext(providers=LiveProviderBundle(lambda *a, **k: None),
                          settings={"expected_profit_percent": 5.0},
                          as_of=datetime(2026, 6, 13, tzinfo=timezone.utc))
    rec = _StubExpert().analyze_as_of(ctx.as_of, ctx)
    assert isinstance(rec, BacktestInterface) is False  # rec is a value object, not the iface
    assert rec.signal == OrderRecommendation.BUY and rec.expected_profit_percent == 5.0


def test_refactored_expert_satisfies_backtest_protocol():
    # A refactored expert (with analyze_as_of) structurally satisfies BacktestInterface.
    assert isinstance(_StubExpert(), BacktestInterface)


def test_unrefactored_expert_fails_loud():
    class Bad(MarketExpertInterface):
        def __init__(self):
            pass

        @classmethod
        def description(cls):
            return "bad"

        def render_market_analysis(self, ma):
            return ""

        def run_analysis(self, symbol, market_analysis):
            return None

    with pytest.raises(NotImplementedError):
        Bad()._gather(None, None)


def test_unrefactored_expert_process_fails_loud():
    class Bad(MarketExpertInterface):
        def __init__(self):
            pass

        @classmethod
        def description(cls):
            return "bad"

        def render_market_analysis(self, ma):
            return ""

        def run_analysis(self, symbol, market_analysis):
            return None

    with pytest.raises(NotImplementedError):
        Bad()._process({}, {}, None)
