"""READ-ONLY reader behind the option-cache chain viewer.

Purpose: when a backtest produces no trades the first question is always "was the data
even there?", and until now the only way to answer it was to write SQL against a 10.9 GB
sqlite. This module answers it, laid out the way a broker shows a chain.

THREE STORES, NOT ONE. They differ in what they can honestly say, so they are surfaced
separately rather than blended:

``alpaca-chain``   ``option_chain`` in the legacy sqlite. 1,440,782 rows over 101
                   underlyings, and exactly ONE ``as_of`` snapshot date for the whole
                   file (2024-02-01). ``open_interest`` and ``volume`` are NULL in every
                   one of those rows; ``iv``/``delta``/``gamma``/``theta``/``vega`` are
                   NOT — they are populated on 663,111 of them (46.0%), across 98 of the
                   101 underlyings. ``bid == ask == last`` in all 1,083,571 quoted rows
                   (``SELECT COUNT(*) FROM option_chain WHERE ask > bid`` is 0).
                   Re-measured 2026-08-31, replacing a "6,757,055 rows / three snapshots
                   / greeks NULL everywhere" description that matched nothing in the file;
                   the full record is in
                   ``ba2_common.core.option_selector._publishes_spread``. That equality is a synthesised placeholder from the cache build,
                   not a quote, so this store exposes a single ``close`` and NO bid/ask
                   keys at all. Rendering it as a spread would be a wrong-but-plausible
                   number.

``alpaca-bars``    ``option_bar`` in the same file. 63,298,448 daily bars over hundreds
                   of dates — the only way to ask "did this contract have data on the day
                   my backtest traded?". Carries OHLC + volume and nothing else: on the
                   real file this table does not even have the ``iv``/greek columns
                   (it predates them), so the reader introspects the columns instead of
                   assuming them.

``tastytrade-parquet``  ``CACHE_FOLDER/TastyTradeOptionsProvider/<SYM>/exp=<DATE>/``.
                   The ONLY store carrying ``iv`` and ``open_interest`` — the two fields
                   that cannot be recovered from OHLC — and therefore the only one where
                   a greek is computable at all. Frequently absent (the download runs
                   elsewhere); absence is reported, never crashed on.

NEVER WRITES. Both sqlite handles are opened ``mode=ro&immutable=1``. This module
deliberately does NOT use the cache class in ``backtest/options_cache.py``: that class
runs ``CREATE TABLE`` / ``ALTER TABLE ADD COLUMN`` / ``CREATE INDEX`` in its constructor,
which would put a write path one import away from the production file.

EVERY UNAVAILABLE FIELD CARRIES A REASON. A cell is always
``{"value", "source", "reason"}`` with ``source`` one of:

  ``cache``        the value is in the store, as stored. A recorded 0 is a fact.
  ``computed``     model output (Black-Scholes), not exchange data.
  ``derived``      a summary this module calculated over cached rows.
  ``unavailable``  there is no value, and ``reason`` says why.

``value: None`` never degrades to ``0.0``. A strike with no open interest recorded is not
a strike with zero open interest, and the difference is the whole point of the viewer.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import urllib.parse
from datetime import date
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

# --- store identities ------------------------------------------------------
LEGACY_CHAIN = "alpaca-chain"
LEGACY_BARS = "alpaca-bars"
PARQUET = "tastytrade-parquet"
STORE_IDS = (LEGACY_CHAIN, LEGACY_BARS, PARQUET)

PARQUET_DIR = "TastyTradeOptionsProvider"

#: Black-Scholes is model output. Named in the response so nobody mistakes a computed
#: delta for one the exchange published.
GREEKS_MODEL = ("Black-Scholes-Merton — ba2_common.core.finance_calc.derivatives."
                "black_scholes (European, continuous dividend yield)")

_CHAIN_NULL_REASON = (
    "NULL in the legacy Alpaca chain snapshot — the build never recorded this column "
    "(0 of 1,440,782 rows populated). Absent, not zero.")
_BAR_NO_COLUMN_REASON = (
    "The legacy option_bar table carries OHLC and volume only; it has no column for this "
    "field. Absent, not zero.")
_PARQUET_NULL_REASON = (
    "NULL in the parquet partition — the vendor served no value for this contract on this "
    "date. Absent, not zero.")
_NO_IV_REASON = (
    "Not computable: this contract has no implied volatility in the cache, and a greek "
    "without a volatility is not a number this data can produce.")
_NO_SPOT_REASON = (
    "Not computable: the options cache stores no underlying spot price, and none was "
    "supplied. Enter a spot to compute greeks.")
_EXPIRED_REASON = (
    "Not computable: the expiry is on or before the as-of date, so there is no time value "
    "to price.")
_SPOT_ABSENT_REASON = (
    "No underlying spot price: neither the option_chain/option_bar tables nor the parquet "
    "partitions store one. Supply a spot to compute greeks.")

#: 357,211 of the 1,440,782 chain rows (24.8%) carry NULL bid/ask/last: the contract is in
#: the snapshot but the build captured no price for it. "In the chain" and "priced" are not
#: the same claim, and the difference is exactly what someone asking "was the data there?"
#: needs to see.
_NO_QUOTE_REASON = (
    "No price recorded. The contract is in the chain snapshot but the build captured no "
    "quote for it — 357,211 of the 1,440,782 chain rows are like this. Absent, not zero.")

_QUOTE_NOTE_LEGACY = (
    "This store records bid == ask == close in every quoted row (0 of 1,083,571 have "
    "ask > bid). That is a placeholder written by the cache build, not a market quote, so "
    "no bid/ask spread is shown — only the close it was copied from.")

_PARQUET_ABSENT_REASON_FMT = (
    "No TastyTrade parquet store at {root}. It is built by tools/warm_options_history.py; "
    "until then there is no cached implied volatility or open interest for any symbol.")


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------
def cell(value: Any, source: str, reason: Optional[str] = None) -> Dict[str, Any]:
    return {"value": value, "source": source, "reason": reason}


def na(reason: str) -> Dict[str, Any]:
    """An absent value. NEVER 0.0 — the caller renders this as 'n/a' plus the reason."""
    return cell(None, "unavailable", reason)


def cached_or_na(value: Any, reason: str) -> Dict[str, Any]:
    """A stored value, or 'unavailable' when it is NULL.

    ``0`` is a stored value and stays one: a recorded zero open interest is a fact about
    a strike nobody holds, and conflating it with "never recorded" is exactly the error
    this whole module exists to avoid.
    """
    if value is None:
        return na(reason)
    return cell(value, "cache")


# ---------------------------------------------------------------------------
# Read-only sqlite access
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def open_legacy_readonly(path: str):
    """A handle that physically cannot write to the cache file.

    ``mode=ro`` makes any INSERT/CREATE/ALTER raise; ``immutable=1`` additionally stops
    sqlite touching the file at all (no -wal/-shm sidecars, no locking) — which matters
    because this file is 10.9 GB of irreplaceable download.
    """
    uri = "file:" + urllib.parse.quote(path) + "?mode=ro&immutable=1"
    cx = sqlite3.connect(uri, uri=True)
    cx.row_factory = sqlite3.Row
    try:
        yield cx
    finally:
        cx.close()


def _legacy_db_path() -> str:
    # Imported at CALL time, never bound at import: tests rebind the attribute on
    # ba2_common.config, and an import-time capture would send every read at the real
    # 10.9 GB file.
    import ba2_common.config as cfg
    return cfg.OPTIONS_CACHE_DB


def _parquet_root() -> str:
    import ba2_common.config as cfg
    return os.path.join(cfg.CACHE_FOLDER, PARQUET_DIR)


def _legacy_present() -> bool:
    return os.path.isfile(_legacy_db_path())


# --- memoisation -----------------------------------------------------------
# Only the two genuinely expensive lookups are memoised, keyed on the file's identity
# (path + size + mtime) so a rebuilt cache is never served stale.
_TABLE_COLUMNS: Dict[Tuple, List[str]] = {}


def reset_caches() -> None:
    """Drop every memo. Called by tests between fixtures."""
    _TABLE_COLUMNS.clear()


def _file_key(path: str) -> Tuple:
    try:
        st = os.stat(path)
        return (path, st.st_size, st.st_mtime_ns)
    except OSError:
        return (path, None, None)


def _columns(cx, table: str, path: str) -> List[str]:
    key = _file_key(path) + (table,)
    cols = _TABLE_COLUMNS.get(key)
    if cols is None:
        cols = [r[1] for r in cx.execute(f"PRAGMA table_info({table})")]
        _TABLE_COLUMNS[key] = cols
    return cols


# ---------------------------------------------------------------------------
# Store descriptors
# ---------------------------------------------------------------------------
def _parquet_store():
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore
    return OptionHistoryParquetStore(root=_parquet_root())


def _parquet_symbols() -> List[str]:
    root = _parquet_root()
    if not os.path.isdir(root):
        return []
    return _parquet_store().underlyings()


def stores() -> List[Dict[str, Any]]:
    """What each backend is, whether it is here, and what it can honestly report."""
    db = _legacy_db_path()
    legacy = _legacy_present()
    legacy_bytes = os.path.getsize(db) if legacy else 0
    legacy_absent = None if legacy else (
        f"No legacy options cache at {db}. It is built by `ba2-test fetch-options`.")

    root = _parquet_root()
    pq_syms = _parquet_symbols()
    pq_present = bool(pq_syms)

    return [
        {
            "id": LEGACY_CHAIN,
            "label": "Legacy chain snapshots (Alpaca sqlite)",
            "present": legacy,
            "path": db,
            "bytes": legacy_bytes,
            "absent_reason": legacy_absent,
            "symbols": None,
            "has_iv": False,
            "has_open_interest": False,
            "has_greeks": False,
            "has_volume": False,
            "has_quote_spread": False,
            "quote_note": _QUOTE_NOTE_LEGACY,
            "description": (
                "Point-in-time chain snapshots. Very few as-of dates (one in the whole "
                "file); prices only, every greek and open-interest column NULL."),
        },
        {
            "id": LEGACY_BARS,
            "label": "Legacy daily bars (Alpaca sqlite)",
            "present": legacy,
            "path": db,
            "bytes": legacy_bytes,
            "absent_reason": legacy_absent,
            "symbols": None,
            "has_iv": False,
            "has_open_interest": False,
            "has_greeks": False,
            "has_volume": True,
            "has_quote_spread": False,
            "quote_note": (
                "Daily OHLC bars. There are no quotes here at all — not a zero spread, "
                "no bid and no ask were ever recorded."),
            "description": (
                "One row per contract per traded day, over hundreds of dates. Use this to "
                "ask whether a contract had data on a specific backtest date."),
        },
        {
            "id": PARQUET,
            "label": "TastyTrade parquet store",
            "present": pq_present,
            "path": root,
            "bytes": None,
            "absent_reason": None if pq_present else _PARQUET_ABSENT_REASON_FMT.format(root=root),
            "symbols": len(pq_syms),
            "has_iv": True,
            "has_open_interest": True,
            "has_greeks": False,
            "has_volume": True,
            "has_quote_spread": False,
            "quote_note": (
                "Daily OHLC bars plus vendor implied volatility and open interest. No "
                "quotes: dxfeed serves no historical NBBO for dead contracts."),
            "description": (
                "The only store carrying implied volatility and open interest, and so the "
                "only one where greeks can be computed."),
        },
    ]


def _store_by_id(store_id: str) -> Optional[Dict[str, Any]]:
    for s in stores():
        if s["id"] == store_id:
            return s
    return None


# ---------------------------------------------------------------------------
# Symbol search
# ---------------------------------------------------------------------------
def search_symbols(q: str, limit: int = 50) -> Dict[str, Any]:
    """Prefix search across every store.

    A PREFIX range, not a scan: ``SELECT DISTINCT underlying FROM option_bar`` costs
    ~1.4 s on the real 63 M-row table, but ``underlying >= 'AAP' AND underlying < 'AAP\\uffff'``
    is a range seek on idx_option_bar_underlying and returns in single-digit ms. The
    endpoint therefore requires a prefix rather than offering a "list everything" mode
    that would time out the UI.
    """
    prefix = (q or "").strip().upper()
    hit: Dict[str, set] = {}
    if prefix and _legacy_present():
        hi = prefix + "￿"
        with open_legacy_readonly(_legacy_db_path()) as cx:
            for table, sid in (("option_chain", LEGACY_CHAIN), ("option_bar", LEGACY_BARS)):
                for row in cx.execute(
                        f"SELECT DISTINCT underlying FROM {table} "
                        f"WHERE underlying >= ? AND underlying < ? ORDER BY underlying "
                        f"LIMIT ?", (prefix, hi, limit)):
                    hit.setdefault(row[0], set()).add(sid)
    for sym in _parquet_symbols():
        if prefix and sym.upper().startswith(prefix):
            hit.setdefault(sym.upper(), set()).add(PARQUET)

    ordered = sorted(hit)
    out = [{"symbol": s, "stores": sorted(hit[s])} for s in ordered[:limit]]
    return {"query": prefix, "symbols": out, "truncated": len(ordered) > limit}


# ---------------------------------------------------------------------------
# Available as-of dates — the picker may only offer these
# ---------------------------------------------------------------------------
def available_dates(symbol: str) -> Dict[str, Any]:
    """Per store, the as-of dates that actually hold rows for ``symbol``.

    A free calendar over a store with a single snapshot date would return nothing on almost
    every click and read as broken, so the UI picks from this list.
    """
    sym = symbol.strip().upper()
    result: Dict[str, Any] = {}
    legacy = _legacy_present()
    absent = None if legacy else (
        f"No legacy options cache at {_legacy_db_path()}.")

    for sid in (LEGACY_CHAIN, LEGACY_BARS):
        result[sid] = {"present": legacy, "dates": [], "absent_reason": absent}
    if legacy:
        with open_legacy_readonly(_legacy_db_path()) as cx:
            # A prefix seek on the (underlying, as_of, occ_symbol) primary key — 8 ms for
            # AAPL's 20k chain rows on the real file.
            result[LEGACY_CHAIN]["dates"] = [
                {"as_of": r[0], "rows": r[1]} for r in cx.execute(
                    "SELECT as_of, COUNT(*) FROM option_chain WHERE underlying=? "
                    "GROUP BY as_of ORDER BY as_of", (sym,))]
            # option_bar has no (underlying, date) index, so this walks the underlying's
            # rows: ~210 ms for AAPL's 342,642 bars on the real file. See the module note.
            result[LEGACY_BARS]["dates"] = [
                {"as_of": r[0], "rows": r[1]} for r in cx.execute(
                    "SELECT date, COUNT(*) FROM option_bar WHERE underlying=? "
                    "GROUP BY date ORDER BY date", (sym,))]

    pq = _store_by_id(PARQUET)
    result[PARQUET] = {"present": pq["present"], "dates": [],
                       "absent_reason": pq["absent_reason"]}
    if pq["present"]:
        df = _parquet_frame(sym)
        if df is not None and len(df):
            counts = df.groupby("bar_date").size()
            result[PARQUET]["dates"] = [
                {"as_of": str(d), "rows": int(n)} for d, n in sorted(counts.items())]
    return {"symbol": sym, "stores": result}


def _parquet_frame(symbol: str):
    store = _parquet_store()
    try:
        return store.read_underlying(symbol.upper())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Greeks — computed, and labelled as such
# ---------------------------------------------------------------------------
_GREEK_KEYS = ("delta", "gamma", "theta", "vega")


def _greeks(spot: Optional[float], strike: float, years: float, rate: float,
            iv: Optional[float], option_type: str,
            dividend_yield: float) -> Dict[str, Dict[str, Any]]:
    """Black-Scholes greeks, or four reasons why not.

    The shared implementation is used verbatim (see GREEKS_MODEL); nothing here
    re-derives one. Order of refusal matters for the message the user sees: a missing IV
    is a property of the DATA and no spot will fix it, so it is reported first.
    """
    if iv is None:
        return {k: na(_NO_IV_REASON) for k in _GREEK_KEYS}
    if iv <= 0:
        return {k: na("Not computable: the cached implied volatility is not positive.")
                for k in _GREEK_KEYS}
    if spot is None or spot <= 0:
        return {k: na(_NO_SPOT_REASON) for k in _GREEK_KEYS}
    if years <= 0:
        return {k: na(_EXPIRED_REASON) for k in _GREEK_KEYS}

    from ba2_common.core.finance_calc.derivatives import black_scholes
    bs = black_scholes(spot, strike, years, rate, iv, option_type=option_type,
                       dividend_yield=dividend_yield)
    return {
        "delta": cell(bs["delta"], "computed", GREEKS_MODEL),
        "gamma": cell(bs["gamma"], "computed", GREEKS_MODEL),
        # theta per calendar day, vega per volatility point — the desk conventions the
        # shared helper already returns. Stated so nobody reads them as annualised.
        "theta": cell(bs["theta_per_day"], "computed", GREEKS_MODEL + "; per calendar day"),
        "vega": cell(bs["vega_per_point"], "computed", GREEKS_MODEL + "; per 1 vol point"),
    }


# ---------------------------------------------------------------------------
# Chain assembly
# ---------------------------------------------------------------------------
class ChainUnavailable(Exception):
    """No chain to show, with a message that says specifically why."""

    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.message = message
        self.status = status


def _py(v):
    """numpy/pandas scalar -> plain Python, NA -> None."""
    if v is None:
        return None
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    item = getattr(v, "item", None)
    return item() if callable(item) else v


def _dte(expiry: str, as_of: str) -> int:
    return (date.fromisoformat(expiry) - date.fromisoformat(as_of)).days


def _assemble(legs: List[Dict[str, Any]], as_of: str) -> List[Dict[str, Any]]:
    """Group legs by expiry, then pair call/put onto one row per strike.

    Strikes down the middle: each row carries ONE strike with the call that belongs to it
    on one side and the put on the other. A strike quoted on only one side leaves the
    other slot ``None`` rather than inventing a leg — and no leg may appear under an
    expiry other than its own, which is the invariant the ordering below preserves.
    """
    by_expiry: Dict[str, Dict[float, Dict[str, Any]]] = {}
    ivs: Dict[str, List[float]] = {}
    for leg in legs:
        exp = leg["expiry"]
        row = by_expiry.setdefault(exp, {}).setdefault(
            leg["strike"], {"strike": leg["strike"], "call": None, "put": None})
        row[leg["option_type"]] = leg
        iv = leg.get("iv")
        if iv is not None and iv["value"] is not None:
            ivs.setdefault(exp, []).append(iv["value"])

    out = []
    for exp in sorted(by_expiry):
        vals = ivs.get(exp) or []
        if vals:
            iv_median = cell(median(vals), "derived",
                             f"Median of the {len(vals)} cached implied-volatility values "
                             f"on this expiry. A summary of what is stored — NOT a vendor "
                             f"volatility index, and not interpolated to a constant tenor.")
        else:
            iv_median = na("No implied volatility is stored for any contract on this "
                           "expiry, so there is nothing to summarise.")
        out.append({
            "expiry": exp,
            "dte": _dte(exp, as_of),
            "iv_median": iv_median,
            "rows": [by_expiry[exp][k] for k in sorted(by_expiry[exp])],
        })
    return out


def _chain_legacy_chain(cx, sym: str, as_of: str) -> List[Dict[str, Any]]:
    dates = [r[0] for r in cx.execute(
        "SELECT DISTINCT as_of FROM option_chain WHERE underlying=? ORDER BY as_of", (sym,))]
    if not dates:
        raise ChainUnavailable(
            f"The legacy chain snapshots hold no rows for {sym}. Nothing was ever cached "
            f"for this underlying.")
    if as_of not in dates:
        raise ChainUnavailable(
            f"No chain snapshot for {sym} on {as_of}. This store holds snapshots only, and "
            f"for {sym} the cached as-of dates are: {', '.join(dates)}.")
    legs = []
    for r in cx.execute(
            "SELECT * FROM option_chain WHERE underlying=? AND as_of=?", (sym, as_of)):
        legs.append({
            "occ_symbol": r["occ_symbol"],
            "option_type": r["option_type"],
            "strike": r["strike"],
            "expiry": r["expiry"],
            "store": LEGACY_CHAIN,
            # ONE price column. bid/ask are deliberately not emitted: they are equal to
            # this same close in every quoted row of the real file.
            "close": cached_or_na(r["last"], _NO_QUOTE_REASON),
            "volume": na(_CHAIN_NULL_REASON),
            "open_interest": na(_CHAIN_NULL_REASON),
            "iv": na(_CHAIN_NULL_REASON),
        })
    return legs


def _chain_legacy_bars(cx, sym: str, as_of: str) -> List[Dict[str, Any]]:
    cols = set(_columns(cx, "option_bar", _legacy_db_path()))
    dates = [r[0] for r in cx.execute(
        "SELECT DISTINCT date FROM option_bar WHERE underlying=? ORDER BY date", (sym,))]
    if not dates:
        raise ChainUnavailable(
            f"The legacy daily bars hold no rows for {sym}. Nothing was ever cached for "
            f"this underlying.")
    if as_of not in dates:
        shown = ", ".join(dates[:8]) + (" …" if len(dates) > 8 else "")
        raise ChainUnavailable(
            f"No daily bars for {sym} on {as_of} — nothing traded, or nothing was cached. "
            f"{len(dates)} dates are cached for {sym}, from {dates[0]} to {dates[-1]} "
            f"({shown}).")
    legs = []
    for r in cx.execute(
            "SELECT * FROM option_bar WHERE underlying=? AND date=?", (sym, as_of)):
        # iv/greek columns are absent from the real file's option_bar (it predates them),
        # so they are read only where they exist and reported as absent where they do not.
        iv = r["iv"] if "iv" in cols else None
        legs.append({
            "occ_symbol": r["occ_symbol"],
            "option_type": r["option_type"],
            "strike": r["strike"],
            "expiry": r["expiry"],
            "store": LEGACY_BARS,
            "open": cached_or_na(r["open"], "No open recorded."),
            "high": cached_or_na(r["high"], "No high recorded."),
            "low": cached_or_na(r["low"], "No low recorded."),
            "close": cached_or_na(r["close"], "No close recorded."),
            "volume": cached_or_na(r["volume"], "No volume recorded."),
            "open_interest": na(_BAR_NO_COLUMN_REASON),
            "iv": cached_or_na(iv, _BAR_NO_COLUMN_REASON if "iv" not in cols
                               else _PARQUET_NULL_REASON),
        })
    return legs


def _chain_parquet(sym: str, as_of: str) -> List[Dict[str, Any]]:
    df = _parquet_frame(sym)
    if df is None or not len(df):
        raise ChainUnavailable(
            f"The TastyTrade parquet store holds no partitions for {sym}.")
    dates = sorted({str(d) for d in df["bar_date"]})
    if as_of not in dates:
        shown = ", ".join(dates[:8]) + (" …" if len(dates) > 8 else "")
        raise ChainUnavailable(
            f"No parquet rows for {sym} on {as_of}. {len(dates)} dates are cached for "
            f"{sym}, from {dates[0]} to {dates[-1]} ({shown}).")
    legs = []
    for rec in df[df["bar_date"] == as_of].to_dict("records"):
        legs.append({
            "occ_symbol": _py(rec["occ_symbol"]),
            "option_type": _py(rec["option_type"]),
            "strike": float(_py(rec["strike"])),
            "expiry": str(_py(rec["expiry"])),
            "store": PARQUET,
            "open": cached_or_na(_py(rec["open"]), "No open recorded."),
            "high": cached_or_na(_py(rec["high"]), "No high recorded."),
            "low": cached_or_na(_py(rec["low"]), "No low recorded."),
            "close": cached_or_na(_py(rec["close"]), "No close recorded."),
            "volume": cached_or_na(_py(rec["volume"]), _PARQUET_NULL_REASON),
            "open_interest": cached_or_na(_py(rec["open_interest"]), _PARQUET_NULL_REASON),
            "iv": cached_or_na(_py(rec["iv"]), _PARQUET_NULL_REASON),
        })
    return legs


_COLUMNS_BY_STORE = {
    LEGACY_CHAIN: {"quote": "close", "has_quote_spread": False, "iv": False,
                   "open_interest": False, "volume": False},
    LEGACY_BARS: {"quote": "ohlc", "has_quote_spread": False, "iv": False,
                  "open_interest": False, "volume": True},
    PARQUET: {"quote": "ohlc", "has_quote_spread": False, "iv": True,
              "open_interest": True, "volume": True},
}


def chain(symbol: str, as_of: str, store_id: str, *, spot: Optional[float],
          rate: float, dividend_yield: float) -> Dict[str, Any]:
    """One store's chain for one symbol on one as-of date, laid out by expiry then strike.

    ``rate`` and ``dividend_yield`` are REQUIRED, with no default here. They are pricing
    assumptions, not data — nothing in any cache records them — and every computed greek
    moves with them. A default on this function would have been dead code (the endpoint
    always passes its own), which means a mutation to it could never be caught: one
    default, declared once, at the HTTP boundary where it is documented to the user.
    """
    if store_id not in STORE_IDS:
        raise ChainUnavailable(
            f"Unknown store '{store_id}'. Choose one of: {', '.join(STORE_IDS)}.", status=400)
    try:
        date.fromisoformat(as_of)
    except ValueError:
        raise ChainUnavailable(f"as_of must be YYYY-MM-DD, got '{as_of}'.", status=400)

    desc = _store_by_id(store_id)
    if not desc["present"]:
        raise ChainUnavailable(desc["absent_reason"])

    sym = symbol.strip().upper()
    if store_id == PARQUET:
        legs = _chain_parquet(sym, as_of)
    else:
        with open_legacy_readonly(_legacy_db_path()) as cx:
            legs = (_chain_legacy_chain(cx, sym, as_of) if store_id == LEGACY_CHAIN
                    else _chain_legacy_bars(cx, sym, as_of))

    for leg in legs:
        leg.update(_greeks(spot, leg["strike"],
                           _dte(leg["expiry"], as_of) / 365.0, rate,
                           leg["iv"]["value"], leg["option_type"], dividend_yield))

    greeks_mode = "computed" if (desc["has_iv"] and spot is not None) else "unavailable"
    columns = dict(_COLUMNS_BY_STORE[store_id])
    columns["greeks"] = greeks_mode

    spot_cell = (cell(spot, "user-supplied") if spot is not None
                 else na(_SPOT_ABSENT_REASON))

    notes = [desc["quote_note"]]
    if greeks_mode == "computed":
        notes.append(
            f"Greeks are COMPUTED from the cached implied volatility and the spot you "
            f"supplied, using {GREEKS_MODEL}. They are model output, not exchange data — "
            f"no store here publishes vendor greeks. Rate {rate:.4f}, dividend yield "
            f"{dividend_yield:.4f}, actual/365 day count; change the rate and every greek "
            f"below changes with it.")
    else:
        notes.append(
            "No greeks are shown: they are model output and this view has no inputs to "
            "compute them from. See each cell's reason.")
    if not desc["has_iv"]:
        notes.append(
            "This store records no implied volatility and no open interest. Those two "
            "fields cannot be recovered from OHLC — only the TastyTrade parquet store "
            "carries them.")

    return {
        "symbol": sym,
        "as_of": as_of,
        "store": store_id,
        "store_label": desc["label"],
        "columns": columns,
        "spot": spot_cell,
        "greeks_model": GREEKS_MODEL,
        "greeks_inputs": {"rate": rate, "dividend_yield": dividend_yield,
                          "day_count": "actual/365"},
        "contracts": len(legs),
        "expiries": _assemble(legs, as_of),
        "notes": notes,
    }
