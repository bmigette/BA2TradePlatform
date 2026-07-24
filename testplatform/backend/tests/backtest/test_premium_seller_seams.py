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


# --------------------------------------------------------------------------- #
# _apply_bypass_stops capability guard (Task 5b): the per-bar bypass stop pass
# must SKIP a portfolio manager that owns its exits and has no apply_stop_losses
# (PremiumSeller's OptionPortfolioManager), instead of AttributeErroring into the
# broad except -> returning True -> book_dirty + a log line EVERY non-entry bar.
# --------------------------------------------------------------------------- #

def test_bypass_stops_skipped_when_manager_lacks_apply_stop_losses():
    """Stub bypass expert + stub account with a held position; the pre-seeded manager
    has NO apply_stop_losses (like OptionPortfolioManager, whose exits are owned by
    manage_open). The stop pass must return False (nothing submitted), raise nothing,
    and leave the manager untouched."""
    from datetime import datetime
    from app.services.backtest.daily_engine import DailyBacktestEngine

    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)  # no __init__ / no DB needed
    engine._bypass_veq_pct = {}

    class _Account:
        def get_positions(self):
            return [{"symbol": "AAPL", "qty": 1}]

        def get_balance(self):
            return 1000.0

    engine.account = _Account()

    class _Expert:
        bypasses_classic_rm = True

        def get_setting_with_interface_default(self, key, log_warning=False):
            return 1.0

    class _NoStopsManager:
        """Like OptionPortfolioManager: no apply_stop_losses. Any attribute touch beyond
        the capability probe is a test failure (the manager must be left alone)."""

        def __getattr__(self, name):
            if name == "apply_stop_losses":
                raise AttributeError(name)
            raise AssertionError(f"engine must not touch manager.{name}")

    # Pre-seed the per-run manager cache so _bypass_manager returns the stub (no resolver).
    engine._bypass_pm = {7: _NoStopsManager()}

    assert engine._apply_bypass_stops(_Expert(), 7, {}, datetime(2024, 1, 5)) is False


def test_bypass_stops_still_applied_when_manager_supports_it():
    """Control: a manager WITH apply_stop_losses (FactorRanker's FactorPortfolioManager
    shape) is still invoked exactly once with (float(stop_pct), equity=...) and the helper
    returns False when it submits nothing — the guard must not change that path."""
    from datetime import datetime
    from app.services.backtest.daily_engine import DailyBacktestEngine

    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine._bypass_veq_pct = {}

    class _Account:
        def get_positions(self):
            return [{"symbol": "AAPL", "qty": 1}]

        def get_balance(self):
            return 1000.0

    engine.account = _Account()

    class _Expert:
        bypasses_classic_rm = True

        def get_setting_with_interface_default(self, key, log_warning=False):
            return 1.0

    class _StopsManager:
        def __init__(self):
            self.calls = []

        def apply_stop_losses(self, stop_pct, equity=None, prices=None):
            self.calls.append((stop_pct, equity))
            return []  # nothing breached

    pm = _StopsManager()
    engine._bypass_pm = {9: pm}

    assert engine._apply_bypass_stops(_Expert(), 9, {}, datetime(2024, 1, 5)) is False
    assert pm.calls == [(1.0, 1000.0)]
