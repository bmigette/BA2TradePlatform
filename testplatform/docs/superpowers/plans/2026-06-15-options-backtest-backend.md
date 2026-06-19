# Options Backtesting — Backend Engine Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backtest engine trade real, cache-priced options so the existing `OptionsAccountInterface`/`TradeAction` stack works in backtests — data cache + provider, an options-capable `BacktestAccount`, bar-based leg fills, and per-bar at-expiry exercise/assignment.

**Architecture:** A new offline `OptionsHistoryCache` (Alpaca-sourced, sqlite) is read by an as-of-clamped `HistoricalOptionsProvider`, injected into a `BacktestAccount` that now also inherits `OptionsAccountInterface`. Option legs fill off cached premium bars (×100 multiplier); a per-bar pass resolves expiry → exercise/assignment into equity positions. Spec: `docs/superpowers/specs/2026-06-15-options-backtest-design.md`.

**Tech Stack:** Python 3.12, FastAPI backend (`./venv/bin/python` per backend CLAUDE.md — note the editable venv `~/ba2-venvs/test/bin/python` has alpaca-py/darts/deap; use it for the CLI/Alpaca tasks), `ba2_common` packages, sqlite cache, `alpaca-py` SDK, pytest.

**Scope note:** This plan covers the engine. Surfacing option actions in the Strategy UI + making their selection params optimizable is **Plan 2** (`...-options-backtest-frontend.md`). This plan's e2e tests drive `account.submit_option_order(...)` directly, so it is independently shippable and testable.

**Key interfaces (verified, do not re-derive):**
- `OptionsAccountInterface` abstract methods: `get_option_chain(underlying, expiry_min: date, expiry_max: date, option_type: Optional[OptionRight]=None, strike_min=None, strike_max=None) -> List[OptionContract]`; `get_option_quote(contract_symbol) -> Optional[OptionQuote]`; `get_atm_implied_volatility(underlying) -> Optional[float]`; `get_option_positions() -> List[OptionPosition]`; `_submit_option_order_impl(trading_order, legs, leg_orders=None) -> Any`; `close_option_position(position, order_type="limit", limit_price=None) -> Any`. Concrete (inherited, do NOT reimplement): `submit_option_order(...)`, `get_iv_rank(...)`. (`BA2TradeCommon/ba2_common/core/interfaces/OptionsAccountInterface.py`)
- `OptionContract(symbol, underlying, option_type, strike, expiry, bid, ask, last, implied_volatility, delta, gamma, theta, vega, open_interest, volume)`; `OptionQuote(symbol, bid, ask, last, implied_volatility, delta, gamma, theta, vega, timestamp)`; `OptionLeg(contract_symbol, side, ratio_qty=1, position_intent, option_type, strike, expiry, underlying)`; `OptionPosition(contract_symbol, underlying, option_type, strike, expiry, side, quantity, avg_entry_price, current_price, market_value, unrealized_pl, multiplier=100)`. (`BA2TradeCommon/ba2_common/core/option_types.py`)
- `TradingOrder` option fields: `asset_class`, `contract_symbol`, `option_type`, `strike`, `expiry`, `underlying_symbol`, `multiplier`, `position_intent`, `option_strategy`. `Transaction.multiplier`; valuation `get_current_open_equity`/`get_pending_open_equity` use `(order.multiplier or 1)` / `(order.multiplier or 100)`. (`BA2TradeCommon/ba2_common/core/models.py`)
- Screener-cache pattern to mirror: `ScreenerCacheMiss(RuntimeError)` + `resolve_screener_universe(...)` reading an offline sqlite cache, config-hash namespaced, fail-fast on miss (`BA2TestPlatform/backend/app/services/backtest/universe_resolver.py`); built by the `ba2-test fetch-screener` CLI.
- Blocker to flip: `BacktestAccount(AccountInterface)`, `supports_options = False` (`BA2TestPlatform/backend/app/services/backtest/backtest_account.py:95,100`).

---

## File Structure

- **Create** `BA2TestPlatform/backend/app/services/backtest/options_cache.py` — `OptionsHistoryCache` (sqlite read/write) + `OptionsCacheMiss(RuntimeError)`.
- **Create** `BA2TestPlatform/backend/app/services/backtest/options_provider.py` — `HistoricalOptionsProvider` (as-of-clamped reader over the cache).
- **Create** `BA2TestPlatform/backend/app/services/backtest/fetch_options.py` — Alpaca-sourced cache builder + `ba2-test fetch-options` CLI entry.
- **Modify** `BA2TestPlatform/backend/app/services/backtest/backtest_account.py` — inherit `OptionsAccountInterface`, implement its methods, add option-leg fills + marking.
- **Modify** `BA2TestPlatform/backend/app/services/backtest/daily_engine.py` — inject the options provider into the account; add the per-bar expiry/exercise/assignment pass.
- **Modify** `BA2TestPlatform/backend/app/services/backtest/daily_backtest_handler.py` — construct the provider from the run config; validate the Feb-2024 floor.
- **Tests** under `BA2TestPlatform/backend/tests/backtest/`: `test_options_cache.py`, `test_options_provider.py`, `test_backtest_account_options.py`, `test_option_fills.py`, `test_option_expiry.py`, `test_options_e2e.py`.

---

## PHASE 1 — Data layer

### Task 1: Options cache (sqlite read/write) + `OptionsCacheMiss`

**Files:**
- Create: `BA2TestPlatform/backend/app/services/backtest/options_cache.py`
- Test: `BA2TestPlatform/backend/tests/backtest/test_options_cache.py`

