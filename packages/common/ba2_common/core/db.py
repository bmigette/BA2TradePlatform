from sqlmodel import Session, SQLModel, create_engine

from ba2_common.config import DB_FILE as _DEFAULT_DB_FILE, DB_PERF_LOG_THRESHOLD_MS
from ba2_common.logger import logger
from sqlalchemy import select, event
# Underscored: ``ba2_trade_platform/core/db.py`` re-exports this module with ``import *``, and a
# bare ``object_session`` there would be a new public name nobody asked for.
from sqlalchemy.orm import object_session as _object_session
import os
import sys
import threading
import time
from queue import Queue
import atexit
from contextlib import contextmanager


# --- DB config seam (lazy, configurable engine) ---------------------------------
# No DB I/O / engine creation happens at import. The engine is built lazily on the
# first get_engine() call. configure_db() lets tests/backtests point ba2_common at
# a throwaway sqlite file; the default preserves the live ~/Documents path.
_db_file = _DEFAULT_DB_FILE   # global default; live path for backward-compat
_engine = None
# Per-THREAD DB override. The global engine above is what the live app + its worker threads
# use (unchanged). A thread that calls configure_db_threadlocal() gets its OWN engine to its
# OWN sqlite file — so parallel backtest trials never clobber each other's per-run DB. Threads
# WITHOUT an override fall through to the shared global engine.
_tls = threading.local()


def _build_engine(db_file: str):
    """Create a configured SQLModel engine for ``db_file`` (pooled, WAL pragmas).

    The sentinel ``":memory:"`` builds a private, RAM-only SQLite engine using a
    ``StaticPool`` (a single shared connection — required so every Session sees the SAME
    in-memory database rather than a fresh empty one per pooled connection). Used by the
    backtest trading DB: those order/transaction rows are ephemeral (the run's results are
    extracted before teardown), so keeping them in RAM removes the per-write disk fsync that
    dominates a many-thousand-order backtest. The live app passes a real file path and is
    unaffected by this branch.
    """
    logger.debug(f"Database file path: {db_file}")
    if db_file == ":memory:":
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )

        @event.listens_for(engine, "connect")
        def _set_memory_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA synchronous=OFF")   # no durability needed for a throwaway DB
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.close()

        return engine

    dirpath = os.path.dirname(db_file)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
        pool_size=20, max_overflow=40, pool_timeout=10, pool_recycle=600,
        pool_pre_ping=True, echo=False,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def configure_db(db_file: str) -> None:
    """Point ba2_common's GLOBAL engine at a specific sqlite file. Call BEFORE first
    get_engine(). Resets the global engine so the new path takes effect (tests / a single
    backtest). Unchanged behaviour for the live app.

    Also repoints the rotating file logs to ``<db folder>/logs`` so each instance (live trade
    vs test, or two live instances on different --db-file paths) logs to its own folder instead
    of all sharing BA2TradeCommon/logs and racing on rollover (Windows WinError 32)."""
    global _db_file, _engine
    _db_file = db_file
    _engine = None
    # Keep logs next to this instance's DB. Skip throwaway/in-memory DBs (no on-disk folder).
    if db_file and db_file != ":memory:":
        try:
            from ba2_common.logger import reconfigure_file_logging
            reconfigure_file_logging(os.path.join(os.path.dirname(os.path.abspath(db_file)), "logs"))
        except Exception as e:  # noqa: BLE001 — logging relocation must never break DB config
            logger.warning(f"could not relocate logs next to DB {db_file!r}: {e}")


def configure_db_threadlocal(db_file: str) -> None:
    """Set a PER-THREAD DB override (parallel backtest trials). ``get_engine()`` consults this
    BEFORE the global default and memoizes a per-thread engine; the global engine other threads
    use is untouched. Disposes any prior thread-local engine to avoid connection leaks."""
    old = getattr(_tls, "engine", None)
    if old is not None:
        try:
            old.dispose()
        except Exception:  # noqa: BLE001
            pass
    _tls.db_file = db_file
    _tls.engine = None
    _tls.ruleset_cache = None  # fresh run on this thread -> drop any prior run's ruleset cache


def clear_threadlocal_db() -> None:
    """Drop this thread's DB override (back to the global default), disposing its engine."""
    old = getattr(_tls, "engine", None)
    if old is not None:
        try:
            old.dispose()
        except Exception:  # noqa: BLE001
            pass
    _tls.db_file = None
    _tls.engine = None
    _tls.ruleset_cache = None  # drop the per-run ruleset cache (see cached_ruleset_eventactions)


def cached_ruleset_eventactions(ruleset_id, loader):
    """Per-thread, per-run cache of a ruleset's ``(ruleset, event_actions)`` — the evaluator loads
    these on EVERY evaluate() via a join, but they are STATIC within a backtest run, so re-loading
    them per (symbol, bar) is pure overhead.

    Only caches when a thread-local BT DB is active (``configure_db_threadlocal``); in LIVE (no
    thread-local DB) it always calls ``loader()`` fresh, because a user can edit a ruleset between
    analyses. The cache lives on the thread-local and is cleared by ``configure_db_threadlocal`` /
    ``clear_threadlocal_db``, so parallel GA trials + reruns (each on their own :memory: DB, reusing
    the same ruleset ids) never share entries. Cached rows are read-only in the evaluator.
    """
    if not getattr(_tls, "db_file", None):  # live (no thread-local DB) -> never cache
        return loader()
    cache = getattr(_tls, "ruleset_cache", None)
    if cache is None:
        cache = {}
        _tls.ruleset_cache = cache
    if ruleset_id not in cache:
        cache[ruleset_id] = loader()
    return cache[ruleset_id]


