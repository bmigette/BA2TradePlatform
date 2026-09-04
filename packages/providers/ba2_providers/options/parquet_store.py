"""Resumable on-disk parquet store for historical option bars.

WHY PARQUET, NOT SQLITE. The incumbent options cache is a single ~10 GB sqlite file, and the
GA fans a backtest out over worker processes that all read it at once — a shape sqlite
handles badly. The platform's cache tier is already 33,425 parquet files against 2 sqlite, so
parquet is also the house style. This store writes to a NEW path and never reads, migrates or
touches the incumbent: the warm-up rebuilds a superset of it for the window.

LAYOUT — deliberately the existing conventions, not a new scheme::

    <CACHE_FOLDER>/TastyTradeOptionsProvider/          <- same shape as the OHLCV cache's
      AAPL/                                               AlpacaOHLCVProvider/, FMPOHLCVProvider/
        exp=2023-01-20/                                <- hive partition, same spelling as the
          AAPL_2023-01-20_1d.parquet                      screener metric store's ym=YYYY-MM
          _manifest.json
        exp=2023-01-27/
          ...

One partition per (underlying, expiry) — that is also the download's UNIT OF WORK, so an
interrupt loses at most one expiry's chain.

RESUME. The manifest is the completion marker and is written LAST, atomically, after the
parquet. That ordering is the whole contract:

    no manifest            -> MISSING   (never fetched, or killed mid-fetch) -> (re)fetch
    manifest + parquet     -> COMPLETE  (rows on disk)                       -> skip
    manifest, status empty -> EMPTY     (genuinely no bars — a recorded FACT) -> skip
    manifest, narrower window / newer schema -> STALE                        -> refetch

EMPTY is a first-class state, not a synonym for MISSING: a strike that never traded must be
recorded as such or the warm-up re-requests every dead contract on every run, forever.

The window actually fetched is recorded and re-checked. That is the screener metric store's
bug, ported forward: a cache keyed on symbol alone silently served a SHORTER range once a
later build widened ``start``.

Every write is temp-file + ``os.replace`` (atomic on POSIX and on Windows for a same-volume
rename), with a pid+thread-unique temp name so two concurrent builders cannot clobber each
other's half-written file.
"""
from __future__ import annotations

import glob
import json
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
from ba2_common.logger import logger

#: Sub-directory of CACHE_FOLDER. Named for the provider class, exactly like the OHLCV
#: cache's AlpacaOHLCVProvider/ and FMPOHLCVProvider/ directories.
PROVIDER_DIR = "TastyTradeOptionsProvider"

BARS_INTERVAL = "1d"          # dxfeed serves no intraday data for dead contracts
MANIFEST_NAME = "_manifest.json"


class PartitionState(str, Enum):
    """What a re-run must do with a partition."""
    MISSING = "missing"     # never fetched, or a fetch that did not finish -> fetch it
    COMPLETE = "complete"   # bars on disk for a window covering the request -> skip
    EMPTY = "empty"         # fetched and genuinely had no bars -> skip, do NOT retry
    STALE = "stale"         # fetched over a narrower window / older schema -> refetch


@dataclass(frozen=True)
class StoreProgress:
    """Counts for the warm-up's progress line."""
    total: int
    complete: int
    empty: int
    pending: int


def _default_root() -> str:
    # Imported at CALL time, never bound at import: the providers conftest rebinds
    # ba2_common.config.CACHE_FOLDER to a temp dir, and an import-time capture would send
    # every test write into the real ~/Documents cache tree.
    import ba2_common.config as _cfg
    return os.path.join(_cfg.CACHE_FOLDER, PROVIDER_DIR)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _tmp_name(path: str) -> str:
    return f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"