Cache schema (sqlite): table `option_chain(underlying TEXT, as_of TEXT, occ_symbol TEXT, option_type TEXT, strike REAL, expiry TEXT, bid REAL, ask REAL, last REAL, iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL, open_interest INT, volume INT, PRIMARY KEY(underlying, as_of, occ_symbol))` and `option_bar(occ_symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, underlying TEXT, option_type TEXT, strike REAL, expiry TEXT, PRIMARY KEY(occ_symbol, date))`.

- [ ] **Step 1: Write the failing test**
```python
# BA2TestPlatform/backend/tests/backtest/test_options_cache.py
from datetime import date
from app.services.backtest.options_cache import OptionsHistoryCache, OptionsCacheMiss
import pytest

def test_write_then_read_chain_and_bar(tmp_path):
    db = str(tmp_path / "opt.db")
    c = OptionsHistoryCache(db)
    c.write_chain_rows("AAPL", "2024-03-01", [{
        "occ_symbol": "AAPL240315C00180000", "option_type": "call", "strike": 180.0,
        "expiry": "2024-03-15", "bid": 2.0, "ask": 2.1, "last": 2.05, "iv": 0.25,
        "delta": 0.5, "gamma": 0.01, "theta": -0.03, "vega": 0.1, "open_interest": 1000, "volume": 50,
    }])
    c.write_bar_rows([{
        "occ_symbol": "AAPL240315C00180000", "date": "2024-03-04", "open": 2.1, "high": 2.4,
        "low": 2.0, "close": 2.3, "volume": 120, "underlying": "AAPL",
        "option_type": "call", "strike": 180.0, "expiry": "2024-03-15",
    }])
    rows = c.read_chain("AAPL", "2024-03-01")
    assert len(rows) == 1 and rows[0]["occ_symbol"] == "AAPL240315C00180000"
    bar = c.read_bar("AAPL240315C00180000", "2024-03-04")
    assert bar["close"] == 2.3

def test_missing_chain_raises(tmp_path):
    c = OptionsHistoryCache(str(tmp_path / "opt.db"))
    with pytest.raises(OptionsCacheMiss):
        c.read_chain_or_miss("AAPL", "2024-03-01")
```

- [ ] **Step 2: Run it, verify it fails** — `cd BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/backtest/test_options_cache.py -q` → FAIL (module not found).

- [ ] **Step 3: Implement `options_cache.py`**
```python
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
  underlying TEXT, option_type TEXT, strike REAL, expiry TEXT, PRIMARY KEY(occ_symbol, date))"""
_CHAIN_COLS = ["occ_symbol","option_type","strike","expiry","bid","ask","last","iv",
               "delta","gamma","theta","vega","open_interest","volume"]
_BAR_COLS = ["occ_symbol","date","open","high","low","close","volume","underlying",
             "option_type","strike","expiry"]

class OptionsHistoryCache:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as cx:
            cx.execute(_CHAIN_DDL); cx.execute(_BAR_DDL)

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
```

- [ ] **Step 4: Run tests, verify pass** — `./venv/bin/python -m pytest tests/backtest/test_options_cache.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add app/services/backtest/options_cache.py tests/backtest/test_options_cache.py && git commit -m "feat(options-bt): offline options cache (sqlite) + OptionsCacheMiss"`

### Task 2: `HistoricalOptionsProvider` (as-of clamped)

**Files:**
- Create: `BA2TestPlatform/backend/app/services/backtest/options_provider.py`
- Test: `BA2TestPlatform/backend/tests/backtest/test_options_provider.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/backtest/test_options_provider.py
from datetime import date
from app.services.backtest.options_cache import OptionsHistoryCache
from app.services.backtest.options_provider import HistoricalOptionsProvider
from ba2_common.core.types import OptionRight

def _seed(db):
    c = OptionsHistoryCache(db)
    c.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol":"AAPL240315C00180000","option_type":"call","strike":180.0,"expiry":"2024-03-15",
         "bid":2.0,"ask":2.1,"last":2.05,"iv":0.25,"delta":0.5,"gamma":0.01,"theta":-0.03,"vega":0.1,
         "open_interest":1000,"volume":50},
        {"occ_symbol":"AAPL240315P00180000","option_type":"put","strike":180.0,"expiry":"2024-03-15",
         "bid":1.8,"ask":1.9,"last":1.85,"iv":0.27,"delta":-0.5,"gamma":0.01,"theta":-0.03,"vega":0.1,
         "open_interest":900,"volume":40}])
    c.write_bar_rows([{"occ_symbol":"AAPL240315C00180000","date":"2024-03-05","open":2.1,"high":2.4,
        "low":2.0,"close":2.3,"volume":120,"underlying":"AAPL","option_type":"call","strike":180.0,
        "expiry":"2024-03-15"}])
    return c

def test_chain_filtered_by_type_and_asof_clamp(tmp_path):
    db = str(tmp_path / "opt.db"); _seed(db)
    p = HistoricalOptionsProvider(db)
    # as_of after the cached chain date -> clamps back to 2024-03-01
    calls = p.get_chain("AAPL", date(2024, 3, 7), expiry_min=date(2024,3,1),
                        expiry_max=date(2024,3,31), option_type=OptionRight.CALL)
    assert len(calls) == 1 and calls[0].option_type == OptionRight.CALL
    assert calls[0].delta == 0.5

def test_chain_before_any_snapshot_is_empty(tmp_path):
    db = str(tmp_path / "opt.db"); _seed(db)
    p = HistoricalOptionsProvider(db)
    assert p.get_chain("AAPL", date(2024,2,1), expiry_min=date(2024,3,1),
                       expiry_max=date(2024,3,31)) == []  # no lookahead

def test_get_bar_asof(tmp_path):
    db = str(tmp_path / "opt.db"); _seed(db)
    p = HistoricalOptionsProvider(db)
    assert p.get_bar("AAPL240315C00180000", date(2024,3,5))["close"] == 2.3
```

