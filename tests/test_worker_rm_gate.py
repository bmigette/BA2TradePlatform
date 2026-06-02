"""Tests for the ``expert_uses_risk_manager`` resolver.

The resolver decides whether the platform's risk manager should be triggered
after an expert's analysis completes. It must be robust to experts that declare
``uses_risk_manager`` only via ``get_expert_properties()`` (e.g. FactorRanker)
as well as those that declare it as a class attribute.
"""

from ba2_trade_platform.core.utils import expert_uses_risk_manager
from ba2_trade_platform.modules.experts.FactorRanker import FactorRanker
from ba2_trade_platform.modules.experts.FMPRating import FMPRating


class _PropsFalse:
    """Declares uses_risk_manager=False via get_expert_properties only."""

    @classmethod
    def get_expert_properties(cls):
        return {"uses_risk_manager": False}


class _NoSignal:
    """No risk manager signal anywhere -> default True."""

    @classmethod
    def get_expert_properties(cls):
        return {}


class _ClassAttrFalse:
    """Declares uses_risk_manager=False via class attribute only."""

    uses_risk_manager = False

    @classmethod
    def get_expert_properties(cls):
        return {}


class _PropsOverridesClassAttr:
    """get_expert_properties takes precedence over the class attribute."""

    uses_risk_manager = True

    @classmethod
    def get_expert_properties(cls):
        return {"uses_risk_manager": False}


class _Broken:
    """get_expert_properties raises -> resolver defaults to True."""

    @classmethod
    def get_expert_properties(cls):
        raise RuntimeError("boom")


def test_props_false_returns_false():
    assert expert_uses_risk_manager(_PropsFalse) is False


def test_no_signal_returns_true():
    assert expert_uses_risk_manager(_NoSignal) is True


def test_class_attr_false_returns_false():
    assert expert_uses_risk_manager(_ClassAttrFalse) is False


def test_props_overrides_class_attr():
    assert expert_uses_risk_manager(_PropsOverridesClassAttr) is False


def test_broken_defaults_to_true():
    assert expert_uses_risk_manager(_Broken) is True


def test_factorranker_resolves_to_false():
    # FactorRanker self-executes via FactorPortfolioManager and declares
    # uses_risk_manager=False only through get_expert_properties().
    assert expert_uses_risk_manager(FactorRanker) is False


def test_fmprating_resolves_to_true():
    # FMPRating does not opt out, so it uses the platform risk manager.
    assert expert_uses_risk_manager(FMPRating) is True


# --------------------------------------------------------------------------- #
# expert_schedules_open_positions resolver (open-positions schedule visibility)
# --------------------------------------------------------------------------- #

from ba2_trade_platform.core.utils import expert_schedules_open_positions


def test_schedules_open_positions_false_via_properties():
    class _Exp:
        @classmethod
        def get_expert_properties(cls):
            return {"schedules_open_positions": False}
    assert expert_schedules_open_positions(_Exp) is False


def test_schedules_open_positions_defaults_true():
    class _Exp:
        @classmethod
        def get_expert_properties(cls):
            return {}
    assert expert_schedules_open_positions(_Exp) is True


def test_schedules_open_positions_factorranker_is_false():
    from ba2_trade_platform.modules.experts.FactorRanker import FactorRanker
    assert expert_schedules_open_positions(FactorRanker) is False
