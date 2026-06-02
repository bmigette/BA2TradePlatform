# FactorRanker Expert — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `FactorRanker`, a configurable cross-sectional multi-factor equity expert (momentum / post-earnings-drift / value / quality) that ranks a universe each rebalance and holds the top slice via a dedicated portfolio manager.

**Architecture:** A plain `MarketExpertInterface` expert using `instrument_selection_method="expert"` + `should_expand_instrument_jobs=False`, so JobManager runs a single `run_analysis("EXPERT", ma)` per rebalance. Inside, it resolves a universe, bulk-fetches factor inputs, computes per-factor scores, z-scores + weights them into a composite, ranks, builds long-only target weights, and a `FactorPortfolioManager` diffs targets vs holdings and submits buy/sell orders directly (no `ExpertRecommendation`, no `SmartRiskManager`). Pure logic (factors, combine, construction, rebalance math) is isolated for unit testing.

**Tech Stack:** Python, pandas/numpy, SQLModel, pytest, NiceGUI. Data via existing FMP providers (`modules/dataproviders/ohlcv/FMPOHLCVProvider.py`, `fundamentals/.../FMPCompanyOverviewProvider.py`, `screener/FMPScreenerProvider.py`).

**Design doc:** `docs/plans/2026-06-02-factorranker-design.md`. **Depends on:** `2026-06-02-scheduler-monthly-extension.md` (for monthly cadence; weekly works without it).

---

## Background the implementer needs (read these first)

- **Expert template:** `modules/experts/FMPRating.py` — full example of `get_settings_definitions`, `run_analysis`, `_create_*`, `render_market_analysis`, status handling (`MarketAnalysisStatus.RUNNING/COMPLETED/FAILED/SKIPPED`), and `AnalysisOutput` writes.
- **Direct execution template:** `modules/experts/PennyMomentumTrader/trade_manager.py` — how to build a `TradingOrder` and submit via `self.account.submit_order(order, is_closing_order=...)`, plus `data={"fixed_quantity": True}` on deliberate sizes.
- **Self-contained scheduling props:** `FMPSenateTraderCopy.get_settings_definitions` includes `"should_expand_instrument_jobs": False`. FactorRanker also sets `instrument_selection_method` default `"expert"` and (new) `"schedules_open_positions": False`.
- **Base interface:** `core/interfaces/MarketExpertInterface.py` — abstract methods: `get_settings_definitions`, `description`, `run_analysis(symbol, market_analysis)`, `render_market_analysis(market_analysis)`. Helpers: `self.settings`, `self.get_setting_with_interface_default`, `self._get_current_price`, `self.get_enabled_instruments()`, `self.account` (via `get_account_instance_from_id`).
- **Universe resolution:** `get_enabled_instruments()` already returns the resolved list for static/label/screener/AI selection. For FactorRanker the candidate pool to *rank* is this list.
- **Models:** `TradingOrder`, `Transaction`, `MarketAnalysis`, `AnalysisOutput` in `core/models.py`. `Transaction.get_current_open_qty()` gives held shares.

**Conventions (CLAUDE.md):** confidence 1-100; no default fallbacks for prices/qty (raise instead); `logger` with `exc_info=True` only inside `except`; explicit dict access for config.

---

## Task 1: Factor module skeleton + momentum (pure)

**Files:**
- Create: `ba2_trade_platform/modules/experts/FactorRanker/__init__.py` (empty package marker for now)
- Create: `ba2_trade_platform/modules/experts/FactorRanker/factors.py`
- Test: `tests/test_factorranker_factors.py`

