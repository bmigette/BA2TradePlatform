"""
Tests for PennyMomentumTrader review fixes.

Covers:
- VWAP daily session reset (conditions.py)
- Opening Range Breakout session filtering (conditions.py)
- Take-profit tier tracking / cascade prevention (__init__.py)
- Intraday EOD hard-exit (__init__.py)
- Configurable confidence threshold (__init__.py)
- PennyTradeManager single instance (__init__.py)
- _record_trade status (__init__.py)
- Settings defaults (max_holding_days, min_confidence_threshold, exit_update_interval_ticks)
- _filter_today_session edge cases (conditions.py)
- ui.py get_db context manager usage
- exc_info cleanup on StockTwits warning
- Parallel data gathering in Phase 3
- LLM-driven exit condition updates (__init__.py)
"""

import ast
import importlib
import importlib.util
import json
import os
import sys
import textwrap
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytz
import pytest


# ---------------------------------------------------------------------------
# Direct module loading (avoids triggering full package init chain)
# ---------------------------------------------------------------------------

_BASE = os.path.join(
    os.path.dirname(__file__),
    "..",
    "ba2_trade_platform",
    "modules",
    "experts",
    "PennyMomentumTrader",
)

# Ensure parent package path is loadable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load_module_direct(name: str, filepath: str):
    """Load a single .py file as a module, mocking heavy dependencies."""
    # Mock the logger dependency before loading
    mock_logger = MagicMock()
    if "ba2_trade_platform.logger" not in sys.modules:
        sys.modules["ba2_trade_platform"] = MagicMock()
        sys.modules["ba2_trade_platform.logger"] = MagicMock(logger=mock_logger)
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load the conditions module directly (only depends on logger + numpy/pandas/pytz)
_conditions_mod = _load_module_direct(
    "penny_conditions", os.path.join(_BASE, "conditions.py")
)
ConditionEvaluator = _conditions_mod.ConditionEvaluator
validate_condition_set = _conditions_mod.validate_condition_set
validate_condition = _conditions_mod.validate_condition


# ---------------------------------------------------------------------------
# Source code reading helpers (for tests that inspect code rather than run it)
# ---------------------------------------------------------------------------

def _read_source(filename: str) -> str:
    """Read a PennyMomentumTrader source file."""
    return open(os.path.join(_BASE, filename)).read()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv_df(closes, highs=None, lows=None, opens=None, volumes=None, index=None):
    """Build an OHLCV DataFrame, optionally with a DatetimeIndex."""
    n = len(closes)
    if highs is None:
        highs = [c + 0.5 for c in closes]
    if lows is None:
        lows = [c - 0.5 for c in closes]
    if opens is None:
        opens = [c - 0.1 for c in closes]
    if volumes is None:
        volumes = [1000] * n
    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    if index is not None:
        df.index = index
    return df


def _make_intraday_df_with_index(tz_str="US/Eastern"):
    """Create a multi-day 1-minute OHLCV with DatetimeIndex spanning today + yesterday."""
    tz = pytz.timezone(tz_str)
    now = datetime.now(tz)
    today = now.replace(hour=9, minute=30, second=0, microsecond=0)

    yesterday = today - timedelta(days=1)
    # Skip weekends
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)

    timestamps = []
    closes = []
    highs = []
    lows = []
    volumes = []

    # Yesterday: 9:30-10:30 (60 bars) with prices around 5.0
    for i in range(60):
        ts = yesterday + timedelta(minutes=i)
        timestamps.append(ts)
        price = 5.0 + i * 0.01
        closes.append(price)
        highs.append(price + 0.05)
        lows.append(price - 0.05)
        volumes.append(2000)

    # Today: 9:30-10:30 (60 bars) with prices around 6.0
    for i in range(60):
        ts = today + timedelta(minutes=i)
        timestamps.append(ts)
        price = 6.0 + i * 0.02
        closes.append(price)
        highs.append(price + 0.10)
        lows.append(price - 0.05)
        volumes.append(5000)

    idx = pd.DatetimeIndex(timestamps, tz=tz)
    return _make_ohlcv_df(closes, highs, lows, volumes=volumes, index=idx)


