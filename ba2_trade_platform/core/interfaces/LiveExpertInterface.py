import threading
import time
from abc import abstractmethod
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import pytz
from .MarketExpertInterface import MarketExpertInterface
from ...logger import logger


class LiveExpertInterface(MarketExpertInterface):
    """
    Abstract base class for live trading experts that run continuously
    in a background thread with a daily pipeline lifecycle.

    Extends MarketExpertInterface with:
    - Background daemon thread with start/stop lifecycle
    - Timezone-aware scheduling (kickoff time, market close, trading days)
    - Manual start/stop controls
    - Abstract _run_daily_pipeline() for subclass orchestration
    """

    def __init__(self, id: int):
        super().__init__(id)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._manual_start_event = threading.Event()
        self._is_running = False
        self._current_phase: Optional[str] = None

    # ------------------------------------------------------------------
    # Builtin settings merge
    # ------------------------------------------------------------------

    @classmethod
    def _get_live_expert_settings(cls) -> Dict[str, Any]:
        """Return the settings specific to LiveExpertInterface."""
        return {
            "start_time": {
                "type": "str",
                "required": False,
                "default": "07:00",
                "description": "Daily kick-off time (in market timezone)",
            },
            "market_timezone": {
                "type": "str",
                "required": False,
                "default": "US/Eastern",
                "description": "Market timezone for scheduling",
                "valid_values": [
                    "US/Eastern",
                    "US/Central",
                    "US/Pacific",
                    "Europe/London",
                    "Europe/Paris",
                ],
            },
            "trading_days": {
                "type": "json",
                "required": False,
                "default": {
                    "mon": True,
                    "tue": True,
                    "wed": True,
                    "thu": True,
                    "fri": True,
                    "sat": False,
                    "sun": False,
                },
                "description": "Which days of the week to trade",
            },
            "monitoring_interval_seconds": {
                "type": "int",
                "required": False,
                "default": 60,
                "description": "Interval in seconds between monitoring checks",
            },
            "market_close_time": {
                "type": "str",
                "required": False,
                "default": "16:00",
                "description": "Market close time (in market timezone)",
            },
        }

    @classmethod
    def _ensure_builtin_settings(cls):
        """Merge live-expert settings into the parent builtin settings."""
        super()._ensure_builtin_settings()
        live_settings = cls._get_live_expert_settings()
        cls._builtin_settings.update(live_settings)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Create and start the background daemon thread."""
        if self._is_running:
            logger.warning(f"LiveExpert {self.id} is already running")
            return

        self._stop_event.clear()
        self._manual_start_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"LiveExpert-{self.id}",
            daemon=True,
        )
        self._is_running = True
        self._thread.start()
        logger.info(f"LiveExpert {self.id} started")

    def stop(self):
        """Signal the thread to stop, wait for it to finish, and reset state."""
        if not self._is_running:
            logger.warning(f"LiveExpert {self.id} is not running")
            return

        logger.info(f"LiveExpert {self.id} stopping...")
        self._stop_event.set()
        # Unblock any wait on manual_start_event
        self._manual_start_event.set()

        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                logger.warning(f"LiveExpert {self.id} thread did not stop within 30s")

        self._is_running = False
        self._thread = None
        self._current_phase = None
        self._stop_event.clear()
        self._manual_start_event.clear()
        logger.info(f"LiveExpert {self.id} stopped")

    def request_manual_start(self) -> str:
        """Trigger the daily pipeline immediately (unblock the wait)."""
        self._manual_start_event.set()
        return "Manual scan started"

    def request_stop(self) -> str:
        """Request a graceful stop of the running loop."""
        self._stop_event.set()
        return "Scan stop requested"

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def current_phase(self) -> Optional[str]:
        return self._current_phase

    # ------------------------------------------------------------------
    # Thread loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        """Main loop executed in the background thread."""
        logger.info(f"LiveExpert {self.id} run loop entered")
        try:
            while not self._stop_event.is_set():
                # Check if today is a trading day
                if not self._is_trading_day():
                    logger.debug(f"LiveExpert {self.id}: not a trading day, sleeping 1h")
                    # Sleep up to 1 hour, checking stop every second
                    if self._stop_event.wait(timeout=3600):
                        break
                    continue

                if self._should_auto_start():
                    # Before kickoff — wait until kickoff time (manual can interrupt)
                    seconds = self._seconds_until_kickoff()
                    if seconds > 0:
                        logger.info(
                            f"LiveExpert {self.id}: waiting {seconds:.0f}s until kickoff"
                        )
                        triggered = self._manual_start_event.wait(timeout=seconds)
                        self._manual_start_event.clear()
                        if self._stop_event.is_set():
                            break
                else:
                    # Past kickoff — wait for manual start or next kickoff
                    seconds = self._seconds_until_next_kickoff()
                    logger.info(
                        f"LiveExpert {self.id}: past kickoff, waiting for manual start "
                        f"or next kickoff in {seconds:.0f}s"
                    )
                    triggered = self._manual_start_event.wait(timeout=seconds)
                    self._manual_start_event.clear()
                    if self._stop_event.is_set():
                        break
                    if not triggered:
                        # Timed out — loop back to re-evaluate
                        continue

                # Run the daily pipeline
                if not self._stop_event.is_set():
                    try:
                        logger.info(f"LiveExpert {self.id}: running daily pipeline")
                        self._run_daily_pipeline()
                    except Exception as e:
                        logger.error(
                            f"LiveExpert {self.id}: daily pipeline failed: {e}",
                            exc_info=True,
                        )

                    # Sleep until next kickoff
                    seconds = self._seconds_until_next_kickoff()
                    if seconds > 0 and not self._stop_event.is_set():
                        logger.info(
                            f"LiveExpert {self.id}: pipeline done, sleeping {seconds:.0f}s "
                            f"until next kickoff"
                        )
                        triggered = self._manual_start_event.wait(timeout=seconds)
                        self._manual_start_event.clear()

        except Exception as e:
            logger.error(
                f"LiveExpert {self.id}: run loop crashed: {e}", exc_info=True
            )
        finally:
            self._current_phase = None
            logger.info(f"LiveExpert {self.id} run loop exited")

    # ------------------------------------------------------------------
    # Abstract method
    # ------------------------------------------------------------------

    @abstractmethod
    def _run_daily_pipeline(self):
        """
        Execute the daily trading pipeline.

        Subclasses must implement phase orchestration here.
        Between phases, check ``self._stop_event.is_set()`` and bail out
        early if requested.
        """
        pass

    # ------------------------------------------------------------------
    # Timezone helpers
    # ------------------------------------------------------------------

    def _get_market_tz(self) -> pytz.BaseTzInfo:
        """Return the pytz timezone from the market_timezone setting."""
        tz_name = self.get_setting_with_interface_default(
            "market_timezone", log_warning=False
        )
        return pytz.timezone(tz_name)

    def _get_market_now(self) -> datetime:
        """Return the current datetime in the market timezone."""
        return datetime.now(self._get_market_tz())

    def _get_kickoff_time_today(self) -> datetime:
        """Parse start_time setting and return today's kickoff as a tz-aware datetime."""
        start_time_str = self.get_setting_with_interface_default(
            "start_time", log_warning=False
        )
        hour, minute = (int(p) for p in start_time_str.split(":"))
        tz = self._get_market_tz()
        now = self._get_market_now()
        return tz.localize(
            datetime(now.year, now.month, now.day, hour, minute, 0)
        )

    def _get_market_close_today(self) -> datetime:
        """Parse market_close_time setting and return today's close as a tz-aware datetime."""
        close_time_str = self.get_setting_with_interface_default(
            "market_close_time", log_warning=False
        )
        hour, minute = (int(p) for p in close_time_str.split(":"))
        tz = self._get_market_tz()
        now = self._get_market_now()
        return tz.localize(
            datetime(now.year, now.month, now.day, hour, minute, 0)
        )

    def _is_trading_day(self, dt: Optional[datetime] = None) -> bool:
        """Check whether the given (or current market) date is a trading day."""
        trading_days = self.get_setting_with_interface_default(
            "trading_days", log_warning=False
        )
        if dt is None:
            dt = self._get_market_now()
        day_map = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        day_key = day_map[dt.weekday()]
        return bool(trading_days.get(day_key, False))

    def _should_auto_start(self) -> bool:
        """Return True if we are before today's kickoff time."""
        now = self._get_market_now()
        kickoff = self._get_kickoff_time_today()
        return now < kickoff

    def _seconds_until_kickoff(self) -> float:
        """Return seconds until today's kickoff time (may be negative if past)."""
        now = self._get_market_now()
        kickoff = self._get_kickoff_time_today()
        delta = (kickoff - now).total_seconds()
        return max(delta, 0)

    def _seconds_until_next_kickoff(self) -> float:
        """
        Find the next trading day's kickoff and return seconds until then.

        Searches up to 7 days ahead.
        """
        tz = self._get_market_tz()
        now = self._get_market_now()
        start_time_str = self.get_setting_with_interface_default(
            "start_time", log_warning=False
        )
        hour, minute = (int(p) for p in start_time_str.split(":"))

        for days_ahead in range(1, 8):
            candidate = now + timedelta(days=days_ahead)
            candidate_dt = tz.localize(
                datetime(candidate.year, candidate.month, candidate.day, hour, minute, 0)
            )
            if self._is_trading_day(candidate_dt):
                return (candidate_dt - now).total_seconds()

        # Fallback: 24 hours
        return 86400.0

    def _is_market_open(self) -> bool:
        """Return True if the current time is between kickoff and market close."""
        now = self._get_market_now()
        kickoff = self._get_kickoff_time_today()
        close = self._get_market_close_today()
        return kickoff <= now <= close
