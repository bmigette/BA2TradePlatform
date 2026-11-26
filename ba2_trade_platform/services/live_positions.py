"""Inputs for the Live Trades "Live View" tab: the BROKER's positions (never the platform's transactions) of every
account in the header's scope, with instrument labels. Everything that decides anything is in
``ui.utils.live_positions`` (pure); this module only reads, and runs in a worker thread.

ONE ``get_positions()`` per account per load, the accounts in parallel (``account_fanout.fan_out``), each under
``BROKER_DEADLINE_S``. No positions cache: the tab refreshes every few seconds and must show the broker's current
figures (the management API's 15 s ``_positions_cache`` is deliberately not used). An account answers:

  * ok        -- a list (``[]`` = genuinely flat);
  * failed    -- ``None`` (the adapter's "the read failed"), an exception (named by CLASS only: a broker error's text
                 can echo request data; the full error goes to the log), or an account that cannot be instantiated;
  * timed out -- no answer within the deadline. Its thread keeps running; until it ends, later loads do NOT start a
                 second read of that account (a hung Gateway polled every 5 s would otherwise pile up threads), they
                 report it timed out again.

Labels: ``Instrument.labels`` by symbol (``core.utils.get_labels_by_symbol`` / ``normalize_symbol``, as the Account
Overview positions table did). A failed label read leaves the labels empty and is reported in ``labels_error``.

TIMING: one ``[PERF:page:load] LiveView(...)`` line per load, at INFO for the tab's first load and at DEBUG for the
periodic refreshes (so a 5 s refresh does not flood the log). Two browser tabs refreshing the same scope at the same
time share ONE in-flight load."""
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..core.utils import get_account_instance_from_id, get_labels_by_symbol, normalize_symbol
from ..logger import logger
from ..ui.utils.live_positions import (STATUS_FAILED, STATUS_OK, STATUS_TIMED_OUT, AccountRead, PositionRow,
                                       position_row)
from .account_fanout import BROKER_DEADLINE_S, FAILED, OK, account_names, accounts_in_scope, fan_out
from .load_timing import LoadBreakdown, step


@dataclass
class LiveLoad:
    reads: List[AccountRead]
    #: what the load cost: positions reads started, accounts skipped (their previous read still running)
    calls: Dict[str, int] = field(default_factory=dict)
    labels_error: Optional[str] = None


class _Uninstantiable(Exception):
    pass


#: Per account, a positions read that outlived its deadline and has not ended yet.
_STILL_RUNNING: Dict[int, threading.Thread] = {}
_RUNNING_LOCK = threading.Lock()
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: Dict[Optional[int], Future] = {}


def clear_state() -> None:
    """Forget the still-running reads and in-flight loads (tests)."""
    with _RUNNING_LOCK:
        _STILL_RUNNING.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()


def _still_running(account_id: int) -> bool:
    with _RUNNING_LOCK:
        t = _STILL_RUNNING.get(account_id)
        if t is not None and not t.is_alive():
            del _STILL_RUNNING[account_id]
            t = None
        return t is not None


def _read(account_id: int):
    account = get_account_instance_from_id(account_id)
    if account is None:
        raise _Uninstantiable(f"account {account_id} cannot be instantiated")
    return account.get_positions()


def _labels(symbols: List[str]) -> Dict[str, List[str]]:
    return get_labels_by_symbol(symbols) if symbols else {}