def get_engine():
    """Lazily build (and memoize) the SQLModel engine. A per-thread override wins; otherwise
    the shared global engine. No DB I/O happens at import."""
    global _engine
    tl_file = getattr(_tls, "db_file", None)
    if tl_file:
        eng = getattr(_tls, "engine", None)
        if eng is None:
            eng = _build_engine(tl_file)
            _tls.engine = eng
        return eng
    if _engine is None:
        _engine = _build_engine(_db_file)
    return _engine


def _log_db_perf(operation: str, detail: str, duration_ms: float):
    """Log a database performance measurement if above threshold."""
    if duration_ms >= DB_PERF_LOG_THRESHOLD_MS:
        msg = f"[DB:{operation}] {detail} - {duration_ms:.2f}ms"
        if duration_ms > 1000:
            logger.warning(msg)
        else:
            logger.info(msg)


def _who(depth: int = 2) -> str:
    """``<thread name>/<calling function>()`` for the frame ``depth`` levels up, or a safe
    placeholder. Deliberately cheap: ``sys._getframe`` + two attribute reads, no
    ``inspect.stack()`` (which materialises the WHOLE stack -- the old lock-wait log built it
    up to three times per contended acquire)."""
    try:
        name = sys._getframe(depth).f_code.co_name
    except Exception:  # noqa: BLE001 -- a diagnostic must never break a write
        name = "unknown"
    return f"{threading.current_thread().name}/{name}()"