**Step 1 — failing test:**
```python
# tests/test_factorranker_factors.py
import pandas as pd
from ba2_trade_platform.modules.experts.FactorRanker.factors import momentum_12_1

def test_momentum_12_1_basic():
    # 260 trading days; AAA doubled over the 12->1 month window, BBB flat
    idx = pd.RangeIndex(260)
    aaa = pd.Series([100.0]*8 + list(range(100, 352)), index=idx)[:260]  # rising
    bbb = pd.Series([100.0]*260, index=idx)
    out = momentum_12_1({"AAA": aaa, "BBB": bbb})
    assert out["BBB"] == 0.0
    assert out["AAA"] > 0.0  # positive 12-1 momentum
    assert set(out) == {"AAA", "BBB"}

def test_momentum_skips_recent_month():
    # A spike only in the last 21 days must NOT count (12-1 skips last month)
    idx = pd.RangeIndex(260)
    flat_then_spike = pd.Series([100.0]*239 + [200.0]*21, index=idx)
    out = momentum_12_1({"X": flat_then_spike})
    assert out["X"] == 0.0
```

**Step 2 — run, expect ImportError:** `\.venv\Scripts\python.exe -m pytest tests/test_factorranker_factors.py -q`

**Step 3 — implement:**
```python
# factors.py
from typing import Dict
import pandas as pd

def momentum_12_1(prices: Dict[str, pd.Series], lookback: int = 252, skip: int = 21) -> Dict[str, float]:
    """12-1 month total return: P[-skip] / P[-lookback] - 1. Skips the most recent
    `skip` days to avoid short-term reversal. Symbols with insufficient history are 0."""
    out: Dict[str, float] = {}
    for sym, s in prices.items():
        s = s.dropna()
        if len(s) < lookback:
            out[sym] = 0.0
            continue
        p_start = float(s.iloc[-lookback])
        p_end = float(s.iloc[-skip - 1])
        out[sym] = (p_end / p_start - 1.0) if p_start > 0 else 0.0
    return out
```

**Step 4 — run, expect PASS.** **Step 5 — commit:** `git commit -m "feat(factorranker): momentum 12-1 factor"`

---

## Task 2: PEAD (standardized earnings surprise, drift-window gated) (pure)

**Files:** Modify `factors.py`; Test `tests/test_factorranker_factors.py`

**Step 1 — failing test:**
```python
from ba2_trade_platform.modules.experts.FactorRanker.factors import earnings_surprise

def test_earnings_surprise_within_window():
    data = {
        "A": {"actual": 1.2, "estimate": 1.0, "estimate_std": 0.1, "days_since": 5},
        "B": {"actual": 0.9, "estimate": 1.0, "estimate_std": 0.1, "days_since": 5},
        "C": {"actual": 1.5, "estimate": 1.0, "estimate_std": 0.1, "days_since": 90},  # stale
    }
    out = earnings_surprise(data, drift_window_days=60)
    assert round(out["A"], 1) == 2.0   # (1.2-1.0)/0.1
    assert round(out["B"], 1) == -1.0
    assert out["C"] == 0.0             # outside drift window -> no signal
```

**Step 3 — implement:**
```python
def earnings_surprise(data: Dict[str, dict], drift_window_days: int = 60) -> Dict[str, float]:
    """Standardized unexpected earnings (SUE), zeroed outside the post-earnings drift window."""
    out: Dict[str, float] = {}
    for sym, d in data.items():
        days = d.get("days_since")
        std = d.get("estimate_std") or 0.0
        if days is None or days > drift_window_days or std <= 0:
            out[sym] = 0.0
            continue
        out[sym] = (float(d["actual"]) - float(d["estimate"])) / std
    return out
```

**Step 4 — PASS. Step 5 — commit:** `git commit -m "feat(factorranker): PEAD earnings-surprise factor"`

---

## Task 3: Value factor (earnings yield + FCF/EV) (pure)

**Files:** Modify `factors.py`; Test.

**Step 1 — failing test:**
```python
from ba2_trade_platform.modules.experts.FactorRanker.factors import value_score
def test_value_score_cheaper_is_higher():
    data = {
        "CHEAP": {"eps_ttm": 5.0, "price": 50.0, "fcf_ttm": 10.0, "enterprise_value": 100.0},
        "RICH":  {"eps_ttm": 1.0, "price": 100.0, "fcf_ttm": 1.0, "enterprise_value": 500.0},
    }
    out = value_score(data)
    assert out["CHEAP"] > out["RICH"]
```

