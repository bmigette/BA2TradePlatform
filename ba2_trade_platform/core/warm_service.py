"""Live host wiring for the background warm service (spec step 4, section 6).

``app.services.warm`` decides WHAT is missing and HOW to close it;
``ba2_common.core.replay.dependencies`` + ``ba2_experts.replay_dependencies``
decide what a configuration needs. This module is the only place that decides
WHETHER this installation warms anything, WITH WHAT BUDGET, and WHEN --
the same division ``replay_capture`` draws for recording.

**Off by default and independently of capture.** ``warm_enabled`` is created as
``"false"`` on first read (there is no alembic migration for AppSetting rows).
With it off nothing is scheduled, no thread starts and the batch-end hook is a
function call that returns 0. Spec section 6: "Warm enabled -- off independently
until its plan is reviewed; no deployment-triggered bulk download."

**Never in the way of a trade.** Every entry point is wrapped: a warm that cannot
plan, cannot enqueue or cannot schedule leaves an ERROR in the log and returns.
Warming is an optimisation for a later comparison; it may lose coverage, never a
trade. The queue holds no account lock and runs on its own threads.

**Two triggers, both bounded** (spec section 6, lifecycle steps 3 and 5):

* after each analysis BATCH, resolve what the analyses in that batch declared and
  enqueue only what the planner marks missing or stale -- "as each screener
  finishes, queue only newly required historical dependencies";
* after the exchange close plus a settlement offset, extend the price tails and
  pin the artifacts -- "exchanges, holidays and DST determine the close; no fixed
  Paris-time close", so the time comes from the account's own ``get_market_hours``.
"""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool
from ba2_common.core.replay import get_replay_store
from ba2_common.core.replay.dependencies import (
    MissingDependencySetting,
    Window,
    required_replay_inputs,
)

from ..logger import logger
from .db import add_instance
from .models import AppSetting

#: The switch. Created as "false" on first read.
WARM_ENABLED_KEY = "warm_enabled"
#: Worker threads. Two, low priority, sharing the FMP gate (spec section 6 pilot).
WARM_WORKERS_KEY = "warm_workers"
#: The daily background download allowance, in MiB (spec section 6 pilot: 100).
WARM_ALLOWANCE_KEY = "warm_daily_allowance_mib"
#: How long after the exchange close to wait for the provider to settle before
#: extending price tails and pinning artifacts.
WARM_SETTLEMENT_OFFSET_KEY = "warm_settlement_offset_minutes"

#: Pilot values, as documented in spec section 6's operational-policy table. Created
#: on first read; an operator changes them from the settings page.
WARM_DEFAULTS: Dict[str, str] = {
    WARM_ENABLED_KEY: "false",
    WARM_WORKERS_KEY: "2",
    WARM_ALLOWANCE_KEY: "100",
    WARM_SETTLEMENT_OFFSET_KEY: "90",
}

#: The scheduled settlement job's id.
WARM_SETTLEMENT_JOB_ID = "warm_settlement_job"

#: How much history a batch-end warm asks for when an adapter does not state its own
#: lookback. Two years: longer than the deepest lookback any adapted expert declares
#: (DeterministicScorer's 600 calendar days of daily bars) with room for a position
#: held across a gap. Stated here rather than left to each caller so the number is
#: reviewable in one place -- it is a POLICY, not a default standing in for a value
#: someone forgot to pass.
WARM_WINDOW_DAYS = 730

#: Where a settlement run pins its artifacts: ``<cache>/replay/pinned/<date>``.
PINNED_SUBDIR = os.path.join("replay", "pinned")

_LOCK = threading.RLock()
_QUEUE = None


class WarmServiceUnavailable(RuntimeError):
    """The warm implementation is not present in this deployment."""


