# FactorRanker: screener universes + 10 BA2NewStrat instances + e2e verify — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** (A) Let FactorRanker resolve its candidate universe from the StockScreener (not just static symbols); (B) create 10 varied FactorRanker instances on the **BA2NewStrat** account (5 static Nasdaq-50 + 5 screener-based, with different rank algos); (C) an end-to-end verification script that runs each instance against **real FMP data but places no orders**.

**Architecture:** FactorRanker keeps `instrument_selection_method="expert"` (single batch run). A new `universe_source` setting chooses how `_resolve_universe` builds the candidate pool: `static` (existing `enabled_instruments`) or `screener` (`StockScreener(self.settings).screen()`). Instances are created with NO execution schedule, so JobManager never auto-trades them. The e2e script monkeypatches `FactorPortfolioManager.rebalance` to a dry-run.

**Tech Stack:** Python, pytest, SQLModel, existing `core/StockScreener.py`, FMP providers.

**Context:** FactorRanker (one configurable expert) is already implemented and on `dev` (v689). User wants varied instances spanning screener + static-Nasdaq-50 universes and different rank algos. Key judgment call: the screener's **default rvol (1.05) and price_drop (15%) filters are penny-momentum-oriented and wrong for factor strategies** — every screener config below **disables them (0)** and selects on market cap / volume / price, sorting by market_cap or composite.

---

## Background the implementer needs

- `ba2_trade_platform/modules/experts/FactorRanker/__init__.py`:
  - `get_settings_definitions` (add `universe_source` here).
  - `_resolve_universe` (currently `list(self._get_enabled_instruments_config().keys())` + min_price guard) — branch on `universe_source`.