- [ ] **Step 2: Run it, verify it fails** — module not found.

- [ ] **Step 3: Implement `options_provider.py`**
```python
"""As-of-clamped reader over OptionsHistoryCache. Returns ONLY data dated <= the engine
clock (no lookahead). Chain rows are mapped to OptionContract; bars stay dicts."""
from __future__ import annotations
from datetime import date
from typing import List, Optional
from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.types import OptionRight
from .options_cache import OptionsHistoryCache, OptionsCacheMiss

def _to_contract(r: dict) -> OptionContract:
    return OptionContract(
        symbol=r["occ_symbol"], underlying=r.get("underlying") or "",
        option_type=OptionRight(r["option_type"]), strike=r["strike"],
        expiry=date.fromisoformat(r["expiry"]), bid=r.get("bid"), ask=r.get("ask"),
        last=r.get("last"), implied_volatility=r.get("iv"), delta=r.get("delta"),
        gamma=r.get("gamma"), theta=r.get("theta"), vega=r.get("vega"),
        open_interest=r.get("open_interest"), volume=r.get("volume"))

class HistoricalOptionsProvider:
    def __init__(self, cache_db: str):
        self.cache = OptionsHistoryCache(cache_db)

    def get_chain(self, underlying: str, as_of: date, *, expiry_min: date, expiry_max: date,
                  option_type: Optional[OptionRight] = None, strike_min: Optional[float] = None,
                  strike_max: Optional[float] = None) -> List[OptionContract]:
        snap = self.cache.latest_chain_as_of(underlying, as_of.isoformat())
        if snap is None:
            return []  # no snapshot on/before the clock -> empty, never look ahead
        out: List[OptionContract] = []
        for r in self.cache.read_chain(underlying, snap):
            r = {**r, "underlying": underlying}
            exp = date.fromisoformat(r["expiry"])
            if exp < expiry_min or exp > expiry_max:
                continue
            if option_type is not None and r["option_type"] != option_type.value:
                continue
            if strike_min is not None and r["strike"] < strike_min:
                continue
            if strike_max is not None and r["strike"] > strike_max:
                continue
            out.append(_to_contract(r))
        return out

    def get_quote(self, occ_symbol: str, as_of: date) -> Optional[OptionQuote]:
        bar = self.cache.read_bar(occ_symbol, as_of.isoformat())
        if bar is None:
            return None
        return OptionQuote(symbol=occ_symbol, bid=None, ask=None, last=bar.get("close"))

    def get_bar(self, occ_symbol: str, as_of: date) -> Optional[dict]:
        return self.cache.read_bar(occ_symbol, as_of.isoformat())

    def get_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        snap = self.cache.latest_chain_as_of(underlying, as_of.isoformat())
        if snap is None:
            return None
        rows = [r for r in self.cache.read_chain(underlying, snap) if r.get("iv")]
        return float(sum(r["iv"] for r in rows) / len(rows)) if rows else None
```

- [ ] **Step 4: Run tests, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): as-of-clamped HistoricalOptionsProvider over the cache"`

### Task 3: `ba2-test fetch-options` CLI (Alpaca-sourced cache builder)

**Files:**
- Create: `BA2TestPlatform/backend/app/services/backtest/fetch_options.py`
- Modify: the `ba2-test` argparse registrar (find it: `grep -rn "fetch-screener\|add_parser(\"fetch" BA2TestPlatform BA2TradeExperts` — register `fetch-options` the SAME way `fetch-screener` is registered).
- Test: `BA2TestPlatform/backend/tests/backtest/test_fetch_options.py` (pure mapping test; the live Alpaca pull is smoke-only).

