"""Engine seam generalizations for PremiumSeller (spec §3.3):
manager-class resolution (default FactorPortfolioManager — FactorRanker
byte-identical) and manage-cadence routing (default off)."""
from ba2_experts.FactorRanker.portfolio import FactorPortfolioManager
from ba2_experts.PremiumSeller import PremiumSeller
from ba2_experts.PremiumSeller.portfolio import OptionPortfolioManager


def test_resolve_default_is_factor_ranker_manager():
    from app.services.backtest.daily_engine import _resolve_bypass_manager_class
    from ba2_experts.FactorRanker import FactorRanker
    assert _resolve_bypass_manager_class(FactorRanker) is FactorPortfolioManager


def test_resolve_premium_seller_manager():
    from app.services.backtest.daily_engine import _resolve_bypass_manager_class
    assert _resolve_bypass_manager_class(PremiumSeller) is OptionPortfolioManager


def test_run_kind_factor_ranker_entry_only():
    from app.services.backtest.daily_engine import _bypass_run_kind
    from ba2_experts.FactorRanker import FactorRanker
    stub = type("E", (), {"bypasses_classic_rm": True})()   # no manages flag -> default False
    assert _bypass_run_kind(stub, entry_ok=True, manage_ok=True) == "entry"
    assert _bypass_run_kind(stub, entry_ok=False, manage_ok=True) is None
    assert getattr(FactorRanker, "manages_between_entries", False) is False


def test_run_kind_premium_seller_manage_bars():
    from app.services.backtest.daily_engine import _bypass_run_kind
    assert _bypass_run_kind(PremiumSeller, entry_ok=True, manage_ok=True) == "entry"
    assert _bypass_run_kind(PremiumSeller, entry_ok=False, manage_ok=True) == "manage"
    assert _bypass_run_kind(PremiumSeller, entry_ok=False, manage_ok=False) is None


def test_premium_seller_registered_in_handler_map():
    from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS
    assert "PremiumSeller" in _SUPPORTED_EXPERTS
