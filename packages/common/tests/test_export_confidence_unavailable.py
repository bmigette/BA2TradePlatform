"""A confidence an expert never computes must not be printed as "0.0%".

FactorRanker is basket-level: every Recommendation it builds passes a LITERAL
``0.0`` confidence (there is no per-name conviction to report — the decision is
a ranking, not a score). SYMBOL360 then renders "Confidence: 0.0%", which reads
as "zero conviction in this name". That is the same unknown-reads-as-zero
defect as the composite, one line higher up the card.

Experts that DO compute a confidence are untouched: the opt-in is a single
class attribute, default None.
"""
from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_common.core.interfaces.ExpertDataExportInterface import (
    ExpertDataExportInterface,
)
from ba2_common.core.types import OrderRecommendation, Recommendation


class _RealConfidenceExpert(ExpertDataExportInterface):
    @classmethod
    def get_merged_settings_definitions(cls):
        return {}

    @classmethod
    def get_settings_definitions(cls):
        return {}

    @classmethod
    def _ensure_builtin_settings(cls):
        return None

    def analyze_as_of(self, as_of, context):
        return Recommendation(OrderRecommendation.BUY, 72.5, 10.0, "ok")


class _NoConfidenceExpert(_RealConfidenceExpert):
    EXPORT_CONFIDENCE_UNAVAILABLE_REASON = (
        "FactorRanker ranks a universe; it computes no per-symbol confidence.")

    def analyze_as_of(self, as_of, context):
        return Recommendation(OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked book")


def _resolver(cat, name, **kw):
    return None


def test_an_expert_that_computes_a_confidence_still_reports_it():
    result = _RealConfidenceExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is None, result.error
    assert result.confidence == 72.5
    assert result.confidence_unavailable_reason is None


def test_a_declared_placeholder_confidence_is_reported_as_unavailable():
    result = _NoConfidenceExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is None, result.error
    assert result.confidence is None
    assert "no per-symbol confidence" in result.confidence_unavailable_reason


def test_a_real_zero_confidence_is_not_swallowed_by_the_opt_in():
    """The inverse error: an expert WITHOUT the declaration that genuinely
    scores 0.0 confidence must still report 0.0, not 'unavailable'."""
    class _GenuineZero(_RealConfidenceExpert):
        def analyze_as_of(self, as_of, context):
            return Recommendation(OrderRecommendation.HOLD, 0.0, 10.0, "flat")

    result = _GenuineZero.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.confidence == 0.0
    assert result.confidence_unavailable_reason is None


def test_the_default_is_off_so_no_existing_expert_changes():
    assert ExpertDataExportInterface.EXPORT_CONFIDENCE_UNAVAILABLE_REASON is None
    assert ExpertDataExportInterface.EXPORT_SIGNAL_UNAVAILABLE_REASON is None


# --------------------------------------------------------------------------
# The same defect for the header BADGE
# --------------------------------------------------------------------------

class _AlwaysSameSignalExpert(_RealConfidenceExpert):
    """A basket expert whose Recommendation.signal is a fixed constant: it is
    "here is the ranked book", not a per-symbol verdict, so the card's badge
    says BUY for literally every symbol ever searched."""
    EXPORT_SIGNAL_UNAVAILABLE_REASON = (
        "Basket-level expert: the signal describes the whole ranked book, "
        "not this symbol.")

    def analyze_as_of(self, as_of, context):
        return Recommendation(OrderRecommendation.OVERWEIGHT, 0.0, None, "Ranked book")


def test_a_declared_constant_signal_is_reported_as_unavailable():
    result = _AlwaysSameSignalExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.error is None, result.error
    assert result.overall_signal is None
    assert "whole ranked book" in result.signal_unavailable_reason


def test_a_real_signal_is_still_reported():
    result = _RealConfidenceExpert.export_symbol_data("AAPL", providers_resolver=_resolver)
    assert result.overall_signal == "buy"
    assert result.signal_unavailable_reason is None
