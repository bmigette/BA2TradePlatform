# Option Grid Max Stock Price (Gate-Only Screener) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a configurable max underlying stock price (default $100) to the options strategy matrix, enforced as a point-in-time per-bar entry gate via the existing screener metric-store machinery, without switching the grid off its static top-100 universe.

**Architecture:** "Gate-only" screener mode: the launcher attaches the parquet metric store to the optimize run as a `screener_opt` block tagged `"gate_only": true`; the optimization handler builds the per-trial `screener_runtime` entry gate (existing code path) but skips the candidate-bound universe restriction; the daily engine's existing per-bar gate (`daily_engine._screened_symbols_for_bar` → `metric_store.screen_universe_for_day`, which already filters `price_max` point-in-time) vetoes entries while the underlying trades above the cap.

**Tech Stack:** Python 3.11+, pytest, pandas/parquet metric store (`ba2_providers.screener.metric_store`), ba2-test launcher (`testplatform/ba2test_launcher.py`).

Spec: `docs/superpowers/specs/2026-07-29-option-grid-max-stock-price-design.md`

## Global Constraints

- Repo root: `C:/Users/basti/Documents/dev/BA2TradePlatform`. Use the repo venv python: `.venv/Scripts/python.exe` (Windows Git Bash).
- Backend tests must run with cwd `testplatform/backend` (they import `app...` and `ba2_providers`): `cd testplatform/backend && ../../.venv/Scripts/python.exe -m pytest tests/...`
- Provider tests run from repo root: `.venv/Scripts/python.exe -m pytest packages/providers/tests/...`
- Precedence for gate base settings (high → low): per-strategy `screener_gate_base` in `_OPTION_STRATS` > `--screener-base-json` > the `--max-stock-price` default block.
- `--max-stock-price` default is `100.0`; `0` disables the price filter (metric store treats `price_max <= 0` / absent as "not enforced").
- Gate-only mode must NOT: switch the run universe, add `screener:*` GA genes, set `apply_to_expert_settings`, or apply the candidate-bound universe restriction.
- `--screener-gate-store` combined with `--screener` is a hard error.
- Do not bump `APP_VERSION` (no live-app change; version bumps happen before push, separately).
- Commit after every task (frequent commits).

---

### Task 1: Characterization test — metric store `price_max` filter

The whole feature rests on `metric_store.screen_universe_for_day` filtering `price_max` point-in-time. That path currently has NO test. Add one before touching anything else.

**Files:**
- Test: `packages/providers/tests/test_screener_metric_store.py` (append; follow its existing `screen_universe_for_day` tests around line 354)

**Interfaces:**
- Consumes: `ba2_providers.screener.metric_store.screen_universe_for_day(store_df, day: str, settings: dict) -> list[str]` — settings keys are UNPREFIXED (`price_max`, not `screener_price_max`); a filter value of `0`/absent means "not enforced".
- Produces: nothing (test only).

- [ ] **Step 1: Write the failing test**

Append to `packages/providers/tests/test_screener_metric_store.py`:

```python
def test_screen_universe_for_day_price_max_point_in_time():
    """The option grid's max-stock-price gate rests on this: price_max excludes rows whose
    POINT-IN-TIME store price is above the cap — per scan date, not statically. A symbol cheap
    on one scan and expensive on another is admitted only on the cheap scan."""
    df = pd.DataFrame({
        "symbol": ["AAA", "AAA", "BBB", "BBB"],
        "date": ["2024-01-31", "2024-02-29", "2024-01-31", "2024-02-29"],
        "price": [80.0, 150.0, 40.0, 45.0],
        "market_cap": [5e9] * 4, "relative_volume": [1.0] * 4,
        "price_drop_pct": [0.0] * 4,
    })
    settings = {"price_max": 100.0, "max_stocks": 10000}
    # Jan: AAA at 80 passes. Feb: AAA at 150 is gated out; BBB stays under the cap on both.
    assert set(screen_universe_for_day(df, "2024-01-31", settings)) == {"AAA", "BBB"}
    assert screen_universe_for_day(df, "2024-02-29", settings) == ["BBB"]
    # 0 disables the filter entirely (the --max-stock-price 0 escape hatch).
    assert set(screen_universe_for_day(df, "2024-02-29",
                                       {"price_max": 0, "max_stocks": 10000})) == {"AAA", "BBB"}
```