class _TimedWriteLock:
    """``threading.Lock`` that reports who waited, how long, and WHO WAS HOLDING IT.

    Read the log line carefully before blaming SQLite. This is a plain, NON-REENTRANT,
    in-process mutex serialising this process's own writers. It is NOT a sqlite lock and a wait
    here says nothing about database contention -- the old wording,
    ``update_instance() waited for write lock``, was read as SQLite contention by every
    investigator of the 2026-08 incidents (and by the incident report), which sent days of
    analysis in the wrong direction.

    The holder is recorded on acquire so a wait line is ACTIONABLE: a line that says only that
    somebody waited tells you nothing about whom to go and look at.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # "<thread>/<function>()" of the current holder. Only ever written while the lock is
        # held (or cleared just before release), so the racy read below can see a stale-but-
        # plausible name at worst -- acceptable for a diagnostic, and far better than nothing.
        self._holder = None

    def __enter__(self):
        blocked_by = "nobody (uncontended)"
        start = time.perf_counter()
        if not self._lock.acquire(blocking=False):
            # Snapshot the holder BEFORE blocking: that is the thread that is making us wait.
            blocked_by = self._holder or "unknown (released while we were queuing)"
            self._lock.acquire()
        wait_ms = (time.perf_counter() - start) * 1000
        self._caller_wait_ms = wait_ms
        self._holder = _who()
        if wait_ms >= DB_PERF_LOG_THRESHOLD_MS:
            _log_db_perf(
                "lock_wait",
                f"{self._holder} waited for the in-process write mutex "
                f"(db._db_write_lock, a threading.Lock -- NOT a SQLite lock), "
                f"held by {blocked_by}",
                wait_ms,
            )
        return self

    def __exit__(self, *args):
        self._holder = None
        self._lock.release()

    def acquire(self, *args, **kwargs):
        # Kept in step with __enter__ so a holder recorded here is never stale/missing for the
        # next waiter (nothing uses these directly today; a future caller must not silently
        # lose the attribution).
        got = self._lock.acquire(*args, **kwargs)
        if got:
            self._holder = _who()
        return got

    def release(self):
        self._holder = None
        return self._lock.release()


# Thread lock for all database write operations (with timing instrumentation)
_db_write_lock = _TimedWriteLock()

# Activity logging queue for async processing (prevents blocking on database locks)
_activity_log_queue = Queue(maxsize=1000)
_activity_log_thread = None

# Backtest-scoped switch: when True, log_activity() is a no-op. A daily backtest would
# otherwise enqueue an ActivityLog write on every actionable bar (from TradeActionEvaluator
# and TradeRiskManagement), each serialized through the shared DB write lock — dominating
# the run time. The LIVE path never disables this; default is enabled.
_activity_logging_disabled = False


def set_activity_logging_disabled(disabled: bool) -> None:
    """Set the global activity-logging kill switch. Prefer the ``activity_logging_disabled()``
    context manager; this setter exists for explicit teardown in tests."""
    global _activity_logging_disabled
    _activity_logging_disabled = bool(disabled)


@contextmanager
def activity_logging_disabled():
    """Within this context ``log_activity`` is a no-op (backtest perf). Restores the prior
    flag on exit (re-entrant safe), so nested use and the live default are preserved."""
    global _activity_logging_disabled
    prev = _activity_logging_disabled
    _activity_logging_disabled = True
    try:
        yield
    finally:
        _activity_logging_disabled = prev


def retry_on_lock(func):
    """Decorator to retry database operations on lock errors with exponential backoff."""
    def wrapper(*args, **kwargs):
        max_retries = 4    # 4 tries => sleeps after attempts 0, 1, 2 only (3 sleeps, not 4)
        base_delay = 1.0   # first sleep is ~1s
        max_delay = 30.0   # ceiling on ONE sleep -- see reachable_cap below: never reached
        # min(1.0 * 2**attempt, 30.0) over attempt in {0,1,2} = 1 + 2 + 4 = 7s of backoff.
        # The 30s ceiling would need attempt >= 5, which this loop cannot reach, so reporting
        # it as the delay budget (as this decorator used to) overstates the real wait 4x.
        reachable_cap = min(base_delay * (2 ** (max_retries - 2)), max_delay)
        delays = []

        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Check if it's a database lock error
                if "database is locked" in str(e).lower():
                    if attempt < max_retries - 1:
                        # Exponential backoff with jitter to prevent thundering herd
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        # Add small random jitter (±10%) to prevent synchronized retries
                        import random
                        jitter = delay * 0.1 * (random.random() * 2 - 1)  # ±10% jitter
                        actual_delay = max(0.5, delay + jitter)  # Minimum 0.5s delay
                        
                        # Only show warning without stack trace for retry attempts
                        logger.warning(f"Database locked, retrying in {actual_delay:.2f}s (attempt {attempt + 1}/{max_retries})")
                        delays.append(actual_delay)
                        time.sleep(actual_delay)
                    else:
                        # Report what ACTUALLY happened. The old line -- "Database locked after 4
                        # attempts with up to 30.0s delays" -- was wrong twice: it implied a ~2
                        # minute wait, when the real total is ~7s, and it advertised a 30s cap
                        # this loop can never reach. Investigators sized their expectations off
                        # this line and looked for a stall that was 4x longer than the real one.
                        waited = sum(delays)
                        detail = ", ".join(f"{d:.2f}s" for d in delays) or "none"
                        logger.error(
                            f"Database locked: gave up after {max_retries} attempts and "
                            f"{waited:.2f}s of actual backoff (sleeps: {detail}); the longest "
                            f"single delay this decorator can ever reach is {reachable_cap:.1f}s "
                            f"(the {max_delay:.0f}s ceiling needs more attempts than it makes). "
                            f"Any stall much longer than {waited:.2f}s came from somewhere else "
                            f"- e.g. sqlite's 30s busy_timeout, or the in-process write mutex.",
                            exc_info=True,
                        )
                        raise
                else:
                    # Not a lock error, raise immediately with stack trace
                    logger.error(f"Error in {func.__name__}: {e}", exc_info=True)
                    raise
        
    return wrapper


def _activity_log_worker():
    """
    Background worker thread that processes activity log entries from the queue.
    This prevents activity logging from blocking database writes during high concurrency.
    """
    from ba2_common.core.models import ActivityLog
    
    while True:
        try:
            # Get item from queue with timeout (allows thread to exit cleanly)
            item = _activity_log_queue.get(timeout=2.0)
            
            if item is None:  # Sentinel value to stop the thread
                break
            
            # Try to add the activity log entry with retries
            severity, activity_type, description, data, source_expert_id, source_account_id = item
            
            try:
                activity = ActivityLog(
                    severity=severity,
                    type=activity_type,
                    description=description,
                    data=data or {},
                    source_expert_id=source_expert_id,
                    source_account_id=source_account_id
                )
                add_instance(activity)  # This has @retry_on_lock decorator
                logger.debug(f"Activity logged (async): {activity_type}")
            except Exception as e:
                # Even async logging failed - log warning but don't crash worker
                logger.warning(f"Failed to log activity (async): {e}")
        
        except Exception as e:
            # Queue timeout or other error - continue processing
            if "Empty" not in str(type(e)):
                logger.debug(f"Activity log worker: {e}")


def _start_activity_log_worker():
    """Start the background activity log worker thread."""
    global _activity_log_thread
    
    if _activity_log_thread is None or not _activity_log_thread.is_alive():
        _activity_log_thread = threading.Thread(target=_activity_log_worker, daemon=True)
        _activity_log_thread.name = "ActivityLogWorker"
        _activity_log_thread.start()
        logger.debug("Started activity log worker thread")


def _stop_activity_log_worker():
    """Stop the background activity log worker thread gracefully."""
    global _activity_log_thread
    
    if _activity_log_thread and _activity_log_thread.is_alive():
        # Send sentinel to stop the worker
        try:
            _activity_log_queue.put(None, timeout=1.0)
        except Exception as e:
            logger.warning(f"Could not send stop signal to activity log worker: {e}")
        
        # Wait for worker to finish
        _activity_log_thread.join(timeout=5.0)
        # NOTE: do NOT log here. This is only ever invoked from the atexit handler during
        # interpreter shutdown, when logging's stdio handler may already be closed; logging
        # swallows the resulting IOError internally and prints "I/O operation on closed file"
        # via handleError (a try/except here cannot catch it). The trace was pure shutdown
        # noise, so the debug line is removed.


# Register cleanup function to stop worker on exit
atexit.register(_stop_activity_log_worker)


def _foreign_session(instance, session):
    """The live Session that owns ``instance`` and that is NOT the one the caller told us to
    use -- or None. See ``_guard_foreign_session`` for why that combination is the bug.

    ``update_instance(row, session)`` where ``row`` came out of that same ``session`` is the
    NORMAL, CORRECT shape (``ExtendableSettingsInterface.set_setting`` and the whole settings UI
    do it): no second connection is opened, so there is nothing to flag. Only a session we are
    NOT going to use is dangerous.
    """
    try:
        owner = _object_session(instance)
    except Exception:  # noqa: BLE001 -- a safety check must never break a write
        return None
    return None if owner is None or owner is session else owner


def _guard_foreign_session(op: str, instance, session):
    """Detect the single-thread self-deadlock and return the session that must be used.

    THE BUG (PROD 2026-08-10, and again 2026-08-24 after a partial fix): a caller holds a
    long-lived Session, an autoflush emits an UPDATE -- which takes sqlite's one write lock on
    THAT connection -- and then the same thread calls ``update_instance(row)`` with no session.
    We open a SECOND connection whose COMMIT waits for a lock its own caller is holding and
    nobody is left to release. 15 minutes of no writes, ending 15 ms after the outer session
    closed; a funded trade lost, twice.

    So: if a live session other than ``session`` owns this row, using a different connection is
    never correct. We say so LOUDLY (with a stack, so the call site is attributable and gets
    fixed) and then use the owning session, which is the only connection that can commit this
    row without waiting for itself.

    Note that ``expunge(instance)`` -- the first fix considered -- does NOT work: detaching the
    object removes it from the owner's identity map but leaves the owner's already-flushed
    UPDATE, and therefore the write lock, exactly where it was. It converts the freeze into a
    ``database is locked`` after the full busy_timeout. ``tests/test_db_attached_instance_guard``
    pins that.

    Returns the Session to use (the caller's ``session`` when there is nothing wrong).
    """
    owner = _foreign_session(instance, session)
    if owner is None:
        return session
    pk = getattr(instance, "id", None)
    logger.error(
        f"{op}({type(instance).__name__} id={pk}) was handed a row that session "
        f"id={id(owner)} still owns"
        + (f", while the caller asked for session id={id(session)}" if session is not None else
           " (no session argument)")
        + ". Using a second connection for a row a live session already holds is what "
          "self-deadlocked production on 2026-08-10 and 2026-08-24. Falling back to the owning "
          "session -- FIX THE CALL SITE: pass the session explicitly, or commit/close it first.",
        stack_info=True,
    )
    return owner


def _inmem_route(instance_or_model) -> bool:
    """True iff this (instance or model class) should be routed to the sql-less in-memory trade
    store instead of SQLite. Only ever True inside a backtest's ``inmem_trades()`` block
    (thread-local flag). Live NEVER enters that block, so this is always False in live -> the db
    helpers take the unchanged SQLite path. Imported lazily to avoid a db<->models<->trade_store
    import cycle."""
    from ba2_common.core import trade_store as _ts
    if not _ts.inmem_trades_active():
        return False
    model = instance_or_model if isinstance(instance_or_model, type) else type(instance_or_model)
    return _ts.is_inmem_model(model)


# --- genesis stamp: a schema create_all() just built IS the alembic baseline ------
#
# The migration chain was authored against databases SQLModel had ALREADY created
# (46 revisions, 9 create_table calls, 21 of the 28 tables in SQLModel.metadata
# created by no revision at all), so it cannot be replayed from an empty database.
# create_all() is therefore the genesis path -- and a database it has just built is
# already AT head. Unless it says so in alembic_version, `migrate.py upgrade` walks
# the whole chain from base and dies on the first duplicate column, which is why no
# fresh install has ever been able to run a migration.
#
# Everything below is best-effort and imported lazily. alembic is a dev/ops tool,
# not a runtime dependency of this package (the test platform imports ba2_common
# without needing it), db.py is deliberately import-light, and a failed stamp must
# never stop the app from starting -- so every failure path logs and returns.

_UNRESOLVED = object()
_alembic_head_script = _UNRESOLVED   # memoized (ScriptDirectory, head) | None


def _find_alembic_dir():
    """Locate the repo's ``alembic/`` migration-scripts directory, or None.

    Looks for an ``alembic.ini`` sitting next to an ``alembic/versions/`` directory,
    walking up from this file (repo root is 5 levels above
    ``packages/common/ba2_common/core/db.py``) and then from the cwd. Returns None
    when this package is installed without the repo around it: there is then no
    chain to stamp against and we do nothing.
    """
    import pathlib
    here = pathlib.Path(__file__).resolve()
    cwd = pathlib.Path.cwd().resolve()
    # Bounded walks -- an unbounded climb to "/" could match some unrelated project.
    bases = list(here.parents)[:6] + [cwd] + list(cwd.parents)[:3]
    for base in bases:
        if (base / "alembic.ini").is_file() and (base / "alembic" / "versions").is_dir():
            return str(base / "alembic")
    return None


def _resolve_alembic_head():
    """``(ScriptDirectory, head_revision)`` for the migration chain, or None.

    The head is READ from the revision scripts, never hardcoded. Returns None -- and
    stamps nothing -- if alembic is unavailable, the scripts are not there, or the
    history has branched into several heads (guessing which one a fresh schema
    corresponds to is exactly the kind of wrong-by-default this fixes).

    Memoized (successes AND failures) because scanning versions/ is not free and
    init_db() runs once per backtest run.
    """
    global _alembic_head_script
    if _alembic_head_script is not _UNRESOLVED:
        return _alembic_head_script
    _alembic_head_script = None
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except Exception as e:  # noqa: BLE001 -- alembic is optional at runtime
        logger.warning(f"alembic is not available ({e}); a new database will not be stamped")
        return None
    script_location = _find_alembic_dir()
    if script_location is None:
        logger.debug("no alembic/ migration directory found; a new database will not be stamped")
        return None
    try:
        # A BARE Config carrying only script_location: reading alembic.ini would apply
        # its prepend_sys_path (mutating sys.path in the live process), and running
        # env.py would import the whole live platform and re-point the engine.
        cfg = Config()
        cfg.set_main_option("script_location", script_location)
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"could not read alembic revisions in {script_location}: {e}")
        return None
    if len(heads) != 1:
        logger.warning(
            f"alembic history has {len(heads)} heads {heads}; refusing to guess -- "
            "the new database is left unstamped (run `python migrate.py stamp <rev>`)"
        )
        return None
    _alembic_head_script = (script, heads[0])
    return _alembic_head_script


def _schema_is_absent(engine) -> bool:
    """True iff ``engine``'s database currently holds NO tables -- a brand-new file.

    Deliberately strict, and deliberately called BEFORE create_all(). A database that
    has tables but no ``alembic_version`` row is NOT a fresh install: it is the
    known-broken existing state, and stamping it to head would silently skip real
    pending migrations. In-memory databases are excluded too -- they are throwaway
    backtest DBs that are never migrated. On any error the answer is "not fresh":
    never stamp on doubt.
    """
    try:
        if engine.url.database in (None, "", ":memory:"):
            return False
        from sqlalchemy import inspect as sa_inspect
        tables = [t for t in sa_inspect(engine).get_table_names()
                  if not t.startswith("sqlite_")]
        return not tables
    except Exception as e:  # noqa: BLE001
        logger.warning(f"could not inspect the database before create_all ({e}); "
                       "treating it as an existing install and not stamping")
        return False


def _stamp_alembic_head(engine) -> None:
    """Write the migration chain's head into ``alembic_version`` on a just-created
    schema. Only ever called when ``_schema_is_absent()`` was true before create_all.
    Never raises."""
    resolved = _resolve_alembic_head()
    if resolved is None:
        return
    script, head = resolved
    try:
        from alembic.runtime.migration import MigrationContext
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            existing = context.get_current_heads()
            if existing:
                # Someone (a concurrent process, or a pre-stamped file) got there first.
                logger.debug(f"alembic_version already holds {existing}; not stamping")
                return
            context.stamp(script, head)
        logger.info(f"New database: schema created by SQLModel and stamped at alembic "
                    f"head {head} (migrations are up to date)")
    except Exception as e:
        logger.warning(f"could not stamp the new database at alembic head {head}: {e}",
                       exc_info=True)


def init_db():
    """
    Import models and create all database tables if they do not exist.
    Ensures the database directory exists before table creation.

    When the database turns out to be brand new -- i.e. create_all() is what creates
    the schema -- ``alembic_version`` is stamped at head, because that schema IS the
    head schema. Existing databases are never touched.
    """
    logger.debug("Importing models for table creation")
    from ba2_common.core import models  # Import the models module to register all models
    logger.debug("Models imported successfully")
    # get_engine() lazily creates the engine (and the DB directory) on first use
    engine = get_engine()
    # Must be observed BEFORE create_all -- afterwards every database looks populated.
    fresh_install = _schema_is_absent(engine)
    SQLModel.metadata.create_all(engine)
    logger.info("Database initialized with WAL mode enabled")
    if fresh_install:
        _stamp_alembic_head(engine)

    # Start activity log worker thread
    _start_activity_log_worker()

def get_db():
    """
    Returns a new database session. Caller is responsible for closing the session.
    
    ⚠️ WARNING: Always close the session when done to prevent connection pool exhaustion!
    
    **RECOMMENDED USAGE** (automatically closes session):
    ```python
    with get_db() as session:
        # Use session here
        results = session.exec(select(Model)).all()
    # Session automatically closed
    ```
    
    **DISCOURAGED USAGE** (manual close required):
    ```python
    session = get_db()
    try:
        results = session.exec(select(Model)).all()
    finally:
        session.close()  # ⚠️ MUST close manually!
    ```

    Returns:
        Session: An active SQLModel session.
    """
    session = Session(get_engine())
    
    # Get caller information from stack trace
    # import traceback
    # import inspect
    # stack = inspect.stack()
    
    # # Build caller info string with last 2 calling functions
    # caller_info = []
    # for i in range(1, min(3, len(stack))):  # Get frames 1 and 2 (skip current function)
    #     frame_info = stack[i]
    #     func_name = frame_info.function
    #     filename = os.path.basename(frame_info.filename)
    #     line_no = frame_info.lineno
    #     caller_info.append(f"{filename}:{func_name}():{line_no}")
    
    # caller_str = " <- ".join(caller_info) if caller_info else "unknown"
    
    # logger.debug(f"Database session created (id={id(session)}) [Called from: {caller_str}]")
    return session

@retry_on_lock
def add_instance(instance, session: Session | None = None, expunge_after_flush: bool = False):
    """
    Add a new instance to the database.
    If a session is provided, use it; otherwise, create a new session.
    Commits the transaction after adding.
    Thread-safe: Uses a lock to prevent concurrent write conflicts.
    Retries on database lock errors with exponential backoff.

    If a LIVE session other than ``session`` still owns ``instance``, this logs a loud,
    stack-carrying error and writes through the OWNING session instead of opening a second
    connection to a row that session already holds (which self-deadlocked production twice in
    2026-08). That owning session gets committed as a result. See ``_guard_foreign_session``.

    Args:
        instance: The instance to add.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.
        expunge_after_flush (bool, optional): If True, expunge the instance from the session after flush
            to prevent attribute expiration. This allows the instance to be used like a normal
            Pydantic/SQLModel object without session errors. Default is False for backward compatibility.

    Returns:
        The ID of the added instance.
    """
    # Backtest sql-less store (flag-gated, BT-only): TradingOrder/Transaction live in RAM dicts
    # during a run (no ORM compile/flush/commit). Live never sets the flag -> falls through.
    if _inmem_route(instance):
        from ba2_common.core import trade_store as _ts
        return _ts.store_add(instance)
    # Self-deadlock guard. Placed after the in-memory route (those rows never touch a Session,
    # and the BT hot path should not pay for the check) but before ANY connection is taken.
    session = _guard_foreign_session("add_instance", instance, session)
    start = time.perf_counter()
    instance_class = instance.__class__.__name__
    try:
        with _db_write_lock:
            try:
                if session:
                    session.add(instance)
                    session.flush()  # Flush to generate the ID without committing
                    instance_id = instance.id  # Get ID after flush
                    if expunge_after_flush:
                        session.expunge(instance)  # Detach from session to prevent attribute expiration
                    session.commit()
                    logger.info(f"Added instance: {instance_class} (id={instance_id})")
                    return instance_id
                else:
                    with Session(get_engine()) as new_session:
                        new_session.add(instance)
                        new_session.flush()  # Flush to generate the ID without committing
                        instance_id = instance.id  # Get ID after flush
                        if expunge_after_flush:
                            new_session.expunge(instance)  # Detach from session to prevent attribute expiration
                        new_session.commit()
                        logger.info(f"Added instance: {instance_class} (id={instance_id})")
                        return instance_id
            except Exception as e:
                # Let the retry decorator handle logging with appropriate detail level
                raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _log_db_perf("query", f"add_instance({instance_class})", duration_ms)

@retry_on_lock
def update_instance(instance, session: Session | None = None):
    """
    Update an existing instance in the database.
    If a session is provided, use it; otherwise, create a new session.
    Commits and refreshes the instance after updating.
    Thread-safe: Uses a lock to prevent concurrent write conflicts.
    Retries on database lock errors with exponential backoff.

    If a LIVE session other than ``session`` still owns ``instance``, that is the
    self-deadlock of 2026-08-10/2026-08-24: this logs a loud, stack-carrying error and then
    writes through the OWNING session, because no other connection can commit that row while
    its owner holds the write lock. Note the consequence -- that owning session gets committed,
    so any other pending work in it is committed too. See ``_guard_foreign_session``; the fix
    is to stop calling this with an attached row, not to rely on the fallback.

    Args:
        instance: The instance to update.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.

    Returns:
        True if update was successful.
    """
    # Backtest sql-less store: the store holds the SAME object by identity, so a caller that
    # mutated it in place is already reflected — persist is a no-op (BT-only; live falls through).
    if _inmem_route(instance):
        from ba2_common.core import trade_store as _ts
        return _ts.store_update(instance)
    # Self-deadlock guard. Placed after the in-memory route (those rows never touch a Session,
    # and the BT hot path should not pay for the check) but before ANY connection is taken.
    session = _guard_foreign_session("update_instance", instance, session)
    start = time.perf_counter()
    instance_class = instance.__class__.__name__
    try:
        with _db_write_lock:
            try:
                instance_id = instance.id
                model_class = type(instance)

                if session:
                    # Merge the instance into the current session to avoid attachment issues
                    merged_instance = session.get(model_class, instance_id)
                    if merged_instance:
                        # Update merged instance with the values from the passed instance
                        for key, value in instance.__dict__.items():
                            if not key.startswith('_'):
                                setattr(merged_instance, key, value)
                        session.commit()
                        session.refresh(merged_instance)
                        # Update the original instance with refreshed values
                        for key in instance.__dict__.keys():
                            if not key.startswith('_') and hasattr(merged_instance, key):
                                setattr(instance, key, getattr(merged_instance, key))
                    else:
                        # Object not found in current session, try adding it
                        session.add(instance)
                        session.commit()
                        session.refresh(instance)
                else:
                    with Session(get_engine()) as new_session:
                        # Get the instance in this session
                        merged_instance = new_session.get(model_class, instance_id)
                        if merged_instance:
                            # Update merged instance with the values from the passed instance
                            for key, value in instance.__dict__.items():
                                if not key.startswith('_'):
                                    setattr(merged_instance, key, value)
                            new_session.commit()
                            new_session.refresh(merged_instance)
                            # Update the original instance with refreshed values
                            for key in instance.__dict__.keys():
                                if not key.startswith('_') and hasattr(merged_instance, key):
                                    setattr(instance, key, getattr(merged_instance, key))
                        else:
                            # Object not found in new session, add it
                            new_session.add(instance)
                            new_session.commit()
                            new_session.refresh(instance)
                return True
            except Exception as e:
                # Let the retry decorator handle logging with appropriate detail level
                raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _log_db_perf("query", f"update_instance({instance_class})", duration_ms)


def delete_instance(instance, session: Session | None = None):
    """
    Delete an instance from the database.
    If a session is provided, use it; otherwise, create a new session.
    Commits the transaction after deleting.
    Thread-safe: Uses a lock to prevent concurrent write conflicts.

    Args:
        instance: The instance to delete.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.

    Returns:
        True if deletion was successful.
    """
    # Backtest sql-less store: drop the row from the in-mem dict (BT-only; live falls through).
    if _inmem_route(instance):
        from ba2_common.core import trade_store as _ts
        return _ts.store_delete(instance)
    with _db_write_lock:
        try:
            instance_id = instance.id
            model_class = type(instance)
            
            if session:
                # Merge the instance into the current session to avoid attachment issues
                merged_instance = session.get(model_class, instance_id)
                if merged_instance:
                    session.delete(merged_instance)
                    session.commit()
                    logger.info(f"Deleted instance with id: {instance_id}")
                    return True
                else:
                    logger.warning(f"Instance {model_class.__name__} with id {instance_id} not found in database")
                    return False
            else:
                with Session(get_engine()) as new_session:
                    # Get the instance in this session
                    merged_instance = new_session.get(model_class, instance_id)
                    if merged_instance:
                        new_session.delete(merged_instance)
                        new_session.commit()
                        logger.info(f"Deleted instance with id: {instance_id}")
                        return True
                    else:
                        logger.warning(f"Instance {model_class.__name__} with id {instance_id} not found in database")
                        return False
        except Exception as e:
            logger.error(f"Error deleting instance: {e}", exc_info=True)
            raise
class InstanceNotFound(LookupError):
    """A row with that id does not exist.

    A DEDICATED type because "not found" is a legitimate, expected data condition that callers
    routinely tolerate -- but it used to be signalled with a BARE ``Exception``, which no handler
    can name. Under deny-by-default error handling (failure_modes.absorb_if_benign) an
    un-nameable signal forces the call site back to swallowing everything, which is exactly the
    blindness that let the ATR tz bug survive for months. Subclasses LookupError, and any
    existing ``except Exception`` keeps catching it, so this is backward compatible.
    """



def get_instance(model_class, instance_id, session: Session | None = None):
    """
    Retrieve a single instance by model class and primary key ID.

    Args:
        model_class: The SQLModel class to query.
        instance_id: The primary key value of the instance.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.

    Returns:
        The instance if found, otherwise None.
    """
    # Backtest sql-less store: return the stored object by id (same identity, no ORM round-trip).
    # Match the SQLite path's "raise if not found" contract (BT-only; live falls through).
    if _inmem_route(model_class):
        from ba2_common.core import trade_store as _ts
        instance = _ts.store_get(model_class, instance_id)
        if not instance:
            logger.error(f"Instance with id {instance_id}/{model_class} not found.")
            raise InstanceNotFound(f"Instance with id {instance_id}/{model_class} not found.")
        return instance
    start = time.perf_counter()
    try:
        if session:
            instance = session.get(model_class, instance_id)
            if not instance:
                logger.error(f"Instance with id {instance_id}/{model_class} not found.")
                raise InstanceNotFound(f"Instance with id {instance_id}/{model_class} not found.")
            return instance
        else:
            with Session(get_engine()) as new_session:
                instance = new_session.get(model_class, instance_id)
                if not instance:
                    logger.error(f"Instance with id {instance_id}/{model_class} not found.")
                    raise InstanceNotFound(f"Instance with id {instance_id}/{model_class} not found.")
                return instance
    except Exception as e:
        logger.error(f"Error retrieving instance: {e}", exc_info=True)
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _log_db_perf("query", f"get_instance({model_class.__name__}, {instance_id})", duration_ms)
    
def get_all_instances(model_class, session: Session | None = None):
    """
    Retrieve all instances of a given model class from the database.

    Args:
        model_class: The SQLModel class to query.
        session (Session, optional): An existing SQLModel session. If not provided, a new session is created.

    Returns:
        List of all instances of the model class.
    """
    # Backtest sql-less store: return all stored rows of this model (BT-only; live falls through).
    if _inmem_route(model_class):
        from ba2_common.core import trade_store as _ts
        return _ts.store_all(model_class)
    start = time.perf_counter()
    try:
        if session:
            statement = select(model_class)
            results = session.exec(statement)
            instances = results.all()
        else:
            with Session(get_engine()) as session:
                statement = select(model_class)
                results = session.exec(statement)
                instances = results.all()
        #logger.debug(f"Retrieved {len(instances)} instances of {model_class.__name__}")
        result_list = [i[0] for i in instances]
        return result_list
    except Exception as e:
        logger.error(f"Error retrieving all instances: {e}", exc_info=True)
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _log_db_perf("query", f"get_all_instances({model_class.__name__})", duration_ms)


def get_setting(key: str) -> str | None:
    """
    Retrieve an AppSetting value by key.

    Args:
        key: The setting key to retrieve.

    Returns:
        The value_str field of the AppSetting if found, otherwise None.
    """
    try:
        from ba2_common.core.models import AppSetting
        with Session(get_engine()) as session:
            statement = select(AppSetting).where(AppSetting.key == key)
            result = session.exec(statement).first()
            if result:
                #logger.info(f"Retrieved setting {key}: {result[0].value_str}")
                return result[0].value_str
            else:
                logger.warning(f"Setting {key} not found in database")
                return None
    except Exception as e:
        logger.error(f"Error retrieving setting {key}: {e}", exc_info=True)
        return None


def reorder_ruleset_rules(ruleset_id: int, rule_order: list[int]) -> bool:
    """
    Reorder the rules in a ruleset by updating the order_index field.
    Thread-safe: Uses a lock to prevent concurrent write conflicts.
    
    Args:
        ruleset_id: The ID of the ruleset to reorder
        rule_order: List of eventaction_ids in the desired order
        
    Returns:
        True if successful, False otherwise
    """
    with _db_write_lock:
        try:
            from ba2_common.core.models import RulesetEventActionLink
            with Session(get_engine()) as session:
                # Update each link with its new order index
                for index, eventaction_id in enumerate(rule_order):
                    # Use SQLAlchemy Core update for better performance and compatibility
                    from sqlalchemy import update
                    stmt = update(RulesetEventActionLink).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.eventaction_id == eventaction_id
                    ).values(order_index=index)
                    
                    result = session.execute(stmt)
                    if result.rowcount == 0:
                        logger.error(f"Link not found for ruleset {ruleset_id}, eventaction {eventaction_id}")
                        return False
                
                session.commit()
                logger.info(f"Reordered rules for ruleset {ruleset_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error reordering ruleset rules: {e}", exc_info=True)
            return False


def move_rule_up(ruleset_id: int, eventaction_id: int) -> bool:
    """
    Move a rule up one position in the ruleset order.
    Thread-safe: Uses a lock to prevent concurrent write conflicts.
    
    Args:
        ruleset_id: The ID of the ruleset
        eventaction_id: The ID of the eventaction to move up
        
    Returns:
        True if successful, False otherwise
    """
    with _db_write_lock:
        try:
            from ba2_common.core.models import RulesetEventActionLink
            from sqlalchemy import update
            with Session(get_engine()) as session:
                # Get the current order index (scalar to get int, not Row)
                current_order = session.exec(
                    select(RulesetEventActionLink.order_index).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.eventaction_id == eventaction_id
                    )
                ).scalar()

                if current_order is None or current_order == 0:
                    return False  # Already at top or not found

                target_order = current_order - 1

                # Get the eventaction_id that's currently at the target position
                above_ea_id = session.exec(
                    select(RulesetEventActionLink.eventaction_id).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.order_index == target_order
                    )
                ).scalar()

                if above_ea_id is not None:
                    # Swap the order indexes using SQLAlchemy Core updates
                    # Move current rule to target position
                    stmt1 = update(RulesetEventActionLink).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.eventaction_id == eventaction_id
                    ).values(order_index=target_order)
                    
                    # Move above rule to current position
                    stmt2 = update(RulesetEventActionLink).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.eventaction_id == above_ea_id
                    ).values(order_index=current_order)

                    session.execute(stmt1)
                    session.execute(stmt2)
                    session.commit()
                    logger.info(f"Moved rule {eventaction_id} up in ruleset {ruleset_id}")
                    return True

                return False

        except Exception as e:
            logger.error(f"Error moving rule up: {e}", exc_info=True)
            return False


def move_rule_down(ruleset_id: int, eventaction_id: int) -> bool:
    """
    Move a rule down one position in the ruleset order.
    Thread-safe: Uses a lock to prevent concurrent write conflicts.
    
    Args:
        ruleset_id: The ID of the ruleset
        eventaction_id: The ID of the eventaction to move down
        
    Returns:
        True if successful, False otherwise
    """
    with _db_write_lock:
        try:
            from ba2_common.core.models import RulesetEventActionLink
            from sqlalchemy import update
            with Session(get_engine()) as session:
                # Get the current order index (scalar to get int, not Row)
                current_order = session.exec(
                    select(RulesetEventActionLink.order_index).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.eventaction_id == eventaction_id
                    )
                ).scalar()

                if current_order is None:
                    return False  # Not found

                # Get the max order index for this ruleset
                max_order = session.exec(
                    select(RulesetEventActionLink.order_index).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id
                    ).order_by(RulesetEventActionLink.order_index.desc())
                ).scalar()

                if max_order is None or current_order >= max_order:
                    return False  # Already at bottom

                target_order = current_order + 1

                # Get the eventaction_id that's currently at the target position
                below_ea_id = session.exec(
                    select(RulesetEventActionLink.eventaction_id).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.order_index == target_order
                    )
                ).scalar()

                if below_ea_id is not None:
                    # Swap the order indexes using SQLAlchemy Core updates
                    # Move current rule to target position
                    stmt1 = update(RulesetEventActionLink).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.eventaction_id == eventaction_id
                    ).values(order_index=target_order)
                    
                    # Move below rule to current position
                    stmt2 = update(RulesetEventActionLink).where(
                        RulesetEventActionLink.ruleset_id == ruleset_id,
                        RulesetEventActionLink.eventaction_id == below_ea_id
                    ).values(order_index=current_order)
                    
                    session.execute(stmt1)
                    session.execute(stmt2)
                    session.commit()
                    logger.info(f"Moved rule {eventaction_id} down in ruleset {ruleset_id}")
                    return True
                
                return False
                
        except Exception as e:
            logger.error(f"Error moving rule down: {e}", exc_info=True)
            return False

def log_activity(
    severity: 'ActivityLogSeverity',
    activity_type: 'ActivityLogType', 
    description: str,
    data: dict = None,
    source_expert_id: int = None,
    source_account_id: int = None
) -> None:
    """
    Log an activity to the ActivityLog table (asynchronously).
    
    This function queues activity logs to be written asynchronously by a background worker.
    This prevents activity logging from blocking database operations during high concurrency.
    
    Args:
        severity: ActivityLogSeverity enum value
        activity_type: ActivityLogType enum value
        description: Human-readable description
        data: Optional structured data (will be stored as JSON)
        source_expert_id: Optional expert instance ID
        source_account_id: Optional account ID
        
    Returns:
        None (logging is asynchronous)
        
    Example:
        log_activity(
            ActivityLogSeverity.SUCCESS,
            ActivityLogType.TRANSACTION_CREATED,
            "Opened BUY position for AAPL",
            data={"symbol": "AAPL", "quantity": 10, "price": 150.25},
            source_expert_id=42
        )
    """
    # Backtest-scoped no-op (avoids per-bar ActivityLog write churn). Live default is on.
    if _activity_logging_disabled:
        return
    # Ensure worker thread is running
    if _activity_log_thread is None or not _activity_log_thread.is_alive():
        _start_activity_log_worker()
    
    # Queue the activity log entry for async processing
    try:
        _activity_log_queue.put(
            (severity, activity_type, description, data, source_expert_id, source_account_id),
            timeout=2.0  # Don't block if queue is full, just skip this log
        )
    except Exception as e:
        # Queue full or other error - log warning but don't block caller
        logger.debug(f"Could not queue activity log: {e}")