def _load(account_id: Optional[int], timing) -> LiveLoad:
    calls = {"positions": 0, "skipped": 0}
    with step(timing, "scope") as entry:
        ids = accounts_in_scope(account_id)
        names = account_names(ids)
        entry.note = f"{len(ids)} accounts"
    skipped = [aid for aid in ids if _still_running(aid)]
    to_read = [aid for aid in ids if aid not in skipped]
    calls["positions"], calls["skipped"] = len(to_read), len(skipped)
    with step(timing, "brokers") as entry:
        outcomes, alive = fan_out(to_read, _read, BROKER_DEADLINE_S, "liveview-acct")
        with _RUNNING_LOCK:
            _STILL_RUNNING.update(alive)
        failed = sum(1 for o in outcomes.values() if o.status == FAILED or (o.status == OK and o.value is None))
        entry.note = (f"{len(ids)} accounts: {len(to_read)} read, {failed} failed, {len(alive)} timed out, "
                      f"{len(skipped)} skipped (previous read still running)")
    raw: Dict[int, list] = {aid: list(o.value) for aid, o in outcomes.items() if o.status == OK and o.value is not None}
    symbols = sorted({p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)
                      for positions in raw.values() for p in positions} - {None})
    labels_error = None
    with step(timing, "labels") as entry:
        try:
            labels = _labels([s for s in symbols if isinstance(s, str)])
        except Exception as e:  # noqa: BLE001 -- deliberate: labels are cosmetic; the error is shown, not hidden
            logger.warning(f"LiveView: instrument labels could not be read: {type(e).__name__}: {e}")
            labels, labels_error = {}, f"the instrument labels could not be read ({type(e).__name__})"
        entry.note = f"{len(symbols)} symbols"

    reads: List[AccountRead] = []
    for aid in ids:
        name = names[aid] if aid in names else f"account {aid}"
        if aid in skipped:
            reads.append(AccountRead(aid, name, STATUS_TIMED_OUT,
                                     f"its previous read has not answered yet (over {BROKER_DEADLINE_S:g}s)"))
            continue
        o = outcomes[aid]
        if o.status == OK and o.value is not None:
            rows: List[PositionRow] = []
            for p in raw[aid]:
                symbol = p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)
                row = position_row(aid, name, p, labels.get(normalize_symbol(symbol), []))
                if row is None:
                    logger.warning(f"LiveView: account {aid} returned a position without a symbol; not shown")
                    continue
                rows.append(row)
            reads.append(AccountRead(aid, name, STATUS_OK, None, tuple(rows)))
        elif o.status == OK:
            logger.warning(f"LiveView: account {aid} positions read returned nothing (the read failed)")
            reads.append(AccountRead(aid, name, STATUS_FAILED, "the positions read failed"))
        elif o.status == FAILED:
            e = o.error
            if isinstance(e, _Uninstantiable):
                why = "the account cannot be instantiated"
            else:
                why = f"the positions read failed ({type(e).__name__})"
            logger.warning(f"LiveView: account {aid}: {why}: {e}")
            reads.append(AccountRead(aid, name, STATUS_FAILED, why))
        else:
            logger.warning(f"LiveView: account {aid} did not answer within {BROKER_DEADLINE_S:g}s")
            reads.append(AccountRead(aid, name, STATUS_TIMED_OUT, f"no answer within {BROKER_DEADLINE_S:g}s"))
    return LiveLoad(reads=reads, calls=calls, labels_error=labels_error)


def load_live_positions(account_id: Optional[int], *, first_load: bool = False) -> LiveLoad:
    """The broker positions of the header's account (``None`` = all accounts). Worker thread only. A load of a scope
    that is already loading (another browser tab) waits for that one instead of asking the brokers again."""
    timing = LoadBreakdown("LiveView", f"account={account_id}")
    with _INFLIGHT_LOCK:
        running = _INFLIGHT.get(account_id)
        owner = running is None
        if owner:
            running = _INFLIGHT[account_id] = Future()
    try:
        if not owner:
            with step(timing, "shared in-flight load"):
                return running.result()
        try:
            data = _load(account_id, timing)
            running.set_result(data)
            return data
        except BaseException as e:
            running.set_exception(e)
            raise
        finally:
            with _INFLIGHT_LOCK:
                if _INFLIGHT.get(account_id) is running:
                    del _INFLIGHT[account_id]
    finally:
        line = timing.summary()
        if first_load:
            logger.info(line)
        else:
            logger.debug(line)
