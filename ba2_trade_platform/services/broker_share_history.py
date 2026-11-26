"""The stored copy of an account's broker SHARE MOVEMENTS, for the Overview heatmap's "My P/L" (TastyTrade accounts:
``TastyTradeAccount.read_share_movements``), kept in the Account Growth snapshot table (``growth_snapshot``, dataset
``share_movements``).

STORAGE. Per account: every classified row WITH its broker transaction ``id``, the instant from which the store
provably holds every share movement (``covered_from``), and when it was last synced / last read in FULL.

SYNC RULE (``sync``). The first sync of an account, a store that does not reach back far enough, or a store whose
last FULL read is ``FULL_READ_EVERY_S`` old: a FULL read from ``REQUEST_LEAD_DAYS`` before the earliest reference
session the heatmap needs. Otherwise an INCREMENTAL read from the last synced New York day minus ``OVERLAP_DAYS``
(normally one small page), merged by transaction id: an id read again must be IDENTICAL to the stored row, and every
stored row inside the overlap window must be read again; otherwise the store is replaced by a full read. When the
store still provably covers and only the DAILY full re-read is due, that read gets ``DAILY_FULL_MAX_S`` of the sync's
time; if it fails it is logged and backed off on its own (``LONG_BACKOFF_S`` after the page cap or its deadline,
``BACKOFF_S`` otherwise: no full attempt before then) and the sync goes on INCREMENTALLY on the existing store, so the
tiles keep their numbers. Only without a usable store (first read, another format, an overlap mismatch) does a failed
full read fail the sync. Coverage
comes from what was PROVABLY read: a complete read requested from day D covers every movement executed from D + 2
days 00:00 UTC on (``covered_from_for``) -- whatever the endpoint's start-date means (a New York trade date, a UTC
date, a settlement date), it cannot exclude those. A read that fails, or is cut by the sync's own deadline
(``SYNC_MAX_S``), stores nothing; a save that fails is a failed sync.

A HISTORY NEWER THAN THE POSITIONS (``request_sync``). Positions reads and sync starts are ordered by one in-process
sequence (``next_seq``). A load hands in the sequence number taken right AFTER its positions read and gets a sync that
STARTED after it: the running one if it started later, else one more sync queued behind it (one queued sync per
account, shared by every load that needs it). So a history is never used with positions read after it began.

ONE SYNC AT A TIME per account, in its own thread with its own stop flag (never a load's): a load that stops waiting
leaves it running, and the result is stored for the next load. ``clear`` forgets failures, never a running sync.

FAILURE BACKOFF. A failed sync is LOGGED in its own thread and remembered per account (when, and the redacted
reason); no sync of that account starts for ``BACKOFF_S`` (``LONG_BACKOFF_S`` after the page cap or the sync's own
deadline), and every load meanwhile gets the reason."""
import itertools
import threading
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ba2_common.core.market_calendar import NY_TZ

from ..logger import logger
from ..ui.utils.position_heatmap import BrokerHistory, ShareMove
from . import growth_snapshot
from .growth_snapshot import StoredShareMovements

#: A full read starts this many days before the earliest reference session (never rely on the date filter at the edge).
REQUEST_LEAD_DAYS = 3
#: An incremental read re-reads this many days already stored (back-dated bookings, a changed row).
OVERLAP_DAYS = 7
#: A full re-read at most this often per account.
FULL_READ_EVERY_S = 24 * 3600.0
#: A sync's own deadline (its stop flag); a sync cut by it stores nothing.
SYNC_MAX_S = 120.0
#: The DAILY full re-read's share of a sync's time (the rest is left for the incremental read it falls back to).
DAILY_FULL_MAX_S = 90.0
#: How long after its positions read a load waits for the history (never longer than its tile inputs take).
WAIT_S = 20.0
#: After a failed sync, no new sync of that account for this long ...
BACKOFF_S = 300.0
#: ... or this long when the history did not end within the page cap or the sync hit its own deadline.
LONG_BACKOFF_S = 1800.0

STILL_LOADING = "broker history still loading, reload"


def canonical_symbol(raw) -> str:
    """ONE spelling per instrument: the repo's canonicaliser (``ba2_providers.screener.symbol_universe``: ``BRK.B`` ->
    ``BRK-B``); TastyTrade's slash share class (``BRK/B``) is read as the dot first."""
    from ba2_providers.screener.symbol_universe import canonical_symbol as screener_canonical
    return screener_canonical(raw.replace("/", ".")) if isinstance(raw, str) else ""