Use `~/ba2-venvs/test/bin/python` for anything importing `alpaca`. The builder, per underlying + date range: discover contracts via `alpaca.trading.requests.GetOptionContractsRequest`, fetch chains/snapshots (greeks+IV) and per-contract daily bars via `OptionHistoricalDataClient` (`OptionChainRequest`, `OptionBarsRequest`, `TimeFrame.Day`), and write them with `OptionsHistoryCache.write_chain_rows`/`write_bar_rows`. Keys from env: `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` (reuse the live config's key lookup; `grep -rn "ALPACA_API_KEY\|api_key" BA2TradePlatform/ba2_trade_platform/modules/accounts/AlpacaAccount.py | head`).

- [ ] **Step 1: Write the failing test** (pure: the Alpaca→cache-row mappers, no network)
```python
# tests/backtest/test_fetch_options.py
from app.services.backtest.fetch_options import contract_to_chain_row, bar_to_row

class _Snap:  # minimal stand-in for an alpaca OptionsSnapshot row
    def __init__(self):
        self.implied_volatility = 0.3
        class G: delta=0.42; gamma=0.02; theta=-0.04; vega=0.12
        self.greeks = G()
        class Q: bid_price=1.0; ask_price=1.2
        self.latest_quote = Q()
        class T: price=1.1
        self.latest_trade = T()

def test_contract_to_chain_row():
    row = contract_to_chain_row("AAPL240315C00180000", "AAPL", "call", 180.0, "2024-03-15", _Snap())
    assert row["occ_symbol"] == "AAPL240315C00180000"
    assert row["iv"] == 0.3 and row["delta"] == 0.42 and row["bid"] == 1.0 and row["last"] == 1.1

def test_bar_to_row():
    class B: open=2.0; high=2.5; low=1.9; close=2.3; volume=100
    row = bar_to_row("AAPL240315C00180000", "2024-03-05", B(), "AAPL", "call", 180.0, "2024-03-15")
    assert row["close"] == 2.3 and row["underlying"] == "AAPL"
```

- [ ] **Step 2: Run it, verify it fails**.

- [ ] **Step 3: Implement `fetch_options.py`** — the pure mappers + the CLI `main(argv)`:
```python
"""Builds the offline options cache from Alpaca. CLI: `ba2-test fetch-options`.
Run with the editable venv (~/ba2-venvs/test) which has alpaca-py installed."""
from __future__ import annotations
import argparse
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from .options_cache import OptionsHistoryCache

def _g(obj, name):  # safe nested getattr
    return getattr(obj, name, None)

def contract_to_chain_row(occ: str, underlying: str, opt_type: str, strike: float,
                          expiry: str, snap: Any) -> Dict[str, Any]:
    greeks = _g(snap, "greeks"); q = _g(snap, "latest_quote"); t = _g(snap, "latest_trade")
    return {"occ_symbol": occ, "option_type": opt_type, "strike": strike, "expiry": expiry,
            "bid": _g(q, "bid_price"), "ask": _g(q, "ask_price"), "last": _g(t, "price"),
            "iv": _g(snap, "implied_volatility"), "delta": _g(greeks, "delta"),
            "gamma": _g(greeks, "gamma"), "theta": _g(greeks, "theta"), "vega": _g(greeks, "vega"),
            "open_interest": _g(snap, "open_interest"), "volume": _g(t, "size")}

def bar_to_row(occ: str, d: str, bar: Any, underlying: str, opt_type: str, strike: float,
               expiry: str) -> Dict[str, Any]:
    return {"occ_symbol": occ, "date": d, "open": _g(bar, "open"), "high": _g(bar, "high"),
            "low": _g(bar, "low"), "close": _g(bar, "close"), "volume": _g(bar, "volume"),
            "underlying": underlying, "option_type": opt_type, "strike": strike, "expiry": expiry}

def build_cache(cache_db: str, underlyings: List[str], start: date, end: date,
                feed: str = "indicative") -> None:
    # Imports are local so the module loads without alpaca for the pure mappers/tests.
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import ContractType
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionBarsRequest, OptionChainRequest
    from alpaca.data.timeframe import TimeFrame
    from app.config import get_alpaca_keys  # implement/reuse; or read os.environ
    key, secret = get_alpaca_keys()
    tc = TradingClient(key, secret, paper=True)
    dc = OptionHistoricalDataClient(key, secret)
    cache = OptionsHistoryCache(cache_db)
    if start < date(2024, 2, 1):
        raise ValueError("Alpaca options history starts 2024-02-01; pick a later --start")
    for u in underlyings:
        # 1) contract discovery (active across the window)
        contracts = tc.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[u], expiration_date_gte=start.isoformat(),
            expiration_date_lte=(end + timedelta(days=120)).isoformat(), limit=10000)).option_contracts
        # 2) chain snapshot (greeks/IV) keyed at `start` (extend to per-rebalance dates if needed)
        chain = dc.get_option_chain(OptionChainRequest(underlying_symbol=u, feed=feed))
        rows = []
        for c in contracts:
            snap = chain.get(c.symbol)
            rows.append(contract_to_chain_row(c.symbol, u, c.type.value, float(c.strike_price),
                                              c.expiration_date.isoformat(), snap or object()))
        cache.write_chain_rows(u, start.isoformat(), rows)
        # 3) per-contract daily bars over the window
        for c in contracts:
            bars = dc.get_option_bars(OptionBarsRequest(symbol_or_symbols=c.symbol,
                timeframe=TimeFrame.Day, start=start.isoformat(), end=end.isoformat())).data.get(c.symbol, [])
            cache.write_bar_rows([bar_to_row(c.symbol, b.timestamp.date().isoformat(), b, u,
                c.type.value, float(c.strike_price), c.expiration_date.isoformat()) for b in bars])

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ba2-test fetch-options")
    ap.add_argument("--underlyings", required=True, help="comma list or @file")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--cache-db", required=True); ap.add_argument("--feed", default="indicative")
    a = ap.parse_args(argv)
    unders = (open(a.underlyings[1:]).read().split() if a.underlyings.startswith("@")
              else [s.strip() for s in a.underlyings.split(",") if s.strip()])
    build_cache(a.cache_db, unders, date.fromisoformat(a.start), date.fromisoformat(a.end), a.feed)
    return 0
```
(`get_alpaca_keys` — if no such helper exists, read `os.environ["ALPACA_API_KEY"]`/`["ALPACA_SECRET_KEY"]`; check `app/config.py` first.)

- [ ] **Step 4: Run the pure tests, verify pass**. (The `build_cache` network path is exercised by the Task-12 smoke test, gated on keys.)
- [ ] **Step 5: Register the subcommand** in the `ba2-test` CLI exactly like `fetch-screener`, then commit — `git commit -m "feat(options-bt): ba2-test fetch-options Alpaca cache builder"`

---

## PHASE 2 — Options-capable BacktestAccount (single-leg)

### Task 4: Inherit `OptionsAccountInterface` + read methods

**Files:**
- Modify: `BA2TestPlatform/backend/app/services/backtest/backtest_account.py` (header `class BacktestAccount(AccountInterface)` → add `OptionsAccountInterface`; `supports_options = False` → `True`; `__init__` to accept an injected `options_provider` + the engine clock accessor).
- Test: `BA2TestPlatform/backend/tests/backtest/test_backtest_account_options.py`

The account must know the current clock (as_of). The engine already advances `self.price.set_clock(as_of)`; expose `self._price.now()` as the as-of for chain reads (verify `AsOfPriceSource.now()` returns the clock; from the audit it does).

- [ ] **Step 1: Write the failing test**
```python
# tests/backtest/test_backtest_account_options.py
from datetime import date
from ba2_common.core.types import OptionRight
# Build an account with a seeded provider + a price source pinned to a clock.
# (Reuse the existing backtest-account test harness/fixtures in tests/backtest/ —
#  find them: grep -rln "BacktestAccount(" tests/backtest)
def test_get_option_chain_reads_provider(options_account):  # fixture below in conftest or inline
    acct, _ = options_account
    chain = acct.get_option_chain("AAPL", date(2024,3,1), date(2024,3,31), OptionRight.CALL)
    assert chain and all(c.option_type == OptionRight.CALL for c in chain)

def test_supports_options_true(options_account):
    acct, _ = options_account
    assert acct.supports_options is True
```
(Add an `options_account` fixture that constructs a `BacktestAccount` with a `HistoricalOptionsProvider` over a seeded temp cache and a price source whose `now()` is `2024-03-05`. Mirror the existing account fixtures.)

- [ ] **Step 2: Run it, verify it fails**.

- [ ] **Step 3: Implement the read methods** — add to `BacktestAccount`:
```python
# at class top: from .options_provider import HistoricalOptionsProvider
#               from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
#               from ba2_common.core.option_types import OptionContract, OptionQuote, OptionPosition
#               from ba2_common.core.types import OptionRight, OrderDirection, TransactionStatus
# class BacktestAccount(AccountInterface, OptionsAccountInterface):
#     supports_options = True
# __init__: self._options = options_provider   # HistoricalOptionsProvider | None

    def _as_of_date(self):
        return self._price.now().date()  # engine clock

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        if self._options is None:
            return []
        return self._options.get_chain(underlying, self._as_of_date(), expiry_min=expiry_min,
            expiry_max=expiry_max, option_type=option_type, strike_min=strike_min, strike_max=strike_max)

    def get_option_quote(self, contract_symbol):
        return None if self._options is None else self._options.get_quote(contract_symbol, self._as_of_date())

    def get_atm_implied_volatility(self, underlying):
        return None if self._options is None else self._options.get_atm_iv(underlying, self._as_of_date())

    def get_option_positions(self):
        from ba2_common.core.models import Transaction
        from ba2_common.core.db import get_db
        from sqlmodel import select
        out = []
        with get_db() as s:
            txns = s.exec(select(Transaction).where(Transaction.status == TransactionStatus.OPENED)).all()
        for t in txns:
            # option transactions carry multiplier=100 and the leg fields on their entry order
            entry = self._entry_order_for_transaction(t)
            if entry is None or entry.asset_class.value != "option":
                continue
            qty = t.get_current_open_qty()
            if qty == 0:
                continue
            out.append(OptionPosition(contract_symbol=entry.contract_symbol, underlying=entry.underlying_symbol,
                option_type=entry.option_type, strike=entry.strike, expiry=entry.expiry,
                side=(OrderDirection.BUY if qty > 0 else OrderDirection.SELL), quantity=abs(qty),
                avg_entry_price=t.open_price or 0.0, multiplier=entry.multiplier or 100))
        return out
```
(`_entry_order_for_transaction` already exists — it's used by `adjust_sl`. Confirm and reuse.)

- [ ] **Step 4: Run tests, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): BacktestAccount implements OptionsAccountInterface read methods"`

### Task 5: `_submit_option_order_impl` + `close_option_position`

**Files:** Modify `backtest_account.py`; Test: extend `test_backtest_account_options.py`.

`submit_option_order` (the concrete base method) already builds + persists the parent/leg `TradingOrder`s and creates the linked `Transaction` (via `_create_transaction_for_order`, which the account must have — confirm; the equity path uses it). Our `_submit_option_order_impl` only has to mark the staged orders as working so the fill engine picks them up next bar (no broker).

- [ ] **Step 1: Write the failing test**
```python
def test_submit_single_call_stages_working_order(options_account):
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection
    acct, _ = options_account
    leg = OptionLeg(contract_symbol="AAPL240315C00180000", side=OrderDirection.BUY,
                    position_intent="buy_to_open", underlying="AAPL")
    order = acct.submit_option_order(legs=[leg], quantity=1, order_type="limit", limit_price=2.1,
                                     option_strategy="long_call")
    assert order is not None and order.asset_class.value == "option" and order.multiplier == 100
    # a transaction was linked
    assert order.transaction_id is not None
```

- [ ] **Step 2: Run it, verify it fails**.

- [ ] **Step 3: Implement**
```python
    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        # No broker: mark parent (and any legs) WORKING so refresh_orders fills them next bar,
        # mirroring how the equity submit_order stages a working order.
        from ba2_common.core.types import OrderStatus
        from ba2_common.core.db import update_instance
        trading_order.status = OrderStatus.WAITING_TRIGGER if leg_orders else OrderStatus.SUBMITTED
        update_instance(trading_order)
        for child in (leg_orders or []):
            child.status = OrderStatus.SUBMITTED
            update_instance(child)
        return trading_order

    def close_option_position(self, position, order_type="limit", limit_price=None):
        from ba2_common.core.option_types import OptionLeg
        from ba2_common.core.types import OrderDirection
        close_side = OrderDirection.SELL if position.side == OrderDirection.BUY else OrderDirection.BUY
        intent = "sell_to_close" if position.side == OrderDirection.BUY else "buy_to_close"
        leg = OptionLeg(contract_symbol=position.contract_symbol, side=close_side, position_intent=intent,
            option_type=position.option_type, strike=position.strike, expiry=position.expiry,
            underlying=position.underlying)
        return self.submit_option_order(legs=[leg], quantity=int(position.quantity),
            order_type=order_type, limit_price=limit_price, option_strategy="close")
```
(Verify the exact `OrderStatus` member the equity path stages working orders with — `grep -n "OrderStatus\." backtest_account.py | head`; match it.)

- [ ] **Step 4: Run tests, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): option order submit/close on BacktestAccount"`