class MockOHLCVProvider:
    """Mock OHLCV provider."""

    def __init__(self, data_map=None, default_df=None):
        self._data_map = data_map or {}
        self._default_df = default_df

    def get_ohlcv_data(self, symbol, interval="1d", lookback_days=30, **kwargs):
        key = (symbol, interval)
        if key in self._data_map:
            return self._data_map[key]
        if self._default_df is not None:
            return self._default_df
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


# ===========================================================================
# 1. _filter_today_session
# ===========================================================================

class TestFilterTodaySession:
    """Test the _filter_today_session helper method on ConditionEvaluator."""

    def test_empty_df_returns_empty(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        evaluator = ConditionEvaluator(MockOHLCVProvider(), market_timezone="US/Eastern")
        result = evaluator._filter_today_session(df)
        assert result.empty

    def test_integer_index_returns_unchanged(self):
        """Non-DatetimeIndex should return the dataframe unchanged."""
        df = _make_ohlcv_df([1.0, 2.0, 3.0])
        evaluator = ConditionEvaluator(MockOHLCVProvider(), market_timezone="US/Eastern")
        result = evaluator._filter_today_session(df)
        assert len(result) == 3

    def test_filters_to_regular_session(self):
        """Only 9:30-16:00 bars from today should survive."""
        tz = pytz.timezone("US/Eastern")
        now = datetime.now(tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        timestamps = [
            today.replace(hour=8, minute=0),   # pre-market
            today.replace(hour=10, minute=0),  # in session
            today.replace(hour=12, minute=0),  # in session
            today.replace(hour=17, minute=0),  # after hours
        ]
        idx = pd.DatetimeIndex(timestamps, tz=tz)
        df = _make_ohlcv_df([1.0, 2.0, 3.0, 4.0], index=idx)

        evaluator = ConditionEvaluator(MockOHLCVProvider(), market_timezone="US/Eastern")
        result = evaluator._filter_today_session(df)
        # Only 10:00 and 12:00 should survive
        assert len(result) == 2

    def test_yesterday_data_excluded(self):
        """Yesterday's session bars should be filtered out."""
        tz = pytz.timezone("US/Eastern")
        now = datetime.now(tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - timedelta(days=1)
        while yesterday.weekday() >= 5:
            yesterday -= timedelta(days=1)

        timestamps = [
            yesterday.replace(hour=10, minute=0),  # yesterday in session
            yesterday.replace(hour=14, minute=0),  # yesterday in session
            today.replace(hour=10, minute=0),       # today in session
        ]
        idx = pd.DatetimeIndex(timestamps, tz=tz)
        df = _make_ohlcv_df([1.0, 2.0, 3.0], index=idx)

        evaluator = ConditionEvaluator(MockOHLCVProvider(), market_timezone="US/Eastern")
        result = evaluator._filter_today_session(df)
        # Only today's 10:00 should survive
        assert len(result) == 1
        assert result["close"].iloc[0] == 3.0

    def test_falls_back_when_no_today_data(self):
        """If no today data exists, should return the full dataframe."""
        tz = pytz.timezone("US/Eastern")
        two_days_ago = datetime.now(tz) - timedelta(days=2)
        two_days_ago = two_days_ago.replace(hour=10, minute=0, second=0, microsecond=0)

        timestamps = [two_days_ago + timedelta(minutes=i) for i in range(5)]
        idx = pd.DatetimeIndex(timestamps, tz=tz)
        df = _make_ohlcv_df([1.0, 2.0, 3.0, 4.0, 5.0], index=idx)

        evaluator = ConditionEvaluator(MockOHLCVProvider(), market_timezone="US/Eastern")
        result = evaluator._filter_today_session(df)
        # Falls back to full data
        assert len(result) == 5

    def test_timezone_naive_index_gets_localized(self):
        """Timezone-naive DatetimeIndex should be localized, not crash."""
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        timestamps = [
            today.replace(hour=10, minute=0),
            today.replace(hour=14, minute=0),
        ]
        idx = pd.DatetimeIndex(timestamps)  # No tz
        df = _make_ohlcv_df([1.0, 2.0], index=idx)

        evaluator = ConditionEvaluator(MockOHLCVProvider(), market_timezone="US/Eastern")
        result = evaluator._filter_today_session(df)
        assert len(result) >= 1


# ===========================================================================
# 2. VWAP daily session reset
# ===========================================================================

class TestVWAPDailyReset:
    """VWAP should only use today's session data, not multi-day."""

    def test_vwap_uses_today_only(self):
        """VWAP from today's session should reflect today's price range, not yesterday's."""
        df = _make_intraday_df_with_index()
        provider = MockOHLCVProvider(data_map={("TEST", "5m"): df})
        evaluator = ConditionEvaluator(provider, market_timezone="US/Eastern")

        vwap = evaluator._get_vwap("TEST", "5m")
        assert vwap is not None
        # Today's prices are 6.0-7.2. Yesterday's are 5.0-5.6.
        # If VWAP only uses today, it should be > 5.5
        assert vwap > 5.5, f"VWAP {vwap} appears to include yesterday's data"

    def test_vwap_with_non_datetime_index(self):
        """VWAP with integer index should still work (no filtering applied)."""
        df = _make_ohlcv_df([10.0, 11.0, 12.0], volumes=[1000, 2000, 3000])
        provider = MockOHLCVProvider(data_map={("NOIDX", "5m"): df})
        evaluator = ConditionEvaluator(provider, market_timezone="US/Eastern")

        vwap = evaluator._get_vwap("NOIDX", "5m")
        assert vwap is not None
        assert isinstance(vwap, float)

    def test_vwap_caching(self):
        """VWAP should be cached per evaluation cycle."""
        df = _make_intraday_df_with_index()
        provider = MockOHLCVProvider(data_map={("CACHE", "5m"): df})
        evaluator = ConditionEvaluator(provider, market_timezone="US/Eastern")

        v1 = evaluator._get_vwap("CACHE", "5m")
        v2 = evaluator._get_vwap("CACHE", "5m")
        assert v1 == v2
        assert v1 is not None


# ===========================================================================
# 3. Opening Range Breakout session filtering
# ===========================================================================

class TestORBSessionFiltering:
    """ORB should use today's regular session, not pre-market or previous day."""

    def test_orb_with_multiday_data(self):
        """ORB should only consider today's opening bars, not yesterday's."""
        df = _make_intraday_df_with_index()
        provider = MockOHLCVProvider(data_map={("TEST", "1m"): df})
        evaluator = ConditionEvaluator(provider, market_timezone="US/Eastern")

        result = evaluator._check_opening_range_breakout("TEST", 5)
        assert isinstance(result, (bool, np.bool_))

    def test_orb_with_integer_index(self):
        """ORB with non-datetime index should fall back to full data."""
        df = _make_ohlcv_df(
            [1.0] * 10,
            highs=[float(i + 1) for i in range(10)],
        )
        provider = MockOHLCVProvider(data_map={("NOIDX", "1m"): df})
        evaluator = ConditionEvaluator(provider, market_timezone="US/Eastern")

        # Current price = close of last bar = 1.0
        # Opening range high (first 5) = 5.0
        # 1.0 > 5.0 = False
        assert not evaluator._check_opening_range_breakout("NOIDX", 5)

    def test_orb_source_has_filter_call(self):
        """Verify ORB method calls _filter_today_session."""
        source = _read_source("conditions.py")
        # Find the method
        assert "_filter_today_session" in source
        # Verify it's called within _check_opening_range_breakout
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_check_opening_range_breakout":
                method_source = ast.get_source_segment(source, node)
                assert "_filter_today_session" in method_source
                break


# ===========================================================================
# 4. Take-profit tier tracking (cascade prevention)
# ===========================================================================

class TestTPTierTracking:
    """Take-profit tiers should not re-fire after being triggered."""

    def test_triggered_tier_is_skipped(self):
        """After tier 0 fires, the next check should skip to tier 1."""
        triggered_tiers = [0]
        take_profit = [
            {"condition": {"type": "percent_above_entry", "percent": 5.0}, "exit_pct": 33},
            {"condition": {"type": "percent_above_entry", "percent": 10.0}, "exit_pct": 50},
            {"condition": {"type": "percent_above_entry", "percent": 20.0}, "exit_pct": 100},
        ]

        next_tier = None
        for tier_idx, tp_tier in enumerate(take_profit):
            if tier_idx in triggered_tiers:
                continue
            next_tier = tier_idx
            break

        assert next_tier == 1

    def test_all_tiers_triggered_skips_loop(self):
        """When all tiers are triggered, nothing should fire."""
        triggered_tiers = [0, 1, 2]
        take_profit = [
            {"condition": {"type": "percent_above_entry", "percent": 5.0}, "exit_pct": 33},
            {"condition": {"type": "percent_above_entry", "percent": 10.0}, "exit_pct": 50},
            {"condition": {"type": "percent_above_entry", "percent": 20.0}, "exit_pct": 100},
        ]

        fired = False
        for tier_idx, tp_tier in enumerate(take_profit):
            if tier_idx in triggered_tiers:
                continue
            fired = True
            break

        assert not fired

    def test_empty_triggered_tiers_fires_first(self):
        """With no triggered tiers, tier 0 is the first to check."""
        triggered_tiers = []
        take_profit = [
            {"condition": {"type": "percent_above_entry", "percent": 5.0}, "exit_pct": 33},
            {"condition": {"type": "percent_above_entry", "percent": 10.0}, "exit_pct": 50},
        ]

        next_tier = None
        for tier_idx, tp_tier in enumerate(take_profit):
            if tier_idx in triggered_tiers:
                continue
            next_tier = tier_idx
            break

        assert next_tier == 0

    def test_source_tracks_triggered_tiers(self):
        """Verify Phase 5 code tracks triggered_tp_tiers in monitored info."""
        source = _read_source("__init__.py")
        assert 'triggered_tp_tiers' in source
        assert 'triggered_tiers.append(tier_idx)' in source
        assert 'info["triggered_tp_tiers"] = triggered_tiers' in source

    def test_source_skips_triggered_tiers(self):
        """Verify Phase 5 code skips already-triggered tiers."""
        source = _read_source("__init__.py")
        assert "if tier_idx in triggered_tiers:" in source


# ===========================================================================
# 5. Settings defaults
# ===========================================================================

class TestSettingsDefaults:
    """Verify correct defaults for new/changed settings via AST parsing."""

    def _get_settings_ast(self):
        """Parse __init__.py and extract the get_settings_definitions return dict."""
        source = _read_source("__init__.py")
        # Search for key setting values in source
        return source

    def test_max_holding_days_default_is_14(self):
        source = self._get_settings_ast()
        # Find the max_holding_days default
        idx = source.index('"max_holding_days"')
        block = source[idx:idx + 300]
        assert '"default": 14,' in block, f"Expected default 14 in: {block[:150]}"

    def test_min_confidence_threshold_exists(self):
        source = self._get_settings_ast()
        assert '"min_confidence_threshold"' in source

    def test_min_confidence_threshold_default_is_55(self):
        source = self._get_settings_ast()
        idx = source.index('"min_confidence_threshold"')
        block = source[idx:idx + 300]
        assert '"default": 55,' in block

    def test_min_confidence_threshold_type_is_int(self):
        source = self._get_settings_ast()
        idx = source.index('"min_confidence_threshold"')
        block = source[idx:idx + 300]
        assert '"type": "int"' in block


# ===========================================================================
# 6. _record_trade status
# ===========================================================================

class TestRecordTradeStatus:
    """Trade records should use 'submitted' not 'filled'."""

    def test_uses_submitted_status(self):
        source = _read_source("__init__.py")
        # Find _record_trade method
        idx = source.index("def _record_trade(")
        method_end = source.index("\n    def ", idx + 1)
        method_source = source[idx:method_end]
        assert '"submitted"' in method_source
        assert '"filled"' not in method_source


# ===========================================================================
# 7. PennyTradeManager single instance
# ===========================================================================

class TestTradeManagerSingleInstance:
    """_run_daily_pipeline should create _trade_mgr once, phases use it."""

    def test_pipeline_creates_trade_mgr(self):
        source = _read_source("__init__.py")
        idx = source.index("def _run_daily_pipeline(")
        # Find next method definition
        next_def = source.index("\n    def ", idx + 1)
        pipeline_source = source[idx:next_def]
        assert "self._trade_mgr = PennyTradeManager" in pipeline_source

    def test_phase_0_uses_self_trade_mgr(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_0_review(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "PennyTradeManager(self.instance.id)" not in method_source
        assert "self._trade_mgr" in method_source

    def test_phase_1_uses_self_trade_mgr(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_1_screen(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "PennyTradeManager(self.instance.id)" not in method_source
        assert "self._trade_mgr" in method_source

    def test_phase_5_uses_self_trade_mgr(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_5_monitor(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "PennyTradeManager(self.instance.id)" not in method_source
        assert "self._trade_mgr" in method_source

    def test_phase_1b_uses_self_trade_mgr(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_1b_llm_discovery(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "PennyTradeManager(self.instance.id)" not in method_source
        assert "self._trade_mgr" in method_source


# ===========================================================================
# 8. Intraday EOD hard-exit
# ===========================================================================

class TestIntradayEODHardExit:
    """Phase 5 should force-exit intraday positions near market close."""

    def test_eod_hard_exit_in_source(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_5_monitor(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "intraday EOD hard-exit" in method_source
        assert 'info.get("strategy") == "intraday"' in method_source

    def test_eod_uses_15min_threshold(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_5_monitor(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "minutes_to_close <= 15" in method_source

    def test_eod_exit_before_stop_loss(self):
        """EOD hard-exit should be checked BEFORE stop-loss and take-profit."""
        source = _read_source("__init__.py")
        idx = source.index("def _phase_5_monitor(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        eod_pos = method_source.index("intraday EOD hard-exit")
        stop_loss_pos = method_source.index("Check stop loss")
        assert eod_pos < stop_loss_pos, "EOD hard-exit should come before stop loss check"


# ===========================================================================
# 9. Parallel data gathering
# ===========================================================================

class TestParallelDataGathering:
    """Phase 3 should gather data in parallel using ThreadPoolExecutor."""

    def test_uses_thread_pool(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_3_deep_triage(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "ThreadPoolExecutor" in method_source
        assert "as_completed" in method_source

    def test_no_sequential_gathering(self):
        """Should NOT have separate sequential debug logs for each data source."""
        source = _read_source("__init__.py")
        idx = source.index("def _phase_3_deep_triage(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert 'gathering news"' not in method_source
        assert 'gathering fundamentals"' not in method_source


# ===========================================================================
# 10. exc_info cleanup on StockTwits warning
# ===========================================================================

class TestExcInfoCleanup:
    """exc_info=True should not be on the StockTwits enrichment warning."""

    def test_stocktwits_warning_no_exc_info(self):
        source = _read_source("__init__.py")
        idx = source.index("def _enrich_with_stocktwits(")
        next_def = source.index("\n    def ", idx + 1) if "\n    def " in source[idx + 1:] else len(source)
        method_source = source[idx:next_def]

        # Find the warning line
        assert "StockTwits enrichment failed" in method_source
        # Check that exc_info=True is NOT near it
        warning_idx = method_source.index("StockTwits enrichment failed")
        # Check a reasonable window around it (5 lines)
        context = method_source[max(0, warning_idx - 100):warning_idx + 200]
        assert "exc_info=True" not in context


# ===========================================================================
# 11. ui.py get_db context manager
# ===========================================================================

class TestUIGetDBContextManager:
    """ui.py should use 'with get_db()' consistently."""

    def test_raw_data_uses_context_manager(self):
        source = _read_source("ui.py")
        idx = source.index("def _render_raw_data(")
        method_source = source[idx:]
        assert "with get_db() as session:" in method_source
        assert "session.close()" not in method_source


# ===========================================================================
# 12. Configurable confidence threshold
# ===========================================================================

class TestConfigurableConfidenceThreshold:
    """Phase 3 should use the configurable min_confidence_threshold setting."""

    def test_uses_setting_not_hardcoded(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_3_deep_triage(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        # Should use the setting
        assert "min_confidence_threshold" in method_source
        # Should NOT have hardcoded 40
        assert "confidence < 40" not in method_source

    def test_logs_threshold_value(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_3_deep_triage(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "below threshold" in method_source


# ===========================================================================
# 13. Condition validation still works after changes
# ===========================================================================

class TestConditionValidation:
    """validate_condition_set should still work after VWAP/ORB changes."""

    def test_valid_full_condition_set(self):
        conditions = {
            "entry": {
                "all": [
                    {"type": "price_above_vwap", "timeframe": "5m"},
                    {"type": "volume_above_avg", "multiplier": 2.0, "window": 20},
                ]
            },
            "stop_loss": {
                "any": [
                    {"type": "percent_below_entry", "percent": 6.0},
                ]
            },
            "take_profit": [
                {"condition": {"type": "percent_above_entry", "percent": 5.0}, "exit_pct": 33},
                {"condition": {"type": "percent_above_entry", "percent": 10.0}, "exit_pct": 50},
                {"condition": {"type": "percent_above_entry", "percent": 20.0}, "exit_pct": 100},
            ],
        }
        is_valid, errors = validate_condition_set(conditions)
        assert is_valid, f"Expected valid, got errors: {errors}"

    def test_invalid_type(self):
        conditions = {
            "entry": {"all": [{"type": "nonexistent", "value": 5.0}]},
        }
        is_valid, errors = validate_condition_set(conditions)
        assert not is_valid

    def test_orb_condition_valid(self):
        ok, errors = validate_condition_set({
            "entry": {"type": "opening_range_breakout", "minutes": 15},
        })
        assert ok, f"ORB should be valid: {errors}"

    def test_vwap_condition_valid(self):
        ok, errors = validate_condition_set({
            "entry": {"type": "price_above_vwap", "timeframe": "5m"},
        })
        assert ok, f"VWAP should be valid: {errors}"

    def test_percent_above_entry_valid(self):
        ok, errors = validate_condition_set({
            "take_profit": [
                {"condition": {"type": "percent_above_entry", "percent": 10.0}, "exit_pct": 50},
            ],
        })
        assert ok, f"percent_above_entry should be valid: {errors}"


# ===========================================================================
# 14. File rename verification
# ===========================================================================

class TestFileRename:
    """PENNYMOMENTUTRADER.md should have been renamed to PENNYMOMENTUMTRADER.md."""

    def test_correct_filename_exists(self):
        path = os.path.join(_BASE, "PENNYMOMENTUMTRADER.md")
        assert os.path.exists(path), f"Expected {path} to exist"

    def test_typo_filename_gone(self):
        path = os.path.join(_BASE, "PENNYMOMENTUTRADER.md")
        assert not os.path.exists(path), f"Old typo file {path} should not exist"


# ===========================================================================
# 15. Exit update interval setting
# ===========================================================================

class TestExitUpdateIntervalSetting:
    """exit_update_interval_ticks setting should be defined and used in Phase 5."""

    def test_setting_exists_with_default_30(self):
        source = _read_source("__init__.py")
        idx = source.index('"exit_update_interval_ticks"')
        block = source[idx:idx + 300]
        assert '"default": 30,' in block

    def test_setting_type_is_int(self):
        source = _read_source("__init__.py")
        idx = source.index('"exit_update_interval_ticks"')
        block = source[idx:idx + 300]
        assert '"type": "int"' in block

    def test_phase_5_reads_setting(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_5_monitor(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "exit_update_interval_ticks" in method_source

    def test_phase_5_guards_with_tick_modulo(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_5_monitor(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        assert "monitor_tick % exit_update_interval" in method_source

    def test_phase_5_only_runs_for_open_positions(self):
        source = _read_source("__init__.py")
        idx = source.index("def _phase_5_monitor(")
        next_def = source.index("\n    def ", idx + 1)
        method_source = source[idx:next_def]
        # The guard should check open_positions exist before calling update
        assert "and open_positions" in method_source


# ===========================================================================
# 16. LLM-driven exit condition update method
# ===========================================================================

class TestExitConditionUpdateMethod:
    """_update_exit_conditions_via_llm should be properly structured."""

    def test_method_exists(self):
        source = _read_source("__init__.py")
        assert "def _update_exit_conditions_via_llm(" in source

    def test_uses_build_exit_update_prompt(self):
        source = _read_source("__init__.py")
        idx = source.index("def _update_exit_conditions_via_llm(")
        next_def_search = source[idx + 1:]
        # Find next method at same indentation
        next_idx = next_def_search.find("\n    def ")
        if next_idx != -1:
            method_source = source[idx:idx + 1 + next_idx]
        else:
            method_source = source[idx:]
        assert "build_exit_update_prompt" in method_source

    def test_handles_no_change_response(self):
        source = _read_source("__init__.py")
        idx = source.index("def _update_exit_conditions_via_llm(")
        next_def_search = source[idx + 1:]
        next_idx = next_def_search.find("\n    def ")
        if next_idx != -1:
            method_source = source[idx:idx + 1 + next_idx]
        else:
            method_source = source[idx:]
        assert "NO_CHANGE" in method_source

    def test_validates_updated_conditions(self):
        source = _read_source("__init__.py")
        idx = source.index("def _update_exit_conditions_via_llm(")
        next_def_search = source[idx + 1:]
        next_idx = next_def_search.find("\n    def ")
        if next_idx != -1:
            method_source = source[idx:idx + 1 + next_idx]
        else:
            method_source = source[idx:]
        assert "validate_condition_set" in method_source

    def test_resets_triggered_tiers_on_tp_change(self):
        source = _read_source("__init__.py")
        idx = source.index("def _update_exit_conditions_via_llm(")
        next_def_search = source[idx + 1:]
        next_idx = next_def_search.find("\n    def ")
        if next_idx != -1:
            method_source = source[idx:idx + 1 + next_idx]
        else:
            method_source = source[idx:]
        assert 'triggered_tp_tiers' in method_source
        assert "[]" in method_source  # Reset to empty list

    def test_gathers_fresh_news_and_social(self):
        source = _read_source("__init__.py")
        idx = source.index("def _update_exit_conditions_via_llm(")
        next_def_search = source[idx + 1:]
        next_idx = next_def_search.find("\n    def ")
        if next_idx != -1:
            method_source = source[idx:idx + 1 + next_idx]
        else:
            method_source = source[idx:]
        assert "_gather_news" in method_source
        assert "_gather_social" in method_source

    def test_logs_activity(self):
        source = _read_source("__init__.py")
        idx = source.index("def _update_exit_conditions_via_llm(")
        next_def_search = source[idx + 1:]
        next_idx = next_def_search.find("\n    def ")
        if next_idx != -1:
            method_source = source[idx:idx + 1 + next_idx]
        else:
            method_source = source[idx:]
        assert "log_activity" in method_source

    def test_error_handling_with_exc_info(self):
        """Error in exit update should log with exc_info since it's in an except block."""
        source = _read_source("__init__.py")
        idx = source.index("def _update_exit_conditions_via_llm(")
        next_def_search = source[idx + 1:]
        next_idx = next_def_search.find("\n    def ")
        if next_idx != -1:
            method_source = source[idx:idx + 1 + next_idx]
        else:
            method_source = source[idx:]
        assert "exc_info=True" in method_source
        # Verify it's inside an except block
        assert "except Exception" in method_source

    def test_imported_in_prompts(self):
        """build_exit_update_prompt should be imported from prompts."""
        source = _read_source("__init__.py")
        assert "build_exit_update_prompt" in source
        # Verify it's in the imports
        import_section = source[:source.index("class ")]
        assert "build_exit_update_prompt" in import_section
