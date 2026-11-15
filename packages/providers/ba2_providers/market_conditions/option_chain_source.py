"""Read-only end-of-day option chains for the chain-input market-condition profiles (``iv-v1``).

The ONE backtest/store source today is the ThetaData EOD parquet tree the option backfill writes
(``<CACHE_FOLDER>/ThetaDataOptionsProvider/<SYM>/exp=YYYY-MM-DD/*.parquet``, one partition per
expiry, ``bar_date`` an ISO string, bid/ask the vendor's end-of-day NBBO). This module reads it
READ-ONLY (columns ``option_type, strike, expiry, bar_date, bid, ask`` -- never the vendor ``iv``
column, which is keyed without the expiry) and hands the pure calculator one
``implied_vol.ChainQuotes`` per session.

A future LIVE adapter implements the same two methods (``has_symbol`` / ``chains``) over a live
source of the prior session's EOD chain; the calculator and every gate stay the same.

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

__all__ = ["OPTION_SOURCE_THETADATA_EOD", "ThetaDataParquetChainSource", "default_option_root"]

#: The ``option_source`` a manifest built from this tree records (an OPTIONAL manifest key, so the
#: identity of every OHLCV-profile manifest is unchanged by its existence).
OPTION_SOURCE_THETADATA_EOD = "thetadata-eod-parquet-v1"

_COLUMNS = ["option_type", "strike", "expiry", "bar_date", "bid", "ask"]
_PROVIDER_DIR = "ThetaDataOptionsProvider"


def default_option_root(cache_root: str) -> str:
    """``<cache_root>/ThetaDataOptionsProvider`` -- where the ThetaData backfill writes."""
    return os.path.join(cache_root, _PROVIDER_DIR)


class ThetaDataParquetChainSource:
    """Per-session EOD chains of one symbol from the ThetaData parquet tree (read-only)."""

    option_source = OPTION_SOURCE_THETADATA_EOD

    def __init__(self, root: str):
        self.root = os.path.abspath(os.fspath(root))
        if not os.path.isdir(self.root):
            raise FileNotFoundError(f"option chain root {self.root} does not exist")
        #: parquet files read (visibility for the build report and tests).
        self.files_read = 0

    def symbol_dir(self, symbol: str) -> Optional[str]:
        """The symbol's directory; the share-class spellings the tree uses are tried in order
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
        """The last date the backfill FETCHED this symbol's tree through (the ``end`` its
        partition manifests record), or None when no partition records one. A chain that stops
        here while the underlying keeps trading is a source tail, not a delisting."""
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

    def chains(self, symbol: str, first: date, last: date) -> Dict[date, ChainQuotes]:
        """``{session: ChainQuotes}`` for every ``bar_date`` in ``[first, last]`` that has at least
        one contract with ``DTE_MIN <= dte <= DTE_MAX``. A session absent from the result had no
        such row (the calculator then reports ``missing_session``)."""
        import pyarrow.parquet as pq

        sdir = self.symbol_dir(symbol)
        if sdir is None:
            return {}
        lo_exp, hi_exp = first + timedelta(days=DTE_MIN), last + timedelta(days=DTE_MAX)
        first_s, last_s = first.isoformat(), last.isoformat()
        cols: Dict[str, list] = {c: [] for c in ("bar", "exp", "call", "strike", "bid", "ask")}
        for exp in self._expiries(sdir):
            if exp < lo_exp or exp > hi_exp:
                continue
            pdir = os.path.join(sdir, f"exp={exp.isoformat()}")
            for name in sorted(os.listdir(pdir)):
                if not name.endswith(".parquet"):
                    continue
                t = pq.read_table(os.path.join(pdir, name), columns=_COLUMNS)
                self.files_read += 1
                if t.num_rows == 0:
                    continue
                bar = t.column("bar_date").to_numpy(zero_copy_only=False).astype("U10")
                keep = (bar >= first_s) & (bar <= last_s)
                if not keep.any():
                    continue
                bar_days = bar[keep].astype("datetime64[D]").astype(np.int64)
                exp_days = t.column("expiry").to_numpy(zero_copy_only=False).astype("U10")[keep] \
                    .astype("datetime64[D]").astype(np.int64)
                dte = exp_days - bar_days
                ok = (dte >= DTE_MIN) & (dte <= DTE_MAX)
                if not ok.any():
                    continue
                otype = t.column("option_type").to_numpy(zero_copy_only=False).astype("U8")[keep][ok]
                otype = np.char.lower(otype)
                is_call = otype == "call"
                bad = sorted(set(otype[~is_call & (otype != "put")].tolist()))
                if bad:
                    raise ValueError(f"{pdir}/{name}: option_type values {bad!r} are neither call nor put")

                def f8(col: str) -> np.ndarray:
                    return np.asarray(t.column(col).to_numpy(zero_copy_only=False), dtype=np.float64)[keep][ok]

                cols["bar"].append(bar_days[ok])
                cols["exp"].append(exp_days[ok])
                cols["call"].append(is_call)
                cols["strike"].append(f8("strike"))
                cols["bid"].append(f8("bid"))
                cols["ask"].append(f8("ask"))
        if not cols["bar"]:
            return {}
        bar = np.concatenate(cols["bar"])
        order = np.argsort(bar, kind="stable")
        arrays = {k: np.concatenate(v)[order] for k, v in cols.items()}
        bar = arrays["bar"]
        uniq, starts = np.unique(bar, return_index=True)
        bounds = list(starts) + [len(bar)]
        out: Dict[date, ChainQuotes] = {}
        epoch = date(1970, 1, 1)
        for i, d in enumerate(uniq.tolist()):
            lo, hi = int(bounds[i]), int(bounds[i + 1])
            out[epoch + timedelta(days=int(d))] = ChainQuotes(
                arrays["exp"][lo:hi], arrays["call"][lo:hi], arrays["strike"][lo:hi],
                arrays["bid"][lo:hi], arrays["ask"][lo:hi])
        return out