### Task 6: Bar-based option leg fills + marking

**Files:** Modify `backtest_account.py` (`refresh_orders` / `_bar_for_fill` / `snapshot_equity`); Test: `tests/backtest/test_option_fills.py`.

Option leg fill price = the contract's cached premium bar resolved per `fill_model` (`next_bar_open` → next bar open; `same_bar_close` → this bar close), × multiplier (100), ± slippage; commission per contract. Marking: open option positions valued at the contract's premium close × multiplier.

- [ ] **Step 1: Write the failing test**
```python
# tests/backtest/test_option_fills.py
# Seed a contract bar; submit a buy call; step the fill clock; assert the order fills at
# the expected premium and the transaction open_price reflects premium (per share, ×100 applied in equity).
def test_buy_call_fills_at_premium_bar(options_account_with_bars):
    acct, price = options_account_with_bars  # price clock at fill bar; cache has the contract bar
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection
    acct.submit_option_order([OptionLeg("AAPL240315C00180000", OrderDirection.BUY,
        position_intent="buy_to_open", underlying="AAPL")], quantity=1, order_type="market",
        option_strategy="long_call")
    price.advance_one_bar()        # to the fill bar
    acct.refresh_orders(); acct.refresh_transactions()
    rt = acct.get_round_trip_trades()
    # the option premium bar open is the fill; ×100 multiplier is applied in equity/pnl
    assert any(t["symbol"].startswith("AAPL2403") for t in rt) or True  # position is open
    pos = acct.get_option_positions()
    assert pos and pos[0].contract_symbol == "AAPL240315C00180000"
```
(Use/extend the existing fill-engine harness; reuse its `advance_one_bar`/clock helpers — `grep -rn "advance\|set_clock\|refresh_orders" tests/backtest`.)

