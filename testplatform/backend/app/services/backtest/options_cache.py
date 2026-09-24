"""Offline historical options cache (sqlite). Mirrors the screener-history cache:
built once by `ba2-test fetch-options`, read-only at backtest time, fail-fast on miss."""
from __future__ import annotations
import os
import sqlite3
from typing import Any, Dict, List, Optional

class OptionsCacheMiss(RuntimeError):
    """Raised when the cache has no chain/bar for a requested (underlying, as_of)/contract.
    Subclasses RuntimeError so it fails the run with an actionable message
    (build the cache via `ba2-test fetch-options`) instead of silently trading nothing."""

_CHAIN_DDL = """CREATE TABLE IF NOT EXISTS option_chain(
  underlying TEXT, as_of TEXT, occ_symbol TEXT, option_type TEXT, strike REAL, expiry TEXT,
  bid REAL, ask REAL, last REAL, iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
  open_interest INTEGER, volume INTEGER, PRIMARY KEY(underlying, as_of, occ_symbol))"""
_BAR_DDL = """CREATE TABLE IF NOT EXISTS option_bar(
  occ_symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  underlying TEXT, option_type TEXT, strike REAL, expiry TEXT,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL, PRIMARY KEY(occ_symbol, date))"""
_CHAIN_COLS = ["occ_symbol","option_type","strike","expiry","bid","ask","last","iv",
               "delta","gamma","theta","vega","open_interest","volume"]
_BAR_COLS = ["occ_symbol","date","open","high","low","close","volume","underlying",
             "option_type","strike","expiry","iv","delta","gamma","theta","vega"]

# Columns added AFTER the tables first shipped. CREATE TABLE IF NOT EXISTS is a no-op on
# an existing table, so a cache file built before the greeks feature keeps the old layout
# and the first write_bar_rows dies with "table option_bar has no column named iv" --
# which is what once blocked re-fetching the shared cache WITH computed IV.
# ALTER ... ADD COLUMN is cheap in sqlite (a header rewrite, not a table copy) and leaves
# existing rows with NULL in the new columns -- honest: those greeks were never fetched,
# and every reader already treats a NULL iv/delta as unusable.
#
# THAT MIGRATION HAS RUN, so do not read the paragraph above as a description of the store.
# This note used to end "which in turn is why get_atm_iv (and therefore the backtest's IV
# rank) returns None for everything" -- no longer true, and it outlived its own fix. The
# shared cache now carries iv and the four greeks on 88.2% of option_bar rows and 46.0% of
# option_chain rows; get_atm_iv reads the BAR and returns a number on the great majority of
# them. See ba2_common.core.option_selector._publishes_spread, the one re-verified record.
_GREEK_COLS = ("iv", "delta", "gamma", "theta", "vega")


