"""Offline historical options cache (sqlite). Mirrors the screener-history cache:
built once by `ba2-test fetch-options`, read-only at backtest time, fail-fast on miss."""
from __future__ import annotations
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

class OptionsHistoryCache:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as cx:
            cx.execute(_CHAIN_DDL); cx.execute(_BAR_DDL)
            # Speeds up options_provider.py's per-underlying chain load (the read-side worker
            # cache queries "WHERE underlying=?" once per underlying per worker process instead
            # of per bar/contract) -- idempotent, cheap no-op once built.
            cx.execute("CREATE INDEX IF NOT EXISTS idx_option_chain_underlying ON option_chain(underlying)")
            cx.execute("CREATE INDEX IF NOT EXISTS idx_option_bar_underlying ON option_bar(underlying)")

    def _conn(self):
        cx = sqlite3.connect(self.db_path); cx.row_factory = sqlite3.Row; return cx

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