class OptionHistoryParquetStore:
    """Partitioned parquet store keyed on (underlying, expiry)."""

    SCHEMA_VERSION = 1

    #: The columns every partition carries. ``iv`` and ``open_interest`` are the entire
    #: reason this pipeline exists and are declared even when wholly null, so a concat
    #: across partitions never loses them. ``bid``/``ask`` (added 2026-09-03): TastyTrade
    #: never has them (dxfeed serves no historical NBBO for dead contracts, hence always
    #: None here), but ThetaData's EOD report does — OptionEodBar.bid/ask carried the
    #: values all along, this store just dropped them on the floor at write time. Declared
    #: even when wholly null for the same concat-safety reason as iv/open_interest.
    COLUMNS: Sequence[str] = (
        "occ_symbol", "underlying", "option_type", "strike", "expiry", "bar_date",
        "open", "high", "low", "close", "volume", "bid", "ask", "iv", "open_interest",
    )

    def __init__(self, root: Optional[str] = None,
                 clock: Optional[Callable[[], datetime]] = None):
        self._root = root
        self._clock = clock or _utcnow

    # -- layout ---------------------------------------------------------
    @property
    def root(self) -> str:
        return self._root if self._root is not None else _default_root()

    def partition_dir(self, underlying: str, expiry: date) -> str:
        return os.path.join(self.root, underlying.upper(), f"exp={expiry:%Y-%m-%d}")

    def bars_path(self, underlying: str, expiry: date) -> str:
        return os.path.join(self.partition_dir(underlying, expiry),
                            f"{underlying.upper()}_{expiry:%Y-%m-%d}_{BARS_INTERVAL}.parquet")

    def manifest_path(self, underlying: str, expiry: date) -> str:
        return os.path.join(self.partition_dir(underlying, expiry), MANIFEST_NAME)

    # -- resume state ---------------------------------------------------
    def read_manifest(self, underlying: str, expiry: date) -> Optional[dict]:
        """The partition's manifest, or None if absent OR unreadable.

        A truncated/corrupt manifest is deliberately indistinguishable from no manifest:
        both mean "we cannot prove this partition is whole", and the only safe response to
        that is to fetch it again.
        """
        path = self.manifest_path(underlying, expiry)
        try:
            with open(path) as f:
                m = json.load(f)
        except (OSError, ValueError):
            return None
        return m if isinstance(m, dict) else None

    def partition_state(self, underlying: str, expiry: date,
                        start: date, end: date) -> PartitionState:
        m = self.read_manifest(underlying, expiry)
        if m is None:
            return PartitionState.MISSING
        if m.get("schema_version") != self.SCHEMA_VERSION:
            # Written by a different (older or newer) layout — do not guess at its meaning.
            return PartitionState.STALE
        got_start, got_end = m.get("start"), m.get("end")
        if not got_start or not got_end:
            return PartitionState.STALE
        # THE END WE NEED IS min(end, expiry), NOT end. A contract cannot print a bar
        # after it expires, so once a partition has been fetched through its own expiry
        # it is FINAL -- a later window end asks for days on which this contract did not
        # exist, and re-fetching them can only return the same rows or fewer.
        #
        # Comparing against the raw `end` instead is how a whole cache re-downloads
        # itself. The warmup defaults its window end to TODAY, so every run moved `end`
        # forward, every already-complete partition compared STALE, and the fetcher
        # re-pulled the entire store: measured 2026-08-30, 820 of 857 symbols and 1,576
        # partitions rewritten in ~35 minutes of a run nobody asked for.
        #
        # That is not merely wasted bandwidth. `write_partition` DELETES the existing
        # parquet when a re-fetch returns no rows, so a partition that was good becomes
        # `status="empty"` the moment the vendor declines to resolve its (long-dead)
        # contracts -- which dxfeed routinely does for expired options. Staleness that
        # cannot be satisfied is a data-loss loop: it re-fetches forever and degrades a
        # little more each pass.
        needed_end = min(end, expiry)
        if got_start > start.isoformat() or got_end < needed_end.isoformat():
            # Covers less than what is being asked for now.
            return PartitionState.STALE
        status = m.get("status")
        if status == "empty":
            return PartitionState.EMPTY
        if status == "complete":
            # The manifest claims rows; if the parquet is gone the claim is void.
            if os.path.exists(self.bars_path(underlying, expiry)):
                return PartitionState.COMPLETE
            return PartitionState.MISSING
        return PartitionState.STALE

    def is_done(self, underlying: str, expiry: date, start: date, end: date) -> bool:
        """True when a re-run must SKIP this partition (complete or genuinely empty)."""
        return self.partition_state(underlying, expiry, start, end) in (
            PartitionState.COMPLETE, PartitionState.EMPTY)

    def pending_partitions(self, underlying: str, expiries: Iterable[date],
                           start: date, end: date) -> List[date]:
        """The expiries still to fetch, in the order given."""
        return [e for e in expiries if not self.is_done(underlying, e, start, end)]

    def progress(self, underlying: str, expiries: Sequence[date],
                 start: date, end: date) -> StoreProgress:
        states = [self.partition_state(underlying, e, start, end) for e in expiries]
        complete = sum(1 for s in states if s is PartitionState.COMPLETE)
        empty = sum(1 for s in states if s is PartitionState.EMPTY)
        return StoreProgress(total=len(states), complete=complete, empty=empty,
                             pending=len(states) - complete - empty)

    # -- writing --------------------------------------------------------
    def write_partition(self, underlying: str, expiry: date, bars: Sequence[OptionEodBar],
                        start: date, end: date, *,
                        empty_contracts: Sequence[str] = ()) -> dict:
        """Write one (underlying, expiry) partition and mark it done.

        Ordering is load-bearing: parquet first, manifest LAST, each via temp+rename. A
        process killed anywhere in here leaves either nothing or an un-manifested parquet,
        and both read back as MISSING on the next run.

        ``bars`` empty and ``empty_contracts`` non-empty records the partition as genuinely
        EMPTY — no parquet is written, but the fact that it was fetched and had nothing IS.
        """
        underlying = underlying.upper()
        d = self.partition_dir(underlying, expiry)
        os.makedirs(d, exist_ok=True)

        bars_path = self.bars_path(underlying, expiry)
        if bars:
            df = self._frame(bars)
            tmp = _tmp_name(bars_path)
            try:
                df.to_parquet(tmp, index=False)
                os.replace(tmp, bars_path)
            finally:
                # A kill BETWEEN to_parquet and replace must not leave a stray temp behind
                # that a later glob could mistake for data.
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:  # pragma: no cover - best effort
                        pass
            rows, contracts = len(df), int(df["occ_symbol"].nunique())
            status = "complete"
        else:
            # No rows: make sure a previous partition's parquet cannot linger and make the
            # (now empty) manifest look complete.
            if os.path.exists(bars_path):
                os.remove(bars_path)
            rows, contracts = 0, 0
            status = "empty"

        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            "status": status,
            "underlying": underlying,
            "expiry": expiry.isoformat(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "interval": BARS_INTERVAL,
            "rows": rows,
            "contracts": contracts,
            "empty_contracts": sorted(empty_contracts),
            "written_at": self._clock().isoformat(),
        }
        self._write_manifest(underlying, expiry, manifest)
        return manifest

    def _write_manifest(self, underlying: str, expiry: date, manifest: dict) -> None:
        path = self.manifest_path(underlying, expiry)
        tmp = _tmp_name(path)
        try:
            with open(tmp, "w") as f:
                json.dump(manifest, f, sort_keys=True)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:  # pragma: no cover - best effort
                    pass

    def _frame(self, bars: Sequence[OptionEodBar]):
        import pandas as pd

        from ba2_providers.options.tastytrade import parse_occ

        rows: List[Dict[str, object]] = []
        for b in bars:
            meta = parse_occ(b.occ_symbol)
            rows.append({
                "occ_symbol": b.occ_symbol,
                "underlying": meta.underlying,
                "option_type": meta.option_type,
                "strike": float(meta.strike),
                "expiry": meta.expiry.isoformat(),
                "bar_date": b.bar_date.isoformat(),
                # NOT float(): OHLC is Optional -- None means "did not trade that day", and
                # float(None) raises. Left as None here and typed by the astype("float64")
                # below, which stores it as a parquet NULL and reads back as NaN. Coercing it
                # to 0.0 instead would mark a contract that merely did not trade -- 44.9% of a
                # liquid chain's rows, some quoted at a $60 mid -- as worthless.
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": b.volume,
                "bid": b.bid,
                "ask": b.ask,
                "iv": b.iv,
                "open_interest": b.open_interest,
            })
        df = pd.DataFrame(rows, columns=list(self.COLUMNS))
        # Explicit dtypes so an ALL-NULL iv/open_interest column still round-trips as a
        # typed column rather than as object/null (which a cross-partition concat drops or
        # chokes on), and so open_interest comes back an integer rather than 12345.0.
        for col in ("open", "high", "low", "close", "strike", "bid", "ask", "iv"):
            df[col] = df[col].astype("float64")
        for col in ("volume", "open_interest"):
            df[col] = df[col].astype("Int64")
        for col in ("occ_symbol", "underlying", "option_type", "expiry", "bar_date"):
            df[col] = df[col].astype("string")
        return df

    # -- discovery cache ------------------------------------------------
    # Enumerating 3.5 years of a liquid name's chain is itself slow (a paginated listing
    # call per underlying), and a resumed run would pay for it again on every restart even
    # though every bar is already on disk. Cache the contract list beside the partitions,
    # keyed on the window it was enumerated over — same staleness rule as the partitions.
    def contracts_path(self, underlying: str) -> str:
        return os.path.join(self.root, underlying.upper(), "_contracts.json")

    def write_contracts(self, underlying: str, contracts: Sequence[Any],
                        start: date, end: date) -> None:
        path = self.contracts_path(underlying)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "underlying": underlying.upper(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "written_at": self._clock().isoformat(),
            "contracts": [
                {"occ_symbol": c.occ_symbol, "underlying": c.underlying,
                 "option_type": c.option_type, "strike": float(c.strike),
                 "expiry": c.expiry.isoformat()}
                for c in contracts
            ],
        }
        tmp = _tmp_name(path)
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f, sort_keys=True)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:  # pragma: no cover - best effort
                    pass

    def read_contracts(self, underlying: str, start: date, end: date):
        """The cached contract list, or None if absent / corrupt / narrower than asked.

        None always means "enumerate again": there is no state in which guessing is safer
        than one extra listing call.
        """
        from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionContractMeta
        try:
            with open(self.contracts_path(underlying)) as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            return None
        got_start, got_end = payload.get("start"), payload.get("end")
        if not got_start or not got_end:
            return None
        if got_start > start.isoformat() or got_end < end.isoformat():
            return None
        try:
            return [OptionContractMeta(
                occ_symbol=c["occ_symbol"], underlying=c["underlying"],
                option_type=c["option_type"], strike=float(c["strike"]),
                expiry=date.fromisoformat(c["expiry"]))
                for c in payload.get("contracts") or []]
        except (KeyError, TypeError, ValueError):
            return None

    # -- reading --------------------------------------------------------
    def read_partition(self, underlying: str, expiry: date):
        """The partition's rows, or None when there is no parquet (missing OR empty)."""
        import pandas as pd
        path = self.bars_path(underlying, expiry)
        if not os.path.exists(path):
            return None
        return pd.read_parquet(path)

    def completed_expiries(self, underlying: str) -> List[date]:
        """Expiries with a readable manifest, ascending. Directories with a leftover temp
        file but no manifest are NOT listed — they are unfinished work, not data."""
        base = os.path.join(self.root, underlying.upper())
        if not os.path.isdir(base):
            return []
        out: List[date] = []
        for name in sorted(os.listdir(base)):
            if not name.startswith("exp="):
                continue
            try:
                exp = date.fromisoformat(name[len("exp="):])
            except ValueError:
                continue
            if self.read_manifest(underlying, exp) is not None:
                out.append(exp)
        return sorted(out)

    def read_underlying(self, underlying: str):
        """Every partition for ``underlying`` concatenated, or None if there are none.

        Globs only the exact ``*_1d.parquet`` name, so a leftover ``.tmp`` from a killed
        write can never be read back as data.
        """
        import pandas as pd
        base = os.path.join(self.root, underlying.upper())
        parts = sorted(glob.glob(os.path.join(base, "exp=*", f"*_{BARS_INTERVAL}.parquet")))
        if not parts:
            return None
        return pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)

    def underlyings(self) -> List[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isdir(os.path.join(self.root, d)))

    def disk_bytes(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:  # pragma: no cover - raced deletion
                    pass
        return total


def log_store_root(store: OptionHistoryParquetStore) -> None:
    """Make the destination unmissable in the log of a multi-hour run."""
    logger.info("option history parquet store: %s", store.root)