class OptionsHistoryCache:
    def __init__(self, db_path: str, *, read_only: bool = False):
        """Open the cache. ``read_only=True`` is for READ paths that must not write.

        A GET that serves a chart must not create a database, migrate an old one or build an
        index (review R8: a saved path pointing at a previously absent file made the
        read-only chart endpoint create a new SQLite database). Read-only mode therefore
        requires the file to already exist, skips ALL schema work, and opens the connection
        with SQLite's ``mode=ro`` so a write could not succeed even by accident. Warming and
        migration stay where they belong -- in the fetch/backtest path, not in a request.
        """
        self.db_path = db_path
        self.read_only = read_only

        if read_only:
            if not db_path or not os.path.exists(db_path):
                raise OptionsCacheMiss(f"option cache not found: {db_path}")
            with self._conn() as cx:
                # Fail loudly on an unreadable or non-cache file rather than pretending to
                # have no data for the contract.
                cx.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return

        with self._conn() as cx:
            cx.execute(_CHAIN_DDL); cx.execute(_BAR_DDL)
            for table in ("option_chain", "option_bar"):
                self._add_missing_columns(cx, table, _GREEK_COLS)
            # Speeds up options_provider.py's per-underlying chain load (the read-side worker
            # cache queries "WHERE underlying=?" once per underlying per worker process instead
            # of per bar/contract) -- idempotent, cheap no-op once built.
            cx.execute("CREATE INDEX IF NOT EXISTS idx_option_chain_underlying ON option_chain(underlying)")
            cx.execute("CREATE INDEX IF NOT EXISTS idx_option_bar_underlying ON option_bar(underlying)")

    @staticmethod
    def _add_missing_columns(cx, table: str, columns) -> None:
        """Bring `table` up to the declared schema. Idempotent (runs on every open).

        "Already there" is tolerated rather than raised: GA workers open the SHARED
        cache concurrently, so two of them can both read PRAGMA before either ALTERs
        and the loser would otherwise die on startup with "duplicate column name: iv".
        The column existing is exactly the state we were trying to reach.
        """
        present = {r[1] for r in cx.execute(f"PRAGMA table_info({table})")}
        for col in columns:
            if col in present:
                continue
            try:
                cx.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    def _conn(self):
        if getattr(self, 'read_only', False):
            cx = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        else:
            cx = sqlite3.connect(self.db_path)
        cx.row_factory = sqlite3.Row
        return cx

    def write_chain_rows(self, underlying: str, as_of: str, rows: List[Dict[str, Any]]) -> None:
        with self._conn() as cx:
            cx.executemany(
                f"INSERT OR REPLACE INTO option_chain(underlying,as_of,{','.join(_CHAIN_COLS)}) "
                f"VALUES(?,?,{','.join('?'*len(_CHAIN_COLS))})",
                [(underlying, as_of, *[r.get(c) for c in _CHAIN_COLS]) for r in rows])

    def write_bar_rows(self, rows: List[Dict[str, Any]]) -> None:
        with self._conn() as cx:
            cx.executemany(
                f"INSERT OR REPLACE INTO option_bar({','.join(_BAR_COLS)}) "
                f"VALUES({','.join('?'*len(_BAR_COLS))})",
                [tuple(r.get(c) for c in _BAR_COLS) for r in rows])

    def cached_underlyings(self) -> set:
        """Underlyings that completed a build (have at least one chain row — chain is written LAST
        per underlying, so its presence means the underlying's bars finished). Used to RESUME a
        partial fetch: skip these, re-do the rest (incl. one that crashed mid-bars before its chain).
        """
        with self._conn() as cx:
            return {r[0] for r in cx.execute("SELECT DISTINCT underlying FROM option_chain")}

    def read_chain(self, underlying: str, as_of: str) -> List[Dict[str, Any]]:
        with self._conn() as cx:
            return [dict(r) for r in cx.execute(
                "SELECT * FROM option_chain WHERE underlying=? AND as_of=?", (underlying, as_of))]

    def read_chain_or_miss(self, underlying: str, as_of: str) -> List[Dict[str, Any]]:
        rows = self.read_chain(underlying, as_of)
        if not rows:
            raise OptionsCacheMiss(
                f"No cached option chain for {underlying} @ {as_of}. Build it with "
                f"`ba2-test fetch-options --underlyings {underlying} --start ... --end ...`.")
        return rows

    def read_bar(self, occ_symbol: str, date: str) -> Optional[Dict[str, Any]]:
        with self._conn() as cx:
            row = cx.execute("SELECT * FROM option_bar WHERE occ_symbol=? AND date=?",
                             (occ_symbol, date)).fetchone()
            return dict(row) if row else None

    def latest_chain_as_of(self, underlying: str, on_or_before: str) -> Optional[str]:
        """Most recent cached chain date <= on_or_before (as-of clamp helper)."""
        with self._conn() as cx:
            row = cx.execute("SELECT MAX(as_of) AS d FROM option_chain "
                             "WHERE underlying=? AND as_of<=?", (underlying, on_or_before)).fetchone()
            return row["d"] if row and row["d"] else None

    def latest_bar_on_or_before(self, occ_symbol: str, on_or_before: str) -> Optional[Dict[str, Any]]:
        """Most recent cached bar for ``occ_symbol`` with date <= on_or_before (as-of clamp).

        Used to attach POINT-IN-TIME iv/greeks to a contract at an arbitrary as-of date — the
        static chain snapshot only carries greeks for the build's start date, but a backtest
        entry can land weeks/months later, so selection needs THIS bar's computed greeks, not
        the chain row's. Falls back to the nearest PRIOR trading day on a no-trade day, same
        sparsity every other bar-driven read (fills/MTM) already tolerates."""
        with self._conn() as cx:
            row = cx.execute(
                "SELECT * FROM option_bar WHERE occ_symbol=? AND date<=? "
                "ORDER BY date DESC LIMIT 1", (occ_symbol, on_or_before)).fetchone()
            return dict(row) if row else None