**Step 3 — implement** (equal-weight of earnings yield E/P and FCF/EV; missing inputs contribute 0):
```python
def value_score(data: Dict[str, dict]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for sym, d in data.items():
        ey = (d["eps_ttm"] / d["price"]) if d.get("eps_ttm") and d.get("price") else 0.0
        fcfy = (d["fcf_ttm"] / d["enterprise_value"]) if d.get("fcf_ttm") and d.get("enterprise_value") else 0.0
        out[sym] = 0.5 * ey + 0.5 * fcfy
    return out
```

**Step 5 — commit:** `git commit -m "feat(factorranker): value factor"`

---

## Task 4: Quality factor (ROE + gross profitability − accruals) (pure)

**Files:** Modify `factors.py`; Test.

**Step 1 — failing test:**
```python
from ba2_trade_platform.modules.experts.FactorRanker.factors import quality_score
def test_quality_score_higher_for_profitable_low_accruals():
    data = {
        "GOOD": {"roe": 0.25, "gross_profit": 50.0, "total_assets": 100.0, "accruals_ratio": 0.02},
        "BAD":  {"roe": 0.02, "gross_profit": 5.0,  "total_assets": 100.0, "accruals_ratio": 0.20},
    }
    out = quality_score(data)
    assert out["GOOD"] > out["BAD"]
```

**Step 3 — implement:**
```python
def quality_score(data: Dict[str, dict]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for sym, d in data.items():
        roe = d.get("roe") or 0.0
        gp = (d["gross_profit"] / d["total_assets"]) if d.get("gross_profit") and d.get("total_assets") else 0.0
        accr = d.get("accruals_ratio") or 0.0
        out[sym] = roe + gp - accr
    return out
```

**Step 5 — commit:** `git commit -m "feat(factorranker): quality factor"`

---

## Task 5: Cross-sectional z-score + weighted composite + rank (pure)

**Files:** Modify `factors.py`; Test `tests/test_factorranker_combine.py` (new)

**Step 1 — failing tests:**
```python
from ba2_trade_platform.modules.experts.FactorRanker.factors import (
    cross_sectional_zscore, composite_score, rank_symbols,
)

def test_zscore_centers_and_scales():
    z = cross_sectional_zscore({"A": 1.0, "B": 2.0, "C": 3.0})
    assert round(z["B"], 6) == 0.0          # B is the mean
    assert z["A"] < 0 < z["C"]

def test_composite_weights_and_zero_weight_disables():
    factors = {
        "momentum": {"A": 3.0, "B": 1.0},
        "value":    {"A": 1.0, "B": 3.0},
    }
    comp = composite_score(factors, weights={"momentum": 1.0, "value": 0.0})
    # value disabled -> A (high momentum) ranks above B
    assert comp["A"] > comp["B"]

def test_rank_descending():
    assert rank_symbols({"A": 0.1, "B": 0.9, "C": 0.5}) == ["B", "C", "A"]
```

**Step 3 — implement:**
```python
import numpy as np

def cross_sectional_zscore(values: Dict[str, float], winsorize_pct: float = 0.0) -> Dict[str, float]:
    syms = list(values)
    arr = np.array([values[s] for s in syms], dtype=float)
    if winsorize_pct > 0 and len(arr) > 2:
        lo, hi = np.quantile(arr, [winsorize_pct, 1 - winsorize_pct])
        arr = np.clip(arr, lo, hi)
    mu, sd = arr.mean(), arr.std()
    z = (arr - mu) / sd if sd > 0 else np.zeros_like(arr)
    return {s: float(z[i]) for i, s in enumerate(syms)}

def composite_score(factor_values: Dict[str, Dict[str, float]], weights: Dict[str, float],
                    winsorize_pct: float = 0.0) -> Dict[str, float]:
    symbols = set().union(*[set(v) for v in factor_values.values()]) if factor_values else set()
    out = {s: 0.0 for s in symbols}
    for fname, vals in factor_values.items():
        w = weights.get(fname, 0.0)
        if w == 0.0:
            continue
        z = cross_sectional_zscore(vals, winsorize_pct)
        for s in symbols:
            out[s] += w * z.get(s, 0.0)
    return out

def rank_symbols(composite: Dict[str, float]) -> list:
    return [s for s, _ in sorted(composite.items(), key=lambda kv: kv[1], reverse=True)]
```