Check the imports at the top of the file — `pd` and `screen_universe_for_day` are already imported by the existing tests (the file calls `ms.screen_universe_for_day` in some tests; if so, call it as `ms.screen_universe_for_day` instead and drop the bare-name import assumption).

- [ ] **Step 2: Run test to verify it passes (characterization of EXISTING behavior)**

Run: `.venv/Scripts/python.exe -m pytest packages/providers/tests/test_screener_metric_store.py -k price_max -v`
Expected: PASS. If it FAILS, the store does not implement `price_max` as designed — STOP and report; do not "fix" the store without flagging it.

- [ ] **Step 3: Commit**

```bash
git add packages/providers/tests/test_screener_metric_store.py
git commit -m "test: characterize metric store price_max point-in-time filter"
```

---

### Task 2: Optimization handler — `gate_only` hoisting + skip candidate bound

**Files:**
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py`
  - `_build_hoisted_state` (starts line 889)
  - `_build_daily_trial_config` (starts line 965; candidate-bound block at ~line 1082 `if not bypass:`)
- Test: `testplatform/backend/tests/backtest/test_screener_genes.py` (append; the existing `test_trial_config_carries_screener_runtime` at ~line 32 is the exact pattern to copy)

**Interfaces:**
- Consumes: run-level `backtest_cfg["screener_opt"]` dict (keys: `store`, `base_settings`, `cadence_days`, optional `apply_to_expert_settings`; NEW optional `gate_only: bool`).
- Produces: `hoisted["screener_gate_only"] -> bool` (consumed by `_build_daily_trial_config`); trial config `"screener_runtime"` and `"enabled_instruments"` (unchanged shapes).

- [ ] **Step 1: Write the failing test**

Append to `testplatform/backend/tests/backtest/test_screener_genes.py`:

```python
def test_trial_config_gate_only_keeps_static_universe(tmp_path):
    """GATE-ONLY screener mode (options grid max-stock-price): the store is attached PURELY as
    a per-bar entry gate. The trial config still carries screener_runtime (with the normalized
    price_max), but the candidate-bound universe restriction is SKIPPED — the static run
    universe passes through untouched even when a symbol NEVER passes the screen. Without
    gate_only the same config restricts enabled_instruments to the screened union (contrast)."""
    import pandas as pd
    from ba2_providers.screener import metric_store as ms

    store = str(tmp_path / "s")
    # AAA's store price (10) NEVER passes price_max 5 — under full screener mode the candidate
    # bound would drop it from enabled_instruments entirely.
    ms.write_partitions(store, pd.DataFrame({
        "symbol": ["AAA"], "date": ["2023-01-31"], "close": [10.0],
        "market_cap": [3e9], "relative_volume": [1.6], "price_drop_pct": [20.0],
        "sector": ["T"], "volume": [2e6], "price": [10.0]}))
    ms.clear_store_memo()

    backtest_cfg = {
        "backtest_id": 99,
        "start_date": "2023-01-02",
        "end_date": "2023-02-28",
        "enabled_instruments": ["AAA"],
        "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 7,
        "screener_opt": {
            "store": store,
            "base_settings": {"price_max": 5.0},
            "cadence_days": 7,
            "gate_only": True,
        },
    }
    decoded = {
        "tp": 5.0, "sl": 5.0,
        "expert_overrides": {},
        "screener_overrides": {},   # gate-only runs have NO screener genes
        "buy_tree": None, "sell_tree": None, "exit_rules": [],
    }

    hoisted = H._build_hoisted_state(backtest_cfg)
    assert hoisted["screener_gate_only"] is True

    cfg = H._build_daily_trial_config(backtest_cfg, decoded, hoisted)
    rt = cfg["screener_runtime"]
    assert rt["store"] == store
    assert rt["settings"]["price_max"] == 5.0          # normalized, carried to the engine gate
    assert cfg["enabled_instruments"] == ["AAA"]       # candidate bound SKIPPED

    # Contrast: same block WITHOUT gate_only applies the candidate bound (existing behavior) —
    # AAA never passes price_max 5, so it is restricted out of the loaded universe.
    backtest_cfg["screener_opt"] = {
        "store": store, "base_settings": {"price_max": 5.0}, "cadence_days": 7,
    }
    hoisted2 = H._build_hoisted_state(backtest_cfg)
    assert hoisted2["screener_gate_only"] is False
    cfg2 = H._build_daily_trial_config(backtest_cfg, decoded, hoisted2)
    assert cfg2["enabled_instruments"] == []
