"""OptionsHistoryCache must ADD the greeks columns to a pre-greeks cache file.

The shipped 10 GB cache on this machine has an ``option_bar`` table whose on-disk DDL
predates the greeks feature:

    (occ_symbol, date, open, high, low, close, volume, underlying, option_type, strike, expiry)

``_BAR_DDL`` declares ``iv, delta, gamma, theta, vega`` as well, but ``CREATE TABLE IF
NOT EXISTS`` is a NO-OP against an existing table, so the constructor left the file
unchanged and the first ``write_bar_rows`` died with

    OperationalError: table option_bar has no column named iv

That is what blocked re-fetching the cache with computed IV — and with no IV in the
cache, ``get_atm_iv`` returns None for every symbol and date, so the backtest's IV rank
can never be anything but None no matter how correct the rank code is.
"""
from __future__ import annotations

import sqlite3

import pytest

_OLD_BAR_DDL = (
    "CREATE TABLE option_bar("
    " occ_symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,"
    " underlying TEXT, option_type TEXT, strike TEXT, expiry TEXT,"
    " PRIMARY KEY(occ_symbol, date))"
)
_ROW = {
    "occ_symbol": "AAPL240315C00180000", "date": "2024-03-01",
    "open": 3.0, "high": 3.4, "low": 2.9, "close": 3.1, "volume": 120,
    "underlying": "AAPL", "option_type": "call", "strike": 180.0, "expiry": "2024-03-15",
    "iv": 0.2512, "delta": 0.55, "gamma": 0.04, "theta": -0.05, "vega": 0.10,
}


def _cols(path, table):
    cx = sqlite3.connect(path)
    try:
        return [r[1] for r in cx.execute(f"PRAGMA table_info({table})")]
    finally:
        cx.close()


@pytest.fixture
def legacy_cache_path(tmp_path):
    """A cache file whose option_bar predates the greeks columns."""
    path = str(tmp_path / "legacy.sqlite")
    cx = sqlite3.connect(path)
    cx.execute(_OLD_BAR_DDL)
    cx.commit()
    cx.close()
    return path


def test_constructor_adds_the_missing_greeks_columns(legacy_cache_path):
    from app.services.backtest.options_cache import OptionsHistoryCache

    assert "iv" not in _cols(legacy_cache_path, "option_bar")   # precondition
    OptionsHistoryCache(legacy_cache_path)
    assert set(_cols(legacy_cache_path, "option_bar")) >= {"iv", "delta", "gamma", "theta", "vega"}


def test_a_legacy_cache_can_be_written_to_after_the_upgrade(legacy_cache_path):
    """The actual crash, reproduced end to end."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(legacy_cache_path)
    cache.write_bar_rows([_ROW])

    cx = sqlite3.connect(legacy_cache_path)
    try:
        assert cx.execute("SELECT iv, delta FROM option_bar").fetchone() == (0.2512, 0.55)
    finally:
        cx.close()


def test_existing_rows_are_preserved_with_null_greeks(legacy_cache_path):
    """An ALTER must not drop the 63M bars already in the file; the added columns are
    simply NULL until a refetch backfills them (and NULL is honest: the greeks were
    never fetched, and get_atm_iv already treats a NULL iv as unusable)."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cx = sqlite3.connect(legacy_cache_path)
    cx.execute("INSERT INTO option_bar(occ_symbol, date, close, underlying)"
               " VALUES('OLD240315C00100000','2024-01-02', 1.5, 'OLD')")
    cx.commit()
    cx.close()

    OptionsHistoryCache(legacy_cache_path)

    cx = sqlite3.connect(legacy_cache_path)
    try:
        assert cx.execute("SELECT close, iv FROM option_bar WHERE underlying='OLD'"
                          ).fetchone() == (1.5, None)
    finally:
        cx.close()


def test_the_upgrade_is_idempotent(legacy_cache_path):
    """The constructor runs on every worker process and every fetch."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    OptionsHistoryCache(legacy_cache_path)
    before = _cols(legacy_cache_path, "option_bar")
    OptionsHistoryCache(legacy_cache_path)
    assert _cols(legacy_cache_path, "option_bar") == before


def test_a_column_added_by_a_racing_worker_is_not_an_error(legacy_cache_path):
    """GA workers open the shared cache concurrently. Two of them can both read
    PRAGMA table_info before either ALTERs, and the loser would get
    "duplicate column name: iv" and die on startup. The column existing is precisely
    the state we wanted, so it must be tolerated, not raised."""
    import sqlite3
    from app.services.backtest.options_cache import OptionsHistoryCache

    cx = sqlite3.connect(legacy_cache_path)

    class _Racing:
        """Delegates to a real connection, but lets the OTHER worker win once."""
        def __init__(self, real):
            self._real = real
            self._raced = False

        def execute(self, sql, *a):
            if sql.startswith("ALTER") and not self._raced:
                self._raced = True
                other = sqlite3.connect(legacy_cache_path)
                for col in ("iv", "delta", "gamma", "theta", "vega"):
                    other.execute(f"ALTER TABLE option_bar ADD COLUMN {col} REAL")
                other.commit()
                other.close()
            return self._real.execute(sql, *a)

    OptionsHistoryCache._add_missing_columns(
        _Racing(cx), "option_bar", ("iv", "delta", "gamma", "theta", "vega"))
    cx.close()

    assert set(_cols(legacy_cache_path, "option_bar")) >= {"iv", "delta", "gamma", "theta", "vega"}


def test_a_real_schema_failure_still_raises(legacy_cache_path):
    """Only "already there" is benign. A read-only file, a locked DB or a typo'd table
    must still blow up — swallowing every OperationalError would leave the cache
    silently un-upgraded and send us straight back to "no iv anywhere"."""
    import sqlite3
    from app.services.backtest.options_cache import OptionsHistoryCache

    cx = sqlite3.connect(f"file:{legacy_cache_path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            OptionsHistoryCache._add_missing_columns(cx, "option_bar", ("iv",))
    finally:
        cx.close()


def test_a_fresh_cache_still_gets_the_full_schema(tmp_path):
    from app.services.backtest.options_cache import OptionsHistoryCache

    path = str(tmp_path / "fresh.sqlite")
    OptionsHistoryCache(path)
    assert set(_cols(path, "option_bar")) >= {"iv", "delta", "gamma", "theta", "vega"}
    assert set(_cols(path, "option_chain")) >= {"iv", "delta", "gamma", "theta", "vega"}