**Step 5 — commit:** `git commit -m "feat(factorranker): z-score, composite, rank"`

---

## Task 6: Long-only top-N construction → target weights (pure)

**Files:** Create `ba2_trade_platform/modules/experts/FactorRanker/construction.py`; Test `tests/test_factorranker_construction.py`

**Step 1 — failing tests:**
```python
from ba2_trade_platform.modules.experts.FactorRanker.construction import long_only_top_n

def test_equal_weight_top_n_caps_and_sums_to_gross():
    ranked = ["A", "B", "C", "D"]
    scores = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.5}
    w = long_only_top_n(ranked, scores, top_n=2, weighting="equal",
                        max_weight_per_name=1.0, gross_exposure=1.0)
    assert set(w) == {"A", "B"}
    assert round(sum(w.values()), 6) == 1.0
    assert round(w["A"], 6) == round(w["B"], 6) == 0.5

def test_cap_applies():
    ranked = ["A", "B", "C"]
    scores = {"A": 3.0, "B": 2.0, "C": 1.0}
    w = long_only_top_n(ranked, scores, top_n=3, weighting="equal",
                        max_weight_per_name=0.25, gross_exposure=1.0)
    assert all(v <= 0.25 + 1e-9 for v in w.values())
```

**Step 3 — implement:**
```python
from typing import Dict, List

def long_only_top_n(ranked: List[str], scores: Dict[str, float], top_n: int,
                    weighting: str = "equal", max_weight_per_name: float = 1.0,
                    gross_exposure: float = 1.0) -> Dict[str, float]:
    picks = ranked[:top_n]
    if not picks:
        return {}
    if weighting == "score":
        raw = {s: max(scores.get(s, 0.0), 0.0) for s in picks}
        total = sum(raw.values()) or 1.0
        w = {s: gross_exposure * raw[s] / total for s in picks}
    else:  # equal
        w = {s: gross_exposure / len(picks) for s in picks}
    # apply per-name cap, then renormalize to gross_exposure
    w = {s: min(v, max_weight_per_name) for s, v in w.items()}
    total = sum(w.values()) or 1.0
    return {s: gross_exposure * v / total for s, v in w.items()}
```
*(Note: with a cap that binds for all names, the renormalize keeps it proportional; acceptable for v1.)*

**Step 5 — commit:** `git commit -m "feat(factorranker): long-only top-N construction"`

---

## Task 7: Rebalance math — target weights vs holdings → share deltas (pure)

**Files:** Create `ba2_trade_platform/modules/experts/FactorRanker/portfolio.py` (pure function first; the DB/account-bound `FactorPortfolioManager` comes in Task 9); Test `tests/test_factorranker_rebalance.py`

**Step 1 — failing tests:**
```python
from ba2_trade_platform.modules.experts.FactorRanker.portfolio import rebalance_deltas

def test_rebalance_buys_new_and_sells_dropped():
    target = {"A": 0.5, "B": 0.5}          # weights
    held = {"A": 10.0, "C": 20.0}          # shares
    prices = {"A": 10.0, "B": 5.0, "C": 4.0}
    deltas = rebalance_deltas(target, held, prices, equity=1000.0)
    # target A = $500 -> 50 sh, have 10 -> +40 ; B = $500 -> 100 sh, have 0 -> +100
    # C not in target -> sell all 20
    assert deltas["A"] == 40.0
    assert deltas["B"] == 100.0
    assert deltas["C"] == -20.0

def test_no_trade_when_on_target():
    deltas = rebalance_deltas({"A": 1.0}, {"A": 100.0}, {"A": 10.0}, equity=1000.0)
    assert deltas.get("A", 0.0) == 0.0
```

