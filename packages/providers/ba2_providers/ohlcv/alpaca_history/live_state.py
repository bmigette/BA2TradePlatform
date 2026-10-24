"""Live basis maintenance of the Alpaca history cache (plan 2026-10-24-history-source-task11, Task 4).

Owner decisions 2026-10-24: the BASIS of a symbol's files is recorded in ``_meta/build_manifest.json`` itself
(``symbols[SYM].events`` + ``symbols[SYM].live.effective``); there is no separate live-state file.

* ``locked_symbol`` / ``locked_manifest``: cross-process lock files (``O_CREAT|O_EXCL`` under ``_meta/locks``, pid + time
  inside, stale takeover with a WARNING). Writers of one symbol (top-up, re-base) hold the symbol lock; readers take NO
  lock: every file is replaced atomically (``native_cache.write_timeseries`` / ``os.replace``), so a reader sees the old
  or the new complete file.
* ``rebase_symbol``: a corporate action accepted after the build divides the existing rows (``build.adjust`` with just
  the new events), daily file, then 5-minute file, then the manifest. An intent marker (``locks/<SYM>.rebase.json``)
  is written first and removed last: a crash in between leaves the marker and the NEXT call refuses
  (``BasisRecordMismatch``) instead of dividing again. Re-basing an event the manifest already holds is a no-op.
* ``extend_manifest``: appends the event to the manifest atomically with a ``.bak`` backup, then re-stamps the gate
  certificate's ``build_manifest_sha256`` (the PASS certificate pins the manifest's hash; without the re-stamp the
  option split-basis check would refuse the whole cache), also with a backup.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

import pandas as pd

from ba2_common.core import native_cache
from ba2_common.core.ohlcv_history import HistoryUnavailable
from ba2_common.logger import logger
from ba2_providers.ohlcv.alpaca_history import build

PROVIDER = "AlpacaHistoryOHLCVProvider"
CERTIFICATE_NAME = "gate_certificate.json"
LOCK_TIMEOUT_S = 120.0
STALE_LOCK_S = 600.0

#: (ISO date, factor, kind) with kind "split" (prices / f, volume x f) or "spinoff" (prices / f)
Event = Tuple[str, float, str]


class BasisRecordMismatch(HistoryUnavailable):
    """A previous re-base of this symbol was interrupted: the files and the manifest may disagree. Nothing was changed."""


class LockTimeout(HistoryUnavailable):
    pass


def cache_dir() -> str:
    return os.path.join(native_cache.CACHE_FOLDER, PROVIDER)


def _meta() -> str:
    return os.path.join(cache_dir(), "_meta")


def replace_with_retry(src: str, dst: str, *, attempts: int = 8, sleep: Callable[[float], None] = time.sleep) -> None:
    """``os.replace`` that survives a Windows sharing violation (a reader holding the target open): bounded retry with
    backoff, then the PermissionError is raised -- the caller's write fails loudly and the next read retries."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            sleep(0.05 * (2 ** i))


def write_timeseries_with_retry(provider: str, symbol: str, interval: str, frame: pd.DataFrame, *,
                                attempts: int = 8, sleep: Callable[[float], None] = time.sleep) -> None:
    """``native_cache.write_timeseries`` retried on a Windows sharing violation (its ``os.replace`` onto a file a reader has open)."""
    for i in range(attempts):
        try:
            native_cache.write_timeseries(provider, symbol, interval, frame)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            sleep(0.05 * (2 ** i))


