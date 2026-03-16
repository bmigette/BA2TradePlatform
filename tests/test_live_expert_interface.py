"""
Tests for LiveExpertInterface — timezone helpers, trading-day checks,
seconds calculations, and thread lifecycle.

Uses a minimal concrete subclass (StubLiveExpert) that bypasses DB access.
"""
import threading
import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytz

from ba2_trade_platform.core.interfaces.LiveExpertInterface import LiveExpertInterface


# ---------------------------------------------------------------------------
# Concrete stub — no DB, deterministic settings
# ---------------------------------------------------------------------------

class StubLiveExpert(LiveExpertInterface):
    """Minimal concrete subclass for testing LiveExpertInterface helpers."""

    # Class-level default overrides
    _builtin_settings = {}

    def __init__(self, settings_override: dict = None):
        # Skip parent __init__ entirely to avoid DB access
        self.id = 999
        self._settings_cache = None
        self._thread = None
        self._stop_event = threading.Event()
        self._manual_start_event = threading.Event()
        self._is_running = False
        self._current_phase = None
        self._pipeline_called = threading.Event()
        self._pipeline_call_count = 0

        # Provide in-memory settings
        self._fake_settings = {
            "start_time": "07:00",
            "market_timezone": "US/Eastern",
            "trading_days": {
                "mon": True, "tue": True, "wed": True,
                "thu": True, "fri": True, "sat": False, "sun": False,
            },
            "monitoring_interval_seconds": 60,
            "market_close_time": "16:00",
        }
        if settings_override:
            self._fake_settings.update(settings_override)

    # Override settings property to avoid DB
    @property
    def settings(self):
        return self._fake_settings

    def get_setting_with_interface_default(self, key, log_warning=True):
        if key in self._fake_settings:
            return self._fake_settings[key]
        raise ValueError(f"Setting '{key}' not found")

    # Abstract methods
    @classmethod
    def get_settings_definitions(cls):
        return {}

    @classmethod
    def description(cls):
        return "Stub live expert for tests"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        pass

    def _run_daily_pipeline(self):
        self._pipeline_call_count += 1
        self._pipeline_called.set()


# ---------------------------------------------------------------------------
# Timezone / time-parsing tests
# ---------------------------------------------------------------------------

class TestTimezoneHelpers:
    def test_get_market_tz_default(self):
        expert = StubLiveExpert()
        tz = expert._get_market_tz()
        assert str(tz) == "US/Eastern"

    def test_get_market_tz_custom(self):
        expert = StubLiveExpert({"market_timezone": "Europe/Paris"})
        tz = expert._get_market_tz()
        assert str(tz) == "Europe/Paris"

    def test_get_market_now_returns_aware_datetime(self):
        expert = StubLiveExpert()
        now = expert._get_market_now()
        assert now.tzinfo is not None

    def test_get_kickoff_time_today_parsing(self):
        expert = StubLiveExpert({"start_time": "09:30"})
        kickoff = expert._get_kickoff_time_today()
        assert kickoff.hour == 9
        assert kickoff.minute == 30
        assert kickoff.second == 0

    def test_get_kickoff_time_default(self):
        expert = StubLiveExpert()
        kickoff = expert._get_kickoff_time_today()
        assert kickoff.hour == 7
        assert kickoff.minute == 0

    def test_get_market_close_today(self):
        expert = StubLiveExpert({"market_close_time": "15:45"})
        close = expert._get_market_close_today()
        assert close.hour == 15
        assert close.minute == 45


# ---------------------------------------------------------------------------
# Trading day checks
# ---------------------------------------------------------------------------