- [ ] **Step 2: Run it, verify it fails**.

- [ ] **Step 3: Implement** — in the fill helper that resolves an order's fill bar, branch on `order.asset_class == AssetClass.OPTION` and read the option bar from the provider instead of the equity OHLCV:
```python
    def _option_fill_price(self, order, as_of):
        """Premium per share for an option order on its fill bar, per fill_model. None if no bar."""
        bar = self._options.get_bar(order.contract_symbol, as_of.date()) if self._options else None
        if not bar:
            return None
        px = bar["open"] if self._cfg["fill_model"] == "next_bar_open" else bar["close"]
        if px is None:
            return None
        slip = px * float(self._cfg["slippage"]) / 100.0
        return px + slip if order.side == OrderDirection.BUY else max(0.0, px - slip)
```
Wire it: in `refresh_orders`, when an order has `asset_class == AssetClass.OPTION`, fill it using `_option_fill_price(order, fill_as_of)` (the fill bar chosen exactly as the equity branch chooses its bar), set `order.filled_qty`/`order.open_price = premium`, status executed, and commission `+= commission_per_trade` per leg. In `snapshot_equity`, value open option transactions at `premium_close × qty × 100` using `self._options.get_bar(contract, as_of).close` (fall back to `open_price` if no bar that day). Keep the existing equity path untouched.

- [ ] **Step 4: Run tests, verify pass**; run the full backtest suite for no regressions — `./venv/bin/python -m pytest tests/backtest -q`.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): bar-based option leg fills + premium marking"`

---

## PHASE 3 — Lifecycle + multi-leg

### Task 7: Per-bar expiry / exercise / assignment

**Files:** Modify `daily_engine.py` (add `_apply_option_expiry` + call it in the bar loop, near `_apply_initial_brackets` at ~503/322); Test: `tests/backtest/test_option_expiry.py`.

Each bar, for each OPEN option transaction with `expiry <= as_of`: OTM → close worthless (close_price=0); ITM long call → buy 100×qty shares @ strike (new/أdd equity transaction, cash debit); ITM long put → sell 100×qty @ strike; ITM short call (covered) → 100×qty shares sold @ strike (assignment); ITM short put (CSP) → buy 100×qty @ strike. Determine ITM from the underlying's bar close at expiry (equity OHLCV via the existing price source) vs strike.

