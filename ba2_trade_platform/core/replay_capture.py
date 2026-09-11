"""Live host wiring for expert input capture (spec step 2).

`docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md` sections 3
and 4. ``ba2_common.core.replay`` decides WHAT is recorded and HOW it is stored;
this module is the only place that decides WHETHER, WHERE and FOR WHICH INSTANCE
-- it reads the app setting, resolves the store root under this instance's cache
folder, describes the running build, and installs the store through the package
seam.

**Off by default.** The ``replay_capture_enabled`` AppSetting is created as
``"false"`` on first read (there is no alembic migration for AppSetting rows --
same get-or-create convention as ``worker_count``). With it off, ``get_replay_store()``
stays ``None``, every tap and the clock seam are plain passthroughs, and the live
path is byte-identical to a build without this module.

**Never in the way of a trade.** Every function here is wrapped: a capture that
cannot start, cannot roll its session or cannot shut down leaves an ERROR in the
log and returns. Recording is observational -- it may lose coverage, never a trade.
"""
import hashlib
import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool
from ba2_common.core.interfaces.MarketExpertInterface import set_capture_batch_provider
from ba2_common.core.replay import (
    ReplayStore,
    SessionRecord,
    get_replay_store,
    set_replay_store,
)

from ..logger import logger
from .db import add_instance
from .models import AppSetting

#: The app setting that turns capture on. Created as "false" on first read.
CAPTURE_SETTING_KEY = "replay_capture_enabled"

#: Store layout version. A future incompatible layout gets its own directory
#: rather than mixing two shapes in one root.
STORE_LAYOUT = "v1"

#: The exchange the recorded decisions are made against.
EXCHANGE_TZ = "America/New_York"

#: How long a session rollover waits for the writer before reporting what it
#: could not flush. SHORT on purpose: the rollover happens on an analysis thread
#: at the first analysis after midnight UTC, and a slow disk must delay a trading
#: decision by seconds at most. Whatever is still queued is reported by ``drain``
#: and leaves the old session marked ``interrupted`` -- an honest, visible gap
#: rather than a stalled worker.
ROLLOVER_DRAIN_TIMEOUT = 2.0

#: The expert classes whose live analyses this delivery records (spec section 5).
#: Named explicitly: an expert that is NOT in this list is uncovered, and a
#: coverage report must be able to say so rather than imply it was checked.
RECORDED_EXPERTS = (
    "FMPRating",
    "FMPEarningsDrift",
    "FMPInsiderClusterBuy",
    "DeterministicScorer",
)

_LOCK = threading.RLock()
_SESSION_DATE: Optional[str] = None
_SESSION_TEMPLATE: Dict[str, Any] = {}
_BATCH = threading.local()


# --------------------------------------------------------------------------- #
# Batch linkage
# --------------------------------------------------------------------------- #
def set_current_batch(batch_id: Optional[str]) -> None:
    """Remember which analysis batch THIS worker thread is running.

    Thread-local on purpose: the batch id belongs to the recording, and writing
    it onto the MarketAnalysis (or any other trading row) to carry it across
    would change live data for a diagnostic. Workers run concurrently, so a
    process-wide value would attribute one expert's analyses to another's batch.
    """
    _BATCH.batch_id = batch_id


def clear_current_batch() -> None:
    """Forget this thread's batch id (call from the worker's ``finally``)."""
    _BATCH.batch_id = None