class TestTradingDayChecks:
    def test_weekday_is_trading_day(self):
        expert = StubLiveExpert()
        tz = pytz.timezone("US/Eastern")
        # 2026-03-16 is Monday
        monday = tz.localize(datetime(2026, 3, 16, 10, 0, 0))
        assert expert._is_trading_day(monday) is True

    def test_saturday_is_not_trading_day(self):
        expert = StubLiveExpert()
        tz = pytz.timezone("US/Eastern")
        # 2026-03-21 is Saturday
        saturday = tz.localize(datetime(2026, 3, 21, 10, 0, 0))
        assert expert._is_trading_day(saturday) is False

    def test_sunday_is_not_trading_day(self):
        expert = StubLiveExpert()
        tz = pytz.timezone("US/Eastern")
        # 2026-03-22 is Sunday
        sunday = tz.localize(datetime(2026, 3, 22, 10, 0, 0))
        assert expert._is_trading_day(sunday) is False

    def test_custom_trading_days(self):
        expert = StubLiveExpert({
            "trading_days": {
                "mon": False, "tue": False, "wed": True,
                "thu": False, "fri": False, "sat": True, "sun": False,
            }
        })
        tz = pytz.timezone("US/Eastern")
        # 2026-03-18 is Wednesday
        wed = tz.localize(datetime(2026, 3, 18, 10, 0, 0))
        assert expert._is_trading_day(wed) is True
        # 2026-03-16 is Monday
        mon = tz.localize(datetime(2026, 3, 16, 10, 0, 0))
        assert expert._is_trading_day(mon) is False
        # 2026-03-21 is Saturday
        sat = tz.localize(datetime(2026, 3, 21, 10, 0, 0))
        assert expert._is_trading_day(sat) is True


# ---------------------------------------------------------------------------
# Seconds calculations
# ---------------------------------------------------------------------------