def covered_from_for(requested: date) -> datetime:
    """The instant from which a COMPLETE read requested from ``requested`` provably holds every movement."""
    return datetime(requested.year, requested.month, requested.day, tzinfo=timezone.utc) + timedelta(days=2)


def mask_account_numbers(text: str, number: Optional[str] = None) -> str:
    """``/accounts/<number>/...`` -> ``/accounts/***/...``, and the literal ``number`` wherever it appears (an SDK /
    HTTP error can carry the account number)."""
    import re
    if isinstance(number, str) and number.strip():
        text = text.replace(number.strip(), "***")
    return re.sub(r"(/accounts/)[^/\s?]+", r"\1***", text)


def redacted(text: str, account=None) -> str:
    """Keys / tokens (``price_history.redact_message``) and account numbers removed."""
    from .price_history import redact_message
    number = getattr(getattr(account, "_account", None), "account_number", None)
    return mask_account_numbers(redact_message(text), number if isinstance(number, str) else None)


def _ny_day(moment: datetime) -> date:
    return moment.astimezone(NY_TZ).date()


# --------------------------------------------------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------------------------------------------------

def load_store(account_id: int) -> Optional[StoredShareMovements]:
    """Never raises (``growth_snapshot`` logs an unreadable row)."""
    return growth_snapshot.load_share_movements(account_id)


def _sorted(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    far = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(rows, key=lambda r: (r["executed_at"] or far, r["id"] if isinstance(r["id"], int) else -1))


def _overlap_consistent(stored: List[Dict], read: List[Dict], window_from: datetime) -> bool:
    """Every row read again is identical to the stored one, and every stored row inside the window was read again."""
    stored_by_id = {r["id"]: r for r in stored}
    read_ids = set()
    for r in read:
        read_ids.add(r["id"])
        if r["id"] in stored_by_id and stored_by_id[r["id"]] != r:
            return False
    return all(r["id"] in read_ids for r in stored if r["executed_at"] is not None and r["executed_at"] >= window_from)


class _EitherStop:
    """Set when either flag is: the sync's own stop, or the daily full read's shorter deadline."""

    def __init__(self, *events):
        self._events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)

    def wait(self, timeout: Optional[float] = None) -> bool:
        import time as _time
        ends = None if timeout is None else _time.monotonic() + timeout
        while not self.is_set():
            if ends is not None and _time.monotonic() >= ends:
                return False
            _time.sleep(0.02)
        return True


_FULL_LOCK = threading.Lock()
#: per account: (no daily full re-read before, the redacted reason it last failed)
_FULL_BACKOFF: Dict[int, Tuple[datetime, str]] = {}