**Step 3 — implement** (whole-share targets; deltas are signed share counts):
```python
from typing import Dict
import math

def rebalance_deltas(target_weights: Dict[str, float], held_shares: Dict[str, float],
                     prices: Dict[str, float], equity: float) -> Dict[str, float]:
    deltas: Dict[str, float] = {}
    symbols = set(target_weights) | set(held_shares)
    for s in symbols:
        price = prices.get(s)
        if price is None or price <= 0:
            # Can't price a held name we must exit -> still allow full sell using held qty
            if s in held_shares and target_weights.get(s, 0.0) == 0.0:
                deltas[s] = -float(held_shares[s])
            continue
        target_shares = math.floor((target_weights.get(s, 0.0) * equity) / price)
        delta = target_shares - float(held_shares.get(s, 0.0))
        if delta != 0.0:
            deltas[s] = float(delta)
    return deltas
```

**Step 5 — commit:** `git commit -m "feat(factorranker): rebalance delta math"`

---

## Task 8: Data-fetch adapters (bulk) — thin wrappers over existing providers

**Files:** Create `ba2_trade_platform/modules/experts/FactorRanker/data.py`; Test with mocks only where logic exists.

**Goal:** one function per factor returning the exact dict shapes Tasks 1-4 consume, fetched in **bulk** for the whole universe. Reuse:
- Prices → `FMPOHLCVProvider` (see `modules/dataproviders/ohlcv/FMPOHLCVProvider.py`); return `{symbol: pd.Series(close)}`.
- Value/quality fundamentals → `FMPCompanyOverviewProvider` / FMP key-metrics & cash-flow endpoints (follow the direct-`requests` pattern in `FMPRating._fetch_*`, with the same retry/timeout). Return the dict shapes from Tasks 3-4.
- PEAD → FMP earnings-surprises + earnings-calendar endpoints → `{symbol: {actual, estimate, estimate_std, days_since}}`.

**Tests:** keep these wrappers thin (mostly I/O). Unit-test only any pure transformation you add (e.g. "raw FMP record → factor input dict") by feeding a captured JSON sample and asserting the mapped dict. Do **not** hit the network in tests.

**Commit per wrapper:** `git commit -m "feat(factorranker): bulk data adapter for <factor>"`

---

## Task 9: `FactorPortfolioManager` — apply rebalance deltas as orders

**Files:** Modify `portfolio.py` (add the class); Test `tests/test_factorranker_portfolio_manager.py` (in-memory DB + conftest `MockAccount`).

