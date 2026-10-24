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
import time
from datetime import datetime, timezone
from typing import Callable, Iterator, List, Sequence, Tuple

import pandas as pd

from ba2_common.core import native_cache
from ba2_common.logger import logger
from ba2_providers.ohlcv.alpaca_history import build

PROVIDER = "AlpacaHistoryOHLCVProvider"
CERTIFICATE_NAME = "gate_certificate.json"
LOCK_TIMEOUT_S = 120.0
STALE_LOCK_S = 600.0

#: (ISO date, factor, kind) with kind "split" (prices / f, volume x f) or "spinoff" (prices / f)
Event = Tuple[str, float, str]


class BasisRecordMismatch(RuntimeError):
    """A previous re-base of this symbol was interrupted: the files and the manifest may disagree. Nothing was changed."""


class LockTimeout(RuntimeError):
    pass


def cache_dir() -> str:
    return os.path.join(native_cache.CACHE_FOLDER, PROVIDER)


def _meta() -> str:
    return os.path.join(cache_dir(), "_meta")


@contextlib.contextmanager
def _file_lock(path: str, *, timeout: float = LOCK_TIMEOUT_S, stale_after: float = STALE_LOCK_S,
               poll: float = 0.05, clock: Callable[[], float] = time.time,
               sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    started = clock()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = clock() - os.path.getmtime(path)
            except FileNotFoundError:
                continue
            if age > stale_after:
                logger.warning(f"taking over stale lock {path} (untouched for {age:.0f}s)")
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)
                continue
            if clock() - started > timeout:
                raise LockTimeout(f"could not get {path} within {timeout}s")
            sleep(poll)
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat()}).encode())
    finally:
        os.close(fd)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


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
    os.replace(tmp, path)


def _marker(symbol: str) -> str:
    return os.path.join(_meta(), "locks", f"{symbol.upper()}.rebase.json")


_EVENTS_MEMO: dict = {}          # manifest path -> ((mtime_ns, size), {SYMBOL: [Event, ...]})


def _events_by_symbol() -> dict:
    """``{SYMBOL: [events]}`` of the manifest; the 8 MB file is parsed once per (mtime, size) and only the events are kept."""
    path = os.path.join(_meta(), build.MANIFEST_NAME)
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    hit = _EVENTS_MEMO.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    with open(path, encoding="utf-8") as f:
        symbols = json.load(f)["symbols"]
    out = {s: ([(d, float(x), "split") for d, x in e["events"]["splits"]]
               + [(d, float(x), "spinoff") for d, x in e["events"]["spinoffs"]])
           for s, e in symbols.items() if "events" in e}
    _EVENTS_MEMO[path] = (key, out)
    return out


def manifest_events(symbol: str) -> List[Event]:
    """The events the files of ``symbol`` are on, from the manifest (``[]`` when it has none). ``KeyError`` when the
    manifest does not list the symbol (not in the build)."""
    return list(_events_by_symbol()[symbol.upper()])


def extend_manifest(symbol: str, events: Sequence[Event], *, now: datetime) -> bool:
    """Append ``events`` to the manifest entry of ``symbol`` (atomic, ``.bak`` backup, lock) and re-stamp the gate
    certificate. ``False`` (nothing written) when every event is already recorded."""
    sym = symbol.upper()
    mpath = os.path.join(_meta(), build.MANIFEST_NAME)
    with locked_manifest():
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
        ev = manifest["symbols"][sym]["events"]
        todo = [e for e in events if [e[0], e[1]] not in [list(x) for x in ev["splits" if e[2] == "split" else "spinoffs"]]]
        if not todo:
            return False
        shutil.copyfile(mpath, mpath + ".bak")
        for d, fac, kind in todo:
            key = "splits" if kind == "split" else "spinoffs"
            ev[key] = sorted(ev[key] + [[d, fac]])
        manifest["symbols"][sym]["live"] = {"effective": max(e[0] for e in events),
                                            "extended_at": now.astimezone(timezone.utc).isoformat()}
        _atomic_json(mpath, manifest)
        cpath = os.path.join(_meta(), CERTIFICATE_NAME)
        if os.path.isfile(cpath):
            with open(cpath, encoding="utf-8") as f:
                cert = json.load(f)
            shutil.copyfile(cpath, cpath + ".bak")
            cert["build_manifest_sha256"] = _sha256(mpath)
            cert["live_extended_at"] = now.astimezone(timezone.utc).isoformat()
            _atomic_json(cpath, cert)
        return True


def rebase_symbol(symbol: str, events: Sequence[Event], *, now: datetime) -> bool:
    """Bring the files of ``symbol`` onto the basis that includes ``events`` (accepted after the last re-base).
    ``False`` = already recorded, nothing done. Raises ``BasisRecordMismatch`` after an interrupted earlier re-base."""
    sym = symbol.upper()
    with locked_symbol(sym):
        if os.path.exists(_marker(sym)):
            raise BasisRecordMismatch(
                f"{sym}: an earlier re-base was interrupted ({_marker(sym)}); files and manifest may disagree. "
                f"Restore the symbol from the build or clear the marker after checking; nothing changed.")
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
            native_cache.write_timeseries(PROVIDER, sym, interval,
                                          build.finish(build.adjust(df, splits, spinoffs), interval))
        extend_manifest(sym, todo, now=now)
        os.remove(_marker(sym))
        return True