def _read_token(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["token"]
    except (OSError, ValueError, KeyError):
        return None


@contextlib.contextmanager
def _file_lock(path: str, *, timeout: float = LOCK_TIMEOUT_S, stale_after: float = STALE_LOCK_S,
               poll: float = 0.05, clock: Callable[[], float] = time.time,
               sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    """Cross-process lock file. The file holds a random TOKEN: only its owner removes it, a stale lock is taken over by an
    ATOMIC rename to a unique name (exactly one taker wins), a heartbeat keeps the mtime fresh while a slow holder works, and
    a Windows PermissionError on creation (delete-pending file) is just "busy"."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    token = uuid.uuid4().hex
    started = clock()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            try:
                age = clock() - os.path.getmtime(path)
            except (FileNotFoundError, PermissionError):
                age = 0.0
            if age > stale_after:
                grave = f"{path}.stale.{token}"
                try:
                    os.rename(path, grave)                      # atomic: of several takers exactly one succeeds
                except OSError:
                    continue                                   # somebody else took it (or it was released): look again
                logger.warning(f"taking over stale lock {path} (untouched for {age:.0f}s)")
                with contextlib.suppress(OSError):
                    os.remove(grave)
                continue
            if clock() - started > timeout:
                raise LockTimeout(f"could not get {path} within {timeout}s")
            sleep(poll)
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "token": token,
                                 "at": datetime.now(timezone.utc).isoformat()}).encode())
    finally:
        os.close(fd)
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(max(0.05, min(stale_after / 4.0, 30.0))):
            if _read_token(path) == token:
                with contextlib.suppress(OSError):
                    os.utime(path)
    heart = threading.Thread(target=beat, name="ba2-lock-heartbeat", daemon=True)
    heart.start()
    try:
        yield
    finally:
        stop.set()
        heart.join(timeout=2.0)
        if _read_token(path) == token:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
        else:
            logger.error(f"lock {path} is no longer ours (taken over as stale while held); left untouched")


def locked_symbol(symbol: str, **kw):
    """Hold the cross-process write lock of one symbol (both intervals)."""
    return _file_lock(os.path.join(_meta(), "locks", f"{symbol.upper()}.lock"), **kw)


def locked_manifest(**kw):
    return _file_lock(os.path.join(_meta(), "locks", "_manifest.lock"), **kw)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    replace_with_retry(tmp, path)


def _marker(symbol: str) -> str:
    return os.path.join(_meta(), "locks", f"{symbol.upper()}.rebase.json")


_EVENTS_MEMO: dict = {}          # manifest path -> ((mtime_ns, size), view)


def _manifest_view() -> dict:
    """``{"store_asof", "events": {SYMBOL: [Event, ...]}, "effective": {SYMBOL: iso date}}`` of the manifest; the 8 MB file is
    parsed once per (mtime, size) and only these parts are kept."""
    path = os.path.join(_meta(), build.MANIFEST_NAME)
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    hit = _EVENTS_MEMO.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    symbols = manifest["symbols"]
    def _earliest(interval: str):
        firsts = [e[interval]["first"] for e in symbols.values() if interval in e and "first" in e[interval]]
        return min(firsts) if firsts else None
    view = {"store_asof": manifest["store_asof"] if "store_asof" in manifest else None,
            "first": {"1d": _earliest("1d"), "5min": _earliest("5min")},
            "events": {s: ([(d, float(x), "split") for d, x in e["events"]["splits"]]
                           + [(d, float(x), "spinoff") for d, x in e["events"]["spinoffs"]])
                       for s, e in symbols.items() if "events" in e},
            "effective": {s: e["live"]["effective"] for s, e in symbols.items() if "live" in e},
            "partial": {s: {iv: dict(e[iv]) for iv in ("1d", "5min") if iv in e and "partial" in e[iv] and e[iv]["partial"]}
                        for s, e in symbols.items()
                        if any(iv in e and "partial" in e[iv] and e[iv]["partial"] for iv in ("1d", "5min"))}}
    _EVENTS_MEMO[path] = (key, view)
    return view


def _events_by_symbol() -> dict:
    return _manifest_view()["events"]


def store_asof() -> str:
    """The build's end date (``YYYY-MM-DD``); ``KeyError`` when the manifest records none."""
    v = _manifest_view()["store_asof"]
    if v is None:
        raise KeyError("store_asof")
    return v


def build_history_start(interval: str) -> str:
    """The earliest first bar of any built symbol for ``interval`` ('1d' | '5min'): the build's own history start."""
    v = _manifest_view()["first"][interval]
    if v is None:
        raise KeyError(f"the manifest records no first bar for {interval}")
    return v


def partial_coverage(symbol: str, interval: str) -> Optional[dict]:
    """The stamp ``{"first", "last", "bars", "covered_from", "partial": True}`` of an interval that was backfilled live for
    only the range a caller asked for; None for a fully built interval, a symbol or manifest the cache does not have."""
    try:
        view = _manifest_view()
    except FileNotFoundError:
        return None
    sym = view["partial"][symbol.upper()] if symbol.upper() in view["partial"] else {}
    return sym[interval] if interval in sym else None


def manifest_events(symbol: str) -> List[Event]:
    """The events the files of ``symbol`` are on, from the manifest (``[]`` when it has none). ``KeyError`` when the
    manifest does not list the symbol (not in the build)."""
    return list(_events_by_symbol()[symbol.upper()])


def _extend_split_table(sym: str, todo: Sequence[Event], manifest: dict) -> None:
    """Add ``todo`` to ``split_table.json`` (atomic, ``.bak``) and record the new file hash in the manifest's
    ``split_table`` block: the option split-basis check compares the table's events and hash with the manifest's."""
    tpath = os.path.join(_meta(), build.SPLIT_TABLE_NAME)
    with open(tpath, encoding="utf-8") as f:
        table = json.load(f)
    shutil.copyfile(tpath, tpath + ".bak")
    for d, fac, kind in todo:
        bucket = table["splits" if kind == "split" else "spinoffs"]
        rows = bucket[sym] if sym in bucket else []
        if [d, fac] not in [list(x) for x in rows]:
            bucket[sym] = sorted(rows + [[d, fac]])
    _atomic_json(tpath, table)
    manifest["split_table"]["sha256"] = _sha256(tpath)


def _restamp_certificate(mpath: str, now: datetime) -> bool:
    """Make the gate certificate pin the CURRENT manifest hash (backup first). True when it had to change."""
    cpath = os.path.join(_meta(), CERTIFICATE_NAME)
    if not os.path.isfile(cpath):
        return False
    with open(cpath, encoding="utf-8") as f:
        cert = json.load(f)
    actual = _sha256(mpath)
    if cert["build_manifest_sha256"] == actual:
        return False
    shutil.copyfile(cpath, cpath + ".bak")
    cert["build_manifest_sha256"] = actual
    cert["live_extended_at"] = now.astimezone(timezone.utc).isoformat()
    _atomic_json(cpath, cert)
    return True


def _repair_locked(manifest: dict, mpath: str, now: datetime) -> bool:
    """IDEMPOTENT consistency repair (manifest lock held): the manifest must record the real hash of ``split_table.json`` and the
    certificate must pin the real hash of the manifest. A crash between the table, manifest and certificate writes leaves one of
    them behind; every call fixes it, also when there is nothing else to do."""
    changed = False
    tpath = os.path.join(_meta(), build.SPLIT_TABLE_NAME)
    if os.path.isfile(tpath) and "split_table" in manifest:
        actual = _sha256(tpath)
        if manifest["split_table"]["sha256"] != actual:
            logger.warning("repairing the manifest's split_table hash (an interrupted live update left it behind)")
            shutil.copyfile(mpath, mpath + ".bak")
            manifest["split_table"]["sha256"] = actual
            _atomic_json(mpath, manifest)
            changed = True
    if _restamp_certificate(mpath, now):
        logger.warning("repairing the gate certificate (an interrupted live update left it behind)")
        changed = True
    return changed


def repair_consistency(now: datetime) -> bool:
    """Run the idempotent repair under the manifest lock (the daily refresh calls it first)."""
    mpath = os.path.join(_meta(), build.MANIFEST_NAME)
    with locked_manifest():
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
        return _repair_locked(manifest, mpath, now)


def extend_manifest(symbol: str, events: Sequence[Event], *, now: datetime, sync_split_table: bool = False) -> bool:
    """Append ``events`` to the manifest entry of ``symbol`` (atomic, ``.bak`` backup, lock) and re-stamp the gate
    certificate. ``sync_split_table`` adds the same events to ``split_table.json`` and the manifest's hash of it, in the
    same locked section. ``False`` (nothing written) when every event is already recorded."""
    sym = symbol.upper()
    mpath = os.path.join(_meta(), build.MANIFEST_NAME)
    with locked_manifest():
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
        ev = manifest["symbols"][sym]["events"]
        todo = [e for e in events if [e[0], e[1]] not in [list(x) for x in ev["splits" if e[2] == "split" else "spinoffs"]]]
        if not todo:
            _repair_locked(manifest, mpath, now)
            return False
        shutil.copyfile(mpath, mpath + ".bak")
        if sync_split_table:
            _extend_split_table(sym, todo, manifest)
        for d, fac, kind in todo:
            key = "splits" if kind == "split" else "spinoffs"
            ev[key] = sorted(ev[key] + [[d, fac]])
        manifest["symbols"][sym]["live"] = {"effective": max(e[0] for e in events),
                                            "extended_at": now.astimezone(timezone.utc).isoformat()}
        _atomic_json(mpath, manifest)
        _restamp_certificate(mpath, now)
        return True


def assert_no_interrupted_rebase(symbol: str) -> None:
    """``BasisRecordMismatch`` while an interrupted re-base of ``symbol`` left its intent marker: the files and the manifest may
    disagree, so nothing may read-modify-write this symbol (top-up, backfill, review check, another re-base)."""
    if os.path.exists(_marker(symbol)):
        raise BasisRecordMismatch(
            f"{symbol.upper()}: an earlier re-base was interrupted ({_marker(symbol)}); files and manifest may disagree. "
            f"Restore the symbol from the build or clear the marker after checking; nothing changed.")


def rebase_symbol(symbol: str, events: Sequence[Event], *, now: datetime, sync_split_table: bool = False) -> bool:
    """Bring the files of ``symbol`` onto the basis that includes ``events`` (accepted after the last re-base).
    ``False`` = already recorded, nothing done. Raises ``BasisRecordMismatch`` after an interrupted earlier re-base."""
    sym = symbol.upper()
    with locked_symbol(sym):
        assert_no_interrupted_rebase(sym)
        recorded = {(d, round(f, 12), k) for d, f, k in manifest_events(sym)}
        todo = [e for e in events if (e[0], round(e[1], 12), e[2]) not in recorded]
        if not todo:
            return False
        os.makedirs(os.path.dirname(_marker(sym)), exist_ok=True)
        with open(_marker(sym), "w", encoding="utf-8") as f:
            json.dump({"symbol": sym, "events": [list(e) for e in todo], "at": now.isoformat()}, f)
        splits = [(d, f) for d, f, k in todo if k == "split"]
        spinoffs = [(d, f) for d, f, k in todo if k == "spinoff"]
        for interval in ("1d", "5min"):
            path = native_cache.find_timeseries_path(PROVIDER, sym, interval)
            if path is None:
                continue
            df = pd.read_parquet(path)
            write_timeseries_with_retry(PROVIDER, sym, interval,
                                        build.finish(build.adjust(df, splits, spinoffs), interval))
        extend_manifest(sym, todo, now=now, sync_split_table=sync_split_table)
        os.remove(_marker(sym))
        return True


def register_backfilled_symbol(symbol: str, interval: str, events: Sequence[Event], first: str, last: str, bars: int, *,
                               now: datetime, file_name: str, covered_from: str, origin: str = "live_backfill",
                               stamp_update: bool = True) -> bool:
    """Record a symbol (or one more interval of it) that was backfilled from Alpaca after the build: manifest entry
    (events, per-interval first / last / bars, origin marker + date), the same events in ``split_table.json`` and its hash,
    and the gate certificate re-stamp -- ONE locked section, ``.bak`` backups, atomic replaces (the transaction of
    ``extend_manifest``). Idempotent: a repeat adds nothing new and returns False, and still repairs a stale split-table hash /
    certificate. ``stamp_update=False`` records only the events (the backward extension records its events BEFORE writing the
    file and moves the coverage claim only AFTER, so a crash never claims coverage the file lacks). Call BEFORE the bar file is written:
    an entry without a file is simply backfilled again, a file without an entry would be refused as 'not in the build'."""
    sym = symbol.upper()
    mpath = os.path.join(_meta(), build.MANIFEST_NAME)
    with locked_manifest():
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
        entry = manifest["symbols"][sym] if sym in manifest["symbols"] else None
        new_entry = entry is None
        if new_entry:
            entry = {"events": {"splits": [], "spinoffs": []}, "files": {}, "origin": origin,
                     "backfilled_on": now.astimezone(timezone.utc).date().isoformat()}
        have = {(k, d, round(float(x), 12)) for k, rows in entry["events"].items() for d, x in rows}
        todo = [e for e in events if ("splits" if e[2] == "split" else "spinoffs", e[0], round(e[1], 12)) not in have]
        stamp = {"first": first, "last": last, "bars": bars, "covered_from": covered_from, "partial": True}
        if not new_entry and not todo and (not stamp_update or (interval in entry and entry[interval] == stamp)):
            _repair_locked(manifest, mpath, now)
            return False
        shutil.copyfile(mpath, mpath + ".bak")
        if todo:
            _extend_split_table(sym, todo, manifest)
            for d, fac, kind in todo:
                key = "splits" if kind == "split" else "spinoffs"
                entry["events"][key] = sorted(entry["events"][key] + [[d, fac]])
            entry["live"] = {"effective": max(e[0] for e in todo), "extended_at": now.astimezone(timezone.utc).isoformat()}
        if stamp_update:
            entry[interval] = stamp
            entry.setdefault("files", {})[interval] = file_name
        manifest["symbols"][sym] = entry
        _atomic_json(mpath, manifest)
        _restamp_certificate(mpath, now)
        return True