def _daily_full_read(account_id: int, account, full_start: date, now: datetime,
                     stop: Optional[threading.Event]) -> Optional[List[Dict]]:
    """The DAILY full re-read's rows, or None when it is backing off or fails (logged, backed off)."""
    with _FULL_LOCK:
        backoff = _FULL_BACKOFF.get(account_id)
    if backoff is not None and now < backoff[0]:
        return None
    deadline = threading.Event()
    timer = threading.Timer(DAILY_FULL_MAX_S, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        rows = account.read_share_movements(full_start, stop=_EitherStop(stop, deadline))["rows"]
    except Exception as e:  # noqa: BLE001 -- the existing store still covers: fall back to the incremental read
        if stop is not None and stop.is_set():
            raise                                          # the sync's own deadline: nothing is left for a fallback
        kind = getattr(e, "heatmap_kind", None)
        if deadline.is_set() or kind == "stopped":
            why, seconds = f"did not finish within {DAILY_FULL_MAX_S:.0f}s", LONG_BACKOFF_S
        else:
            why = redacted(f"{type(e).__name__}: {e}", account)
            seconds = LONG_BACKOFF_S if kind == "page_cap" else BACKOFF_S
        until = now + timedelta(seconds=seconds)
        with _FULL_LOCK:
            _FULL_BACKOFF[account_id] = (until, why)
        logger.warning(f"Heatmap: account {account_id}: the daily full re-read of the broker history failed ({why}); "
                       f"syncing incrementally on the stored copy, no full re-read before {until:%H:%M:%S} UTC")
        return None
    finally:
        timer.cancel()
    with _FULL_LOCK:
        _FULL_BACKOFF.pop(account_id, None)
    return rows


def sync(account_id: int, account, first_day: date, *, now: Optional[datetime] = None,
         stop: Optional[threading.Event] = None) -> StoredShareMovements:
    """Bring the account's store up to date (see the module docstring) and store it. RAISES when the broker read fails
    or is incomplete without a usable store to fall back on, or the store cannot be saved (nothing is stored then).
    Callers other than tests go through ``request_sync`` (one sync at a time per account)."""
    now = now or datetime.now(timezone.utc)
    current = load_store(account_id)
    full_start = first_day - timedelta(days=REQUEST_LEAD_DAYS)
    usable = current is not None and current.covered_from <= covered_from_for(full_start)
    if usable and (now - current.full_read_at).total_seconds() >= FULL_READ_EVERY_S:
        rows = _daily_full_read(account_id, account, full_start, now, stop)
        if rows is not None:
            store = StoredShareMovements(rows=_sorted(rows), covered_from=covered_from_for(full_start),
                                         synced_at=now, full_read_at=now)
            growth_snapshot.save_share_movements(account_id, store)
            return store
    store = None
    if usable:
        start = _ny_day(current.synced_at) - timedelta(days=OVERLAP_DAYS)
        read = account.read_share_movements(start, stop=stop)["rows"]
        if _overlap_consistent(current.rows, read, covered_from_for(start)):
            known = {r["id"] for r in current.rows}
            store = replace(current, rows=_sorted(list(current.rows) + [r for r in read if r["id"] not in known]),
                            synced_at=now)
        else:
            logger.warning(f"Heatmap: account {account_id}'s stored share movements disagree with the broker in the "
                           f"overlap window; replacing them with a full read")
    if store is None:
        with _FULL_LOCK:
            backoff = _FULL_BACKOFF.get(account_id)
        if usable and backoff is not None and now < backoff[0]:
            raise ValueError(f"the stored broker history disagrees with the broker and its full re-read failed "
                             f"({backoff[1]}); next full re-read after {backoff[0]:%H:%M} UTC")
        read = account.read_share_movements(full_start, stop=stop)["rows"]
        store = StoredShareMovements(rows=_sorted(read), covered_from=covered_from_for(full_start), synced_at=now,
                                     full_read_at=now)
    growth_snapshot.save_share_movements(account_id, store)
    return store


# --------------------------------------------------------------------------------------------------------------------
# one sync at a time, newer than the positions, with a failure backoff
# --------------------------------------------------------------------------------------------------------------------

class SyncFailed(Exception):
    """A sync's failure (or an active backoff), its message already redacted: what the tiles show."""


@dataclass
class _Account:
    running: Optional[Future] = None
    running_seq: int = 0
    queued: Optional[Future] = None
    queued_with: Optional[Tuple[Any, date]] = None
    failure: Optional[Tuple[datetime, str]] = None       # (no sync before, the redacted reason)


_LOCK = threading.Lock()
_STATE: Dict[int, _Account] = {}
_SEQ = itertools.count(1)


def next_seq() -> int:
    """The next number of the one sequence that orders positions reads and sync starts (thread-safe)."""
    with _LOCK:
        return next(_SEQ)


def clear() -> None:
    """Forget remembered failures (a reload may retry at once). A running or queued sync is kept: it is still the
    account's one sync, and a second one must not start beside it. The store itself is in the database."""
    with _LOCK:
        for state in _STATE.values():
            state.failure = None
    with _FULL_LOCK:
        _FULL_BACKOFF.clear()


def failure_of(account_id: int) -> Optional[Tuple[datetime, str]]:
    """``(no sync before, reason)`` while the account is backing off, else None."""
    with _LOCK:
        state = _STATE.get(account_id)
        if state is None or state.failure is None or state.failure[0] <= datetime.now(timezone.utc):
            return None
        return state.failure


def _backing_off(state: _Account) -> Optional[Future]:
    if state.failure is None:
        return None
    until, reason = state.failure
    if until <= datetime.now(timezone.utc):
        state.failure = None
        return None
    done: Future = Future()
    done.set_exception(SyncFailed(f"{reason}; retrying after {until:%H:%M} UTC"))
    return done


def request_sync(account_id: int, account, first_day: date, positions_seq: int) -> Future:
    """A sync of the account that STARTED after ``positions_seq`` (``next_seq`` taken after the load's positions
    read): the running one if it did, else the one queued behind it (created if needed). During a backoff: an
    already failed future carrying the reason. The future's result is the store; its exception a ``SyncFailed``."""
    with _LOCK:
        state = _STATE.setdefault(account_id, _Account())
        if state.running is not None:
            if state.running_seq > positions_seq:
                return state.running
            if state.queued is None:
                state.queued = Future()
            state.queued_with = (account, first_day)        # the newest caller's objects
            return state.queued
        waiting = _backing_off(state)
        if waiting is not None:
            return waiting
        future: Future = Future()
        _start(account_id, state, future, account, first_day)
        return future


def _start(account_id: int, state: _Account, future: Future, account, first_day: date) -> None:
    """Under ``_LOCK``: make ``future`` the account's running sync and start its thread."""
    state.running, state.running_seq = future, next(_SEQ)
    threading.Thread(target=_run, args=(account_id, future, account, first_day), daemon=True,
                     name=f"share-history-{account_id}").start()


def _reason(error: BaseException, stopped: bool, account) -> Tuple[str, float]:
    """The redacted reason a sync failed, and how long to back off."""
    kind = getattr(error, "heatmap_kind", None)
    if stopped or kind == "stopped":
        return f"the broker history sync did not finish within {SYNC_MAX_S:.0f}s", LONG_BACKOFF_S
    detail = redacted(f"{type(error).__name__}: {error}", account)
    if kind == "page_cap":
        return f"the broker history is too long to read ({detail})", LONG_BACKOFF_S
    if kind == "page_timeout":
        return f"a broker history page did not answer within its time limit ({detail})", BACKOFF_S
    return f"the broker history read failed ({detail})", BACKOFF_S


def _run(account_id: int, future: Future, account, first_day: date) -> None:
    stop = threading.Event()
    timer = threading.Timer(SYNC_MAX_S, stop.set)
    timer.daemon = True
    timer.start()
    failure = None
    try:
        store = sync(account_id, account, first_day, stop=stop)
    except BaseException as e:  # noqa: BLE001 -- logged and remembered here, handed to every waiter
        why, backoff = _reason(e, stop.is_set(), account)
        until = datetime.now(timezone.utc) + timedelta(seconds=backoff)
        logger.warning(f"Heatmap: account {account_id}: {why}; no new sync before {until:%H:%M:%S} UTC")
        failure = (until, why)
    finally:
        timer.cancel()
    with _LOCK:
        state = _STATE.setdefault(account_id, _Account())
        if state.running is future:
            state.running = None
        queued, queued_with = state.queued, state.queued_with
        state.queued = state.queued_with = None
        if failure is not None:
            state.failure = failure
        elif queued is not None:
            _start(account_id, state, queued, *queued_with)
            queued = None
    if failure is not None:
        future.set_exception(SyncFailed(failure[1]))
        if queued is not None:
            queued.set_exception(SyncFailed(f"{failure[1]}; retrying after {failure[0]:%H:%M} UTC"))
    else:
        future.set_result(store)


def wait_for_store(future: Future, timeout: float) -> Tuple[Optional[StoredShareMovements], Optional[str]]:
    """``(store, None)``, or ``(None, why)``: still running after ``timeout`` (it keeps running and stores its
    result), or failed / backing off (the reason, already redacted)."""
    try:
        return future.result(timeout=max(0.0, timeout)), None
    except BaseException:  # noqa: BLE001 -- sorted out below (concurrent.futures.TimeoutError IS TimeoutError)
        pass
    if not future.done():
        return None, STILL_LOADING
    error = future.exception()
    if isinstance(error, SyncFailed):
        return None, str(error)
    return None, redacted(f"the broker history read failed ({type(error).__name__}: {error})")


# --------------------------------------------------------------------------------------------------------------------
# for the pure module
# --------------------------------------------------------------------------------------------------------------------

def history_from_store(store: StoredShareMovements, positions_at: Optional[datetime]) -> BrokerHistory:
    moves = tuple(ShareMove(symbol=canonical_symbol(r["symbol"]) or (r["symbol"] or ""), quantity=r["quantity"],
                            price=r["price"], executed_at=r["executed_at"], kind=r["kind"], detail=r["detail"],
                            id=r["id"], underlying=canonical_symbol(r["underlying"]) or None,
                            reverses_id=r["reverses_id"])
                  for r in store.rows)
    return BrokerHistory(moves=moves, covers_from=store.covered_from, positions_at=positions_at)