def _ensure_backend_importable() -> str:
    """Make ``app.services.warm`` importable from this checkout, and say so if it is not.

    The planner/budget/worker/pin code lives in ``testplatform/backend`` -- one
    repository, two installable trees, and only the test platform installs its
    ``app`` package. Rather than duplicate the implementation on this side, the host
    puts its OWN checkout's backend directory on ``sys.path`` (derived from this
    file, never from an installed distribution, so a worktree warms with its own
    code -- the same trap ``testplatform/backend/pytest.ini`` documents).

    APPENDED, not inserted: a path prepended here would shadow this application's
    own modules. A deployment without the test platform raises
    :class:`WarmServiceUnavailable`, which leaves warming off with a clear reason
    instead of an ImportError from four frames down.
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    backend = os.path.join(repo, "testplatform", "backend")
    if not os.path.isdir(backend):
        raise WarmServiceUnavailable(
            f"the warm service implementation ({backend}) is not part of this deployment; "
            f"background warming is unavailable")
    if backend not in sys.path:
        sys.path.append(backend)
    return backend


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _setting(key: str) -> str:
    """Read ``key``, creating it at its documented pilot value on first use.

    Reads the row directly rather than through ``get_setting``: on the FIRST run the
    row legitimately does not exist, and warning about the expected path teaches
    readers to ignore warnings (same reasoning as ``replay_capture.capture_enabled``).
    """
    from sqlmodel import select

    from .db import get_db

    with get_db() as session:
        row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
    if row is None:
        value = WARM_DEFAULTS[key]
        add_instance(AppSetting(key=key, value_str=value))
        logger.info(f"Created {key} AppSetting with default value: {value}")
        return value
    if row.value_str is None:
        raise ValueError(f"{key} exists with no value; set it or delete the row")
    return row.value_str


def warm_enabled() -> bool:
    """Whether this installation runs the background warm at all."""
    return coerce_bool(_setting(WARM_ENABLED_KEY))


def warm_workers() -> int:
    return int(_setting(WARM_WORKERS_KEY))


def warm_daily_allowance_bytes() -> int:
    return int(_setting(WARM_ALLOWANCE_KEY)) * 1024 * 1024


def warm_settlement_offset_minutes() -> int:
    return int(_setting(WARM_SETTLEMENT_OFFSET_KEY))


def ensure_settings() -> Dict[str, str]:
    """Create every warm setting at its pilot value and return what they now hold.

    Called at startup even when warming is OFF, so the rows exist and the switch is
    reachable from the UI -- nothing migrates AppSetting rows into existence.
    """
    return {key: _setting(key) for key in WARM_DEFAULTS}


# --------------------------------------------------------------------------- #
# Cache roots
# --------------------------------------------------------------------------- #
def cache_roots() -> List[str]:
    """The roots a plan inspects. The first is the writable one this host fills."""
    import ba2_trade_platform.config as config

    return [config.CACHE_FOLDER]


def pinned_root_for(day: str) -> str:
    import ba2_trade_platform.config as config

    return os.path.join(config.CACHE_FOLDER, PINNED_SUBDIR, day)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def get_warm_queue():
    """The running queue, or ``None`` when warming is off or not started."""
    return _QUEUE


def initialize_warm_service(job_manager=None):
    """Start the warm queue and schedule the settlement job, if the setting says so.

    Returns the queue (``None`` when warming is off). Safe to call twice.
    """
    global _QUEUE
    try:
        with _LOCK:
            ensure_settings()
            if _QUEUE is not None:
                logger.debug("warm service already initialized")
                return _QUEUE
            if not warm_enabled():
                logger.info(
                    f"Warm service is OFF ({WARM_ENABLED_KEY}=false); no background "
                    f"downloads and no scheduled warm job")
                return None

            _ensure_backend_importable()
            from app.services.warm.budget import WarmBudget
            from app.services.warm.worker import DefaultWarmFetcher, WarmQueue

            # The adapters have to be REGISTERED before anything resolves a
            # requirement; importing the module is what registers them.
            import ba2_experts.replay_dependencies  # noqa: F401

            budget = WarmBudget(
                allowance_bytes=warm_daily_allowance_bytes(),
                unknown_reserve_bytes=_unknown_reserve_bytes(),
            )
            queue = WarmQueue(
                workers=warm_workers(),
                budget=budget,
                fetcher=DefaultWarmFetcher(
                    ohlcv_provider=_indicator_ohlcv_provider(),
                    end_date=datetime.now(timezone.utc),
                    fmp_key=_api_key("FMP_API_KEY", "FMP_API_KEY"),
                    fred_key=_api_key("FRED_API_KEY", "fred_api_key"),
                ),
            )
            queue.start()
            _QUEUE = queue
            logger.info(
                f"Warm service is ON: {warm_workers()} worker(s), "
                f"{warm_daily_allowance_bytes() / 1048576:.0f} MiB/day allowance")
        if job_manager is not None:
            schedule_settlement_job(job_manager)
        return _QUEUE
    except Exception as e:
        logger.error(f"Warm service could not be initialized: {e}", exc_info=True)
        return None


def shutdown_warm_service(timeout: float = 5.0) -> None:
    """Stop the queue. Queued work is abandoned -- a warm may always be cut short."""
    global _QUEUE
    try:
        with _LOCK:
            if _QUEUE is None:
                return
            _QUEUE.stop(timeout=timeout)
            _QUEUE = None
            logger.info("Warm service stopped")
    except Exception as e:
        logger.error(f"Warm service could not be stopped cleanly: {e}", exc_info=True)


def _unknown_reserve_bytes() -> int:
    """What to reserve for a response whose size nothing measured.

    Derived from what an empty plan over the real roots measures, so the number
    comes off this installation's own files. When the root holds nothing at all the
    reserve is the whole allowance: the FIRST item then consumes it and the warm
    pauses with a gap report -- honest, and impossible to mistake for a working
    budget -- rather than being sized by a guess.
    """
    _ensure_backend_importable()
    from app.services.warm import planner
    from app.services.warm.budget import WarmBudgetError, unknown_reserve_for

    empty = planner.plan([], cache_roots(), as_of_now=datetime.now(timezone.utc))
    try:
        return unknown_reserve_for(empty)
    except WarmBudgetError:
        logger.warning(
            "warm service: no artifact on the cache root to size an unknown-size "
            "reservation from; reserving the whole daily allowance per unknown item so "
            "the warm pauses visibly instead of spending blind")
        return warm_daily_allowance_bytes()


def _indicator_ohlcv_provider() -> str:
    """The OHLCV provider this host's indicator stack reads.

    Imported from the seam helper that builds it, so the warm cannot warm a
    different provider's parquet from the one ATR is computed off.
    """
    from .seam_helpers import DEFAULT_INDICATOR_OHLCV_PROVIDER

    return DEFAULT_INDICATOR_OHLCV_PROVIDER


def _api_key(env_name: str, setting_key: str) -> Optional[str]:
    """env first, then the app-settings DB -- the order every other resolver uses."""
    value = os.getenv(env_name)
    if value:
        return value
    try:
        from ba2_common.config import get_app_setting

        return get_app_setting(setting_key)
    except Exception as e:  # noqa: BLE001 - a missing key is refused per requirement, loudly
        logger.warning(f"warm service: {setting_key} unavailable: {e}")
        return None


# --------------------------------------------------------------------------- #
# Trigger 1: the end of an analysis batch
# --------------------------------------------------------------------------- #
def on_analysis_batch_end(batch_id: str) -> int:
    """Enqueue what the analyses in ``batch_id`` declared and the roots do not hold.

    Returns the number of requirements enqueued (0 when warming is off, when nothing
    was recorded, or when everything is already present).

    Reads the configuration out of the CAPTURE STORE, not out of the database: the
    settings that matter are the ones the recorded analysis actually ran with, and an
    instance edited between the analysis and this hook would otherwise warm for a
    configuration that never ran.
    """
    queue = get_warm_queue()
    if queue is None:
        return 0
    try:
        groups = _recorded_batch_groups(batch_id)
        if not groups:
            logger.debug(f"warm: batch {batch_id} recorded no analyses to resolve")
            return 0

        _ensure_backend_importable()
        from app.services.warm import planner

        now = datetime.now(timezone.utc)
        window = Window(start=now - timedelta(days=WARM_WINDOW_DAYS), end=now)
        requirements = []
        for (expert_class, _hash), group in groups.items():
            try:
                requirements.extend(required_replay_inputs(
                    expert_class, group["settings"], group["rules"],
                    sorted(group["symbols"]), window))
            except MissingDependencySetting as e:
                logger.error(
                    f"warm: cannot resolve {expert_class}'s dependencies for batch "
                    f"{batch_id}: {e}. Nothing is warmed for it rather than warming a "
                    f"configuration it did not run.")
        if not requirements:
            return 0
        plan = planner.plan(requirements, cache_roots(), as_of_now=now)
        enqueued = queue.submit_plan(plan)
        totals = plan.totals()
        logger.info(
            f"warm: batch {batch_id} resolved {len(requirements)} requirement(s); "
            f"{totals['present'] + totals['checked_empty']} already on disk, "
            f"{len(enqueued)} enqueued")
        return len(enqueued)
    except Exception as e:
        logger.error(f"warm: batch-end hook failed for {batch_id}: {e}", exc_info=True)
        return 0


def _recorded_batch_groups(batch_id: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """``(expert_class, settings_hash) -> {settings, rules, symbols}`` for one batch.

    Grouped by the SETTINGS, not by the instance: two instances of one expert with
    identical settings declare identical requirements, and resolving once per
    instance would enqueue the same keys twice for the dedupe to throw away.
    """
    store = get_replay_store()
    if store is None:
        return {}
    session_id = store.session_id
    if session_id is None:
        return {}
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for record in store.index.analyses(session_id):
        if (record.branch_flags or {}).get("batch_id") != batch_id:
            continue
        if record.settings_object is None:
            logger.warning(
                f"warm: analysis {record.analysis_id} ({record.expert_class}) recorded no "
                f"settings; its dependencies cannot be resolved")
            continue
        key = (record.expert_class, record.settings_object)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "settings": store.decode_object(record.settings_object),
                "rules": _rules_for_instance(record.expert_instance_id),
                "symbols": set(),
            }
        if record.symbol:
            group["symbols"].add(record.symbol)
    return groups


def _rules_for_instance(expert_instance_id: Optional[int]):
    """Both rulesets of an expert instance, as the event actions the resolver reads.

    The rules are NOT in the capture record (they are not an expert input), so they
    come from the database. An instance that has since been deleted yields ``None``
    -- the expert's own declarations still resolve; the rule extras cannot, and that
    is visible as their absence rather than as a wrong answer.
    """
    if expert_instance_id is None:
        return None
    try:
        from .db import get_instance
        from .models import ExpertInstance, Ruleset

        instance = get_instance(ExpertInstance, expert_instance_id)
        if instance is None:
            return None
        actions = []
        for ruleset_id in (instance.enter_market_ruleset_id,
                           instance.open_positions_ruleset_id):
            if ruleset_id is None:
                continue
            ruleset = get_instance(Ruleset, ruleset_id)
            if ruleset is not None:
                actions.extend(ruleset.event_actions)
        return actions
    except Exception as e:  # noqa: BLE001 - the expert's own declarations still stand
        logger.warning(f"warm: rules for expert instance {expert_instance_id} unavailable: {e}")
        return None


# --------------------------------------------------------------------------- #
# Trigger 2: after the exchange close
# --------------------------------------------------------------------------- #
def schedule_settlement_job(job_manager, close_time_provider=None) -> Optional[str]:
    """Schedule the daily post-close warm, or refuse and say why.

    The time is the ACCOUNT's exchange close plus ``warm_settlement_offset_minutes``.
    There is no fallback close time: an installation whose accounts cannot answer
    gets no job and an ERROR in the log, because a guessed close would run the warm
    against a half-published session and pin an incomplete artifact as if it were
    final (spec section 6: "Exchanges, holidays and DST determine the close").
    """
    try:
        provider = close_time_provider or resolve_market_close
        close = provider()
        if close is None:
            logger.error(
                "warm: no account could report its exchange close, so the post-close warm "
                "is NOT scheduled (a guessed close would pin a half-published session)")
            return None
        hour, minute, tz_name = close
        offset = warm_settlement_offset_minutes()
        at = (datetime(2000, 1, 1, hour, minute) + timedelta(minutes=offset))

        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger(hour=at.hour, minute=at.minute, day_of_week="mon-fri",
                              timezone=tz_name)
        job_manager._scheduler.add_job(
            func=run_settlement_warm,
            trigger=trigger,
            id=WARM_SETTLEMENT_JOB_ID,
            name="Post-close Warm and Pin Job",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            f"Post-close warm scheduled for {at.hour:02d}:{at.minute:02d} {tz_name}, Mon-Fri "
            f"(exchange close {hour:02d}:{minute:02d} + {offset}m settlement offset)")
        return WARM_SETTLEMENT_JOB_ID
    except Exception as e:
        logger.error(f"warm: could not schedule the post-close job: {e}", exc_info=True)
        return None


def resolve_market_close() -> Optional[Tuple[int, int, str]]:
    """``(hour, minute, tz)`` of the exchange close, from the first account that knows.

    ``None`` when no account can answer -- a failure, not a default.
    """
    from sqlmodel import select

    from .db import get_db
    from .models import AccountDefinition
    from .utils import get_account_instance_from_id

    with get_db() as session:
        account_ids = [a.id for a in session.exec(select(AccountDefinition)).all()]
    for account_id in account_ids:
        try:
            account = get_account_instance_from_id(account_id)
            if account is None:
                continue
            hours = account.get_market_hours()
            close = hours.close_at or hours.next_close
            if not hours.is_known or close is None:
                continue
            local = close.astimezone(close.tzinfo)
            return local.hour, local.minute, str(close.tzinfo)
        except Exception as e:  # noqa: BLE001 - try the next account
            logger.warning(f"warm: account {account_id} could not report market hours: {e}")
    return None


def run_settlement_warm() -> Optional[str]:
    """Extend the price tails, then pin today's artifacts. Returns the pinned root.

    Step 5 of the lifecycle. The price tails are the requirements the CURRENT
    session's analyses declared: after the close their final daily bars exist, and
    the partial frame live actually saw is already preserved in its capture, so
    fetching the settled tail here cannot overwrite evidence.
    """
    queue = get_warm_queue()
    if queue is None:
        logger.info("warm: settlement job ran with warming off; nothing to do")
        return None
    try:
        _ensure_backend_importable()
        from app.services.warm import planner
        from app.services.warm.roots import materialize_pinned_root

        now = datetime.now(timezone.utc)
        requirements = _session_requirements(now)
        if not requirements:
            logger.info("warm: settlement job found no recorded analyses to settle")
            return None
        plan = planner.plan(requirements, cache_roots(), as_of_now=now)
        enqueued = queue.submit_plan(plan)
        if enqueued:
            # Bounded: the pin waits for the tails, but never past the point where it
            # would still be running into the next session.
            queue.join(timeout=float(warm_settlement_offset_minutes() * 60))
        settled = planner.plan(requirements, cache_roots(), as_of_now=now)
        day = now.date().isoformat()
        destination = pinned_root_for(day)
        manifest = materialize_pinned_root(settled, cache_roots(), destination,
                                           warmed=queue.warmed_keys())
        logger.info(
            f"warm: settlement pinned {len(manifest['files'])} artifact(s) into {destination} "
            f"({len(manifest['unpinned'])} requirement(s) with no artifact)")
        return destination
    except Exception as e:
        logger.error(f"warm: settlement job failed: {e}", exc_info=True)
        return None


def _session_requirements(now: datetime):
    """Every requirement the OPEN capture session's analyses declare."""
    store = get_replay_store()
    if store is None or store.session_id is None:
        return []
    batches = set()
    for record in store.index.analyses(store.session_id):
        batch_id = (record.branch_flags or {}).get("batch_id")
        if batch_id:
            batches.add(batch_id)
    window = Window(start=now - timedelta(days=WARM_WINDOW_DAYS), end=now)
    requirements = []
    for batch_id in sorted(batches):
        for (expert_class, _hash), group in _recorded_batch_groups(batch_id).items():
            try:
                requirements.extend(required_replay_inputs(
                    expert_class, group["settings"], group["rules"],
                    sorted(group["symbols"]), window))
            except MissingDependencySetting as e:
                logger.error(f"warm: settlement cannot resolve {expert_class}: {e}")
    return requirements


__all__ = [
    "PINNED_SUBDIR",
    "WARM_ALLOWANCE_KEY",
    "WARM_DEFAULTS",
    "WARM_ENABLED_KEY",
    "WARM_SETTLEMENT_JOB_ID",
    "WARM_SETTLEMENT_OFFSET_KEY",
    "WARM_WINDOW_DAYS",
    "WARM_WORKERS_KEY",
    "WarmServiceUnavailable",
    "cache_roots",
    "ensure_settings",
    "get_warm_queue",
    "initialize_warm_service",
    "on_analysis_batch_end",
    "pinned_root_for",
    "resolve_market_close",
    "run_settlement_warm",
    "schedule_settlement_job",
    "shutdown_warm_service",
    "warm_daily_allowance_bytes",
    "warm_enabled",
    "warm_settlement_offset_minutes",
    "warm_workers",
]