```

Note: the file already does `import app.services.strategy_optimization_handler as H` at the top — reuse it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd testplatform/backend && ../../../.venv/Scripts/python.exe -m pytest tests/backtest/test_screener_genes.py -k gate_only -v`
(If `../../../.venv` doesn't resolve from there, use the absolute path `C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe`.)
Expected: FAIL — `KeyError: 'screener_gate_only'` (the hoisted key does not exist yet).

- [ ] **Step 3: Implement `gate_only` in `_build_hoisted_state`**

In `testplatform/backend/app/services/strategy_optimization_handler.py`, inside `_build_hoisted_state`, immediately after the existing line `hoisted["screener_apply_to_expert_settings"] = bool(screener_opt.get("apply_to_expert_settings"))`, add:

```python
        # GATE-ONLY mode (options grid max-stock-price): the store rides along PURELY as a
        # per-bar entry gate — _build_daily_trial_config skips its candidate-bound universe
        # restriction so the static run universe is kept byte-identical.
        hoisted["screener_gate_only"] = bool(screener_opt.get("gate_only"))
```

- [ ] **Step 4: Implement the candidate-bound skip in `_build_daily_trial_config`**

In the same file, find the candidate-bound guard (currently):

```python
        if not bypass:
```

inside the `if hoisted and hoisted.get("screener_store"):` block (just above the `try:` that computes `_union`). Change it to:

```python
        if not bypass and not hoisted.get("screener_gate_only"):
```

That is the entire change — the `screener_runtime` block above it already builds the gate from base settings whenever `hoisted["screener_store"]` is set, and gate-only runs have empty `screener_overrides` (no genes), so the effective settings are exactly the launcher's base block.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd testplatform/backend && C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe -m pytest tests/backtest/test_screener_genes.py -v`
Expected: PASS (all tests in the file, old and new — the old ones guard against regressions in full `--screener` mode).

- [ ] **Step 6: Commit**

```bash
git add testplatform/backend/app/services/strategy_optimization_handler.py testplatform/backend/tests/backtest/test_screener_genes.py
git commit -m "feat: gate_only screener mode skips candidate-bound universe restriction"
```

---

### Task 3: Engine per-bar gate test with `price_max`

Prove the daily engine's existing per-bar entry gate actually vetoes entries on `price_max` point-in-time (this is the function the gate-only mode feeds). The gate sits at entry level (`daily_engine.py` ~line 578, `entry_universe` intersection), so it covers option entries too — no option-specific engine test needed.

**Files:**
- Test: `testplatform/backend/tests/backtest/test_single_bt_screener_metric_store.py` (append; reuse the `_screened_symbols_for_bar` import already at the top of the file and the store-building style of its `_build_store`)

**Interfaces:**
- Consumes: `app.services.backtest.daily_engine._screened_symbols_for_bar(screener_runtime: dict | None, as_of_dt: datetime, screened_cache: dict) -> list[str] | None` — `screener_runtime = {"store": <path>, "settings": <normalized unprefixed dict>, "cadence_days": int}`; resolves to the latest scan date <= the bar.
- Produces: nothing (test only).

- [ ] **Step 1: Write the failing test**

Append to `testplatform/backend/tests/backtest/test_single_bt_screener_metric_store.py`:

```python
def test_per_bar_gate_filters_on_price_max_point_in_time(tmp_path):
    """The engine's per-bar entry gate applies price_max POINT-IN-TIME: A is priced 120 on the
    Jan scan (gated out) and 80 on the Feb scan (admitted). This is the exact semantics the
    options grid's max-stock-price gate relies on — a stock crossing the cap mid-backtest is
    only excluded while above it."""
    rows = [
        {"date": "2024-01-01", "symbol": "A", "market_cap": 2e9, "price": 120.0, "volume": 1e6,
         "relative_volume": 1.5, "price_drop_pct": 0.0, "weinstein_stage": 2, "sector": "X", "close": 120.0},
        {"date": "2024-02-01", "symbol": "A", "market_cap": 2e9, "price": 80.0, "volume": 1e6,
         "relative_volume": 1.5, "price_drop_pct": 0.0, "weinstein_stage": 2, "sector": "X", "close": 80.0},
    ]
    store = str(tmp_path / "ms")
    ms.write_partitions(store, pd.DataFrame(rows))
    ms.clear_store_memo()

    rt = {"store": store, "settings": {"price_max": 100.0, "max_stocks": 10000},
          "cadence_days": 7}
    cache = {}
    # Bar between the two scans resolves to the Jan scan (price 120 > 100): gated out.
    assert _screened_symbols_for_bar(rt, datetime(2024, 1, 15, tzinfo=timezone.utc), cache) == []
    # Bar after the Feb scan (price 80 <= 100): admitted again.
    assert _screened_symbols_for_bar(rt, datetime(2024, 2, 15, tzinfo=timezone.utc), cache) == ["A"]
```

- [ ] **Step 2: Run test to verify it passes (characterization of EXISTING behavior)**

Run: `cd testplatform/backend && C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe -m pytest tests/backtest/test_single_bt_screener_metric_store.py -k price_max -v`
Expected: PASS. If it FAILS, the engine gate does not apply `price_max` as designed — STOP and report.

- [ ] **Step 3: Commit**

```bash
git add testplatform/backend/tests/backtest/test_single_bt_screener_metric_store.py
git commit -m "test: engine per-bar screener gate applies price_max point-in-time"
```

---

### Task 4: Launcher — `--screener-gate-store` / `--max-stock-price` + per-strategy override

**Files:**
- Modify: `testplatform/ba2test_launcher.py`
  - near line 2229 (`_OPTION_MIN_VOLUME_DEFAULT` block): new module default
  - after the `_OPTION_GROUPS` definitions (~line 2190): two new helper functions
  - in `_cmd_optimize` (starts line 2516), immediately AFTER the `if getattr(args, "screener", False):` block ends (~line 2700, before the `# Target-anchored variant (S4)` comment): the gate-only attach block
  - in the `optimize` argparse section, after `--screener-cap-band` (~line 3645): the two new flags
- Test: `testplatform/backend/tests/test_launcher_screener_gate.py` (create; model the sys.path header on `testplatform/backend/tests/test_option_min_volume_wiring.py`)

**Interfaces:**
- Consumes: parsed optimize args (`args.screener_gate_store`, `args.max_stock_price`, `args.screener`, `args.screener_base_json`, `args.screener_cadence_days`, `args.strategy`); `_OPTION_STRATS` member dicts (new optional `screener_gate_base` key).
- Produces (used by Task 5 and `_cmd_optimize`):
  - `_screener_gate_base_for_strategy(kind: str) -> dict` — per-strategy gate overrides; merges active group members for `OS1`-`OS4`; `{}` for equity/unknown keys.
  - `_screener_gate_opt_block(args, strategy_key: str) -> dict | None` — the full `screener_opt` block (`store`, `base_settings`, `cadence_days`, `apply_to_expert_settings: False`, `gate_only: True`) or `None` when the flag is unset. Calls `sys.exit` on the `--screener` conflict.
  - `_MAX_STOCK_PRICE_DEFAULT = 100.0`.

- [ ] **Step 1: Write the failing test**

Create `testplatform/backend/tests/test_launcher_screener_gate.py`:

```python
"""Gate-only screener mode wiring (options grid max-stock-price, 2026-07-29).

Asserts the WIRING, not the mechanism (the metric store / engine gate have their own tests):
the launcher's screener_opt block carries gate_only + the configured price cap, honors the
precedence chain, and refuses to combine with full --screener mode.
"""
import sys, os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import pytest

import ba2test_launcher as L


def _args(**over):
    base = dict(
        screener_gate_store="/tmp/store.parquet",
        max_stock_price=100.0,
        screener=False,
        screener_base_json=None,
        screener_cadence_days=7,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_no_flag_no_block():
    assert L._screener_gate_opt_block(_args(screener_gate_store=None), "O_IC") is None


def test_block_is_gate_only_with_default_cap():
    blk = L._screener_gate_opt_block(_args(), "O_IC")
    assert blk["gate_only"] is True
    assert blk["apply_to_expert_settings"] is False
    assert blk["store"] == "/tmp/store.parquet"
    assert blk["cadence_days"] == 7
    # Default: everything most-admitting except the $100 price cap.
    assert blk["base_settings"]["price_max"] == 100.0
    assert blk["base_settings"]["market_cap_min"] == 0.0
    assert blk["base_settings"]["relative_volume_min"] == 0.0
    assert blk["base_settings"]["price_drop_pct"] == 0.0


def test_max_stock_price_configurable_and_zero_disables():
    blk = L._screener_gate_opt_block(_args(max_stock_price=60.0), "O_IC")
    assert blk["base_settings"]["price_max"] == 60.0
    blk0 = L._screener_gate_opt_block(_args(max_stock_price=0.0), "O_IC")
    assert "price_max" not in blk0["base_settings"]


def test_per_strategy_override_wins(monkeypatch):
    monkeypatch.setitem(L._OPTION_STRATS["O_CSP"], "screener_gate_base", {"price_max": 40.0})
    blk = L._screener_gate_opt_block(_args(), "O_CSP")
    assert blk["base_settings"]["price_max"] == 40.0
    # A strategy without an override keeps the CLI value.
    assert L._screener_gate_opt_block(_args(), "O_IC")["base_settings"]["price_max"] == 100.0


def test_group_merges_active_member_overrides(monkeypatch):
    monkeypatch.setitem(L._OPTION_STRATS["O_SSTG"], "screener_gate_base", {"price_max": 55.0})
    # OS2's active members include O_SSTG; the merge picks its override up.
    assert L._screener_gate_base_for_strategy("OS2")["price_max"] == 55.0


def test_base_json_beats_cli_default_and_loses_to_strategy(tmp_path, monkeypatch):
    import json
    p = tmp_path / "base.json"
    p.write_text(json.dumps({"price_max": 77.0, "relative_volume_min": 1.5}))
    blk = L._screener_gate_opt_block(_args(screener_base_json=str(p)), "O_IC")
    assert blk["base_settings"]["price_max"] == 77.0
    assert blk["base_settings"]["relative_volume_min"] == 1.5
    monkeypatch.setitem(L._OPTION_STRATS["O_IC"], "screener_gate_base", {"price_max": 33.0})
    blk2 = L._screener_gate_opt_block(_args(screener_base_json=str(p)), "O_IC")
    assert blk2["base_settings"]["price_max"] == 33.0


def test_combining_with_full_screener_mode_is_a_hard_error():
    with pytest.raises(SystemExit):
        L._screener_gate_opt_block(_args(screener=True, screener_store="/tmp/x"), "O_IC")


def test_equity_and_unknown_keys_have_no_strategy_override():
    assert L._screener_gate_base_for_strategy("O_STK") == {}
    assert L._screener_gate_base_for_strategy("S1") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd testplatform/backend && C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe -m pytest tests/test_launcher_screener_gate.py -v`
Expected: FAIL — `AttributeError: module 'ba2test_launcher' has no attribute '_screener_gate_opt_block'`.

- [ ] **Step 3: Add the module default and helpers to the launcher**

In `testplatform/ba2test_launcher.py`, right after the `_OPTION_MIN_VOLUME = _OPTION_MIN_VOLUME_DEFAULT` line (~line 2231), add:

```python
# Default cap on the UNDERLYING price for the gate-only screener entry gate
# (--screener-gate-store): the options grid runs at $20k, where full-notional structures on
# $100+ underlyings reserve more than the account (see the reserve table above
# _FULL_NOTIONAL_OPTION_KINDS). 100 keeps every grid structure openable on gated names.
_MAX_STOCK_PRICE_DEFAULT = 100.0
```

Then, immediately after the `_option_entry_action_for` function (~line 2246), add:

```python
def _screener_gate_base_for_strategy(kind: str) -> dict:
    """Per-strategy gate-only screener overrides declared on _OPTION_STRATS members
    (``screener_gate_base``). A group (OS1-4) merges its ACTIVE members' dicts in order
    (later member wins). Equity/unknown keys -> {}."""
    if kind in _OPTION_STRATS:
        return dict(_OPTION_STRATS[kind].get("screener_gate_base") or {})
    merged: dict = {}
    for member in _OPTION_GROUPS.get(kind, []):
        merged.update(_OPTION_STRATS[member].get("screener_gate_base") or {})
    return merged


def _screener_gate_opt_block(args, strategy_key: str) -> "dict | None":
    """The gate-only screener_opt block for --screener-gate-store (None when the flag is unset).

    Gate-only = the metric store is attached PURELY as a per-bar entry gate: the run universe
    stays the static --universe, no screener:* genes enter the search, and the optimization
    handler skips its candidate-bound universe restriction. Base settings are most-admitting
    except the price cap, so ONLY --max-stock-price bites unless --screener-base-json or a
    per-strategy screener_gate_base says otherwise. Precedence (high -> low): per-strategy
    screener_gate_base > --screener-base-json > the --max-stock-price default block.
    """
    store = getattr(args, "screener_gate_store", None)
    if not store:
        return None
    if getattr(args, "screener", False):
        sys.exit("optimize: --screener-gate-store cannot be combined with --screener "
                 "(full screener mode already gates entries; pick one).")
    base: dict = {
        "market_cap_min": 0.0,
        "relative_volume_min": 0.0,
        "price_drop_pct": 0.0,
        "weinstein_stage2_only": 0,
        "max_stocks": 10000,
    }
    max_price = float(getattr(args, "max_stock_price", _MAX_STOCK_PRICE_DEFAULT))
    if max_price > 0:
        base["price_max"] = max_price
    if getattr(args, "screener_base_json", None):
        with open(args.screener_base_json) as _f:
            base.update(json.load(_f))
    base.update(_screener_gate_base_for_strategy(strategy_key))
    return {
        "store": store,
        "base_settings": base,
        "cadence_days": int(args.screener_cadence_days),
        "apply_to_expert_settings": False,
        "gate_only": True,
    }
```

- [ ] **Step 4: Attach the block in `_cmd_optimize`**

In `_cmd_optimize`, immediately AFTER the end of the `if getattr(args, "screener", False):` block (its last line is `screener_genes = {f"screener:{k}": v for k, v in _scr_opt.items()}`, ~line 2700) and BEFORE the `# Target-anchored variant (S4):` comment, add:

```python
        # GATE-ONLY screener (--screener-gate-store): attach the metric store PURELY as a
        # per-bar entry gate (the options grid's max-stock-price cap) — the run universe stays
        # the static --universe and NO screener genes enter the search. The optimization
        # handler reads gate_only to skip its candidate-bound universe restriction.
        _gate_opt = _screener_gate_opt_block(args, args.strategy)
        if _gate_opt:
            backtest_block["screener_opt"] = _gate_opt
            from ba2_providers.screener import metric_store as _gate_ms
            _gate_df = _gate_ms.load_store(_gate_opt["store"])
            if _gate_df.empty:
                sys.exit(f"optimize: --screener-gate-store {_gate_opt['store']!r} has no symbols")
            # Coverage guard: a symbol with no store row can NEVER pass the per-bar gate, so a
            # store that doesn't cover the static universe silently starves those names. Warn
            # loud instead of trading a quietly-shrunk universe.
            _covered = set(_gate_df["symbol"].unique())
            _static = list(backtest_block["enabled_instruments"])
            _uncovered = [s for s in _static if s not in _covered]
            if len(_uncovered) > max(1, len(_static) // 10):
                print(f"optimize: WARNING screener-gate store covers only "
                      f"{len(_static) - len(_uncovered)}/{len(_static)} universe symbols; the "
                      f"uncovered {len(_uncovered)} can NEVER enter — rebuild/extend the store "
                      f"(ba2-test build-screener-metrics) for this universe.")
```

Verify while editing: `backtest_block["enabled_instruments"]` is already set to the static universe before this point in `_cmd_optimize` (it is — the `--screener` block REPLACES it, and gate-only is mutually exclusive with `--screener`).

- [ ] **Step 5: Register the CLI flags**

In the `optimize` parser section, immediately after the `--screener-cap-band` argument (~line 3645), add:

```python
    op.add_argument("--screener-gate-store", default=None,
                    help="GATE-ONLY screener mode: path to the parquet metric store used PURELY "
                         "as a per-bar entry gate — the run universe stays the static "
                         "--universe and no screener:* genes enter the search. Pairs with "
                         "--max-stock-price so the options grid skips underlyings a $20k "
                         "account cannot structure. Cannot be combined with --screener.")
    op.add_argument("--max-stock-price", type=float, default=_MAX_STOCK_PRICE_DEFAULT,
                    help="Max UNDERLYING price admitted by the gate-only screener entry gate "
                         "(default 100 — the $20k-account cap for the options grid). "
                         "Point-in-time: a name above the cap is only excluded while above it. "
                         "0 disables the price filter. Per-strategy overrides live in "
                         "_OPTION_STRATS[].screener_gate_base.")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd testplatform/backend && C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe -m pytest tests/test_launcher_screener_gate.py tests/test_option_min_volume_wiring.py -v`
Expected: PASS (the min-volume wiring tests guard against launcher import breakage).

- [ ] **Step 7: Commit**

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/test_launcher_screener_gate.py
git commit -m "feat: gate-only screener entry gate with configurable max stock price for the options grid"
```

---

### Task 5: Matrix driver passthrough (`tools/run_options_matrix.py`)

**Files:**
- Modify: `tools/run_options_matrix.py` (argparse block ~line 86-148; command construction ~line 168-198)
- Test: `testplatform/backend/tests/test_run_options_matrix_gate.py` (create)

**Interfaces:**
- Consumes: `args.screener_gate_store` (str | None), `args.max_stock_price` (float, default 100.0).
- Produces: `_gate_passthrough(args) -> list[str]` — extra CLI tokens for the `ba2-test optimize` command; `[]` when no gate store is set. The launcher's flags it emits: `--screener-gate-store`, `--max-stock-price`.

- [ ] **Step 1: Write the failing test**

Create `testplatform/backend/tests/test_run_options_matrix_gate.py`:

```python
"""run_options_matrix passes the gate-only screener flags through to every optimize job."""
import sys, os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools"))

import run_options_matrix as M


def test_gate_passthrough_empty_without_store():
    assert M._gate_passthrough(SimpleNamespace(screener_gate_store=None,
                                               max_stock_price=100.0)) == []


def test_gate_passthrough_emits_both_flags():
    out = M._gate_passthrough(SimpleNamespace(screener_gate_store="/tmp/store.parquet",
                                              max_stock_price=80.0))
    assert out == ["--screener-gate-store", "/tmp/store.parquet",
                   "--max-stock-price", "80.0"]
```

Note: importing `run_options_matrix` is side-effect free (argparse runs inside `main()`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd testplatform/backend && C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe -m pytest tests/test_run_options_matrix_gate.py -v`
Expected: FAIL — `AttributeError: module 'run_options_matrix' has no attribute '_gate_passthrough'`.

- [ ] **Step 3: Implement the passthrough**

In `tools/run_options_matrix.py`, add to the argparse block (after the `--dry-run` argument, ~line 148):

```python
    ap.add_argument("--screener-gate-store", default=None,
                    help="Attach this parquet metric store as a GATE-ONLY per-bar entry gate on "
                         "EVERY job (passes --screener-gate-store/--max-stock-price through to "
                         "ba2-test optimize). The store must cover the options universe.")
    ap.add_argument("--max-stock-price", type=float, default=100.0,
                    help="Max underlying price for the gate-only entry gate (default 100 — the "
                         "$20k-account cap). 0 disables the price filter.")
```

Add the helper above `main()`:

```python
def _gate_passthrough(args) -> list:
    """Extra optimize CLI tokens for the gate-only screener entry gate ([] when unset)."""
    if not args.screener_gate_store:
        return []
    return ["--screener-gate-store", args.screener_gate_store,
            "--max-stock-price", str(args.max_stock_price)]
```

And in the command construction inside `main()`, immediately after the line building `cmd` (after `"--run-schedule", "daily", "--name", name, "--parallel", str(args.parallel)]`, ~line 180), add:

```python
        cmd += _gate_passthrough(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd testplatform/backend && C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe -m pytest tests/test_run_options_matrix_gate.py -v`
Expected: PASS.

- [ ] **Step 5: Manual smoke check**

Run: `.venv/Scripts/python.exe tools/run_options_matrix.py --dry-run --screener-gate-store /tmp/nope.parquet`
Expected: job list prints as usual (dry-run doesn't validate the store path). Then verify the real flag reaches the launcher help: `.venv/Scripts/python.exe testplatform/ba2test_launcher.py optimize --help | grep -A2 "screener-gate-store"` shows the new flag.

- [ ] **Step 6: Commit**

```bash
git add tools/run_options_matrix.py testplatform/backend/tests/test_run_options_matrix_gate.py
git commit -m "feat: options matrix driver passes gate-only screener flags through"
```

---

### Task 6: Full regression sweep

- [ ] **Step 1: Run the affected test suites**

```bash
cd testplatform/backend && C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe -m pytest tests/backtest/test_screener_genes.py tests/backtest/test_single_bt_screener_metric_store.py tests/backtest/test_screener_opt_e2e.py tests/test_launcher_screener_gate.py tests/test_run_options_matrix_gate.py tests/test_option_min_volume_wiring.py tests/test_strategy_optimization_handler.py -v
```

Expected: all PASS. `test_screener_opt_e2e.py` and `test_strategy_optimization_handler.py` guard the full `--screener` path the `gate_only` edits touch.

- [ ] **Step 2: Run the providers screener tests**

```bash
C:/Users/basti/Documents/dev/BA2TradePlatform/.venv/Scripts/python.exe -m pytest packages/providers/tests/test_screener_metric_store.py packages/providers/tests/test_historical_screener.py -v
```

Expected: all PASS.

- [ ] **Step 3: Commit (only if the sweep produced fixes)**

```bash
git add -A
git commit -m "test: regression sweep for gate-only screener mode"
```
