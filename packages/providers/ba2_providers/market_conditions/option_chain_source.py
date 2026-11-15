"""Read-only end-of-day option chains for the chain-input market-condition profiles (``iv-v1``).

TWO store sources, chosen EXPLICITLY (never a default vendor), named exactly like the backtest's
``options_store`` so a manifest's ``option_source`` and a job's store compare directly:

* ``thetadata``    -- ``<CACHE_FOLDER>/ThetaDataOptionsProvider/<SYM>/exp=YYYY-MM-DD/*.parquet``
  (the ThetaData EOD backfill), one partition per expiry;
* ``alphavantage`` -- ``<CACHE_FOLDER>/AlphaVantageOptionsProvider/`` (``tools/download_av_option_chains.py``):
  the SAME partition layout, plus not-yet-compacted ``<SYM>/_days/<date>.parquet`` staged days and a
  ``_ledger.jsonl`` that records every (symbol, date) fetched -- the download's resume authority.

Both are read READ-ONLY, columns ``option_type, strike, expiry, bar_date, bid, ask`` only: never a
vendor ``iv`` / greeks column. Each session's rows become one ``implied_vol.ChainQuotes`` for the
shared calculator. The LIVE reader (``ba2_providers.market_conditions.av_live_chain``) builds the
same ``ChainQuotes`` from the same Alpha Vantage chain, mapped by the same ``map_chain``.

Memory: rows are kept only for sessions in the requested span and contracts with
``DTE_MIN <= expiry - bar_date <= DTE_MAX``, and the store build asks for one CALENDAR YEAR of a
symbol at a time (a large cap's year is ~1-2 million quote rows, tens of MB).
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Dict, List, Optional

import numpy as np

from ba2_common.core.implied_vol import DTE_MAX, DTE_MIN, ChainQuotes

__all__ = ["OPTION_SOURCE_THETADATA", "OPTION_SOURCE_ALPHAVANTAGE", "OPTION_SOURCES",
           "ThetaDataParquetChainSource", "AlphaVantageParquetChainSource", "chain_source_for",
           "default_option_root", "chain_quotes_from_frame"]

#: The ``option_source`` a chain manifest records (an OPTIONAL manifest key: OHLCV-profile manifests
#: never carry it). Equal to the backtest's ``options_store`` name of the same store.
OPTION_SOURCE_THETADATA = "thetadata"
OPTION_SOURCE_ALPHAVANTAGE = "alphavantage"

_COLUMNS = ["option_type", "strike", "expiry", "bar_date", "bid", "ask"]
_EPOCH = date(1970, 1, 1)


def chain_quotes_from_frame(t, first_s: str, last_s: str, where: str = "") -> Dict[str, np.ndarray]:
    """``{"bar", "exp", "call", "strike", "bid", "ask"}`` arrays of the rows of a pyarrow table
    (``_COLUMNS``) with ``first_s <= bar_date <= last_s`` and ``DTE_MIN <= dte <= DTE_MAX``."""
    if t.num_rows == 0:
        return {}
    bar = t.column("bar_date").to_numpy(zero_copy_only=False).astype("U10")
    keep = (bar >= first_s) & (bar <= last_s)
    if not keep.any():
        return {}
    bar_days = bar[keep].astype("datetime64[D]").astype(np.int64)
    exp_days = t.column("expiry").to_numpy(zero_copy_only=False).astype("U10")[keep] \
        .astype("datetime64[D]").astype(np.int64)
    dte = exp_days - bar_days
    ok = (dte >= DTE_MIN) & (dte <= DTE_MAX)
    if not ok.any():
        return {}
    otype = np.char.lower(t.column("option_type").to_numpy(zero_copy_only=False).astype("U8")[keep][ok])
    is_call = otype == "call"
    bad = sorted(set(otype[~is_call & (otype != "put")].tolist()))
    if bad:
        raise ValueError(f"{where}: option_type values {bad!r} are neither call nor put")

    def f8(col: str) -> np.ndarray:
        return np.asarray(t.column(col).to_numpy(zero_copy_only=False), dtype=np.float64)[keep][ok]

    return {"bar": bar_days[ok], "exp": exp_days[ok], "call": is_call, "strike": f8("strike"),
            "bid": f8("bid"), "ask": f8("ask")}


def _group_by_session(parts: List[dict]) -> Dict[date, ChainQuotes]:
    if not parts:
        return {}
    bar = np.concatenate([p["bar"] for p in parts])
    order = np.argsort(bar, kind="stable")
    arrays = {k: np.concatenate([p[k] for p in parts])[order] for k in ("bar", "exp", "call", "strike", "bid", "ask")}
    bar = arrays["bar"]
    uniq, starts = np.unique(bar, return_index=True)
    bounds = list(starts) + [len(bar)]
    out: Dict[date, ChainQuotes] = {}
    for i, d in enumerate(uniq.tolist()):
        lo, hi = int(bounds[i]), int(bounds[i + 1])
        out[_EPOCH + timedelta(days=int(d))] = ChainQuotes(
            arrays["exp"][lo:hi], arrays["call"][lo:hi], arrays["strike"][lo:hi],
            arrays["bid"][lo:hi], arrays["ask"][lo:hi])
    return out


class _ParquetTreeChainSource:
    """Per-session EOD chains of one symbol from a per-expiry partition tree (read-only)."""

    option_source = ""
    provider_dir = ""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.fspath(root))
        if not os.path.isdir(self.root):
            raise FileNotFoundError(f"option chain root {self.root} does not exist")
        #: parquet files read (visibility for the build report and tests).
        self.files_read = 0

    def symbol_dir(self, symbol: str) -> Optional[str]:
        """The symbol's directory; the share-class spellings the trees use are tried in order
        (``BRK-B`` / ``BRK.B`` / ``BRK_B``), exactly one may exist."""
        found = [os.path.join(self.root, s) for s in dict.fromkeys(
            (symbol, symbol.replace("-", "."), symbol.replace(".", "-"), symbol.replace("-", "_")))
            if os.path.isdir(os.path.join(self.root, s))]
        if len(found) > 1:
            raise ValueError(f"option chain tree holds more than one directory for {symbol}: {found}")
        return found[0] if found else None

    def has_symbol(self, symbol: str) -> bool:
        return self.symbol_dir(symbol) is not None

    def _expiries(self, sdir: str) -> List[date]:
        out = []
        for name in os.listdir(sdir):
            if name.startswith("exp=") and os.path.isdir(os.path.join(sdir, name)):
                try:
                    out.append(date.fromisoformat(name[4:14]))
                except ValueError:
                    raise ValueError(f"unparseable expiry partition {os.path.join(sdir, name)}")
        return sorted(out)

    def fetched_through(self, symbol: str) -> Optional[date]:
        """The last date this symbol's tree was FETCHED through (the ``end`` its partition manifests
        record), or None. A chain that stops here while the stock trades is a source tail."""
        sdir = self.symbol_dir(symbol)
        if sdir is None:
            return None
        best: Optional[date] = None
        for exp in reversed(self._expiries(sdir)[-6:]):
            p = os.path.join(sdir, f"exp={exp.isoformat()}", "_manifest.json")
            try:
                with open(p, "r", encoding="utf-8") as f:
                    end = json.load(f).get("end")
            except (OSError, ValueError):
                continue
            if end:
                d = date.fromisoformat(str(end)[:10])
                best = d if best is None or d > best else best
        return best

    def _partition_parts(self, sdir: str, first: date, last: date) -> List[dict]:
        import pyarrow.parquet as pq

        lo_exp, hi_exp = first + timedelta(days=DTE_MIN), last + timedelta(days=DTE_MAX)
        first_s, last_s = first.isoformat(), last.isoformat()
        parts = []
        for exp in self._expiries(sdir):
            if exp < lo_exp or exp > hi_exp:
                continue
            pdir = os.path.join(sdir, f"exp={exp.isoformat()}")
            for name in sorted(os.listdir(pdir)):
                if not name.endswith(".parquet"):
                    continue
                t = pq.read_table(os.path.join(pdir, name), columns=_COLUMNS)
                self.files_read += 1
                part = chain_quotes_from_frame(t, first_s, last_s, f"{pdir}/{name}")
                if part:
                    parts.append(part)
        return parts

    def chains(self, symbol: str, first: date, last: date) -> Dict[date, ChainQuotes]:
        """``{session: ChainQuotes}`` for every ``bar_date`` in ``[first, last]`` that has at least
        one contract with ``DTE_MIN <= dte <= DTE_MAX``. A session absent from the result had no
        such row (the calculator then reports ``missing_session``)."""
        sdir = self.symbol_dir(symbol)
        if sdir is None:
            return {}
        return _group_by_session(self._partition_parts(sdir, first, last))


class ThetaDataParquetChainSource(_ParquetTreeChainSource):
    option_source = OPTION_SOURCE_THETADATA
    provider_dir = "ThetaDataOptionsProvider"


class AlphaVantageParquetChainSource(_ParquetTreeChainSource):
    """The Alpha Vantage store: the compacted partitions PLUS the staged ``_days`` a running download
    has not compacted yet. A staged day REPLACES that day's partition rows (compaction keeps the
    staged rows, ``AvOptionStore.compact``), so a read mid-download sees what compaction will write.
    ``fetched_through`` is the ledger's last fetched date (ok or empty), the download's own record."""

    option_source = OPTION_SOURCE_ALPHAVANTAGE
    provider_dir = "AlphaVantageOptionsProvider"

    def has_symbol(self, symbol: str) -> bool:
        sdir = self.symbol_dir(symbol)
        return sdir is not None and bool(self._ledger_days(sdir))

    @staticmethod
    def _ledger_days(sdir: str) -> List[date]:
        out = []
        try:
            with open(os.path.join(sdir, "_ledger.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        if e.get("s") in ("ok", "empty"):
                            out.append(date.fromisoformat(e["d"]))
                    except (ValueError, KeyError, TypeError):
                        continue        # a torn last line: that date is simply not recorded yet
        except OSError:
            pass
        return sorted(set(out))

    def fetched_through(self, symbol: str) -> Optional[date]:
        sdir = self.symbol_dir(symbol)
        days = self._ledger_days(sdir) if sdir else []
        return days[-1] if days else None

    def chains(self, symbol: str, first: date, last: date) -> Dict[date, ChainQuotes]:
        import pyarrow.parquet as pq

        sdir = self.symbol_dir(symbol)
        if sdir is None:
            return {}
        first_s, last_s = first.isoformat(), last.isoformat()
        staged: List[dict] = []
        staged_days = set()
        sdays = os.path.join(sdir, "_days")
        if os.path.isdir(sdays):
            for name in sorted(os.listdir(sdays)):
                if not name.endswith(".parquet") or not (first_s <= name[:10] <= last_s):
                    continue
                t = pq.read_table(os.path.join(sdays, name), columns=_COLUMNS)
                self.files_read += 1
                part = chain_quotes_from_frame(t, first_s, last_s, f"{sdays}/{name}")
                staged_days.add(np.datetime64(name[:10], "D").astype(np.int64))
                if part:
                    staged.append(part)
        parts = self._partition_parts(sdir, first, last)
        if staged_days:
            drop = np.array(sorted(staged_days), dtype=np.int64)
            trimmed = []
            for p in parts:
                keep = ~np.isin(p["bar"], drop)
                if keep.any():
                    trimmed.append({k: v[keep] for k, v in p.items()})
            parts = trimmed
        return _group_by_session(parts + staged)


#: ``option_source`` -> its store reader.
OPTION_SOURCES = {OPTION_SOURCE_THETADATA: ThetaDataParquetChainSource,
                  OPTION_SOURCE_ALPHAVANTAGE: AlphaVantageParquetChainSource}


def default_option_root(cache_root: str, option_source: str) -> str:
    """``<cache_root>/<ProviderDir>`` of ``option_source``'s store."""
    return os.path.join(cache_root, OPTION_SOURCES[option_source].provider_dir)


def chain_source_for(option_source: str, root: Optional[str] = None, cache_root: Optional[str] = None):
    """The store reader of ``option_source`` (KeyError-free ValueError for an unknown name)."""
    if option_source not in OPTION_SOURCES:
        raise ValueError(f"unknown option source {option_source!r}; known: {sorted(OPTION_SOURCES)}")
    if root is None:
        if cache_root is None:
            raise ValueError("chain_source_for needs a root or a cache_root")
        root = default_option_root(cache_root, option_source)
    return OPTION_SOURCES[option_source](root)
