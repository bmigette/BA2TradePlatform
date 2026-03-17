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

    # Live experts manage their own risk — disable platform risk manager UI by default.
    # Override to True in a subclass if it actually delegates to the platform risk manager.
    uses_risk_manager: bool = False

    # Class-level registry: expert_id -> instance currently running.
    # Guards against duplicate starts across different object instances for the same ID.
    _running_registry: Dict[int, "LiveExpertInterface"] = {}
    _registry_lock: threading.Lock = threading.Lock()

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

    def _get_logger(self):
        """Return the expert's own logger if set, otherwise the module logger."""
        return getattr(self, 'logger', logger)

    def start(self):
        """Create and start the background daemon thread."""
        with LiveExpertInterface._registry_lock:
            existing = LiveExpertInterface._running_registry.get(self.id)
            if existing is not None:
                self._get_logger().warning(
                    f"LiveExpert {self.id} is already running "
                    f"(registered instance: {id(existing):#x}, this: {id(self):#x}) — skipping duplicate start"
                )
                return
            if self._is_running:
                self._get_logger().warning(f"LiveExpert {self.id} is already running")
                return

            self._stop_event.clear()
            self._manual_start_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=f"LiveExpert-{self.id}",
                daemon=True,
            )
            self._is_running = True
            LiveExpertInterface._running_registry[self.id] = self

        self._thread.start()
        self._get_logger().info(f"LiveExpert {self.id} started")

    def stop(self):
        """Signal the thread to stop, wait for it to finish, and reset state."""
        if not self._is_running:
            self._get_logger().warning(f"LiveExpert {self.id} is not running")
            return

        self._get_logger().info(f"LiveExpert {self.id} stopping...")
        self._stop_event.set()
        # Unblock any wait on manual_start_event
        self._manual_start_event.set()

        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                self._get_logger().warning(f"LiveExpert {self.id} thread did not stop within 30s")

        self._is_running = False
        self._thread = None
        self._current_phase = None
        self._stop_event.clear()
        self._manual_start_event.clear()
        with LiveExpertInterface._registry_lock:
            LiveExpertInterface._running_registry.pop(self.id, None)
        self._get_logger().info(f"LiveExpert {self.id} stopped")

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
        _log = self._get_logger()
        _log.info(f"LiveExpert {self.id} run loop entered")
        try:
            while not self._stop_event.is_set():
                # Check if today is a trading day
                if not self._is_trading_day():
                    _log.debug(f"LiveExpert {self.id}: not a trading day, sleeping 1h")
                    # Sleep up to 1 hour, checking stop every second
                    if self._stop_event.wait(timeout=3600):
                        break
                    continue

                if self._should_auto_start():
                    # Before kickoff — wait until kickoff time (manual can interrupt)
                    seconds = self._seconds_until_kickoff()
                    if seconds > 0:
                        _log.info(
                            f"LiveExpert {self.id}: waiting {seconds:.0f}s until kickoff"
                        )
                        if self._wait_with_countdown(seconds, _log):
                            break  # stop requested
                else:
                    # Past kickoff — wait for manual start or next kickoff
                    seconds = self._seconds_until_next_kickoff()
                    _log.info(
                        f"LiveExpert {self.id}: past kickoff, waiting for manual start "
                        f"or next kickoff in {seconds:.0f}s"
                    )
                    triggered = self._wait_with_countdown(seconds, _log, return_triggered=True)
                    if self._stop_event.is_set():
                        break
                    if triggered is False:
                        # Timed out — loop back to re-evaluate
                        continue

                # Run the daily pipeline
                if not self._stop_event.is_set():
                    try:
                        _log.info(f"LiveExpert {self.id}: running daily pipeline")
                        self._run_daily_pipeline()
                    except Exception as e:
                        _log.error(
                            f"LiveExpert {self.id}: daily pipeline failed: {e}",
                            exc_info=True,
                        )

                    # Sleep until next kickoff
                    seconds = self._seconds_until_next_kickoff()
                    if seconds > 0 and not self._stop_event.is_set():
                        _log.info(
                            f"LiveExpert {self.id}: pipeline done, sleeping {seconds:.0f}s "
                            f"until next kickoff"
                        )
                        self._wait_with_countdown(seconds, _log)

        except Exception as e:
            _log.error(
                f"LiveExpert {self.id}: run loop crashed: {e}", exc_info=True
            )
        finally:
            self._current_phase = None
            self._is_running = False
            with LiveExpertInterface._registry_lock:
                LiveExpertInterface._running_registry.pop(self.id, None)
            _log.info(f"LiveExpert {self.id} run loop exited")

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    def _get_idle_status(self) -> Optional[str]:
        """
        Return an optional status string appended to the countdown log every tick.
        Subclasses can override this to surface live information (e.g. monitored symbols).
        Return None to add nothing.
        """
        return None

    def _wait_with_countdown(self, total_seconds: float, _log, return_triggered: bool = False):
        """Wait for *total_seconds*, logging a countdown every 15 minutes.

        Returns:
            If *return_triggered* is False: True when stop was requested.
            If *return_triggered* is True: True if manual start triggered,
            False if timed out, or True (stop) — caller checks _stop_event.
        """
        remaining = total_seconds
        INTERVAL = 900  # log every 15 minutes

        while remaining > 0 and not self._stop_event.is_set():
            # Log countdown
            hours = int(remaining // 3600)
            minutes = int((remaining % 3600) // 60)
            extra = self._get_idle_status()
            suffix = f" | {extra}" if extra else ""
            if hours > 0:
                _log.info(f"LiveExpert {self.id}: will start in {hours}h {minutes}min{suffix}")
            elif minutes > 0:
                _log.info(f"LiveExpert {self.id}: will start in {minutes}min{suffix}")

            wait_chunk = min(remaining, INTERVAL)
            triggered = self._manual_start_event.wait(timeout=wait_chunk)
            self._manual_start_event.clear()

            if self._stop_event.is_set():
                return True if not return_triggered else True
            if triggered:
                return False if not return_triggered else True

            remaining -= wait_chunk

        if return_triggered:
            return False  # timed out
        return self._stop_event.is_set()

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