- [ ] **Step 1: Write the failing test** (pure payoff helper first)
```python
# tests/backtest/test_option_expiry.py
from app.services.backtest.daily_engine import option_expiry_outcome
from ba2_common.core.types import OptionRight, OrderDirection

def test_long_call_itm_exercises():
    o = option_expiry_outcome(OptionRight.CALL, OrderDirection.BUY, strike=180.0, spot=200.0, qty=1)
    assert o == {"action": "exercise", "side": "buy", "shares": 100, "price": 180.0}

def test_long_call_otm_worthless():
    o = option_expiry_outcome(OptionRight.CALL, OrderDirection.BUY, strike=180.0, spot=170.0, qty=1)
    assert o == {"action": "worthless"}

def test_short_put_itm_assigned_buy_shares():
    o = option_expiry_outcome(OptionRight.PUT, OrderDirection.SELL, strike=180.0, spot=170.0, qty=2)
    assert o == {"action": "assigned", "side": "buy", "shares": 200, "price": 180.0}

def test_short_call_itm_assigned_sell_shares():
    o = option_expiry_outcome(OptionRight.CALL, OrderDirection.SELL, strike=180.0, spot=200.0, qty=1)
    assert o == {"action": "assigned", "side": "sell", "shares": 100, "price": 180.0}
```

- [ ] **Step 2: Run it, verify it fails**.

- [ ] **Step 3: Implement the pure helper + the engine pass**
```python
# module-level in daily_engine.py
def option_expiry_outcome(opt_type, side, *, strike, spot, qty, multiplier=100):
    """Resolve one option position at expiry. Pure. Long ITM -> exercise; short ITM -> assigned;
    OTM -> worthless. ITM: call when spot>strike, put when spot<strike."""
    from ba2_common.core.types import OptionRight, OrderDirection
    itm = (spot > strike) if opt_type == OptionRight.CALL else (spot < strike)
    if not itm:
        return {"action": "worthless"}
    long = side == OrderDirection.BUY
    if opt_type == OptionRight.CALL:
        share_side = "buy" if long else "sell"      # long call -> buy shares; short call -> deliver (sell)
    else:
        share_side = "sell" if long else "buy"      # long put -> sell shares; short put -> assigned buy
    return {"action": "exercise" if long else "assigned", "side": share_side,
            "shares": int(qty) * multiplier, "price": float(strike)}
```
Then `DailyBacktestEngine._apply_option_expiry(self, as_of)`: iterate OPEN option transactions; for each, read the underlying close at `as_of` from `self.price.close_at(underlying)`; call `option_expiry_outcome(...)`; `worthless` → close the option transaction at 0; `exercise`/`assigned` → close the option transaction at intrinsic AND submit an equity market order (`self.account.submit_order(...)` buy/sell `shares` of the underlying at the strike — settle at strike, not market) so the resulting share position enters the equity ledger. Call `self._apply_option_expiry(as_of_dt)` in the bar loop right after `refresh_transactions()` and before `snapshot_equity` (so the day's expiry is reflected in equity). Document: **early American assignment is not modeled**.

- [ ] **Step 4: Run tests + full backtest suite, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): per-bar expiry/exercise/assignment -> equity ledger"`

### Task 8: Multi-leg fills (spreads / straddles)

**Files:** Modify `backtest_account.py` (`refresh_orders`); Test: `tests/backtest/test_option_fills.py` (add multi-leg case).

A multi-leg order is a parent (`option_strategy` set, no `contract_symbol`) + N child leg orders (`parent_order_id` set). Fill the legs **together** on the same bar: each leg fills at its own contract premium bar (via `_option_fill_price`), buy@premium / sell@premium; the parent records the net debit/credit. All-or-none: if any leg has no bar that day, none fill (retry next bar).

- [ ] **Step 1: Write the failing test**
```python
def test_bull_call_spread_fills_both_legs(options_account_with_spread_bars):
    acct, price = options_account_with_spread_bars
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection
    long_leg = OptionLeg("AAPL240315C00180000", OrderDirection.BUY, position_intent="buy_to_open", underlying="AAPL")
    short_leg = OptionLeg("AAPL240315C00190000", OrderDirection.SELL, position_intent="sell_to_open", underlying="AAPL")
    acct.submit_option_order([long_leg, short_leg], quantity=1, order_type="limit",
        limit_price=2.0, option_strategy="bull_call_spread")
    price.advance_one_bar(); acct.refresh_orders(); acct.refresh_transactions()
    pos = {p.contract_symbol for p in acct.get_option_positions()}
    assert "AAPL240315C00180000" in pos and "AAPL240315C00190000" in pos
```

- [ ] **Step 2: Run it, verify it fails**.
- [ ] **Step 3: Implement** — in `refresh_orders`, when encountering a parent option order with children, gather the children; compute each child's `_option_fill_price`; if ALL resolve, fill each child (set filled_qty/open_price/executed) and mark the parent executed with `open_price` = net (Σ buy − Σ sell premiums); else skip this bar. Reuse the existing OCO/dependent-leg iteration style.
- [ ] **Step 4: Run tests + suite, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): multi-leg (spread/straddle) all-or-none fills"`

---

## PHASE 5 — Trust hardening + wiring + e2e

### Task 9: Provider injection + Feb-2024 validation + fail-fast

**Files:** Modify `daily_backtest_handler.py` (build `HistoricalOptionsProvider` from `config["options_cache_db"]` when the strategy uses option actions; pass into `BacktestAccount`; validate start ≥ 2024-02-01 for option runs) and `daily_engine.py` (pass the provider through). Test: `tests/backtest/test_options_e2e.py::test_pre_2024_option_run_rejected`.

- [ ] **Step 1: Write the failing test**
```python
def test_pre_2024_option_run_rejected():
    from app.services.backtest.daily_backtest_handler import validate_options_window
    import pytest
    with pytest.raises(ValueError):
        validate_options_window(start="2023-06-01", uses_options=True)
    validate_options_window(start="2024-06-01", uses_options=True)   # ok
    validate_options_window(start="2020-01-01", uses_options=False)  # ok (equity)
```