def get_current_batch() -> Optional[str]:
    """This thread's batch id, or ``None``."""
    return getattr(_BATCH, "batch_id", None)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def capture_enabled() -> bool:
    """Read (creating on first use) the ``replay_capture_enabled`` app setting.

    Reads the row directly rather than through ``get_setting``: on the FIRST run
    the row legitimately does not exist yet, and ``get_setting`` logs that absence
    as a WARNING. A warning that fires on the expected path is noise that teaches
    readers to ignore warnings. A real failure (no database, a broken schema) is
    not swallowed here -- it propagates to :func:`initialize_replay_capture`,
    which logs it as the ERROR it is and leaves capture off.
    """
    from sqlmodel import select

    from .db import get_db

    with get_db() as session:
        row = session.exec(
            select(AppSetting).where(AppSetting.key == CAPTURE_SETTING_KEY)
        ).first()
    if row is None:
        add_instance(AppSetting(key=CAPTURE_SETTING_KEY, value_str="false"))
        logger.info(
            f"Created {CAPTURE_SETTING_KEY} AppSetting with default value: false"
        )
        return False
    # coerce_bool, not `== 'true'`: a value written as 1/"1"/"True" means the same
    # thing to whoever set it, and raises loudly on a spelling nothing can mean.
    return coerce_bool(row.value_str)


def store_root() -> str:
    """Where this instance's captures live: ``<CACHE_FOLDER>/replay/<layout>``."""
    import ba2_trade_platform.config as config

    return os.path.join(config.CACHE_FOLDER, "replay", STORE_LAYOUT)


def initialize_replay_capture() -> Optional[ReplayStore]:
    """Open the capture store and begin a session, if the setting says so.

    Returns the store (``None`` when capture is off). Safe to call twice: the
    second call is a no-op while a store is already installed.
    """
    try:
        with _LOCK:
            if get_replay_store() is not None:
                logger.debug("replay capture already initialized")
                return get_replay_store()
            if not capture_enabled():
                logger.info(
                    f"Replay capture is OFF ({CAPTURE_SETTING_KEY}=false); "
                    f"no expert inputs are recorded"
                )
                return None

            root = store_root()
            store = ReplayStore(root, writer="thread")
            # A session left 'open' means the previous process died: mark it so
            # its analyses stay visible in coverage instead of looking complete.
            store.mark_interrupted_sessions()
            global _SESSION_TEMPLATE
            _SESSION_TEMPLATE = _describe_instance()
            store.set_session_hook(_roll_session_if_needed)
            set_replay_store(store)
            set_capture_batch_provider(get_current_batch)
            _begin_session(store)
            logger.info(f"Replay capture is ON; recording expert inputs to {root}")
            return store
    except Exception as e:
        logger.error(f"Replay capture could not be initialized: {e}", exc_info=True)
        return None


def shutdown_replay_capture(timeout: float = 30.0) -> None:
    """Finalize the open session, drain the writer and uninstall the store.

    THE LIVE APP HAS NO SHUTDOWN PATH that reaches here: nothing calls
    ``shutdown_worker_queue`` either, and the process is stopped outright. That
    is by design, not an oversight to fix by inventing an atexit hook that would
    run during interpreter teardown -- the recovery is at the OTHER end.
    :func:`initialize_replay_capture` calls ``mark_interrupted_sessions()`` when
    it opens the store, so a session the previous process left ``open`` becomes
    ``interrupted`` and its analyses stay visible in coverage instead of reading
    as a complete session. This function exists for the callers that DO have a
    lifecycle: tests, tools, and any future orderly shutdown.
    """
    try:
        with _LOCK:
            store = get_replay_store()
            if store is None:
                return
            global _SESSION_DATE
            session_id = store.session_id
            set_replay_store(None)
            set_capture_batch_provider(None)
            if session_id is not None:
                store.finalize_session(session_id, timeout=timeout)
            store.close(timeout=timeout)
            _SESSION_DATE = None
            logger.info(f"Replay capture stopped (session {session_id})")
    except Exception as e:
        logger.error(f"Replay capture could not be shut down cleanly: {e}", exc_info=True)