- `ba2_trade_platform/core/StockScreener.py`: `StockScreener(settings: dict, progress_callback=None).screen()` → `{"results": [ {"symbol": ...}, ... ], "stats": {...}}`. The screener_* setting keys it reads are already in the base `MarketExpertInterface.get_settings_definitions` (so they're in `self.settings`).
- Settings API on an expert instance: `expert.save_setting(key, value)` (typed via definitions), `expert.set_enabled_instruments({sym: {"enabled": True, "weight": 1.0}})`, `expert.settings` (read), `expert.get_setting_with_interface_default(key)`.
- Instance creation: `ExpertInstance(account_id, expert="FactorRanker", enabled=True, alias=..., virtual_equity_pct=...)` → `add_instance(...)` → `get_expert_instance_from_id(id, use_cache=False)`.
- BA2NewStrat = **account id 4** (resolve by name to be safe).
- Order-free e2e: monkeypatch `FactorRanker.portfolio.FactorPortfolioManager.rebalance` to compute deltas (via `get_holdings` + `rebalance_deltas`) and RETURN them WITHOUT submitting or creating Transactions.

---

## Part A — FactorRanker screener universe support

### Task A1: add `universe_source` setting

**File:** `modules/experts/FactorRanker/__init__.py` (`get_settings_definitions`)

Add:
```python
"universe_source": {
    "type": "str", "required": False, "default": "static",
    "choices": ["static", "screener"],
    "description": "Candidate universe: 'static' (enabled_instruments) or 'screener' (StockScreener filters).",
},
```
Commit: `git commit -m "feat(factorranker): add universe_source setting"`

### Task A2: `_screen_universe` helper (TDD)

**Files:** modify `__init__.py`; test `tests/test_factorranker_universe.py` (new)

**Step 1 — failing test** (patch StockScreener so no network):
```python
from unittest.mock import patch, MagicMock
from ba2_trade_platform.modules.experts.FactorRanker import __init__ as fr_mod

def test_screen_universe_returns_symbols(monkeypatch):
    fake = MagicMock()
    fake.screen.return_value = {"results": [{"symbol": "aapl"}, {"symbol": "MSFT"}, {"nope": 1}], "stats": {}}
    monkeypatch.setattr(fr_mod, "StockScreener", lambda settings, **k: fake)
    inst = fr_mod.FactorRanker.__new__(fr_mod.FactorRanker)   # bypass __init__/DB
    inst.logger = MagicMock()
    inst.settings = {"screener_market_cap_min": 1}            # any dict
    syms = inst._screen_universe()
    assert syms == ["AAPL", "MSFT"]      # uppercased, dicts without symbol skipped
```

**Step 2 — run, expect fail.**

**Step 3 — implement** (import `StockScreener` at module top: `from ....core.StockScreener import StockScreener`):
```python
def _screen_universe(self) -> List[str]:
    """Resolve the candidate universe by running the configured StockScreener."""
    try:
        result = StockScreener(dict(self.settings)).screen()
        syms = [r["symbol"].upper() for r in (result.get("results") or []) if r.get("symbol")]
        self.logger.info(f"FactorRanker: screener returned {len(syms)} candidates")
        return syms
    except Exception as e:
        self.logger.error(f"FactorRanker: screener universe resolution failed: {e}", exc_info=True)
        return []
```

**Step 4 — run, expect PASS. Step 5 — commit.**

### Task A3: route `_resolve_universe` on `universe_source` (TDD)

**Step 1 — failing test:** patch `_screen_universe` and `_get_enabled_instruments_config`; assert `_resolve_universe` uses the screener when `universe_source == "screener"`, else the static config. (Set `min_price` to 0 so the guard is a no-op; stub `get_setting_with_interface_default`.)

**Step 3 — implement:** at the top of `_resolve_universe`, before reading the static config:
```python
source = (self.get_setting_with_interface_default("universe_source") or "static").lower()
if source == "screener":
    universe = self._screen_universe()
else:
    universe = list(self._get_enabled_instruments_config().keys())
```
(Keep the existing `min_price` guard and `min_dollar_volume` note that follow.)

**Step 5 — commit:** `git commit -m "feat(factorranker): resolve universe from screener when configured"`

### Task A4: full FactorRanker suite green
Run `\.venv\Scripts\python.exe -m pytest tests/test_factorranker_*.py tests/test_factorranker_universe.py -q` — all pass.

---

## Part B — Nasdaq-50 static list

Use this constant (top ~50 Nasdaq-100 names) in the creation script:
```python
NASDAQ_50 = [
    "AAPL","MSFT","NVDA","AMZN","AVGO","META","GOOGL","GOOG","TSLA","COST",
    "NFLX","PLTR","CSCO","TMUS","AMD","PEP","LIN","INTU","TXN","ADBE",
    "QCOM","BKNG","AMGN","ISRG","HON","AMAT","GILD","CMCSA","ADP","VRTX",
    "PANW","ADI","MU","LRCX","REGN","MELI","KLAC","SBUX","CDNS","SNPS",
    "MAR","ORLY","CSX","ABNB","FTNT","ADSK","WDAY","NXPI","ROP","PCAR",
]
```

---

## Part C — the 10 configs

Common to all: `instrument_selection_method="expert"`, `virtual_equity_pct=10.0`, NO schedule.
Screener configs MUST disable penny filters: `screener_relative_volume_min=0`, `screener_price_drop_pct=0`.

| # | alias | universe_source | universe / screener filters | factor_weights | top_n | weighting | other |
|---|-------|-----------------|------------------------------|----------------|-------|-----------|-------|
| 1 | FR-N50-Momentum | static | Nasdaq-50 | {momentum:1} | 15 | equal | |
| 2 | FR-N50-Value | static | Nasdaq-50 | {value:1} | 15 | equal | |
| 3 | FR-N50-Quality | static | Nasdaq-50 | {quality:1} | 15 | equal | |
| 4 | FR-N50-MultiFactor | static | Nasdaq-50 | {momentum:1,value:1,quality:1} | 20 | equal | |
| 5 | FR-N50-MultiScore | static | Nasdaq-50 | {momentum:1,value:1,quality:1} | 20 | score | |
| 6 | FR-Scr-LargeCap-Multi | screener | mcap_min 10e9, price_min 10, vol_min 1e6, sort market_cap, max 50 | {momentum:1,value:1,quality:1} | 20 | equal | |
| 7 | FR-Scr-MidCap-Value | screener | mcap_min 2e9, mcap_max 20e9, vol_min 5e5, sort composite, max 60 | {value:1,quality:1} | 20 | equal | |
| 8 | FR-Scr-HighLiq-Momentum | screener | vol_min 5e6, mcap_min 5e9, sort volume, max 50 | {momentum:1} | 15 | equal | |
| 9 | FR-Scr-Broad-AllFactor | screener | mcap_min 1e9, price_min 5, vol_min 1e6, sort market_cap, max 80 | {momentum:1,value:1,quality:1,pead:0.5} | 25 | equal | pead_drift_window_days 60 |
| 10 | FR-Scr-Concentrated | screener | mcap_min 20e9, sort market_cap, max 40 | {momentum:1,value:1,quality:1} | 8 | equal | max_weight_per_name 0.20 |

For every screener row also set: `screener_relative_volume_min=0`, `screener_price_drop_pct=0`, `screener_price_min` as listed (else 0), `screener_volume_min`/`screener_market_cap_*`/`screener_sort_metric`/`screener_max_stocks` as listed.

---

## Part D — creation script (run once)

**File:** `test_files/create_factorranker_instances.py`

Structure:
1. Resolve account id by name `BA2NewStrat`.
2. Guard: if FactorRanker instances already exist on the account, print ids and exit (no dupes).
3. For each config: create `ExpertInstance(... alias, virtual_equity_pct=10.0)`, `add_instance`, `get_expert_instance_from_id(iid, use_cache=False)`, then:
   - `save_setting("instrument_selection_method", "expert")`, `save_setting("universe_source", source)`.
   - static → `set_enabled_instruments({s:{"enabled":True,"weight":1.0} for s in NASDAQ_50})`.
   - screener → `save_setting` each screener_* key from the config (including the two disabled penny filters).
   - `save_setting` each factor/rank override (factor_weights, top_n, weighting, max_weight_per_name, pead_drift_window_days...).
4. Print created ids + a one-instance verification round-trip (`settings`, `factor_weights`, universe size).
5. Print the safety note: no schedule set → no auto-trading until a schedule is added in the UI.

Run: `\.venv\Scripts\python.exe test_files\create_factorranker_instances.py`

---

## Part E — end-to-end verification script (NO orders)

**File:** `test_files/e2e_factorranker.py`

Behavior:
1. **Disable orders globally**: monkeypatch `FactorPortfolioManager.rebalance` to a dry-run that computes `held,_=self.get_holdings()`; prices via `self.account.get_instrument_current_price`; `deltas=rebalance_deltas(targets, held, prices, equity=self.expert.get_virtual_balance() or 0)`; log + return `deltas` WITHOUT submitting or creating Transactions. (Patch at `ba2_trade_platform.modules.experts.FactorRanker.portfolio.FactorPortfolioManager.rebalance`.)
2. For each FactorRanker `ExpertInstance` on BA2NewStrat:
   - Create a `MarketAnalysis` (subtype `ENTER_MARKET`, symbol `"EXPERT"`, expert_instance_id) via `add_instance`.
   - `expert = get_expert_instance_from_id(iid, use_cache=False)`; `expert.run_analysis("EXPERT", ma)`.
   - Capture from `ma.state["factor_ranker"]`: status, universe_size, held_count, top-10 ranking rows (symbol, composite, target_weight), and the dry-run `deltas` (would-be trades).
   - Print a per-instance report; collect failures.
3. Exit non-zero if any instance ended `FAILED` or produced an empty universe; print a summary table.

This hits real FMP (verifying the data adapters + screener end-to-end) but never submits an order. Expect: each instance resolves a universe, computes factors, ranks, and prints intended (un-placed) trades.

**Optional speed-up:** if FMP rate limits bite with 10 instances, memoize the `data.py` fetchers per-universe (fetch once, reuse) — note it but only if needed.

---

## Verification / acceptance

- Part A: new unit tests + full FactorRanker suite green; no new failures in the wider suite.
- Part D: script prints 10 created instance ids on account 4; re-running is a no-op.
- Part E: script runs all 10 with **zero** orders placed (confirm via Alpaca: no new orders) and prints sensible rankings/would-be trades.
- Bump `ba2_trade_platform/version.py` before any push.

## Notes / decisions
- Screener penny filters (rvol, price_drop) are disabled for factor universes — deliberate.
- Instances created WITHOUT a schedule for safety (BA2NewStrat is a live Alpaca account). Add monthly/weekly schedules in the UI when ready (see `2026-06-02-scheduler-monthly-extension.md`).
- `virtual_equity_pct=10` per instance (10 × 10% = 100%); adjust to taste.