class TestSecondsCalculations:
    def test_seconds_until_kickoff_before(self):
        expert = StubLiveExpert({"start_time": "10:00"})
        tz = pytz.timezone("US/Eastern")
        fake_now = tz.localize(datetime(2026, 3, 16, 9, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            seconds = expert._seconds_until_kickoff()
            assert seconds == 3600.0  # 1 hour

    def test_seconds_until_kickoff_after(self):
        """If past kickoff, should return 0."""
        expert = StubLiveExpert({"start_time": "07:00"})
        tz = pytz.timezone("US/Eastern")
        fake_now = tz.localize(datetime(2026, 3, 16, 12, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            seconds = expert._seconds_until_kickoff()
            assert seconds == 0

    def test_should_auto_start_before_kickoff(self):
        expert = StubLiveExpert({"start_time": "10:00"})
        tz = pytz.timezone("US/Eastern")
        fake_now = tz.localize(datetime(2026, 3, 16, 9, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            assert expert._should_auto_start() is True

    def test_should_auto_start_after_kickoff(self):
        expert = StubLiveExpert({"start_time": "07:00"})
        tz = pytz.timezone("US/Eastern")
        fake_now = tz.localize(datetime(2026, 3, 16, 12, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            assert expert._should_auto_start() is False

    def test_seconds_until_next_kickoff(self):
        expert = StubLiveExpert({"start_time": "07:00"})
        tz = pytz.timezone("US/Eastern")
        # Monday 12:00 -> next kickoff is Tuesday 07:00 = 19 hours
        fake_now = tz.localize(datetime(2026, 3, 16, 12, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            seconds = expert._seconds_until_next_kickoff()
            assert seconds == pytest.approx(19 * 3600, abs=1)

    def test_seconds_until_next_kickoff_friday_to_monday(self):
        """Friday should skip to Monday."""
        expert = StubLiveExpert({"start_time": "07:00"})
        tz = pytz.timezone("US/Eastern")
        # 2026-03-20 is Friday, 12:00 -> next is Monday 2026-03-23 07:00
        fake_now = tz.localize(datetime(2026, 3, 20, 12, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            seconds = expert._seconds_until_next_kickoff()
            # Friday 12:00 to Monday 07:00 = 2 days 19 hours = 67 hours
            expected = 67 * 3600
            assert seconds == pytest.approx(expected, abs=1)


# ---------------------------------------------------------------------------
# Market open check
# ---------------------------------------------------------------------------

class TestIsMarketOpen:
    def test_market_open_within_hours(self):
        expert = StubLiveExpert({"start_time": "07:00", "market_close_time": "16:00"})
        tz = pytz.timezone("US/Eastern")
        fake_now = tz.localize(datetime(2026, 3, 16, 10, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            assert expert._is_market_open() is True

    def test_market_closed_before_open(self):
        expert = StubLiveExpert({"start_time": "07:00", "market_close_time": "16:00"})
        tz = pytz.timezone("US/Eastern")
        fake_now = tz.localize(datetime(2026, 3, 16, 6, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            assert expert._is_market_open() is False

    def test_market_closed_after_close(self):
        expert = StubLiveExpert({"start_time": "07:00", "market_close_time": "16:00"})
        tz = pytz.timezone("US/Eastern")
        fake_now = tz.localize(datetime(2026, 3, 16, 17, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            assert expert._is_market_open() is False

    def test_market_open_at_boundary(self):
        expert = StubLiveExpert({"start_time": "07:00", "market_close_time": "16:00"})
        tz = pytz.timezone("US/Eastern")
        # Exactly at kickoff
        fake_now = tz.localize(datetime(2026, 3, 16, 7, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            assert expert._is_market_open() is True
        # Exactly at close
        fake_now = tz.localize(datetime(2026, 3, 16, 16, 0, 0))
        with patch.object(expert, "_get_market_now", return_value=fake_now):
            assert expert._is_market_open() is True


# ---------------------------------------------------------------------------
# Settings merge
# ---------------------------------------------------------------------------

class TestSettingsMerge:
    def test_live_expert_settings_defined(self):
        settings = StubLiveExpert._get_live_expert_settings()
        assert "start_time" in settings
        assert "market_timezone" in settings
        assert "trading_days" in settings
        assert "monitoring_interval_seconds" in settings
        assert "market_close_time" in settings

    def test_default_values(self):
        settings = StubLiveExpert._get_live_expert_settings()
        assert settings["start_time"]["default"] == "07:00"
        assert settings["market_timezone"]["default"] == "US/Eastern"
        assert settings["monitoring_interval_seconds"]["default"] == 60
        assert settings["market_close_time"]["default"] == "16:00"
        assert settings["trading_days"]["default"]["mon"] is True
        assert settings["trading_days"]["default"]["sat"] is False


# ---------------------------------------------------------------------------
# Thread lifecycle tests
# ---------------------------------------------------------------------------

class TestThreadLifecycle:
    def test_start_sets_running(self):
        expert = StubLiveExpert()
        # Make it think it is NOT a trading day to avoid pipeline execution
        with patch.object(expert, "_is_trading_day", return_value=False):
            expert.start()
            assert expert.is_running is True
            assert expert._thread is not None
            assert expert._thread.daemon is True
            expert.stop()

    def test_stop_clears_state(self):
        expert = StubLiveExpert()
        with patch.object(expert, "_is_trading_day", return_value=False):
            expert.start()
            expert.stop()
            assert expert.is_running is False
            assert expert._thread is None
            assert expert.current_phase is None

    def test_request_manual_start(self):
        expert = StubLiveExpert()
        result = expert.request_manual_start()
        assert result == "Manual scan started"
        assert expert._manual_start_event.is_set()

    def test_request_stop(self):
        expert = StubLiveExpert()
        result = expert.request_stop()
        assert result == "Scan stop requested"
        assert expert._stop_event.is_set()

    def test_manual_start_triggers_pipeline(self):
        """Verify manual start actually triggers _run_daily_pipeline."""
        expert = StubLiveExpert()
        tz = pytz.timezone("US/Eastern")
        # Simulate past-kickoff so loop waits for manual start
        fake_now = tz.localize(datetime(2026, 3, 16, 12, 0, 0))

        with patch.object(expert, "_get_market_now", return_value=fake_now):
            expert.start()
            time.sleep(0.1)  # Let thread enter the wait

            expert.request_manual_start()
            # Wait for pipeline to be called
            called = expert._pipeline_called.wait(timeout=5)
            assert called, "Pipeline was not called within timeout"
            assert expert._pipeline_call_count >= 1

            expert.stop()

    def test_double_start_is_safe(self):
        expert = StubLiveExpert()
        with patch.object(expert, "_is_trading_day", return_value=False):
            expert.start()
            expert.start()  # Should warn but not crash
            assert expert.is_running is True
            expert.stop()

    def test_double_stop_is_safe(self):
        expert = StubLiveExpert()
        with patch.object(expert, "_is_trading_day", return_value=False):
            expert.start()
            expert.stop()
            expert.stop()  # Should warn but not crash
            assert expert.is_running is False