def get_capture_health() -> Dict[str, int]:
    """Per-kind capture failure counts for this process (empty when capture is off)."""
    store = get_replay_store()
    if store is None:
        return {}
    return store.health.as_dict()


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def _begin_session(store: ReplayStore) -> str:
    global _SESSION_DATE
    today = datetime.now(timezone.utc).date().isoformat()
    session = SessionRecord(
        session_id=f"{today}-{uuid.uuid4().hex[:12]}",
        started_at=datetime.now(timezone.utc),
        exchange_tz=EXCHANGE_TZ,
        **_SESSION_TEMPLATE,
    )
    store.begin_session(session)
    _SESSION_DATE = today
    return session.session_id


def _roll_session_if_needed() -> None:
    """Start a new session when the UTC date has changed (called per analysis).

    A session is the unit an export ships and a replay reads, so it is bounded by
    something an operator can name. The check is a string compare against the open
    session's date; the rollover finalizes the old session so its status is
    ``finalized``, not left ``open`` forever.

    ORDER MATTERS, because this runs on an analysis thread. The new session is
    opened INSIDE the lock -- one thread wins the race, everyone else sees the new
    date and returns immediately -- and the old session is finalized OUTSIDE it,
    with a short timeout. Finalizing under the lock (as this first did) meant one
    thread waiting up to 30 seconds on the writer while every concurrent analysis
    queued behind it at the very first analysis after midnight. What the writer
    cannot flush in time is reported by ``drain`` and marks that session
    ``interrupted``: a visible gap, not a stalled worker.
    """
    store = get_replay_store()
    if store is None:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    if _SESSION_DATE == today:
        return
    with _LOCK:
        if _SESSION_DATE == today:          # another thread rolled it first
            return
        previous = store.session_id
        new_session = _begin_session(store)
        logger.info(
            f"Replay capture session rolled at the UTC date change: "
            f"{previous} -> {new_session}"
        )
    if previous is not None:
        store.finalize_session(previous, timeout=ROLLOVER_DRAIN_TIMEOUT)


# --------------------------------------------------------------------------- #
# What was running
# --------------------------------------------------------------------------- #
def _describe_instance() -> Dict[str, Any]:
    """The build/instance fields every session of this process carries."""
    from ba2_trade_platform.version import APP_VERSION

    revision, dirty = _source_revision()
    return {
        "instance_id": _instance_id(),
        "app_version": APP_VERSION,
        "package_versions": _package_versions(),
        "source_revision": revision,
        "dirty": dirty,
        "config_hashes": {},
        "capabilities": {"recorded_experts": list(RECORDED_EXPERTS)},
    }


def _instance_id() -> str:
    """A stable, OPAQUE id for this installation: sha256 of its database path.

    The path itself names a user's home directory and, on the prod box, which
    instance is which -- neither belongs in an export that gets shared. The hash
    still tells two instances apart, which is all a comparison needs.
    """
    import ba2_trade_platform.config as config

    absolute = os.path.abspath(config.DB_FILE)
    return hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:16]


def _package_versions() -> Dict[str, str]:
    """The shared packages this process is actually running."""
    versions: Dict[str, str] = {}
    for name in ("ba2_common", "ba2_providers", "ba2_experts"):
        try:
            module = __import__(name)
            # "unknown" rather than a guess: a package that declares no version
            # is a fact a comparison must see, not one to invent a number for.
            versions[name] = str(getattr(module, "__version__", "unknown"))
        except Exception as e:
            logger.warning(f"replay capture: {name} version unavailable: {e}")
            versions[name] = "unknown"
    return versions


def _source_revision():
    """``(revision, dirty)`` from git, or ``(None, False)`` when git cannot answer.

    Guarded on purpose: an installed copy with no ``.git`` is a normal deployment,
    not an error -- but the revision then stays UNKNOWN (``None``) instead of
    being filled with something that looks like an answer.
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
            text=True, timeout=10,
        )
        if revision.returncode != 0:
            return None, False
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True,
            text=True, timeout=10,
        )
        dirty = bool(status.returncode == 0 and status.stdout.strip())
        return revision.stdout.strip() or None, dirty
    except Exception as e:
        logger.warning(f"replay capture: source revision unavailable: {e}")
        return None, False