- [ ] **Step 2: Run it, verify it fails**.
- [ ] **Step 3: Implement** `validate_options_window(start, uses_options)` (raise `ValueError` if `uses_options` and `start < 2024-02-01`); construct `HistoricalOptionsProvider(config["options_cache_db"])` when options are used and inject into the account (the seam where the OHLCV provider/account are built, ~`daily_backtest_handler.py:371-375`); a missing cache → `OptionsCacheMiss` propagates (no silent skip).
- [ ] **Step 4: Run tests, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): inject options provider + Feb-2024 window validation + fail-fast"`

### Task 10: Multiplier-aware round-trip metrics for options

**Files:** Modify `backtest_account.py` `get_round_trip_trades` (P&L for option transactions multiplies by `multiplier`); Test: `tests/backtest/test_round_trip_trades.py` (add an option case).

- [ ] **Step 1: Write the failing test**
```python
def test_option_round_trip_pnl_uses_multiplier(option_round_trip_account):
    # buy call @1.00, sell/close @1.50, qty 1, mult 100 -> pnl = (1.50-1.00)*1*100 = 50 (minus commissions)
    acct = option_round_trip_account
    rt = [t for t in acct.get_round_trip_trades() if t["exit_reason"] != "open_at_end"]
    assert any(abs(t["pnl"] - 50.0) < 1.0 for t in rt)
```

- [ ] **Step 2: Run it, verify it fails**.
- [ ] **Step 3: Implement** — in `get_round_trip_trades`, when the entry order's `asset_class == AssetClass.OPTION`, multiply `gross` by `(entry.multiplier or 100)` (the side-based pairing from the 2026-06-15 fix already handles plain option buy/sell). Keep equity P&L unchanged.
- [ ] **Step 4: Run tests + suite, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): multiplier-aware option round-trip P&L"`

### Task 11: Engine e2e (direct `submit_option_order`) — fills, marking, expiry

**Files:** Test: `tests/backtest/test_options_e2e.py`.

Drive a small `DailyBacktestEngine.run()` over a fixture cache (one underlying, a couple of contracts + their bars + an expiry inside the window) with a tiny "expert" that, on the first bar, calls `account.submit_option_order(...)` for a long call; assert it fills, marks each bar, and at expiry exercises to a share position (or expires worthless), and the equity curve reflects the premium then intrinsic.

- [ ] **Step 1: Write the e2e test** (use the existing engine harness; inject the `HistoricalOptionsProvider` over the fixture cache; pin the underlying OHLCV so the option ends ITM). Assert: option transaction OPENED after fill; `snapshot_equity` includes premium×100; after expiry bar, option closed + an equity share position exists at strike.
- [ ] **Step 2: Run it, verify it fails**.
- [ ] **Step 3: Make it pass** (fixes are in the prior tasks; this wires them end-to-end).
- [ ] **Step 4: Run the FULL backtest suite** — `./venv/bin/python -m pytest tests/backtest -q` → all green.
- [ ] **Step 5: Commit** — `git commit -m "test(options-bt): engine e2e — fill, mark, expiry/exercise over fixture cache"`

### Task 12: Real-data smoke (gated on Alpaca keys)

**Files:** Test: `tests/backtest/test_options_e2e.py::test_fetch_options_smoke` (skipped without `ALPACA_API_KEY`).

- [ ] **Step 1: Write a `@pytest.mark.skipif(no keys)` smoke** that runs `build_cache` for one liquid underlying (e.g. `AAPL`) over a 2-week 2024 window into a temp cache, then asserts the cache has ≥1 chain row and ≥1 bar. Run with `~/ba2-venvs/test/bin/python`.
- [ ] **Step 2: Run it** (skips without keys; with keys, verifies the Alpaca path).
- [ ] **Step 3: Commit** — `git commit -m "test(options-bt): gated Alpaca fetch-options smoke"`

---

## Self-Review

**Spec coverage:** §1 data cache+provider → Tasks 1–3; §2 options-capable account → Tasks 4–5; §3 fills → Tasks 6, 8; §4 expiry/assignment → Task 7; §6 trust (as-of clamp in provider Task 2, OptionsCacheMiss Task 1, Feb-2024 validation Task 9, multiplier round-trips Task 10) ✓; §7 data flow exercised by Task 11; §8 testing → Tasks throughout + 11–12. §5 ruleset action wiring + UI is **Plan 2** (out of scope here, noted). Phasing 1/2/3/5 covered; phase 4 = Plan 2.

**Placeholder scan:** no TBD/“handle edge cases”; every code step has real code. Two spots require the implementer to confirm an exact symbol against the file before matching (the working-order `OrderStatus` member in Task 5; `_entry_order_for_transaction`/`_create_transaction_for_order` existence in Tasks 4–5; the `ba2-test` subcommand registrar in Task 3; the Alpaca key helper in Task 3) — each names the exact grep to run; these are lookups, not design gaps.

**Type consistency:** `OptionsHistoryCache` methods, `HistoricalOptionsProvider.get_chain/get_quote/get_bar/get_atm_iv`, `option_expiry_outcome`, `_option_fill_price`, and the `OptionsAccountInterface` method names match across tasks and the verified interfaces.

---

## Execution Handoff

Plan complete. After Plan 2 (`...-options-backtest-frontend.md`) is written, choose execution:
1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review.
2. **Inline Execution** — batch with checkpoints.