**Behavior:** given target weights, read current holdings (`Transaction.get_current_open_qty()` for this expert's open transactions), get prices (`self.account.get_instrument_current_price`), compute `rebalance_deltas`, then submit one `TradingOrder` per non-zero delta (`OrderDirection.BUY` if >0 else SELL), `order_type=MARKET`, `data={"fixed_quantity": True}`, via `self.account.submit_order(order, is_closing_order=(delta<0))`. Mirror the structure of `PennyTradeManager.execute_exit`.

**Test (TDD):** build account def + expert instance + a couple of open transactions/orders via `tests/factories.py`; patch `get_account_instance_from_id`/`get_expert_instance_from_id` like `tests/test_penny_exit_staging.py` does; assert the submitted orders match expected deltas (buy new, sell dropped). Follow that test file's `_CapturingAccount` pattern.

**Commit:** `git commit -m "feat(factorranker): FactorPortfolioManager rebalance execution"`

---

## Task 10: The `FactorRanker` expert — settings, properties, `run_analysis("EXPERT")`

**Files:** Modify `modules/experts/FactorRanker/__init__.py`; Test `tests/test_factorranker_expert.py`

**Settings (`get_settings_definitions`)** — include at minimum:
- `instrument_selection_method` default `"expert"`, `should_expand_instrument_jobs` `False`, `schedules_open_positions` `False`.
- `factor_weights` (dict; default `{"momentum":1.0,"value":1.0,"quality":1.0,"pead":0.0}`).
- `top_n` (int, default 20), `weighting` (`"equal"|"score"`, default `"equal"`), `max_weight_per_name` (float, default 0.10), `gross_exposure` (float, default 1.0).
- `winsorize_pct` (default 0.02), `sector_neutralize` (bool, default False — implement in a later iteration; v1 may ignore).
- `min_price`, `min_dollar_volume` (liquidity guards), `pead_drift_window_days` (default 60), `hard_stop_pct` (optional).

**`run_analysis(self, symbol, market_analysis)`** orchestration (symbol will be `"EXPERT"`):
1. Set `market_analysis.status = RUNNING`.
2. `universe = self.get_enabled_instruments()`; apply `min_price`/`min_dollar_volume` guards.
3. For each enabled factor (weight > 0), call its `data.py` adapter then its `factors.py` calculator → `{symbol: raw}`.
4. `comp = composite_score(factor_values, factor_weights, winsorize_pct)`; `ranked = rank_symbols(comp)`.
5. `targets = long_only_top_n(ranked, comp, top_n, weighting, max_weight_per_name, gross_exposure)`.
6. `FactorPortfolioManager(self.id).rebalance(targets)`.
7. Write `AnalysisOutput`s: the ranked table (symbol, per-factor sub-scores, composite, rank, target weight, action) + a summary; set `market_analysis.state` with the book; status `COMPLETED`.
8. Wrap in try/except → on error set `FAILED` + error `AnalysisOutput` (copy FMPRating's pattern). Use `exc_info=True` only inside except.

**Test (TDD):** with mocked `data.py` adapters returning small fixtures and a patched account, call `run_analysis("EXPERT", ma)` and assert: composite/rank correct, `FactorPortfolioManager.rebalance` called with the expected target weights, `market_analysis.status == COMPLETED`, and the ranked book stored in state. (Mock `data.py` functions so no network.)

**Commit:** `git commit -m "feat(factorranker): expert orchestration run_analysis"`

---

## Task 11: `render_market_analysis` UI

**Files:** Modify `modules/experts/FactorRanker/__init__.py` (or a `ui.py` submodule).

Render from `market_analysis.state`: a header (rebalance date, # names, gross exposure), a **ranked universe table** (symbol · momentum z · value z · quality z · pead z · composite · rank · target weight · action), and a **current-vs-target** panel + the resulting trades. Reuse FMPRating's NiceGUI card/table style. Handle PENDING/RUNNING/FAILED/SKIPPED states like FMPRating. No unit test required (UI); manual-verify in Task 13.

**Commit:** `git commit -m "feat(factorranker): ranked-book market analysis UI"`

---

## Task 12: Register the expert

**Files:** Modify `modules/experts/__init__.py` (follow how FMPRating/PennyMomentumTrader are registered/discovered — check `get_expert_class`/registry).

Add FactorRanker to the registry so it appears in the UI expert list. Confirm `description()` returns a clear one-liner ("Configurable cross-sectional multi-factor equity ranker (momentum/value/quality/PEAD)").

**Commit:** `git commit -m "feat(factorranker): register expert"`

---

## Task 13: Integration test + manual verification

- **Integration test** (`tests/test_factorranker_integration.py`): in-memory DB + `MockAccount`; configure a FactorRanker instance over a 4-5 symbol enabled universe with mocked `data.py`; run `run_analysis("EXPERT")` twice (second run with shifted scores) and assert the rebalance sells names dropped from the top-N and buys new entrants.
- **Manual:** use the `/run` or `@verify` skill — create a FactorRanker on a paper account with ~5 enabled symbols, trigger a manual run, confirm the ranked-book UI renders and orders match target weights.
- Run full suite: `\.venv\Scripts\python.exe -m pytest -q` — no new failures.
- **Commit:** `git commit -m "test(factorranker): integration + verification"`

---

## Cross-cutting reminders
- **TDD:** every pure function gets a failing test first (Tasks 1-7, 9, 10). Watch it fail, then implement minimally.
- **DRY:** factors/combine/construction/rebalance are pure and shared; the expert only orchestrates.
- **YAGNI:** no long-short, no sector-neutralization v1 (leave the setting but it can no-op), no backtester.
- **No recommendations / no SmartRiskManager** — `MarketAnalysis` is the audit trail; `FactorPortfolioManager` executes.
- Bump `ba2_trade_platform/version.py` build number before any push.
